from datetime import UTC, datetime, time
from pathlib import Path

import pytest

from fda510k.robots import RobotsError, VisitWindow, parse, parse_visiting_hours

ROBOTS = (Path(__file__).parent / "fixtures" / "accessdata_robots.txt").read_text()
UA = "510k-browser/0.1.0 (+https://github.com/example/510k-browser)"
SITE = "https://www.accessdata.fda.gov"


@pytest.fixture(scope="module")
def policy():
    return parse(ROBOTS, UA)


def test_fda_extensions(policy) -> None:
    assert policy.delay == 30
    assert policy.window == VisitWindow(time(3, 0), time(9, 0))
    assert policy.concurrent_hits == 2


@pytest.mark.parametrize(
    "path",
    [
        "/robots.txt",
        "/cdrh_docs/pdf23/K231446.pdf",
        "/cdrh_docs/pdf/K992302.pdf",
        "/scripts/cdrh/cfdocs/cfpmn/pmn.cfm?ID=K051826",
        "/scripts/cdrh/cfdocs/cfpmn/denovo.cfm?ID=DEN200001",
        "/scripts/cdrh/cfdocs/cfpma/pma.cfm?id=P970003",
        # Matching is case-sensitive; FDA lists only some case variants.
        "/Scripts/cdrh/devicesatfda/index.cfm",
    ],
)
def test_allowed(policy, path: str) -> None:
    assert policy.allowed(SITE + path)


@pytest.mark.parametrize(
    "path",
    [
        "/scripts/cdrh/cfdocs/cftplc/tplc.cfm?id=1",
        "/scripts/cdrh/devicesatfda/index.cfm",
        "/SCRIPTs/cdrh/devicesatfda/",
        "/scripts/cdrh/cfdocs/cfpmn/pmnexcel.cfm",  # * wildcard
        "/scripts/cdrh/cfdocs/cfpmn/ExcelExport.CFM",
        "/scripts/search/?q=x",
        "/scripts/cdrh/cfdocs/cfres/textresults.cfm",
    ],
)
def test_disallowed(policy, path: str) -> None:
    assert not policy.allowed(SITE + path)


def test_specific_agent_group_applies() -> None:
    blocked = parse(ROBOTS, "EventMachine HttpClient")
    assert not blocked.allowed(SITE + "/cdrh_docs/pdf23/K231446.pdf")


def test_allow_beats_shorter_disallow_and_dollar_anchor() -> None:
    text = (
        "User-agent: *\nDisallow: /docs/\nAllow: /docs/public/\nDisallow: /*.cfm$\n"
        "Hit-rate: 30\nVisiting-hours: 00:00UTC-23:00UTC\nConcurrent-hits: 1\n"
    )
    p = parse(text, UA)
    assert p.allowed(SITE + "/docs/public/a.pdf")
    assert not p.allowed(SITE + "/docs/private/a.pdf")
    assert not p.allowed(SITE + "/x/y.cfm")
    assert p.allowed(SITE + "/x/y.cfm?id=1")


def test_hit_rate_never_below_floor() -> None:
    p = parse(ROBOTS.replace("Hit-rate: 30", "Hit-rate: 5"), UA)
    assert p.delay == 30
    assert parse(ROBOTS.replace("Hit-rate: 30", "Hit-rate: 45"), UA).delay == 45


@pytest.mark.parametrize(
    ("change", "message"),
    [
        (("Hit-rate: 30", ""), "no Hit-rate"),
        (("Hit-rate: 30", "Hit-rate: soon"), "can't parse Hit-rate"),
        (("Hit-rate: 30", "Hit-rate: 0"), "must be positive"),
        (("Visiting-hours: 23:00EDT-05:00EDT", ""), "no Visiting-hours"),
        (("23:00EDT-05:00EDT", "23:00XYZ-05:00XYZ"), "unknown time zone"),
        (("23:00EDT-05:00EDT", "23:00-05:00"), "no time zone"),
        (("23:00EDT-05:00EDT", "late night"), "can't parse Visiting-hours"),
        (("Concurrent-hits: 2", "Concurrent-hits: 0"), "Concurrent-hits"),
        (("Concurrent-hits: 2", ""), "no Concurrent-hits"),
    ],
)
def test_fails_closed(change: tuple[str, str], message: str) -> None:
    with pytest.raises(RobotsError, match=message):
        parse(ROBOTS.replace(*change), UA)


def test_visiting_hours_conversions() -> None:
    assert parse_visiting_hours("23:00EST-05:00EST") == VisitWindow(time(4), time(10))
    assert parse_visiting_hours("01:30UTC-02:00") == VisitWindow(time(1, 30), time(2))


@pytest.mark.parametrize(
    ("hour", "minute", "inside"),
    [(3, 0, True), (8, 59, True), (9, 0, False), (2, 59, False), (15, 0, False)],
)
def test_window_contains(hour: int, minute: int, inside: bool) -> None:
    window = VisitWindow(time(3), time(9))
    assert window.contains(datetime(2026, 11, 25, hour, minute, tzinfo=UTC)) is inside


def test_window_crossing_midnight() -> None:
    window = VisitWindow(time(22), time(2))
    at = lambda h: datetime(2026, 11, 25, h, tzinfo=UTC)  # noqa: E731
    assert window.contains(at(23)) and window.contains(at(1))
    assert not window.contains(at(2)) and not window.contains(at(12))
    assert window.next_start(at(12)) == at(22)
    assert window.closes_at(at(23)) == datetime(2026, 11, 26, 2, tzinfo=UTC)


def test_next_start_rolls_to_tomorrow() -> None:
    window = VisitWindow(time(3), time(9))
    assert window.next_start(datetime(2026, 11, 25, 10, tzinfo=UTC)) == datetime(
        2026, 11, 26, 3, tzinfo=UTC
    )
