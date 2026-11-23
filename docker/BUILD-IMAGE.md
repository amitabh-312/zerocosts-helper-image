# Build and publish the ZeroCosts helper image

The image holds the Rust toolchain, gdb and the helper. Learners pull it once through the launcher.
It contains no course content, so it is safe to make public. You do this on any machine with Docker
(your laptop is fine); the VPS does not need Docker for this.

The `docker/` folder of this package already holds `serve.py` and `rf_tracer.py`. Two ways to build: GitHub Actions (recommended, first) or your own computer.

## Recommended: let GitHub build it (no Docker needed on your computer)
Do not build on the VPS: it only has one CPU type, no multi-architecture builder, and Docker there is extra attack surface.

1. Create a new repository on github.com (name it `zerocosts-helper-image`, **Public**, empty).
2. Upload the `docker/` folder of this package to it: Add file → Upload files → drag the whole `docker` folder (it must stay a folder
   called `docker` at the top of the repository, holding Dockerfile, serve.py, rf_tracer.py, entrypoint.sh, selftest.sh).
3. Add file → Create new file. In the name box type `.github/workflows/helper-image.yml` (typing the slashes makes the folders),
   paste the contents of `docker/github-workflow-helper-image.yml`, and Commit.
4. Open the repository's **Actions** tab. The workflow starts by itself; if not, choose helper-image → Run workflow.
   The first run takes 20-40 minutes (the arm64 part is emulated). Green tick = done.
5. Your profile → Packages → `zerocosts-helper` should now exist. Open it → Package settings → Change visibility → **Public**
   (a package published from a public repository is usually public already; check anyway).
6. Verify from any machine with Docker: `docker logout ghcr.io && docker pull ghcr.io/amitabh-312/zerocosts-helper:latest`.

## Alternative: build it yourself on a computer with Docker (not the VPS)

### 1. Build and test it locally (about 5-10 minutes the first time)

    cd docker
    docker build -t zerocosts-helper .
    docker run --rm --entrypoint /app/selftest.sh zerocosts-helper

Expected last line: `✓ self-test: rustc and the memory tracer's debugger both work`.
If it says the debugger could not trace, run it again with `--cap-add=SYS_PTRACE` (the learner launcher already does this).

Optional second image with GHC for the Haskell stage (adds about 500 MB):

    docker build --build-arg WITH_HASKELL=1 -t zerocosts-helper:haskell .

### 2. Log in to GitHub's registry (ghcr.io)

1. GitHub → Settings → Developer settings → Personal access tokens → Tokens (classic) → Generate new token.
   Tick only `write:packages` (it adds `read:packages`). Copy the token.
2. Log in (the username is your GitHub username, in lowercase):

        echo YOUR_TOKEN | docker login ghcr.io -u amitabh-312 --password-stdin

### 3. Push a multi-architecture image (PCs and Apple Silicon)

One time only, create a builder that can do both architectures:

    docker buildx create --name zc --use
    docker run --privileged --rm tonistiigi/binfmt --install arm64     # lets an x86 machine build arm64 (skip on Apple Silicon)

Then build and push (arm64 under emulation is slow; 20-40 minutes the first time is normal):

    docker buildx build --platform linux/amd64,linux/arm64 \
      -t ghcr.io/amitabh-312/zerocosts-helper:latest --push .

Haskell variant, if you want it:

    docker buildx build --platform linux/amd64,linux/arm64 --build-arg WITH_HASKELL=1 \
      -t ghcr.io/amitabh-312/zerocosts-helper:haskell --push .

### 4. Make the package public

GitHub → your profile → Packages → `zerocosts-helper` → Package settings → Change visibility → Public.
Check from a machine that is not logged in:

    docker logout ghcr.io
    docker pull ghcr.io/amitabh-312/zerocosts-helper:latest

## Then: point the website at it (needed either way)

On the VPS, in the `.env` file:

    RF_HELPER_IMAGE=ghcr.io/amitabh-312/zerocosts-helper:latest

Then `sudo systemctl restart zerocosts`. Download the helper from the site (Account → Run on your computer)
and check that the launcher mentions that image name.

## Updating later

Change `serve.py` or the Dockerfile, rebuild with step 3, and learners get the new image the next time they run
`docker pull ghcr.io/amitabh-312/zerocosts-helper:latest` (the launcher pulls when it starts).
