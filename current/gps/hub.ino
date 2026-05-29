#include <WiFi.h>
#include <esp_now.h>
#include <mbedtls/md.h>
#include <mbedtls/base64.h>
#include <HardwareSerial.h>

// Wi-Fi AP credentials
#define WIFI_SSID "PWMB_Hub"
#define WIFI_PASS "12345678"
#define AP_CHANNEL 1

// RTCM message prefix for ESP-NOW broadcast
#define RTCM_PREFIX "RTCM:"
#define RTCM_ESPNOW_PREFIX 0xD3

// ESP-NOW max payload is 250 bytes - chunk larger RTCM messages
#define ESPNOW_MAX_PAYLOAD 240

// Shared HMAC key for message authentication (32 bytes for HMAC-SHA256)
static const uint8_t HMAC_KEY[32] = {
  0x7A, 0x4E, 0x2B, 0x91, 0xF3, 0x8C, 0x5D, 0xE7,
  0x1F, 0x6A, 0xC4, 0x83, 0x9B, 0x2E, 0xD5, 0x70,
  0xA8, 0x3F, 0x6C, 0x19, 0xE2, 0x7D, 0x4B, 0x95,
  0x0C, 0x68, 0xB1, 0xF9, 0x3A, 0x57, 0xDE, 0x84
};

// Sequence number for replay protection
static uint32_t txSequence = 0;

// Headband status relay queue (ESP-NOW -> TCP)
#define HB_QUEUE_SIZE 30
#define HB_MSG_SIZE 120
static char hbQueue[HB_QUEUE_SIZE][HB_MSG_SIZE];
static volatile uint8_t hbQueueHead = 0;
static volatile uint8_t hbQueueTail = 0;

// TCP server port
#define TCP_PORT 80

const String ROW_NUM = "0";
const String COL_NUM = "3";

// ─────────────── RTK GPS Configuration ────────────────────
#define GPS_RX_PIN 21
#define GPS_TX_PIN 22
#define GPS_RST_PIN 4
#define GPS_CEN_PIN 2
#define GPS_WUP_PIN 15
HardwareSerial GPSSerial(2);

static const long GPS_BAUDS[] = {460800, 115200, 230400, 921600, 38400, 9600};
static const int  GPS_BAUD_COUNT = sizeof(GPS_BAUDS) / sizeof(GPS_BAUDS[0]);
static long       gpsActiveBaud = 0;

// Live GPS state
static double cur_LAT = 0.0;
static double cur_LON = 0.0;
static int    cur_IMU = 0;
static uint8_t gps_fix_quality = 0;
static bool  gps_valid = false;

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

WiFiServer tcpServer(TCP_PORT);
unsigned long lastDataSentTime = 0;
const unsigned long DATA_SEND_INTERVAL = 5000;

// Broadcast MAC address
uint8_t broadcastAddress[] = { 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF };

// TCP connection health monitoring
static unsigned long lastClientActivity = 0;
#define CLIENT_TIMEOUT_MS 30000
#define HEARTBEAT_INTERVAL 10000
static unsigned long lastHeartbeat = 0;

// RTCM diagnostic counters
static unsigned long rtcmBytesFromLaptop = 0;
static unsigned long rtcmBytesToGPS = 0;
static unsigned long rtcmBytesToESPNOW = 0;
static unsigned long rtcmMsgCount = 0;

// Static RTCM decode buffer — avoids malloc/free heap fragmentation
#define RTCM_DECODE_BUF_SIZE 1500
static unsigned char rtcmDecodeBuf[RTCM_DECODE_BUF_SIZE];

void OnDataSent(const wifi_tx_info_t *info, esp_now_send_status_t status) {
  if (status != ESP_NOW_SEND_SUCCESS) {
    Serial.println("ESP-NOW send FAILED");
  }
}

// Receive callback — handles headband status reports for relay to laptop
void OnHubDataRecv(const esp_now_recv_info *info, const uint8_t *data, int len) {
  if (len < 3) return;
  if (data[0] == 'H' && data[1] == 'B' && data[2] == ':') {
    uint8_t nextHead = (hbQueueHead + 1) % HB_QUEUE_SIZE;
    if (nextHead != hbQueueTail) {
      int cpLen = (len < HB_MSG_SIZE - 1) ? len : (HB_MSG_SIZE - 1);
      memcpy(hbQueue[hbQueueHead], data, cpLen);
      hbQueue[hbQueueHead][cpLen] = '\0';
      hbQueueHead = nextHead;
    }
  }
}

// Generate HMAC-SHA256 and return first 8 bytes as hex string
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
  for (int i = 0; i < 8; i++) {
    sprintf(&hexStr[i * 2], "%02x", hmacResult[i]);
  }
  hexStr[16] = '\0';
  return String(hexStr);
}

String createAuthenticatedMessage(const String& payload) {
  uint32_t seq = txSequence++;
  String hmac = generateHMAC(seq, payload);
  return String(seq) + ":" + payload + ":" + hmac;
}

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
        char c = GPSSerial.read();
        totalBytes++;
        if (c == '$') sawDollar = true;
        if (c >= 0x20 && c < 0x7F) asciiCount++;
      }
      if (!GPSSerial.available()) delayMicroseconds(100);
    }

    Serial.printf("  -> %d bytes, %d ASCII, dollar=%s\n", totalBytes, asciiCount, sawDollar ? "YES" : "no");

    if (sawDollar || (totalBytes > 10 && asciiCount > totalBytes / 2)) {
      Serial.printf("GPS detected at %ld baud\n", baud);
      GPSSerial.end();
      GPSSerial.setRxBufferSize(2048);
      GPSSerial.begin(baud, SERIAL_8N1, GPS_RX_PIN, GPS_TX_PIN);
      return baud;
    }
    if (totalBytes > 0) Serial.printf("  -> garbage data, wrong baud\n");
  }

  Serial.println("WARNING: No GPS module detected at any baud rate!");
  GPSSerial.end();
  GPSSerial.setRxBufferSize(2048);
  GPSSerial.begin(460800, SERIAL_8N1, GPS_RX_PIN, GPS_TX_PIN);
  return 0;
}

// ─────────────── NMEA parsing ───────────────────────────────
String nmeaField(const char* sentence, int fieldNum) {
  int current = 0;
  const char* start = sentence;
  for (const char* p = sentence; *p; p++) {
    if (*p == ',' || *p == '*') {
      if (current == fieldNum)
        return String(sentence).substring(start - sentence, p - sentence);
      current++;
      start = p + 1;
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

  char tmp[24];
  int cpLen = (rawLen < 23) ? rawLen : 23;
  memcpy(tmp, raw, cpLen); tmp[cpLen] = '\0';
  char degBuf[8];
  memcpy(degBuf, tmp, degLen); degBuf[degLen] = '\0';

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
      gps_valid = true;
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
        gps_valid = true;
      }
      if (course.length() > 0) cur_IMU = (int)course.toFloat();
    }
  }
}

void readGPS() {
  while (GPSSerial.available()) {
    char c = GPSSerial.read();
    gpsRxByteCount++;
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

void setup() {
  Serial.begin(115200);
  Serial.println("ESP32 ESP-NOW Relay Starting...");

  pinMode(GPS_RST_PIN, OUTPUT);
  pinMode(GPS_CEN_PIN, OUTPUT);
  pinMode(GPS_WUP_PIN, OUTPUT);

  Serial.println("Enabling GPS module (CEN HIGH)...");
  digitalWrite(GPS_CEN_PIN, HIGH); delay(1000);
  digitalWrite(GPS_CEN_PIN, LOW);  delay(1000);
  digitalWrite(GPS_CEN_PIN, HIGH); delay(1000);

  Serial.println("Sending WUP pulse...");
  digitalWrite(GPS_WUP_PIN, HIGH); delay(100);
  digitalWrite(GPS_WUP_PIN, LOW);  delay(1000);

  Serial.println("Resetting GPS module (RST pulse)...");
  digitalWrite(GPS_RST_PIN, LOW);  delay(100);
  digitalWrite(GPS_RST_PIN, HIGH); delay(2000);

  Serial.println("Detecting GPS module baud rate...");
  gpsActiveBaud = detectGPSBaud();
  if (gpsActiveBaud > 0) Serial.printf("GPS module responding at %ld baud\n", gpsActiveBaud);
  else Serial.println("GPS module not detected");

  WiFi.mode(WIFI_AP_STA);
  WiFi.softAP(WIFI_SSID, WIFI_PASS, AP_CHANNEL);
  Serial.print("AP IP address: "); Serial.println(WiFi.softAPIP());

  tcpServer.begin();
  Serial.println("TCP server started on port " + String(TCP_PORT));

  if (esp_now_init() != ESP_OK) { Serial.println("Error initializing ESP-NOW"); return; }
  Serial.println("ESP-NOW initialized.");

  esp_now_register_send_cb(OnDataSent);
  esp_now_register_recv_cb(OnHubDataRecv);

  esp_now_peer_info_t peerInfo = {};
  memcpy(peerInfo.peer_addr, broadcastAddress, 6);
  peerInfo.channel = AP_CHANNEL;
  peerInfo.encrypt = false;
  if (esp_now_add_peer(&peerInfo) != ESP_OK) { Serial.println("Failed to add broadcast peer"); return; }
  Serial.println("Broadcast peer added successfully.");
}

// Non-blocking line reader — returns pointer to static buffer, or NULL
static char tcpReadBuf[2048];
static int  tcpReadIdx = 0;

const char* tcpReadLine(WiFiClient& client) {
  while (client.available()) {
    char c = client.read();
    if (c == '\n') {
      if (tcpReadIdx > 0 && tcpReadBuf[tcpReadIdx - 1] == '\r') tcpReadIdx--;
      tcpReadBuf[tcpReadIdx] = '\0';
      int len = tcpReadIdx;
      tcpReadIdx = 0;
      return (len > 0) ? tcpReadBuf : NULL;
    } else if (tcpReadIdx < (int)sizeof(tcpReadBuf) - 1) {
      tcpReadBuf[tcpReadIdx++] = c;
    }
  }
  return NULL;
}

// Persistent client
static WiFiClient activeClient;
static bool clientConnected = false;

void loop() {
  unsigned long currentMillis = millis();

  // ALWAYS read GPS
  readGPS();

  // Print GPS status periodically
  if (currentMillis - lastGpsPrint >= GPS_PRINT_INTERVAL) {
    lastGpsPrint = currentMillis;
    if (gps_valid)
      Serial.printf("GPS: %.8f, %.8f | Heading: %d° | Fix: %s (%d)\n",
                    cur_LAT, cur_LON, cur_IMU, fixQualityStr(gps_fix_quality), gps_fix_quality);
    else
      Serial.printf("GPS: Waiting for fix... | UART RX: %lu bytes, %lu '$', %lu sentences | Baud: %ld\n",
                    gpsRxByteCount, gpsDollarCount, gpsNmeaParsed, gpsActiveBaud);
  }

  // Accept new client if none connected
  if (!clientConnected) {
    WiFiClient newClient = tcpServer.available();
    if (newClient) {
      activeClient = newClient;
      clientConnected = true;
      lastClientActivity = currentMillis;
      lastHeartbeat = currentMillis;
      tcpReadIdx = 0;
      Serial.println("Client connected");
    }
    delay(1);
    return;
  }

  // Check client health
  if (!activeClient.connected()) {
    Serial.println("Client disconnected");
    activeClient.stop();
    clientConnected = false;
    return;
  }

  // Watchdog
  if (currentMillis - lastClientActivity > CLIENT_TIMEOUT_MS) {
    Serial.printf("Client watchdog timeout (%lus no activity)\n", (currentMillis - lastClientActivity) / 1000);
    activeClient.stop();
    clientConnected = false;
    return;
  }

  // Send GPS data every 5 seconds
  if (currentMillis - lastDataSentTime >= DATA_SEND_INTERVAL) {
    char dataStr[96];
    if (gps_valid)
      snprintf(dataStr, sizeof(dataStr), "GPS:%.8f,%.8f|IMU:%d|FIX:%d", cur_LAT, cur_LON, cur_IMU, gps_fix_quality);
    else
      snprintf(dataStr, sizeof(dataStr), "GPS:0,0|IMU:0|FIX:0");
    if (activeClient.println(dataStr)) { lastClientActivity = currentMillis; Serial.printf("Sent: %s\n", dataStr); }
    lastDataSentTime = currentMillis;
  }

  // Heartbeat with RTCM diagnostics
  if (currentMillis - lastHeartbeat >= HEARTBEAT_INTERVAL) {
    char hbMsg[120];
    snprintf(hbMsg, sizeof(hbMsg), "HUB:OK|RTCM:%lu,%lu,%lu,%lu",
             rtcmMsgCount, rtcmBytesFromLaptop, rtcmBytesToGPS, rtcmBytesToESPNOW);
    if (activeClient.println(hbMsg)) lastClientActivity = currentMillis;
    lastHeartbeat = currentMillis;
  }

  // Relay queued headband status messages
  while (hbQueueTail != hbQueueHead) {
    if (activeClient.println(hbQueue[hbQueueTail])) lastClientActivity = currentMillis;
    hbQueueTail = (hbQueueTail + 1) % HB_QUEUE_SIZE;
  }

  // Non-blocking read from client
  const char* line = tcpReadLine(activeClient);
  if (line) {
    lastClientActivity = currentMillis;

    if (strncmp(line, RTCM_PREFIX, 5) == 0) {
      // RTCM — decode into STATIC buffer (no malloc)
      const char* b64data = line + 5;
      size_t b64len = strlen(b64data);
      size_t outputLen = 0;
      int ret = mbedtls_base64_decode(rtcmDecodeBuf, RTCM_DECODE_BUF_SIZE, &outputLen,
                                       (const unsigned char*)b64data, b64len);
      if (ret != 0) {
        Serial.printf("RTCM: base64 decode error %d (input %zu bytes)\n", ret, b64len);
      } else if (outputLen > 0) {
        rtcmBytesFromLaptop += outputLen;
        rtcmMsgCount++;

        // Write to hub's own GPS module
        size_t written = GPSSerial.write(rtcmDecodeBuf, outputLen);
        rtcmBytesToGPS += written;
        if (written != outputLen) Serial.printf("RTCM GPS: only %zu/%zu bytes written!\n", written, outputLen);

        // Broadcast to headbands via ESP-NOW
        const size_t CHUNK_PAYLOAD_SIZE = ESPNOW_MAX_PAYLOAD - 2;
        size_t offset = 0;
        uint8_t chunkNum = 0;
        uint8_t totalChunks = (outputLen + CHUNK_PAYLOAD_SIZE - 1) / CHUNK_PAYLOAD_SIZE;
        while (offset < outputLen) {
          size_t remaining = outputLen - offset;
          size_t chunkSize = (remaining < CHUNK_PAYLOAD_SIZE) ? remaining : CHUNK_PAYLOAD_SIZE;
          uint8_t packet[ESPNOW_MAX_PAYLOAD];
          packet[0] = RTCM_ESPNOW_PREFIX;
          packet[1] = (chunkNum << 4) | (totalChunks & 0x0F);
          memcpy(packet + 2, rtcmDecodeBuf + offset, chunkSize);
          esp_now_send(broadcastAddress, packet, chunkSize + 2);
          rtcmBytesToESPNOW += chunkSize;
          offset += chunkSize;
          chunkNum++;
          delay(1);
        }
        Serial.printf("RTCM: %zu bytes -> GPS(%zu), ESP-NOW(%d chunks)\n", outputLen, written, chunkNum);
      }
    } else {
      // Regular control message
      String received(line);
      String authMsg = createAuthenticatedMessage(received);
      esp_now_send(broadcastAddress, (uint8_t*)authMsg.c_str(), authMsg.length());
      activeClient.println("OK");
    }
  }

  delay(1);
}
