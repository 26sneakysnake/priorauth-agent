"""Application-level encryption of sensitive stored fields (the principle of 45 CFR 164.312(a)(2)(iv)).

Columns typed `EncryptedText` (the record, its conditions, the member id, the result with its quotes) are encrypted
with Fernet (AES-128-CBC + HMAC-SHA256) before they reach the database driver and decrypted when read. The key comes
from PRIORAUTH_FIELD_KEY: one or more comma-separated Fernet keys, the first encrypts and every one decrypts, so a key
can be rotated by putting the new key first (rows written before are still read; re-encryption is not automated).
Without a key nothing is stored: writing raises MissingKey (fail-closed, never cleartext).

Not covered: key management (a KMS or HSM), database or disk encryption, encryption of backups, searchable fields.
Idempotency keys, procedure codes, statuses, costs and the audit log stay in clear: they hold no record content.

    python -m priorauth.security.crypto      # print a new key
"""

import os

from cryptography.fernet import Fernet, InvalidToken, MultiFernet
from sqlalchemy import Dialect, Text
from sqlalchemy.types import TypeDecorator

ENV = "PRIORAUTH_FIELD_KEY"


class MissingKey(RuntimeError):
    pass


class FieldCipher:
    def __init__(self, keys: str) -> None:
        parts = [k.strip() for k in keys.split(",") if k.strip()]
        if not parts:
            raise MissingKey(f"{ENV} holds no key")
        self._fernet = MultiFernet([Fernet(k.encode()) for k in parts])

    def encrypt(self, value: str) -> str:
        return self._fernet.encrypt(value.encode()).decode()

    def decrypt(self, token: str) -> str:
        try:
            return self._fernet.decrypt(token.encode()).decode()
        except InvalidToken as e:  # never echo the stored value
            raise InvalidToken("stored field cannot be decrypted with the configured keys") from e


_cipher: FieldCipher | None = None
_cipher_keys: str | None = None


def cipher() -> FieldCipher:
    """The cipher for the current PRIORAUTH_FIELD_KEY (re-read when the variable changes, e.g. between tests)."""
    global _cipher, _cipher_keys
    keys = os.environ.get(ENV, "")
    if not keys.strip():
        raise MissingKey(f"{ENV} is not set: sensitive fields cannot be stored or read")
    if _cipher is None or keys != _cipher_keys:
        _cipher, _cipher_keys = FieldCipher(keys), keys
    return _cipher


class EncryptedText(TypeDecorator[str]):
    impl = Text
    cache_ok = True

    def process_bind_param(self, value: str | None, dialect: Dialect) -> str | None:
        return None if value is None else cipher().encrypt(value)

    def process_result_value(self, value: str | None, dialect: Dialect) -> str | None:
        return None if value is None else cipher().decrypt(value)


def new_key() -> str:
    return Fernet.generate_key().decode()


if __name__ == "__main__":
    print(new_key())
