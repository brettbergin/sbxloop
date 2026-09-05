#!/bin/sh
# sbx wrapper — written by `sbxloop init`; edits here are overwritten.
#
# Runs the Docker Sandboxes CLI the home installed (sbx/bin/sbx) with the
# same environment the sbxloop launcher sets, so `sbx` on a shell and `sbx`
# under the daemon agree on the keyring, the PATH and the temp directory.
home="$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd -P)"
export SBXLOOP_HOME="$home"
export PATH="$home/bin:/usr/local/bin:/usr/sbin:/sbin:$PATH"
export TMPDIR="$home/tmp"
if [ -z "${DBUS_SESSION_BUS_ADDRESS:-}" ] && [ -S "/run/user/$(id -u)/bus" ]; then
  export DBUS_SESSION_BUS_ADDRESS="unix:path=/run/user/$(id -u)/bus"
fi
if [ ! -x "$home/sbx/bin/sbx" ]; then
  echo "sbx is not installed in $home (expected $home/sbx/bin/sbx); run: sbxloop init" >&2
  exit 127
fi
exec "$home/sbx/bin/sbx" "$@"
