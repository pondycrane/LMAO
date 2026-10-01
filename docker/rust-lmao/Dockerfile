# Rust LMAO server — LXMF-rs spine image
#
# Packages the LXMF-rs 0.12 release binaries (FreeTAKTeam) + the RNode `/dev/ttyUSB0`
# config for the full-Rust LMAO server. The release tarball is downloaded at build
# time (no binaries committed to the repo). Adjust LXMF_RS_VERSION / arch as needed.
#
# Build+push (arm64 nodes):
#   docker build -t 192.168.50.153:5000/lmao-server-rust:test docker/rust-lmao
#   docker push 192.168.50.153:5000/lmao-server-rust:test
FROM debian:bookworm-slim
ARG LXMF_RS_VERSION=0.12.0
RUN apt-get update && apt-get install -y --no-install-recommends curl ca-certificates \
    && case "$(uname -m)" in \
         aarch64) LXMF_ARCH="linux-aarch64" ;; \
         armv7l|armhf) LXMF_ARCH="linux-armv7" ;; \
         *) LXMF_ARCH="linux-x86_64" ;; \
       esac \
    && curl -sL -o /tmp/lxmf.tgz \
       "https://github.com/FreeTAKTeam/LXMF-rs/releases/download/v${LXMF_RS_VERSION}/lxmf-rs_${LXMF_RS_VERSION}_${LXMF_ARCH}.tar.gz" \
    && tar -xzf /tmp/lxmf.tgz -C /usr/local/bin --strip-components=1 \
    && chmod +x /usr/local/bin/* \
    && rm -f /tmp/lxmf.tgz /var/lib/apt/lists/*/*
RUN mkdir -p /etc/lxmf /var/lib/lxmf
COPY daemon.toml rns.toml /etc/lxmf/
WORKDIR /var/lib/lxmf
ENTRYPOINT ["lxmd","--config","/etc/lxmf/daemon.toml","--rnsconfig","/etc/lxmf/rns.toml"]
