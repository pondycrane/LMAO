# Rust LMAO server app layer (`lmao-server-rs`) — the full-Rust replacement for
# the Python `lmao_server` (gRPC :50051 + contacts :8081 + NATS JetStream +
# DeliveryHandler). See docs/rust-server-migration.md.
#
# Build (repo root is the context so `proto/` resolves for tonic-build):
#   docker build -f docker/rust-lmao-server/Dockerfile -t lmao-server-rust-app:latest .
# Release via install_all:
#   bazel run //tools:install_all -- --include-services
#
# Build-time optimization — warm builder base:
#   `builder.Dockerfile` compiles the FULL dependency tree ONCE into
#   /src/rust-client/target (tag lmao-server-rust-builder:<tag>; install_all
#   builds it on first deploy if missing). This Dockerfile starts FROM that
#   warm image so each deploy recompiles only the changed server source
#   against the cached deps — minutes, not the ~15-min cold tree compile.
#   No cargo-chef/BuildKit/dummy-crate gymnastics; works on any builder.

# ── builder: thin build on top of the warm deps image ───────────────────────
FROM lmao-server-rust-builder:1 AS builder
WORKDIR /src/rust-client
# Copy the real (possibly changed) server source; only changed crates rebuild.
COPY rust-client/host /src/rust-client/host
RUN cargo build --release -p lmao-server-rs

# ── Runtime ────────────────────────────────────────────────────────────────
FROM debian:bookworm-slim
RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates \
    && rm -rf /var/lib/apt/lists/*

COPY --from=builder /src/rust-client/target/release/lmao-server-rs /usr/local/bin/lmao-server

WORKDIR /data
ENV LMAO_CONTACTS_DB=/data/contacts.db \
    NATS_SERVER=nats://nats-server.default.svc.cluster.local:4222
EXPOSE 50051 8081
ENTRYPOINT ["lmao-server"]
