/*
  Access Control - ESP32-CAM + PN532

  ESP32 ramane controllerul de la usa:
    - PN532 si cardurile autorizate (NVS)
    - releu, buton si camera
    - API HTTP local pentru Raspberry Pi 5

  Raspberry Pi se ocupa de Telegram si cere fotografiile prin /api/photo.
*/

#include <WiFi.h>
#include <HTTPClient.h>
#include <WebServer.h>
#include <Wire.h>
#include <Adafruit_PN532.h>
#include <Preferences.h>
#include "esp_camera.h"
#include "secrets.h"

// ---------- CONFIG ----------
// Adresa serviciului din Portainer. Foloseste IP-ul fix al RPi.
const char* RPI_EVENTS_URL = RPI_EVENTS_ENDPOINT;
const char* API_KEY = ACCESS_API_KEY;

// ---------- Pinout ----------
#define SDA_PIN 14
#define SCL_PIN 15
#define RELAY_PIN 12
#define LED_PIN 4
#define BUTTON_PIN 13

// ---------- Camera AI Thinker ESP32-CAM ----------
#define PWDN_GPIO_NUM 32
#define RESET_GPIO_NUM -1
#define XCLK_GPIO_NUM 0
#define SIOD_GPIO_NUM 26
#define SIOC_GPIO_NUM 27
#define Y9_GPIO_NUM 35
#define Y8_GPIO_NUM 34
#define Y7_GPIO_NUM 39
#define Y6_GPIO_NUM 36
#define Y5_GPIO_NUM 21
#define Y4_GPIO_NUM 19
#define Y3_GPIO_NUM 18
#define Y2_GPIO_NUM 5
#define VSYNC_GPIO_NUM 25
#define HREF_GPIO_NUM 23
#define PCLK_GPIO_NUM 22

Adafruit_PN532 nfc(SDA_PIN, SCL_PIN);
Preferences prefs;
WebServer server(80);

enum Mode { MODE_NORMAL, MODE_ENROLL };
Mode mode = MODE_NORMAL;
unsigned long enrollStartTime = 0;
const unsigned long ENROLL_WINDOW_MS = 20000;
unsigned long lastNfcCheck = 0;
const unsigned long NFC_CHECK_INTERVAL = 300;
unsigned long lastButtonPress = 0;
const unsigned long BUTTON_COOLDOWN_MS = 8000;
bool buttonWasPressed = false;
unsigned long lastWifiReconnect = 0;
const unsigned long WIFI_RECONNECT_INTERVAL_MS = 10000;

void blinkLed(int times, int onMs, int offMs = 200) {
  for (int i = 0; i < times; i++) {
    digitalWrite(LED_PIN, HIGH);
    delay(onMs);
    digitalWrite(LED_PIN, LOW);
    if (i < times - 1) delay(offMs);
  }
}

String uidToString(uint8_t* uid, uint8_t len) {
  String result;
  for (uint8_t i = 0; i < len; i++) {
    if (uid[i] < 0x10) result += "0";
    result += String(uid[i], HEX);
    if (i < len - 1) result += ":";
  }
  result.toUpperCase();
  return result;
}

bool isAuthorized(const String& uid) {
  int count = prefs.getInt("count", 0);
  for (int i = 0; i < count; i++) {
    if (prefs.getString(("uid" + String(i)).c_str(), "") == uid) return true;
  }
  return false;
}

bool saveNewUid(const String& uid) {
  if (isAuthorized(uid)) return false;
  int count = prefs.getInt("count", 0);
  prefs.putString(("uid" + String(count)).c_str(), uid);
  prefs.putInt("count", count + 1);
  return true;
}

bool initCamera() {
  camera_config_t config;
  config.ledc_channel = LEDC_CHANNEL_0;
  config.ledc_timer = LEDC_TIMER_0;
  config.pin_d0 = Y2_GPIO_NUM;
  config.pin_d1 = Y3_GPIO_NUM;
  config.pin_d2 = Y4_GPIO_NUM;
  config.pin_d3 = Y5_GPIO_NUM;
  config.pin_d4 = Y6_GPIO_NUM;
  config.pin_d5 = Y7_GPIO_NUM;
  config.pin_d6 = Y8_GPIO_NUM;
  config.pin_d7 = Y9_GPIO_NUM;
  config.pin_xclk = XCLK_GPIO_NUM;
  config.pin_pclk = PCLK_GPIO_NUM;
  config.pin_vsync = VSYNC_GPIO_NUM;
  config.pin_href = HREF_GPIO_NUM;
  config.pin_sccb_sda = SIOD_GPIO_NUM;
  config.pin_sccb_scl = SIOC_GPIO_NUM;
  config.pin_pwdn = PWDN_GPIO_NUM;
  config.pin_reset = RESET_GPIO_NUM;
  config.xclk_freq_hz = 20000000;
  config.pixel_format = PIXFORMAT_JPEG;
  // VGA pastreaza suficienta claritate pentru identificare si reduce
  // semnificativ timpul de transfer catre Raspberry Pi si Telegram.
  config.frame_size = FRAMESIZE_VGA;
  config.jpeg_quality = 15;
  config.fb_count = 1;
  return esp_camera_init(&config) == ESP_OK;
}

void sendEvent(const String& type, const String& uid, const String& status,
               const String& message) {
  if (WiFi.status() != WL_CONNECTED) return;

  HTTPClient http;
  http.begin(RPI_EVENTS_URL);
  http.addHeader("Content-Type", "application/json");
  http.addHeader("X-Access-Key", API_KEY);
  String body = "{\"type\":\"" + type + "\",\"uid\":\"" + uid +
                "\",\"status\":\"" + status + "\",\"message\":\"" +
                message + "\",\"esp_url\":\"http://" +
                WiFi.localIP().toString() + "\"}";
  int code = http.POST(body);
  if (code <= 0) {
    Serial.printf("Eroare HTTP RPi: %s (%d), endpoint=%s\n",
                  http.errorToString(code).c_str(), code, RPI_EVENTS_URL);
  } else {
    Serial.printf("Eveniment RPi HTTP: %d\n", code);
  }
  http.end();
}

void handlePhoto() {
  if (server.header("X-Access-Key") != API_KEY) {
    server.send(401, "text/plain", "Unauthorized");
    return;
  }
  camera_fb_t* fb = esp_camera_fb_get();
  if (!fb) {
    server.send(503, "text/plain", "Camera indisponibila");
    return;
  }
  server.sendHeader("Cache-Control", "no-store");
  server.setContentLength(fb->len);
  server.send(200, "image/jpeg");
  server.client().write(fb->buf, fb->len);
  esp_camera_fb_return(fb);
}

void handleCommand() {
  if (server.header("X-Access-Key") != API_KEY) {
    server.send(401, "text/plain", "Unauthorized");
    return;
  }
  String command = server.arg("plain");
  Serial.print("Comanda RPi primita: ");
  Serial.println(command);
  if (command.indexOf("enroll") >= 0) {
    mode = MODE_ENROLL;
    enrollStartTime = millis();
    blinkLed(1, 300);
    server.send(200, "application/json", "{\"ok\":true,\"command\":\"enroll\"}");
    return;
  }
  if (command.indexOf("open_door") >= 0) {
    digitalWrite(RELAY_PIN, HIGH);
    delay(3000);
    digitalWrite(RELAY_PIN, LOW);
    server.send(200, "application/json", "{\"ok\":true,\"command\":\"open_door\"}");
    return;
  }
  server.send(400, "application/json", "{\"ok\":false,\"error\":\"Comanda necunoscuta\"}");
}

void handleStatus() {
  if (server.header("X-Access-Key") != API_KEY) {
    server.send(401, "text/plain", "Unauthorized");
    return;
  }
  String response = "{\"wifi\":true,\"ip\":\"" + WiFi.localIP().toString() +
                    "\",\"enroll\":" + (mode == MODE_ENROLL ? "true" : "false") + "}";
  server.send(200, "application/json", response);
}

void setup() {
  Serial.begin(115200);
  pinMode(RELAY_PIN, OUTPUT);
  digitalWrite(RELAY_PIN, LOW);
  pinMode(LED_PIN, OUTPUT);
  digitalWrite(LED_PIN, LOW);
  pinMode(BUTTON_PIN, INPUT_PULLUP);

  prefs.begin("access", false);
  Wire.begin(SDA_PIN, SCL_PIN);
  nfc.begin();
  if (!nfc.getFirmwareVersion()) Serial.println("PN532 nu raspunde.");
  else nfc.SAMConfig();

  if (!initCamera()) Serial.println("Eroare initializare camera.");

  WiFi.mode(WIFI_STA);
  WiFi.begin(WIFI_SSID, WIFI_PASSWORD);
  Serial.print("Conectare WiFi");
  while (WiFi.status() != WL_CONNECTED) {
    delay(500);
    Serial.print(".");
  }
  Serial.println();
  Serial.print("ESP32 IP: ");
  Serial.println(WiFi.localIP());

  const char* headers[] = {"X-Access-Key"};
  server.collectHeaders(headers, 1);
  server.on("/api/photo", HTTP_GET, handlePhoto);
  server.on("/api/command", HTTP_POST, handleCommand);
  server.on("/api/status", HTTP_GET, handleStatus);
  server.begin();
  Serial.println("API HTTP pornit. Telegram ruleaza pe Raspberry Pi.");
}

void loop() {
  server.handleClient();
  unsigned long now = millis();

  if (WiFi.status() != WL_CONNECTED &&
      now - lastWifiReconnect >= WIFI_RECONNECT_INTERVAL_MS) {
    lastWifiReconnect = now;
    Serial.println("Wi-Fi deconectat; incerc reconectarea.");
    WiFi.disconnect();
    WiFi.begin(WIFI_SSID, WIFI_PASSWORD);
  }

  if (mode == MODE_ENROLL && now - enrollStartTime > ENROLL_WINDOW_MS) {
    mode = MODE_NORMAL;
    blinkLed(1, 600);
    sendEvent("enroll_timeout", "", "timeout", "Inregistrarea a expirat.");
  }

  bool buttonPressed = digitalRead(BUTTON_PIN) == LOW;
  if (buttonPressed && !buttonWasPressed &&
      now - lastButtonPress > BUTTON_COOLDOWN_MS) {
    lastButtonPress = now;
    Serial.println("Buton apel detectat.");
    sendEvent("button", "", "request", "Cineva a apasat butonul de apel.");
  }
  buttonWasPressed = buttonPressed;

  if (now - lastNfcCheck < NFC_CHECK_INTERVAL) return;
  lastNfcCheck = now;

  uint8_t uid[7];
  uint8_t uidLength;
  if (!nfc.readPassiveTargetID(PN532_MIFARE_ISO14443A, uid, &uidLength, 150)) return;

  String uidStr = uidToString(uid, uidLength);
  Serial.print("Card NFC detectat: ");
  Serial.println(uidStr);
  if (mode == MODE_ENROLL) {
    bool saved = saveNewUid(uidStr);
    mode = MODE_NORMAL;
    blinkLed(2, 150, 150);
    sendEvent("enroll", uidStr, saved ? "saved" : "exists",
              saved ? "Card salvat cu succes." : "Cardul exista deja.");
  } else {
    bool authorized = isAuthorized(uidStr);
    sendEvent("card", uidStr, authorized ? "authorized" : "unknown",
              authorized ? "Card autorizat." : "Card necunoscut.");
    if (authorized) {
      digitalWrite(RELAY_PIN, HIGH);
      delay(3000);
      digitalWrite(RELAY_PIN, LOW);
    }
  }
  delay(1000);
}
