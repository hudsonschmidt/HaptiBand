#include <WiFi.h>
#include <esp_now.h>

const int MOTOR_PIN_5 = 5;
const int MOTOR_PIN_18 = 18;
const int MOTOR_PIN_19 = 19;
const int MOTOR_PIN_23 = 23;

const String ROW_NUM = "1";
const String COL_NUM = "2";

const String cur_GPS = "35.303176,-120.664059";
const String cur_IMU = "194";

// ── NEW helper ------------------------------------------------------
void buzz(int pin, bool on) {
  digitalWrite(pin, on ? HIGH : LOW);
}

// ── UPDATED callback -----------------------------------------------
void OnDataRecv(const esp_now_recv_info_t *info,
                const uint8_t *data, int len)
{
  if (len <= 0) return;                 // safety

  /* ----- make a clean, NUL-terminated copy of the payload -------- */
  char buf[len + 1];
  memcpy(buf, data, len);
  buf[len] = '\0';                      // hard terminate
  String msg(buf);
  msg.trim();                           // drop CR/LF if present

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

    Serial.printf("GPS=%s  IMU=%s\n", gps.c_str(), imu.c_str());

    if (gps == cur_GPS && imu == cur_IMU) {
      /* flash all four buzzers twice */
      for (int i = 0; i < 2; ++i) {
        buzz(MOTOR_PIN_5,  HIGH);
        buzz(MOTOR_PIN_18, HIGH);
        buzz(MOTOR_PIN_19, HIGH);
        buzz(MOTOR_PIN_23, HIGH);
        delay(100);                     // 0.1 s
        buzz(MOTOR_PIN_5,  LOW);
        buzz(MOTOR_PIN_18, LOW);
        buzz(MOTOR_PIN_19, LOW);
        buzz(MOTOR_PIN_23, LOW);
        delay(100);
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
  delay(10);
}
