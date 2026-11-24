"""Stage `probe` (milestone M1): can this machine fetch from accessdata.fda.gov?

Sends 7 requests, 30+ s apart, only inside the visiting window: robots.txt, five
summary PDFs at their conventional URLs (one per URL era), and one 510(k) record page
whose "Summary" link is checked against the convention. Writes `report.json`,
`summary.md` and the downloaded PDFs to the output directory.
"""

import hashlib
import json
from dataclasses import asdict, dataclass, field
from datetime import timedelta
from enum import StrEnum
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, NoReturn
from urllib.parse import urljoin, urlsplit

import httpx

from fda510k.config import ACCESSDATA_KNOWN_WINDOW, USER_AGENT
from fda510k.fda_urls import ACCESSDATA, record_url, summary_pdf_url
from fda510k.polite import (
    Blocked,
    Clock,
    CrawlRefused,
    Fetched,
    PoliteClient,
    SystemClock,
    html_title,
)
from fda510k.robots import RobotsError, parse

ROBOTS_URL = f"{ACCESSDATA}/robots.txt"
HOST = urlsplit(ACCESSDATA).hostname or ""

# Summaries upstream (wcedmisten/fda-510k-analysis) downloaded and parsed, one per
# URL era: pdf{YY}/ two digits, pdf{Y}/ one digit, and the flat pdf/ directory.
PDF_SAMPLES = ["K231446", "K151778", "K051826", "K012941", "K992302"]
RECORD_SAMPLE = "K051826"

START_DELAY = timedelta(minutes=1)
HEADERS_KEPT = (
    "content-type",
    "content-length",
    "server",
    "cache-control",
    "last-modified",
    "retry-after",
    "via",
    "x-cache",
)


class Verdict(StrEnum):
    REACHABLE = "reachable"
    BLOCKED = "blocked"
    REFUSED = "refused"
    ERROR = "error"


EXIT_CODES = {Verdict.REACHABLE: 0, Verdict.ERROR: 1, Verdict.BLOCKED: 2, Verdict.REFUSED: 3}


class _Stop(Exception):
    """The probe must end now; the verdict is already set."""


class Outcome(StrEnum):
    OK = "ok"
    NOT_FOUND = "not_found"
    UNEXPECTED = "unexpected"
    BLOCKED = "blocked"
    ERROR = "error"


@dataclass
class RequestReport:
    kind: str
    k_number: str | None
    url: str
    outcome: Outcome = Outcome.ERROR
    status: int | None = None
    hops: list[dict[str, Any]] = field(default_factory=list)
    final_url: str | None = None
    headers: dict[str, str] = field(default_factory=dict)
    set_cookie_names: list[str] = field(default_factory=list)
    bytes: int | None = None
    sha256: str | None = None
    starts_with_pdf: bool | None = None
    html_title: str | None = None
    summary_link: str | None = None
    summary_link_matches_convention: bool | None = None
    error: str | None = None


@dataclass
class ProbeReport:
    user_agent: str
    started_at: str
    finished_at: str | None = None
    verdict: Verdict = Verdict.ERROR
    detail: str = ""
    window: str | None = None
    delay_seconds: float | None = None
    concurrent_hits: int | None = None
    requests_sent: int = 0
    requests: list[RequestReport] = field(default_factory=list)


def run(
    out: Path,
    client: httpx.Client | None = None,
    clock: Clock | None = None,
    wait_hours: float = 0,
) -> ProbeReport:
    clock = clock or SystemClock()
    owns_client = client is None
    client = client or httpx.Client(headers={"User-Agent": USER_AGENT}, timeout=60)
    report = ProbeReport(user_agent=USER_AGENT, started_at=clock.now().isoformat())
    try:
        _probe(report, out, client, clock, wait_hours)
    finally:
        if owns_client:
            client.close()
        report.finished_at = clock.now().isoformat()
        out.mkdir(parents=True, exist_ok=True)
        (out / "report.json").write_text(json.dumps(asdict(report), indent=2) + "\n")
        (out / "summary.md").write_text(render_summary(report))
    return report


def _probe(
    report: ProbeReport, out: Path, client: httpx.Client, clock: Clock, wait_hours: float
) -> None:
    # Checked against the last-known window before anything is sent, so not even
    # robots.txt is requested outside it; then against the freshly parsed window.
    now = clock.now()
    if not ACCESSDATA_KNOWN_WINDOW.contains(now):
        opens = ACCESSDATA_KNOWN_WINDOW.next_start(now) + START_DELAY
        wait = opens - now
        if wait > timedelta(hours=wait_hours):
            report.verdict = Verdict.REFUSED
            report.detail = (
                f"{now:%H:%M} UTC is outside {ACCESSDATA_KNOWN_WINDOW}; the window opens at "
                f"{opens:%H:%M} UTC, more than {wait_hours:g} h away. Nothing was sent."
            )
            return
        clock.sleep(wait.total_seconds())

    polite = PoliteClient(client, HOST, ACCESSDATA_KNOWN_WINDOW, clock)
    try:
        _run_requests(report, out, polite)
    except _Stop:
        return
    finally:
        report.requests_sent = polite.requests_sent

    pdf_ok = any(r.kind == "pdf" and r.outcome is Outcome.OK for r in report.requests)
    report.verdict = Verdict.REACHABLE if pdf_ok else Verdict.ERROR
    ok = sum(r.outcome is Outcome.OK for r in report.requests)
    report.detail = f"{ok} of {len(report.requests)} requests succeeded."


def _run_requests(report: ProbeReport, out: Path, polite: PoliteClient) -> None:
    robots = _request(polite, "robots", None, ROBOTS_URL, report)
    if robots is None or robots.response.status_code != 200:
        status = robots.response.status_code if robots else "no response"
        _stop(
            report, Verdict.ERROR, f"robots.txt request failed ({status}); nothing else was sent."
        )
    assert robots is not None
    try:
        policy = parse(robots.response.text, USER_AGENT)
    except RobotsError as exc:
        _stop(report, Verdict.ERROR, f"robots.txt could not be parsed: {exc}")
    polite.apply_policy(policy)
    report.window = str(policy.window)
    report.delay_seconds = polite.delay
    report.concurrent_hits = policy.concurrent_hits

    pdfs = out / "pdfs"
    pdfs.mkdir(parents=True, exist_ok=True)
    for k_number in PDF_SAMPLES:
        fetched = _request(polite, "pdf", k_number, summary_pdf_url(k_number) or "", report)
        if fetched and report.requests[-1].outcome is Outcome.OK:
            (pdfs / f"{k_number}.pdf").write_bytes(fetched.response.content)

    fetched = _request(polite, "record", RECORD_SAMPLE, record_url(RECORD_SAMPLE) or "", report)
    if fetched is not None:
        entry = report.requests[-1]
        link = find_summary_link(fetched.response.text, str(fetched.response.url))
        entry.summary_link = link
        entry.summary_link_matches_convention = (
            link == summary_pdf_url(RECORD_SAMPLE) if link else None
        )
        if link is None and entry.outcome is Outcome.OK:
            entry.outcome = Outcome.UNEXPECTED
            entry.error = "no Summary link found on the record page"


def _stop(report: ProbeReport, verdict: Verdict, detail: str) -> NoReturn:
    report.verdict = verdict
    report.detail = detail
    raise _Stop


def _request(
    polite: PoliteClient, kind: str, k_number: str | None, url: str, report: ProbeReport
) -> Fetched | None:
    """Fetch one URL into the report.

    Returns None if the request failed in transit (recorded, probe continues) and
    raises _Stop if the site refused us or the crawl rules say to stop.
    """
    entry = RequestReport(kind=kind, k_number=k_number, url=url)
    report.requests.append(entry)
    try:
        fetched = polite.get(url)
    except Blocked as exc:
        entry.outcome, entry.status, entry.error = Outcome.BLOCKED, exc.status, exc.reason
        _stop(
            report, Verdict.BLOCKED, f"Blocked at {url}: HTTP {exc.status}, {exc.reason}. Stopped."
        )
    except CrawlRefused as exc:
        entry.error = str(exc)
        _stop(report, Verdict.REFUSED, f"Stopped before {url}: {exc}")
    except httpx.HTTPError as exc:
        entry.error = f"{type(exc).__name__}: {exc}"
        return None

    response = fetched.response
    entry.status = response.status_code
    entry.hops = [
        {
            "url": h.url,
            "status": h.status,
            "started_at": h.started_at.isoformat(),
            "elapsed_seconds": round(h.elapsed_seconds, 3),
        }
        for h in fetched.hops
    ]
    entry.final_url = str(response.url)
    entry.headers = {k: response.headers[k] for k in HEADERS_KEPT if k in response.headers}
    entry.headers.update({k: v for k, v in response.headers.items() if "akamai" in k.lower()})
    entry.set_cookie_names = sorted(
        {c.split("=", 1)[0].strip() for c in response.headers.get_list("set-cookie")}
    )
    entry.bytes = len(response.content)
    entry.sha256 = hashlib.sha256(response.content).hexdigest()
    entry.starts_with_pdf = response.content.startswith(b"%PDF-")
    entry.html_title = html_title(response)
    entry.outcome = _outcome(kind, response, entry.starts_with_pdf)
    return fetched


def _outcome(kind: str, response: httpx.Response, is_pdf: bool) -> Outcome:
    if response.status_code == 404:
        return Outcome.NOT_FOUND
    if response.status_code != 200:
        return Outcome.UNEXPECTED
    content_type = response.headers.get("content-type", "")
    expected = {
        "robots": "text/plain" in content_type,
        "pdf": is_pdf,
        "record": "html" in content_type,
    }
    return Outcome.OK if expected.get(kind, False) else Outcome.UNEXPECTED


class _SummaryLinkFinder(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.href: str | None = None
        self.links: list[str] = []
        self._text: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "a":
            self.href = dict(attrs).get("href")
            self._text = []

    def handle_data(self, data: str) -> None:
        if self.href is not None:
            self._text.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag == "a" and self.href is not None:
            if " ".join("".join(self._text).split()).lower() == "summary":
                self.links.append(self.href)
            self.href = None


def find_summary_link(html: str, base_url: str) -> str | None:
    """The absolute URL of the record page's "Summary" link, if any."""
    finder = _SummaryLinkFinder()
    finder.feed(html)
    return urljoin(base_url, finder.links[0]) if finder.links else None


def render_summary(report: ProbeReport) -> str:
    lines = [
        "# accessdata.fda.gov probe",
        "",
        f"**Verdict: {report.verdict.value}.** {report.detail}",
        "",
        f"- Started {report.started_at}, finished {report.finished_at}",
        f"- User-Agent: `{report.user_agent}`",
        f"- Requests sent: {report.requests_sent}",
    ]
    if report.window:
        lines.append(
            f"- robots.txt: window {report.window}, {report.delay_seconds:g} s between requests, "
            f"{report.concurrent_hits} concurrent allowed (we use 1)"
        )
    if report.requests:
        lines += [
            "",
            "| Request | Status | Outcome | Bytes | Notes |",
            "|---|---|---|---|---|",
        ]
        for r in report.requests:
            notes = [n for n in (r.html_title and f"title: {r.html_title}", r.error) if n]
            if len(r.hops) > 1:
                notes.append("redirected via " + " → ".join(str(h["status"]) for h in r.hops))
            if server := r.headers.get("server"):
                notes.append(f"server: {server}")
            if r.summary_link:
                match = "matches" if r.summary_link_matches_convention else "differs from"
                notes.append(f"Summary link {match} convention: {r.summary_link}")
            label = f"{r.kind} {r.k_number}" if r.k_number else r.kind
            lines.append(
                f"| [{label}]({r.url}) | {r.status or ''} | {r.outcome.value} | "
                f"{r.bytes if r.bytes is not None else ''} | {'; '.join(notes)} |"
            )
    return "\n".join(lines) + "\n"
