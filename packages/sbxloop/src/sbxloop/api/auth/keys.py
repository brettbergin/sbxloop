"""The Ed25519 key access tokens are signed with.

Generated into ``<home>/config/api-signing.key`` (0600) the first time the
listener is enabled or by ``sbxloop api key rotate``, which keeps the key it
replaces as ``api-signing.key.prev`` so tokens minted under it still verify
until they expire. The key id (``kid``) in a token's header is a digest of
the public key, so a verifier picks the right one without trying both.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey

from sbxloop.hostfiles import create_private
from sbxloop.paths import SbxloopHome

KEY_FILE = "api-signing.key"
PREVIOUS_KEY_FILE = "api-signing.key.prev"


def key_id(public: Ed25519PublicKey) -> str:
    raw = public.public_bytes(
        encoding=serialization.Encoding.Raw, format=serialization.PublicFormat.Raw
    )
    return hashlib.sha256(raw).hexdigest()[:8]


@dataclass(frozen=True, slots=True)
class SigningKey:
    kid: str
    private: Ed25519PrivateKey | None
    public: Ed25519PublicKey


@dataclass(frozen=True, slots=True)
class SigningKeys:
    """The key that signs, and the one before it that still verifies."""

    current: SigningKey
    previous: SigningKey | None = None

    def verifier(self, kid: str | None) -> SigningKey | None:
        for key in (self.current, self.previous):
            if key is not None and key.kid == kid:
                return key
        return None


def _pem(private: Ed25519PrivateKey) -> bytes:
    return private.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )


def _read(path: Path) -> SigningKey:
    loaded = serialization.load_pem_private_key(path.read_bytes(), password=None)
    if not isinstance(loaded, Ed25519PrivateKey):
        raise ValueError(f"{path} is not an Ed25519 private key")
    return SigningKey(kid=key_id(loaded.public_key()), private=loaded, public=loaded.public_key())


def _write(path: Path, private: Ed25519PrivateKey) -> None:
    create_private(path)
    path.write_bytes(_pem(private))


def key_paths(home: SbxloopHome) -> tuple[Path, Path]:
    return home.config / KEY_FILE, home.config / PREVIOUS_KEY_FILE


def load_or_create(home: SbxloopHome) -> SigningKeys:
    """The keys on disk, generating the current one when there is none."""
    current_path, previous_path = key_paths(home)
    if not current_path.exists():
        current_path.parent.mkdir(parents=True, exist_ok=True)
        _write(current_path, Ed25519PrivateKey.generate())
    current = _read(current_path)
    previous = _read(previous_path) if previous_path.exists() else None
    return SigningKeys(current=current, previous=previous)


def rotate(home: SbxloopHome) -> SigningKeys:
    """Replace the signing key, keeping the old one to verify what it
    signed; the one before that is gone (its tokens have long expired)."""
    current_path, previous_path = key_paths(home)
    if current_path.exists():
        previous_path.unlink(missing_ok=True)
        current_path.replace(previous_path)
    _write(current_path, Ed25519PrivateKey.generate())
    return load_or_create(home)
