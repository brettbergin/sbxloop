"""Who a remote request is from.

A client is registered on the host (``sbxloop api client create``) with a
name, a secret shown once, and the capabilities it may exercise. It
exchanges the secret for a short-lived access token (an Ed25519-signed
JWT) and a refresh token; the access token is what every request carries.
Only verifiers are stored — the scrypt hash of a secret, the digest of a
refresh token — never a value that would log a client in.
"""
