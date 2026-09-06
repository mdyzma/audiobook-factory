#!/usr/bin/env bash
# Bootstrap audiobook-factory on macOS or Linux.
#
# Installs the three host tools (uv, just, ffmpeg) through whatever package
# manager the machine has, then builds the three Python environments.
#
#   ./install.sh              install what is missing, then set up
#   ./install.sh --check      report what is missing, change nothing
#   ./install.sh --no-setup   install tools only, skip `just setup`
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT"

CHECK_ONLY=0
RUN_SETUP=1

while [[ $# -gt 0 ]]; do
    case "$1" in
        --check)    CHECK_ONLY=1; shift ;;
        --no-setup) RUN_SETUP=0; shift ;;
        -h|--help)  sed -n '2,9p' "$0" | sed 's/^# \?//'; exit 0 ;;
        *) echo "install.sh: unknown option '$1'" >&2; exit 1 ;;
    esac
done

bold() { printf '\033[1m%s\033[0m\n' "$*"; }
ok()   { printf '  \033[32mok\033[0m      %s\n' "$*"; }
miss() { printf '  \033[33mmissing\033[0m %s\n' "$*"; }
fail() { printf '  \033[31mfailed\033[0m  %s\n' "$*"; }

OS="$(uname -s)"
ARCH="$(uname -m)"

# --- work out how to install things -----------------------------------------
PM=""
case "$OS" in
    Darwin) command -v brew >/dev/null && PM="brew" ;;
    Linux)
        for candidate in apt-get dnf pacman zypper; do
            command -v "$candidate" >/dev/null && { PM="$candidate"; break; }
        done ;;
    *) echo "install.sh: unsupported OS '$OS'. On Windows use install.ps1." >&2; exit 1 ;;
esac

bold "audiobook-factory bootstrap"
echo "  host: $OS $ARCH, package manager: ${PM:-none found}"
echo

# uv and just both publish an official installer script, which is the only way
# to get them on a distro whose repositories do not carry them.
install_uv()   { curl -LsSf https://astral.sh/uv/install.sh | sh; }
install_just() {
    mkdir -p "$HOME/.local/bin"
    curl -LsSf https://just.systems/install.sh | bash -s -- --to "$HOME/.local/bin"
}

install_ffmpeg() {
    case "$PM" in
        brew)    brew install ffmpeg ;;
        apt-get) sudo apt-get update -qq && sudo apt-get install -y ffmpeg ;;
        dnf)     sudo dnf install -y ffmpeg ;;
        pacman)  sudo pacman -S --noconfirm ffmpeg ;;
        zypper)  sudo zypper install -y ffmpeg ;;
        *) return 1 ;;
    esac
}

# --- check, then install what is missing -------------------------------------
MISSING=()
bold "Checking host tools"
for tool in uv just ffmpeg; do
    if command -v "$tool" >/dev/null; then
        ok "$tool ($("$tool" --version 2>&1 | head -1))"
    else
        miss "$tool"
        MISSING+=("$tool")
    fi
done

# bash 4+ is not required, but the scripts do use arrays and [[ ]].
if [[ -n "${BASH_VERSION:-}" ]]; then
    ok "bash $BASH_VERSION"
else
    fail "not running under bash"
fi

if [[ ${#MISSING[@]} -eq 0 ]]; then
    echo
    bold "All host tools present."
else
    echo
    if [[ $CHECK_ONLY -eq 1 ]]; then
        bold "Would install: ${MISSING[*]}"
        exit 1
    fi
    if [[ -z "$PM" && " ${MISSING[*]} " == *" ffmpeg "* ]]; then
        echo "install.sh: no supported package manager found; install ffmpeg yourself." >&2
        exit 1
    fi
    bold "Installing: ${MISSING[*]}"
    for tool in "${MISSING[@]}"; do
        case "$tool" in
            uv)     install_uv ;;
            just)   install_just ;;
            ffmpeg) install_ffmpeg || { fail "ffmpeg; install it manually"; exit 1; } ;;
        esac
    done
    # The official installers drop binaries in ~/.local/bin, which may not be on
    # PATH in this shell yet.
    export PATH="$HOME/.local/bin:$HOME/.cargo/bin:$PATH"
    for tool in "${MISSING[@]}"; do
        command -v "$tool" >/dev/null && ok "$tool" || { fail "$tool still not on PATH"; exit 1; }
    done
    echo
    echo "  note: if a later shell cannot find uv or just, add this to your profile:"
    echo "        export PATH=\"\$HOME/.local/bin:\$PATH\""
fi

if [[ $CHECK_ONLY -eq 1 ]]; then
    echo
    bold "Check only; nothing was changed."
    exit 0
fi

# --- build the three environments -------------------------------------------
if [[ $RUN_SETUP -eq 1 ]]; then
    echo
    bold "Building the three Python environments"
    echo "  This downloads roughly 3 GB and takes a few minutes."
    echo "  uv installs its own Python 3.11.9; nothing is compiled."
    echo
    just setup
    echo
    just doctor
fi

echo
bold "Done."
cat <<'NEXT'
  Next:
    just doctor                      confirm the environments resolved
    bin/audiobook --help             the one-command path
    docs/RUNBOOK.md                  everyday tasks

  On a CUDA machine, read docs/HANDOFF-GPU.md before installing GPU wheels:
  the gpu-torch recipe still points at CUDA 12.4, which does not support
  Blackwell cards such as the RTX 5090.
NEXT
