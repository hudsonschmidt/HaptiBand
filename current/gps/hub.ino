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
// Format: 0xD3 (RTCM preamble) followed by raw RTCM data
#define RTCM_PREFIX "RTCM:"
#define RTCM_ESPNOW_PREFIX 0xD3  // Standard RTCM3 preamble byte

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

// TCP server port
#define TCP_PORT 80

const String ROW_NUM = "0";
const String COL_NUM = "3";

// ─────────────── RTK GPS Configuration ────────────────────
// RTK GPS serial connection (UART2)
// Wiring: Click board TX → ESP32 GPIO21, Click board RX → ESP32 GPIO22
#define GPS_RX_PIN 21
#define GPS_TX_PIN 22
// Control pins — directly drive these from ESP32 GPIOs
#define GPS_RST_PIN 4    // Click board RST → ESP32 D4
#define GPS_CEN_PIN 2    // Click board CS  → ESP32 D2 (chip enable / LDO power)
#define GPS_WUP_PIN 15   // Click board PWM → ESP32 D15 (wakeup pulse)
HardwareSerial GPSSerial(2);

// Baud rates to try during auto-detection (most likely first)
static const long GPS_BAUDS[] = {460800, 115200, 230400, 921600, 38400, 9600};
static const int  GPS_BAUD_COUNT = sizeof(GPS_BAUDS) / sizeof(GPS_BAUDS[0]);
static long       gpsActiveBaud = 0;

// Live GPS state (updated from RTK module NMEA output)
static double cur_LAT = 0.0;
static double cur_LON = 0.0;
static int    cur_IMU = 0;          // heading from course over ground
static uint8_t gps_fix_quality = 0; // 0=none, 1=GPS, 2=DGPS, 4=RTK fixed, 5=RTK float
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
const unsigned long DATA_SEND_INTERVAL = 5000; // 5 seconds

// Broadcast MAC address for ESP-NOW (to send to all peers)
uint8_t broadcastAddress[] = { 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF };

void OnDataSent(const wifi_tx_info_t *info, esp_now_send_status_t status) {
  Serial.print("ESP-NOW broadcast status: ");
  Serial.println((status == ESP_NOW_SEND_SUCCESS) ? "Success" : "Failure");
}

// Generate HMAC-SHA256 and return first 8 bytes as hex string (16 chars)
String generateHMAC(uint32_t seq, const String& payload) {
  // Create message: "seq:payload"
  String message = String(seq) + ":" + payload;

  uint8_t hmacResult[32];
  mbedtls_md_context_t ctx;
  mbedtls_md_init(&ctx);
  mbedtls_md_setup(&ctx, mbedtls_md_info_from_type(MBEDTLS_MD_SHA256), 1);
  mbedtls_md_hmac_starts(&ctx, HMAC_KEY, sizeof(HMAC_KEY));
  mbedtls_md_hmac_update(&ctx, (const unsigned char*)message.c_str(), message.length());
  mbedtls_md_hmac_finish(&ctx, hmacResult);
  mbedtls_md_free(&ctx);

  // Convert first 8 bytes to hex string
  char hexStr[17];
  for (int i = 0; i < 8; i++) {
    sprintf(&hexStr[i * 2], "%02x", hmacResult[i]);
  }
  hexStr[16] = '\0';
  return String(hexStr);
}

// Create authenticated message: "seq:payload:hmac"
String createAuthenticatedMessage(const String& payload) {
  uint32_t seq = txSequence++;
  String hmac = generateHMAC(seq, payload);
  return String(seq) + ":" + payload + ":" + hmac;
}

// Decode base64 and broadcast RTCM data via ESP-NOW
// RTCM messages skip HMAC for latency - they're time-sensitive corrections
void handleRTCMMessage(const String& base64Data) {
  // Decode base64
  size_t inputLen = base64Data.length();
  size_t outputLen = 0;

  // First, get required output length
  unsigned char* decoded = (unsigned char*)malloc(inputLen);  // Output always smaller than input
  if (!decoded) {
    Serial.println("RTCM: malloc failed");
    return;
  }

  int ret = mbedtls_base64_decode(decoded, inputLen, &outputLen,
                                   (const unsigned char*)base64Data.c_str(), inputLen);
  if (ret != 0) {
    Serial.printf("RTCM: base64 decode error %d\n", ret);
    free(decoded);
    return;
  }

  Serial.printf("RTCM: decoded %zu bytes\n", outputLen);

  // Broadcast via ESP-NOW, chunking if necessary
  // Prepend RTCM marker byte (0xD3) to distinguish from control messages
  // Each chunk has 2 bytes header, so payload per chunk is (ESPNOW_MAX_PAYLOAD - 2)
  const size_t CHUNK_PAYLOAD_SIZE = ESPNOW_MAX_PAYLOAD - 2;
  size_t offset = 0;
  uint8_t chunkNum = 0;
  uint8_t totalChunks = (outputLen + CHUNK_PAYLOAD_SIZE - 1) / CHUNK_PAYLOAD_SIZE;

  while (offset < outputLen) {
    size_t remaining = outputLen - offset;
    size_t chunkSize = min(remaining, CHUNK_PAYLOAD_SIZE);

    // Build chunk: [0xD3][chunkNum|totalChunks][data...]
    uint8_t packet[ESPNOW_MAX_PAYLOAD];
    packet[0] = RTCM_ESPNOW_PREFIX;
    packet[1] = (chunkNum << 4) | (totalChunks & 0x0F);
    memcpy(packet + 2, decoded + offset, chunkSize);

    esp_now_send(broadcastAddress, packet, chunkSize + 2);

    offset += chunkSize;
    chunkNum++;
    delay(1);  // Small delay between chunks
  }

  free(decoded);
  Serial.printf("RTCM: sent %d chunks\n", chunkNum);
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
      if (!GPSSerial.available()) delayMicroseconds(100);
    }

    Serial.printf("  -> %d bytes, %d ASCII, dollar=%s\n",
                  totalBytes, asciiCount, sawDollar ? "YES" : "no");

    if (sawDollar || (totalBytes > 10 && asciiCount > totalBytes / 2)) {
      Serial.printf("GPS detected at %ld baud\n", baud);
      GPSSerial.end();
      GPSSerial.setRxBufferSize(2048);
      GPSSerial.begin(baud, SERIAL_8N1, GPS_RX_PIN, GPS_TX_PIN);
      return baud;
    }

    if (totalBytes > 0) {
      Serial.printf("  -> garbage data, wrong baud\n");
    }
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
      if (current == fieldNum) {
        return String(sentence).substring(start - sentence, p - sentence);
      }
      current++;
      start = p + 1;
    }
  }
  if (current == fieldNum) {
    return String(start);
  }
  return "";
}

double nmeaToDecimal(const char* raw, int rawLen, const String& dir) {
  if (rawLen == 0) return 0.0;

  int dotPos = -1;
  for (int i = 0; i < rawLen; i++) {
    if (raw[i] == '.') { dotPos = i; break; }
  }
  if (dotPos < 0) return 0.0;

  int degLen = dotPos - 2;
  if (degLen < 1) return 0.0;

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

void parseNMEA(const char* sentence) {
  if (strlen(sentence) < 7) return;
  const char* type = sentence + 3;

  if (strncmp(type, "GGA,", 4) == 0) {
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
    }

  } else if (strncmp(type, "RMC,", 4) == 0) {
    String status = nmeaField(sentence, 2);

    if (status == "A") {
      String lat_raw = nmeaField(sentence, 3);
      String lat_dir = nmeaField(sentence, 4);
      String lon_raw = nmeaField(sentence, 5);
      String lon_dir = nmeaField(sentence, 6);
      String course  = nmeaField(sentence, 8);

      if (lat_raw.length() > 0 && lon_raw.length() > 0) {
        cur_LAT = nmeaToDecimal(lat_raw.c_str(), lat_raw.length(), lat_dir);
        cur_LON = nmeaToDecimal(lon_raw.c_str(), lon_raw.length(), lon_dir);
        gps_valid = true;
      }

      if (course.length() > 0) {
        cur_IMU = (int)course.toFloat();
      }
    }
  }
}

void readGPS() {
  while (GPSSerial.available()) {
    char c = GPSSerial.read();
    gpsRxByteCount++;

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
      nmeaIdx = 0;
      nmeaBuf[nmeaIdx++] = c;
    } else if (c == '\n' || c == '\r') {
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

void setup() {
  Serial.begin(115200);
  Serial.println("ESP32 ESP-NOW Relay Starting...");

  // ─── Initialize RTK GPS module ───
  pinMode(GPS_RST_PIN, OUTPUT);
  pinMode(GPS_CEN_PIN, OUTPUT);
  pinMode(GPS_WUP_PIN, OUTPUT);

  // Step 1: Enable module power via CEN (CS) pin
  Serial.println("Enabling GPS module (CEN HIGH)...");
  digitalWrite(GPS_CEN_PIN, HIGH);
  delay(1000);
  digitalWrite(GPS_CEN_PIN, LOW);
  delay(1000);
  digitalWrite(GPS_CEN_PIN, HIGH);
  delay(1000);

  // Step 2: Wake up module with WUP pulse
  Serial.println("Sending WUP pulse...");
  digitalWrite(GPS_WUP_PIN, HIGH);
  delay(100);
  digitalWrite(GPS_WUP_PIN, LOW);
  delay(1000);

  // Step 3: Hardware reset
  Serial.println("Resetting GPS module (RST pulse)...");
  digitalWrite(GPS_RST_PIN, LOW);
  delay(100);
  digitalWrite(GPS_RST_PIN, HIGH);
  delay(2000);

  // Auto-detect GPS baud rate
  Serial.println("Detecting GPS module baud rate...");
  gpsActiveBaud = detectGPSBaud();
  if (gpsActiveBaud > 0) {
    Serial.printf("GPS module responding at %ld baud\n", gpsActiveBaud);
  } else {
    Serial.println("GPS module not detected — check wiring and power");
  }

  // Set Wi-Fi mode to AP+STA for proper ESP-NOW operation with a soft AP.
  WiFi.mode(WIFI_AP_STA);
  WiFi.softAP(WIFI_SSID, WIFI_PASS, AP_CHANNEL);
  Serial.print("AP IP address: ");
  Serial.println(WiFi.softAPIP());

  tcpServer.begin();
  Serial.println("TCP server started on port " + String(TCP_PORT));

  // Initialize ESP-NOW
  if (esp_now_init() != ESP_OK) {
    Serial.println("Error initializing ESP-NOW");
    return;
  }
  Serial.println("ESP-NOW initialized.");

  // Register the send callback
  esp_now_register_send_cb(OnDataSent);

  // Configure the broadcast peer
  // Note: ESP-NOW encryption doesn't work with broadcast (0xFF:FF:FF:FF:FF:FF)
  // We use application-layer HMAC authentication instead
  esp_now_peer_info_t peerInfo = {};
  memcpy(peerInfo.peer_addr, broadcastAddress, 6);
  peerInfo.channel = AP_CHANNEL;
  peerInfo.encrypt = false;  // Use app-layer HMAC instead

  // Add the broadcast peer
  if (esp_now_add_peer(&peerInfo) != ESP_OK) {
    Serial.println("Failed to add ESP-NOW broadcast peer");
    return;
  }
  Serial.println("Broadcast peer added successfully.");
}

void loop() {
  unsigned long currentMillis = millis();

  // Read NMEA sentences from RTK GPS module
  readGPS();

  // Print GPS status periodically
  if (currentMillis - lastGpsPrint >= GPS_PRINT_INTERVAL) {
    lastGpsPrint = currentMillis;
    if (gps_valid) {
      Serial.printf("GPS: %.8f, %.8f | Heading: %d° | Fix: %s (%d)\n",
                    cur_LAT, cur_LON, cur_IMU, fixQualityStr(gps_fix_quality),
                    gps_fix_quality);
    } else {
      Serial.printf("GPS: Waiting for fix... | UART RX: %lu bytes, %lu '$', %lu sentences | Baud: %ld\n",
                    gpsRxByteCount, gpsDollarCount, gpsNmeaParsed, gpsActiveBaud);
    }
  }

  WiFiClient client = tcpServer.available();

  if (client) {
    Serial.println("Client connected");

    while (client.connected()) {
      currentMillis = millis();

      // Read GPS continuously while client is connected
      readGPS();

      // Send live GPS and IMU data every 5 seconds
      if (currentMillis - lastDataSentTime >= DATA_SEND_INTERVAL) {
        if (gps_valid) {
          // Build string with full precision
          char dataStr[80];
          snprintf(dataStr, sizeof(dataStr), "GPS:%.8f,%.8f|IMU:%d", cur_LAT, cur_LON, cur_IMU);
          client.println(dataStr);
          Serial.printf("Sent to client: %s | Fix: %s\n", dataStr, fixQualityStr(gps_fix_quality));
        } else {
          client.println("GPS:0,0|IMU:0");
          Serial.println("Sent to client: no fix yet");
        }
        lastDataSentTime = currentMillis;
      }

      if (client.available()) {
        String received = client.readStringUntil('\n');
        received.trim();

        // Check if this is RTCM correction data
        if (received.startsWith(RTCM_PREFIX)) {
          String rtcmData = received.substring(strlen(RTCM_PREFIX));
          handleRTCMMessage(rtcmData);

          // Also forward RTCM to hub's own GPS module
          size_t inputLen = rtcmData.length();
          unsigned char* decoded = (unsigned char*)malloc(inputLen);
          if (decoded) {
            size_t outputLen = 0;
            int ret = mbedtls_base64_decode(decoded, inputLen, &outputLen,
                                             (const unsigned char*)rtcmData.c_str(), inputLen);
            if (ret == 0 && outputLen > 0) {
              GPSSerial.write(decoded, outputLen);
            }
            free(decoded);
          }
        } else {
          // Regular control message - authenticate and relay
          Serial.println("Received from client: " + received);
          String authMsg = createAuthenticatedMessage(received);
          Serial.println("Sending authenticated: " + authMsg);
          esp_now_send(broadcastAddress,
            (uint8_t*)authMsg.c_str(),
            authMsg.length());
          client.println("OK");
        }
      }

      delay(1);
    }

    client.stop();
    Serial.println("Client disconnected");
  }

  delay(1);
}
