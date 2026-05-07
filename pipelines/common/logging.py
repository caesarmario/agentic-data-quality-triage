####
## Logging Utility for Agentic Data Quality Triage
## Author: Mario Caesar // hello@caesarmar.io // https://caesarmar.io/
####

# --- Importing Libraries
from __future__ import annotations

import logging
import sys


# --- Defining Constants
LOG_FORMAT          = "%(asctime)s - %(filename)s - Line: %(lineno)d - %(levelname)s - %(message)s"
DEFAULT_LOGGER_NAME = "agentic_dq"


# --- Defining Functions
def configure_logger(name: str = DEFAULT_LOGGER_NAME, level: int = logging.INFO) -> logging.Logger:
    """
    Build a stdout logger for local Docker and Airflow task execution.

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

    formatter      = logging.Formatter(LOG_FORMAT)
    stdout_handler = logging.StreamHandler(sys.stdout)

    stdout_handler.setFormatter(formatter)
    logger.addHandler(stdout_handler)

    logger.info("Logger configured | name=%s level=%s", name, logging.getLevelName(level))
    return logger


# --- Getting Logger
logger = configure_logger()
