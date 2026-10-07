"""Secret storage for OAuth tokens: the OS keychain in the app, memory in tests."""

from __future__ import annotations

import logging
from typing import Protocol

log = logging.getLogger("reelay")

SERVICE = "Reelay"


class TokenStore(Protocol):
    def get(self, key: str) -> str | None: ...
    def set(self, key: str, value: str) -> None: ...
    def delete(self, key: str) -> None: ...


class KeyringTokenStore:
    """macOS Keychain / Windows Credential Manager / Secret Service."""

    def get(self, key: str) -> str | None:
        import keyring
        return keyring.get_password(SERVICE, key)

    def set(self, key: str, value: str) -> None:
        import keyring
        keyring.set_password(SERVICE, key, value)

    def delete(self, key: str) -> None:
        import keyring
        from keyring.errors import PasswordDeleteError
        try:
            keyring.delete_password(SERVICE, key)
        except PasswordDeleteError:
            pass


class MemoryTokenStore:
    def __init__(self) -> None:
        self.data: dict[str, str] = {}

    def get(self, key: str) -> str | None:
        return self.data.get(key)

    def set(self, key: str, value: str) -> None:
        self.data[key] = value

    def delete(self, key: str) -> None:
        self.data.pop(key, None)
