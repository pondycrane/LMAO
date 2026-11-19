#pragma once

// Shared ESP-IDF WiFi STA helper for the LMAO LAN clients (RoboDog home client
// and the humidifier Atom Socket).  One canonical copy — both board firmwares
// stage this component, so the reconnect/SNTP behaviour cannot drift.
//
// NVS must be initialised by app_main before calling (esp_wifi requires it).
namespace lma_wifi {

    // Connect to *ssid* as a station and wait up to *timeout_s* for an IP.
    // Keeps reconnecting for the life of the process (a dropped AP must never
    // strand a controller), and attempts an SNTP sync after DHCP (non-fatal).
    // Returns true when an IP was obtained within the timeout.
    bool connect(const char* ssid, const char* pass, int timeout_s);

    // True once SNTP produced a plausible Unix epoch (> 2023-11-14).
    bool time_synced();

}
