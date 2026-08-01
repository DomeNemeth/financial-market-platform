"""
Minimal forward-only SQL migration runner.

Why this exists: `docker/postgres/init.sql` is mounted into the Postgres image's
entrypoint, which executes it *only when the data directory is empty*. Editing it
therefore has no effect on any database that already exists — the Phase 1
`volume BIGINT -> NUMERIC(20,6)` change had to be applied by hand-written ALTER,
which is not repeatable and leaves no record of what a given database contains.

Deliberately not Alembic. The schema here is hand-written DDL rather than
SQLAlchemy models, so autogenerate has nothing to work from, and the pieces that
matter — partial indexes, CHECK constraints, a temp-table upsert path — are ones
Alembic would need raw SQL for anyway. What is actually needed is an ordered list
of SQL files and a record of which have run.

Usage:
    .venv\\Scripts\\python.exe -m src.common.migrate          # apply pending
    .venv\\Scripts\\python.exe -m src.common.migrate --status  # show state
"""

import argparse
import hashlib
import logging
import re
from pathlib import Path

from sqlalchemy import text

from src.common.database import engine
from src.common.logging import configure_logging

logger = logging.getLogger(__name__)

MIGRATIONS_DIR = Path(__file__).resolve().parents[2] / "migrations"
FILENAME_RE = re.compile(r"^(\d{4})_[a-z0-9_]+\.sql$")

LEDGER_DDL = """
CREATE TABLE IF NOT EXISTS public.schema_migrations (
    version     VARCHAR(4)   PRIMARY KEY,
    name        TEXT         NOT NULL,
    checksum    CHAR(64)     NOT NULL,
    applied_at  TIMESTAMPTZ  NOT NULL DEFAULT NOW()
)
"""


def discover() -> list[tuple[str, Path]]:
    """Return [(version, path)] sorted by version, rejecting malformed names."""
    if not MIGRATIONS_DIR.is_dir():
        raise FileNotFoundError(f"No migrations directory at {MIGRATIONS_DIR}")

    found: dict[str, Path] = {}
    for path in sorted(MIGRATIONS_DIR.glob("*.sql")):
        match = FILENAME_RE.match(path.name)
        if not match:
            raise ValueError(
                f"Migration filename {path.name!r} must match NNNN_lower_snake.sql"
            )
        version = match.group(1)
        if version in found:
            # Two files claiming one version means apply order is undefined.
            raise ValueError(
                f"Duplicate migration version {version}: "
                f"{found[version].name} and {path.name}"
            )
        found[version] = path

    return sorted(found.items())


def _checksum(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def applied(conn) -> dict[str, str]:
    """Return {version: checksum} for migrations already recorded."""
    rows = conn.execute(text("SELECT version, checksum FROM public.schema_migrations"))
    return {row[0]: row[1] for row in rows}


def migrate(dry_run: bool = False) -> list[str]:
    """Apply every pending migration in version order. Returns versions applied."""
    migrations = discover()

    with engine.begin() as conn:
        conn.execute(text(LEDGER_DDL))
        already = applied(conn)

    # A migration whose content changed after being applied means the database
    # and the repository disagree about what "0002" is. Forward-only migrations
    # have no way to reconcile that, so fail loudly rather than guess.
    for version, path in migrations:
        if version in already and already[version] != _checksum(path):
            raise RuntimeError(
                f"Migration {path.name} was modified after it was applied.\n"
                f"  recorded: {already[version]}\n"
                f"  on disk:  {_checksum(path)}\n"
                "Forward-only migrations are immutable once applied — add a new "
                "migration instead of editing this one."
            )

    pending = [(v, p) for v, p in migrations if v not in already]
    if not pending:
        logger.info(f"Schema up to date at version {migrations[-1][0]} — nothing to apply")
        return []

    if dry_run:
        for version, path in pending:
            logger.info(f"PENDING {version} {path.name}")
        return [v for v, _ in pending]

    done: list[str] = []
    for version, path in pending:
        logger.info(f"Applying {path.name}")
        # One transaction per migration: a failure rolls back that migration
        # entirely and leaves every earlier one committed, so a rerun resumes
        # from exactly the file that failed. Postgres does transactional DDL,
        # so a half-applied migration is not a state that can occur.
        with engine.begin() as conn:
            conn.execute(text(path.read_text(encoding="utf-8")))
            conn.execute(
                text("""
                    INSERT INTO public.schema_migrations (version, name, checksum)
                    VALUES (:version, :name, :checksum)
                """),
                {"version": version, "name": path.name, "checksum": _checksum(path)},
            )
        done.append(version)
        logger.info(f"  applied {version}")

    logger.info(f"Applied {len(done)} migration(s): {', '.join(done)}")
    return done


def status() -> None:
    migrations = discover()
    with engine.begin() as conn:
        conn.execute(text(LEDGER_DDL))
        already = applied(conn)

    for version, path in migrations:
        mark = "applied" if version in already else "PENDING"
        drift = ""
        if version in already and already[version] != _checksum(path):
            drift = "  <-- CHECKSUM MISMATCH"
        logger.info(f"  [{mark:>7}] {path.name}{drift}")


def main() -> None:
    configure_logging()
    parser = argparse.ArgumentParser(description="Apply pending SQL migrations")
    parser.add_argument("--status", action="store_true", help="show state, apply nothing")
    parser.add_argument("--dry-run", action="store_true", help="list what would be applied")
    args = parser.parse_args()

    if args.status:
        status()
    else:
        migrate(dry_run=args.dry_run)


if __name__ == "__main__":
    main()
