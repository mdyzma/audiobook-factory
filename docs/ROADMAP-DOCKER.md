# Roadmap: a containerised audiobook-factory

Where the Docker story stands, what is wrong with it, and the order to fix it.

Written on 2026-09-06 from a Mac, which cannot test any of this: Docker on macOS
runs in a Linux VM with no GPU passthrough. Everything below is a plan, and the
current assets are **unbuilt and unrun**.

## Why containers suit this project

The whole architecture is three environments that cannot share an interpreter,
plus native dependencies that have already cost real time: FFmpeg version
coupling, a torchcodec extension tied to one torch ABI, and CUDA wheels that
must match the card. A container pins all of that per service, which is exactly
the shape of the problem.

It also solves the thing a bootstrap script cannot: reproducing the *native*
layer, not just the Python one.

## What exists today

```
bookbinder/Dockerfile      debian:bookworm-slim + uv         BUILT, VERIFIED
studio/Dockerfile          debian:bookworm-slim + uv         BUILT, VERIFIED
transcriber/Dockerfile     nvidia/cuda:12.8.1 + uv           written, never built
narrator/Dockerfile        nvidia/cuda:12.8.1 + uv           written, never built
docker-compose.yml         cpu and gpu profiles
```

The CUDA images cannot be built here: those bases are amd64 only and this is
Apple Silicon. They are written to the same pattern as the two that work, but
treat the first build as work rather than a formality.

### Phase 1: make the images honest — DONE (2026-09-07)

All four install from the committed `uv.lock` with `uv sync --frozen`, so a
container resolves the same versions CI and the host resolve. Verified for
bookbinder: every Python package matches the host exactly, down to the patch.

Details worth keeping:

- **uv installs its own CPython**, the same 3.11.9 the host uses, so the image
  is not merely similar to a working machine but identical to one.
- **The layout mirrors the repository.** Modules find the project root as
  `parents[3]` of their own file, so the code lives at
  `/app/<env>/src/<env>/…` and `/app` is the root, with `data/` and `config/`
  mounted there. Putting it anywhere else silently resolved the root to `/`.
- **Dependencies are copied and synced before the source**, so editing code does
  not re-resolve anything.
- **No hard-coded stage.** `ENTRYPOINT ["uv", "run", "--frozen", "--no-dev"]`,
  and the command names the module, so one image serves every stage of its
  environment.
- **ffmpeg is pinned by the base image**, at 5.1 on bookworm. That is deliberate:
  FFmpeg major versions have already broken this project once. The host runs 9.0
  and both produce byte-comparable chapter marks, which is the property that
  matters here.
- **A `.dockerignore`** keeps `data/`, `training/` and the virtualenvs out of the
  build context. Without it every build ships several gigabytes to the daemon.

### Phase 2: CPU-only path first — DONE (2026-09-07)

`docker compose --profile cpu` builds bookbinder and studio, around 690 MB each,
and needs no GPU. `just docker-smoke` takes an ebook to a chaptered m4b of
silence entirely in containers, which was the bar for this phase.

The dashboard also runs containerised on `127.0.0.1:8765`, reading the shared
`data/` mount. One limitation, deliberate: `just` is not in that image, so the
buttons that start pipeline stages do not work there. In a container it is a
browsing and listening view. Running stages across containers is phase 4.

### Phase 3: CUDA services

- Move both GPU images to a CUDA 12.8 base for Blackwell.
- Install torch from the cu128 index while holding the version pins.
- Persist the model cache as a named volume, so weights survive a rebuild:
  `~/.local/share/tts` in the container.
- Verify inside the container that `torch.cuda.get_device_capability()` reports
  `(12, 0)` and that a matrix multiply actually runs, not merely that
  `is_available()` is true.

**Done when:** `docker compose run narrator synth <slug>` renders real audio on
the 5090 at well above realtime.

### Phase 4: one command

A top-level service, or an extension to `bin/audiobook`, that runs the stages in
order across containers with the shared `data/` mount as the only channel. The
stages already communicate solely through files, so this is orchestration rather
than redesign.

**Done when:** `docker compose run factory --voice sample.mp3 --book book.epub`
produces an audiobook.

### Phase 5: publish

Only worth doing once phases 1 to 4 are proven.

- Build in CI on tags and push to GitHub Container Registry.
- Tag by both project version and CUDA version, since the CUDA build is a
  meaningful axis for anyone with a different card.
- A CPU-only variant of the narrator would be much smaller and useful to people
  who only want to assemble, but it is optional.

## Platform notes

**Linux** is the target. It needs `nvidia-container-toolkit` on the host for GPU
passthrough; without it the CUDA services start and see no device.

**Windows** works through WSL2 with Docker Desktop and the NVIDIA driver on the
Windows side, not inside WSL. Expect the bind mount to be slower across the
filesystem boundary; keeping `data/` inside the WSL filesystem rather than under
`/mnt/c` matters for a multi-gigabyte render.

**macOS** gets no GPU. Phase 2 is testable there; phases 3 and 4 are not. That is
also why the native uv path stays the supported one on a Mac, and containers are
an alternative rather than a replacement.

## What this does not replace

`install.sh` and `install.ps1` remain the right answer for a development machine.
They install only what the host needs, they do not ship CUDA wheels to a laptop
that cannot use them, and they leave the code editable in place. Containers are
for reproducing a known-good run, not for working on the code.
