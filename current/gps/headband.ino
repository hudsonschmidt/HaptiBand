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

// Replay protection: track last seen sequence number
// Allow a window to handle out-of-order packets
static uint32_t lastSeqNum = 0;
static bool seqInitialized = false;      // false until first valid message received
static const uint32_t SEQ_WINDOW = 100;  // Accept packets within this window ahead

// RTCM handling
#define RTCM_ESPNOW_PREFIX 0xD3  // Standard RTCM3 preamble byte

// RTK GPS serial connection (UART2)
// Wiring: GPS module TX → ESP32 GPIO16 (RX), GPS module RX → ESP32 GPIO17 (TX)
#define GPS_RX_PIN 16
#define GPS_TX_PIN 17
HardwareSerial GPSSerial(2);

// Baud rates to try during auto-detection (most likely first)
static const long GPS_BAUDS[] = {460800, 115200, 38400, 9600};
static const int  GPS_BAUD_COUNT = sizeof(GPS_BAUDS) / sizeof(GPS_BAUDS[0]);
static long       gpsActiveBaud = 0;  // The baud rate that worked

// WiFi channel — MUST match hub AP_CHANNEL
#define WIFI_CHANNEL 1

// RTCM reassembly buffer for chunked messages
#define RTCM_BUFFER_SIZE 1024
static uint8_t rtcmBuffer[RTCM_BUFFER_SIZE];
static size_t rtcmBufferLen = 0;
static uint8_t expectedChunk = 0;
static uint8_t totalChunks = 0;
static unsigned long lastRtcmTime = 0;
#define RTCM_TIMEOUT_MS 1000  // Reset if chunks arrive too slowly

// Deferred RTCM write buffer — callback copies here, loop() writes to UART
// This avoids blocking GPSSerial.write() inside the ESP-NOW callback
#define RTCM_WRITE_BUF_SIZE 1024
static uint8_t  rtcmWriteBuf[RTCM_WRITE_BUF_SIZE];
static volatile size_t rtcmWriteLen = 0;  // >0 means data pending for GPS

const int MOTOR_PIN_5 = 5;
const int MOTOR_PIN_18 = 18;
const int MOTOR_PIN_19 = 19;
const int MOTOR_PIN_23 = 23;

const String ROW_NUM = "1";
const String COL_NUM = "3";

// Live GPS state (updated from RTK module NMEA output)
static double cur_LAT = 0.0;
static double cur_LON = 0.0;
static int    cur_IMU = 0;          // heading not available from GPS alone
static uint8_t gps_fix_quality = 0; // 0=none, 1=GPS, 2=DGPS, 4=RTK fixed, 5=RTK float
static bool  gps_valid = false;     // true once we have a valid fix
static unsigned long lastGpsTime = 0;

// NMEA sentence buffer
#define NMEA_BUF_SIZE 256
static char nmeaBuf[NMEA_BUF_SIZE];
static uint16_t nmeaIdx = 0;        // uint16_t to safely index up to NMEA_BUF_SIZE

// GPS status print interval
static unsigned long lastGpsPrint = 0;
#define GPS_PRINT_INTERVAL 5000  // print status every 5 seconds

// GPS UART diagnostic counters
static unsigned long gpsRxByteCount = 0;   // total bytes received from GPS UART
static unsigned long gpsDollarCount = 0;   // '$' characters seen (start of NMEA)
static unsigned long gpsNmeaParsed = 0;    // complete sentences parsed
static bool gpsDiagDone = false;           // only dump raw bytes once

// Tolerance for GPS comparison (~1 foot at this latitude)
const double GPS_TOLERANCE = 0.000003;

// IMU tolerance thresholds (degrees)
const int IMU_DEADZONE = 5;       // no correction needed
const int IMU_SOFT_LIMIT = 15;    // gentle correction
// beyond SOFT_LIMIT = urgent correction

// ─────────────── Baud rate auto-detection ────────────────────
// Try each baud rate and look for readable ASCII / '$' within a timeout.
// Returns the working baud rate, or 0 if none found.
long detectGPSBaud() {
  for (int i = 0; i < GPS_BAUD_COUNT; i++) {
    long baud = GPS_BAUDS[i];
    Serial.printf("Probing GPS at %ld baud...\n", baud);

    GPSSerial.end();
    GPSSerial.setRxBufferSize(2048);
    GPSSerial.begin(baud, SERIAL_8N1, GPS_RX_PIN, GPS_TX_PIN);

    // Drain any stale data
    while (GPSSerial.available()) GPSSerial.read();

    // Wait up to 1.5 seconds for recognizable data
    unsigned long start = millis();
    int asciiCount = 0;
    int totalBytes = 0;
    bool sawDollar = false;

    while (millis() - start < 1500) {
      if (GPSSerial.available()) {
        char c = GPSSerial.read();
        totalBytes++;
        if (c == '$') sawDollar = true;
        if (c >= 0x20 && c < 0x7F) asciiCount++;
      }
      // Don't burn CPU — but check frequently
      if (!GPSSerial.available()) delayMicroseconds(100);
    }

    Serial.printf("  → %d bytes, %d ASCII, dollar=%s\n",
                  totalBytes, asciiCount, sawDollar ? "YES" : "no");

    // Success: saw a '$' or >50% of bytes are printable ASCII
    if (sawDollar || (totalBytes > 10 && asciiCount > totalBytes / 2)) {
      Serial.printf("GPS detected at %ld baud\n", baud);
      // Re-init cleanly at this baud
      GPSSerial.end();
      GPSSerial.setRxBufferSize(2048);
      GPSSerial.begin(baud, SERIAL_8N1, GPS_RX_PIN, GPS_TX_PIN);
      return baud;
    }

    // If we got bytes but they're garbage, wrong baud — try next
    if (totalBytes > 0) {
      Serial.printf("  → garbage data, wrong baud\n");
    }
  }

  Serial.println("WARNING: No GPS module detected at any baud rate!");
  Serial.println("Check wiring: GPS TX → ESP32 GPIO16, GPS RX → ESP32 GPIO17");
  // Fall back to 460800 and keep trying
  GPSSerial.end();
  GPSSerial.setRxBufferSize(2048);
  GPSSerial.begin(460800, SERIAL_8N1, GPS_RX_PIN, GPS_TX_PIN);
  return 0;
}

// ─────────────── NMEA parsing ───────────────────────────────
// Get the Nth comma-delimited field from an NMEA sentence (0-indexed)
String nmeaField(const char* sentence, int fieldNum) {
  int current = 0;
  const char* start = sentence;

  for (const char* p = sentence; *p; p++) {
    if (*p == ',' || *p == '*') {
      if (current == fieldNum) {
        return String(sentence).substring(start - sentence, p - sentence);
      }
      current++;
      start = p + 1;
    }
  }
  // Last field (no trailing comma)
  if (current == fieldNum) {
    return String(start);
  }
  return "";
}

// Convert NMEA lat/lon (DDMM.MMMMM) to decimal degrees
double nmeaToDecimal(const char* raw, int rawLen, const String& dir) {
  if (rawLen == 0) return 0.0;

  // Find the decimal point to split degrees from minutes
  int dotPos = -1;
  for (int i = 0; i < rawLen; i++) {
    if (raw[i] == '.') { dotPos = i; break; }
  }
  if (dotPos < 0) return 0.0;

  // Degrees are everything before the last 2 digits before the dot
  int degLen = dotPos - 2;
  if (degLen < 1) return 0.0;

  // Use strtod for full double precision (toFloat truncates to 32-bit)
  char tmp[24];
  int cpLen = (rawLen < 23) ? rawLen : 23;
  memcpy(tmp, raw, cpLen);
  tmp[cpLen] = '\0';

  char degBuf[8];
  memcpy(degBuf, tmp, degLen);
  degBuf[degLen] = '\0';

  double degrees = strtod(degBuf, NULL);
  double minutes = strtod(tmp + degLen, NULL);
  double decimal = degrees + (minutes / 60.0);

  if (dir == "S" || dir == "W") {
    decimal = -decimal;
  }
  return decimal;
}

const char* fixQualityStr(uint8_t q) {
  switch (q) {
    case 0: return "No fix";
    case 1: return "GPS";
    case 2: return "DGPS";
    case 4: return "RTK Fixed";
    case 5: return "RTK Float";
    default: return "Unknown";
  }
}

// Parse a complete NMEA sentence
void parseNMEA(const char* sentence) {
  // We care about GGA (position + fix quality) and RMC (position + course)
  // Accept any talker ID (GP, GN, GL, etc.)
  if (strlen(sentence) < 7) return;  // safety: need at least $XXYYY,
  const char* type = sentence + 3;   // skip $XX

  if (strncmp(type, "GGA,", 4) == 0) {
    // $xxGGA,time,lat,N/S,lon,E/W,quality,numSV,HDOP,alt,M,sep,M,age,stn*cs
    //         0    1   2   3   4     5      6     7    8  9 10 11  12  13
    String lat_raw = nmeaField(sentence, 2);
    String lat_dir = nmeaField(sentence, 3);
    String lon_raw = nmeaField(sentence, 4);
    String lon_dir = nmeaField(sentence, 5);
    String quality = nmeaField(sentence, 6);

    uint8_t q = quality.toInt();
    gps_fix_quality = q;

    if (q > 0 && lat_raw.length() > 0 && lon_raw.length() > 0) {
      cur_LAT = nmeaToDecimal(lat_raw.c_str(), lat_raw.length(), lat_dir);
      cur_LON = nmeaToDecimal(lon_raw.c_str(), lon_raw.length(), lon_dir);
      gps_valid = true;
      lastGpsTime = millis();
      Serial.printf("GGA: %.6f, %.6f | Fix: %s (%d)\n", cur_LAT, cur_LON, fixQualityStr(q), q);
    }

  } else if (strncmp(type, "RMC,", 4) == 0) {
    // $xxRMC,time,status,lat,N/S,lon,E/W,speed,course,date,...
    //         0     1     2   3   4   5    6      7      8
    String status = nmeaField(sentence, 2);

    if (status == "A") {  // A = valid
      String lat_raw = nmeaField(sentence, 3);
      String lat_dir = nmeaField(sentence, 4);
      String lon_raw = nmeaField(sentence, 5);
      String lon_dir = nmeaField(sentence, 6);
      String course  = nmeaField(sentence, 8);

      if (lat_raw.length() > 0 && lon_raw.length() > 0) {
        cur_LAT = nmeaToDecimal(lat_raw.c_str(), lat_raw.length(), lat_dir);
        cur_LON = nmeaToDecimal(lon_raw.c_str(), lon_raw.length(), lon_dir);
        gps_valid = true;
        lastGpsTime = millis();
      }

      // Course over ground (heading) — only valid when moving
      if (course.length() > 0) {
        cur_IMU = (int)course.toFloat();
      }

      Serial.printf("RMC: %.6f, %.6f | Heading: %d°\n",
                    cur_LAT, cur_LON, cur_IMU);
    }
  }
}

// Read and process available bytes from GPS serial
void readGPS() {
  while (GPSSerial.available()) {
    char c = GPSSerial.read();
    gpsRxByteCount++;

    // Dump first 64 raw bytes once for diagnostics (detect baud mismatch)
    if (!gpsDiagDone && gpsRxByteCount <= 64) {
      Serial.printf("GPS_RAW[%lu]: 0x%02X '%c'\n",
                    gpsRxByteCount, (uint8_t)c,
                    (c >= 0x20 && c < 0x7F) ? c : '.');
      if (gpsRxByteCount == 64) {
        gpsDiagDone = true;
        Serial.println("--- end GPS raw dump ---");
      }
    }

    if (c == '$') {
      gpsDollarCount++;
      // Start of new sentence
      nmeaIdx = 0;
      nmeaBuf[nmeaIdx++] = c;
    } else if (c == '\n' || c == '\r') {
      // End of sentence
      if (nmeaIdx > 5) {
        nmeaBuf[nmeaIdx] = '\0';
        gpsNmeaParsed++;
        parseNMEA(nmeaBuf);
      }
      nmeaIdx = 0;
    } else if (nmeaIdx < NMEA_BUF_SIZE - 1) {
      nmeaBuf[nmeaIdx++] = c;
    }
  }
}

// helper ------------------------------------------------------
void buzz(int pin, bool on) {
  digitalWrite(pin, on ? HIGH : LOW);
}

// Single pulse on a pin
void buzzPulse(int pin, int duration_ms) {
  buzz(pin, HIGH);
  delay(duration_ms);
  buzz(pin, LOW);
}

// Soft correction: single short pulse
void buzzSoft(int pin) {
  buzzPulse(pin, 100);
}

// Hard correction: double pulse (more urgent)
void buzzHard(int pin) {
  for (int i = 0; i < 2; ++i) {
    buzzPulse(pin, 100);
    delay(50);
  }
}

// Rotation patterns for heading correction
void buzzRotateLeft(bool hard) {
  // Front then left to indicate "turn left"
  if (hard) {
    buzzHard(MOTOR_PIN_18);
    delay(50);
    buzzHard(MOTOR_PIN_5);
  } else {
    buzzSoft(MOTOR_PIN_18);
    delay(50);
    buzzSoft(MOTOR_PIN_5);
  }
}

void buzzRotateRight(bool hard) {
  // Front then right to indicate "turn right"
  if (hard) {
    buzzHard(MOTOR_PIN_18);
    delay(50);
    buzzHard(MOTOR_PIN_19);
  } else {
    buzzSoft(MOTOR_PIN_18);
    delay(50);
    buzzSoft(MOTOR_PIN_19);
  }
}

// Normalize heading difference to -180 to +180
int normalizeHeading(int diff) {
  while (diff > 180) diff -= 360;
  while (diff < -180) diff += 360;
  return diff;
}

// Handle incoming RTCM chunk and reassemble.
// When complete, copies into rtcmWriteBuf for deferred write in loop().
bool handleRTCMChunk(const uint8_t* data, int len) {
  if (len < 3) return false;

  // Parse header: [0xD3][chunkNum|totalChunks][data...]
  uint8_t chunkInfo = data[1];
  uint8_t chunkNum = (chunkInfo >> 4) & 0x0F;
  uint8_t chunks = chunkInfo & 0x0F;
  const uint8_t* payload = data + 2;
  size_t payloadLen = len - 2;

  unsigned long now = millis();

  // Reset buffer if timeout or new message sequence
  if (chunkNum == 0 || (now - lastRtcmTime > RTCM_TIMEOUT_MS)) {
    rtcmBufferLen = 0;
    expectedChunk = 0;
    totalChunks = chunks;
  }

  lastRtcmTime = now;

  // Check chunk sequence
  if (chunkNum != expectedChunk) {
    Serial.printf("RTCM: expected chunk %d, got %d - resetting\n", expectedChunk, chunkNum);
    rtcmBufferLen = 0;
    expectedChunk = 0;
    return false;
  }

  // Append to buffer
  if (rtcmBufferLen + payloadLen > RTCM_BUFFER_SIZE) {
    Serial.println("RTCM: buffer overflow");
    rtcmBufferLen = 0;
    expectedChunk = 0;
    return false;
  }

  memcpy(rtcmBuffer + rtcmBufferLen, payload, payloadLen);
  rtcmBufferLen += payloadLen;
  expectedChunk++;

  // Check if complete — defer UART write to loop() to avoid blocking callback
  if (expectedChunk >= totalChunks) {
    if (rtcmWriteLen == 0 && rtcmBufferLen <= RTCM_WRITE_BUF_SIZE) {
      memcpy(rtcmWriteBuf, rtcmBuffer, rtcmBufferLen);
      rtcmWriteLen = rtcmBufferLen;
    } else {
      Serial.println("RTCM: write buffer busy, dropping");
    }

    rtcmBufferLen = 0;
    expectedChunk = 0;
    totalChunks = 0;
    return true;
  }

  return false;
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

// Verify authenticated message format: "seq:payload:hmac"
// Returns payload if valid, empty string if invalid
String verifyAndExtract(const String& authMsg) {
  // Find first colon (after seq)
  int firstColon = authMsg.indexOf(':');
  if (firstColon < 0) {
    Serial.println(F("Auth failed: no sequence number"));
    return "";
  }

  // Find last colon (before hmac)
  int lastColon = authMsg.lastIndexOf(':');
  if (lastColon <= firstColon || lastColon == authMsg.length() - 1) {
    Serial.println(F("Auth failed: no HMAC"));
    return "";
  }

  // Extract components
  String seqStr = authMsg.substring(0, firstColon);
  String payload = authMsg.substring(firstColon + 1, lastColon);
  String receivedHmac = authMsg.substring(lastColon + 1);
  receivedHmac.toLowerCase();

  uint32_t seq = strtoul(seqStr.c_str(), NULL, 10);

  // Check for replay attack (skip on first message ever received)
  if (seqInitialized) {
    if (seq <= lastSeqNum && lastSeqNum - seq < SEQ_WINDOW) {
      Serial.printf("Replay rejected: seq %lu <= last %lu\n", seq, lastSeqNum);
      return "";
    }

    // Reject sequences too far in the future (possible attack)
    if (seq > lastSeqNum + SEQ_WINDOW * 10) {
      Serial.printf("Suspicious seq: %lu (last: %lu)\n", seq, lastSeqNum);
      return "";
    }
  }

  // Verify HMAC
  String expectedHmac = generateHMAC(seq, payload);
  if (!receivedHmac.equals(expectedHmac)) {
    Serial.println(F("Auth failed: HMAC mismatch"));
    Serial.println("Expected: " + expectedHmac);
    Serial.println("Received: " + receivedHmac);
    return "";
  }

  // Update sequence tracking
  if (!seqInitialized || seq > lastSeqNum) {
    lastSeqNum = seq;
    seqInitialized = true;
  }

  Serial.printf("Auth OK: seq=%lu\n", seq);
  return payload;
}

// callback -----------------------------------------------
void OnDataRecv(const esp_now_recv_info *info, const uint8_t *data, int len)
{
  if (len <= 0) return;                 // safety

  /* ----- Check for RTCM correction data (starts with 0xD3) ------- */
  if (data[0] == RTCM_ESPNOW_PREFIX) {
    handleRTCMChunk(data, len);
    return;  // RTCM handled separately, no auth needed
  }

  /* ----- make a clean, NUL-terminated copy of the payload -------- */
  char buf[len + 1];
  memcpy(buf, data, len);
  buf[len] = '\0';                      // hard terminate
  String authMsg(buf);
  authMsg.trim();                       // drop CR/LF if present

  /* ----- verify authentication and extract payload --------------- */
  String msg = verifyAndExtract(authMsg);
  if (msg.length() == 0) {
    return;  // Authentication failed - ignore message
  }

  /* ----- determine packet type ----------------------------------- */
  bool hasBar = msg.indexOf('|') != -1; // GPS|IMU if true

  int semi  = msg.indexOf(';');
  int colon = msg.indexOf(':');
  if (semi < 0 || colon < 0) {
    Serial.println(F("Ill-formed packet (missing ; or :)"));
    return;
  }

  String row = msg.substring(0, semi);           row.trim();
  String col = msg.substring(semi + 1, colon);   col.trim();

  /* Ignore packets not addressed to our row */
  if (row != ROW_NUM) return;

  /* ────────────── GPS | IMU confirmation branch ────────────────── */
  if (hasBar) {
    if (col != COL_NUM) return;         // not our column

    int bar   = msg.indexOf('|');
    String gps = msg.substring(colon + 1, bar); gps.trim();
    String imu = msg.substring(bar + 1);        imu.trim();

    // Parse received GPS coordinates
    int comma = gps.indexOf(',');
    if (comma < 0) {
      Serial.println(F("Ill-formed GPS (missing comma)"));
      return;
    }
    double recv_lat = strtod(gps.substring(0, comma).c_str(), NULL);
    double recv_lon = strtod(gps.substring(comma + 1).c_str(), NULL);
    int recv_imu = imu.toInt();

    Serial.printf("Target GPS=%.6f,%.6f  IMU=%d\n", recv_lat, recv_lon, recv_imu);
    Serial.printf("Current GPS=%.6f,%.6f  IMU=%d | Fix: %s\n",
                  cur_LAT, cur_LON, cur_IMU, fixQualityStr(gps_fix_quality));

    if (!gps_valid) {
      Serial.println("WARNING: No GPS fix yet, cannot compare position");
      return;
    }

    // Compare GPS position
    bool lat_match = fabs(recv_lat - cur_LAT) < GPS_TOLERANCE;
    bool lon_match = fabs(recv_lon - cur_LON) < GPS_TOLERANCE;
    bool gps_match = lat_match && lon_match;

    // Calculate heading difference (positive = we're facing too far right)
    int heading_diff = normalizeHeading(cur_IMU - recv_imu);
    int abs_heading_diff = abs(heading_diff);

    Serial.printf("GPS match: %s, Heading diff: %d°\n",
                  gps_match ? "YES" : "NO", heading_diff);

    // Handle IMU correction with tiered tolerance
    if (abs_heading_diff <= IMU_DEADZONE) {
      // On target heading - check if GPS also matches for confirmation
      if (gps_match) {
        Serial.println("ON TARGET - confirmation buzz");
        // Quick confirmation: all motors single pulse
        buzz(MOTOR_PIN_5,  HIGH);
        buzz(MOTOR_PIN_18, HIGH);
        buzz(MOTOR_PIN_19, HIGH);
        buzz(MOTOR_PIN_23, HIGH);
        delay(150);
        buzz(MOTOR_PIN_5,  LOW);
        buzz(MOTOR_PIN_18, LOW);
        buzz(MOTOR_PIN_19, LOW);
        buzz(MOTOR_PIN_23, LOW);
      }
      // else: heading OK but position off - GPS correction would go here
    } else if (abs_heading_diff <= IMU_SOFT_LIMIT) {
      // Soft correction needed
      Serial.printf("SOFT correction: turn %s\n", heading_diff > 0 ? "LEFT" : "RIGHT");
      if (heading_diff > 0) {
        buzzRotateLeft(false);   // facing too far right, turn left
      } else {
        buzzRotateRight(false);  // facing too far left, turn right
      }
    } else {
      // Hard correction needed
      Serial.printf("HARD correction: turn %s\n", heading_diff > 0 ? "LEFT" : "RIGHT");
      if (heading_diff > 0) {
        buzzRotateLeft(true);    // facing too far right, turn left
      } else {
        buzzRotateRight(true);   // facing too far left, turn right
      }
    }
    return;                             // done
  }

  /* ─────────────── manual single-pin command branch ─────────────── */
  int pin   = col.toInt();                    // 5/18/19/23
  int state = msg.substring(colon + 1).toInt(); // 0 or 1

  if (pin == MOTOR_PIN_5 || pin == MOTOR_PIN_18 ||
      pin == MOTOR_PIN_19 || pin == MOTOR_PIN_23) {
    buzz(pin, state);
    Serial.printf("Manual: pin %d → %s\n",
                  pin, state ? "HIGH" : "LOW");
  } else {
    Serial.println(F("Unknown pin in manual packet"));
  }
}


void setup() {
  // Initialize Serial Monitor for debugging
  Serial.begin(115200);
  Serial.println("ESP32 ESP-NOW Receiver Starting...");

  // Set the motor pins as outputs
  pinMode(MOTOR_PIN_5, OUTPUT);
  pinMode(MOTOR_PIN_18, OUTPUT);
  pinMode(MOTOR_PIN_19, OUTPUT);
  pinMode(MOTOR_PIN_23, OUTPUT);

  // Auto-detect GPS baud rate
  Serial.println("Detecting GPS module baud rate...");
  gpsActiveBaud = detectGPSBaud();
  if (gpsActiveBaud > 0) {
    Serial.printf("GPS module responding at %ld baud\n", gpsActiveBaud);
  } else {
    Serial.println("GPS module not detected — check wiring and power");
  }

  // Set Wi-Fi mode to station (STA) mode and pin channel to match hub
  WiFi.mode(WIFI_STA);
  esp_wifi_set_channel(WIFI_CHANNEL, WIFI_SECOND_CHAN_NONE);

  // Initialize ESP-NOW
  if (esp_now_init() != ESP_OK) {
    Serial.println("Error initializing ESP-NOW");
    return;
  }
  Serial.println("ESP-NOW initialized.");

  // Register the receive callback using the updated function signature
  esp_now_register_recv_cb(OnDataRecv);
}

void loop() {
  // Write deferred RTCM data to GPS module (from ESP-NOW callback)
  if (rtcmWriteLen > 0) {
    size_t toWrite = rtcmWriteLen;
    size_t written = GPSSerial.write(rtcmWriteBuf, toWrite);
    rtcmWriteLen = 0;  // Clear even on partial write to avoid re-sending stale data
    Serial.printf("RTCM: sent %zu/%zu bytes to GPS\n", written, toWrite);
  }

  // Read NMEA sentences from RTK GPS module
  readGPS();

  // Print GPS status periodically
  unsigned long now = millis();
  if (now - lastGpsPrint >= GPS_PRINT_INTERVAL) {
    lastGpsPrint = now;

    if (gps_valid) {
      unsigned long age = (now - lastGpsTime) / 1000;
      Serial.printf("GPS: %.6f, %.6f | Heading: %d° | Fix: %s (%d) | Age: %lus\n",
                    cur_LAT, cur_LON, cur_IMU, fixQualityStr(gps_fix_quality),
                    gps_fix_quality, age);
    } else {
      Serial.printf("GPS: Waiting for fix... | UART RX: %lu bytes, %lu '$', %lu sentences | Baud: %ld\n",
                    gpsRxByteCount, gpsDollarCount, gpsNmeaParsed, gpsActiveBaud);
    }
  }

  delay(1);
}
