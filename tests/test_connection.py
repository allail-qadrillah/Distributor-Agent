"""Connection management unit tests — fully mocked, no live database."""

import importlib
from contextlib import contextmanager

import pytest
from fastapi import FastAPI

import app.main
from app.config import settings
from app.database import connection
from app.database.connection import get_connection, transaction


class FakeConn:
    """Mimics psycopg transaction semantics for testing."""

    def __init__(self) -> None:
        self.ops: list[str] = []
        self.closed = False

    @contextmanager
    def transaction(self):
        self.ops.append("begin")
        try:
            yield self
            self.ops.append("commit")
        except BaseException:
            self.ops.append("rollback")
            raise

    def close(self) -> None:
        self.closed = True


@pytest.fixture
def fake_connect(monkeypatch):
    calls: dict = {}

    def _connect(*args, **kwargs):
        calls["args"], calls["kwargs"] = args, kwargs
        return FakeConn()

    monkeypatch.setattr(connection.psycopg, "connect", _connect)
    return calls


def test_connection_uses_settings_and_parameterized_kwargs(
    fake_connect, monkeypatch
) -> None:
    url = "postgresql://user:secret@db.example.com:5432/postgres"
    monkeypatch.setattr(settings, "supabase_db_url", url)
    with get_connection() as conn:
        assert isinstance(conn, FakeConn)
    assert fake_connect["args"] == (url,)
    assert fake_connect["kwargs"]["connect_timeout"] == connection.CONNECT_TIMEOUT
    assert fake_connect["kwargs"]["application_name"] == connection.APPLICATION_NAME


def test_connection_closed_after_normal_exit(fake_connect) -> None:
    with get_connection() as conn:
        assert not conn.closed
    assert conn.closed


def test_connection_closed_after_error(fake_connect) -> None:
    with pytest.raises(RuntimeError):
        with get_connection() as conn:
            raise RuntimeError("boom")
    assert conn.closed


def test_transaction_commits_on_success(fake_connect) -> None:
    with transaction() as conn:
        conn.ops.append("work")
    assert conn.ops == ["begin", "work", "commit"]
    assert conn.closed


def test_transaction_rolls_back_and_reraises(fake_connect) -> None:
    with pytest.raises(RuntimeError, match="boom"):
        with transaction() as conn:
            raise RuntimeError("boom")
    assert conn.ops == ["begin", "rollback"]  # no "commit" — and exception re-raised
    assert conn.closed


def test_app_imports_without_database_connection(monkeypatch) -> None:
    """Importing app/main and the connection module must never call connect()."""

    def _fail(*args, **kwargs):
        raise AssertionError("connect() must not be called during import")

    monkeypatch.setattr(connection.psycopg, "connect", _fail)
    importlib.reload(connection)  # module import: no connection opened
    importlib.reload(app.main)  # app import: no connection opened
    assert isinstance(app.main.app, FastAPI)
    # health endpoint still functional without any DB
    from fastapi.testclient import TestClient

    with TestClient(app.main.app) as client:
        assert client.get("/health").json() == {"status": "ok"}
