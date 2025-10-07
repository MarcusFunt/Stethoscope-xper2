/*
  XIAO MG24 Sense — buffered capture then bulk send
  Target: 8 kHz sample rate, up to 10 s (80k samples ~160 KB)
  Mic pin: PC9

  Protocol unchanged:
    Host sends:   REC,<sr_hz>,<num_samples>

    Device sends: DATA,<num_samples>
 + <binary int16 LE samples> + DONE

  Notes:
    - Allocates capture buffer on demand to conserve SRAM
    - Clamps sr to 8000 max and n to 80k max by default
*/
#include <Arduino.h>
#include <stdlib.h>

#if defined(PC9)
  #define MIC_PIN PC9
#elif defined(PIN_PC9)
  #define MIC_PIN PIN_PC9
#else
  #warning "PC9 not defined by this core; adjust MIC_PIN."
  #define MIC_PIN PC9
#endif

static const uint32_t MAX_SR = 8000;      // hard limit per your use case
static const uint32_t MAX_SECONDS = 10;
static const uint32_t MAX_SAMPLES = MAX_SR * MAX_SECONDS; // 80,000
static const uint16_t TX_CHUNK = 1024;    // samples per write during bulk send

static int16_t *sampleBuf = nullptr;
static uint32_t sampleBufCapacity = 0;

static uint16_t adcMidpoint = 512;
static int16_t adcScale = 64;

void configure_adc_scaling(uint8_t bits){
  if(bits < 2 || bits > 16){
    bits = 10;
  }

  uint32_t midpoint = 1u << (bits - 1);
  adcMidpoint = (uint16_t)midpoint;

  int32_t scaleNumerator = 1 << 15; // 32768, keeps output within int16 range
  int32_t divisor = (int32_t)midpoint;
  if(divisor <= 0){
    divisor = 1;
  }
  adcScale = (int16_t)(scaleNumerator / divisor);
  if(adcScale <= 0){
    adcScale = 1;
  }
}

bool ensure_buffer(uint32_t n){
  if(sampleBufCapacity >= n) return true;
  size_t bytes = (size_t)n * sizeof(int16_t);
  int16_t *newBuf = (int16_t*)realloc(sampleBuf, bytes);
  if(!newBuf){
    return false;
  }
  sampleBuf = newBuf;
  sampleBufCapacity = n;
  return true;
}

bool parse_rec_cmd(const String &line, uint32_t &sr, uint32_t &n) {
  if (!line.startsWith("REC")) return false;
  int c1 = line.indexOf(',');
  int c2 = line.indexOf(',', c1 + 1);
  if (c1 < 0 || c2 < 0) return false;
  uint32_t sr_requested = (uint32_t) line.substring(c1 + 1, c2).toInt();
  uint32_t n_requested  = (uint32_t) line.substring(c2 + 1).toInt();
  if (sr_requested == 0 || n_requested == 0) return false;

  if (n_requested > MAX_SAMPLES) {
    n_requested = MAX_SAMPLES;
  }

  sr = sr_requested;
  if (sr > MAX_SR) {
    sr = MAX_SR;
  }

  uint32_t effective_n = n_requested;
  if (sr_requested != sr) {
    uint64_t scaled = (uint64_t)n_requested * sr + (sr_requested / 2);
    scaled /= sr_requested;
    if (scaled == 0) {
      scaled = 1;
    }
    if (scaled > MAX_SAMPLES) {
      scaled = MAX_SAMPLES;
    }
    effective_n = (uint32_t)scaled;
  }

  if (effective_n > MAX_SAMPLES) {
    effective_n = MAX_SAMPLES;
  }

  n = effective_n;
  return true;
}

void setup(){
  Serial.begin(115200);
  while(!Serial){delay(10);}
  configure_adc_scaling(10);
  #if defined(analogReadResolution)
    analogReadResolution(12);
    configure_adc_scaling(12);
  #elif defined(ADC_RESOLUTION)
    configure_adc_scaling((uint8_t)ADC_RESOLUTION);
  #endif
  pinMode(MIC_PIN, INPUT);
  Serial.println(F("READY"));
}

void record_then_send(uint32_t sr, uint32_t n){
  if(!ensure_buffer(n)){
    Serial.println(F("ERR,BUF"));
    return;
  }
  const uint32_t periodUs = 1000000UL / sr;
  uint32_t nextTick = micros() + 200;

  // --- record into RAM ---
  for(uint32_t i=0;i<n;i++){
    while((int32_t)(micros() - nextTick) < 0) { /* wait */ }
    nextTick += periodUs;

    int raw = analogRead(MIC_PIN);
    sampleBuf[i] = (int16_t)(((int32_t)raw - (int32_t)adcMidpoint) * adcScale);
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