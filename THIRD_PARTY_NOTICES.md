# Third-Party Notices

LMAO builds on open-source projects. This notice records the attribution and
license obligations of bundled and depended-upon components.

## Project: LMAO

- Copyright (c) 2026 Pondycrane
- Licensed under the [MIT License](LICENSE)

## Reticulum

- Source: https://github.com/markqvist/Reticulum
- Dependency: `rns` (Reticulum Network Stack)
- License: **Reticulum License** (permissive, fields of use apply)
- Copyright (c) 2016-2026 Mark Qvist

## LXMF

- Source: https://github.com/markqvist/LXMF
- Dependency: `lxmf` (pinned to `1.0.1` in `lmao_server/requirements_lock.txt`)
- License: **Reticulum License** (permissive, fields of use apply)
- Copyright (c) 2020-2025 Mark Qvist

### Reticulum License — summary

Both Reticulum and LXMF are distributed under the "Reticulum License", which
is MIT-like with the following additional restrictions:

1. The Software shall not be used in any kind of system which includes amongst
   its functions the ability to purposefully do harm to human beings.
2. The Software shall not be used, directly or indirectly, in the creation of
   an artificial intelligence, machine learning, or language model training
   dataset, including but not limited to any use that contributes to the
   training or development of such a model or algorithm.
3. The copyright and permission notices must be retained in all copies or
   substantial portions of the Software.

**Implication for LMAO:** because LMAO `import`s and depends on RNS/LXMF,
these conditions ride along with the redistributed system. This is reducible
to a requirement to keep attribution, plus a statement of the two restrictions
above. Anyone building on LMAO inherits these conditions for the RNS/LXMF-
derived portions.

## Other dependencies

RNS and LXMF in turn depend on additional packages (e.g. `cryptography`,
`msgpack`, `netifaces`, `pyserial`, `adafruit` for the Cardputer toolchain,
protobuf/grpc). Review each package's own license for redistribution terms;
most are permissive (MIT/Apache/BSD).
