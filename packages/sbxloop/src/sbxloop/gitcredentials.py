"""One-shot Git authentication scoped to the operator's HTTP(S) origin."""

from __future__ import annotations

import shlex
import sys
from urllib.parse import urlsplit

TOKEN_ENV = "SBXLOOP_GIT_TOKEN"  # nosec B105 - environment variable name
AUTHORITY_ENV = "SBXLOOP_GIT_AUTHORITY"

# Git appends the operation (get/store/erase). Parse credential-protocol
# input as data and answer only get requests for this exact configured scheme, host and
# port. No repository imports or startup hooks can run in this interpreter.
_HELPER = """\
import os, sys
from urllib.parse import urlsplit
if len(sys.argv) != 2 or sys.argv[1] != 'get':
    sys.exit(0)
fields = {}
for line in sys.stdin:
    line = line.rstrip('\\n')
    if not line:
        break
    key, sep, value = line.partition('=')
    if not sep:
        sys.exit(0)
    # Git may repeat array fields (capability[], wwwauth[], state[]).
    # Only routing fields matter here; ambiguity in either fails closed.
    if key in ('protocol', 'host'):
        if key in fields:
            sys.exit(0)
        fields[key] = value
host = fields.get('host', '')
protocol = fields.get('protocol', '')
if protocol not in ('http', 'https'):
    sys.exit(0)
try:
    url = urlsplit(protocol + '://' + host)
    port = url.port or (443 if protocol == 'https' else 80)
    authority = protocol + '://' + (url.hostname or '').lower() + ':' + str(port)
except ValueError:
    sys.exit(0)
if (url.netloc == host
        and not url.username and not url.password and not url.path
        and not url.query and not url.fragment
        and authority == os.environ.get('SBXLOOP_GIT_AUTHORITY')):
    token = os.environ.get('SBXLOOP_GIT_TOKEN', '')
    if token and '\\n' not in token and '\\r' not in token:
        print('username=x-access-token')
        print('password=' + token)
"""

HELPER = f"!{shlex.quote(sys.executable)} -I -S -c {shlex.quote(_HELPER)}"


def authority(credential_url: str) -> str:
    """Bind credentials to an explicit HTTP(S) origin; never follow a downgrade."""
    try:
        url = urlsplit(credential_url)
        if url.scheme not in ("http", "https") or not url.hostname or url.username or url.password:
            return ""
        port = url.port or (443 if url.scheme == "https" else 80)
        return f"{url.scheme}://{url.hostname.lower()}:{port}"
    except ValueError:
        return ""
