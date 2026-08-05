"""
SCD2 and identity tests for the security master (ADR-0004, ADR-0007).

The property under test is the one that motivates the whole surrogate-key design:
a ticker is a leased, reusable label, so identity must not be anchored to it.
Two distinct failures are covered, and both are silent in a ticker-keyed model:

  1. Ticker CHANGE   — one company, new symbol (FB -> META). A ticker-keyed
                       system fragments one history into two.
  2. Ticker REUSE    — one symbol, two unrelated companies over time. A
                       ticker-keyed system splices two histories into one.

Synthetic FIGIs and ZZ-prefixed tickers are used throughout so nothing here can
collide with, or be confused for, real ingested data. Everything is cleaned up
afterwards.

Requires the Docker stack. Runs `dbt snapshot` as a subprocess.
"""

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
from sqlalchemy import text

from src.common.config import settings
from src.ingestion.security_master import SecurityMasterIngestion

pytestmark = pytest.mark.integration

REPO_ROOT = Path(__file__).resolve().parents[2]
DBT_PROJECT = REPO_ROOT / "dbt"


def _dbt_executable() -> str:
    """
    Locate dbt next to the interpreter running this test.

    This used to be a hard-coded `.venv/Scripts/dbt.exe`, directly under a
    docstring claiming the test was "runnable on any platform" — a path that
    cannot exist on Linux. Nothing caught it for three phases because nothing
    ever ran the suite anywhere but this one Windows machine. CI found it on its
    first run, which is a fair summary of why ADR-0013 exists.

    Console scripts are installed alongside the interpreter, so the sibling of
    `sys.executable` is the dbt belonging to *this* environment — which matters
    more than it looks: ADR-0010 pins dbt to the 1.11 line on Python 3.11, and a
    bare `dbt` off PATH could easily be a different install with a different
    resolution of that pin. PATH is the fallback, not the first choice.
    """
    sibling = Path(sys.executable).parent / ("dbt.exe" if os.name == "nt" else "dbt")
    if sibling.exists():
        return str(sibling)

    found = shutil.which("dbt")
    if not found:
        pytest.fail(
            f"dbt not found next to {sys.executable} or on PATH. "
            f"Install the project's dev dependencies into the active environment."
        )
    return found


#: The dbt profile target. CI points this at its own service container via the
#: `ci` output in dbt/profiles.yml.example; locally it stays `dev`.
DBT_TARGET = os.environ.get("DBT_TARGET", "dev")

# Synthetic, clearly-fake identifiers. Real composite FIGIs start "BBG".
FIGI_ORIGINAL = "TEST00000001"
FIGI_SUCCESSOR = "TEST00000002"
TICKER_BEFORE = "ZZTESTA"
TICKER_AFTER = "ZZTESTB"
TEST_SOURCE = "test_scd2"


def run_dbt_snapshot() -> None:
    """
    Invoke `dbt snapshot` with credentials in the real environment.

    dbt's env_var() reads os.environ, not .env — the same reason
    scripts/dbt.ps1 exists. pytest has already loaded .env via pydantic-settings,
    so the values are pushed back out here rather than shelling through the
    PowerShell wrapper, which keeps this test runnable on any platform.
    """
    env = {
        **os.environ,
        "POSTGRES_HOST": settings.postgres_host,
        "POSTGRES_PORT": str(settings.postgres_port),
        "POSTGRES_USER": settings.postgres_user,
        "POSTGRES_PASSWORD": settings.postgres_password,
        "POSTGRES_DB": settings.postgres_db,
    }
    result = subprocess.run(
        [
            _dbt_executable(), "snapshot",
            "--project-dir", str(DBT_PROJECT),
            "--target", DBT_TARGET,
        ],
        env=env,
        capture_output=True,
        text=True,
        timeout=300,
    )
    if result.returncode != 0:
        pytest.fail(f"dbt snapshot failed:\n{result.stdout}\n{result.stderr}")


def upsert_master_row(conn, security_id: int, ticker: str, figi: str) -> None:
    """Write a security_master row directly, standing in for a vendor refresh."""
    conn.execute(
        text("""
            INSERT INTO raw.security_master
                (security_id, ticker, name, figi, exchange, currency,
                 security_type, active, source)
            VALUES
                (:security_id, :ticker, 'Test Security', :figi, 'XNAS', 'USD',
                 'CS', true, :source)
            ON CONFLICT (security_id, source) DO UPDATE SET
                ticker = EXCLUDED.ticker,
                figi = EXCLUDED.figi,
                ingested_at = NOW()
        """),
        {
            "security_id": security_id,
            "ticker": ticker,
            "figi": figi,
            "source": "polygon",  # the snapshot filters on source = 'polygon'
        },
    )


@pytest.fixture
def clean_test_securities(db_engine):
    """Remove synthetic rows before and after, so reruns are deterministic."""

    def _cleanup():
        with db_engine.connect() as conn:
            conn.execute(
                text("""
                    DELETE FROM snapshots.security_master_snapshot
                    WHERE figi LIKE 'TEST%' OR ticker LIKE 'ZZTEST%'
                """)
            )
            conn.execute(
                text("""
                    DELETE FROM raw.security_master
                    WHERE figi LIKE 'TEST%' OR ticker LIKE 'ZZTEST%'
                """)
            )
            conn.execute(
                text("DELETE FROM raw.security_identity WHERE identity_key LIKE '%TEST%'")
            )
            conn.commit()

    # The snapshot table may not exist on a fresh database; ignore that.
    try:
        _cleanup()
    except Exception:
        pass
    yield
    try:
        _cleanup()
    except Exception:
        pass


# ------------------------------------------------------------------ identity


def test_same_figi_different_ticker_keeps_one_security_id(db_engine, clean_test_securities):
    """
    A rebrand (FB -> META) must NOT create a new security. FIGI is stable across
    ticker changes, which is exactly why it anchors identity rather than the
    ticker does.
    """
    with db_engine.begin() as conn:
        first = SecurityMasterIngestion.resolve_security_id(
            conn, TICKER_BEFORE, FIGI_ORIGINAL, TEST_SOURCE
        )
        second = SecurityMasterIngestion.resolve_security_id(
            conn, TICKER_AFTER, FIGI_ORIGINAL, TEST_SOURCE
        )

    assert first == second, "a ticker change must not mint a new security_id"


def test_same_ticker_different_figi_gets_distinct_security_ids(
    db_engine, clean_test_securities
):
    """
    Ticker REUSE: one delists, the symbol is reassigned to an unrelated company.
    These are two securities and must never share a surrogate key — joining their
    price histories together is the silent corruption this design prevents.
    """
    with db_engine.begin() as conn:
        original = SecurityMasterIngestion.resolve_security_id(
            conn, TICKER_BEFORE, FIGI_ORIGINAL, TEST_SOURCE
        )
        successor = SecurityMasterIngestion.resolve_security_id(
            conn, TICKER_BEFORE, FIGI_SUCCESSOR, TEST_SOURCE
        )

    assert original != successor, "ticker reuse must produce distinct security_ids"


def test_provisional_identity_is_promoted_without_changing_security_id(
    db_engine, clean_test_securities
):
    """
    A security first seen without a FIGI gets a provisional identity. When
    OpenFIGI later resolves, the anchor is rewritten but security_id is not —
    that is what keeps every existing foreign key valid (ADR-0004).
    """
    with db_engine.begin() as conn:
        provisional = SecurityMasterIngestion.resolve_security_id(
            conn, TICKER_BEFORE, None, TEST_SOURCE
        )
        kind_before = conn.execute(
            text("SELECT identity_kind FROM raw.security_identity WHERE security_id = :i"),
            {"i": provisional},
        ).scalar()

        promoted = SecurityMasterIngestion.resolve_security_id(
            conn, TICKER_BEFORE, FIGI_ORIGINAL, TEST_SOURCE
        )
        row = conn.execute(
            text("""
                SELECT identity_kind, identity_key, resolved_at
                FROM raw.security_identity WHERE security_id = :i
            """),
            {"i": promoted},
        ).fetchone()

    assert kind_before == "vendor_ticker"
    assert promoted == provisional, "promotion must preserve the surrogate key"
    assert row[0] == "figi"
    assert row[1] == f"figi:{FIGI_ORIGINAL}"
    assert row[2] is not None, "resolved_at should be stamped on promotion"


def test_resolution_is_idempotent(db_engine, clean_test_securities):
    """Resolving the same security twice must not mint a second identity."""
    with db_engine.begin() as conn:
        first = SecurityMasterIngestion.resolve_security_id(
            conn, TICKER_BEFORE, FIGI_ORIGINAL, TEST_SOURCE
        )
        second = SecurityMasterIngestion.resolve_security_id(
            conn, TICKER_BEFORE, FIGI_ORIGINAL, TEST_SOURCE
        )
        count = conn.execute(
            text("SELECT count(*) FROM raw.security_identity WHERE identity_key LIKE '%TEST%'")
        ).scalar()

    assert first == second
    assert count == 1


# ---------------------------------------------------------------------- SCD2


def test_snapshot_records_ticker_change_as_new_version(db_engine, clean_test_securities):
    """
    The system-time axis. A ticker change produces a SECOND snapshot row: the old
    one closed with dbt_valid_to set, the new one open. The old row is what makes
    "what was this security's ticker on date X?" answerable at all.
    """
    with db_engine.begin() as conn:
        security_id = SecurityMasterIngestion.resolve_security_id(
            conn, TICKER_BEFORE, FIGI_ORIGINAL, TEST_SOURCE
        )
        upsert_master_row(conn, security_id, TICKER_BEFORE, FIGI_ORIGINAL)

    run_dbt_snapshot()

    with db_engine.connect() as conn:
        rows = conn.execute(
            text("""
                SELECT ticker, dbt_valid_from, dbt_valid_to
                FROM snapshots.security_master_snapshot
                WHERE security_id = :i
                ORDER BY dbt_valid_from
            """),
            {"i": security_id},
        ).fetchall()

    assert len(rows) == 1, f"expected one initial version, got {len(rows)}"
    assert rows[0][0] == TICKER_BEFORE
    assert rows[0][2] is None, "the current version must be open-ended"

    # The ticker changes; security_id does not.
    with db_engine.begin() as conn:
        upsert_master_row(conn, security_id, TICKER_AFTER, FIGI_ORIGINAL)

    run_dbt_snapshot()

    with db_engine.connect() as conn:
        rows = conn.execute(
            text("""
                SELECT ticker, dbt_valid_from, dbt_valid_to
                FROM snapshots.security_master_snapshot
                WHERE security_id = :i
                ORDER BY dbt_valid_from
            """),
            {"i": security_id},
        ).fetchall()

    assert len(rows) == 2, f"ticker change should create a second version, got {len(rows)}"

    old, current = rows
    assert old[0] == TICKER_BEFORE
    assert old[2] is not None, "the superseded version must be closed"
    assert current[0] == TICKER_AFTER
    assert current[2] is None, "the current version must be open-ended"
    assert old[2] == current[1], "versions must abut with no gap or overlap"


def test_snapshot_does_not_version_on_reingestion_alone(db_engine, clean_test_securities):
    """
    Re-ingesting unchanged data must NOT create a version. ingested_at advances on
    every run and is deliberately excluded from check_cols — including it would
    make every run look like an attribute change and render the history useless.
    """
    with db_engine.begin() as conn:
        security_id = SecurityMasterIngestion.resolve_security_id(
            conn, TICKER_BEFORE, FIGI_ORIGINAL, TEST_SOURCE
        )
        upsert_master_row(conn, security_id, TICKER_BEFORE, FIGI_ORIGINAL)

    run_dbt_snapshot()
    # Same values, fresh ingested_at — exactly what a no-change re-run looks like.
    with db_engine.begin() as conn:
        upsert_master_row(conn, security_id, TICKER_BEFORE, FIGI_ORIGINAL)
    run_dbt_snapshot()

    with db_engine.connect() as conn:
        count = conn.execute(
            text("""
                SELECT count(*) FROM snapshots.security_master_snapshot
                WHERE security_id = :i
            """),
            {"i": security_id},
        ).scalar()

    assert count == 1, f"re-ingestion with no attribute change created {count} versions"
