"""Reelay AI Client Pairing (Issue #7).

Enables AI clients (Claude Code, Cursor, Muse, local agents) to securely pair with Reelay
via a human-friendly one-time pairing code (e.g. RLY-7X9K-2M4P) rather than requiring
manual token extraction or copying.

Security Model:
- Code format: RLY-XXXX-XXXX using unambiguous characters (no 0/O, 1/I, L).
- Short Time-To-Live (TTL): 5 minutes (300 seconds).
- Rate-limited attempts: 3 failed tries against a live code invalidate it, and wrong codes count too:
  MAX_GLOBAL_FAILURES bad codes within GLOBAL_WINDOW_SECONDS cancel every pending code and refuse
  pairing for LOCKOUT_SECONDS (a guess is by definition a code that doesn't exist).
- Human-in-the-loop: A person generates the pairing code inside Reelay GUI.
- Hashed token storage: Only the SHA-256 hash of issued tokens is stored, never plain text.
- Scoped tokens: Token origin tagged as 'mcp:<client_name>'.
- Revocation: Creator can revoke any paired client at any time.
"""

from __future__ import annotations

import hashlib
import json
import logging
import secrets
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Any

log = logging.getLogger("reelay.pairing")

PAIRING_TTL_SECONDS = 300.0  # 5 minutes
MAX_FAILED_ATTEMPTS = 3
CODE_ALPHABET = "23456789ABCDEFGHJKMNPQRSTUVWXYZ"  # 31 unambiguous characters
MAX_GLOBAL_FAILURES = 5          # wrong codes (any code) within the window before pairing locks
GLOBAL_WINDOW_SECONDS = 300.0
LOCKOUT_SECONDS = 60.0
FAILURES_SETTING = "auth.pairing_failures"
PAIRED_CLIENTS_SETTING = "auth.paired_clients"
PENDING_CODES_SETTING = "auth.pending_pairing_codes"


def generate_pairing_code() -> str:
    """Generates an RLY-XXXX-XXXX pairing code using unambiguous characters."""
    part1 = "".join(secrets.choice(CODE_ALPHABET) for _ in range(4))
    part2 = "".join(secrets.choice(CODE_ALPHABET) for _ in range(4))
    return f"RLY-{part1}-{part2}"


def hash_token(token: str) -> str:
    """Computes SHA-256 hash of a bearer token."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


class PairingManager:
    """Manages active pairing codes and persists authorized client token hashes."""

    def __init__(self, store: Any = None):
        self.store = store

    def _get_pending(self, store: Any = None) -> dict[str, dict[str, Any]]:
        target = store or self.store
        if not target or not hasattr(target, "get_setting"):
            return {}
        try:
            raw = target.get_setting(PENDING_CODES_SETTING, "{}")
            data = json.loads(raw or "{}")
            # Filter expired
            now = time.time()
            valid = {
                k: v for k, v in data.items()
                if v.get("expires_at", 0) > now and v.get("failed_attempts", 0) < MAX_FAILED_ATTEMPTS
            }
            if len(valid) != len(data):
                self._save_pending(valid, target)
            return valid
        except Exception:
            return {}

    def _save_pending(self, data: dict[str, dict[str, Any]], store: Any = None) -> None:
        target = store or self.store
        if target and hasattr(target, "set_setting"):
            try:
                target.set_setting(PENDING_CODES_SETTING, json.dumps(data))
            except Exception as exc:
                log.warning("Could not save pending pairing codes: %s", exc)

    # ---------------------------------------------------------------- global brute-force guard
    def _failures(self, store: Any) -> dict[str, float]:
        try:
            data = json.loads(store.get_setting(FAILURES_SETTING, "{}") or "{}")
            return {k: float(v) for k, v in data.items()}
        except Exception:
            return {}

    def _save_failures(self, data: dict[str, float], store: Any) -> None:
        try:
            store.set_setting(FAILURES_SETTING, json.dumps(data))
        except Exception as exc:
            log.warning("Could not save pairing failure count: %s", exc)

    def locked_out_for(self, store: Any = None) -> float:
        """Seconds until pairing is allowed again after too many wrong codes (0 if not locked)."""
        target = store or self.store
        if not target or not hasattr(target, "get_setting"):
            return 0.0
        return max(0.0, self._failures(target).get("locked_until", 0.0) - time.time())

    def create_pairing_code(self, client_name: str | None = None, store: Any = None) -> tuple[str, float]:
        """Creator clicks 'Pair AI Client…' in GUI.

        Generates and registers an RLY-XXXX-XXXX code.
        Returns (code, ttl_seconds).
        """
        code = generate_pairing_code()
        now = time.time()
        pending = self._get_pending(store)
        pending[code] = {
            "code": code,
            "created_at": now,
            "expires_at": now + PAIRING_TTL_SECONDS,
            "client_name": client_name,
            "failed_attempts": 0,
            "status": "pending",
        }
        self._save_pending(pending, store)
        return code, PAIRING_TTL_SECONDS

    def claim_pairing_code(
        self,
        code: str,
        client_name: str,
        store: Any = None,
    ) -> tuple[bool, str, str | None]:
        """AI client posts code & client_name to POST /v1/pair.

        If valid and not expired, generates a token, saves token hash, and returns
        (True, 'Paired successfully', raw_token).
        """
        clean_code = code.strip().upper()
        target_store = store or self.store
        if not target_store:
            return False, "Reelay store is unavailable.", None

        wait = self.locked_out_for(target_store)
        if wait > 0:
            return False, (f"Too many wrong pairing codes. Try again in about {int(wait) + 1} s with a new code "
                           "from Reelay."), None

        pending = self._get_pending(target_store)
        session = pending.get(clean_code)

        if not session:
            return False, "Invalid or expired pairing code.", None

        now = time.time()
        if session.get("expires_at", 0) <= now or session.get("failed_attempts", 0) >= MAX_FAILED_ATTEMPTS:
            pending.pop(clean_code, None)
            self._save_pending(pending, target_store)
            return False, "Pairing code has expired. Please generate a new code in Reelay.", None

        if session.get("status") != "pending":
            return False, f"Pairing code has already been {session.get('status')}.", None

        # Issue bearer token
        raw_token = "rly_client_" + secrets.token_urlsafe(32)
        thash = hash_token(raw_token)
        client_id = secrets.token_hex(8)
        cname = client_name.strip() or session.get("client_name") or "AI Assistant"

        # Remove from pending; a successful pairing clears the failure count
        pending.pop(clean_code, None)
        self._save_pending(pending, target_store)
        self._save_failures({}, target_store)

        # Save client record with hashed token
        self._save_client_record(target_store, client_id, cname, thash)

        log.info("Paired new client '%s' (ID: %s)", cname, client_id)
        return True, f"Successfully paired with {cname}.", raw_token

    def record_failed_attempt(self, code: str, store: Any = None) -> None:
        """Records an invalid attempt against a pairing code for brute-force defense."""
        clean_code = code.strip().upper()
        target_store = store or self.store
        if not target_store or not hasattr(target_store, "get_setting"):
            return
        pending = self._get_pending(target_store)
        session = pending.get(clean_code)
        if session:
            session["failed_attempts"] = session.get("failed_attempts", 0) + 1
            if session["failed_attempts"] >= MAX_FAILED_ATTEMPTS:
                pending.pop(clean_code, None)
            self._save_pending(pending, target_store)

        # Count every failure, including codes that don't exist (that's what a guess looks like).
        now = time.time()
        failures = self._failures(target_store)
        if now - failures.get("window_start", 0.0) > GLOBAL_WINDOW_SECONDS:
            failures = {"window_start": now, "count": 0.0}
        failures["count"] = failures.get("count", 0.0) + 1
        if failures["count"] >= MAX_GLOBAL_FAILURES:
            failures = {"window_start": now, "count": 0.0, "locked_until": now + LOCKOUT_SECONDS}
            self._save_pending({}, target_store)            # every live code is now void
            log.warning("Pairing locked for %d s after %d wrong codes.", LOCKOUT_SECONDS, MAX_GLOBAL_FAILURES)
        self._save_failures(failures, target_store)

    def _save_client_record(self, store: Any, client_id: str, client_name: str, token_hash: str) -> None:
        try:
            raw = store.get_setting(PAIRED_CLIENTS_SETTING, "[]") if hasattr(store, "get_setting") else "[]"
            clients = json.loads(raw or "[]")
            clients.append({
                "client_id": client_id,
                "client_name": client_name,
                "token_hash": token_hash,
                "created_at": datetime.now(timezone.utc).isoformat(),
                "last_used_at": None,
            })
            if hasattr(store, "set_setting"):
                store.set_setting(PAIRED_CLIENTS_SETTING, json.dumps(clients))
        except Exception as exc:
            log.warning("Could not persist paired client: %s", exc)

    def verify_token(self, token: str, store: Any = None) -> tuple[bool, str | None]:
        """Validates incoming Bearer token against stored token hashes.

        Returns (is_valid, client_name).
        """
        if not token:
            return False, None
        target_store = store or self.store
        if not target_store or not hasattr(target_store, "get_setting"):
            return False, None

        thash = hash_token(token)
        try:
            raw = target_store.get_setting(PAIRED_CLIENTS_SETTING, "[]")
            clients = json.loads(raw or "[]")
            for c in clients:
                stored_hash = c.get("token_hash", "")
                if stored_hash and secrets.compare_digest(stored_hash, thash):
                    # Update last used timestamp
                    c["last_used_at"] = datetime.now(timezone.utc).isoformat()
                    try:
                        target_store.set_setting(PAIRED_CLIENTS_SETTING, json.dumps(clients))
                    except Exception:
                        pass
                    return True, c.get("client_name") or "Paired AI Client"
        except Exception as exc:
            log.warning("Token verification error: %s", exc)
        return False, None

    @staticmethod
    def list_paired_clients(store: Any) -> list[dict[str, Any]]:
        """Returns list of paired clients with token hashes omitted."""
        if not store or not hasattr(store, "get_setting"):
            return []
        try:
            raw = store.get_setting(PAIRED_CLIENTS_SETTING, "[]")
            clients = json.loads(raw or "[]")
            out = []
            for c in clients:
                out.append({
                    "client_id": c.get("client_id"),
                    "client_name": c.get("client_name"),
                    "created_at": c.get("created_at"),
                    "last_used_at": c.get("last_used_at"),
                })
            return out
        except Exception:
            return []

    @staticmethod
    def revoke_client(store: Any, client_id: str) -> bool:
        """Revokes an authorized client by ID immediately."""
        if not store or not hasattr(store, "get_setting") or not hasattr(store, "set_setting"):
            return False
        try:
            raw = store.get_setting(PAIRED_CLIENTS_SETTING, "[]")
            clients = json.loads(raw or "[]")
            initial_count = len(clients)
            filtered = [c for c in clients if c.get("client_id") != client_id]
            if len(filtered) != initial_count:
                store.set_setting(PAIRED_CLIENTS_SETTING, json.dumps(filtered))
                log.info("Revoked paired client %s", client_id)
                return True
        except Exception as exc:
            log.warning("Revocation failed: %s", exc)
        return False
