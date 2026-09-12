#!/bin/sh
# sbxloop bootstrap: one command from a bare host to an initialised home.
#
#   curl -fsSL https://raw.githubusercontent.com/brettbergin/sbxloop/main/scripts/install.sh | sh
#
# Puts everything under $SBXLOOP_HOME (~/.sbxloop): uv in bin/, a uv-managed
# CPython under python/, the venv with sbxloop and its worker, then hands
# over to `sbxloop init --systemd`, which lays out the rest (launchers, sbx,
# config, units). Re-running is safe; every step is idempotent.
#
#   SBXLOOP_HOME=/srv/loop      install somewhere else
#   SBXLOOP_VERSION=1.2.3       pin the release (default: the latest on PyPI)
#   SBXLOOP_INIT_ARGS="--no-systemd --sbx-version 0.38.0"   extra init flags
#
# Installing needs no root: everything lands under $SBXLOOP_HOME, which this
# account owns. Preparing the *host* is a separate, one-time job, and parts of
# it are an administrator's.
#
#   installation prerequisites — this script stops without them:
#     curl, tar, git on PATH
#   host preparation (Linux) — reported here, done by an administrator:
#     e2fsprogs (mkfs.ext4, for sandboxd's block driver)
#     /dev/kvm, openable by this account
#
# Nothing here installs a package, joins a group, or elevates a privilege.
set -eu

SBXLOOP_HOME="${SBXLOOP_HOME:-$HOME/.sbxloop}"
SBXLOOP_VERSION="${SBXLOOP_VERSION:-}"
PYTHON_SERIES="3.13"
EXTRAS="discord,slack"

say() { printf '%s\n' "sbxloop install: $*"; }

for tool in curl tar; do
  command -v "$tool" >/dev/null 2>&1 || { say "$tool is required"; exit 2; }
done

# Git is a *host* prerequisite, separate from the git the sandbox carries:
# importing sbxloop pulls in GitPython, which resolves the git executable at
# import time, so a host without one fails only after this script has
# downloaded an interpreter and every package — and fails with an ImportError
# rather than something an operator can act on. Check it here, before the
# first byte lands. GitPython honours $GIT_PYTHON_GIT_EXECUTABLE over $PATH,
# so that is what gets checked when it is set.
git_advice() {
  case "$(uname -s 2>/dev/null || echo unknown)" in
    Darwin) printf '%s' "install Apple's command line tools (xcode-select --install) or Homebrew's git" ;;
    Linux) printf '%s' "install your distribution's git package (apt-get install git, dnf install git, apk add git, …)" ;;
    *) printf '%s' "install git and put it on PATH" ;;
  esac
}

git_exe="${GIT_PYTHON_GIT_EXECUTABLE:-git}"
if ! command -v "$git_exe" >/dev/null 2>&1; then
  if [ -n "${GIT_PYTHON_GIT_EXECUTABLE:-}" ]; then
    say "GIT_PYTHON_GIT_EXECUTABLE=$git_exe is not an executable; point it at a git binary or unset it to use the one on PATH"
  else
    say "git is required on this host, and sbxloop cannot start without it; $(git_advice)"
  fi
  exit 2
fi
if ! "$git_exe" --version >/dev/null 2>&1; then
  say "git at $(command -v "$git_exe") is installed but not usable — 'git --version' failed; $(git_advice)"
  exit 2
fi

# Host preparation, reported but never performed. Unlike curl, tar and git —
# which this script itself needs, so a host without them cannot be installed
# on — these are the *sandbox backend's* prerequisites, and the remedies are an
# administrator's. Installing without them is still useful: the home lands,
# `sbxloop doctor` diagnoses the host, and `sbxloop init` refuses by name the
# one step that cannot work (the sbx install without mkfs.ext4). So they are
# said here, early, and left to the operator to get done.
#
# Only on Linux: a sandbox is a microVM, so Linux needs the kernel's
# virtualisation device and the tool that formats the sandbox's block devices,
# while macOS brings its own virtualisation and keeps neither. The device is
# probed by opening it, not by reading a group list — a group added in this
# shell is not in this process's credentials until the next login, so
# membership proves nothing about right now.
warn() { printf '%s\n' "sbxloop install: host preparation needed: $*" >&2; }

if [ "$(uname -s 2>/dev/null || echo unknown)" = Linux ]; then
  if [ ! -e /dev/kvm ]; then
    warn "/dev/kvm does not exist, and every sandbox is a microVM that needs it; an administrator enables hardware virtualisation for this machine (in firmware, or as nested virtualisation on a hosted VM) and loads the kvm module"
  elif [ ! -r /dev/kvm ] || [ ! -w /dev/kvm ]; then
    warn "/dev/kvm exists but this account cannot open it for reading and writing; an administrator grants access — Docker's Linux setup documents adding the account to the 'kvm' group — and a group added now takes effect at your next login"
  fi
  # Debian and its derivatives keep /usr/sbin off a non-root PATH, which is
  # where mkfs.ext4 lives; sbxloop adds it back when it runs Docker's
  # installer, so this looks in the same places rather than faulting a host
  # that is in fact ready.
  if ! (PATH="/usr/sbin:/sbin:$PATH"; command -v mkfs.ext4 >/dev/null 2>&1); then
    warn "mkfs.ext4 is not installed, and the sandbox backend formats its block devices with it; an administrator installs the distribution's e2fsprogs package (apt-get install e2fsprogs, dnf install e2fsprogs, apk add e2fsprogs)"
  fi
fi

mkdir -p "$SBXLOOP_HOME/bin" "$SBXLOOP_HOME/cache/uv" "$SBXLOOP_HOME/python" "$SBXLOOP_HOME/tmp"
export UV_INSTALL_DIR="$SBXLOOP_HOME/bin"
export UV_NO_MODIFY_PATH=1
export UV_CACHE_DIR="$SBXLOOP_HOME/cache/uv"
export UV_PYTHON_INSTALL_DIR="$SBXLOOP_HOME/python"
export TMPDIR="$SBXLOOP_HOME/tmp"

uv="$SBXLOOP_HOME/bin/uv"
if [ ! -x "$uv" ]; then
  say "installing uv into $SBXLOOP_HOME/bin"
  curl -LsSf https://astral.sh/uv/install.sh | sh
  [ -x "$uv" ] || { say "uv did not land at $uv"; exit 1; }
fi

say "installing CPython $PYTHON_SERIES under $SBXLOOP_HOME/python"
"$uv" python install "$PYTHON_SERIES"

if [ ! -x "$SBXLOOP_HOME/venv/bin/python" ]; then
  say "creating $SBXLOOP_HOME/venv"
  "$uv" venv --python "$PYTHON_SERIES" "$SBXLOOP_HOME/venv"
fi

if [ -n "$SBXLOOP_VERSION" ]; then
  say "installing sbxloop $SBXLOOP_VERSION"
  "$uv" pip install --python "$SBXLOOP_HOME/venv/bin/python" \
    "sbxloop[$EXTRAS]==$SBXLOOP_VERSION" "sbxloop-worker==$SBXLOOP_VERSION"
else
  say "installing the latest sbxloop"
  "$uv" pip install --upgrade --python "$SBXLOOP_HOME/venv/bin/python" "sbxloop[$EXTRAS]"
fi

say "laying out the home"
# shellcheck disable=SC2086
exec "$SBXLOOP_HOME/venv/bin/sbxloop" init --systemd ${SBXLOOP_INIT_ARGS:-} "$@"
