#pragma once
// ESP-IDF WiFi STA helper for the RoboDog home client.
bool wifi_connect(const char* ssid, const char* pass, int timeout_s);
