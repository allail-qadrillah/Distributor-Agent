"""operator_lock: the bounded-wait advisory lock for operator routes.

I2 was justified by H2: operator transitions now serialize with inbound
processing on the same per-phone lock. An inbound webhook waiting is
correct; an operator HTTP request blocking behind a 120s agent run is
not. Proven here with a scripted fake connection:
- lock free -> acquired immediately, released after the block;
- lock held past the budget -> LockAcquisitionTimeoutError, and the
  failed attempt leaves nothing locked.
"""

import pytest

import app.messaging.locking as locking
from app.messaging.locking import LockAcquisitionTimeoutError, operator_lock


class FakeCursor:
    def __init__(self, conn) -> None:
        self._conn = conn

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, params=()):
        if "pg_try_advisory_lock" in sql:
            self._conn.lock_results.append(self._conn.try_lock_result)
        elif "pg_advisory_unlock" in sql:
            self._conn.unlocked.append(params[0])

    def fetchone(self):
        return {"ok": self._conn.lock_results.pop(0)}


class FakeConn:
    def __init__(self, try_lock_result: bool) -> None:
        self.try_lock_result = try_lock_result
        self.lock_results: list[bool] = []
        self.unlocked: list[int] = []
        self.autocommit = False
        self.closed = False

    def cursor(self):
        return FakeCursor(self)

    def close(self):
        self.closed = True


@pytest.fixture
def _fast_retries(monkeypatch):
    # don't actually sleep between failed attempts
    monkeypatch.setattr(locking.time, "sleep", lambda s: None)


def _patch_connect(monkeypatch, conn):
    monkeypatch.setattr(locking, "connect", lambda: conn)


def test_operator_lock_acquires_when_free(monkeypatch, _fast_retries) -> None:
    conn = FakeConn(try_lock_result=True)
    _patch_connect(monkeypatch, conn)
    with operator_lock("+6281234567890", wait_seconds=1.0):
        assert conn.autocommit is True
    assert len(conn.unlocked) == 1  # released after the block
    assert conn.closed


def test_operator_lock_times_out_when_held(monkeypatch, _fast_retries) -> None:
    conn = FakeConn(try_lock_result=False)  # another session holds the lock
    _patch_connect(monkeypatch, conn)
    with pytest.raises(LockAcquisitionTimeoutError):
        with operator_lock("+6281234567890", wait_seconds=0.0):
            pass
    # the failed attempt unlocked nothing and closed its connection
    assert conn.unlocked == []
    assert conn.closed
