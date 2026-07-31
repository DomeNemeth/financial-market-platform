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
    """

    def __init__(self, flow_name: str, metadata: Optional[dict] = None) -> None:
        self.flow_name = flow_name
        self.metadata = metadata or {}
        self._run_id: Optional[str] = None
        self._rows_ingested: int = 0
        self._conn = None

    def __enter__(self) -> "RunLedger":
        self._conn = engine.connect()
        result = self._conn.execute(
            text("""
                INSERT INTO public.pipeline_runs (flow_name, status, started_at, metadata)
                VALUES (:flow_name, 'RUNNING', :started_at, :metadata::jsonb)
                RETURNING id
            """),
            {
                "flow_name": self.flow_name,
                "started_at": datetime.now(timezone.utc),
                "metadata": json.dumps(self.metadata),
            },
        )
        self._run_id = str(result.fetchone()[0])
        self._conn.commit()
        logger.info(f"Pipeline run started | flow={self.flow_name} | run_id={self._run_id}")
        return self

    def record_rows(self, n: int) -> None:
        """Call this during the run to accumulate row counts."""
        self._rows_ingested += n

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
                        error_message = :error_message
                    WHERE id = :run_id
                """),
                {
                    "status": status,
                    "completed_at": completed_at,
                    "rows_ingested": self._rows_ingested,
                    "error_message": error_message,
                    "run_id": self._run_id,
                },
            )
            self._conn.commit()
        finally:
            self._conn.close()

        # Return False = do NOT suppress exceptions. Always re-raise.
        return False