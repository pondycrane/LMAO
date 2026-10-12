#pragma once
// ESP-IDF-only WiFi helper for the RoboDog native client.
bool wifi_connect(const char* ssid, const char* pass, int timeout_s);
