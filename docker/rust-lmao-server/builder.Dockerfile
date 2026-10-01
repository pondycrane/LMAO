# Warm Rust builder image for `lmao-server-rs` — the slow part, built ONCE.
#
# Compiles the server's full dependency tree (tonic/hyper/rusqlite/async-nats/
# rns-net + leaf crates) into /src/rust-client/target. Deploy builds
# (`Dockerfile`) start `FROM lmao-server-rust-builder:<tag>` and only recompile
# the changed server source against this warm target — minutes, not the
# ~15-min cold tree compile, and with no dependence on Docker layer caching.
#
#   docker build -t lmao-server-rust-builder:1 -f docker/rust-lmao-server/builder.Dockerfile .
# (install_all builds it automatically on first deploy if missing.)

FROM rust:1.98-bookworm

RUN apt-get update \
    && apt-get install -y --no-install-recommends protobuf-compiler ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /src
COPY rust-client /src/rust-client
COPY proto /src/proto

WORKDIR /src/rust-client
RUN cargo build --release -p lmao-server-rs && rm -rf target/release/lmao-server
