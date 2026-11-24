from collections.abc import Callable
from datetime import UTC, datetime, timedelta

import httpx


class FakeClock:
    """A clock that only moves when someone sleeps (or a request takes time)."""

    def __init__(self, start: datetime) -> None:
        self.current = start
        self._mono = 0.0
        self.slept: list[float] = []

    def now(self) -> datetime:
        return self.current

    def monotonic(self) -> float:
        return self._mono

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.advance(seconds)

    def advance(self, seconds: float) -> None:
        self._mono += seconds
        self.current += timedelta(seconds=seconds)


def utc(hour: int, minute: int = 0) -> datetime:
    return datetime(2026, 11, 25, hour, minute, tzinfo=UTC)


Handler = Callable[[httpx.Request], httpx.Response]


class RecordingTransport(httpx.MockTransport):
    """Records (url, clock time) per request; each request takes `latency` seconds."""

    def __init__(self, handler: Handler, clock: FakeClock, latency: float = 1.0) -> None:
        self.calls: list[tuple[str, datetime]] = []

        def wrapped(request: httpx.Request) -> httpx.Response:
            self.calls.append((str(request.url), clock.now()))
            clock.advance(latency)
            return handler(request)

        super().__init__(wrapped)
