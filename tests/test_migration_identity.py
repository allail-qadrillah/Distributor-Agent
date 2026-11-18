"""The two directories exist because Supabase expects timestamp-prefixed
names in supabase/migrations/ while the app references the shorter names;
the invariant is identical content, not identical names.
"""

import hashlib
from pathlib import Path

ROOT = Path(__file__).parents[1]
SRC_MIGRATIONS = ROOT / "src/app/database/migrations"
SUPABASE_MIGRATIONS = ROOT / "supabase/migrations"


def _content_hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _migration_files(directory: Path) -> dict[str, Path]:
    result: dict[str, Path] = {}
    for p in sorted(directory.glob("*.sql")):
        h = _content_hash(p)
        if h in result:
            raise AssertionError(
                f"Duplicate content hash in {directory}: {p.name} and "
                f"{result[h].name} have the same SHA-256 ({h[:12]}…)"
            )
        result[h] = p
    return result


def test_migrations_identical_between_directories() -> None:
    src = _migration_files(SRC_MIGRATIONS)
    supabase = _migration_files(SUPABASE_MIGRATIONS)

    src_hashes = set(src.keys())
    supabase_hashes = set(supabase.keys())

    only_in_src = src_hashes - supabase_hashes
    only_in_supabase = supabase_hashes - src_hashes

    messages: list[str] = []
    for h in sorted(only_in_src):
        messages.append(
            f"  {src[h].name} (in {SRC_MIGRATIONS}) has no counterpart "
            f"in {SUPABASE_MIGRATIONS}"
        )
    for h in sorted(only_in_supabase):
        messages.append(
            f"  {supabase[h].name} (in {SUPABASE_MIGRATIONS}) has no "
            f"counterpart in {SRC_MIGRATIONS}"
        )

    assert not messages, (
        f"{len(messages)} migration(s) are not mirrored:\n" + "\n".join(messages)
    )
