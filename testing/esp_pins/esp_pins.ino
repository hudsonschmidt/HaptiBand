void setup() {
    Serial.begin(115200);
    Serial.printf("PSRAM size: %d\n", ESP.getPsramSize());
    Serial.printf("Free PSRAM: %d\n", ESP.getFreePsram());
    if (ESP.getPsramSize() > 0) {
      Serial.println("*** THIS IS A WROVER - GPIO16/17 ARE NOT AVAILABLE ***");
      Serial.println("*** USE DIFFERENT PINS FOR UART2 (e.g. 25/26) ***");
    } else {
      Serial.println("This is a WROOM - GPIO16/17 are fine");
    }
  }
  void loop() {}