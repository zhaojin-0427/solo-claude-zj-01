"""Pure-Python civil-time engine with explicit DST rules.

No system tzdata is used: the caller supplies the standard UTC offset and a
DST rule (none / fixed date / nth weekday of month). All datetimes are naive
UTC internally; "local clock time" is always presented together with its
offset so ambiguities are explicit.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta

from .schemas import DSTRule, Weekday

WEEKDAYS = {
    Weekday.mon: 0, Weekday.tue: 1, Weekday.wed: 2, Weekday.thu: 3,
    Weekday.fri: 4, Weekday.sat: 5, Weekday.sun: 6,
}


def _hhmm(s: str) -> int:
    h, m = s.split(":")
    return int(h) * 60 + int(m)


def _nth_weekday(year: int, month: int, weekday: Weekday, week: int) -> date:
    first = date(year, month, 1)
    target = WEEKDAYS[weekday]
    delta = (target - first.weekday()) % 7
    if week == 5:  # last
        d = first + timedelta(days=delta)
        last = d
        while True:
            nxt = last + timedelta(days=7)
            if nxt.month != month:
                return last
            last = nxt
    return first + timedelta(days=delta + 7 * (week - 1))


def _rule_date(year: int, month: int, day: int | None,
               weekday: Weekday | None, week: int | None) -> date:
    if day is not None:
        return date(year, month, day)
    assert weekday is not None and week is not None
    return _nth_weekday(year, month, weekday, week)


@dataclass(frozen=True)
class Transition:
    """A DST boundary, expressed in UTC."""

    kind: str  # "start" | "end"
    utc: datetime
    offset_before: int
    offset_after: int


class TimezoneEngine:
    def __init__(self, standard_offset_minutes: int, rule: DSTRule):
        self.std = standard_offset_minutes
        self.rule = rule
        self.dst_delta = rule.dst_offset_minutes if rule.mode != "none" else 0

    # ----------------------------------------------------------- rules --
    def transitions_for_year(self, year: int) -> list[Transition]:
        if self.rule.mode == "none":
            return []
        r = self.rule
        start_day = _rule_date(
            year, r.start_month, r.start_day, r.start_weekday, r.start_week
        )
        end_day = _rule_date(
            year, r.end_month, r.end_day, r.end_weekday, r.end_week
        )
        # 02:00 standard clock -> UTC on spring-forward day
        start_utc = datetime.combine(start_day, datetime.min.time()) + timedelta(
            minutes=_hhmm(r.start_at_local) - self.std
        )
        # 03:00 daylight clock -> UTC on fall-back day
        end_utc = datetime.combine(end_day, datetime.min.time()) + timedelta(
            minutes=_hhmm(r.end_at_local) - (self.std + self.dst_delta)
        )
        return [
            Transition("start", start_utc, self.std, self.std + self.dst_delta),
            Transition("end", end_utc, self.std + self.dst_delta, self.std),
        ]

    def transitions_between(self, start_utc: datetime,
                            end_utc: datetime) -> list[Transition]:
        out: list[Transition] = []
        for y in range(start_utc.year - 1, end_utc.year + 2):
            for t in self.transitions_for_year(y):
                if start_utc - timedelta(days=2) <= t.utc <= end_utc + timedelta(days=2):
                    out.append(t)
        return sorted(out, key=lambda t: t.utc)

    # --------------------------------------------------------- queries --
    def _year_transitions(self, utc: datetime) -> list[Transition]:
        out: list[Transition] = []
        for y in (utc.year - 1, utc.year, utc.year + 1):
            out.extend(self.transitions_for_year(y))
        return sorted(out, key=lambda t: t.utc)

    def offset_at(self, utc: datetime,
                  transitions: list[Transition] | None = None) -> int:
        if self.rule.mode == "none":
            return self.std
        if transitions is None:
            transitions = self._year_transitions(utc)
        offset = self.std
        for t in transitions:
            if t.utc <= utc:
                offset = t.offset_after
        return offset

    def is_dst(self, utc: datetime) -> bool:
        return self.offset_at(utc) != self.std

    def local_clock(self, utc: datetime) -> tuple[datetime, int, bool]:
        """Return (naive local wall-clock datetime, offset minutes, dst)."""
        offset = self.offset_at(utc)
        return utc + timedelta(minutes=offset), offset, offset != self.std

    def to_utc(self, local_naive: datetime,
               transitions: list[Transition] | None,
               fold: int = 0) -> tuple[datetime, str]:
        """Convert naive local clock time to UTC.

        Status: ``ok`` | ``skipped`` (spring-forward gap) |
        ``ambiguous`` (fall-back overlap). ``fold=0`` picks the first
        occurrence (daylight time), ``fold=1`` the second (standard time).
        """
        if self.rule.mode == "none":
            return local_naive - timedelta(minutes=self.std), "ok"
        if transitions is None:
            transitions = self.transitions_between(
                local_naive - timedelta(days=1),
                local_naive + timedelta(days=1),
            )
        status = "ok"
        offset = self.std
        for t in transitions:
            if t.kind == "start":
                # local [02:00 std, 03:00 dst) does not exist
                gap_begin = t.utc + timedelta(minutes=t.offset_before)
                gap_end = t.utc + timedelta(minutes=t.offset_after)
                if gap_begin <= local_naive < gap_end:
                    status = "skipped"
                    offset = t.offset_after
            else:
                # End instant is local (offset_before) o'clock. The repeated
                # wall-clock interval is [end-offset_after, end-offset_before)
                # e.g. end 03:00 DST (=01:00Z): overlap local [02:00, 03:00)
                ov_begin = t.utc + timedelta(minutes=t.offset_after)
                ov_end = t.utc + timedelta(minutes=t.offset_before)
                if ov_begin <= local_naive < ov_end:
                    status = "ambiguous"
                    # fold 0 = first occurrence, still on daylight offset
                    offset = (
                        t.offset_after if fold == 1 else t.offset_before
                    )
        if status == "ok":
            # Choose whichever offset is self-consistent at this instant
            for cand_offset in (self.std + self.dst_delta, self.std):
                cand_utc = local_naive - timedelta(minutes=cand_offset)
                if self.offset_at(cand_utc, transitions) == cand_offset:
                    offset = cand_offset
                    break
        utc = local_naive - timedelta(minutes=offset)
        return utc, status

    def gap_intervals_between(self, start_utc: datetime,
                              end_utc: datetime) -> list[tuple[datetime, datetime]]:
        """Spring-forward gaps expressed as local-clock intervals."""
        out = []
        for t in self.transitions_between(start_utc, end_utc):
            if t.kind != "start":
                continue
            lb = t.utc + timedelta(minutes=t.offset_before)
            le = t.utc + timedelta(minutes=t.offset_after)
            out.append((lb, le))
        return out
