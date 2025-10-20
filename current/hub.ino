#include <WiFi.h>
#include <esp_now.h>

// Wi-Fi AP credentials
#define WIFI_SSID "PWMB Hub"
#define WIFI_PASS "12345678"  // Must be at least 8 characters
#define AP_CHANNEL 1          // Explicitly set the AP channel

// TCP server port
#define TCP_PORT 80

WiFiServer tcpServer(TCP_PORT);

// Broadcast MAC address for ESP-NOW (to send to all peers)
uint8_t broadcastAddress[] = { 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF };

void OnDataSent(const uint8_t *mac_addr, esp_now_send_status_t status) {
  Serial.print("ESP-NOW broadcast status: ");
  Serial.println((status == ESP_NOW_SEND_SUCCESS) ? "Success" : "Failure");
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
  esp_now_peer_info_t peerInfo = {};
  memcpy(peerInfo.peer_addr, broadcastAddress, 6);
  peerInfo.channel = AP_CHANNEL;  // Use the same channel as the AP
  peerInfo.encrypt = false;

  // Add the broadcast peer
  if (esp_now_add_peer(&peerInfo) != ESP_OK) {
    Serial.println("Failed to add ESP-NOW broadcast peer");
    return;
  }
  Serial.println("Broadcast peer added successfully.");
}

void loop() {
  WiFiClient client = tcpServer.available();
  if (!client) {
    delay(10);
    return;
  }

  Serial.println("Client connected");

  while (client.connected()) {
    if (client.available()) {
      String received = client.readStringUntil('\n');
      received.trim();
      Serial.println("Received: " + received);

      // ── relay via ESP‑NOW ────────────────────────────────────────────
      esp_now_send(broadcastAddress,
        (uint8_t*)received.c_str(),
        received.length() + 1);

      client.println("OK");
    }
  }

  client.stop();
  Serial.println("Client disconnected");
}
