"""Tests for AI client pairing (Issue #7)."""

from __future__ import annotations

import time
import pytest

from reelay.pairing import (
    CODE_ALPHABET,
    MAX_FAILED_ATTEMPTS,
    PAIRING_TTL_SECONDS,
    PairingManager,
    generate_pairing_code,
    hash_token,
)


class FakeStore:
    def __init__(self):
        self.settings: dict[str, str] = {}

    def get_setting(self, key: str, default: str = "") -> str:
        return self.settings.get(key, default)

    def set_setting(self, key: str, value: str) -> None:
        self.settings[key] = value


def test_pairing_code_format():
    code = generate_pairing_code()
    assert code.startswith("RLY-")
    parts = code.split("-")
    assert len(parts) == 3
    assert len(parts[1]) == 4
    assert len(parts[2]) == 4
    for char in parts[1] + parts[2]:
        assert char in CODE_ALPHABET


def test_pairing_lifecycle_and_token_exchange():
    store = FakeStore()
    mgr = PairingManager(store=store)

    # 1. Creator generates code in GUI
    code, ttl = mgr.create_pairing_code(store=store)
    assert ttl == PAIRING_TTL_SECONDS
    assert code.startswith("RLY-")

    # 2. Client claims code
    ok, msg, token = mgr.claim_pairing_code(code, "Claude Code", store=store)
    assert ok is True
    assert "Claude Code" in msg
    assert token.startswith("rly_client_")

    # 3. Code is single-use: cannot be claimed again
    ok2, msg2, token2 = mgr.claim_pairing_code(code, "Another Client", store=store)
    assert ok2 is False
    assert token2 is None

    # 4. Token verification
    valid, client_name = mgr.verify_token(token, store=store)
    assert valid is True
    assert client_name == "Claude Code"

    # 5. Invalid token is rejected
    valid_bad, _ = mgr.verify_token("invalid_token_123", store=store)
    assert valid_bad is False


def test_pairing_code_rate_limiting_after_failed_attempts():
    store = FakeStore()
    mgr = PairingManager(store=store)

    code, _ = mgr.create_pairing_code(store=store)

    # Record MAX_FAILED_ATTEMPTS
    for _ in range(MAX_FAILED_ATTEMPTS):
        mgr.record_failed_attempt(code, store=store)

    # Now attempting to claim the code fails due to rate-limit / lockout
    ok, msg, token = mgr.claim_pairing_code(code, "Attacker", store=store)
    assert ok is False
    assert token is None


def test_pairing_code_expiration(monkeypatch):
    store = FakeStore()
    mgr = PairingManager(store=store)

    fake_now = [1000.0]
    monkeypatch.setattr(time, "time", lambda: fake_now[0])

    code, _ = mgr.create_pairing_code(store=store)

    # Fast forward beyond TTL
    fake_now[0] += PAIRING_TTL_SECONDS + 10.0

    ok, msg, token = mgr.claim_pairing_code(code, "Late Client", store=store)
    assert ok is False
    assert "expired" in msg.lower()
    assert token is None


def test_client_listing_and_revocation():
    store = FakeStore()
    mgr = PairingManager(store=store)

    code1, _ = mgr.create_pairing_code(store=store)
    _, _, token1 = mgr.claim_pairing_code(code1, "Cursor IDE", store=store)

    code2, _ = mgr.create_pairing_code(store=store)
    _, _, token2 = mgr.claim_pairing_code(code2, "Local Agent", store=store)

    clients = mgr.list_paired_clients(store)
    assert len(clients) == 2
    names = [c["client_name"] for c in clients]
    assert "Cursor IDE" in names
    assert "Local Agent" in names

    # Token hashes must not be exposed in client listings
    for c in clients:
        assert "token" not in c
        assert "token_hash" not in c

    # Revoke Cursor
    cursor_id = [c["client_id"] for c in clients if c["client_name"] == "Cursor IDE"][0]
    revoked = mgr.revoke_client(store, cursor_id)
    assert revoked is True

    # Cursor token no longer valid
    valid1, _ = mgr.verify_token(token1, store=store)
    assert valid1 is False

    # Local Agent token remains valid
    valid2, name2 = mgr.verify_token(token2, store=store)
    assert valid2 is True
    assert name2 == "Local Agent"


def test_wrong_codes_count_globally_and_lock_pairing(monkeypatch):
    """Guessing (codes that don't exist) is limited too: after MAX_GLOBAL_FAILURES wrong codes every
    live code is cancelled and pairing is refused until the lockout passes."""
    from reelay import pairing as pairing_mod

    store = FakeStore()
    mgr = PairingManager(store=store)
    real_code, _ = mgr.create_pairing_code(store=store)

    for i in range(pairing_mod.MAX_GLOBAL_FAILURES):
        guess = f"RLY-AAAA-AA{i:02d}"
        ok, _msg, _tok = mgr.claim_pairing_code(guess, "Attacker", store=store)
        assert ok is False
        mgr.record_failed_attempt(guess, store=store)

    assert mgr.locked_out_for(store) > 0
    ok, msg, token = mgr.claim_pairing_code(real_code, "Attacker", store=store)
    assert ok is False and token is None
    assert "Too many wrong pairing codes" in msg

    # After the lockout passes, the old code is gone; a fresh code works.
    now = time.time()
    monkeypatch.setattr(pairing_mod.time, "time", lambda: now + pairing_mod.LOCKOUT_SECONDS + 1)
    ok_old, _, _ = mgr.claim_pairing_code(real_code, "Late", store=store)
    assert ok_old is False
    fresh, _ = mgr.create_pairing_code(store=store)
    ok_new, _, token_new = mgr.claim_pairing_code(fresh, "Claude Code", store=store)
    assert ok_new is True and token_new.startswith("rly_client_")
