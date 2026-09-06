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
# Nothing here needs root. Prerequisites the host must already have: curl,
# tar, e2fsprogs (mkfs.ext4, for sandboxd's block driver) and /dev/kvm.
set -eu

SBXLOOP_HOME="${SBXLOOP_HOME:-$HOME/.sbxloop}"
SBXLOOP_VERSION="${SBXLOOP_VERSION:-}"
PYTHON_SERIES="3.13"
EXTRAS="discord,slack"

say() { printf '%s\n' "sbxloop install: $*"; }

for tool in curl tar; do
  command -v "$tool" >/dev/null 2>&1 || { say "$tool is required"; exit 2; }
done

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
