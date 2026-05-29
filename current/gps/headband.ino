#include <WiFi.h>
#include <esp_wifi.h>
#include <esp_now.h>
#include <math.h>
#include <mbedtls/md.h>
#include <HardwareSerial.h>

// Shared HMAC key for message authentication (must match hub.ino)
static const uint8_t HMAC_KEY[32] = {
  0x7A, 0x4E, 0x2B, 0x91, 0xF3, 0x8C, 0x5D, 0xE7,
  0x1F, 0x6A, 0xC4, 0x83, 0x9B, 0x2E, 0xD5, 0x70,
  0xA8, 0x3F, 0x6C, 0x19, 0xE2, 0x7D, 0x4B, 0x95,
  0x0C, 0x68, 0xB1, 0xF9, 0x3A, 0x57, 0xDE, 0x84
};

// Replay protection
static uint32_t lastSeqNum = 0;
static bool seqInitialized = false;
static const uint32_t SEQ_WINDOW = 100;

// RTCM handling
#define RTCM_ESPNOW_PREFIX 0xD3

// RTK GPS serial connection (UART2)
#define GPS_RX_PIN 21
#define GPS_TX_PIN 22
#define GPS_RST_PIN 4
#define GPS_CEN_PIN 2
#define GPS_WUP_PIN 15
HardwareSerial GPSSerial(2);

static const long GPS_BAUDS[] = {460800, 115200, 230400, 921600, 38400, 9600};
static const int  GPS_BAUD_COUNT = sizeof(GPS_BAUDS) / sizeof(GPS_BAUDS[0]);
static long       gpsActiveBaud = 0;

#define WIFI_CHANNEL 1

// RTCM reassembly buffer
#define RTCM_BUFFER_SIZE 1024
static uint8_t rtcmBuffer[RTCM_BUFFER_SIZE];
static size_t rtcmBufferLen = 0;
static uint8_t expectedChunk = 0;
static uint8_t totalChunks = 0;
static unsigned long lastRtcmTime = 0;
#define RTCM_TIMEOUT_MS 1000

// Deferred RTCM write buffer
#define RTCM_WRITE_BUF_SIZE 1024
static uint8_t  rtcmWriteBuf[RTCM_WRITE_BUF_SIZE];
static volatile size_t rtcmWriteLen = 0;

// RTCM diagnostic counters
static unsigned long rtcmBytesReceived = 0;
static unsigned long rtcmBytesToGPS = 0;
static unsigned long rtcmMsgCount = 0;

const int MOTOR_PIN_5 = 5;
const int MOTOR_PIN_18 = 18;
const int MOTOR_PIN_19 = 19;
const int MOTOR_PIN_23 = 23;

const String ROW_NUM = "1";
const String COL_NUM = "3";

// Broadcast MAC for ESP-NOW send (status reports back to hub)
uint8_t broadcastAddress[] = { 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF };

// Status broadcast interval and stagger
#define STATUS_INTERVAL 5000
static unsigned long lastStatusSend = 0;
static unsigned long statusOffset = 0;

// Live GPS state
static double cur_LAT = 0.0;
static double cur_LON = 0.0;
static int    cur_IMU = 0;
static uint8_t gps_fix_quality = 0;
static bool  gps_valid = false;
static unsigned long lastGpsTime = 0;

// NMEA sentence buffer
#define NMEA_BUF_SIZE 256
static char nmeaBuf[NMEA_BUF_SIZE];
static uint16_t nmeaIdx = 0;

// GPS status print interval
static unsigned long lastGpsPrint = 0;
#define GPS_PRINT_INTERVAL 5000

// GPS UART diagnostic counters
static unsigned long gpsRxByteCount = 0;
static unsigned long gpsDollarCount = 0;
static unsigned long gpsNmeaParsed = 0;
static bool gpsDiagDone = false;

// Tolerance for GPS comparison
const double GPS_TOLERANCE = 0.000003;

// IMU tolerance thresholds
const int IMU_DEADZONE = 5;
const int IMU_SOFT_LIMIT = 15;

// ─────────────── Baud rate auto-detection ────────────────────
long detectGPSBaud() {
  for (int i = 0; i < GPS_BAUD_COUNT; i++) {
    long baud = GPS_BAUDS[i];
    Serial.printf("Probing GPS at %ld baud...\n", baud);
    GPSSerial.end();
    GPSSerial.setRxBufferSize(2048);
    GPSSerial.begin(baud, SERIAL_8N1, GPS_RX_PIN, GPS_TX_PIN);
    while (GPSSerial.available()) GPSSerial.read();

    unsigned long start = millis();
    int asciiCount = 0, totalBytes = 0;
    bool sawDollar = false;
    while (millis() - start < 1500) {
      if (GPSSerial.available()) {
        char c = GPSSerial.read(); totalBytes++;
        if (c == '$') sawDollar = true;
        if (c >= 0x20 && c < 0x7F) asciiCount++;
      }
      if (!GPSSerial.available()) delayMicroseconds(100);
    }
    if (sawDollar || (totalBytes > 10 && asciiCount > totalBytes / 2)) {
      Serial.printf("GPS detected at %ld baud\n", baud);
      GPSSerial.end(); GPSSerial.setRxBufferSize(2048);
      GPSSerial.begin(baud, SERIAL_8N1, GPS_RX_PIN, GPS_TX_PIN);
      return baud;
    }
  }
  Serial.println("WARNING: No GPS module detected!");
  GPSSerial.end(); GPSSerial.setRxBufferSize(2048);
  GPSSerial.begin(460800, SERIAL_8N1, GPS_RX_PIN, GPS_TX_PIN);
  return 0;
}

// ─────────────── NMEA parsing ───────────────────────────────
String nmeaField(const char* sentence, int fieldNum) {
  int current = 0; const char* start = sentence;
  for (const char* p = sentence; *p; p++) {
    if (*p == ',' || *p == '*') {
      if (current == fieldNum) return String(sentence).substring(start - sentence, p - sentence);
      current++; start = p + 1;
    }
  }
  if (current == fieldNum) return String(start);
  return "";
}

double nmeaToDecimal(const char* raw, int rawLen, const String& dir) {
  if (rawLen == 0) return 0.0;
  int dotPos = -1;
  for (int i = 0; i < rawLen; i++) { if (raw[i] == '.') { dotPos = i; break; } }
  if (dotPos < 0) return 0.0;
  int degLen = dotPos - 2;
  if (degLen < 1) return 0.0;
  char tmp[24]; int cpLen = (rawLen < 23) ? rawLen : 23;
  memcpy(tmp, raw, cpLen); tmp[cpLen] = '\0';
  char degBuf[8]; memcpy(degBuf, tmp, degLen); degBuf[degLen] = '\0';
  double decimal = strtod(degBuf, NULL) + (strtod(tmp + degLen, NULL) / 60.0);
  if (dir == "S" || dir == "W") decimal = -decimal;
  return decimal;
}

const char* fixQualityStr(uint8_t q) {
  switch (q) {
    case 0: return "No fix"; case 1: return "GPS"; case 2: return "DGPS";
    case 4: return "RTK Fixed"; case 5: return "RTK Float"; default: return "Unknown";
  }
}

void parseNMEA(const char* sentence) {
  if (strlen(sentence) < 7) return;
  const char* type = sentence + 3;

  if (strncmp(type, "GGA,", 4) == 0) {
    String lat_raw = nmeaField(sentence, 2), lat_dir = nmeaField(sentence, 3);
    String lon_raw = nmeaField(sentence, 4), lon_dir = nmeaField(sentence, 5);
    String quality = nmeaField(sentence, 6);
    uint8_t q = quality.toInt();
    gps_fix_quality = q;
    if (q > 0 && lat_raw.length() > 0 && lon_raw.length() > 0) {
      cur_LAT = nmeaToDecimal(lat_raw.c_str(), lat_raw.length(), lat_dir);
      cur_LON = nmeaToDecimal(lon_raw.c_str(), lon_raw.length(), lon_dir);
      gps_valid = true; lastGpsTime = millis();
    }
  } else if (strncmp(type, "RMC,", 4) == 0) {
    String status = nmeaField(sentence, 2);
    if (status == "A") {
      String lat_raw = nmeaField(sentence, 3), lat_dir = nmeaField(sentence, 4);
      String lon_raw = nmeaField(sentence, 5), lon_dir = nmeaField(sentence, 6);
      String course = nmeaField(sentence, 8);
      if (lat_raw.length() > 0 && lon_raw.length() > 0) {
        cur_LAT = nmeaToDecimal(lat_raw.c_str(), lat_raw.length(), lat_dir);
        cur_LON = nmeaToDecimal(lon_raw.c_str(), lon_raw.length(), lon_dir);
        gps_valid = true; lastGpsTime = millis();
      }
      if (course.length() > 0) cur_IMU = (int)course.toFloat();
    }
  }
}

void readGPS() {
  while (GPSSerial.available()) {
    char c = GPSSerial.read(); gpsRxByteCount++;
    if (!gpsDiagDone && gpsRxByteCount <= 64) {
      Serial.printf("GPS_RAW[%lu]: 0x%02X '%c'\n", gpsRxByteCount, (uint8_t)c, (c >= 0x20 && c < 0x7F) ? c : '.');
      if (gpsRxByteCount == 64) { gpsDiagDone = true; Serial.println("--- end GPS raw dump ---"); }
    }
    if (c == '$') { gpsDollarCount++; nmeaIdx = 0; nmeaBuf[nmeaIdx++] = c; }
    else if (c == '\n' || c == '\r') {
      if (nmeaIdx > 5) { nmeaBuf[nmeaIdx] = '\0'; gpsNmeaParsed++; parseNMEA(nmeaBuf); }
      nmeaIdx = 0;
    } else if (nmeaIdx < NMEA_BUF_SIZE - 1) { nmeaBuf[nmeaIdx++] = c; }
  }
}

// ─────────────── Motor helpers ────────────────────
void buzz(int pin, bool on) { digitalWrite(pin, on ? HIGH : LOW); }
void buzzPulse(int pin, int duration_ms) { buzz(pin, HIGH); delay(duration_ms); buzz(pin, LOW); }
void buzzSoft(int pin) { buzzPulse(pin, 100); }
void buzzHard(int pin) { for (int i = 0; i < 2; ++i) { buzzPulse(pin, 100); delay(50); } }

void buzzRotateLeft(bool hard) {
  if (hard) { buzzHard(MOTOR_PIN_18); delay(50); buzzHard(MOTOR_PIN_5); }
  else { buzzSoft(MOTOR_PIN_18); delay(50); buzzSoft(MOTOR_PIN_5); }
}
void buzzRotateRight(bool hard) {
  if (hard) { buzzHard(MOTOR_PIN_18); delay(50); buzzHard(MOTOR_PIN_19); }
  else { buzzSoft(MOTOR_PIN_18); delay(50); buzzSoft(MOTOR_PIN_19); }
}

int normalizeHeading(int diff) {
  while (diff > 180) diff -= 360;
  while (diff < -180) diff += 360;
  return diff;
}

// ─────────────── RTCM chunk reassembly ────────────────────
bool handleRTCMChunk(const uint8_t* data, int len) {
  if (len < 3) return false;
  uint8_t chunkInfo = data[1];
  uint8_t chunkNum = (chunkInfo >> 4) & 0x0F;
  uint8_t chunks = chunkInfo & 0x0F;
  const uint8_t* payload = data + 2;
  size_t payloadLen = len - 2;
  unsigned long now = millis();

  if (chunkNum == 0 || (now - lastRtcmTime > RTCM_TIMEOUT_MS)) {
    rtcmBufferLen = 0; expectedChunk = 0; totalChunks = chunks;
  }
  lastRtcmTime = now;

  if (chunkNum != expectedChunk) {
    rtcmBufferLen = 0; expectedChunk = 0; return false;
  }
  if (rtcmBufferLen + payloadLen > RTCM_BUFFER_SIZE) {
    rtcmBufferLen = 0; expectedChunk = 0; return false;
  }

  memcpy(rtcmBuffer + rtcmBufferLen, payload, payloadLen);
  rtcmBufferLen += payloadLen;
  rtcmBytesReceived += payloadLen;
  expectedChunk++;

  if (expectedChunk >= totalChunks) {
    rtcmMsgCount++;
    if (rtcmWriteLen == 0 && rtcmBufferLen <= RTCM_WRITE_BUF_SIZE) {
      memcpy(rtcmWriteBuf, rtcmBuffer, rtcmBufferLen);
      rtcmWriteLen = rtcmBufferLen;
    } else { Serial.println("RTCM: write buffer busy, dropping"); }
    rtcmBufferLen = 0; expectedChunk = 0; totalChunks = 0;
    return true;
  }
  return false;
}

// ─────────────── HMAC verification ────────────────────
String generateHMAC(uint32_t seq, const String& payload) {
  String message = String(seq) + ":" + payload;
  uint8_t hmacResult[32];
  mbedtls_md_context_t ctx;
  mbedtls_md_init(&ctx);
  mbedtls_md_setup(&ctx, mbedtls_md_info_from_type(MBEDTLS_MD_SHA256), 1);
  mbedtls_md_hmac_starts(&ctx, HMAC_KEY, sizeof(HMAC_KEY));
  mbedtls_md_hmac_update(&ctx, (const unsigned char*)message.c_str(), message.length());
  mbedtls_md_hmac_finish(&ctx, hmacResult);
  mbedtls_md_free(&ctx);
  char hexStr[17];
  for (int i = 0; i < 8; i++) sprintf(&hexStr[i * 2], "%02x", hmacResult[i]);
  hexStr[16] = '\0';
  return String(hexStr);
}

String verifyAndExtract(const String& authMsg) {
  int firstColon = authMsg.indexOf(':');
  if (firstColon < 0) return "";
  int lastColon = authMsg.lastIndexOf(':');
  if (lastColon <= firstColon || lastColon == authMsg.length() - 1) return "";

  String seqStr = authMsg.substring(0, firstColon);
  String payload = authMsg.substring(firstColon + 1, lastColon);
  String receivedHmac = authMsg.substring(lastColon + 1);
  receivedHmac.toLowerCase();
  uint32_t seq = strtoul(seqStr.c_str(), NULL, 10);

  if (seqInitialized) {
    if (seq <= lastSeqNum && lastSeqNum - seq < SEQ_WINDOW) return "";
    if (seq > lastSeqNum + SEQ_WINDOW * 10) return "";
  }

  String expectedHmac = generateHMAC(seq, payload);
  if (!receivedHmac.equals(expectedHmac)) return "";

  if (!seqInitialized || seq > lastSeqNum) { lastSeqNum = seq; seqInitialized = true; }
  return payload;
}

// ─────────────── ESP-NOW receive callback ────────────────────
void OnDataRecv(const esp_now_recv_info *info, const uint8_t *data, int len) {
  if (len <= 0) return;

  if (data[0] == RTCM_ESPNOW_PREFIX) { handleRTCMChunk(data, len); return; }

  char buf[len + 1];
  memcpy(buf, data, len); buf[len] = '\0';
  String authMsg(buf); authMsg.trim();

  String msg = verifyAndExtract(authMsg);
  if (msg.length() == 0) return;

  bool hasBar = msg.indexOf('|') != -1;
  int semi = msg.indexOf(';'), colon = msg.indexOf(':');
  if (semi < 0 || colon < 0) return;

  String row = msg.substring(0, semi); row.trim();
  String col = msg.substring(semi + 1, colon); col.trim();
  if (row != ROW_NUM) return;

  if (hasBar) {
    if (col != COL_NUM) return;
    int bar = msg.indexOf('|');
    String gps = msg.substring(colon + 1, bar); gps.trim();
    String imu = msg.substring(bar + 1); imu.trim();
    int comma = gps.indexOf(',');
    if (comma < 0) return;

    double recv_lat = strtod(gps.substring(0, comma).c_str(), NULL);
    double recv_lon = strtod(gps.substring(comma + 1).c_str(), NULL);
    int recv_imu = imu.toInt();

    Serial.printf("Target GPS=%.8f,%.8f IMU=%d\n", recv_lat, recv_lon, recv_imu);
    Serial.printf("Current GPS=%.8f,%.8f IMU=%d | Fix: %s\n", cur_LAT, cur_LON, cur_IMU, fixQualityStr(gps_fix_quality));

    if (!gps_valid) { buzzPulse(MOTOR_PIN_18, 150); return; }

    bool gps_match = fabs(recv_lat - cur_LAT) < GPS_TOLERANCE && fabs(recv_lon - cur_LON) < GPS_TOLERANCE;
    int heading_diff = normalizeHeading(cur_IMU - recv_imu);
    int abs_heading_diff = abs(heading_diff);
    bool heading_match = abs_heading_diff <= IMU_DEADZONE;

    if (gps_match && heading_match) {
      buzz(MOTOR_PIN_5, HIGH); buzz(MOTOR_PIN_18, HIGH); buzz(MOTOR_PIN_19, HIGH); buzz(MOTOR_PIN_23, HIGH);
      delay(150);
      buzz(MOTOR_PIN_5, LOW); buzz(MOTOR_PIN_18, LOW); buzz(MOTOR_PIN_19, LOW); buzz(MOTOR_PIN_23, LOW);
    } else if (!heading_match) {
      if (abs_heading_diff <= IMU_SOFT_LIMIT) {
        if (heading_diff > 0) buzzRotateLeft(false); else buzzRotateRight(false);
      } else {
        if (heading_diff > 0) buzzRotateLeft(true); else buzzRotateRight(true);
      }
    } else {
      buzzPulse(MOTOR_PIN_23, 200);
    }
    return;
  }

  // Manual pin command
  int pin = col.toInt();
  int state = msg.substring(colon + 1).toInt();
  if (pin == MOTOR_PIN_5 || pin == MOTOR_PIN_18 || pin == MOTOR_PIN_19 || pin == MOTOR_PIN_23) {
    buzz(pin, state);
    Serial.printf("Manual: pin %d -> %s\n", pin, state ? "HIGH" : "LOW");
  }
}

void setup() {
  Serial.begin(115200);
  Serial.println("ESP32 ESP-NOW Receiver Starting...");

  pinMode(MOTOR_PIN_5, OUTPUT); pinMode(MOTOR_PIN_18, OUTPUT);
  pinMode(MOTOR_PIN_19, OUTPUT); pinMode(MOTOR_PIN_23, OUTPUT);
  pinMode(GPS_RST_PIN, OUTPUT); pinMode(GPS_CEN_PIN, OUTPUT); pinMode(GPS_WUP_PIN, OUTPUT);

  digitalWrite(GPS_CEN_PIN, HIGH); delay(1000);
  digitalWrite(GPS_CEN_PIN, LOW);  delay(1000);
  digitalWrite(GPS_CEN_PIN, HIGH); delay(1000);
  digitalWrite(GPS_WUP_PIN, HIGH); delay(100);
  digitalWrite(GPS_WUP_PIN, LOW);  delay(1000);
  digitalWrite(GPS_RST_PIN, LOW);  delay(100);
  digitalWrite(GPS_RST_PIN, HIGH); delay(2000);

  gpsActiveBaud = detectGPSBaud();
  if (gpsActiveBaud > 0) Serial.printf("GPS module responding at %ld baud\n", gpsActiveBaud);
  else {
    Serial.println("GPS module not detected, trying swapped pins...");
    #undef GPS_RX_PIN
    #undef GPS_TX_PIN
    #define GPS_RX_PIN 22
    #define GPS_TX_PIN 21
    gpsActiveBaud = detectGPSBaud();
    if (gpsActiveBaud == 0) {
      #undef GPS_RX_PIN
      #undef GPS_TX_PIN
      #define GPS_RX_PIN 21
      #define GPS_TX_PIN 22
      GPSSerial.end(); GPSSerial.setRxBufferSize(2048);
      GPSSerial.begin(460800, SERIAL_8N1, GPS_RX_PIN, GPS_TX_PIN);
    }
  }

  WiFi.mode(WIFI_STA);
  esp_wifi_set_channel(WIFI_CHANNEL, WIFI_SECOND_CHAN_NONE);

  if (esp_now_init() != ESP_OK) { Serial.println("Error initializing ESP-NOW"); return; }
  esp_now_register_recv_cb(OnDataRecv);

  // Add broadcast peer for status reports
  esp_now_peer_info_t peerInfo = {};
  memcpy(peerInfo.peer_addr, broadcastAddress, 6);
  peerInfo.channel = WIFI_CHANNEL;
  peerInfo.encrypt = false;
  if (esp_now_add_peer(&peerInfo) == ESP_OK) Serial.println("Status broadcast peer added.");

  statusOffset = (ROW_NUM.toInt() * 5 + COL_NUM.toInt()) * 200;
  lastStatusSend = millis() - STATUS_INTERVAL + statusOffset;
}

void loop() {
  // Write deferred RTCM data to GPS module
  if (rtcmWriteLen > 0) {
    size_t toWrite = rtcmWriteLen;
    size_t written = GPSSerial.write(rtcmWriteBuf, toWrite);
    rtcmWriteLen = 0;
    rtcmBytesToGPS += written;
    if (written != toWrite) Serial.printf("RTCM GPS: only %zu/%zu bytes written!\n", written, toWrite);
  }

  readGPS();

  unsigned long now = millis();
  if (now - lastGpsPrint >= GPS_PRINT_INTERVAL) {
    lastGpsPrint = now;
    if (gps_valid) {
      unsigned long age = (now - lastGpsTime) / 1000;
      Serial.printf("GPS: %.8f, %.8f | Heading: %d° | Fix: %s (%d) | Age: %lus\n",
                    cur_LAT, cur_LON, cur_IMU, fixQualityStr(gps_fix_quality), gps_fix_quality, age);
    } else {
      Serial.printf("GPS: Waiting for fix... | UART RX: %lu bytes, %lu '$', %lu sentences | Baud: %ld\n",
                    gpsRxByteCount, gpsDollarCount, gpsNmeaParsed, gpsActiveBaud);
    }
  }

  // Send status report back to hub
  if (now - lastStatusSend >= STATUS_INTERVAL) {
    lastStatusSend = now;
    char statusMsg[120];
    if (gps_valid)
      snprintf(statusMsg, sizeof(statusMsg), "HB:%s;%s:%.8f,%.8f|%d|%d|RTCM:%lu,%lu",
               ROW_NUM.c_str(), COL_NUM.c_str(), cur_LAT, cur_LON, cur_IMU, gps_fix_quality, rtcmMsgCount, rtcmBytesToGPS);
    else
      snprintf(statusMsg, sizeof(statusMsg), "HB:%s;%s:0,0|0|0|RTCM:%lu,%lu",
               ROW_NUM.c_str(), COL_NUM.c_str(), rtcmMsgCount, rtcmBytesToGPS);
    esp_now_send(broadcastAddress, (uint8_t*)statusMsg, strlen(statusMsg));
    Serial.printf("Status sent: %s\n", statusMsg);
  }

  delay(1);
}
