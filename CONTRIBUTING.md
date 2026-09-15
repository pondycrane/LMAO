# CONTRIBUTING

Thanks for considering a contribution to LMAO. This project is a LoRa mesh
communication POC built on Reticulum + LXMF, and it's developed with Bazel,
hardware E2E gates, and the Archon agent workflows described in `AGENTS.md`.

Please read `AGENTS.md` and `README.md` in full before doing anything.

## Code of conduct

Be respectful and constructive. This is a solo-maintained project; reviewers
have finite time. Assume good faith.

## How to contribute

- **Report bugs / request features**: open a GitHub issue. Include hardware
  (Cardputer, Heltec RNode, etc.), firmware versions, and the exact command
  that failed.
- **Pull requests**: fork, create a feature branch, and open a PR against
  `master`. Keep changes focused and rebased on latest master.

## Development environment

- Build tool: **Bazel** — see `MODULE.bazel` / `WORKSPACE`.
- Python deps live under `lmao_server/requirements*.txt` (RNS, LXMF, protobuf).
- Formatting/linting: `ruff.toml`; type checking: `mypy.ini`. Run both before
  submitting.

## Testing

Unit tests run via Bazel. Hardware-dependent tests skip when no device is
present:

```bash
bazel test //tests:...
```

E2E verification (requires real hardware) is mandatory before marking a
feature complete:

```bash
# Flash verification (requires Cardputer)
bazel test //tests:test_cardputer_e2e --test_output=all

# LoRa communication verification (requires Cardputer + Heltec RNode)
bazel test //tests:test_cardputer_lora_e2e --test_output=all
```

See `AGENTS.md` and `.archon/commands/lmao-hardware-e2e.md` for the exact
checks and Phases 4a-4c.

### Hardware rules (do not violate)

- **NEVER use esptool on the Cardputer.** Flash only via the Bazel
  `//cardputer_client:flash` target or the serial flash tool in download mode.
- **NEVER flash the Heltec RNode via esptool or any other method.** It can
  only be flashed via https://flasher.rnode.network/ — interrupting a flash
  bricks the device.

## Licensing

LMAO is released under the MIT License (see `LICENSE`). By contributing you
agree that your contributions are licensed under the same terms.

LMAO depends on Reticulum and LXMF, which are distributed under the
"Reticulum License" (see `THIRD_PARTY_NOTICES.md`) — a permissive license with
two restrictions: no use to purposefully harm human beings, and no use in
AI/ML training datasets. Keep that notice intact when redistributing LMAO.
