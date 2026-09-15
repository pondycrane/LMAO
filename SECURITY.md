# Security Policy

## Reporting a vulnerability

LMAO is a hardware-and-network project (LoRa RF, NATS, gRPC, DuckDB, K8s).
Please report security issues privately rather than in a public issue.

**Preferred**: email the maintainer (see repo owner) with:
- A clear description of the vulnerability and its impact
- Steps to reproduce
- Affected components (server, client, firmware) and versions

Do **not** test or disclose against production infrastructure (e.g., the
running mesh, the K8s cluster nodes) without permission. Use a sandboxed
setup of your own.

## Scope

Things we care about:
- Authentication / identity handling (Reticulum identities, LXMF delivery)
- Message confidentiality and integrity over the LoRa RF path
- gRPC / NATS / DuckDB ingress paths in `lmao_server`
- The IoT ingest pipeline and any sensor data handling
- Misconfiguration that could expose private keys or mesh identities

Out of scope:
- The M5Stack Cardputer and Heltec RNode are **legacy 32-bit hardware** with
  limited security properties; known limitations are documented in README.

## Supported versions

LMAO is distributed without versioned releases; `master` is the supported
target. You will always need the latest `master` to receive a fix.

## Disclosure

We'll acknowledge reports within 7 days. Fixes land on `master` and are noted
in commit history. Public disclosure is coordinated with the reporter.

## Security-related project rules (from `AGENTS.md`)

- Never run `esptool ... chip_id` or any other esptool probing on the
  Cardputer — it can brick the USB-Serial-JTAG interface.
- The RNode can only be flashed via https://flasher.rnode.network/, never
  esptool.
