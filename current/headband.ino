#include <WiFi.h>
#include <esp_now.h>

const int MOTOR_PIN_5 = 5;
const int MOTOR_PIN_18 = 18;
const int MOTOR_PIN_19 = 19;
const int MOTOR_PIN_23 = 23;

const String ROW_NUM = "1";

// Updated callback function with the new signature
void OnDataRecv(const esp_now_recv_info_t *recv_info, const uint8_t *data, int len) {
  // Convert the sender's MAC address (from recv_info->src_addr) to a string
  char macStr[18];
  snprintf(macStr, sizeof(macStr), "%02X:%02X:%02X:%02X:%02X:%02X",
           recv_info->src_addr[0], recv_info->src_addr[1], recv_info->src_addr[2],
           recv_info->src_addr[3], recv_info->src_addr[4], recv_info->src_addr[5]);

  // Copy incoming data into a temporary buffer and ensure null termination
  char msg[len + 1];
  memcpy(msg, data, len);
  msg[len] = '\0';

  // Print details to the Serial Monitor
  Serial.print("Received from ");
  Serial.print(macStr);
  Serial.print(" - Message: ");
  Serial.println(msg);

  // Convert the C-string msg into an Arduino String for easier manipulation
  String msgStr(msg);

  // Find ';' which separates the row
  int separator1 = msgStr.indexOf(';');
  if (separator1 == -1) {
    Serial.println("Invalid command format (missing semicolon).");
    return;
  }

  // Find ':' which separates the pin number from the command
  int separator2 = msgStr.indexOf(':');
  if (separator2 == -1) {
    Serial.println("Invalid command format (missing colon).");
    return;
  }

  // Extract the row part and the command part
  String row = msgStr.substring(0, separator1);
  row.trim();
  if (row != ROW_NUM) {
    return;
  }

  String pinStr = msgStr.substring(separator1 + 1, separator2);
  String cmd = msgStr.substring(separator2 + 1);

  // Trim whitespace from the command (removes leading/trailing whitespace)
  cmd.trim();

  // Convert the pin substring to an integer
  int pin = pinStr.toInt();

  // Validate that the pin is one of our motor pins
  if (pin != MOTOR_PIN_5 && pin != MOTOR_PIN_18 && pin != MOTOR_PIN_19 && pin != MOTOR_PIN_23) {
    Serial.println("Invalid motor pin.");
    return;
  }

  Serial.printf("  Pin parsed: %d\n", pin);
  Serial.printf("  Command parsed: \"%s\"\n", cmd.c_str());

  // Execute the command: "1" to turn the motor ON, "0" to turn it OFF
  if (cmd == "1") {
    digitalWrite(pin, HIGH);
    Serial.printf("Motor ON at pin %d\n", pin);
  } else if (cmd == "0") {
    digitalWrite(pin, LOW);
    Serial.printf("Motor OFF at pin %d\n", pin);
  } else {
    Serial.println("Invalid command received.");
  }
}

void setup() {
  // Initialize Serial Monitor for debugging
  Serial.begin(115200);
  Serial.println("ESP32 ESP‑NOW Receiver Starting...");

  // Set the motor pins as outputs
  pinMode(MOTOR_PIN_5, OUTPUT);
  pinMode(MOTOR_PIN_18, OUTPUT);
  pinMode(MOTOR_PIN_19, OUTPUT);
  pinMode(MOTOR_PIN_23, OUTPUT);

  // Set Wi‑Fi mode to station (STA) mode
  WiFi.mode(WIFI_STA);

  // Initialize ESP‑NOW
  if (esp_now_init() != ESP_OK) {
    Serial.println("Error initializing ESP‑NOW");
    return;
  }
  Serial.println("ESP‑NOW initialized.");

  // Register the receive callback using the updated function signature
  esp_now_register_recv_cb(OnDataRecv);
}

void loop() {
  // Nothing to do here. The ESP‑NOW receive callback (OnDataRecv) handles incoming messages.
  delay(10);
}
