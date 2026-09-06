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
transcriber/Dockerfile     nvidia/cuda:12.4.1-runtime-ubuntu22.04
narrator/Dockerfile        nvidia/cuda:12.4.1-runtime-ubuntu22.04
bookbinder/Dockerfile      debian:bookworm-slim
docker-compose.yml         three services, shared ./data mount, GPU reservations
```

The shape is right. The contents are stale, because they were written before the
uv migration and before the version work that followed.

## What is wrong with it

**1. The CUDA base is wrong for Blackwell.** `nvidia/cuda:12.4.1` has no kernels
for sm_120, so the RTX 5090 will fail or fall back to CPU. Needs a 12.8 base.
Same trap as [HANDOFF-GPU.md](HANDOFF-GPU.md).

**2. The images ignore the locks.** They `pip install` loose version ranges,
which throws away the entire reason `uv.lock` is committed. A container that
resolves different versions than the host is worse than no container.

**3. The pins are missing.** Neither GPU image carries the constraints that keep
XTTS working: numpy below 2, the spaCy 3.7 line, transformers 4.40.2, torch 2.8
in the narrator and 2.14 in the transcriber. They would resolve to whatever is
current and break in the ways DECISIONS.md documents.

**4. Entry points are wrong.** Each image hard-codes one module, so a service can
only ever run one stage. `narrator` can synthesise but not clone.

**5. The model cache is not persisted.** XTTS-v2 is 1.7 GB and would download on
every container start.

**6. bookbinder has no ffmpeg-shaped story.** It shells out to ffmpeg for silence
and assembly. The slim image installs it, but nothing verifies the version, and
FFmpeg major versions have already bitten this project once.

## Plan

### Phase 1: make the images honest

Rewrite all three to install from `uv.lock` with `uv sync --frozen`, the way the
CI workflow already does. A container must get the same versions CI and the host
get, or it is not reproducing anything.

- Copy `pyproject.toml`, `uv.lock`, `.python-version` first, sync, then copy
  `src/`, so a code change does not re-resolve dependencies.
- Drop the hard-coded entry points. Use `ENTRYPOINT ["uv", "run"]` and pass the
  module as the command, so one image serves every stage of its environment.
- Pin the FFmpeg major version explicitly and record which one.

**Done when:** `docker compose build` succeeds and `just doctor` run inside each
container prints the same versions as the host.

### Phase 2: CPU-only path first

Get the whole pipeline working in containers with no GPU at all, using the
dry-run renderer for synthesis. That validates the mounts, the data contract and
the service wiring without touching CUDA.

- `bookbinder` service: ingest, chunk, dryrun, assemble.
- A compose profile that runs the four in sequence over a sample book.

**Done when:** `docker compose run bookbinder` takes an EPUB to a chaptered m4b
of silence, on a machine with no GPU. This is testable on the Mac, so it is the
part that can be verified before the GPU box is free.

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
