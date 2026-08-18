"""Rate-limit gates for the three source APIs.

Each API misbehaves differently and a single generic backoff handles none of
them correctly:

PROCORE  600 requests per hour, per client. It does NOT send `Retry-After` on a
         429 - it sends `X-Rate-Limit-Reset`, a Unix epoch. Blind exponential
         backoff therefore either sleeps far too little (and burns the retry
         budget) or far too long. We gate on the remaining-quota header BEFORE
         spending a request we do not have.

QBO      Throttles per realm. Sends `Retry-After` in SECONDS.

HUBSPOT  10 requests/second plus a daily cap. Sends `Retry-After` in
         MILLISECONDS - treating it as seconds sleeps 1000x too long and the
         run appears to hang.

`RateLimitedSession` wraps a requests.Session by composition rather than
subclassing it, so the underlying session keeps its own connection pooling,
retries and adapters.
"""

from __future__ import annotations

import time
from typing import Any, Callable

RETRYABLE_STATUS = frozenset({429, 500, 502, 503, 504})

# Stop this many requests short of the limit, so a retry (and the next run's
# token exchange) still has room to land.
RESERVE = 20
MAX_SLEEP = 2400  # 40 minutes; longer than this and we would rather fail loudly

LIMIT_HEADER = "X-Rate-Limit-Limit"
REMAINING_HEADER = "X-Rate-Limit-Remaining"
RESET_HEADER = "X-Rate-Limit-Reset"


class QuotaExhausted(RuntimeError):
    """Out of quota and unwilling to wait.

    Carries the reset time so the caller can report when a retry becomes
    possible instead of just failing.
    """

    def __init__(self, reset_epoch: float | None, remaining: int | None) -> None:
        self.reset_epoch = reset_epoch
        self.remaining = remaining
        when = (
            time.strftime("%H:%M:%SZ", time.gmtime(reset_epoch))
            if reset_epoch
            else "unknown"
        )
        super().__init__(
            f"Rate limit exhausted (remaining={remaining}). Quota resets at {when}."
        )


def _int_or_none(value: Any) -> int | None:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


def _float_or_none(value: Any) -> float | None:
    try:
        return float(str(value).strip())
    except (TypeError, ValueError):
        return None


def retry_delay(response: Any, attempt: int, header_units: str = "seconds") -> float:
    """How long to wait before retrying a retryable response.

    `header_units` is "seconds" (Procore/QBO) or "milliseconds" (HubSpot).
    Falls back to exponential backoff when the header is absent.
    """
    raw = response.headers.get("Retry-After")
    value = _float_or_none(raw)
    if value is not None:
        return value / 1000.0 if header_units == "milliseconds" else value

    # Procore sends no Retry-After; it sends the reset epoch instead.
    reset = _float_or_none(response.headers.get(RESET_HEADER))
    if reset is not None:
        return max(0.0, reset - time.time()) + 1.0

    return float(2**attempt)


class RateLimitedSession:
    """Quota-aware proxy around a requests.Session."""

    def __init__(
        self,
        session: Any,
        reserve: int = RESERVE,
        wait: bool = True,
        header_units: str = "seconds",
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.session = session
        self.reserve = reserve
        self.wait = wait
        self.header_units = header_units
        self._sleep = sleep
        self.limit: int | None = None
        self.remaining: int | None = None
        self.reset_epoch: float | None = None
        self.requests_made = 0

    # ------------------------------------------------------------ internals

    def _seconds_until_reset(self) -> float:
        if self.reset_epoch is None:
            return 60.0
        return max(0.0, self.reset_epoch - time.time())

    def _gate(self) -> None:
        """Block or raise before spending a request we do not have."""
        if self.remaining is None or self.remaining > self.reserve:
            return
        wait_for = self._seconds_until_reset()
        if not self.wait or wait_for > MAX_SLEEP:
            raise QuotaExhausted(self.reset_epoch, self.remaining)
        if wait_for > 0:
            self._sleep(wait_for + 1)
        # Quota has rolled over; forget what we knew and re-learn from the next
        # response rather than assuming a full bucket.
        self.remaining = None

    def _absorb(self, response: Any) -> None:
        limit = _int_or_none(response.headers.get(LIMIT_HEADER))
        remaining = _int_or_none(response.headers.get(REMAINING_HEADER))
        reset = _float_or_none(response.headers.get(RESET_HEADER))
        if limit is not None:
            self.limit = limit
        if remaining is not None:
            self.remaining = remaining
        if reset is not None:
            self.reset_epoch = reset

    # ------------------------------------------------------------ public

    def get(self, url: str, **kwargs: Any) -> Any:
        self._gate()
        response = self.session.get(url, **kwargs)
        self.requests_made += 1
        self._absorb(response)
        return response

    def post(self, url: str, **kwargs: Any) -> Any:
        """Not gated.

        A token exchange is not rate-limited the same way and must never be
        blocked - a blocked token call cannot even report why it failed. Search
        endpoints that are POST (HubSpot) go through `search` instead.
        """
        return self.session.post(url, **kwargs)

    def search(self, url: str, **kwargs: Any) -> Any:
        """Gated POST, for APIs whose read endpoints are POST (HubSpot search)."""
        self._gate()
        response = self.session.post(url, **kwargs)
        self.requests_made += 1
        self._absorb(response)
        return response


def request_with_retry(
    session: Any,
    url: str,
    headers: dict[str, str],
    params: dict[str, Any] | None = None,
    max_attempts: int = 5,
    header_units: str = "seconds",
    sleep: Callable[[float], None] = time.sleep,
    method: str = "get",
    json_body: Any = None,
) -> Any:
    """GET (or search-POST) with rate-limit and transient-failure handling.

    Raises for status on a non-retryable error, so a 401 surfaces immediately
    rather than being retried five times and reported as a timeout.
    """
    last_response = None
    for attempt in range(max_attempts):
        if method == "get":
            response = session.get(url, headers=headers, params=params, timeout=60)
        else:
            response = session.search(url, headers=headers, json=json_body, timeout=60)

        if response.status_code not in RETRYABLE_STATUS:
            response.raise_for_status()
            return response

        last_response = response
        if attempt == max_attempts - 1:
            break
        sleep(retry_delay(response, attempt, header_units))

    assert last_response is not None
    last_response.raise_for_status()
    raise RuntimeError(f"Exhausted {max_attempts} attempts for {url}")
