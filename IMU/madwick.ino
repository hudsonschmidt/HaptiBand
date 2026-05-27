#include <Adafruit_ICM20X.h>
#include <Adafruit_ICM20948.h>
#include <Adafruit_Sensor.h>
#include <Adafruit_AHRS.h>
#include <Wire.h>
#include <math.h>

Adafruit_ICM20948 icm;
Adafruit_Madgwick filter;

#define SDA_PIN 21
#define SCL_PIN 22

const float RAD_TO_DEG_F = 57.295779513f;

const float FILTER_HZ = 50.0;
const unsigned long UPDATE_PERIOD_US = 1000000.0 / FILTER_HZ;

// Gyro calibration offsets, in rad/s
float gyroBiasX = 0.0;
float gyroBiasY = 0.0;
float gyroBiasZ = 0.0;

// Starting neutral body orientation
float zeroYaw = 0.0;
float zeroPitch = 0.0;
float zeroRoll = 0.0;

unsigned long lastUpdate = 0;

float wrap360(float angle) {
  while (angle < 0.0) angle += 360.0;
  while (angle >= 360.0) angle -= 360.0;
  return angle;
}

float wrap180(float angle) {
  while (angle < -180.0) angle += 360.0;
  while (angle >= 180.0) angle -= 360.0;
  return angle;
}

void calibrateGyro() {
  sensors_event_t accel;
  sensors_event_t gyro;
  sensors_event_t mag;
  sensors_event_t temp;

  const int samples = 1000;

  float sumX = 0.0;
  float sumY = 0.0;
  float sumZ = 0.0;

  Serial.println("Calibrating gyro. Keep the IMU completely still...");

  for (int i = 0; i < samples; i++) {
    icm.getEvent(&accel, &gyro, &temp, &mag);

    sumX += gyro.gyro.x;
    sumY += gyro.gyro.y;
    sumZ += gyro.gyro.z;

    delay(3);
  }

  gyroBiasX = sumX / samples;
  gyroBiasY = sumY / samples;
  gyroBiasZ = sumZ / samples;

  Serial.println("Gyro calibration done.");
  Serial.print("Bias X rad/s: "); Serial.println(gyroBiasX, 6);
  Serial.print("Bias Y rad/s: "); Serial.println(gyroBiasY, 6);
  Serial.print("Bias Z rad/s: "); Serial.println(gyroBiasZ, 6);
}

void updateFilterOnce() {
  sensors_event_t accel;
  sensors_event_t gyro;
  sensors_event_t mag;
  sensors_event_t temp;

  icm.getEvent(&accel, &gyro, &temp, &mag);

  // Adafruit ICM20948 gyro values are rad/s.
  // The AHRS filter expects degrees/s.
  float gxDps = (gyro.gyro.x - gyroBiasX) * RAD_TO_DEG_F;
  float gyDps = (gyro.gyro.y - gyroBiasY) * RAD_TO_DEG_F;
  float gzDps = (gyro.gyro.z - gyroBiasZ) * RAD_TO_DEG_F;

  filter.update(
    gxDps,
    gyDps,
    gzDps,
    accel.acceleration.x,
    accel.acceleration.y,
    accel.acceleration.z,
    mag.magnetic.x,
    mag.magnetic.y,
    mag.magnetic.z
  );
}

void setup() {
  Serial.begin(115200);
  delay(2000);

  Serial.println();
  Serial.println("ICM20948 human/body orientation test");

  Wire.begin(SDA_PIN, SCL_PIN);

  if (!icm.begin_I2C()) {
    while (1) {
      Serial.println("Failed to find ICM20948 chip");
      Serial.println("Check SDA=21, SCL=22, 3V3, GND");
      delay(1000);
    }
  }

  Serial.println("ICM20948 Found!");

  // Good starting ranges for body movement
  icm.setAccelRange(ICM20948_ACCEL_RANGE_4_G);
  icm.setGyroRange(ICM20948_GYRO_RANGE_250_DPS);
  icm.setMagDataRate(AK09916_MAG_DATARATE_100_HZ);

  filter.begin(FILTER_HZ);

  calibrateGyro();

  Serial.println();
  Serial.println("Stand/sit in the neutral starting orientation.");
  Serial.println("Hold still while the filter settles...");
  delay(1000);

  // Let the filter stabilize
  for (int i = 0; i < 150; i++) {
    updateFilterOnce();
    delay(20);
  }

  zeroYaw = wrap360(filter.getYaw());
  zeroPitch = filter.getPitch();
  zeroRoll = filter.getRoll();

  Serial.println("Neutral orientation saved.");
  Serial.print("Initial yaw from magnetic north: ");
  Serial.print(zeroYaw, 2);
  Serial.println(" deg");

  Serial.println();
  Serial.println("gyroX_dps,gyroY_dps,gyroZ_dps,yawNorth_deg,pitch_deg,roll_deg,relativeYaw_deg,relativePitch_deg,relativeRoll_deg");

  lastUpdate = micros();
}

void loop() {
  unsigned long now = micros();

  if (now - lastUpdate < UPDATE_PERIOD_US) {
    return;
  }

  lastUpdate = now;

  sensors_event_t accel;
  sensors_event_t gyro;
  sensors_event_t mag;
  sensors_event_t temp;

  icm.getEvent(&accel, &gyro, &temp, &mag);

  float gxDps = (gyro.gyro.x - gyroBiasX) * RAD_TO_DEG_F;
  float gyDps = (gyro.gyro.y - gyroBiasY) * RAD_TO_DEG_F;
  float gzDps = (gyro.gyro.z - gyroBiasZ) * RAD_TO_DEG_F;

  filter.update(
    gxDps,
    gyDps,
    gzDps,
    accel.acceleration.x,
    accel.acceleration.y,
    accel.acceleration.z,
    mag.magnetic.x,
    mag.magnetic.y,
    mag.magnetic.z
  );

  float yaw = wrap360(filter.getYaw());
  float pitch = filter.getPitch();
  float roll = filter.getRoll();

  float relativeYaw = wrap180(yaw - zeroYaw);
  float relativePitch = pitch - zeroPitch;
  float relativeRoll = roll - zeroRoll;

  // Serial.print(gxDps, 3);
  // Serial.print(",");
  // Serial.print(gyDps, 3);
  // Serial.print(",");
  // Serial.print(gzDps, 3);
  // Serial.print(",");

  // Serial.print(yaw, 2);
  // Serial.print(",");
  // Serial.print(pitch, 2);
  // Serial.print(",");
  // Serial.print(roll, 2);
  // Serial.print(",");

  Serial.print(relativeYaw, 2);
  Serial.print(",");
  Serial.print(relativePitch, 2);
  Serial.print(",");
  Serial.println(relativeRoll, 2);
}