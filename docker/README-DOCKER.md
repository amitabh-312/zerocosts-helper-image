# ZeroCosts runner (Docker)

For learners who don't want to install Rust, gdb and a linker (Windows, macOS, or any machine with Docker).
The container holds the toolchain; your projects live in a normal folder on your own computer (`~/ZeroCosts`).
Your code runs on your machine, inside the container, never on our servers.

## Learners
1. Install Docker Desktop (Windows/macOS; free for personal use and learning) or Docker Engine (Linux), open it, and wait until it says it is running.
2. Download the launcher from the course website (Account → Run on your computer), unzip it, then:
   - macOS / Linux: `./zerocosts-docker.sh`
   - Windows: double-click `zerocosts-docker.cmd`
3. Your browser reopens the course already connected. Keep the launcher window open while you study; Ctrl+C stops it.
   (If the page does not connect by itself, paste the pairing code shown in the launcher window.)

First start downloads the image (roughly 1–2 GB: Rust, gdb, a C toolchain). Later starts take a few seconds.
Docker Desktop is free for personal use, education and small businesses; larger companies need a paid plan for professional use.
Free alternatives that work with the same launcher: Podman Desktop with Docker compatibility, Rancher Desktop, OrbStack or Colima (macOS), Docker Engine (Linux).

Things that work: Run, cargo build/run/test/clippy/fmt, multi-file projects, crates from crates.io, the live Memory Tracer.
Server projects (web server :7878, chat :8080, REST API :3000, Redis clone :6380): open `http://127.0.0.1:PORT` in your browser as usual.
Not available: "Open in editor" (use your own editor on `~/ZeroCosts`), reading files outside that folder.

Settings (environment variables): `ZEROCOSTS_DIR` (projects folder), `ZEROCOSTS_PORT` (helper port), `ZEROCOSTS_PORTS` (published server ports),
`ZEROCOSTS_IMAGE` (use another image).

Troubleshooting
- "self-test: the debugger could not trace": Docker is blocking ptrace. Re-run with the launcher (it adds `--cap-add=SYS_PTRACE`),
  or add `--security-opt seccomp=unconfined` to the `docker run` line in the launcher.
- "port is already allocated": something on your machine uses that port; set `ZEROCOSTS_PORTS` without it or stop the other program.
- Linux: the launcher passes `--user` so files in your folder are yours. Don't run it with sudo.

## You (the course owner): build and publish the image
Step-by-step with GitHub's registry: see `BUILD-IMAGE.md` in this folder. The short version:
    cd docker
    docker build -t zerocosts-helper .                                   # local test
    docker build --build-arg WITH_HASKELL=1 -t zerocosts-helper:haskell .
    docker buildx build --platform linux/amd64,linux/arm64 -t ghcr.io/YOU/zerocosts-helper:latest --push .   # for learners
Then set `RF_HELPER_IMAGE=ghcr.io/YOU/zerocosts-helper:latest` on the website server, so the launchers in the helper download
point at it. Make the package public (the image holds no course content, only the helper and the toolchain).
`github-workflow-helper-image.yml` builds and pushes it from GitHub Actions on every change to this folder.

Smoke test after building (needs a real Docker):
    docker run --rm --entrypoint /app/selftest.sh zerocosts-helper          # expect: ✓ self-test
    docker run --rm -e RF_SITE=http://127.0.0.1:8081 -p 127.0.0.1:8765:8765 zerocosts-helper   # expect: Pairing code

## Safety model
- The helper inside the container listens on 0.0.0.0 only because Docker publishes its port to your loopback (`127.0.0.1`) only.
  It still checks the Host header, the allowed site origin and the pairing token, exactly like the native helper.
- Outside the official image, the helper refuses to listen on anything but 127.0.0.1.
- The container gets one extra capability (ptrace, for the debugger) and a process limit. It is not privileged.
