/*
 * main.c -- ESP-IDF entry shim.  The real application lives in robodog.c
 * (robodog_app_main); keeping the entry thin makes the IDF entry point
 * conventional and leaves room to add `shared_stack`/task wiring later
 * without touching the app body.
 */
void robodog_app_main(void);

void app_main(void)
{
    robodog_app_main();
}
