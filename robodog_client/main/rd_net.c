/*
 * rd_net.c -- WiFi station + framed-TCP calendar sync (see rd_net.h).
 */
#include "rd_net.h"

#include <errno.h>
#include <fcntl.h>
#include <string.h>

#include "esp_err.h"
#include "esp_event.h"
#include "esp_log.h"
#include "esp_netif.h"
#include "esp_timer.h"
#include "esp_wifi.h"
#include "freertos/FreeRTOS.h"
#include "freertos/event_groups.h"
#include "lwip/netdb.h"
#include "lwip/sockets.h"

#include "config.h"

static const char *TAG = "rd_net";

#define RD_WIFI_GOT_IP ((EventBits_t)1u << 0)

static EventGroupHandle_t s_wifi_evt;
static bool s_wifi_started = false;
static char s_ssid[33];   /* the event handler logs/reconnects with these */
static char s_pass[65];

/* --------------------------------------------------------------- WiFi ---- */

/* Disconnect reason codes worth naming in the log (esp_wifi_types.h). */
static const char *wifi_reason_str(int r)
{
    switch (r) {
    case 201: return "NO_AP_FOUND (SSID not visible: 2.4GHz? range? hidden?)";
    case 202: return "AUTH_FAIL (wrong password / >63-char passphrase)";
    case 203: return "ASSOC_FAIL";
    case 15:  return "4WAY_HANDSHAKE_TIMEOUT (wrong password / length)";
    case 204: return "AUTH_EXPIRE";
    default:  return "other";
    }
}

static void wifi_event_handler(void *arg, esp_event_base_t base, int32_t id,
                               void *data)
{
    (void)arg;
    if (base == WIFI_EVENT && id == WIFI_EVENT_STA_START) {
        ESP_LOGI(TAG, "wifi STA started, connecting to '%s' (pass len %u)",
                 s_ssid, (unsigned)strlen(s_pass));
        esp_wifi_connect();
    } else if (base == WIFI_EVENT && id == WIFI_EVENT_STA_CONNECTED) {
        ESP_LOGI(TAG, "wifi associated with AP");
    } else if (base == WIFI_EVENT && id == WIFI_EVENT_STA_DISCONNECTED) {
        wifi_event_sta_disconnected_t *d = (wifi_event_sta_disconnected_t *)data;
        ESP_LOGW(TAG, "wifi disconnected reason=%d (%s)",
                 d ? d->reason : -1, wifi_reason_str(d ? d->reason : -1));
        if (s_wifi_started) {
            ESP_LOGW(TAG, "reconnecting");
            esp_wifi_connect();
        }
    } else if (base == IP_EVENT && id == IP_EVENT_STA_GOT_IP) {
        ESP_LOGI(TAG, "connected, got ip");
        xEventGroupSetBits(s_wifi_evt, RD_WIFI_GOT_IP);
    }
}

bool rd_net_init(void)
{
    if (s_wifi_evt != NULL) {
        return true;
    }
    s_wifi_evt = xEventGroupCreate();
    if (s_wifi_evt == NULL) {
        return false;
    }

    wifi_init_config_t wic = WIFI_INIT_CONFIG_DEFAULT();
    ESP_ERROR_CHECK(esp_wifi_init(&wic));
    /* create_default_wifi_sta() AFTER esp_wifi_init(); it returns esp_netif_t*
     * (NULL on failure), not esp_err_t, so it must NOT go through
     * ESP_ERROR_CHECK — check for NULL instead. */
    esp_netif_t *sta = esp_netif_create_default_wifi_sta();
    if (sta == NULL) {
        ESP_LOGE(TAG, "create_default_wifi_sta failed");
        return false;
    }

    ESP_ERROR_CHECK(esp_event_handler_instance_register(
        WIFI_EVENT, ESP_EVENT_ANY_ID, wifi_event_handler, NULL, NULL));
    ESP_ERROR_CHECK(esp_event_handler_instance_register(
        IP_EVENT, IP_EVENT_STA_GOT_IP, wifi_event_handler, NULL, NULL));
    return true;
}

bool rd_net_connect(const rd_config_t *cfg)
{
    snprintf(s_ssid, sizeof(s_ssid), "%s", cfg->wifi_ssid);
    snprintf(s_pass, sizeof(s_pass), "%s", cfg->wifi_pass);

    wifi_config_t wc = {0};
    strncpy((char *)wc.sta.ssid, s_ssid, sizeof(wc.sta.ssid) - 1);
    strncpy((char *)wc.sta.password, s_pass, sizeof(wc.sta.password) - 1);
    ESP_ERROR_CHECK(esp_wifi_set_mode(WIFI_MODE_STA));
    ESP_ERROR_CHECK(esp_wifi_set_config(WIFI_IF_STA, &wc));

    if (!s_wifi_started) {
        ESP_ERROR_CHECK(esp_wifi_start());
        s_wifi_started = true;
    }
    xEventGroupClearBits(s_wifi_evt, RD_WIFI_GOT_IP);
    bool ok = (xEventGroupWaitBits(s_wifi_evt, RD_WIFI_GOT_IP, pdFALSE, pdFALSE,
                                   pdMS_TO_TICKS(RD_WIFI_CONNECT_TIMEOUT_S * 1000)) &
               RD_WIFI_GOT_IP) != 0;
    if (!ok) {
        ESP_LOGW(TAG, "wifi connect timeout after %u s",
                 (unsigned)RD_WIFI_CONNECT_TIMEOUT_S);
        s_wifi_started = false;
        esp_wifi_stop();
    }
    return ok;
}

void rd_net_disconnect(void)
{
    if (s_wifi_started) {
        s_wifi_started = false;   /* stop the disconnect-retry handler first */
        esp_wifi_disconnect();
        esp_wifi_stop();
    }
}

/* --------------------------------------------------------- TCP sync ---- */

/* Monotonic microseconds. */
static int64_t now_us(void)
{
    return esp_timer_get_time();
}

static bool send_all(int fd, const uint8_t *p, size_t n)
{
    while (n > 0) {
        ssize_t w = send(fd, p, n, 0);
        if (w <= 0) {
            return false;
        }
        p += w;
        n -= (size_t)w;
    }
    return true;
}

/* Read exactly n bytes, but never past `deadline_us`: SO_RCVTIMEO bounds each
 * single recv, and the deadline bounds the WHOLE read, so a server that drips
 * one byte just under the socket timeout cannot hold the board for minutes. */
static bool recv_exact(int fd, uint8_t *p, size_t n, int64_t deadline_us)
{
    while (n > 0) {
        if (now_us() >= deadline_us) {
            return false;
        }
        ssize_t r = recv(fd, p, n, 0);
        if (r <= 0) {   /* timeout (SO_RCVTIMEO) or peer closed */
            return false;
        }
        p += r;
        n -= (size_t)r;
    }
    return true;
}

/* Blocking connect with a bounded wait: non-blocking connect + select, then
 * switch the socket back to blocking for the request/reply. */
static bool connect_bounded(int fd, const struct sockaddr *addr,
                            socklen_t addrlen, int timeout_ms)
{
    int flags = fcntl(fd, F_GETFL, 0);
    if (flags < 0 || fcntl(fd, F_SETFL, flags | O_NONBLOCK) < 0) {
        return false;
    }
    int rc = connect(fd, addr, addrlen);
    if (rc != 0) {
        if (errno != EINPROGRESS) {
            return false;
        }
        fd_set wfds;
        FD_ZERO(&wfds);
        FD_SET(fd, &wfds);
        struct timeval tv = {
            .tv_sec = timeout_ms / 1000,
            .tv_usec = (timeout_ms % 1000) * 1000,
        };
        rc = select(fd + 1, NULL, &wfds, NULL, &tv);
        if (rc <= 0) {
            return false;
        }
        int err = 0;
        socklen_t len = sizeof(err);
        if (getsockopt(fd, SOL_SOCKET, SO_ERROR, &err, &len) != 0 || err != 0) {
            return false;
        }
    }
    return fcntl(fd, F_SETFL, flags) == 0;   /* back to blocking */
}

bool rd_net_sync(const rd_config_t *cfg, const lma_cal_request_t *req,
                 lma_cal_bundle_t *bundle)
{
    /* One request/reply frame; the decoder's own buffer is sized by the same
     * contract, so a reply larger than LMA_PB_MAX_FRAME is rejected here
     * before it can be truncated silently. */
    static uint8_t frame[LMA_PB_MAX_FRAME];

    char port[8];
    snprintf(port, sizeof(port), "%u", RD_CAL_TCP_PORT);

    struct addrinfo hints;
    memset(&hints, 0, sizeof(hints));
    hints.ai_family = AF_INET;
    hints.ai_socktype = SOCK_STREAM;
    struct addrinfo *res = NULL;
    if (getaddrinfo(cfg->server_host, port, &hints, &res) != 0 || res == NULL) {
        ESP_LOGE(TAG, "cannot resolve server host '%s'", cfg->server_host);
        return false;
    }

    int fd = socket(AF_INET, SOCK_STREAM, IPPROTO_TCP);
    if (fd < 0) {
        freeaddrinfo(res);
        return false;
    }

    bool ok = false;
    const int plen = lma_proto_req_encode(req, frame + 4, sizeof(frame) - 4);
    if (plen < 0) {
        ESP_LOGE(TAG, "request does not fit the frame buffer");
        goto out;
    }
    if (!connect_bounded(fd, res->ai_addr, res->ai_addrlen,
                         RD_TCP_CONNECT_TIMEOUT_MS)) {
        ESP_LOGW(TAG, "tcp connect to %s:%s failed", cfg->server_host, port);
        goto out;
    }

    /* Per-recv socket timeout for the reply phase. */
    struct timeval tv = {
        .tv_sec = RD_TCP_READ_TIMEOUT_MS / 1000,
        .tv_usec = (RD_TCP_READ_TIMEOUT_MS % 1000) * 1000,
    };
    setsockopt(fd, SOL_SOCKET, SO_RCVTIMEO, &tv, sizeof(tv));

    /* [u32 BE length][payload] */
    frame[0] = (uint8_t)((unsigned)plen >> 24);
    frame[1] = (uint8_t)((unsigned)plen >> 16);
    frame[2] = (uint8_t)((unsigned)plen >> 8);
    frame[3] = (uint8_t)(unsigned)plen;
    if (!send_all(fd, frame, (size_t)plen + 4)) {
        ESP_LOGW(TAG, "request write failed");
        goto out;
    }

    const int64_t deadline = now_us() + (int64_t)RD_TCP_READ_TIMEOUT_MS * 1000;
    uint8_t head[4];
    if (!recv_exact(fd, head, sizeof(head), deadline)) {
        ESP_LOGW(TAG, "reply header read failed (timeout %u ms)",
                 (unsigned)RD_TCP_READ_TIMEOUT_MS);
        goto out;
    }
    const uint32_t rlen = ((uint32_t)head[0] << 24) | ((uint32_t)head[1] << 16) |
                          ((uint32_t)head[2] << 8) | (uint32_t)head[3];
    if (rlen > sizeof(frame)) {
        ESP_LOGW(TAG, "reply too large (%u > %u)", (unsigned)rlen,
                 (unsigned)sizeof(frame));
        goto out;
    }
    if (!recv_exact(fd, frame, rlen, deadline)) {
        ESP_LOGW(TAG, "reply body read failed");
        goto out;
    }
    if (rlen == 0) {
        ESP_LOGE(TAG, "empty calendar reply");
        goto out;
    }
    if (!lma_proto_bundle_decode(frame, rlen, bundle)) {
        ESP_LOGE(TAG, "malformed CalendarBundle (%u bytes)", (unsigned)rlen);
        goto out;
    }
    ok = true;

out:
    close(fd);
    freeaddrinfo(res);
    return ok;
}
