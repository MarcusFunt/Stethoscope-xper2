/*
  XIAO MG24 Sense — buffered capture then bulk send
  Target: 8 kHz sample rate, up to 10 s (80k samples ~160 KB)
  Mic pin: PC3

  Protocol unchanged:
    Host sends:   REC,<sr_hz>,<num_samples>

    Device sends: DATA,<num_samples>
 + <binary int16 LE samples> + DONE

  Notes:
    - Uses a single global buffer to avoid heap fragmentation
    - Clamps sr to 8000 max and n to 80k max by default
*/
#include <Arduino.h>

#if defined(PC3)
  #define MIC_PIN PC3
#elif defined(PIN_PC3)
  #define MIC_PIN PIN_PC3
#else
  #warning "PC3 not defined by this core; adjust MIC_PIN."
  #define MIC_PIN A0
#endif

static const uint32_t MAX_SR = 8000;      // hard limit per your use case
static const uint32_t MAX_SECONDS = 10;
static const uint32_t MAX_SAMPLES = MAX_SR * MAX_SECONDS; // 80,000
static const uint16_t TX_CHUNK = 1024;    // samples per write during bulk send

static int16_t sampleBuf[MAX_SAMPLES];    // ~160 KB global buffer

bool parse_rec_cmd(const String &line, uint32_t &sr, uint32_t &n) {
  if (!line.startsWith("REC")) return false;
  int c1 = line.indexOf(',');
  int c2 = line.indexOf(',', c1 + 1);
  if (c1 < 0 || c2 < 0) return false;
  sr = (uint32_t) line.substring(c1 + 1, c2).toInt();
  n  = (uint32_t) line.substring(c2 + 1).toInt();
  if (sr == 0 || n == 0) return false;
  if (sr > MAX_SR) sr = MAX_SR;
  if (n  > MAX_SAMPLES) n = MAX_SAMPLES;
  return true;
}

void setup(){
  Serial.begin(115200);
  while(!Serial){delay(10);}
  #if defined(analogReadResolution)
    analogReadResolution(12);
  #endif
  pinMode(MIC_PIN, INPUT);
  Serial.println(F("READY"));
}

void record_then_send(uint32_t sr, uint32_t n){
  const uint32_t periodUs = 1000000UL / sr;
  uint32_t nextTick = micros() + 200;

  // --- record into RAM ---
  for(uint32_t i=0;i<n;i++){
    while((int32_t)(micros() - nextTick) < 0) { /* wait */ }
    nextTick += periodUs;

    int raw = analogRead(MIC_PIN);
    #if defined(ARDUINO_ARCH_SAMD) || defined(ARDUINO_ARCH_MBED) || defined(ARDUINO_ARCH_RENESAS) || defined(ARDUINO_ARCH_RP2040)
      sampleBuf[i] = (int16_t)(((int32_t)raw - 2048) * 16);  // 12-bit -> int16
    #else
      sampleBuf[i] = (int16_t)(((int32_t)raw - 512) * 64);   // 10-bit -> int16
    #endif
  }

  // --- bulk transmit ---
  Serial.print(F("DATA,"));
  Serial.println(n);

  uint32_t sent = 0;
  while(sent < n){
    uint32_t blk = min<uint32_t>(TX_CHUNK, n - sent);
    Serial.write((uint8_t*)&sampleBuf[sent], blk * sizeof(int16_t));
    sent += blk;
  }
  Serial.println(F("DONE"));
}

void loop(){
  if(Serial.available()){
    String line = Serial.readStringUntil('\n');
    line.trim();
    uint32_t sr=8000, n=0;
    if(parse_rec_cmd(line, sr, n)){
      Serial.println(F("ACK"));
      record_then_send(sr, n);
    }else{
      Serial.println(F("ERR"));
    }
  }
}