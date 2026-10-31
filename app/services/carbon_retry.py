"""Recover read-only carbon computations after transient upstream disconnects."""

import logging
import time
from collections.abc import Callable
from typing import TypeVar

import requests

logger = logging.getLogger(__name__)
T = TypeVar("T")


def run_carbon_with_retry(operation: Callable[[], T], *, job_id: str) -> T:
    # Repeat computation inside the existing job, so retries do not create
    # additional jobs, provenance records, or frontend requests.
    for attempt in range(3):
        try:
            return operation()
        except requests.exceptions.SSLError:
            raise
        except (requests.ConnectionError, requests.Timeout) as exc:
            if attempt == 2:
                raise
            delay = 2 ** attempt
            # Exception messages can include credential URLs; log only the type.
            logger.warning(
                "Carbon job %s transient %s; retry %d/2 in %ss",
                job_id, type(exc).__name__, attempt + 1, delay,
            )
            time.sleep(delay)
    raise AssertionError("Unreachable retry state")
