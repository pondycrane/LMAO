# Host-testable golden-byte test for the shared family-calendar wire codec
# (firmware_common/lma_common/lma_calendar.cpp).  Golden bytes are generated
# from the canonical Python encoder cardputer_client/proto/lma_encoder.py.

cc_test(
    name = "lma_calendar_test",
    srcs = ["lma_calendar_test.cpp"],
    tags = ["unit"],
    deps = ["//firmware_common:lma_calendar"],
)

# Calendar event-selection/formatting model (selection semantics ported from
# the old MicroPython renderer).
cc_test(
    name = "calendar_model_test",
    srcs = ["calendar_model_test.cpp"],
    tags = ["unit"],
    deps = ["//robodog_client:calendar_model"],
)

# Live interop smoke: fetch the family calendar from a real LMAO server over
# RNS-TCP (mirrors smart_irrigation/native-client's interop harness).  Manual —
# needs a reachable server (production :4246 or a local WiFi-only server).
#   bazel run //robodog_client/firmware/tests:calendar_fetch -- 192.168.50.67 4246
cc_binary(
    name = "calendar_fetch",
    srcs = ["calendar_fetch.cpp"],
    tags = ["manual"],
    deps = ["//robodog_client:robodog_net"],
)
