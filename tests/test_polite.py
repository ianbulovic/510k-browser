import itertools
from datetime import time

import httpx
import pytest

from fda510k.polite import Blocked, Disallowed, OutsideWindow, PoliteClient
from fda510k.robots import CrawlPolicy, Rule, VisitWindow
from tests.fakes import FakeClock, RecordingTransport, utc

HOST = "www.accessdata.fda.gov"
SITE = f"https://{HOST}"
WINDOW = VisitWindow(time(3), time(9))


def make(handler, start=None, latency: float = 1.0):
    clock = FakeClock(start or utc(3, 30))
    transport = RecordingTransport(handler, clock, latency)
    polite = PoliteClient(httpx.Client(transport=transport), HOST, WINDOW, clock)
    return polite, transport, clock


def ok(request: httpx.Request) -> httpx.Response:
    return httpx.Response(200, content=b"%PDF-1.4", headers={"content-type": "application/pdf"})


def test_requests_start_at_least_30s_apart() -> None:
    polite, transport, _ = make(ok, latency=4)
    for i in range(4):
        polite.get(f"{SITE}/cdrh_docs/pdf23/K23000{i}.pdf")
    starts = [t for _, t in transport.calls]
    gaps = [(b - a).total_seconds() for a, b in itertools.pairwise(starts)]
    assert gaps == [30, 30, 30]
    assert polite.requests_sent == 4


def test_policy_delay_and_rules_apply() -> None:
    polite, transport, _ = make(ok)
    polite.apply_policy(
        CrawlPolicy(
            rules=(Rule(allow=False, pattern="/scripts/cdrh/cfdocs/cftplc/"),),
            delay=45,
            window=WINDOW,
            concurrent_hits=2,
        )
    )
    polite.get(f"{SITE}/a.pdf")
    polite.get(f"{SITE}/b.pdf")
    assert (transport.calls[1][1] - transport.calls[0][1]).total_seconds() == 45
    with pytest.raises(Disallowed, match=r"robots\.txt"):
        polite.get(f"{SITE}/scripts/cdrh/cfdocs/cftplc/tplc.cfm")
    assert len(transport.calls) == 2


def test_other_hosts_refused() -> None:
    polite, transport, _ = make(ok)
    with pytest.raises(Disallowed, match="not on"):
        polite.get("https://example.com/x")
    assert transport.calls == []


def test_outside_window_sends_nothing() -> None:
    polite, transport, _ = make(ok, start=utc(12))
    with pytest.raises(OutsideWindow):
        polite.get(f"{SITE}/a.pdf")
    assert transport.calls == []


def test_window_checked_at_send_time() -> None:
    polite, transport, clock = make(ok, start=utc(8, 59))
    polite.get(f"{SITE}/a.pdf")
    clock.advance(60)
    with pytest.raises(OutsideWindow):
        polite.get(f"{SITE}/b.pdf")
    assert len(transport.calls) == 1


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(403),
        httpx.Response(429, headers={"retry-after": "3600"}),
        httpx.Response(
            200,
            content=b"<html><title>Access Denied</title>Reference #18.abc</html>",
            headers={"content-type": "text/html"},
        ),
        httpx.Response(
            200,
            content=b"<html>The requested URL was rejected. Please consult...</html>",
            headers={"content-type": "text/html; charset=utf-8"},
        ),
    ],
)
def test_blocked(response: httpx.Response) -> None:
    polite, _, _ = make(lambda _: response)
    with pytest.raises(Blocked) as exc:
        polite.get(f"{SITE}/a.pdf")
    assert exc.value.status == response.status_code


def test_not_found_is_not_blocked() -> None:
    polite, _, _ = make(lambda _: httpx.Response(404, headers={"content-type": "text/html"}))
    assert polite.get(f"{SITE}/a.pdf").response.status_code == 404


def test_redirect_hops_are_paced_and_recorded() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/old.pdf":
            return httpx.Response(301, headers={"location": "/new.pdf"})
        return ok(request)

    polite, transport, _ = make(handler)
    fetched = polite.get(f"{SITE}/old.pdf")
    assert [h.status for h in fetched.hops] == [301, 200]
    assert fetched.redirected
    assert (transport.calls[1][1] - transport.calls[0][1]).total_seconds() == 30


def test_off_host_redirect_not_followed() -> None:
    polite, transport, _ = make(
        lambda _: httpx.Response(302, headers={"location": "https://www.fda.gov/x"})
    )
    fetched = polite.get(f"{SITE}/a.pdf")
    assert fetched.response.status_code == 302
    assert len(transport.calls) == 1
