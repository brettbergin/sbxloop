#!/bin/sh
# sbxloop launcher — written by `sbxloop init`; edits here are overwritten.
#
# Binds this command to the home it lives in and execs the venv's sbxloop.
# Everything sbxloop does on this host lands under that home. There are no
# secrets in this file: sbxloop reads config/secrets.env itself, and nothing
# is exported to other processes.
home="$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd -P)"
export SBXLOOP_HOME="$home"
# bin/ first so `sbx` resolves to the home's wrapper; sbin because sandboxd's
# block volume driver shells out to mkfs.ext4, which Debian keeps off a
# non-root PATH (the only symptom is "unknown volume driver: block").
export PATH="$home/bin:/usr/local/bin:/usr/sbin:/sbin:$PATH"
export TMPDIR="$home/tmp"
export PIP_CACHE_DIR="$home/cache/pip"
export UV_CACHE_DIR="$home/cache/uv"
export UV_PYTHON_INSTALL_DIR="$home/python"
export COPILOT_CLI_EXTRACT_DIR="$home/cache/copilot-sdk"
# The user bus, for the keyring sbx stores its login in (a bare ssh login
# has no DBUS_SESSION_BUS_ADDRESS; the systemd user manager does).
if [ -z "${DBUS_SESSION_BUS_ADDRESS:-}" ] && [ -S "/run/user/$(id -u)/bus" ]; then
  export DBUS_SESSION_BUS_ADDRESS="unix:path=/run/user/$(id -u)/bus"
fi
exec "$home/venv/bin/sbxloop" "$@"
