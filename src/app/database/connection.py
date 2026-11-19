"""ponytail: when concurrency justifies it, add psycopg[pool] and swap
get_connection() for ConnectionPool.connection().
"""

from collections.abc import Iterator
from contextlib import contextmanager

import psycopg
from psycopg.rows import dict_row

from app.config import settings

CONNECT_TIMEOUT = 10
APPLICATION_NAME = "distributor-agent"


def connect() -> psycopg.Connection:
    return psycopg.connect(
        settings.supabase_db_url,
        connect_timeout=CONNECT_TIMEOUT,
        application_name=APPLICATION_NAME,
        row_factory=dict_row,
    )


@contextmanager
def get_connection() -> Iterator[psycopg.Connection]:
    conn = connect()
    try:
        yield conn
    finally:
        conn.close()


@contextmanager
def transaction() -> Iterator[psycopg.Connection]:
    with get_connection() as conn:
        with conn.transaction():
            yield conn
