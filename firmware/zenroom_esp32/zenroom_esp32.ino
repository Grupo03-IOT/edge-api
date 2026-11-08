// ESP32 clasico, Arduino-ESP32 3.x y DHT sensor library de Adafruit.
#include <Arduino.h>
#include <WiFi.h>
#include <HTTPClient.h>
#include <LittleFS.h>
#include <DHT.h>
#include <esp_system.h>
#include <time.h>
#include <math.h>
#include <atomic>
#include "DeviceConfig.h"
#include "DeviceSignals.h"

DHT dht(DHT_PIN, DHT22);
SemaphoreHandle_t fsMutex;
QueueHandle_t buzzerQueue = nullptr;
std::atomic<bool> automaticBuzzerAllowed{false};
bool fsReady = false, pwmReady = false;
String bootId;
uint32_t sequence = 0;
float temperature = NAN, humidity = NAN, distanceCm = NAN;
float noiseLevel = NAN;
unsigned potAdc = 0;
bool climateValid = false, occupied = false, detected = false;
uint32_t lastDetectionMs = 0, manualUntil = 0, mutedUntil = 0;
int manualPresence = 0;  // 0 automatico, 1 ocupado, 2 vacio
ComfortState currentComfort = ComfortState::NoData;
RedAlertLatch redAlert;
uint32_t redSinceMs = 0;
bool buttonRaw[3] = {HIGH, HIGH, HIGH}, buttonStable[3] = {HIGH, HIGH, HIGH};
uint32_t buttonChanged[3] = {0, 0, 0};
float secondsLeq[10], occupiedSeconds = 0, secondEnergy = 0;
float minLevel = 140, maxLevel = 0;
unsigned histogram[70] = {0}, sampleCount = 0, transitions = 0;
time_t batchStart = 0;

bool pendingTime(uint32_t deadline) {
  return deadline != 0 && int32_t(deadline - millis()) > 0;
}

String quoted(const char *value) {
  String result = "\"";
  for (const char *p = value; *p; ++p) {
    if (*p == '"' || *p == '\\') result += '\\';
    if (uint8_t(*p) < 32) result += ' '; else result += *p;
  }
  return result + '"';
}

void beep(unsigned durationMs, bool automatic = false) {
  if (!buzzerQueue) return;
  BuzzerCommand command{BuzzerAction::Tone, durationMs, automatic};
  xQueueSend(buzzerQueue, &command, 0);
}

void stopBeep(bool automaticOnly = false) {
  if (!buzzerQueue) return;
  if (!automaticOnly) xQueueReset(buzzerQueue);
  BuzzerCommand command{automaticOnly ? BuzzerAction::StopAutomatic : BuzzerAction::StopAll, 0, false};
  xQueueSendToFront(buzzerQueue, &command, 0);
}

void buzzerWorker(void *) {
  BuzzerWindow window;
  bool automatic = false;
  for (;;) {
    uint32_t now = millis();
    if (window.expired(now)) {
      ledcWrite(BUZZER_PIN, 0);
      window.stop(now);
    }
    TickType_t wait = window.active()
      ? max(TickType_t(1), pdMS_TO_TICKS(window.remaining(now))) : portMAX_DELAY;
    BuzzerCommand command;
    if (xQueueReceive(buzzerQueue, &command, wait) != pdTRUE) continue;
    now = millis();
    if (window.expired(now)) {
      ledcWrite(BUZZER_PIN, 0);
      window.stop(now);
    }
    if (command.action == BuzzerAction::StopAll
        || (command.action == BuzzerAction::StopAutomatic && automatic)) {
      ledcWrite(BUZZER_PIN, 0);
      window.stop(now);
    } else if (command.action == BuzzerAction::Tone
               && (!command.automatic || automaticBuzzerAllowed.load())
               && window.start(now, command.durationMs)) {
      automatic = command.automatic;
      ledcWriteTone(BUZZER_PIN, 1600);
    }
  }
}

void updateIndicators(float level) {
  ComfortState next = comfortState(climateValid, temperature, humidity, occupied, level);
  automaticBuzzerAllowed.store(next == ComfortState::Red && !pendingTime(mutedUntil)
                              && ENABLE_AUTOMATIC_BUZZER);
  bool enteredRed = next == ComfortState::Red && currentComfort != ComfortState::Red;
  if (enteredRed) redSinceMs = millis();
  if (next != ComfortState::Red && currentComfort == ComfortState::Red) stopBeep(true);
  currentComfort = next;
  digitalWrite(LED_GREEN_PIN, next == ComfortState::Optimal ? HIGH : LOW);
  digitalWrite(LED_AMBER_PIN, next == ComfortState::Moderate ? HIGH : LOW);
  bool redOn = next == ComfortState::Red && (millis() - redSinceMs) % 1000 < 500;
  digitalWrite(LED_RED_PIN, redOn ? HIGH : LOW);
  if (redAlert.update(next, pendingTime(mutedUntil)) && ENABLE_AUTOMATIC_BUZZER)
    beep(BuzzerWindow::MaxMs, true);
}

void handleButtons() {
  uint32_t now = millis();
  for (unsigned i = 0; i < 3; ++i) {
    bool value = digitalRead(BUTTON_PINS[i]);
    if (value != buttonRaw[i]) { buttonRaw[i] = value; buttonChanged[i] = now; }
    if (now - buttonChanged[i] < 30 || value == buttonStable[i]) continue;
    buttonStable[i] = value;
    if (value != LOW) continue;
    if (i == 0) {
      beep(250);
      Serial.println("Prueba manual del buzzer: 250 ms");
    } else if (i == 1) {
      mutedUntil = now + MUTE_MS;
      automaticBuzzerAllowed.store(false);
      stopBeep();
      Serial.println("Avisos automaticos silenciados durante 60 s");
    } else {
      manualPresence = (manualPresence + 1) % 3;
      manualUntil = manualPresence ? now + MANUAL_PRESENCE_MS : 0;
      Serial.printf("Presencia manual: %s (caduca en 60 s)\n",
                    manualPresence == 1 ? "ocupada" : manualPresence == 2 ? "vacia" : "automatica");
    }
  }
  if (manualPresence && !pendingTime(manualUntil)) manualPresence = 0;
  if (mutedUntil && !pendingTime(mutedUntil)) mutedUntil = 0;
}

void readPresence() {
  digitalWrite(TRIG_PIN, LOW);
  delayMicroseconds(2);
  digitalWrite(TRIG_PIN, HIGH);
  delayMicroseconds(10);
  digitalWrite(TRIG_PIN, LOW);
  unsigned long echo = pulseIn(ECHO_PIN, HIGH, 15000);
  distanceCm = echo ? echo * 0.0343f / 2.0f : NAN;
  // El PIR puede estabilizarse durante el primer minuto.
  bool motion = millis() >= 60000 && digitalRead(PIR_PIN) == HIGH;
  if (motion || (isfinite(distanceCm) && distanceCm >= 2 && distanceCm < PRESENCE_DISTANCE_CM)) {
    detected = true;
    lastDetectionMs = millis();
  }
  bool next = detected && millis() - lastDetectionMs < PRESENCE_HOLD_MS;
  if (manualPresence) next = manualPresence == 1;
  if (next != occupied) ++transitions;
  occupied = next;
}

void resetBatch() {
  sampleCount = 0;
  secondEnergy = 0;
  occupiedSeconds = 0;
  transitions = 0;
  minLevel = 140;
  maxLevel = 0;
  memset(histogram, 0, sizeof(histogram));
}

String makeBatch() {
  struct tm utc;
  gmtime_r(&batchStart, &utc);
  char timestamp[24];
  strftime(timestamp, sizeof(timestamp), "%Y-%m-%dT%H:%M:%SZ", &utc);
  String body;
  body.reserve(1400);
  body = "{\"device_id\":" + quoted(DEVICE_ID) + ",\"room_id\":" + quoted(ROOM_ID);
  body += ",\"boot_id\":\"" + bootId + "\",\"fw_version\":" + quoted(FW_VERSION);
  body += ",\"seq\":" + String(++sequence) + ",\"ts\":\"" + String(timestamp);
  body += "\",\"window_s\":10,\"acoustic\":{\"laeq_1s\":[";
  for (unsigned i = 0; i < 10; ++i) { if (i) body += ','; body += String(secondsLeq[i], 1); }
  body += "],\"lmax\":" + String(maxLevel, 1) + ",\"lmin\":" + String(minLevel, 1);
  body += ",\"hist_min_db\":30,\"hist_bin_db\":1,\"hist\":[";
  for (unsigned i = 0; i < 70; ++i) { if (i) body += ','; body += String(histogram[i]); }
  body += "]},\"climate\":{\"temp_c\":" + String(temperature, 2);
  body += ",\"rh_pct\":" + String(humidity, 1) + "},\"presence\":{\"state\":\"";
  body += occupied ? "occupied" : "vacant";
  body += "\",\"occupied_s\":" + String(occupiedSeconds, 3);
  body += ",\"transitions\":" + String(transitions) + "}}";
  return body;
}

unsigned queueSize() {
  unsigned count = 0;
  File root = LittleFS.open("/");
  for (File file = root.openNextFile(); file; file = root.openNextFile()) {
    if (String(file.name()).endsWith(".json")) ++count;
    file.close();
  }
  return count;
}

void enqueueBatch(const String &body) {
  if (!fsReady) { Serial.println("Sin almacenamiento: lote no guardado"); return; }
  xSemaphoreTake(fsMutex, portMAX_DELAY);
  if (queueSize() >= MAX_QUEUED_BATCHES) {
    Serial.println("Cola llena: lote nuevo descartado; recuperar conexion al Edge");
  } else {
    char name[64];
    snprintf(name, sizeof(name), "/%010lld_%010lu_%s", (long long)batchStart,
             (unsigned long)sequence, bootId.substring(0, 8).c_str());
    String temp = String(name) + ".tmp", target = String(name) + ".json";
    File file = LittleFS.open(temp, "w");
    bool saved = file && file.print(body) == body.length();
    file.close();
    if (!saved || !LittleFS.rename(temp, target)) {
      LittleFS.remove(temp);
      Serial.println("No se pudo guardar el lote; comprobar espacio LittleFS");
    } else Serial.printf("Lote %lu guardado; pendientes %u\n", (unsigned long)sequence, queueSize());
  }
  xSemaphoreGive(fsMutex);
}

void networkWorker(void *) {
  uint32_t lastWifi = 0;
  for (;;) {
    if (WiFi.status() != WL_CONNECTED) {
      if (millis() - lastWifi >= 10000) {
        lastWifi = millis();
        WiFi.begin(WIFI_SSID, WIFI_PASSWORD);
      }
    } else if (fsReady) {
      String oldest, body;
      xSemaphoreTake(fsMutex, portMAX_DELAY);
      File root = LittleFS.open("/");
      for (File file = root.openNextFile(); file; file = root.openNextFile()) {
        String name = file.name();
        if (!name.startsWith("/")) name = "/" + name;
        if (name.endsWith(".json") && (oldest.isEmpty() || name.compareTo(oldest) < 0)) oldest = name;
        file.close();
      }
      if (!oldest.isEmpty()) {
        File file = LittleFS.open(oldest, "r");
        body = file.readString();
        file.close();
      }
      xSemaphoreGive(fsMutex);
      if (!body.isEmpty()) {
        WiFiClient connection;
        HTTPClient http;
        http.setConnectTimeout(1500);
        http.setTimeout(2000);
        if (http.begin(connection, EDGE_READINGS_URL)) {
          http.addHeader("Content-Type", "application/json");
          int status = http.POST(body);
          if (status >= 200 && status < 300) {
            xSemaphoreTake(fsMutex, portMAX_DELAY);
            LittleFS.remove(oldest);
            xSemaphoreGive(fsMutex);
          } else Serial.printf("Edge HTTP %d: lote conservado para reintentar\n", status);
          http.end();
        }
      }
    }
    vTaskDelay(pdMS_TO_TICKS(3000));
  }
}

float readNoiseLevel() {
  unsigned adc = 0;
  for (unsigned i = 0; i < 8; ++i) adc += analogRead(POT_PIN);
  potAdc = adc / 8;
  return 30.0f + (adc / 8.0f) * (69.9f / 4095.0f);
}

void sampleNoise(float level) {
  time_t now = time(nullptr);
  if (now < 1577836800 || !climateValid) { resetBatch(); return; }
  if (sampleCount == 0) {
    if (now % 10 != 0) return;  // ventanas alineadas al minuto UTC
    batchStart = now;
    transitions = 0;
  }
  minLevel = min(minLevel, level);
  maxLevel = max(maxLevel, level);
  ++histogram[constrain(int(level - 30), 0, 69)];
  secondEnergy += powf(10.0f, level / 10.0f);
  if (occupied) occupiedSeconds += 0.125f;
  ++sampleCount;
  if (sampleCount % 8 == 0) {
    secondsLeq[sampleCount / 8 - 1] = 10 * log10f(secondEnergy / 8);
    secondEnergy = 0;
  }
  if (sampleCount == 80) { enqueueBatch(makeBatch()); resetBatch(); }
}

void setup() {
  Serial.begin(115200);
  for (int pin : BUTTON_PINS) pinMode(pin, INPUT_PULLUP);
  for (int pin : {LED_GREEN_PIN, LED_AMBER_PIN, LED_RED_PIN}) {
    pinMode(pin, OUTPUT);
    digitalWrite(pin, LOW);
  }
  pinMode(PIR_PIN, INPUT);
  pinMode(TRIG_PIN, OUTPUT);
  pinMode(ECHO_PIN, INPUT);
  pinMode(BUZZER_PIN, OUTPUT);
  digitalWrite(BUZZER_PIN, BUZZER_ACTIVE_LOW ? HIGH : LOW);
  pwmReady = ledcAttach(BUZZER_PIN, 1600, 8);
  if (pwmReady) {
    ledcOutputInvert(BUZZER_PIN, BUZZER_ACTIVE_LOW);
    ledcWrite(BUZZER_PIN, 0);
    buzzerQueue = xQueueCreate(8, sizeof(BuzzerCommand));
    if (!buzzerQueue || xTaskCreate(buzzerWorker, "buzzer", 3072, nullptr, 3, nullptr) != pdPASS) {
      buzzerQueue = nullptr;
      Serial.println("Buzzer deshabilitado: no se pudo iniciar su temporizador");
    }
  }
  analogReadResolution(12);
  analogSetPinAttenuation(POT_PIN, ADC_11db);
  dht.begin();
  char boot[33];
  snprintf(boot, sizeof(boot), "%08lx%08lx%08lx%08lx", (unsigned long)esp_random(),
           (unsigned long)esp_random(), (unsigned long)esp_random(), (unsigned long)esp_random());
  bootId = boot;
  fsMutex = xSemaphoreCreateMutex();
  fsReady = LittleFS.begin(FORMAT_FILESYSTEM_ON_FAILURE);
  if (!fsReady) Serial.println("LittleFS sin preparar: ver README, inicializacion del almacenamiento");
  WiFi.mode(WIFI_STA);
  WiFi.begin(WIFI_SSID, WIFI_PASSWORD);
  configTime(0, 0, "pool.ntp.org", "time.nist.gov");
  xTaskCreate(networkWorker, "edge-http", 8192, nullptr, 1, nullptr);
  Serial.println("ZenRoom listo. Esperando hora UTC y DHT22 validos.");
}

void loop() {
  static uint32_t lastClimate = 0, lastPresence = 0, nextSample = 0, lastLog = 0;
  uint32_t now = millis();
  handleButtons();
  if (now - lastClimate >= 2200) {
    lastClimate = now;
    temperature = dht.readTemperature();
    humidity = dht.readHumidity();
    climateValid = isfinite(temperature) && isfinite(humidity)
                   && temperature >= -40 && temperature <= 80 && humidity >= 0 && humidity <= 100;
    if (!climateValid) Serial.println("DHT22 invalido: revisar GPIO23; se omite la ventana");
  }
  if (now - lastPresence >= 250) { lastPresence = now; readPresence(); }
  if (int32_t(now - nextSample) >= 0) {
    if (now - nextSample > 250) { resetBatch(); nextSample = now; }
    nextSample += 125;
    noiseLevel = readNoiseLevel();
    updateIndicators(noiseLevel);
    sampleNoise(noiseLevel);
  }
  if (now - lastLog >= 2000) {
    lastLog = now;
    Serial.printf("T=%.1f RH=%.1f ruido=%.1f ADC=%u presencia=%d distancia=%.1f cm hora=%lld\n",
                  temperature, humidity, noiseLevel, potAdc,
                  occupied, distanceCm, (long long)time(nullptr));
  }
  delay(2);
}
