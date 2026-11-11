"""Read-only database connectivity check.

Usage:
    uv run python scripts/check_db_connection.py

Loads SUPABASE_DB_URL via app.config (pydantic-settings, reads .env).
Connects with Psycopg 3, runs SELECT version(), prints server version.
Read-only: no transaction writes, no DDL, no data changes.

Security: never prints the password or the full connection string —
only host:port/dbname. Raw exception text is suppressed because libpq
errors can embed the DSN.
"""
import re
import sys
from urllib.parse import urlsplit

import psycopg

from app.config import settings

# libpq connection timeout (seconds) — fail fast instead of hanging.
CONNECT_TIMEOUT = 10

# hostnames/IPs only — anything else in the "host" or "port" slot means the
# URL is malformed, and its segments may be credential fragments.
_HOST_RE = re.compile(r"^[A-Za-z0-9._-]+$")


def masked_target(url: str) -> str:
    """host:port/dbname only. No user, no password, no query params.

    Returns a fixed placeholder on malformed URLs so raw segments
    (possibly password fragments) are never echoed.
    """
    try:
        parts = urlsplit(url)
        host = parts.hostname or ""
        port = parts.port or 5432
    except ValueError:
        # non-integer port: almost always a mis-pasted password
        return "malformed-connection-string"
    if not host or not _HOST_RE.match(host):
        return "malformed-connection-string"
    db = (parts.path or "").lstrip("/") or "postgres"
    return f"{host}:{port}/{db}"


def main() -> int:
    url = settings.supabase_db_url
    if not url:
        print("FAIL: SUPABASE_DB_URL is not set.")
        print("Fix: cp .env.example .env and fill SUPABASE_DB_URL.")
        return 1

    target = masked_target(url)
    if target == "malformed-connection-string":
        print("FAIL: SUPABASE_DB_URL is not a valid connection URL (structure).")
        print("Expected: postgresql://user:password@host:port/dbname")
        print("If the password contains special characters (@ : / % $ # etc.),")
        print("percent-encode them, e.g. @ -> %40, : -> %3A, % -> %25, $ -> %24.")
        return 1
    print(f"Connecting to {target} ...")
    try:
        # autocommit=True: single read-only SELECT, no commit on exit,
        # no write possible from this script.
        with psycopg.connect(url, autocommit=True, connect_timeout=CONNECT_TIMEOUT) as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT version()")
                row = cur.fetchone()
        version = row[0] if row else "unknown"
        # print first comma-separated chunk: version number without platform info
        print(f"OK: connected. {version.split(',')[0]}")
        print(f"Target: {target}")
        return 0
    except psycopg.OperationalError:
        print(f"FAIL: could not connect to {target} (OperationalError).")
        print("Check: URL format postgresql://user:password@host:port/db,")
        print("       credentials, Supabase project paused/deleted,")
        print("       network/firewall access to port 5432,")
        print("       Supabase IPv4 add-on if on an IPv6-only network.")
        return 1
    except psycopg.Error:
        # unexpected psycopg error — still no raw text (may contain DSN)
        print(f"FAIL: database error while connecting to {target}.")
        return 1


if __name__ == "__main__":
    sys.exit(main())
