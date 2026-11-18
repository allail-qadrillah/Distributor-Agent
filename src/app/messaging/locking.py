"""Per-conversation serialization for message processing.

Two inbound webhooks for the same phone may race (WhatsApp redelivery,
adapter retries). Business effects must not interleave: both would load
the same transcript and both would run the agent against stale history.

A PostgreSQL session-level advisory lock on a key derived from the phone
serializes processing per conversation while it runs. The lock lives on
its own short-lived connection (autocommit, never an open transaction
while the LLM runs); blocking wait is intentional: the second webhook
waits, then processes with fresh, complete history.

ponytail: the lock key is a 64-bit hash of the phone, so hash collisions
over-serialize unrelated conversations (never under-serialize). Upgrade
to a lock table row (SELECT ... FOR UPDATE) if collisions ever matter.
"""

import hashlib
import time
from collections.abc import Iterator
from contextlib import contextmanager

from app.database.connection import connect


class LockAcquisitionTimeoutError(RuntimeError):
    """The per-phone advisory lock could not be acquired within the
    operator wait budget: an agent run is in flight on that phone."""


@contextmanager
def conversation_lock(phone: str) -> Iterator[None]:
    key = _lock_key(phone)
    conn = connect()
    try:
        conn.autocommit = True  # advisory locks are session-level; no open tx
        with conn.cursor() as cur:
            cur.execute("SELECT pg_advisory_lock(%s)", (key,))
        try:
            yield
        finally:
            with conn.cursor() as cur:
                cur.execute("SELECT pg_advisory_unlock(%s)", (key,))
    finally:
        conn.close()


@contextmanager
def operator_lock(phone: str, wait_seconds: float = 10.0) -> Iterator[None]:
    """Bounded-wait variant for OPERATOR transitions only.

    An inbound webhook waiting behind a running agent flow is correct:
    it then processes with fresh history (conversation_lock). An operator
    HTTP request hanging up to the agent provider's 120s read timeout is
    not. Past the wait budget the caller gets LockAcquisitionTimeoutError
    and the route answers 503 — retry, nothing was mutated.
    """
    key = _lock_key(phone)
    deadline = time.monotonic() + wait_seconds
    conn = connect()
    try:
        conn.autocommit = True
        with conn.cursor() as cur:
            while True:
                cur.execute("SELECT pg_try_advisory_lock(%s) AS ok", (key,))
                if cur.fetchone()["ok"]:
                    break
                if time.monotonic() >= deadline:
                    raise LockAcquisitionTimeoutError(
                        f"conversation lock busy for phone {phone!r}"
                    )
                time.sleep(0.05)
        try:
            yield
        finally:
            with conn.cursor() as cur:
                cur.execute("SELECT pg_advisory_unlock(%s)", (key,))
    finally:
        conn.close()


def _lock_key(phone: str) -> int:
    digest = hashlib.sha256(phone.encode("utf-8")).digest()[:8]
    return int.from_bytes(digest, "big", signed=True)
