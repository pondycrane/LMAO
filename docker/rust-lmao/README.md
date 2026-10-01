# Rust LMAO server — build & deploy

Full-Rust LMAO server spine (LXMF-rs 0.12) with the LoRa RNode interface.
Plan + status: `docs/rust-server-migration.md`.

## Build + push (arm64 nodes)

```bash
docker build -t 192.168.50.153:5000/lmao-server-rust:test docker/rust-lmao
docker push 192.168.50.153:5000/lmao-server-rust:test
```

The Dockerfile downloads the LXMF-rs release tarball at build time (FreeTAKTeam
`lxmf-rs_0.12.0_linux-aarch64`), so no binaries are committed.

## Deploy

```bash
kubectl apply -f k8s/lmao-server-rust.yaml   # pins to node tp4 (the RNode node)
```

Mounts `/dev/ttyUSB0` (the Heltec V3 RNode) into the container. The image
runs `lxmd --config daemon.toml --rnsconfig rns.toml` — RNS transport + LXMF
spine with the RNode LoRa interface on 868/BW125/SF7/CR5.

## Verify (from inside the pod)

```bash
POD=$(kubectl get pod -n default -l app=lmao-server-rust -o jsonpath='{.items[0].metadata.name}')
kubectl exec -n default "$POD" -- rnstatus-rs --rpc 127.0.0.1:4243
```

A detected, online RNode shows `rnode868 lora ... detected=true online=true
freq=868000000 bw=125000 sf=7 cr=5`, and growing `Announces rx=` / `Traffic
rx=` means real RF RX on the channel.

## Notes

- **Registry reachability**: nodes on `192.168.10.x` must reach the registry at
  `192.168.50.153:5000` (selfhost wlan0). `tp4` can; pin the deployment there
  (matches the legacy `k8s/lmao-server.yaml` which also pins `tp4`).
- The LMAO `lmao.data` Link+Resource receiver (cardputer resource ingest) is not
  yet wired into this spine — that is the remaining migration app work.
