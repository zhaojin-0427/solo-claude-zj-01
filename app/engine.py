"""Core sundial engine: sampling, shadow intersections, line checks.

The engine is deliberately deterministic: given the same request payload it
always produces identical samples (sorted UTC iteration, stable ordering),
which is what makes a version reproducible.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date, datetime, timedelta

from . import geometry as G
from .obstacles import CompiledContour, blocking_obstacle
from .schemas import (
    DialLine, GenerateOptions, LabelBox, LinePoint,
    ObstacleHitInfo, Point2, SpacingIssue, WallInput,
)
from .solar import (
    solar_terms, solar_time_minutes, sun_position, utc_for_solar_time,
)
from .timezone_engine import TimezoneEngine

BELOW = "below_horizon"
BEHIND = "sun_behind_wall"
PARALLEL = "shadow_parallel"
OUTSIDE = "shadow_outside_panel"
BLOCKED = "blocked_by_obstacle"
OK = "ok"
DST_GAP = "dst_gap"

# status priority when a sample fails for several reasons
ORDER = [DST_GAP, BELOW, BEHIND, PARALLEL, BLOCKED, OUTSIDE]


@dataclass(frozen=True)
class Sample:
    utc: datetime
    local: datetime
    offset: int
    dst: bool
    solar_min: float
    decl_deg: float
    eot_min: float
    alt_deg: float
    az_deg: float
    sun: G.Vec3
    dst_gap: bool = False


# ---------------------------------------------------------------- sampler --


def daterange(d0: date, d1: date):
    cur = d0
    one = timedelta(days=1)
    while cur <= d1:
        yield cur
        cur += one


def _dst_gap_lookup(tz: TimezoneEngine, d0: date, d1: date):
    gaps = tz.gap_intervals_between(
        datetime.combine(d0, datetime.min.time()) - timedelta(days=1),
        datetime.combine(d1, datetime.min.time()) + timedelta(days=2),
    )

    def in_gap(local_naive: datetime) -> bool:
        return any(a <= local_naive < b for a, b in gaps)

    return in_gap, gaps


def build_samples(
    wall: WallInput,
    d0: date,
    d1: date,
    step_minutes: int,
) -> tuple[list[Sample], TimezoneEngine, list[tuple[datetime, datetime]]]:
    """All UTC samples over the range, in strict ascending order.

    The third element is the list of *local-clock* DST gap intervals; no UTC
    instant maps to those wall-clock times, so civil lines receive explicit
    placeholder samples (see :func:`insert_gap_placeholders`).
    """
    tz = TimezoneEngine(wall.standard_offset_minutes, wall.dst)
    in_gap, gaps = _dst_gap_lookup(tz, d0, d1)
    out: list[Sample] = []
    # Window in UTC that fully covers *local* dates [d0, d1]; the offset
    # bounds include the DST jump so no instant on either end is missed.
    off_lo = min(tz.std, tz.std + tz.dst_delta)
    off_hi = max(tz.std, tz.std + tz.dst_delta)
    start = datetime.combine(d0, datetime.min.time()) - timedelta(
        minutes=off_hi + 30
    )
    end = datetime.combine(d1, datetime.min.time()) + timedelta(
        days=1, minutes=-off_lo + 30
    )
    t = start
    step = timedelta(minutes=step_minutes)
    while t <= end:
        pos = sun_position(t, wall.latitude, wall.longitude)
        local, offset, dst = tz.local_clock(t)
        terms = solar_terms(t)
        sm = solar_time_minutes(t, wall.longitude, terms)
        out.append(
            Sample(
                utc=t, local=local, offset=offset, dst=dst, solar_min=sm,
                decl_deg=pos.declination_deg, eot_min=pos.eot_minutes,
                alt_deg=pos.altitude_deg, az_deg=pos.azimuth_deg,
                sun=pos.sun_enu, dst_gap=in_gap(local),
            )
        )
        t += step
    return out, tz, gaps


def make_gap_sample(
    wall: WallInput, tz: TimezoneEngine, local_naive: datetime,
    nominal_offset: int
) -> Sample:
    """Placeholder for a wall-clock instant that no UTC maps to."""
    utc_nominal = local_naive - timedelta(minutes=nominal_offset)
    pos = sun_position(utc_nominal, wall.latitude, wall.longitude)
    terms = solar_terms(utc_nominal)
    sm = solar_time_minutes(utc_nominal, wall.longitude, terms)
    return Sample(
        utc=utc_nominal, local=local_naive, offset=nominal_offset,
        dst=True, solar_min=sm, decl_deg=pos.declination_deg,
        eot_min=pos.eot_minutes, alt_deg=pos.altitude_deg,
        az_deg=pos.azimuth_deg, sun=pos.sun_enu, dst_gap=True,
    )


def insert_gap_placeholders(
    wall: WallInput,
    evs: list[EvaluatedSample],
    tz: TimezoneEngine,
    gaps: list[tuple[datetime, datetime]],
    hour: int | None,
) -> list[EvaluatedSample]:
    """Add ``dst_gap`` samples on the civil hour that springs forward."""
    existing = {(e.sample.utc, e.sample.local) for e in evs}
    extra: list[EvaluatedSample] = []
    for ga, gb in gaps:
        t = ga.replace(minute=(ga.minute // 30) * 30, second=0, microsecond=0)
        if t < ga:
            t += timedelta(minutes=30)
        while t < gb:
            if hour is None or t.hour == hour:
                s = make_gap_sample(
                    wall, tz, t,
                    tz.std + tz.dst_delta,
                )
                if (s.utc, s.local) not in existing:
                    extra.append(EvaluatedSample(
                        sample=s, status=DST_GAP, point=None,
                        sdotn=None, hour_label=hour,
                        note="wall-clock time skipped by spring-forward "
                             "transition",
                    ))
            t += timedelta(minutes=30)
    out = list(evs) + extra
    out.sort(key=lambda e: (e.sample.utc, e.sample.local))
    return out


# ---------------------------------------------------------- geometry run --


@dataclass
class EvaluatedSample:
    sample: Sample
    status: str
    point: G.Pt | None
    sdotn: float | None
    hour_label: int | None = None
    note: str = ""
    obstacle: "ObstacleHit | None" = None
    """profile hit for a blocked sample (also kept on blocked-only samples so
    the original sampling instant stays traceable)"""


@dataclass(frozen=True)
class ObstacleHit:
    name: str
    profile_altitude_deg: float
    margin_deg: float


def _to_hit(ob, s: Sample, hit_point: G.Pt | None) -> tuple[ObstacleHit, str]:
    hit = ObstacleHit(
        name=ob.name, profile_altitude_deg=ob.profile_altitude_deg,
        margin_deg=ob.margin_deg,
    )
    note = (
        f"obstacle '{ob.name}' skyline {ob.profile_altitude_deg:.3f}° at az "
        f"{s.az_deg:.2f}° (margin {ob.margin_deg:.3f}°)"
    )
    if hit_point is not None:
        note += (
            f"; wall-plane intersection ({hit_point[0]:.3f},"
            f"{hit_point[1]:.3f})"
        )
    return hit, note


def evaluate_samples(
    samples: list[Sample],
    frame: G.WallFrame,
    gnomon: G.Gnomon,
    panel: list[G.Pt],
    options: GenerateOptions,
    profiles: list[CompiledContour] | None = None,
) -> list[EvaluatedSample]:
    profiles = profiles or []
    tip = gnomon.tip(frame)
    eps = options.parallel_cos_threshold
    out: list[EvaluatedSample] = []
    for s in samples:
        status = OK
        note = ""
        hit_point: G.Pt | None = None
        ob_hit: ObstacleHit | None = None
        sdotn = G.dot(s.sun, frame.normal)
        if s.dst_gap:
            status = DST_GAP
            note = "wall-clock time skipped by spring-forward transition"
        elif s.alt_deg <= 0:
            status = BELOW
        else:
            hit = G.shadow_intersection(frame, tip, s.sun, eps)
            hit_point = hit.point
            if hit.status == "nodus_in_wall":
                status = BEHIND
                note = "nodus lies in/behind the wall plane; "
            elif hit.status == "sun_behind_wall":
                status = BEHIND
            elif hit.status == "shadow_parallel":
                status = PARALLEL
            else:
                # ray actually meets the wall: only now can a skyline hide
                # the Sun (a sun behind the wall is not an obstacle loss).
                ob = blocking_obstacle(profiles, s.az_deg, s.alt_deg)
                if ob is not None:
                    status = BLOCKED
                    ob_hit, ob_note = _to_hit(ob, s, hit_point)
                elif hit.status == "ok" and not G.point_in_polygon(
                    hit_point, panel
                ):
                    status = OUTSIDE
                else:
                    status = OK
            note += f"s·n={hit.sdotn:.4f}"
            if status == OUTSIDE:
                note += (
                    f"; wall-plane intersection ({hit_point[0]:.3f},"
                    f"{hit_point[1]:.3f}) outside panel"
                )
            if ob_hit is not None:
                note += "; " + ob_note
        out.append(
            EvaluatedSample(
                sample=s, status=status,
                point=hit_point if status == OK else None,
                sdotn=sdotn, note=note.strip(), obstacle=ob_hit,
            )
        )
    return out


def to_line_point(e: EvaluatedSample) -> LinePoint:
    s = e.sample
    return LinePoint(
        date=s.utc.date(),
        hour_label=e.hour_label,
        utc=s.utc.strftime("%Y-%m-%dT%H:%M:%SZ"),
        local_clock=s.local.strftime("%Y-%m-%dT%H:%M")
        + ("D" if s.dst else "S"),
        solar_time_min=round(s.solar_min, 3),
        dst=s.dst,
        altitude_deg=round(s.alt_deg, 4),
        azimuth_deg=round(s.az_deg, 4),
        declination_deg=round(s.decl_deg, 4),
        eot_min=round(s.eot_min, 3),
        sun_enu=tuple(round(v, 6) for v in s.sun),
        shadow=Point2(x=round(e.point[0], 5), y=round(e.point[1], 5))
        if e.point is not None else None,
        status=e.status,
        reason=_reason(e),
        note=e.note,
        obstacle=ObstacleHitInfo(
            name=e.obstacle.name,
            azimuth_deg=round(s.az_deg, 4),
            profile_altitude_deg=round(e.obstacle.profile_altitude_deg, 4),
            margin_deg=round(e.obstacle.margin_deg, 4),
        ) if e.obstacle is not None else None,
    )


def _reason(e: EvaluatedSample) -> str:
    return {
        OK: "shadow falls on the panel",
        BELOW: "sun below horizon",
        BEHIND: "sun is behind the wall",
        PARALLEL: "shadow ray parallel to the wall plane",
        OUTSIDE: "intersection with wall plane lies outside the panel",
        BLOCKED: "sun hidden by an obstruction skyline",
        DST_GAP: "local clock time does not exist (DST gap)",
    }[e.status]


# --------------------------------------------------------------- lines ---


def _segments(ev: list[EvaluatedSample]):
    """Split evaluated samples into connected ok-point segments."""
    segments: list[list[G.Pt]] = []
    cur: list[G.Pt] = []
    for e in ev:
        if e.status == OK:
            cur.append(e.point)
        elif cur:
            segments.append(cur)
            cur = []
    if cur:
        segments.append(cur)
    return segments


def _gaps(ev: list[EvaluatedSample], tolerance: timedelta,
          extent: timedelta) -> list[dict]:
    """Merge consecutive non-ok, above-horizon samples into reason spans.

    Samples below the horizon simply delimit where a line starts/ends
    (there is no shadow at all) and are never reported as line gaps.
    Samples closer in time than ``tolerance`` are one span; hour lines pass
    ~25 h (one sample per date), season lines 1.5 sampling steps. Blocked
    spans are additionally split by the obstructing contour name.
    """
    GAP_STATUSES = {BEHIND, PARALLEL, OUTSIDE, DST_GAP, BLOCKED}
    gaps: list[dict] = []
    cur: dict | None = None
    prev: EvaluatedSample | None = None
    for e in ev:
        ts = e.sample.utc
        contiguous = (
            prev is not None and (ts - prev.sample.utc) <= tolerance
        )
        gap_key = (e.status,
                   e.obstacle.name if e.status == BLOCKED else None)
        if e.status in GAP_STATUSES:
            if cur and cur["key"] == gap_key and contiguous:
                cur["end_utc"] = ts
            else:
                cur = {"key": gap_key, "status": e.status,
                       "obstacle": gap_key[1],
                       "start_utc": ts, "end_utc": ts}
                gaps.append(cur)
        else:
            cur = None
        prev = e
    out = []
    for g in gaps:
        item = {
            "status": g["status"],
            "start_utc": g["start_utc"].strftime("%Y-%m-%dT%H:%M:%SZ"),
            "end_utc": (g["end_utc"] + extent).strftime(
                "%Y-%m-%dT%H:%M:%SZ"
            ),
        }
        if g["obstacle"] is not None:
            item["obstacle"] = g["obstacle"]
        out.append(item)
    return out


def _counts(ev: list[EvaluatedSample]) -> dict:
    c = {"sample_count": len(ev), "ok_count": 0, "outside_count": 0,
         "behind_count": 0, "parallel_count": 0, "below_count": 0,
         "blocked_count": 0, "gap_count": 0}
    for e in ev:
        c[{OK: "ok_count", OUTSIDE: "outside_count", BEHIND: "behind_count",
           PARALLEL: "parallel_count", BELOW: "below_count",
           BLOCKED: "blocked_count",
           DST_GAP: "gap_count"}[e.status]] += 1
    return c


def build_lines(
    wall: WallInput,
    evaluated: list[EvaluatedSample],
    options: GenerateOptions,
    gnomon: G.Gnomon,
    frame: G.WallFrame,
    panel: list[G.Pt],
    step_minutes: int,
    d0: date,
    d1: date,
    tz: TimezoneEngine | None = None,
    gaps: list[tuple[datetime, datetime]] | None = None,
    profiles: list[CompiledContour] | None = None,
) -> list[DialLine]:
    """Build hour and season lines.

    Hour lines contain exactly one exact instant per requested *local* date
    (true-solar inversion or civil-clock lookup), so successive points are
    successive dates and connect smoothly; a non-ok date breaks the engraved
    line. Season date lines are sampled finely across their own day only.
    """
    profiles = profiles or []
    days = list(daterange(d0, d1))
    if tz is None:
        tz = TimezoneEngine(wall.standard_offset_minutes, wall.dst)
    gaps = gaps if gaps is not None else []
    frame_eps = options.parallel_cos_threshold

    lines: list[DialLine] = []

    # ----- hour lines -----
    for hour in sorted(set(options.hours)):
        evs: list[EvaluatedSample] = []
        for day in days:
            evs.append(_hour_instant(
                wall, tz, frame, gnomon, panel, day, hour, options,
                frame_eps, gaps, profiles,
            ))
        label = f"{hour:02d}:00" + ("" if options.time_mode == "solar" else " L")
        lines.append(_make_line(
            "hour", label, evs, hour=hour,
            tolerance=timedelta(hours=25), extent=timedelta(hours=12),
        ))

    # ----- season date lines -----
    for sd in sorted(set(options.season_dates)):
        # Season curves are only meaningful for dates the caller asked for;
        # a single-day request must not drag adjacent-season samples in.
        if not (d0 <= sd <= d1):
            # month/day may still match an occurrence inside the range
            occ = [d for d in days if (d.month, d.day) == (sd.month, sd.day)]
            if not occ:
                continue
            sd = occ[0]
        evs = _season_day(
            wall, tz, gnomon, frame, panel, sd, options, step_minutes, gaps,
            profiles,
        )
        lines.append(_make_line(
            "season", sd.strftime("%m-%d"), evs, season_date=sd,
            tolerance=timedelta(minutes=step_minutes) * 1.5,
            extent=timedelta(minutes=step_minutes),
        ))
    return lines


def _hour_instant(wall, tz, frame, gnomon, panel, day: date, hour: int,
                  options, frame_eps, gaps,
                  profiles: list[CompiledContour]) -> EvaluatedSample:
    """One exact sample per day for the requested hour."""
    mid = datetime.combine(day, datetime.min.time()) + timedelta(hours=12)
    note_prefix = ""
    if options.time_mode == "solar":
        utc, achieved = utc_for_solar_time(
            datetime.combine(day, datetime.min.time()),
            wall.longitude, hour * 60.0,
        )
        note_prefix = (
            f"UTC solved from solar time {hour:02d}:00 via EoT"
            f" (achieved {achieved/60:.4f} h); "
        )
        if abs((achieved / 60.0) - hour) > 0.02:
            note_prefix += "inversion residual >1.2 min; "
        sample = _sample_at(wall, tz, utc)
    else:
        local = datetime.combine(day, datetime.min.time()).replace(hour=hour)
        trs = tz.transitions_between(local - timedelta(days=1),
                                     local + timedelta(days=1))
        utc, conv_status = tz.to_utc(local, trs, fold=0)
        sample = _sample_at(wall, tz, utc)
        if conv_status == "skipped" or _in_any_gap(local, gaps):
            return EvaluatedSample(
                sample=Sample(
                    utc=utc, local=local, offset=tz.std + tz.dst_delta,
                    dst=True, solar_min=sample.solar_min,
                    decl_deg=sample.decl_deg, eot_min=sample.eot_min,
                    alt_deg=sample.alt_deg, az_deg=sample.az_deg,
                    sun=sample.sun, dst_gap=True,
                ),
                status=DST_GAP, point=None, sdotn=None, hour_label=hour,
                note="wall-clock time skipped by spring-forward transition",
            )
        note_prefix = f"civil clock {hour:02d}:00 -> UTC {utc:%H:%M}; "
    ev = _classify(sample, frame, gnomon, panel, frame_eps, profiles)
    ev.hour_label = hour
    ev.note = note_prefix + ev.note
    return ev


def _in_any_gap(local: datetime, gaps) -> bool:
    return any(a <= local < b for a, b in gaps)


def _sample_at(wall, tz: TimezoneEngine, utc: datetime) -> Sample:
    pos = sun_position(utc, wall.latitude, wall.longitude)
    local, offset, dst = tz.local_clock(utc)
    terms = solar_terms(utc)
    sm = solar_time_minutes(utc, wall.longitude, terms)
    return Sample(
        utc=utc, local=local, offset=offset, dst=dst, solar_min=sm,
        decl_deg=pos.declination_deg, eot_min=pos.eot_minutes,
        alt_deg=pos.altitude_deg, az_deg=pos.azimuth_deg, sun=pos.sun_enu,
    )


def _classify(sample: Sample, frame, gnomon, panel, frame_eps,
              profiles: list[CompiledContour] | None = None) -> EvaluatedSample:
    profiles = profiles or []
    tip = gnomon.tip(frame)
    sdotn = G.dot(sample.sun, frame.normal)
    if sample.dst_gap:
        return EvaluatedSample(sample, DST_GAP, None, sdotn,
                               note="DST gap")
    if sample.alt_deg <= 0:
        return EvaluatedSample(sample, BELOW, None, sdotn,
                               note=f"s·n={sdotn:.4f}")
    hit = G.shadow_intersection(frame, tip, sample.sun, frame_eps)
    if hit.status == "nodus_in_wall":
        return EvaluatedSample(sample, BEHIND, None, sdotn,
                               note="nodus in/behind wall plane; "
                                    f"s·n={sdotn:.4f}")
    if hit.status == "sun_behind_wall":
        return EvaluatedSample(sample, BEHIND, None, sdotn,
                               note=f"s·n={sdotn:.4f}")
    if hit.status == "shadow_parallel":
        return EvaluatedSample(sample, PARALLEL, None, sdotn,
                               note=f"|s·n|={abs(sdotn):.4f}")
    p = hit.point
    ob = blocking_obstacle(profiles, sample.az_deg, sample.alt_deg)
    if ob is not None:
        ob_hit, ob_note = _to_hit(ob, sample, p)
        return EvaluatedSample(
            sample, BLOCKED, None, sdotn, obstacle=ob_hit,
            note=f"s·n={sdotn:.4f}; " + ob_note,
        )
    if not G.point_in_polygon(p, panel):
        return EvaluatedSample(
            sample, OUTSIDE, None, sdotn,
            note=f"s·n={sdotn:.4f}; wall-plane intersection "
                 f"({p[0]:.3f},{p[1]:.3f}) outside panel",
        )
    return EvaluatedSample(sample, OK, p, sdotn, note=f"s·n={sdotn:.4f}")


def _season_day(wall, tz, gnomon, frame, panel, sd: date, options,
                step_minutes, gaps,
                profiles: list[CompiledContour] | None = None
                ) -> list[EvaluatedSample]:
    """Fine UTC sampling for one date (season date curves)."""
    profiles = profiles or []
    start = datetime.combine(sd, datetime.min.time()) - timedelta(hours=1)
    end = start + timedelta(hours=26)
    out: list[EvaluatedSample] = []
    t = start
    step = timedelta(minutes=step_minutes)
    while t <= end:
        s = _sample_at(wall, tz, t)
        if s.alt_deg > -0.5:
            ev = _classify(s, frame, gnomon, panel,
                           options.parallel_cos_threshold, profiles)
            out.append(ev)
        t += step
    return out


def _make_line(kind, label, evs, tolerance, extent, hour=None,
               season_date=None) -> DialLine:
    segments = _segments(evs)
    gaps = _gaps(evs, tolerance, extent)
    c = _counts(evs)
    broken = len(segments) > 1
    return DialLine(
        kind=kind, label=label, hour=hour, season_date=season_date,
        segments=[[Point2(x=round(x, 5), y=round(y, 5)) for x, y in seg]
                  for seg in segments],
        sample_basis=[to_line_point(e) for e in evs],
        broken=broken,
        gaps=gaps,
        **c,
    )


# ------------------------------------------------- invalid intervals ----


def merge_intervals(
    samples: list[EvaluatedSample],
    step: timedelta,
    wanted=("sun_behind_wall", "shadow_parallel", "shadow_outside_panel",
            "blocked_by_obstacle", "dst_gap"),
) -> list[tuple[str, str | None, datetime, datetime]]:
    """Join consecutive same-reason bad samples on the regular UTC grid.

    Every gap longer than 1.5 sampling steps (e.g. the night stretch of
    below-horizon samples) closes a span, so morning and afternoon
    outside-panel runs never merge across the night. Blocked spans carry
    the obstructing contour name and adjacent runs hidden by different
    contours are not joined.
    """
    merged: list[list] = []
    prev: EvaluatedSample | None = None
    for e in samples:
        bad = (e.status in wanted and e.status != OK
               and e.status != BELOW)
        key = (e.status,
               e.obstacle.name if e.status == BLOCKED else None)
        prev_key = (
            (prev.status,
             prev.obstacle.name if prev.status == BLOCKED else None)
            if prev is not None else None
        )
        contiguous = (
            bad and prev is not None and prev_key == key
            and e.sample.utc - prev.sample.utc <= step * 1.5
        )
        if bad:
            t = e.sample.utc
            if contiguous:
                merged[-1][3] = t
            else:
                merged.append([e.status, key[1], t, t])
        prev = e
    return [(r, name, a, b + step) for r, name, a, b in merged]


# ------------------------------------------------------------ checks ----


def check_spacing(
    lines: list[DialLine], min_gap: float
) -> tuple[float | None, list[SpacingIssue]]:
    """Min distance between *adjacent* hour lines (label hours differing 1).

    Sampled endpoints/vertices are compared segment-wise; lines that share a
    common point (they meet at the nodus projection) are ignored there.
    """
    hour_lines = sorted(
        (l for l in lines if l.kind == "hour" and l.hour is not None),
        key=lambda l: l.hour,
    )
    overall_min: float | None = None
    issues: list[SpacingIssue] = []

    def seg_points(line: DialLine):
        for seg in line.segments:
            pts = [(p.x, p.y) for p in seg]
            for i in range(len(pts) - 1):
                yield pts[i], pts[i + 1]

    def end_points(line: DialLine):
        for seg in line.segments:
            if seg:
                yield seg[0]
                yield seg[-1]

    for a, b in zip(hour_lines, hour_lines[1:]):
        if b.hour - a.hour != 1:
            continue
        best: float | None = None
        best_at: G.Pt | None = None
        segs_b = list(seg_points(b))
        # compare endpoints of a against segments of b and vice versa:
        # captures how close adjacent engraved strokes actually get
        for pobj in end_points(a):
            p = (pobj.x, pobj.y)
            for c, d in segs_b:
                dist = G.distance_to_segment(p, c, d)
                if best is None or dist < best:
                    best, best_at = dist, p
        for pobj in end_points(b):
            p = (pobj.x, pobj.y)
            for c, d in seg_points(a):
                dist = G.distance_to_segment(p, c, d)
                if best is None or dist < best:
                    best, best_at = dist, p
        if best is not None:
            overall_min = best if overall_min is None else min(overall_min, best)
            if best < min_gap:
                issues.append(SpacingIssue(
                    hour_a=a.hour, hour_b=b.hour, distance=round(best, 5),
                    at=Point2(x=round(best_at[0], 5), y=round(best_at[1], 5)),
                ))
    return (round(overall_min, 5) if overall_min is not None else None, issues)


def label_angle(seg: list[G.Pt]) -> float:
    if len(seg) < 2:
        return 0.0
    (x0, y0), (x1, y1) = seg[0], seg[-1]
    ang = math.atan2(y1 - y0, x1 - x0) / math.pi * 180.0
    while ang > 90:
        ang -= 180
    while ang < -90:
        ang += 180
    return ang


def layout_labels(
    lines: list[DialLine], offset: G.Pt, font_h: float
) -> list[LabelBox]:
    boxes: list[LabelBox] = []
    char_w = font_h * 0.56
    for line in lines:
        if not line.segments:
            continue
        # attach label to the longest visible segment, at its outer end
        seg = max(line.segments, key=lambda s: G.polyline_length(
            [(p.x, p.y) for p in s]
        ))
        pts = [(p.x, p.y) for p in seg]
        end = pts[-1]
        ang = label_angle(pts)
        text = line.label
        w = max(font_h * 0.8, char_w * max(3, len(text)))
        boxes.append(LabelBox(
            line_label=line.label,
            x=round(end[0] + offset[0], 5),
            y=round(end[1] + offset[1], 5),
            width=round(w, 5), height=round(font_h, 5),
            angle_deg=round(ang, 2),
        ))
    return boxes


def check_labels(boxes: list[LabelBox]) -> list[dict]:
    overlaps = []
    polys = [
        (b, G.obb_corners(b.x, b.y, b.width, b.height, b.angle_deg))
        for b in boxes
    ]
    for i in range(len(polys)):
        for j in range(i + 1, len(polys)):
            if G.polygons_overlap(polys[i][1], polys[j][1]):
                overlaps.append({
                    "label_a": polys[i][0].line_label,
                    "label_b": polys[j][0].line_label,
                })
    return overlaps


def check_margins(
    lines: list[DialLine], boxes: list[LabelBox], panel: list[G.Pt],
    margin: float
) -> list[dict]:
    viol = []
    for line in lines:
        for seg in line.segments:
            for p in seg:
                if not G.point_in_eroded_polygon((p.x, p.y), panel, margin):
                    viol.append({"line": line.label, "x": p.x, "y": p.y})
    for b in boxes:
        for cx, cy in G.obb_corners(b.x, b.y, b.width, b.height, b.angle_deg):
            if not G.point_in_eroded_polygon((cx, cy), panel, margin):
                viol.append({"line": b.line_label, "x": round(cx, 4),
                             "y": round(cy, 4), "kind": "label"})
    return viol[:50]


# ----------------------------------------------------------- coverage ---


def coverage_sweep(
    wall: WallInput,
    frame: G.WallFrame,
    gnomon: G.Gnomon,
    samples: list[Sample],
    options: GenerateOptions,
    profiles: list[CompiledContour] | None = None,
) -> dict:
    """Fractions of (a) sun-above-horizon samples and (b) samples whose ray
    meets the wall at all, that land inside the panel.

    With obstruction profiles the *eligible* denominator excludes samples
    hidden by a skyline while the wall is lit; blocked samples are counted
    separately per contour so the loss is attributable.
    """
    profiles = profiles or []
    tip = gnomon.tip(frame)
    panel = [(p.x, p.y) for p in wall.panel]
    eps = options.parallel_cos_threshold
    above = wall_hit = readable = blocked = eligible = 0
    blocked_by_name: dict[str, int] = {}
    for s in samples:
        if s.alt_deg <= 0:
            continue
        above += 1
        hit = G.shadow_intersection(frame, tip, s.sun, eps)
        if hit.status != "ok" or hit.point is None:
            continue
        wall_hit += 1
        ob = blocking_obstacle(profiles, s.az_deg, s.alt_deg)
        if ob is not None:
            blocked += 1
            blocked_by_name[ob.name] = blocked_by_name.get(ob.name, 0) + 1
            continue
        eligible += 1
        if G.point_in_polygon(hit.point, panel):
            readable += 1
    out = {
        "samples_above_horizon": above,
        "samples_wall_lit": wall_hit,
        "samples_readable": readable,
        "readable_of_above": round(readable / above, 4) if above else 0.0,
        "readable_of_wall_lit": round(readable / wall_hit, 4)
        if wall_hit else 0.0,
        "wall_lit_of_above": round(wall_hit / above, 4) if above else 0.0,
    }
    if profiles:
        out.update({
            "samples_blocked_by_obstacle": blocked,
            "samples_eligible": eligible,
            "readable_of_eligible": round(readable / eligible, 4)
            if eligible else 0.0,
            "blocked_by_obstacle": [
                {"name": prof.name,
                 "blocked_samples": blocked_by_name.get(prof.name, 0)}
                for prof in profiles
            ],
        })
    return out


def panel_usage(lines: list[DialLine], panel: list[G.Pt]) -> float:
    """Engraved-length / panel area^(1/2): dimensionless occupancy density."""
    total = 0.0
    for line in lines:
        for seg in line.segments:
            total += G.polyline_length([(p.x, p.y) for p in seg])
    side = math.sqrt(G.polygon_area(panel))
    return round(total / side, 4) if side else 0.0


# --------------------------------------------------------- obstacle loss --


def obstacle_loss_sweep(
    samples: list[Sample],
    frame: G.WallFrame,
    profiles: list[CompiledContour],
    step: timedelta,
    eps: float,
) -> dict:
    """Blocked samples and merged UTC spans per contour.

    The decision is independent of the gnomon: a sample counts as lost when
    the Sun is above the horizon, faces the wall (``s·n >= eps``) and its
    altitude is at or below a profile's interpolated skyline. Consecutive
    blocked samples (same contour, gap <= 1.5 sampling steps) merge into
    one span whose end is extended by one step (the sample represents the
    following interval). Nights and wall-away stretches separate spans.
    """
    by_name = {p.name: {"count": 0, "spans": []} for p in profiles}
    prev_name: str | None = None
    prev_t: datetime | None = None
    for s in samples:
        name = None
        if s.alt_deg > 0 and G.dot(s.sun, frame.normal) >= eps:
            hit = blocking_obstacle(profiles, s.az_deg, s.alt_deg)
            if hit is not None:
                name = hit.name
        if name is not None:
            rec = by_name[name]
            rec["count"] += 1
            t = s.utc
            if (
                prev_name == name and prev_t is not None
                and t - prev_t <= step * 1.5
            ):
                rec["spans"][-1][1] = t
            else:
                rec["spans"].append([t, t])
        prev_name, prev_t = name, s.utc
    for rec in by_name.values():
        rec["intervals"] = [(a, b + step) for a, b in rec.pop("spans")]
    return by_name
