"""
RunLedger behaviour against a real database.

Lived in tests/unit/ until 2026-08-02 despite requiring a live Postgres — every
test here asserts on rows actually written to public.pipeline_runs, so there was
nothing unit about it and `pytest tests/unit` failed whenever Docker was down.
"""

import pytest
from sqlalchemy import text

from src.ingestion.run_ledger import RunLedger

pytestmark = pytest.mark.integration


def test_success_path_writes_correct_row(db_engine):
    """Happy path: context exits cleanly, row shows SUCCESS with correct row count."""
    with RunLedger(flow_name="test_success", metadata={"env": "test"}) as ledger:
        ledger.record_rows(42)
        ledger.record_rows(8)  # record_rows is additive

    with db_engine.connect() as conn:
        row = conn.execute(
            text("SELECT * FROM public.pipeline_runs WHERE flow_name = 'test_success'")
        ).fetchone()

    assert row is not None, "Run should have been recorded"
    assert row.status == "SUCCESS"
    assert row.rows_ingested == 50   # 42 + 8
    assert row.completed_at is not None
    assert row.error_message is None


def test_failure_path_records_error_and_reraises(db_engine):
    """Exception path: exception is recorded AND re-raised (never swallowed)."""
    with pytest.raises(ValueError, match="something went wrong"):
        with RunLedger(flow_name="test_failure"):
            raise ValueError("something went wrong")

    with db_engine.connect() as conn:
        row = conn.execute(
            text("SELECT * FROM public.pipeline_runs WHERE flow_name = 'test_failure'")
        ).fetchone()

    assert row.status == "FAILED"
    assert "something went wrong" in row.error_message
    assert row.completed_at is not None


def test_same_row_is_updated_not_duplicated(db_engine):
    """
    The RUNNING row inserted in __enter__ must be the same row
    updated to SUCCESS in __exit__ — not a second row.
    """
    with RunLedger(flow_name="test_no_duplicate") as ledger:
        ledger.record_rows(1)

    with db_engine.connect() as conn:
        count = conn.execute(
            text(
                "SELECT COUNT(*) FROM public.pipeline_runs "
                "WHERE flow_name = 'test_no_duplicate'"
            )
        ).scalar()

    assert count == 1, "Should be exactly one row, not two"