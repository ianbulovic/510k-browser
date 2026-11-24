"""The only way this project sends requests to accessdata.fda.gov.

Every request goes through `PoliteClient.get`, which enforces, in order:
1. the URL is on the crawl host and allowed by robots.txt,
2. the visiting window is open,
3. at least `delay` seconds have passed since the previous request started,
and then raises `Blocked` on anything that looks like the site refusing us, so callers
stop instead of retrying. Requests are strictly sequential, and redirects are followed
by hand so each hop is checked and paced like any other request.
"""

import re
import time as time_module
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Protocol
from urllib.parse import urljoin, urlsplit

import httpx

from fda510k.robots import MIN_DELAY_SECONDS, CrawlPolicy, VisitWindow

MAX_REDIRECTS = 5
BLOCK_STATUSES = {403, 429}
_BLOCK_MARKERS = (
    "access denied",
    "errors.edgesuite.net",
    "the requested url was rejected",
    "request rejected",
)
_TITLE = re.compile(r"<title[^>]*>(.*?)</title>", re.IGNORECASE | re.DOTALL)


class Clock(Protocol):
    def now(self) -> datetime: ...
    def monotonic(self) -> float: ...
    def sleep(self, seconds: float) -> None: ...


class SystemClock:
    def now(self) -> datetime:
        return datetime.now(UTC)

    def monotonic(self) -> float:
        return time_module.monotonic()

    def sleep(self, seconds: float) -> None:
        time_module.sleep(seconds)


class CrawlRefused(Exception):
    """A request was not sent because it would break the crawl rules."""


class Disallowed(CrawlRefused):
    pass


class OutsideWindow(CrawlRefused):
    pass


class Blocked(Exception):
    """The site refused us (403/429 or a block page). Stop; do not retry."""

    def __init__(self, url: str, status: int, reason: str) -> None:
        super().__init__(f"{url}: HTTP {status}, {reason}")
        self.url = url
        self.status = status
        self.reason = reason


@dataclass
class Hop:
    url: str
    status: int
    started_at: datetime
    elapsed_seconds: float


@dataclass
class Fetched:
    """A final response plus every hop taken to reach it."""

    response: httpx.Response
    hops: list[Hop] = field(default_factory=list)

    @property
    def redirected(self) -> bool:
        return len(self.hops) > 1


class PoliteClient:
    def __init__(
        self,
        client: httpx.Client,
        host: str,
        window: VisitWindow,
        clock: Clock | None = None,
        delay: float = MIN_DELAY_SECONDS,
    ) -> None:
        self._client = client
        self._host = host
        self._window = window
        self._delay = max(delay, MIN_DELAY_SECONDS)
        self._policy: CrawlPolicy | None = None
        self._clock = clock or SystemClock()
        self._last_start: float | None = None
        self.requests_sent = 0

    @property
    def window(self) -> VisitWindow:
        return self._window

    @property
    def delay(self) -> float:
        return self._delay

    def apply_policy(self, policy: CrawlPolicy) -> None:
        """Adopt freshly parsed robots.txt rules. The delay never drops below the floor."""
        self._policy = policy
        self._window = policy.window
        self._delay = max(policy.delay, MIN_DELAY_SECONDS)

    def get(self, url: str) -> Fetched:
        response, hop = self._send(url)
        fetched = Fetched(response=response, hops=[hop])
        while fetched.response.is_redirect and len(fetched.hops) <= MAX_REDIRECTS:
            location = fetched.response.headers.get("location")
            if not location:
                break
            target = urljoin(str(fetched.response.url), location)
            if urlsplit(target).hostname != self._host:
                break
            fetched.response, hop = self._send(target)
            fetched.hops.append(hop)
        return fetched

    def _send(self, url: str) -> tuple[httpx.Response, Hop]:
        if urlsplit(url).hostname != self._host:
            raise Disallowed(f"{url} is not on {self._host}")
        if self._policy is not None and not self._policy.allowed(url):
            raise Disallowed(f"{url} is disallowed by robots.txt")

        if self._last_start is not None:
            wait = self._last_start + self._delay - self._clock.monotonic()
            if wait > 0:
                self._clock.sleep(wait)
        started_at = self._clock.now()
        if not self._window.contains(started_at):
            raise OutsideWindow(f"{started_at:%H:%M} UTC is outside {self._window}")

        self._last_start = self._clock.monotonic()
        self.requests_sent += 1
        response = self._client.get(url, follow_redirects=False)
        response.read()
        hop = Hop(
            url=url,
            status=response.status_code,
            started_at=started_at,
            elapsed_seconds=self._clock.monotonic() - self._last_start,
        )
        if reason := block_reason(response):
            raise Blocked(url, response.status_code, reason)
        return response, hop


def block_reason(response: httpx.Response) -> str | None:
    if response.status_code in BLOCK_STATUSES:
        retry_after = response.headers.get("retry-after")
        return f"refused (Retry-After: {retry_after})" if retry_after else "refused"
    if "html" in response.headers.get("content-type", ""):
        head = response.content[:20_000].decode("utf-8", "replace").lower()
        for marker in _BLOCK_MARKERS:
            if marker in head:
                return f"block page ({marker!r})"
    return None


def html_title(response: httpx.Response) -> str | None:
    if "html" not in response.headers.get("content-type", ""):
        return None
    m = _TITLE.search(response.content[:50_000].decode("utf-8", "replace"))
    return " ".join(m[1].split()) if m else None
