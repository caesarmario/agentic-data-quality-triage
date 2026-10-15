####
## Logging Utility for Agentic Data Quality Triage
## Author: Mario Caesar // hello@caesarmar.io // https://caesarmar.io/
####

# --- Importing Libraries
from __future__ import annotations

import logging
import os
import sys


# --- Defining Constants
LOG_FORMAT          = "%(asctime)s - %(filename)s - Line: %(lineno)d - %(levelname)s - %(message)s"
DEFAULT_LOGGER_NAME = "agentic_dq"
LOG_STREAM_ENV      = "AGENT_LOG_STREAM"


# --- Defining Functions
def configure_logger(name: str = DEFAULT_LOGGER_NAME, level: int = logging.INFO) -> logging.Logger:
    """
    Build an operational logger for local Docker and Airflow task execution.

    ``stdout`` remains the default for existing Airflow and Docker logs. Protocol
    processes such as MCP stdio may set ``AGENT_LOG_STREAM=stderr`` so log lines
    cannot corrupt their machine-readable stdout channel.

    Args:
        name: Logger name used by pipeline modules.
        level: Python logging level. Defaults to logging.INFO for operational visibility.

    Returns:
        Configured logging.Logger instance.
    """
    logger = logging.getLogger(name)
    logger.setLevel(level)
    logger.propagate = False

    # Reset handlers so Airflow DAG parsing does not duplicate log lines on every import.
    for handler in list(logger.handlers):
        logger.removeHandler(handler)

    stream_name = os.getenv(LOG_STREAM_ENV, "stdout").strip().lower()
    stream      = sys.stderr if stream_name == "stderr" else sys.stdout
    formatter   = logging.Formatter(LOG_FORMAT)
    handler     = logging.StreamHandler(stream)

    handler.setFormatter(formatter)
    logger.addHandler(handler)

    logger.info("Logger configured | name=%s level=%s", name, logging.getLevelName(level))
    return logger


# --- Getting Logger
logger = configure_logger()
