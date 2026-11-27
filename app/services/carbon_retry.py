"""Recover read-only carbon computations after transient upstream disconnects."""

import logging
import time
from collections.abc import Callable
from typing import TypeVar

import requests

logger = logging.getLogger(__name__)
T = TypeVar("T")


def _is_rate_limited(exc: Exception) -> bool:
    if getattr(exc, "status_code", None) == 429:
        return True
    message = str(exc).lower()
    return any(token in message for token in (
        "429", "too many requests", "rate limit", "quota exceeded",
        "too many concurrent aggregations",
    ))


def run_carbon_with_retry(operation: Callable[[], T], *, job_id: str) -> T:
    # Retry transient network and Earth Engine throttling failures inside the
    # existing job, without creating duplicate jobs or provenance records.
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
        except Exception as exc:
            if attempt == 2 or not _is_rate_limited(exc):
                raise
            delay = (5, 15)[attempt]
            logger.warning(
                "Carbon job %s was rate limited (%s); retry %d/2 in %ss",
                job_id, type(exc).__name__, attempt + 1, delay,
            )
            time.sleep(delay)
    raise AssertionError("Unreachable retry state")
