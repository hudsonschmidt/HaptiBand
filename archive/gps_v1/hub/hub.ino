#include <WiFi.h>
#include <esp_now.h>

// Wi-Fi AP credentials
#define WIFI_SSID "PWMB Hub"
#define WIFI_PASS "12345678"  // Must be at least 8 characters
#define AP_CHANNEL 1          // Explicitly set the AP channel

// TCP server port
#define TCP_PORT 80

// GPS and IMU data
const String GPS_DATA = "35.303176,-120.664059";
const String IMU_DATA = "194";

WiFiServer tcpServer(TCP_PORT);
unsigned long lastDataSentTime = 0;
const unsigned long DATA_SEND_INTERVAL = 5000; // 5 seconds

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
        Serial.println("Received from client: " + received);

        // Relay via ESP‑NOW
        esp_now_send(broadcastAddress,
          (uint8_t*)received.c_str(),
          received.length());

        client.println("OK");
      }
      
      currentMillis = millis();
      delay(10);
    }

    client.stop();
    Serial.println("Client disconnected");
  }
  
  delay(10);
}
