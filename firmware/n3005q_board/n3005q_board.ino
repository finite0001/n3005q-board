// N3005Q flight board -- M5Stack M5Paper Color (PaperColor), ESP32-S3.
//
// Thin client by design. The server renders a 600x400 six-color PNG; this
// sketch's whole job is to fetch it, paint it, and get back to sleep.
//
// Why it is shaped this way:
//   * Panel refresh is 15-30s and is by far the biggest power draw, so the
//     device asks for the image often but only repaints when the ETag says
//     the bytes actually changed. Most wakes are a ~3s radio blip.
//   * On a failed fetch the panel is left alone. E-ink holds the last image
//     with zero power, so a stale-but-correct card beats a blank screen.
//     Only after several consecutive failures does it repaint to say so.
//   * Server auth uses the ESP32 core's bundled Mozilla root store rather
//     than a pinned CA, so a certificate rotation at the host does not brick
//     the device in the field.

#include <M5Unified.h>
#include <WiFi.h>
#include <WiFiClientSecure.h>
#include <HTTPClient.h>
#include <esp_sleep.h>
#include <time.h>

#include "config.h"

// Portrait: 400 wide x 600 tall. 0 and 2 are the two upright portrait
// orientations (180 degrees apart); override in config.h if it is upside down.
#ifndef DISPLAY_ROTATION
#define DISPLAY_ROTATION 0
#endif

// Mozilla root bundle shipped with the ESP32 Arduino core.
extern const uint8_t rootca_crt_bundle_start[] asm("_binary_x509_crt_bundle_start");

// Survives deep sleep; lost on hard reset, which is the behaviour we want.
RTC_DATA_ATTR static char  rtcEtag[96] = {0};
RTC_DATA_ATTR static uint32_t rtcFailCount = 0;
RTC_DATA_ATTR static uint32_t rtcWakeCount = 0;

// The server leaves this box black on purpose; we draw the battery into it.
// Must match BATTERY_SLOT in render/render.py.
static const int BAT_X = 146, BAT_Y = 8, BAT_W = 52, BAT_H = 24;

static M5Canvas canvas(&M5.Display);

// Declared before the first function on purpose: the Arduino preprocessor
// hoists generated prototypes to just above the first function definition,
// so a return type defined later in the file fails to compile.
struct FetchResult {
  bool ok = false;
  bool unchanged = false;   // server said 304
  uint8_t *data = nullptr;
  size_t len = 0;
  time_t lastModified = 0;
};

// ---------------------------------------------------------------- helpers
static void sleepFor(uint32_t minutes) {
  Serial.printf("sleeping %u min\n", minutes);
  Serial.flush();
  esp_sleep_enable_timer_wakeup((uint64_t)minutes * 60ULL * 1000000ULL);
  esp_deep_sleep_start();
}

// Minutes until the next scheduled wake, based on local time. Falls back to
// the day interval if the clock was never set.
static uint32_t nextWakeMinutes() {
  struct tm t;
  if (!getLocalTime(&t, 100)) return DAY_INTERVAL_MIN;
  bool day = (t.tm_hour >= DAY_START_HOUR && t.tm_hour < DAY_END_HOUR);
  uint32_t step = day ? DAY_INTERVAL_MIN : NIGHT_INTERVAL_MIN;
  // Align to the wall clock so wakes land on :00 / :30 rather than drifting.
  uint32_t past = (t.tm_min % step) * 60 + t.tm_sec;
  uint32_t secs = step * 60 - past;
  if (secs < 60) secs += step * 60;
  return secs / 60;
}

static bool connectWifi() {
  WiFi.mode(WIFI_STA);
  WiFi.begin(WIFI_SSID, WIFI_PASSWORD);
  uint32_t t0 = millis();
  while (WiFi.status() != WL_CONNECTED) {
    if (millis() - t0 > WIFI_TIMEOUT_MS) {
      Serial.println("wifi timeout");
      return false;
    }
    delay(200);
  }
  Serial.printf("wifi ok, rssi %d\n", WiFi.RSSI());
  configTzTime(TZ_STRING, "pool.ntp.org", "time.nist.gov");
  struct tm t;
  getLocalTime(&t, 5000);   // best effort; schedule degrades gracefully
  return true;
}

// newlib here has no timegm(), and mktime() would apply the local timezone to
// a value that is already UTC. Days-from-civil (Hinnant) is exact and has no
// dependencies.
static time_t tmToUtc(const struct tm *t) {
  int y = t->tm_year + 1900;
  unsigned m = (unsigned)t->tm_mon + 1;
  unsigned d = (unsigned)t->tm_mday;
  y -= (m <= 2);
  const int era = (y >= 0 ? y : y - 399) / 400;
  const unsigned yoe = (unsigned)(y - era * 400);
  const unsigned doy = (153u * (m + (m > 2 ? -3u : 9u)) + 2u) / 5u + d - 1u;
  const unsigned doe = yoe * 365u + yoe / 4u - yoe / 100u + doy;
  const long long days = (long long)era * 146097LL + (long long)doe - 719468LL;
  return (time_t)(days * 86400LL + t->tm_hour * 3600LL + t->tm_min * 60LL
                  + t->tm_sec);
}

// Parse an HTTP date (always GMT) into epoch seconds. 0 if unreadable.
static time_t parseHttpDate(const String &s) {
  if (s.length() < 20) return 0;
  struct tm t = {};
  if (!strptime(s.c_str(), "%a, %d %b %Y %H:%M:%S", &t)) return 0;
  return tmToUtc(&t);
}

// ---------------------------------------------------------------- overlays
static void drawBattery() {
  int32_t pct = M5.Power.getBatteryLevel();
  if (pct < 0 || pct > 100) return;      // unknown: leave the slot black
  canvas.fillRect(BAT_X, BAT_Y, BAT_W, BAT_H, TFT_BLACK);
  canvas.setTextColor(pct <= 15 ? TFT_RED : TFT_WHITE, TFT_BLACK);
  canvas.setTextDatum(middle_center);
  canvas.setTextSize(1);
  canvas.drawString(String(pct) + "%", BAT_X + BAT_W / 2, BAT_Y + BAT_H / 2);
}

static void drawBanner(const char *msg, uint16_t bg) {
  const int w = canvas.width(), y = canvas.height() - 28;
  canvas.fillRect(0, y, w, 28, bg);
  canvas.drawRect(0, y, w, 28, TFT_BLACK);
  canvas.setTextColor(TFT_BLACK, bg);
  canvas.setTextDatum(middle_center);
  canvas.drawString(msg, w / 2, y + 14);
}

// ---------------------------------------------------------------- fetch
static FetchResult fetchCard() {
  FetchResult r;

  WiFiClientSecure *client = new WiFiClientSecure;
  if (!client) return r;
  client->setCACertBundle(rootca_crt_bundle_start);
  client->setTimeout(HTTP_TIMEOUT_MS / 1000);

  HTTPClient http;
  http.setTimeout(HTTP_TIMEOUT_MS);
  http.setUserAgent("n3005q-board/1.0 (M5PaperColor)");
  if (!http.begin(*client, CARD_URL)) {
    Serial.println("http.begin failed");
    delete client;
    return r;
  }
  if (rtcEtag[0]) http.addHeader("If-None-Match", rtcEtag);
  const char *collect[] = {"ETag", "Last-Modified"};
  http.collectHeaders(collect, 2);

  int code = http.GET();
  Serial.printf("HTTP %d\n", code);

  if (code == HTTP_CODE_NOT_MODIFIED) {
    r.ok = true;
    r.unchanged = true;
    http.end();
    delete client;
    return r;
  }
  if (code != HTTP_CODE_OK) {
    http.end();
    delete client;
    return r;
  }

  int len = http.getSize();
  size_t cap = (len > 0 && len < MAX_PNG_BYTES) ? (size_t)len : MAX_PNG_BYTES;
  uint8_t *buf = (uint8_t *)ps_malloc(cap);
  if (!buf) {
    Serial.println("ps_malloc failed");
    http.end();
    delete client;
    return r;
  }

  WiFiClient *stream = http.getStreamPtr();
  size_t got = 0;
  uint32_t t0 = millis();
  while (got < cap && (len < 0 || got < (size_t)len)) {
    size_t avail = stream->available();
    if (avail) {
      got += stream->readBytes(buf + got, min(avail, cap - got));
      t0 = millis();
    } else if (!http.connected() || millis() - t0 > 5000) {
      break;
    } else {
      delay(2);
    }
  }
  Serial.printf("read %u bytes\n", (unsigned)got);

  String etag = http.header("ETag");
  if (etag.length() && etag.length() < sizeof(rtcEtag)) {
    strncpy(rtcEtag, etag.c_str(), sizeof(rtcEtag) - 1);
    rtcEtag[sizeof(rtcEtag) - 1] = 0;
  }
  r.lastModified = parseHttpDate(http.header("Last-Modified"));

  http.end();
  delete client;

  if (got < 64) { free(buf); return r; }   // too small to be a real PNG
  r.ok = true;
  r.data = buf;
  r.len = got;
  return r;
}

// ---------------------------------------------------------------- main
void setup() {
  Serial.begin(115200);
#ifdef BOOT_TRACE
  delay(6000);                   // let the host reattach to USB after reset
  Serial.println("TRACE serial up");
#endif
  auto cfg = M5.config();
  cfg.clear_display = false;     // never flash the panel white on boot
  M5.begin(cfg);
#ifdef BOOT_TRACE
  Serial.printf("TRACE M5.begin done, board %d, display %dx%d rot %d\n",
                (int)M5.getBoard(), M5.Display.width(), M5.Display.height(),
                M5.Display.getRotation());
#endif
  M5.Display.setRotation(DISPLAY_ROTATION);
#ifdef BOOT_TRACE
  Serial.println("TRACE setRotation done");
#endif
  rtcWakeCount++;
  Serial.printf("\n=== wake %u (fails %u) ===\n", rtcWakeCount, rtcFailCount);
  Serial.printf("display %dx%d rotation %d\n", M5.Display.width(),
                M5.Display.height(), M5.Display.getRotation());

  if (!connectWifi()) {
    rtcFailCount++;
    if (rtcFailCount >= OFFLINE_AFTER_FAILS) {
      canvas.setPsram(true);
      canvas.createSprite(M5.Display.width(), M5.Display.height());
      canvas.fillSprite(TFT_WHITE);
      canvas.setTextColor(TFT_BLACK, TFT_WHITE);
      canvas.setTextDatum(middle_center);
      canvas.drawString("NO WIFI", canvas.width() / 2, canvas.height() / 2 - 15);
      canvas.drawString("card may be out of date", canvas.width() / 2,
                        canvas.height() / 2 + 15);
      M5.Display.setEpdMode(epd_mode_t::epd_quality);
      canvas.pushSprite(0, 0);
      rtcFailCount = 0;          // do not repaint this every wake
    }
    WiFi.disconnect(true);
    sleepFor(5);                 // short retry, not the full interval
  }

  FetchResult res = fetchCard();

  if (res.ok && res.unchanged) {
    Serial.println("304, panel already correct");
    rtcFailCount = 0;
    WiFi.disconnect(true);
    sleepFor(nextWakeMinutes());
  }

  if (!res.ok) {
    rtcFailCount++;
    Serial.println("fetch failed, leaving panel as-is");
    WiFi.disconnect(true);
    sleepFor(5);
  }

  // Paint. 600x400 sprite lives in PSRAM; the panel is the slow part.
  canvas.setPsram(true);
  if (!canvas.createSprite(M5.Display.width(), M5.Display.height())) {
    Serial.println("createSprite failed");
    free(res.data);
    sleepFor(nextWakeMinutes());
  }
  canvas.fillSprite(TFT_WHITE);
  canvas.drawPng(res.data, res.len, 0, 0);
  free(res.data);

  drawBattery();

  if (res.lastModified) {
    time_t now = time(nullptr);
    long age = (long)(now - res.lastModified) / 60;
    Serial.printf("image age %ld min\n", age);
    if (age > STALE_AFTER_MIN) {
      drawBanner("DATA STALE -- renderer not updating", TFT_YELLOW);
    }
  }

  M5.Display.setEpdMode(epd_mode_t::epd_quality);
  uint32_t t0 = millis();
  canvas.pushSprite(0, 0);
  Serial.printf("panel refresh took %lu ms\n", millis() - t0);

  rtcFailCount = 0;
  WiFi.disconnect(true);
  sleepFor(nextWakeMinutes());
}

void loop() {
  // Never reached: setup() always ends in deep sleep.
}
