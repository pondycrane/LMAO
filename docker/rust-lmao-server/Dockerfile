# Rust LMAO server app layer (`lmao-server-rs`) — the full-Rust replacement for
# the Python `lmao_server` (gRPC :50051 + contacts :8081 + NATS JetStream +
# DeliveryHandler). See docs/rust-server-migration.md.
#
# Build (repo root is the context so `proto/` resolves for tonic-build):
#   docker build -f docker/rust-lmao-server/Dockerfile -t lmao-server-rust-app:latest .
# Release via install_all:
#   bazel run //tools:install_all -- --include-services

# ── Builder: cargo build with protoc for tonic-build/prost-build ────────────
FROM rust:1.83-bookworm AS builder
RUN apt-get update \
    && apt-get install -y --no-install-recommends protobuf-compiler ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /src
# Build context must be the repo root so the crate's build.rs `../../../proto`
# path (relative to host/lmao-server-rs) resolves to /src/proto.
COPY rust-client/ /src/rust-client/
COPY proto/ /src/proto/

WORKDIR /src/rust-client
# Self-contained build (protoc installed above for tonic-build/prost-build).
# The first image build compiles the full dependency tree; subsequent deploys
# only rebuild the crate itself if the dependency graph is unchanged.
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
