import hashlib
import time
from collections.abc import Iterator
from contextlib import contextmanager

from app.database.connection import connect


class LockAcquisitionTimeoutError(RuntimeError):
    pass


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
