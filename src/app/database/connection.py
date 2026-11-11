"""PostgreSQL connection management (Psycopg 3, sync).

Design v0.1:
- Connection per operation via context managers: explicit lifecycle
  (open -> yield -> close), no module-level or import-time connections.
  Importing this module (or the app) never opens a database connection.
- No connection pool: psycopg-pool is not in the dependencies and v0.1
  load does not justify it.
  ponytail: when concurrency justifies it, add the psycopg[pool] extra
  and swap get_connection() for ConnectionPool.connection().
- Secrets: the DSN comes from settings only; this module never logs it
  and never embeds credentials in code.

Transaction semantics: transaction() commits on success, rolls back and
re-raises on failure. Exceptions are never swallowed.
"""
from collections.abc import Iterator
from contextlib import contextmanager

import psycopg

from app.config import settings

CONNECT_TIMEOUT = 10  # seconds — fail fast instead of hanging
APPLICATION_NAME = "distributor-agent"  # visible in Supabase pg_stat_activity


def connect() -> psycopg.Connection:
    """Open a new connection from application settings.

    Parameterized configuration (kwargs), DSN from settings only.
    """
    return psycopg.connect(
        settings.supabase_db_url,
        connect_timeout=CONNECT_TIMEOUT,
        application_name=APPLICATION_NAME,
    )


@contextmanager
def get_connection() -> Iterator[psycopg.Connection]:
    """Connection lifecycle: open -> yield -> close, even on error.

    Transaction handling is the caller's responsibility (use transaction()
    for write paths).
    """
    conn = connect()
    try:
        yield conn
    finally:
        conn.close()


@contextmanager
def transaction() -> Iterator[psycopg.Connection]:
    """Explicit transaction boundary.

    psycopg's conn.transaction(): BEGIN on enter, COMMIT on clean exit,
    ROLLBACK on exception. The exception is re-raised — never swallowed.
    The connection is always closed afterwards.
    """
    with get_connection() as conn:
        with conn.transaction():
            yield conn
