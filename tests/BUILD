load("@rules_python//python:defs.bzl", "py_test")

py_test(
    name = "test_lma_encoder",
    srcs = ["test_lma_encoder.py"],
    deps = [
        "//cardputer_client/proto:lma_encoder",
        "//lma_core",
        "@lmao_pip//protobuf",
        "@lmao_pip//pytest",
    ],
)

py_test(
    name = "test_server_handler",
    srcs = ["test_server_handler.py"],
    deps = [
        "//lmao_server:server_lib",
        "@lmao_pip//protobuf",
        "@lmao_pip//pytest",
        "@lmao_pip//pytest_asyncio",
    ],
)

py_test(
    name = "test_cardputer_e2e",
    srcs = [
        "e2e/test_cardputer_flash.py",
        "e2e/e2e_helpers.py",
    ],
    main = "e2e/test_cardputer_flash.py",
    data = [
        "//cardputer_client:boot.py",
        "//cardputer_client:config.py",
        "//cardputer_client:lora_boards.py",
        "//cardputer_client:main.py",
        "//cardputer_client/proto:lma_encoder.py",
        "//cardputer_client:lib_files",
    ],
    deps = [
        "//cardputer_client:flash_lib",
        "@lmao_pip//pyserial",
        "@lmao_pip//pytest",
    ],
    # E2E test requires physical Cardputer hardware connected via USB.
    # The test auto-skips when hardware is not detected.
    tags = ["e2e", "requires_hardware"],
)

py_test(
    name = "test_main",
    srcs = ["test_main.py"],
    deps = [
        "//cardputer_client:main_lib",
        "@lmao_pip//pytest",
    ],
)

py_test(
    name = "test_flash",
    srcs = ["test_flash.py"],
    deps = [
        "//cardputer_client:flash_lib",
        "@lmao_pip//pyserial",
        "@lmao_pip//pytest",
    ],
)

py_test(
    name = "test_config",
    srcs = ["test_config.py"],
    data = [
        "//cardputer_client:config.py",
    ],
    deps = [
        "//lmao_server:server_lib",
        "@lmao_pip//pytest",
    ],
)

py_test(
    name = "test_lma_core",
    srcs = ["test_lma_core.py"],
    deps = [
        "//lma_core",
        "@lmao_pip//pytest",
    ],
)

py_test(
    name = "test_urns_packet",
    srcs = ["test_urns_packet.py"],
    data = ["//cardputer_client:lib_files"],
    deps = [
        "@lmao_pip//pytest",
    ],
)

py_test(
    name = "test_urns_identity",
    srcs = ["test_urns_identity.py"],
    data = ["//cardputer_client:lib_files"],
    deps = [
        "@lmao_pip//pytest",
    ],
)

py_test(
    name = "test_urns_lxmf_message",
    srcs = ["test_urns_lxmf_message.py"],
    data = ["//cardputer_client:lib_files"],
    deps = [
        "@lmao_pip//pytest",
    ],
)

py_test(
    name = "test_urns_transport",
    srcs = ["test_urns_transport.py"],
    data = ["//cardputer_client:lib_files"],
    deps = [
        "@lmao_pip//pytest",
    ],
)

py_test(
    name = "test_human_client",
    srcs = ["test_human_client.py"],
    deps = [
        "//human_client:client_lib",
        "@lmao_pip//protobuf",
        "@lmao_pip//pytest",
    ],
)

py_test(
    name = "test_cardputer_lora_e2e",
    srcs = [
        "e2e/test_cardputer_lora_e2e.py",
        "e2e/e2e_helpers.py",
    ],
    main = "e2e/test_cardputer_lora_e2e.py",
    data = [
        "//cardputer_client:boot.py",
        "//cardputer_client:config.py",
        "//cardputer_client:lora_boards.py",
        "//cardputer_client:main.py",
        "//cardputer_client/proto:lma_encoder.py",
        "//cardputer_client:lib_files",
    ],
    deps = [
        "//cardputer_client:flash_lib",
        "//lma_core",
        "//lmao_server:server_lib",
        "@lmao_pip//lxmf",
        "@lmao_pip//pyserial",
        "@lmao_pip//pytest",
        "@lmao_pip//rns",
    ],
    # E2E test requires physical Cardputer hardware AND a Heltec RNode
    # connected via USB.  The test auto-skips when hardware is not detected.
    tags = ["e2e", "requires_hardware"],
)

py_test(
    name = "test_human_client_config",
    srcs = ["test_human_client_config.py"],
    deps = [
        "//human_client:client_lib",
        "@lmao_pip//pytest",
    ],
)
