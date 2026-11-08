#pragma once

// Editar antes de cargar. La API key del Cloud pertenece al Edge.
constexpr char WIFI_SSID[] = "FERNANNDO-2.4G";
constexpr char WIFI_PASSWORD[] = "BYS872701";
constexpr char EDGE_READINGS_URL[] = "http://192.168.18.5:5000/api/v1/readings";
constexpr char DEVICE_ID[] = "esp32-sala-01";
constexpr char ROOM_ID[] = "sala-01";
constexpr char FW_VERSION[] = "0.3.0";

constexpr int DHT_PIN = 23;
constexpr int PIR_PIN = 27;
constexpr int TRIG_PIN = 22;
constexpr int ECHO_PIN = 35;
constexpr int POT_PIN = 34;
constexpr int BUZZER_PIN = 26;
constexpr int LED_GREEN_PIN = 21;
constexpr int LED_AMBER_PIN = 19;
constexpr int LED_RED_PIN = 18;
constexpr int BUTTON_PINS[] = {13, 25, 32};

constexpr bool BUZZER_ACTIVE_LOW = true;
constexpr bool ENABLE_AUTOMATIC_BUZZER = true;
// Solo para el primer arranque con una particion dedicada y sin datos utiles.
constexpr bool FORMAT_FILESYSTEM_ON_FAILURE = false;
constexpr unsigned MAX_QUEUED_BATCHES = 120;  // 20 minutos; limite visible por Serial
constexpr float PRESENCE_DISTANCE_CM = 150.0f;
constexpr unsigned PRESENCE_HOLD_MS = 30000;
constexpr unsigned MANUAL_PRESENCE_MS = 60000;
constexpr unsigned MUTE_MS = 60000;
