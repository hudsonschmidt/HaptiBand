#include <HardwareSerial.h>                                                                                                                   
HardwareSerial GPSSerial(2);                                                                                                                  
                                                                                                                                                
void setup() {                                                                                                                                
    Serial.begin(115200);                                                                                                                       
    Serial.println("=== BARE GPS TEST ===");                                                                                                    
                                                                                                                                              
    // Try pins 25/26 to rule out GPIO16/17 issues
    GPSSerial.setRxBufferSize(2048);      
    GPSSerial.begin(460800, SERIAL_8N1, 25, 26);                                                                                                
    Serial.println("460800 baud on GPIO25(RX) GPIO26(TX)");                                                                                     
}                                                                                                                                             
                                                                                                                                                
void loop() {                                                                                                                               
    int avail = GPSSerial.available();                                                                                                          
    if (avail > 0) {
      Serial.printf("[%d bytes] ", avail);                                                                                                      
      while (GPSSerial.available()) {                                                                                                         
        char c = GPSSerial.read();            
        Serial.write(c);                                                                                                                        
      }
      Serial.println();                                                                                                                         
    }                                                                                                                                         
                                                                                                                                                
    // Print heartbeat every 3 seconds so we know it's running                                                                                
    static unsigned long last = 0;        
    if (millis() - last > 3000) {             
      last = millis();                                                                                                                          
      Serial.println("... waiting for GPS data ...");
    }                                                                                                                                           
} 