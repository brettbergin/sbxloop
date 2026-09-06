"""One-shot Git authentication scoped to the operator's HTTPS authority."""

from __future__ import annotations

import shlex
import sys
from urllib.parse import urlsplit

TOKEN_ENV = "SBXLOOP_GIT_TOKEN"  # nosec B105 - environment variable name
AUTHORITY_ENV = "SBXLOOP_GIT_AUTHORITY"

# Git appends the operation (get/store/erase). Parse credential-protocol
# input as data and answer only get requests for this exact HTTPS host and
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
    if not sep or key in fields:
        sys.exit(0)
    fields[key] = value
host = fields.get('host', '')
try:
    url = urlsplit('https://' + host)
    authority = (url.hostname or '').lower() + ':' + str(url.port or 443)
except ValueError:
    sys.exit(0)
if (fields.get('protocol') == 'https' and url.netloc == host
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
    """Empty scope for non-HTTPS URLs; never authorize a transport downgrade."""
    try:
        url = urlsplit(credential_url)
        if url.scheme != "https" or not url.hostname or url.username or url.password:
            return ""
        return f"{url.hostname.lower()}:{url.port or 443}"
    except ValueError:
        return ""
