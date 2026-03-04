#include <WiFi.h>
#include <esp_now.h>
#include <math.h>

const int MOTOR_PIN_5 = 5;
const int MOTOR_PIN_18 = 18;
const int MOTOR_PIN_19 = 19;
const int MOTOR_PIN_23 = 23;

const String ROW_NUM = "1";
const String COL_NUM = "3";

// HARDCODED TEMPORARILY
const float cur_LAT = 35.303276;
const float cur_LON = -120.664299;
const int cur_IMU = 194;

// Tolerance for GPS comparison (~0.3 feet at this latitude)
const float GPS_TOLERANCE = 0.000001;

// IMU tolerance thresholds (degrees)
const int IMU_DEADZONE = 5;       // no correction needed
const int IMU_SOFT_LIMIT = 15;    // gentle correction
// beyond SOFT_LIMIT = urgent correction

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

// callback -----------------------------------------------
void OnDataRecv(const esp_now_recv_info *info, const uint8_t *data, int len)
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

    // Parse received GPS coordinates
    int comma = gps.indexOf(',');
    if (comma < 0) {
      Serial.println(F("Ill-formed GPS (missing comma)"));
      return;
    }
    float recv_lat = gps.substring(0, comma).toFloat();
    float recv_lon = gps.substring(comma + 1).toFloat();
    int recv_imu = imu.toInt();

    Serial.printf("Target GPS=%.6f,%.6f  IMU=%d\n", recv_lat, recv_lon, recv_imu);
    Serial.printf("Current GPS=%.6f,%.6f  IMU=%d\n", cur_LAT, cur_LON, cur_IMU);

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
