import itertools
import json
from pathlib import Path

import httpx
import pytest

from fda510k import probe
from fda510k.fda_urls import record_url, summary_pdf_url
from fda510k.probe import Outcome, Verdict, find_summary_link
from tests.fakes import FakeClock, RecordingTransport, utc

ROBOTS = (Path(__file__).parent / "fixtures" / "accessdata_robots.txt").read_bytes()
PDF = b"%PDF-1.7\n..."


def record_page(href: str) -> bytes:
    return f"""<html><head><title>510(k) Premarket Notification</title></head><body>
    <table><tr><th>Summary</th><td><a href="{href}">  Summary </a></td></tr>
    <tr><td><a href="/other.pdf">Statement</a></td></tr></table></body></html>""".encode()


def site(overrides: dict[str, httpx.Response] | None = None):
    routes: dict[str, httpx.Response] = {
        probe.ROBOTS_URL: httpx.Response(
            200, content=ROBOTS, headers={"content-type": "text/plain"}
        ),
        record_url(probe.RECORD_SAMPLE) or "": httpx.Response(
            200,
            content=record_page(f"/cdrh_docs/pdf5/{probe.RECORD_SAMPLE}.pdf"),
            headers={"content-type": "text/html; charset=utf-8", "server": "Apache"},
        ),
    }
    for k in probe.PDF_SAMPLES:
        routes[summary_pdf_url(k) or ""] = httpx.Response(
            200, content=PDF, headers={"content-type": "application/pdf"}
        )
    routes.update(overrides or {})

    def handler(request: httpx.Request) -> httpx.Response:
        return routes.get(str(request.url), httpx.Response(404))

    return handler


def run(tmp_path: Path, handler, start=None, wait_hours: float = 0):
    clock = FakeClock(start or utc(3, 5))
    transport = RecordingTransport(handler, clock)
    report = probe.run(tmp_path, httpx.Client(transport=transport), clock, wait_hours)
    return report, transport, clock


def test_reachable(tmp_path: Path) -> None:
    report, transport, _ = run(tmp_path, site())
    assert report.verdict is Verdict.REACHABLE
    assert report.requests_sent == len(transport.calls) == 7
    assert [r.outcome for r in report.requests] == [Outcome.OK] * 7
    assert report.window == "03:00-09:00 UTC"
    assert sorted(p.name for p in (tmp_path / "pdfs").iterdir()) == sorted(
        f"{k}.pdf" for k in probe.PDF_SAMPLES
    )
    record = report.requests[-1]
    assert record.summary_link_matches_convention is True
    assert record.headers["server"] == "Apache"

    saved = json.loads((tmp_path / "report.json").read_text())
    assert saved["verdict"] == "reachable"
    summary = (tmp_path / "summary.md").read_text()
    assert "**Verdict: reachable.**" in summary
    assert "matches convention" in summary


def test_urls_and_spacing(tmp_path: Path) -> None:
    _, transport, _ = run(tmp_path, site())
    urls = [u for u, _ in transport.calls]
    assert urls[0] == probe.ROBOTS_URL
    assert all(u.startswith("https://www.accessdata.fda.gov/") for u in urls)
    starts = [t for _, t in transport.calls]
    assert all((b - a).total_seconds() >= 30 for a, b in itertools.pairwise(starts))


def test_blocked_stops_immediately(tmp_path: Path) -> None:
    first_pdf = summary_pdf_url(probe.PDF_SAMPLES[0]) or ""
    report, transport, _ = run(tmp_path, site({first_pdf: httpx.Response(403)}))
    assert report.verdict is Verdict.BLOCKED
    assert len(transport.calls) == 2
    assert report.requests[-1].outcome is Outcome.BLOCKED
    assert "Blocked at" in (tmp_path / "summary.md").read_text()


def test_wrong_convention_still_reachable(tmp_path: Path) -> None:
    old = summary_pdf_url("K992302") or ""
    record = record_url(probe.RECORD_SAMPLE) or ""
    report, _, _ = run(
        tmp_path,
        site(
            {
                old: httpx.Response(404, headers={"content-type": "text/html"}),
                record: httpx.Response(
                    200,
                    content=record_page("/cdrh_docs/pdf5/K051826.pdf?alt=1"),
                    headers={"content-type": "text/html"},
                ),
            }
        ),
    )
    assert report.verdict is Verdict.REACHABLE
    by_k = {r.k_number: r for r in report.requests if r.kind == "pdf"}
    assert by_k["K992302"].outcome is Outcome.NOT_FOUND
    assert report.requests[-1].summary_link_matches_convention is False


def test_html_instead_of_pdf_is_unexpected(tmp_path: Path) -> None:
    url = summary_pdf_url(probe.PDF_SAMPLES[1]) or ""
    report, _, _ = run(
        tmp_path,
        site(
            {
                url: httpx.Response(
                    200, content=b"<html>hi</html>", headers={"content-type": "text/html"}
                )
            }
        ),
    )
    assert report.requests[2].outcome is Outcome.UNEXPECTED


def test_transport_error_recorded_and_probe_continues(tmp_path: Path) -> None:
    url = summary_pdf_url(probe.PDF_SAMPLES[0]) or ""
    base = site()

    def handler(request: httpx.Request) -> httpx.Response:
        if str(request.url) == url:
            raise httpx.ConnectTimeout("timed out", request=request)
        return base(request)

    report, transport, _ = run(tmp_path, handler)
    assert len(transport.calls) == 7
    assert report.requests[1].outcome is Outcome.ERROR
    assert "ConnectTimeout" in (report.requests[1].error or "")
    assert report.verdict is Verdict.REACHABLE


def test_robots_unparseable_stops(tmp_path: Path) -> None:
    report, transport, _ = run(
        tmp_path,
        site({probe.ROBOTS_URL: httpx.Response(200, content=b"User-agent: *\nDisallow:\n")}),
    )
    assert report.verdict is Verdict.ERROR
    assert "could not be parsed" in report.detail
    assert len(transport.calls) == 1


def test_robots_missing_stops(tmp_path: Path) -> None:
    report, transport, _ = run(tmp_path, site({probe.ROBOTS_URL: httpx.Response(404)}))
    assert report.verdict is Verdict.ERROR
    assert len(transport.calls) == 1


def test_outside_window_sends_nothing(tmp_path: Path) -> None:
    report, transport, _ = run(tmp_path, site(), start=utc(15))
    assert report.verdict is Verdict.REFUSED
    assert transport.calls == []
    assert "Nothing was sent" in report.detail
    assert probe.EXIT_CODES[report.verdict] == 3


@pytest.mark.parametrize(("start_hour", "expected_wait_hours"), [(22, 5 + 1 / 60), (2, 1 + 1 / 60)])
def test_waits_for_window(tmp_path: Path, start_hour: int, expected_wait_hours: float) -> None:
    report, transport, clock = run(tmp_path, site(), start=utc(start_hour), wait_hours=6)
    assert report.verdict is Verdict.REACHABLE
    assert clock.slept[0] == pytest.approx(expected_wait_hours * 3600)
    assert transport.calls[0][1].hour == 3


def test_find_summary_link() -> None:
    html = record_page("/cdrh_docs/pdf5/K051826.pdf").decode()
    base = "https://www.accessdata.fda.gov/scripts/cdrh/cfdocs/cfpmn/pmn.cfm?ID=K051826"
    assert (
        find_summary_link(html, base) == "https://www.accessdata.fda.gov/cdrh_docs/pdf5/K051826.pdf"
    )
    assert find_summary_link("<a href='x.pdf'>Statement</a>", base) is None
