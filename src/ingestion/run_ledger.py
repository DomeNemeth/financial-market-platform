import json
import logging
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import text

from src.common.database import engine

logger = logging.getLogger(__name__)


class RunLedger:
    """
    Context manager that records every pipeline run in public.pipeline_runs.

    Usage:
        with RunLedger(flow_name="polygon_ohlcv", metadata={"date": "2024-01-15"}) as ledger:
            rows = do_ingestion()
            ledger.record_rows(rows)
        # Exits cleanly -> status=SUCCESS
        # Raises exception -> status=FAILED, exception re-raised

    Nesting (migration 0006). An orchestrated flow opens a parent ledger and
    passes its run_id to each step, so one flow run produces one parent row and
    a child row per step:

        with RunLedger(flow_name="daily_ingest") as flow:
            with RunLedger(flow_name="polygon_ohlcv", parent_run_id=flow.run_id):
                ...

    parent_run_id stays None for anything started from a CLI. Those runs remain
    first-class — the flow is a convenience over the CLIs, not a replacement,
    and a hand-run backfill must record itself exactly as it always has.
    """

    def __init__(
        self,
        flow_name: str,
        metadata: Optional[dict] = None,
        parent_run_id: Optional[str] = None,
    ) -> None:
        self.flow_name = flow_name
        self.metadata = metadata or {}
        self.parent_run_id = parent_run_id
        self._run_id: Optional[str] = None
        self._rows_ingested: int = 0
        self._conn = None

    @property
    def run_id(self) -> Optional[str]:
        """
        This run's id, for passing to child ledgers. None before __enter__.

        Read-only on purpose: the id is assigned by the database and a setter
        would invite reusing one run's id for another, which the
        ck_pipeline_runs_not_self_parent constraint would then reject at a
        confusing distance from the mistake.
        """
        return self._run_id

    def __enter__(self) -> "RunLedger":
        self._conn = engine.connect()
        result = self._conn.execute(
            # CAST(:param AS jsonb), never :param::jsonb — SQLAlchemy's text()
            # will not bind a parameter immediately followed by a colon. This
            # caused a real bug once.
            text("""
                INSERT INTO public.pipeline_runs
                    (flow_name, status, started_at, metadata, parent_run_id)
                VALUES
                    (:flow_name, 'RUNNING', :started_at, CAST(:metadata AS jsonb),
                     CAST(:parent_run_id AS uuid))
                RETURNING id
            """),
            {
                "flow_name": self.flow_name,
                "started_at": datetime.now(timezone.utc),
                "metadata": json.dumps(self.metadata),
                "parent_run_id": self.parent_run_id,
            },
        )
        self._run_id = str(result.fetchone()[0])
        self._conn.commit()
        logger.info(
            f"Pipeline run started | flow={self.flow_name} | run_id={self._run_id}"
            + (f" | parent={self.parent_run_id}" if self.parent_run_id else "")
        )
        return self

    def record_rows(self, n: int) -> None:
        """Call this during the run to accumulate row counts."""
        self._rows_ingested += n

    def record_metadata(self, **fields) -> None:
        """
        Merge fields into the run's metadata, to be persisted on exit.

        `metadata` is written at __enter__ so that a run which dies hard still
        leaves a record of what it was attempting. But most of what is worth
        recording — a dbt warning count, which sources failed — is only known at
        the end, so __exit__ writes it again.

        Mutating `self.metadata` directly does NOT reach the database; only the
        two writes do. This method exists so that is not something a caller has
        to know.
        """
        self.metadata.update(fields)

    def __exit__(self, exc_type, exc_val, exc_tb) -> bool:
        completed_at = datetime.now(timezone.utc)

        if exc_type is None:
            status = "SUCCESS"
            error_message = None
            logger.info(
                f"Pipeline run succeeded | flow={self.flow_name} "
                f"| rows={self._rows_ingested} | run_id={self._run_id}"
            )
        else:
            status = "FAILED"
            error_message = str(exc_val)[:2000]
            logger.error(
                f"Pipeline run failed | flow={self.flow_name} "
                f"| error={error_message} | run_id={self._run_id}"
            )

        try:
            self._conn.execute(
                text("""
                    UPDATE public.pipeline_runs
                    SET
                        status        = :status,
                        completed_at  = :completed_at,
                        rows_ingested = :rows_ingested,
                        error_message = :error_message,
                        metadata      = CAST(:metadata AS jsonb)
                    WHERE id = :run_id
                """),
                {
                    "status": status,
                    "completed_at": completed_at,
                    "rows_ingested": self._rows_ingested,
                    "error_message": error_message,
                    # Rewritten, because most of what is worth recording is only
                    # known at the end. `default=str` so a date, Decimal or
                    # exception picked up by record_metadata() cannot make the
                    # ledger write itself fail — losing the run record would be
                    # a worse outcome than a stringified value.
                    "metadata": json.dumps(self.metadata, default=str),
                    "run_id": self._run_id,
                },
            )
            self._conn.commit()
        finally:
            self._conn.close()

        # Return False = do NOT suppress exceptions. Always re-raise.
        return False