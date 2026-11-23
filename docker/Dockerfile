# ZeroCosts helper image: rustc + cargo + clippy + rustfmt + gdb + the helper, for learners who don't want to
# install the toolchain (Windows, macOS, or any machine with Docker). Build context = this folder
# (build.py --package --hosted copies serve.py and rf_tracer.py in next to this file).
#
#   docker build -t zerocosts-helper .
#   docker build --build-arg WITH_HASKELL=1 -t zerocosts-helper:haskell .      # adds GHC for the Haskell lessons (+~500 MB)
#
# Multi-arch (Apple Silicon + PCs):  docker buildx build --platform linux/amd64,linux/arm64 -t ghcr.io/OWNER/zerocosts-helper:latest --push .
FROM debian:trixie-slim

ARG RUST_TOOLCHAIN=stable
ARG WITH_HASKELL=0
ENV DEBIAN_FRONTEND=noninteractive

# build-essential: rustc needs a linker; gdb+binutils: the memory tracer; socat: reach servers the learner's program starts
RUN apt-get update \
 && apt-get install -y --no-install-recommends ca-certificates curl git python3 gdb binutils build-essential pkg-config libssl-dev socat procps \
 && if [ "$WITH_HASKELL" = "1" ]; then apt-get install -y --no-install-recommends ghc; fi \
 && rm -rf /var/lib/apt/lists/*

# Rust lives in /opt (read-only at run time, shared by whatever user id the launcher passes in)
ENV RUSTUP_HOME=/opt/rustup CARGO_HOME=/opt/cargo PATH=/opt/cargo/bin:$PATH
RUN curl -sSf https://sh.rustup.rs | sh -s -- -y --profile minimal --default-toolchain "${RUST_TOOLCHAIN}" -c clippy -c rustfmt \
 && chmod -R a+rX /opt/rustup /opt/cargo \
 && rustc --version && cargo --version && gdb --version | head -1

WORKDIR /app
COPY serve.py rf_tracer.py entrypoint.sh selftest.sh /app/
RUN chmod 755 /app/entrypoint.sh /app/selftest.sh && mkdir -p /workspace && chmod 777 /workspace

# Everything the learner creates lives in /workspace (a folder on their own computer, mounted by the launcher).
ENV RF_CONTAINER=1 RF_PORT=8765 RF_WORKSPACE=/workspace HOME=/tmp
EXPOSE 8765
ENTRYPOINT ["/app/entrypoint.sh"]
