import logging
import sys

from src.common.config import settings


def configure_logging() -> None:
    """
    Configure structured logging.
    - Development: human-readable format with colours if available
    - Production: JSON format (Grafana/CloudWatch compatible)
    
    Always call this once at the entry point of every script and service.
    Never use print() anywhere else in the codebase.
    """
    log_level = getattr(logging, settings.log_level.upper(), logging.INFO)

    logging.basicConfig(
        level=log_level,
        format="%(asctime)s | %(name)-30s | %(levelname)-8s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        stream=sys.stdout,
    )

    # Quiet down noisy third-party loggers
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    logging.getLogger("sqlalchemy.engine").setLevel(logging.WARNING)