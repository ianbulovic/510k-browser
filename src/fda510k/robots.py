"""robots.txt parsing, including accessdata.fda.gov's non-standard crawl directives.

Beyond `User-agent`/`Allow`/`Disallow` (with Google-style `*` and `$` wildcards, which
`urllib.robotparser` does not support), FDA's file sets site-wide limits:

    Hit-rate: 30                         # seconds between request starts
    Visiting-hours: 23:00EDT-05:00EDT    # when crawling is allowed
    Concurrent-hits: 2                   # simultaneous requests

Those three are required: a file without them, or with values that can't be parsed,
raises RobotsError instead of falling back to defaults.
"""

import re
from dataclasses import dataclass
from datetime import UTC, datetime, time, timedelta
from functools import cache
from urllib.parse import urlsplit

MIN_DELAY_SECONDS = 30.0

# Fixed offsets: "EDT" in FDA's file is read literally as UTC-4 all year.
TZ_OFFSETS_HOURS = {
    "UTC": 0,
    "GMT": 0,
    "Z": 0,
    "EST": -5,
    "EDT": -4,
    "CST": -6,
    "CDT": -5,
    "MST": -7,
    "MDT": -6,
    "PST": -8,
    "PDT": -7,
}

_VISITING_HOURS = re.compile(
    r"^(\d{1,2}):(\d{2})\s*([A-Za-z]{1,4})?\s*-\s*(\d{1,2}):(\d{2})\s*([A-Za-z]{1,4})?$"
)


class RobotsError(ValueError):
    pass


@dataclass(frozen=True)
class VisitWindow:
    """A daily UTC window, possibly crossing midnight (start > end)."""

    start: time
    end: time

    def contains(self, moment: datetime) -> bool:
        t = moment.astimezone(UTC).time().replace(tzinfo=None)
        if self.start <= self.end:
            return self.start <= t < self.end
        return t >= self.start or t < self.end

    def next_start(self, moment: datetime) -> datetime:
        """The next time the window opens at or after `moment` (`moment` itself if open)."""
        moment = moment.astimezone(UTC)
        if self.contains(moment):
            return moment
        candidate = datetime.combine(moment.date(), self.start, tzinfo=UTC)
        return candidate if candidate > moment else candidate + timedelta(days=1)

    def closes_at(self, moment: datetime) -> datetime:
        """When the window containing `moment` closes. Only meaningful if it is open."""
        moment = moment.astimezone(UTC)
        candidate = datetime.combine(moment.date(), self.end, tzinfo=UTC)
        return candidate if candidate > moment else candidate + timedelta(days=1)

    def __str__(self) -> str:
        return f"{self.start:%H:%M}-{self.end:%H:%M} UTC"


@dataclass(frozen=True)
class Rule:
    allow: bool
    pattern: str

    def matches(self, path: str) -> bool:
        return _pattern_regex(self.pattern).match(path) is not None


@dataclass(frozen=True)
class CrawlPolicy:
    rules: tuple[Rule, ...]
    delay: float
    window: VisitWindow
    concurrent_hits: int

    def allowed(self, url: str) -> bool:
        """Longest matching rule wins; Allow wins ties. Matching is case-sensitive."""
        parts = urlsplit(url)
        path = (parts.path or "/") + (f"?{parts.query}" if parts.query else "")
        best: Rule | None = None
        for rule in self.rules:
            if not rule.pattern or not rule.matches(path):
                continue
            if (
                best is None
                or len(rule.pattern) > len(best.pattern)
                or (len(rule.pattern) == len(best.pattern) and rule.allow)
            ):
                best = rule
        return best is None or best.allow


def parse(text: str, user_agent: str) -> CrawlPolicy:
    groups: list[tuple[list[str], list[Rule]]] = []
    extensions: dict[str, str] = {}
    current: tuple[list[str], list[Rule]] | None = None
    last_was_agent = False

    for raw in text.splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line or ":" not in line:
            continue
        field, value = (s.strip() for s in line.split(":", 1))
        field = field.lower()
        if field == "user-agent":
            if current is None or not last_was_agent:
                current = ([], [])
                groups.append(current)
            current[0].append(value.lower())
            last_was_agent = True
            continue
        last_was_agent = False
        if field in ("allow", "disallow"):
            if current is not None:
                current[1].append(Rule(allow=field == "allow", pattern=value))
        elif field in ("hit-rate", "visiting-hours", "concurrent-hits"):
            extensions[field] = value

    return CrawlPolicy(
        rules=tuple(_rules_for(groups, user_agent)),
        delay=max(_parse_hit_rate(extensions.get("hit-rate")), MIN_DELAY_SECONDS),
        window=parse_visiting_hours(extensions.get("visiting-hours")),
        concurrent_hits=_parse_concurrent_hits(extensions.get("concurrent-hits")),
    )


def parse_visiting_hours(value: str | None) -> VisitWindow:
    if value is None:
        raise RobotsError("robots.txt has no Visiting-hours directive")
    m = _VISITING_HOURS.match(value.strip())
    if not m:
        raise RobotsError(f"can't parse Visiting-hours: {value!r}")
    start = _to_utc(int(m[1]), int(m[2]), m[3])
    end = _to_utc(int(m[4]), int(m[5]), m[6] or m[3])
    if start == end:
        raise RobotsError(f"Visiting-hours window is empty: {value!r}")
    return VisitWindow(start, end)


def _rules_for(groups: list[tuple[list[str], list[Rule]]], user_agent: str) -> list[Rule]:
    """Rules of the group whose agent token appears in our User-Agent, else of `*`."""
    ua = user_agent.lower()
    specific = [rules for agents, rules in groups if any(a != "*" and a in ua for a in agents)]
    if specific:
        return [rule for rules in specific for rule in rules]
    return [rule for agents, rules in groups if "*" in agents for rule in rules]


def _to_utc(hour: int, minute: int, zone: str | None) -> time:
    if zone is None:
        raise RobotsError("Visiting-hours has no time zone")
    offset = TZ_OFFSETS_HOURS.get(zone.upper())
    if offset is None:
        raise RobotsError(f"unknown time zone in Visiting-hours: {zone!r}")
    if not (0 <= hour <= 24 and 0 <= minute < 60):
        raise RobotsError(f"invalid time in Visiting-hours: {hour:02d}:{minute:02d}")
    minutes = (hour * 60 + minute - offset * 60) % (24 * 60)
    return time(minutes // 60, minutes % 60)


def _parse_hit_rate(value: str | None) -> float:
    if value is None:
        raise RobotsError("robots.txt has no Hit-rate directive")
    try:
        seconds = float(value)
    except ValueError:
        raise RobotsError(f"can't parse Hit-rate: {value!r}") from None
    if seconds <= 0:
        raise RobotsError(f"Hit-rate must be positive: {value!r}")
    return seconds


def _parse_concurrent_hits(value: str | None) -> int:
    if value is None:
        raise RobotsError("robots.txt has no Concurrent-hits directive")
    if not value.isdigit() or int(value) < 1:
        raise RobotsError(f"can't parse Concurrent-hits: {value!r}")
    return int(value)


@cache
def _pattern_regex(pattern: str) -> re.Pattern[str]:
    anchored = pattern.endswith("$")
    body = pattern[:-1] if anchored else pattern
    regex = ".*".join(re.escape(part) for part in body.split("*"))
    return re.compile(regex + ("$" if anchored else ""))
