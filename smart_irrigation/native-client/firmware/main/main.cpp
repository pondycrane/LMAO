// Sprout native client (ESP32-PICO-D4, ESP-IDF).
//
// Step 3 (issue #130): boot RNS + UART-AT (RAK3172 DTU P2P) interface on the
// LMAO mesh, register "lmao/sprout", announce every 30s.
// Step 4/5: every SEND_INTERVAL_MS (5 min) read the ENV III air temp +
// humidity (SHT30, G21/G25) AND the soil-moisture probe (ADC1_CH4/GPIO32),
// build ONE LMAOEnvelope SensorReport bundling all three readings
// (sensor_id 3=air temp °C, 2=humidity %, 4=soil moisture %), wrap it in a
// send-only LXMF message to the server's lxmf/delivery destination
// (DEST_HASH), and transmit it on the DTU link. Also learns the server identity
// from its announce (single-hop RNode path).
// Step 6 (2026-09-18): the irrigation control engine (control.h/.cpp) runs on a
// 1 s tick — plant-profile hysteresis on the soil probe with pulse dosing and
// the hard safety overrides evaluated last.  Pump actuation is gated by
// PUMP_ACTUATION_ENABLED (pump.h): 0 until the #119 hardware pull-down is
// fitted and verified, so the node runs DRY RUN and reports no sensor_id 6/7.
#include <cstdio>
#include <memory>
#include <string>

#include "esp_log.h"
#include "esp_timer.h"
#include "nvs_flash.h"
#include "nvs.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "freertos/semphr.h"

#include "rtreticulum/identity.h"
#include "rtreticulum/destination.h"
#include "rtreticulum/transport.h"
#include "rtreticulum/reticulum.h"
#include "uart_at_interface.h"
#include "path_find.h"
#include "sht30.h"
#include "moisture.h"
#include "control.h"
#include "pump.h"
#include "lma_identity.h"
#include "lma_encoder.h"
#include "lxmf_send.h"

using namespace RNS;
static const char* TAG = "sprout";

// Server LXMF delivery destination hash (issue #127 / install_all DEST_HASH).
static const char* DEST_HASH_HEX = "dad35b80164b25f7b1474be86e443702";

// Sensor bundle cadence.  5 min per the Phase 0 algorithm evaluation: the
// ML dataset batches 6 x 5-min samples; 60 s pushes just added LoRa/DuckDB
// load with no control benefit.  The first report is sent immediately after
// boot, and a session end pushes an immediate report so the watering event is
// timestamped (sessions are seconds long and would otherwise fall between two
// 5-min samples).
#define SEND_INTERVAL_MS 300000UL

// Control-engine tick.  The engine is a non-blocking state machine: it needs
// sub-second resolution only inside a pulse train (5 s pulses, 90 s soaks), so
// a 1 s tick is ample and costs one ADC read.
#define TICK_MS 1000UL

// Asset announces every 30 s (unchanged from #130).
#define ANNOUNCE_INTERVAL_MS 30000UL

// Control state is flushed to NVS on every pump edge plus at most this often
// (flash wear): the daily totaliser is exact at pulse boundaries, which is the
// only time it changes.
#define PERSIST_INTERVAL_MS 600000UL

// A cached air-RH sample older than this is dropped, so a missing ENV III
// (#124) disables only the RH lockout clause instead of blocking watering.
#define RH_STALE_MS 600000UL

// hexstr is provided by the shared lma_identity module (firmware_common) —
// use the same one the Cardputer client uses (DRY, see firmware_common/).
using lma_identity::hexstr;

static SemaphoreHandle_t s_ident_lock = nullptr;
static Identity s_server_identity;                 // learned from the server announce
static bool s_have_server = false;

static void on_announce_cb(const Bytes& dh, const Identity& peer, const Bytes&) {
    ESP_LOGI(TAG, "<< announce dest=%s id=%s", hexstr(dh).c_str(),
             hexstr(peer.get_salt()).c_str());
    if (hexstr(dh) == std::string(DEST_HASH_HEX)) {
        if (s_ident_lock) xSemaphoreTake(s_ident_lock, portMAX_DELAY);
        s_server_identity = peer;
        s_have_server = true;
        if (s_ident_lock) xSemaphoreGive(s_ident_lock);
        ESP_LOGI(TAG, "server delivery identity learned");
    }
}

static void send_sensor_report(const Identity& my_identity, const Sht30Reading& air,
                               bool air_ok, bool with_pump, uint32_t pump_interval_s,
                               bool pump_active, float moisture_pct, bool moisture_valid,
                               const sprout::PlantProfile& profile) {
    uint64_t now_ms = (uint64_t)(esp_timer_get_time() / 1000);
    uint64_t unix_s = 946684800ULL + (uint64_t)(esp_timer_get_time() / 1000000ULL); // ~2000-epoch offset
    uint32_t seq = (uint32_t)(now_ms % 100000U);

    // Bundle every reading we can take into ONE SensorReport.  A Port A
    // SHT30 contact failure (#124) no longer drops the whole report — the
    // soil-moisture reading (the control input) still gets through.
    std::vector<std::string> readings;

    if (air_ok) {
        readings.push_back(lma_encoder::encode_reading(3, air.temp_c, "C", now_ms));
        readings.push_back(lma_encoder::encode_reading(2, air.hum_pct, "%", now_ms));
    } else {
        ESP_LOGW(TAG, "SHT30 read failed (Port A contact? #124) — bundle continues without air T/H");
    }

    if (moisture_valid) {
        readings.push_back(lma_encoder::encode_reading(4, moisture_pct, "%", now_ms));
        ESP_LOGI(TAG, "moisture=%.1f%%", moisture_pct);
    }

    // sensor_id 10/11 — the active plant-profile band.  Carrying it with the
    // data keeps the display chart's dry/wet lines sourced from the node's real
    // config instead of a copy that can drift, and records the thresholds each
    // sample was judged against in the training stream.
    readings.push_back(lma_encoder::encode_reading(
        10, (float)profile.dry_q8() / 256.0f, "%", now_ms));
    readings.push_back(lma_encoder::encode_reading(
        11, (float)profile.wet_q8() / 256.0f, "%", now_ms));

    // sensor_id 6/7 are the ML watering-event tags and must describe what the
    // pump physically did — in dry run (actuation disabled) they are omitted
    // rather than filled with the engine's intention.
    if (with_pump) {
        readings.push_back(lma_encoder::encode_reading(6, (float)pump_interval_s, "s", now_ms));
        readings.push_back(lma_encoder::encode_reading(7, pump_active ? 1.0f : 0.0f, "bool", now_ms));
    }

    if (readings.empty()) {
        ESP_LOGW(TAG, "no sensor readings available — skipping SensorReport");
        return;
    }

    std::string sreport = lma_encoder::encode_sensor_report(
        hexstr(my_identity.get_salt()), seq, 0.0f, readings);
    std::string envelope = lma_encoder::encode_envelope(sreport);
    ESP_LOGI(TAG, "SensorReport env=%uB readings=%u (temp/hum/moisture/band)",
             (unsigned)envelope.size(), (unsigned)readings.size());

    Destination my_delivery(my_identity, Type::Destination::OUT, Type::Destination::SINGLE,
                            "lxmf", "delivery");
    if (s_ident_lock) xSemaphoreTake(s_ident_lock, portMAX_DELAY);
    bool have = s_have_server;
    Identity srv = s_server_identity;
    if (s_ident_lock) xSemaphoreGive(s_ident_lock);
    if (!have) {
        // TODO(#130): native RNS path-request hits an RTReticulum PLAIN-dest
        // abort (upstream). For now we discover the server via its periodic
        // announce (server announces every ~60s).
        ESP_LOGW(TAG, "server identity not learned yet — skipping LXMF send (awaiting server announce)");
        return;
    }
    Destination server_delivery(srv, Type::Destination::OUT, Type::Destination::SINGLE,
                                "lxmf", "delivery");
    ESP_LOGI(TAG, "server delivery hash=%s", hexstr(server_delivery.hash()).c_str());

    Bytes body = lxmf_send::build_body(my_delivery, server_delivery, my_identity,
                                       Bytes(envelope), Bytes("p:Envelope"), unix_s);
    Bytes frame = lxmf_send::opportunistic_frame(server_delivery, body);
    if (frame.empty()) { ESP_LOGW(TAG, "no opportunistic frame"); return; }
    ESP_LOGI(TAG, "TX LXMF frame %uB (temp/hum/moisture bundle) -> server",
             (unsigned)frame.size());
    Transport::broadcast(frame, nullptr);
}

extern "C" void app_main(void);

void app_main() {
    ESP_LOGI(TAG, "Sprout native client starting");

    // SAFETY (#119): drive the watering-unit pump enable LOW (OFF) as the very
    // FIRST action — never leave the pump control line floating.
    if (!pump_init()) {
        ESP_LOGE(TAG, "pump init failed — keep the pump disconnected");
    }

    esp_err_t nv = nvs_flash_init();
    if (nv == ESP_ERR_NVS_NO_FREE_PAGES || nv == ESP_ERR_NVS_NEW_VERSION_FOUND) {
        nvs_flash_erase();
        nvs_flash_init();
    }

    s_ident_lock = xSemaphoreCreateMutex();

    auto dt = std::make_shared<UartAtInterface>();
    Transport::register_interface(dt);
    bool dt_up = dt->start();   // configures the RAK3172 DTU P2P radio + PRECV on (Reticulum only calls loop())
    if (dt_up) {
        ESP_LOGI(TAG, "DTU radio interface started");
    } else {
        ESP_LOGW(TAG, "DTU radio interface failed to start");
    }

    // Persistent identity (NVS): keep the same RNS identity across boots so the
    // node is a stable mesh peer and can be whitelisted on the server (#130).
    // Shared with the Cardputer client (firmware_common/lma_identity).
    Identity identity = lma_identity::load_or_create("sprout");
    ESP_LOGI(TAG, "identity hash: %s", hexstr(identity.get_salt()).c_str());
    {
        // The server whitelists the sender's lxmf/delivery hash (what it logs
        // as "From:") — print ours now so it can be added to the server list.
        ESP_LOGI(TAG, "my lxmf/delivery hash: %s",
                 lma_identity::delivery_hash(identity).c_str());
    }

    Destination dest(identity, Type::Destination::IN, Type::Destination::SINGLE,
                     "lmao", "sprout");
    Transport::register_destination(dest);
    ESP_LOGI(TAG, "dest hash: %s", hexstr(dest.hash()).c_str());

    // LXMF delivery destination (IN) — announced so the server can recall the
    // Sprout identity keyed by THIS delivery hash (f5f05952…). Without it the
    // server's LXMF get_source() resolves <unknown> and the allow-list gate
    // drops our SensorReport (the Cardputer works because its delivery hash is
    // announced; ours was only ever heard under lmao/sprout).
    Destination delivery(identity, Type::Destination::IN, Type::Destination::SINGLE,
                         "lxmf", "delivery");
    Transport::register_destination(delivery);
    ESP_LOGI(TAG, "delivery dest hash: %s", hexstr(delivery.hash()).c_str());

    Transport::on_announce(on_announce_cb);

    if (!Reticulum::start(100, 8192, 5)) {
        ESP_LOGE(TAG, "Reticulum::start failed");
        return;
    }

    // Irrigation control engine (docs/algorithm-evaluation.md §1.3 + the §1.4
    // amendment).  Persisted: the daily totaliser plus any pending lockout /
    // session gap; the profile is stored by NAME so reordering the profile
    // table cannot mis-configure a deployed plant.
    sprout::Control control;
    {
        sprout::ControlState st{};
        size_t len = sizeof(st);
        char prof[16] = {0};
        size_t plen = sizeof(prof);
        bool have_state = false;
        nvs_handle_t nv = 0;
        if (nvs_open("sprout", NVS_READONLY, &nv) == ESP_OK) {
            have_state = (nvs_get_blob(nv, "ctl_state", &st, &len) == ESP_OK &&
                          len == sizeof(st));
            nvs_get_str(nv, "profile", prof, &plen);  // absent => default profile
            nvs_close(nv);
        }
        control.begin(have_state ? &st : nullptr,
                      (uint32_t)(esp_timer_get_time() / 1000));
        const int idx = sprout::profile_index_by_name(prof);
        control.set_profile_index(idx >= 0 ? idx : sprout::default_profile_index());
        ESP_LOGI(TAG, "control profile '%s': target %d%% +/- %d%% (dry %d%% / wet %d%%)",
                 control.profile().name, control.profile().target_pct,
                 control.profile().hyst_pct,
                 control.profile().target_pct - control.profile().hyst_pct,
                 control.profile().target_pct + control.profile().hyst_pct);
        ESP_LOGI(TAG, "dosing: pulse %ums soak %ums max %u daily %us (%s, actuation %s)",
                 (unsigned)control.profile().pulse_on_ms,
                 (unsigned)control.profile().soak_ms,
                 (unsigned)control.profile().max_pulses,
                 (unsigned)(control.profile().max_daily_ms / 1000),
                 have_state ? "restored" : "fresh",
                 PUMP_ACTUATION_ENABLED ? "ENABLED" : "dry run");
    }

    uint64_t last_send_ms = 0;
    uint32_t last_announce_ms = 0;
    uint32_t last_persist_ms = 0;
    sprout::q8 rh_q8_cached = sprout::q8_from_pct(50);
    bool rh_ok_cached = false;
    uint32_t rh_ms = 0;
    bool probe_implausible_logged = false;

    for (;;) {
        const uint32_t now_ms = (uint32_t)(esp_timer_get_time() / 1000);

        if (last_announce_ms == 0 || now_ms - last_announce_ms >= ANNOUNCE_INTERVAL_MS) {
            last_announce_ms = now_ms;
            dest.announce(Bytes(), true);
            delivery.announce(Bytes(), true);
            ESP_LOGI(TAG, "announced");

            // On-demand path discovery (issue #135): if the server identity is
            // not learned yet, ask the mesh for a path to the server's
            // lxmf/delivery destination instead of waiting for the server's
            // periodic announce.  path_find::request() is rate-limited (~20 s).
            if (s_ident_lock) xSemaphoreTake(s_ident_lock, portMAX_DELAY);
            const bool have_srv = s_have_server;
            if (s_ident_lock) xSemaphoreGive(s_ident_lock);
            if (!have_srv) path_find::request(DEST_HASH_HEX);
        }

        // ── Control engine tick (non-blocking; the pulse train needs only 1 s
        //    resolution, the decision itself runs on the moisture band) ──
        int32_t moisture_q8 = 0;
        const bool probe_ok = moisture_read_q8(&moisture_q8);
        sprout::Inputs cin;
        cin.now_ms = now_ms;
        cin.moisture_q8 = moisture_q8;
        cin.moisture_ok = probe_ok;
        cin.rh_q8 = rh_q8_cached;
        cin.rh_ok = rh_ok_cached && (now_ms - rh_ms < RH_STALE_MS);
        const sprout::Outputs eng = control.tick(cin);

        if (!eng.pump_on) {
            pump_set(false);
        } else if (PUMP_ACTUATION_ENABLED) {
            pump_set(true);
        } else if (eng.pump_changed) {
            ESP_LOGW(TAG, "DRY RUN: would energise pump (profile=%s pulse %u/%u state=%s)",
                     eng.profile_name, (unsigned)eng.pulses_done,
                     (unsigned)control.profile().max_pulses,
                     sprout::state_name(eng.state));
        }

        if (eng.session_ended) {
            ESP_LOGI(TAG, "session ended: profile=%s pulses=%u pump=%ums elapsed=%ums",
                     eng.profile_name, (unsigned)eng.pulses_done,
                     (unsigned)eng.session_pump_ms, (unsigned)eng.session_elapsed_ms);
        }

        // Probe plausibility (observed failure mode: an unseated probe reads the
        // 0 % air anchor, which is indistinguishable from bone-dry soil).
        if (eng.probe_implausible && !probe_implausible_logged) {
            probe_implausible_logged = true;
            ESP_LOGW(TAG, "moisture %.1f%% is at/below the air anchor — probe not in "
                          "soil? watering suspended until a plausible reading",
                     (double)eng.moisture_q8 / 256.0);
        } else if (!eng.probe_implausible && probe_implausible_logged) {
            probe_implausible_logged = false;
            ESP_LOGI(TAG, "moisture reading plausible again — watering resumed");
        }

        // Persist on every pump edge (the daily totaliser only changes there)
        // and otherwise at most every PERSIST_INTERVAL_MS.  Dry run is skipped
        // entirely: those doses never happened, so they must not become
        // persisted history that the daily cap counts after actuation is on.
        if (PUMP_ACTUATION_ENABLED && control.dirty() &&
            (eng.pump_changed || now_ms - last_persist_ms >= PERSIST_INTERVAL_MS)) {
            last_persist_ms = now_ms;
            nvs_handle_t nv = 0;
            if (nvs_open("sprout", NVS_READWRITE, &nv) == ESP_OK) {
                const sprout::ControlState st = control.state();
                nvs_set_blob(nv, "ctl_state", &st, sizeof(st));
                nvs_set_str(nv, "profile", control.profile().name);
                nvs_commit(nv);
                nvs_close(nv);
                control.clear_dirty();
            }
        }

        // ── Telemetry: 5-min cadence plus an immediate report when a session
        //    ends, so the watering event is timestamped between samples ──
        if (last_send_ms == 0 || now_ms - last_send_ms >= SEND_INTERVAL_MS ||
            eng.session_ended) {
            last_send_ms = now_ms;
            Sht30Reading air{};
            const bool air_ok = sht30_read(&air);
            if (air_ok) {
                rh_q8_cached = (sprout::q8)(air.hum_pct * 256.0f + 0.5f);
                rh_ms = now_ms;
                rh_ok_cached = true;
            }
            // sensor_id 4 carries the conditioned value the controller decided
            // on (median of the settled window).  During a pulse train there is
            // no filtered value, so fall back to the instantaneous sample to
            // keep the series' cadence.
            const float moisture_pct =
                (float)(eng.moisture_usable ? eng.moisture_q8 : moisture_q8) / 256.0f;
            send_sensor_report(identity, air, air_ok, PUMP_ACTUATION_ENABLED != 0,
                               pump_take_interval_ms() / 1000, pump_is_on(),
                               moisture_pct, probe_ok, control.profile());
        }

        vTaskDelay(pdMS_TO_TICKS(TICK_MS));
    }
}
