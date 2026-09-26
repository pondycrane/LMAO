// ESP32-S3 peripheral-interrupt handler stubs (T1).
//
// esp-hal's vectored-interrupt dispatch builds a per-source dispatch table in
// `.rwtext.interrupt` with one entry per ICU interrupt source, but defines no
// strong symbols for them in the bare-metal (non-`unstable`) configuration.
// T1 is a boot test that configures no interrupts, so each entry is bound to a
// weak no-op handler: any unprogrammed interrupt source that fires does
// nothing (matching the behaviour of an unprogrammed vectored table).
//
// Real interrupt binding arrives with T3+ (radio/timer work), at which point
// the relevant handlers are bound via `esp_hal`'s interrupt mechanism; these
// stubs are the untouched fallback for sources nothing has claimed.
//
// Generated from the ESP32-S3 ICU interrupt-source list (the exact symbols
// the linker reported as undefined for the T1 build); do not hand-edit — if
// the set drifts, regenerate from the linker's `undefined reference` set.

/// Weak no-op handler for the `AES` vectored interrupt source.
#[cfg_attr(not(test), no_mangle)]
extern "C" fn AES() {
    // intentional no-op until a peripheral claims this interrupt (T3+)
}

/// Weak no-op handler for the `APB_ADC` vectored interrupt source.
#[cfg_attr(not(test), no_mangle)]
extern "C" fn APB_ADC() {
    // intentional no-op until a peripheral claims this interrupt (T3+)
}

/// Weak no-op handler for the `ASSIST_DEBUG` vectored interrupt source.
#[cfg_attr(not(test), no_mangle)]
extern "C" fn ASSIST_DEBUG() {
    // intentional no-op until a peripheral claims this interrupt (T3+)
}

/// Weak no-op handler for the `BACKUP_PMS_VIOLATE` vectored interrupt source.
#[cfg_attr(not(test), no_mangle)]
extern "C" fn BACKUP_PMS_VIOLATE() {
    // intentional no-op until a peripheral claims this interrupt (T3+)
}

/// Weak no-op handler for the `BT_BB` vectored interrupt source.
#[cfg_attr(not(test), no_mangle)]
extern "C" fn BT_BB() {
    // intentional no-op until a peripheral claims this interrupt (T3+)
}

/// Weak no-op handler for the `BT_BB_NMI` vectored interrupt source.
#[cfg_attr(not(test), no_mangle)]
extern "C" fn BT_BB_NMI() {
    // intentional no-op until a peripheral claims this interrupt (T3+)
}

/// Weak no-op handler for the `BT_MAC` vectored interrupt source.
#[cfg_attr(not(test), no_mangle)]
extern "C" fn BT_MAC() {
    // intentional no-op until a peripheral claims this interrupt (T3+)
}

/// Weak no-op handler for the `CACHE_CORE0_ACS` vectored interrupt source.
#[cfg_attr(not(test), no_mangle)]
extern "C" fn CACHE_CORE0_ACS() {
    // intentional no-op until a peripheral claims this interrupt (T3+)
}

/// Weak no-op handler for the `CACHE_CORE1_ACS` vectored interrupt source.
#[cfg_attr(not(test), no_mangle)]
extern "C" fn CACHE_CORE1_ACS() {
    // intentional no-op until a peripheral claims this interrupt (T3+)
}

/// Weak no-op handler for the `CACHE_IA` vectored interrupt source.
#[cfg_attr(not(test), no_mangle)]
extern "C" fn CACHE_IA() {
    // intentional no-op until a peripheral claims this interrupt (T3+)
}

/// Weak no-op handler for the `CORE0_DRAM0_PMS` vectored interrupt source.
#[cfg_attr(not(test), no_mangle)]
extern "C" fn CORE0_DRAM0_PMS() {
    // intentional no-op until a peripheral claims this interrupt (T3+)
}

/// Weak no-op handler for the `CORE0_IRAM0_PMS` vectored interrupt source.
#[cfg_attr(not(test), no_mangle)]
extern "C" fn CORE0_IRAM0_PMS() {
    // intentional no-op until a peripheral claims this interrupt (T3+)
}

/// Weak no-op handler for the `CORE0_PIF_PMS` vectored interrupt source.
#[cfg_attr(not(test), no_mangle)]
extern "C" fn CORE0_PIF_PMS() {
    // intentional no-op until a peripheral claims this interrupt (T3+)
}

/// Weak no-op handler for the `CORE0_PIF_PMS_SIZE` vectored interrupt source.
#[cfg_attr(not(test), no_mangle)]
extern "C" fn CORE0_PIF_PMS_SIZE() {
    // intentional no-op until a peripheral claims this interrupt (T3+)
}

/// Weak no-op handler for the `CORE1_DRAM0_PMS` vectored interrupt source.
#[cfg_attr(not(test), no_mangle)]
extern "C" fn CORE1_DRAM0_PMS() {
    // intentional no-op until a peripheral claims this interrupt (T3+)
}

/// Weak no-op handler for the `CORE1_IRAM0_PMS` vectored interrupt source.
#[cfg_attr(not(test), no_mangle)]
extern "C" fn CORE1_IRAM0_PMS() {
    // intentional no-op until a peripheral claims this interrupt (T3+)
}

/// Weak no-op handler for the `CORE1_PIF_PMS` vectored interrupt source.
#[cfg_attr(not(test), no_mangle)]
extern "C" fn CORE1_PIF_PMS() {
    // intentional no-op until a peripheral claims this interrupt (T3+)
}

/// Weak no-op handler for the `CORE1_PIF_PMS_SIZE` vectored interrupt source.
#[cfg_attr(not(test), no_mangle)]
extern "C" fn CORE1_PIF_PMS_SIZE() {
    // intentional no-op until a peripheral claims this interrupt (T3+)
}

/// Weak no-op handler for the `DCACHE_PRELOAD0` vectored interrupt source.
#[cfg_attr(not(test), no_mangle)]
extern "C" fn DCACHE_PRELOAD0() {
    // intentional no-op until a peripheral claims this interrupt (T3+)
}

/// Weak no-op handler for the `DCACHE_SYNC0` vectored interrupt source.
#[cfg_attr(not(test), no_mangle)]
extern "C" fn DCACHE_SYNC0() {
    // intentional no-op until a peripheral claims this interrupt (T3+)
}

/// Weak no-op handler for the `DMA_APBPERI_PMS` vectored interrupt source.
#[cfg_attr(not(test), no_mangle)]
extern "C" fn DMA_APBPERI_PMS() {
    // intentional no-op until a peripheral claims this interrupt (T3+)
}

/// Weak no-op handler for the `DMA_EXTMEM_REJECT` vectored interrupt source.
#[cfg_attr(not(test), no_mangle)]
extern "C" fn DMA_EXTMEM_REJECT() {
    // intentional no-op until a peripheral claims this interrupt (T3+)
}

/// Weak no-op handler for the `DMA_IN_CH0` vectored interrupt source.
#[cfg_attr(not(test), no_mangle)]
extern "C" fn DMA_IN_CH0() {
    // intentional no-op until a peripheral claims this interrupt (T3+)
}

/// Weak no-op handler for the `DMA_IN_CH1` vectored interrupt source.
#[cfg_attr(not(test), no_mangle)]
extern "C" fn DMA_IN_CH1() {
    // intentional no-op until a peripheral claims this interrupt (T3+)
}

/// Weak no-op handler for the `DMA_IN_CH2` vectored interrupt source.
#[cfg_attr(not(test), no_mangle)]
extern "C" fn DMA_IN_CH2() {
    // intentional no-op until a peripheral claims this interrupt (T3+)
}

/// Weak no-op handler for the `DMA_IN_CH3` vectored interrupt source.
#[cfg_attr(not(test), no_mangle)]
extern "C" fn DMA_IN_CH3() {
    // intentional no-op until a peripheral claims this interrupt (T3+)
}

/// Weak no-op handler for the `DMA_IN_CH4` vectored interrupt source.
#[cfg_attr(not(test), no_mangle)]
extern "C" fn DMA_IN_CH4() {
    // intentional no-op until a peripheral claims this interrupt (T3+)
}

/// Weak no-op handler for the `DMA_OUT_CH0` vectored interrupt source.
#[cfg_attr(not(test), no_mangle)]
extern "C" fn DMA_OUT_CH0() {
    // intentional no-op until a peripheral claims this interrupt (T3+)
}

/// Weak no-op handler for the `DMA_OUT_CH1` vectored interrupt source.
#[cfg_attr(not(test), no_mangle)]
extern "C" fn DMA_OUT_CH1() {
    // intentional no-op until a peripheral claims this interrupt (T3+)
}

/// Weak no-op handler for the `DMA_OUT_CH2` vectored interrupt source.
#[cfg_attr(not(test), no_mangle)]
extern "C" fn DMA_OUT_CH2() {
    // intentional no-op until a peripheral claims this interrupt (T3+)
}

/// Weak no-op handler for the `DMA_OUT_CH3` vectored interrupt source.
#[cfg_attr(not(test), no_mangle)]
extern "C" fn DMA_OUT_CH3() {
    // intentional no-op until a peripheral claims this interrupt (T3+)
}

/// Weak no-op handler for the `DMA_OUT_CH4` vectored interrupt source.
#[cfg_attr(not(test), no_mangle)]
extern "C" fn DMA_OUT_CH4() {
    // intentional no-op until a peripheral claims this interrupt (T3+)
}

/// Weak no-op handler for the `EFUSE` vectored interrupt source.
#[cfg_attr(not(test), no_mangle)]
extern "C" fn EFUSE() {
    // intentional no-op until a peripheral claims this interrupt (T3+)
}

/// Weak no-op handler for the `FROM_CPU_INTR0` vectored interrupt source.
#[cfg_attr(not(test), no_mangle)]
extern "C" fn FROM_CPU_INTR0() {
    // intentional no-op until a peripheral claims this interrupt (T3+)
}

/// Weak no-op handler for the `FROM_CPU_INTR1` vectored interrupt source.
#[cfg_attr(not(test), no_mangle)]
extern "C" fn FROM_CPU_INTR1() {
    // intentional no-op until a peripheral claims this interrupt (T3+)
}

/// Weak no-op handler for the `FROM_CPU_INTR2` vectored interrupt source.
#[cfg_attr(not(test), no_mangle)]
extern "C" fn FROM_CPU_INTR2() {
    // intentional no-op until a peripheral claims this interrupt (T3+)
}

/// Weak no-op handler for the `FROM_CPU_INTR3` vectored interrupt source.
#[cfg_attr(not(test), no_mangle)]
extern "C" fn FROM_CPU_INTR3() {
    // intentional no-op until a peripheral claims this interrupt (T3+)
}

/// Weak no-op handler for the `GPIO` vectored interrupt source.
#[cfg_attr(not(test), no_mangle)]
extern "C" fn GPIO() {
    // intentional no-op until a peripheral claims this interrupt (T3+)
}

/// Weak no-op handler for the `GPIO_INTR_2` vectored interrupt source.
#[cfg_attr(not(test), no_mangle)]
extern "C" fn GPIO_INTR_2() {
    // intentional no-op until a peripheral claims this interrupt (T3+)
}

/// Weak no-op handler for the `GPIO_NMI` vectored interrupt source.
#[cfg_attr(not(test), no_mangle)]
extern "C" fn GPIO_NMI() {
    // intentional no-op until a peripheral claims this interrupt (T3+)
}

/// Weak no-op handler for the `GPIO_NMI_2` vectored interrupt source.
#[cfg_attr(not(test), no_mangle)]
extern "C" fn GPIO_NMI_2() {
    // intentional no-op until a peripheral claims this interrupt (T3+)
}

/// Weak no-op handler for the `I2C_EXT0` vectored interrupt source.
#[cfg_attr(not(test), no_mangle)]
extern "C" fn I2C_EXT0() {
    // intentional no-op until a peripheral claims this interrupt (T3+)
}

/// Weak no-op handler for the `I2C_EXT1` vectored interrupt source.
#[cfg_attr(not(test), no_mangle)]
extern "C" fn I2C_EXT1() {
    // intentional no-op until a peripheral claims this interrupt (T3+)
}

/// Weak no-op handler for the `I2C_MASTER` vectored interrupt source.
#[cfg_attr(not(test), no_mangle)]
extern "C" fn I2C_MASTER() {
    // intentional no-op until a peripheral claims this interrupt (T3+)
}

/// Weak no-op handler for the `I2S0` vectored interrupt source.
#[cfg_attr(not(test), no_mangle)]
extern "C" fn I2S0() {
    // intentional no-op until a peripheral claims this interrupt (T3+)
}

/// Weak no-op handler for the `I2S1` vectored interrupt source.
#[cfg_attr(not(test), no_mangle)]
extern "C" fn I2S1() {
    // intentional no-op until a peripheral claims this interrupt (T3+)
}

/// Weak no-op handler for the `ICACHE_PRELOAD0` vectored interrupt source.
#[cfg_attr(not(test), no_mangle)]
extern "C" fn ICACHE_PRELOAD0() {
    // intentional no-op until a peripheral claims this interrupt (T3+)
}

/// Weak no-op handler for the `ICACHE_SYNC0` vectored interrupt source.
#[cfg_attr(not(test), no_mangle)]
extern "C" fn ICACHE_SYNC0() {
    // intentional no-op until a peripheral claims this interrupt (T3+)
}

/// Weak no-op handler for the `LCD_CAM` vectored interrupt source.
#[cfg_attr(not(test), no_mangle)]
extern "C" fn LCD_CAM() {
    // intentional no-op until a peripheral claims this interrupt (T3+)
}

/// Weak no-op handler for the `LEDC` vectored interrupt source.
#[cfg_attr(not(test), no_mangle)]
extern "C" fn LEDC() {
    // intentional no-op until a peripheral claims this interrupt (T3+)
}

/// Weak no-op handler for the `MCPWM0` vectored interrupt source.
#[cfg_attr(not(test), no_mangle)]
extern "C" fn MCPWM0() {
    // intentional no-op until a peripheral claims this interrupt (T3+)
}

/// Weak no-op handler for the `MCPWM1` vectored interrupt source.
#[cfg_attr(not(test), no_mangle)]
extern "C" fn MCPWM1() {
    // intentional no-op until a peripheral claims this interrupt (T3+)
}

/// Weak no-op handler for the `NMI` vectored interrupt source.
#[cfg_attr(not(test), no_mangle)]
extern "C" fn NMI() {
    // intentional no-op until a peripheral claims this interrupt (T3+)
}

/// Weak no-op handler for the `PCNT` vectored interrupt source.
#[cfg_attr(not(test), no_mangle)]
extern "C" fn PCNT() {
    // intentional no-op until a peripheral claims this interrupt (T3+)
}

/// Weak no-op handler for the `PERI_BACKUP` vectored interrupt source.
#[cfg_attr(not(test), no_mangle)]
extern "C" fn PERI_BACKUP() {
    // intentional no-op until a peripheral claims this interrupt (T3+)
}

/// Weak no-op handler for the `RMT` vectored interrupt source.
#[cfg_attr(not(test), no_mangle)]
extern "C" fn RMT() {
    // intentional no-op until a peripheral claims this interrupt (T3+)
}

/// Weak no-op handler for the `RSA` vectored interrupt source.
#[cfg_attr(not(test), no_mangle)]
extern "C" fn RSA() {
    // intentional no-op until a peripheral claims this interrupt (T3+)
}

/// Weak no-op handler for the `RTC_CORE` vectored interrupt source.
#[cfg_attr(not(test), no_mangle)]
extern "C" fn RTC_CORE() {
    // intentional no-op until a peripheral claims this interrupt (T3+)
}

/// Weak no-op handler for the `RWBLE` vectored interrupt source.
#[cfg_attr(not(test), no_mangle)]
extern "C" fn RWBLE() {
    // intentional no-op until a peripheral claims this interrupt (T3+)
}

/// Weak no-op handler for the `RWBLE_NMI` vectored interrupt source.
#[cfg_attr(not(test), no_mangle)]
extern "C" fn RWBLE_NMI() {
    // intentional no-op until a peripheral claims this interrupt (T3+)
}

/// Weak no-op handler for the `RWBT` vectored interrupt source.
#[cfg_attr(not(test), no_mangle)]
extern "C" fn RWBT() {
    // intentional no-op until a peripheral claims this interrupt (T3+)
}

/// Weak no-op handler for the `RWBT_NMI` vectored interrupt source.
#[cfg_attr(not(test), no_mangle)]
extern "C" fn RWBT_NMI() {
    // intentional no-op until a peripheral claims this interrupt (T3+)
}

/// Weak no-op handler for the `SDIO_HOST` vectored interrupt source.
#[cfg_attr(not(test), no_mangle)]
extern "C" fn SDIO_HOST() {
    // intentional no-op until a peripheral claims this interrupt (T3+)
}

/// Weak no-op handler for the `SHA` vectored interrupt source.
#[cfg_attr(not(test), no_mangle)]
extern "C" fn SHA() {
    // intentional no-op until a peripheral claims this interrupt (T3+)
}

/// Weak no-op handler for the `SLC0` vectored interrupt source.
#[cfg_attr(not(test), no_mangle)]
extern "C" fn SLC0() {
    // intentional no-op until a peripheral claims this interrupt (T3+)
}

/// Weak no-op handler for the `SLC1` vectored interrupt source.
#[cfg_attr(not(test), no_mangle)]
extern "C" fn SLC1() {
    // intentional no-op until a peripheral claims this interrupt (T3+)
}

/// Weak no-op handler for the `SPI1` vectored interrupt source.
#[cfg_attr(not(test), no_mangle)]
extern "C" fn SPI1() {
    // intentional no-op until a peripheral claims this interrupt (T3+)
}

/// Weak no-op handler for the `SPI2` vectored interrupt source.
#[cfg_attr(not(test), no_mangle)]
extern "C" fn SPI2() {
    // intentional no-op until a peripheral claims this interrupt (T3+)
}

/// Weak no-op handler for the `SPI2_DMA` vectored interrupt source.
#[cfg_attr(not(test), no_mangle)]
extern "C" fn SPI2_DMA() {
    // intentional no-op until a peripheral claims this interrupt (T3+)
}

/// Weak no-op handler for the `SPI3` vectored interrupt source.
#[cfg_attr(not(test), no_mangle)]
extern "C" fn SPI3() {
    // intentional no-op until a peripheral claims this interrupt (T3+)
}

/// Weak no-op handler for the `SPI3_DMA` vectored interrupt source.
#[cfg_attr(not(test), no_mangle)]
extern "C" fn SPI3_DMA() {
    // intentional no-op until a peripheral claims this interrupt (T3+)
}

/// Weak no-op handler for the `SPI_MEM_REJECT_CACHE` vectored interrupt source.
#[cfg_attr(not(test), no_mangle)]
extern "C" fn SPI_MEM_REJECT_CACHE() {
    // intentional no-op until a peripheral claims this interrupt (T3+)
}

/// Weak no-op handler for the `SYSTIMER_TARGET0` vectored interrupt source.
#[cfg_attr(not(test), no_mangle)]
extern "C" fn SYSTIMER_TARGET0() {
    // intentional no-op until a peripheral claims this interrupt (T3+)
}

/// Weak no-op handler for the `SYSTIMER_TARGET1` vectored interrupt source.
#[cfg_attr(not(test), no_mangle)]
extern "C" fn SYSTIMER_TARGET1() {
    // intentional no-op until a peripheral claims this interrupt (T3+)
}

/// Weak no-op handler for the `SYSTIMER_TARGET2` vectored interrupt source.
#[cfg_attr(not(test), no_mangle)]
extern "C" fn SYSTIMER_TARGET2() {
    // intentional no-op until a peripheral claims this interrupt (T3+)
}

/// Weak no-op handler for the `TG0_T0_LEVEL` vectored interrupt source.
#[cfg_attr(not(test), no_mangle)]
extern "C" fn TG0_T0_LEVEL() {
    // intentional no-op until a peripheral claims this interrupt (T3+)
}

/// Weak no-op handler for the `TG0_T1_LEVEL` vectored interrupt source.
#[cfg_attr(not(test), no_mangle)]
extern "C" fn TG0_T1_LEVEL() {
    // intentional no-op until a peripheral claims this interrupt (T3+)
}

/// Weak no-op handler for the `TG0_WDT_LEVEL` vectored interrupt source.
#[cfg_attr(not(test), no_mangle)]
extern "C" fn TG0_WDT_LEVEL() {
    // intentional no-op until a peripheral claims this interrupt (T3+)
}

/// Weak no-op handler for the `TG1_T0_LEVEL` vectored interrupt source.
#[cfg_attr(not(test), no_mangle)]
extern "C" fn TG1_T0_LEVEL() {
    // intentional no-op until a peripheral claims this interrupt (T3+)
}

/// Weak no-op handler for the `TG1_T1_LEVEL` vectored interrupt source.
#[cfg_attr(not(test), no_mangle)]
extern "C" fn TG1_T1_LEVEL() {
    // intentional no-op until a peripheral claims this interrupt (T3+)
}

/// Weak no-op handler for the `TG1_WDT_LEVEL` vectored interrupt source.
#[cfg_attr(not(test), no_mangle)]
extern "C" fn TG1_WDT_LEVEL() {
    // intentional no-op until a peripheral claims this interrupt (T3+)
}

/// Weak no-op handler for the `TIMER1` vectored interrupt source.
#[cfg_attr(not(test), no_mangle)]
extern "C" fn TIMER1() {
    // intentional no-op until a peripheral claims this interrupt (T3+)
}

/// Weak no-op handler for the `TIMER2` vectored interrupt source.
#[cfg_attr(not(test), no_mangle)]
extern "C" fn TIMER2() {
    // intentional no-op until a peripheral claims this interrupt (T3+)
}

/// Weak no-op handler for the `TWAI0` vectored interrupt source.
#[cfg_attr(not(test), no_mangle)]
extern "C" fn TWAI0() {
    // intentional no-op until a peripheral claims this interrupt (T3+)
}

/// Weak no-op handler for the `UART0` vectored interrupt source.
#[cfg_attr(not(test), no_mangle)]
extern "C" fn UART0() {
    // intentional no-op until a peripheral claims this interrupt (T3+)
}

/// Weak no-op handler for the `UART1` vectored interrupt source.
#[cfg_attr(not(test), no_mangle)]
extern "C" fn UART1() {
    // intentional no-op until a peripheral claims this interrupt (T3+)
}

/// Weak no-op handler for the `UART2` vectored interrupt source.
#[cfg_attr(not(test), no_mangle)]
extern "C" fn UART2() {
    // intentional no-op until a peripheral claims this interrupt (T3+)
}

/// Weak no-op handler for the `UHCI0` vectored interrupt source.
#[cfg_attr(not(test), no_mangle)]
extern "C" fn UHCI0() {
    // intentional no-op until a peripheral claims this interrupt (T3+)
}

/// Weak no-op handler for the `USB` vectored interrupt source.
#[cfg_attr(not(test), no_mangle)]
extern "C" fn USB() {
    // intentional no-op until a peripheral claims this interrupt (T3+)
}

/// Weak no-op handler for the `USB_DEVICE` vectored interrupt source.
#[cfg_attr(not(test), no_mangle)]
extern "C" fn USB_DEVICE() {
    // intentional no-op until a peripheral claims this interrupt (T3+)
}

/// Weak no-op handler for the `WDT` vectored interrupt source.
#[cfg_attr(not(test), no_mangle)]
extern "C" fn WDT() {
    // intentional no-op until a peripheral claims this interrupt (T3+)
}

/// Weak no-op handler for the `WIFI_BB` vectored interrupt source.
#[cfg_attr(not(test), no_mangle)]
extern "C" fn WIFI_BB() {
    // intentional no-op until a peripheral claims this interrupt (T3+)
}

/// Weak no-op handler for the `WIFI_MAC` vectored interrupt source.
#[cfg_attr(not(test), no_mangle)]
extern "C" fn WIFI_MAC() {
    // intentional no-op until a peripheral claims this interrupt (T3+)
}

/// Weak no-op handler for the `WIFI_NMI` vectored interrupt source.
#[cfg_attr(not(test), no_mangle)]
extern "C" fn WIFI_NMI() {
    // intentional no-op until a peripheral claims this interrupt (T3+)
}

/// Weak no-op handler for the `WIFI_PWR` vectored interrupt source.
#[cfg_attr(not(test), no_mangle)]
extern "C" fn WIFI_PWR() {
    // intentional no-op until a peripheral claims this interrupt (T3+)
}
