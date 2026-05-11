#include <WiFi.h>
#include <esp_now.h>
#include <mbedtls/md.h>
#include <mbedtls/base64.h>

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

// GPS and IMU data (HARDCODED TEMPORARILY)
const String GPS_DATA = "35.303276,-120.664299";
const String IMU_DATA = "194";

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

void setup() {
  Serial.begin(115200);
  Serial.println("ESP32 ESP-NOW Relay Starting...");

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
  WiFiClient client = tcpServer.available();
  
  if (client) {
    Serial.println("Client connected");

    while (client.connected()) {
      // Send GPS and IMU data every 5 seconds
      if (currentMillis - lastDataSentTime >= DATA_SEND_INTERVAL) {
        // Format: "GPS:35.303176,-120.664059|IMU:194"
        String dataToSend = "GPS:" + GPS_DATA + "|IMU:" + IMU_DATA;
        client.println(dataToSend);
        Serial.println("Sent to client: " + dataToSend);
        lastDataSentTime = currentMillis;
      }
      
      if (client.available()) {
        String received = client.readStringUntil('\n');
        received.trim();

        // Check if this is RTCM correction data
        if (received.startsWith(RTCM_PREFIX)) {
          String rtcmData = received.substring(strlen(RTCM_PREFIX));
          handleRTCMMessage(rtcmData);
          // No ACK for RTCM to reduce latency
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
      
      currentMillis = millis();
      delay(10);
    }

    client.stop();
    Serial.println("Client disconnected");
  }
  
  delay(10);
}
