/*
 * ES8311 audio codec driver for the Cardputer ADV.
 *
 * The ADV routes audio through an ES8311 codec over I2C (SCL=9, SDA=8); the
 * codec is clocked entirely from the I2S master (there is no MCLK pin), so the
 * ES8311 derives its internal MCLK from the I2S BCLK ("MCLK=BCLK" mode,
 * REG01 bit7): with REG02=0x18 the internal MCLK = BCLK*8, which equals
 * 256*fs for 16-bit slots (BCLK = 32*fs stereo).  The I2S must therefore use
 * 16-bit slots, NOT 32-bit (32-bit slots would give BCLK*8 = 512*fs and the
 * codec PLL would never lock -> a silent ADC).
 *
 * Register sequence transcribed verbatim from M5Stack's M5Unified factory
 * driver (components/M5Unified/M5Unified/src/M5Unified.cpp):
 *   _speaker_enabled_cb_cardputer_adv  (DAC path)
 *   _microphone_enabled_cb_atom_echo   (ADC path: REG0E=0x02 enables the
 *                                        analog PGA + ADC modulator, Mic1p-Mic1n,
 *                                        REG14=0x10 PGA min, REG17=0xFF ADC max)
 * This keeps only the ES8311 + esp_driver_i2c; no audio framework pulled in.
 */
#include "es8311.h"

#include "driver/i2c_master.h"
#include "esp_err.h"
#include "esp_log.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"

#define TAG "es8311"

#define ES8311_I2C_PORT I2C_NUM_0
#define ES8311_I2C_CLK  100000
#define ES8311_XFER_TIMEOUT_MS 20

/* --- registers (Everest ES8311 map) ------------------------------------ */
#define REG_RESET   0x00
#define REG_CLK1    0x01
#define REG_CLK2    0x02
#define REG_SYS_0D  0x0D
#define REG_SYS_0E  0x0E
#define REG_SYS_12  0x12
#define REG_SYS_13  0x13
#define REG_ADC_14  0x14
#define REG_ADC_17  0x17
#define REG_ADC_1C  0x1C
#define REG_DAC_32  0x32
#define REG_DAC_37  0x37
#define REG_CHIP_ID 0xFD

static i2c_master_bus_handle_t g_bus = NULL;
static i2c_master_dev_handle_t g_dev = NULL;

/* (reg, value) pairs written on power-up; {0xff,0} terminates. */
static const uint8_t s_power_up[][2] = {
    {REG_RESET, 0x80},   // CSM power on (release reset)
    {REG_CLK1,  0xB5},   // MCLK = BCLK
    {REG_CLK2,  0x18},   // DIG_MCLK = BCLK * 8  -> 256*fs for 16-bit slots
    {REG_SYS_0D, 0x01},  // power up analog
    {REG_SYS_0E, 0x02},  // enable analog PGA + ADC modulator ("mic on")
    {REG_SYS_12, 0x00},  // DAC power up
    {REG_SYS_13, 0x10},  // enable output to HP drive
    {REG_ADC_14, 0x10},  // Mic1p-Mic1n, PGA gain (min)
    {REG_ADC_17, 0xFF},  // ADC volume max gain
    {REG_ADC_1C, 0x6A},  // ADC EQ bypass + DC-offset cancel
    {REG_DAC_32, 0xBF},  // DAC volume 0 dB
    {REG_DAC_37, 0x08},  // DAC EQ bypass
    {0xff, 0},
};

static esp_err_t es8311_write_reg(uint8_t reg, uint8_t data) {
    uint8_t buf[2] = {reg, data};
    esp_err_t rc = i2c_master_transmit(g_dev, buf, sizeof(buf),
                                       ES8311_XFER_TIMEOUT_MS);
    if (rc != ESP_OK) {
        ESP_LOGE(TAG, "i2c write reg 0x%02x failed (%d)", reg, rc);
    }
    return rc;
}

esp_err_t es8311_init(void) {
    i2c_master_bus_config_t bus_cfg = {
        .i2c_port = ES8311_I2C_PORT,
        .sda_io_num = 8,
        .scl_io_num = 9,
        .clk_source = I2C_CLK_SRC_DEFAULT,
        .glitch_ignore_cnt = 7,
        .flags.enable_internal_pullup = true,
    };
    esp_err_t rc = i2c_new_master_bus(&bus_cfg, &g_bus);
    if (rc != ESP_OK) {
        return rc;
    }
    i2c_device_config_t dev_cfg = {
        .dev_addr_length = I2C_ADDR_BIT_LEN_7,
        .device_address = ES8311_ADDR,
        .scl_speed_hz = ES8311_I2C_CLK,
    };
    rc = i2c_master_bus_add_device(g_bus, &dev_cfg, &g_dev);
    if (rc != ESP_OK) {
        return rc;
    }
    for (int i = 0; s_power_up[i][0] != 0xff; i++) {
        // Double-write the first register: the ES8311 occasionally misses the
        // first I2C transaction after power-up (esp-adf workaround).
        if (i == 0) {
            es8311_write_reg(s_power_up[i][0], s_power_up[i][1]);
        }
        rc = es8311_write_reg(s_power_up[i][0], s_power_up[i][1]);
        if (rc != ESP_OK) {
            return rc;
        }
    }
    return ESP_OK;
}

esp_err_t es8311_set_dac_volume(uint8_t reg_value) {
    return es8311_write_reg(REG_DAC_32, reg_value);
}

uint8_t es8311_chip_id(void) {
    uint8_t id = 0;
    if (i2c_master_transmit_receive(g_dev, (const uint8_t[]){REG_CHIP_ID}, 1,
                                    &id, 1, ES8311_XFER_TIMEOUT_MS) != ESP_OK) {
        return 0;
    }
    return id;
}
