# LMAO Identity & Contact System — Design (2026-10-28)

Status: proposal. Audience: anyone touching device provisioning,
`ALLOWED_CLIENTS`, the contact book, or firmware DEST_HASH injection.

This design follows the identity/contact inventory taken from the tree on
2026-10-28 (every claim below cites `file:line` from that inventory, verified
first-hand where load-bearing).

## Problem statement

The network has **two device registries that never interact**, and a
directory that is **write-only**:

| Problem | Evidence |
|---|---|
| Two parallel registries, zero interaction | `ALLOWED_CLIENTS` is a static set (`lmao_server/server.py:92-117`); the contact book is dynamic (`server.py:138-176`). The gate at `server.py:585-595` runs **before** `_learn_contact()` (:599), so an unknown device is dropped silently and cannot even appear in the directory. |
| Adding a device = edit k8s YAML + rollout | `k8s/lmao-server.yaml:113-114`. The list already carries two stale µPython hashes (yaml:106-112) because removal is as painful as addition. |
| Directory is write-only | No send path consults contacts: `send_command` (`server.py:380-466`) and gRPC `Send` (:831-868) take raw hex + `Identity.recall`. #151's "send to any known contact" is half-implemented. `human_client` is raw-hex-only (`human_client/client.py:239-298`) despite contacts being its stated purpose. |
| DEST_HASH baked at flash time, fragile | Server identity loss = every client silently dark (#70). Sprout hardcodes it in committed source (`smart_irrigation/native-client/firmware/main/main.cpp:62`); `install_all` is the only safe flash path. |
| Dead schema | `pubkey_hex` populated only via a swallowed `known_destinations` probe (`server.py:152-157`); `identity_hash` never populated; the PK is `device_name` while docs claim the key is `delivery_hash` (`lma_core/contact_book.py:15` vs :29-32) — renaming to an existing name collides on the wrong constraint. |
| Identity asymmetry | Sprout: NVS-minted, no pinning. human_client: ephemeral identity in `/tmp` (`client.py:311`), can never pass its own server's gate. RoboDog: no RNS identity at all. |
| No lifecycle | No rotation, revocation, or deletion path anywhere. |

## Design principles

1. **The RNS identity hash is the identity.** Self-certifying, already on the
   wire. No PKI on top — contacts are *metadata over hashes* (name, type,
   trust state), never a parallel naming system.
2. **One registry.** The contact book becomes the only device registry; the
   allow-list becomes a column in it.
3. **Provisioning is a physical act.** The operator running `install_all`
   with the device plugged in *is* the trust decision — no separate approval
   ceremony for the flash flow.
4. **Bake = pin, not lifeline.** Flash-time DEST_HASH becomes a TOFU pin for
   bootstrap, with announce-learn as the recovery path.

## Proposed model

```
install_all (operator = trust decision)
  ├── flash device
  ├── pin server DEST_HASH (verify vs pod — existing behavior)
  └── POST /contacts {hash, name, type, pubkey, state=approved}

lmao-server LXMF delivery gate
  ├── known + approved  → accept, touch last_seen
  ├── unknown           → auto-register state=pending + loud log + drop
  └── pending / revoked → drop

send paths resolve names through the directory
  ├── human_client:  /send living-room hi  → GET /contacts/find?name=
  └── send_command / gRPC Send: target = name | type | hash
```

## Phased change list

### Phase 1 — unify the registry (server-only, no device changes)

- Schema: PRIMARY KEY → `delivery_hash`; `device_name` becomes a mutable
  unique attribute (fixes the rename-collides-on-PK bug). Add
  `state TEXT NOT NULL DEFAULT 'pending'` (`pending|approved|revoked`) and
  `approved_at`. Same change in both backends (`contact_book.py`,
  `contact_book_pg.py`).
- Move `_learn_contact` **before** the gate; unknown senders register as
  `pending` with a loud log line. The gate becomes `state == 'approved'`.
- Seed at startup: existing `LMAO_ALLOWED_CLIENTS` env hashes → `approved`
  contacts (type `unknown`). The env becomes a cold-start seed, not the live
  config. Delete the hardcoded `_DEFAULT_ALLOWED_CLIENTS` from source
  (`server.py:103-106`).
- Approval UX: `POST /contacts/<hash>/approve` and `/revoke`. New device
  flashes, sends once, shows up in `GET /contacts?state=pending`, operator
  approves — no rollout, no YAML edit.

### Phase 2 — make the directory readable by senders (completes #151)

- `send_command` / gRPC `Send`: accept `target` as name, type, or hash;
  resolve via the contact book, fall back to `Identity.recall`.
- human_client: `/send <name> <msg>` via the contacts HTTP API (the client is
  on the LAN anyway via `LMAO_SERVER_HOST`); persist its identity in
  `devs/human-<host>/identity` instead of `/tmp` so it can itself be approved
  and receive replies.

### Phase 3 — provisioning closes the loop

- `install_all` already pins the client identity and verifies the pod hash;
  add the step it currently prints as instructions: register the device
  (name/type/pubkey derived locally from the identity file — no fragile
  probe) with `state=approved`.
- Sprout gets the Cardputer's build-time injection
  (`LMAO_DEST_HASH_HEX`/`LMAO_NODE_IDENTITY_HEX` via build.sh), killing the
  hardcoded literals at `main.cpp:62,129-133` (Sprout review P1.2).
- Populate `pubkey_hex` properly at registration or drop the column; same
  decision for `identity_hash` (populate from the identity, or delete —
  nothing reads it).

### Phase 4 — server-hash resilience (optional, LoRa clients only)

- Devices keep the baked DEST_HASH as a TOFU pin but may *learn* the server
  from its announce when no pin exists (Sprout's currently-dead
  announce-learn path becomes the recovery path); the learned hash is stored
  in NVS so reboots don't need a re-announce.
- Hash *change* with an existing pin = warn-log, keep pin — a rogue LoRa
  announce can then only waste airtime, not redirect data.
- Server identity loss stops being an all-clients-dark event and becomes
  "re-pin on next install".

## Rejected alternatives

- **On-air pairing handshake**: LoRa airtime cost, zero added trust over
  install-time pinning on a home mesh.
- **Allow-list stays in YAML as GitOps**: device churn here is physical flash
  events, not declarative config — DB state matches reality. The env remains
  as a seed for cold start only.
- **mTLS / certificate layer**: RNS identities are already self-certifying
  keys; a second PKI adds nothing.

## Explicit non-goals

- RoboDog stays LAN-trust with no RNS identity; it gets a contact entry
  (`type=robodog`) only so the directory is a complete inventory.
- Contacts/query APIs stay unauthenticated in-cluster (unchanged trust
  model).
- The server private key never enters the DB; recovery remains PVC +
  documented `~/lmao-identity-backup-*` restore.

## Sizing

Phase 1 is ~150 lines of server + migration code and removes the worst daily
friction (YAML-edit-to-add-a-device, silent drops, dead columns). Phases 2
and 3 are independent and can land in either order. Phase 4 is optional and
LoRa-only.
