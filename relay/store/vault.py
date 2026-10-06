"""Encrypted at-rest storage for a supplied Site password.

The Relay holds a Site password because someone typed it into Telegram. That
password has already travelled through Telegram's servers and every device that
rendered a notification, so keeping another plaintext copy buys very little for
a lot of exposure — especially since the nightly ledger backup is delivered to
the same Telegram chat.

So: encrypt with a key derived from the Telegram bot token. That makes a stolen
ledger (or the backup sitting in the chat) inert without the token, and
rotating the token at @BotFather makes the old ciphertext permanently
unrecoverable rather than merely inconvenient.

This is deliberately not a general secrets manager. One algorithm, one purpose.
"""
import base64
import hashlib
import hmac
import os

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

# Bumped if the scheme ever changes, so old rows are detectable rather than
# silently undecryptable.
VAULT_VERSION = 1

# scrypt parameters, sized to OpenSSL's default limit rather than to taste.
#
# N=2**15 with r=8 wants 256 * N * r = 64MB of memory, and OpenSSL's built-in
# maxmem for scrypt is 32MB by default, so it fails outright with "memory limit
# exceeded". Dropping to N=2**14 asks for 32MB, which fits. The threat model
# does not need more: the attacker holding a stolen ledger has no token, and
# an offline attacker is already guessing a passphrase we would not put here.
# Raising this would mean passing maxmem= explicitly to hashlib.scrypt.
_SCRYPT_N = 1 << 14
_SCRYPT_R = 8
_SCRYPT_P = 1
_KEY_LEN = 32
_SALT = b"relay-credential-vault"   # fixed: the token is the only secret here
_NONCE_LEN = 12


class DecryptionFailed(Exception):
    """Wrong key, tampered ciphertext, or a vault written by another token."""


def _derive(token):
    if not token:
        raise DecryptionFailed("no bot token available to derive a key")
    return hashlib.scrypt(
        token.encode("utf-8"),
        salt=_SALT,
        n=_SCRYPT_N,
        r=_SCRYPT_R,
        p=_SCRYPT_P,
        dklen=_KEY_LEN,
    )


def seal(token, plaintext):
    """Encrypt a string. Returns a storable blob."""
    key = _derive(token)
    nonce = os.urandom(_NONCE_LEN)
    ct = AESGCM(key).encrypt(nonce, plaintext.encode("utf-8"), None)
    return "v%d.%s.%s" % (
        VAULT_VERSION,
        base64.b64encode(nonce).decode(),
        base64.b64encode(ct).decode(),
    )


def open_sealed(token, blob):
    """Decrypt a blob produced by seal(). Raises DecryptionFailed on any problem."""
    if not blob or not isinstance(blob, str):
        raise DecryptionFailed("empty or malformed vault entry")
    try:
        version, nonce_b64, ct_b64 = blob.split(".", 2)
        if int(version.lstrip("v")) != VAULT_VERSION:
            raise DecryptionFailed("vault entry written by a different version")
        key = _derive(token)
        return AESGCM(key).decrypt(
            base64.b64decode(nonce_b64),
            base64.b64decode(ct_b64),
            None,
        ).decode("utf-8")
    except DecryptionFailed:
        raise
    except InvalidTag as err:
        # Wrong key (token rotated) or the bytes were altered. Same response
        # either way: we cannot tell, and saying otherwise would leak whether
        # a guess was close.
        raise DecryptionFailed(
            "cannot decrypt: wrong key or altered data") from err
    except Exception as err:
        raise DecryptionFailed(
            "cannot decrypt: malformed vault entry") from err


def can_open(token, blob):
    try:
        open_sealed(token, blob)
        return True
    except DecryptionFailed:
        return False


def fingerprint(token, username):
    """Non-reversible id for a credential pair, for display and comparison.

    Lets /status show "credentials for <Original Author's username> are present" without decrypting,
    and lets a new token be compared against a stored entry without exposing it.
    """
    h = hmac.new(token.encode("utf-8"), b"relay-fingerprint",
                 hashlib.sha256).digest()
    return hmac.new(h, username.encode("utf-8"), hashlib.sha256).hexdigest()[:16]
