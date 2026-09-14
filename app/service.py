"""Deterministic hashing and the end-to-end generation pipeline."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta

from . import engine, geometry as G
from .obstacles import compile_contours
from .schemas import (
    CheckReport, DateRange, DialResult, DSTEvent, GenerateOptions,
    GnomonInput, InvalidInterval, ObstacleLoss, ObstacleReport, WallInput,
)
from .svg_render import render_svg
from .timezone_engine import TimezoneEngine

ALGORITHM_VERSION = "noaa-fixed-v1"


def canonical_json(obj) -> str:
    if hasattr(obj, "model_dump"):
        obj = obj.model_dump(mode="json")
    return json.dumps(obj, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False)


def sha256_text(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


def geometry_hash(wall: WallInput) -> str:
    """Hash of the engraved geometry only (name excluded).

    Named obstruction skylines are part of the frozen wall geometry. The
    key is only added when a wall actually carries profiles, so walls
    without obstacles keep the same hash as before.
    """
    payload = {
        "lat": round(wall.latitude, 9),
        "lon": round(wall.longitude, 9),
        "std_offset": wall.standard_offset_minutes,
        "dst": wall.dst.model_dump(mode="json"),
        "azimuth": round(wall.azimuth, 9),
        "inclination": round(wall.inclination, 9),
        "panel": [(round(p.x, 6), round(p.y, 6)) for p in wall.panel],
    }
    if wall.obstacles:
        payload["obstacles"] = [
            {
                "name": ob.name,
                "wrap": ob.wrap,
                "points": [(round(p.azimuth_deg, 6),
                            round(p.altitude_deg, 6)) for p in ob.points],
            }
            for ob in wall.obstacles
        ]
    return sha256_text(canonical_json(payload))[:16]


def input_hash(wall: WallInput, gnomon: GnomonInput, dr: DateRange,
               options: GenerateOptions, label_offset=(0.0, 0.0)) -> str:
    payload = {
        "v": ALGORITHM_VERSION,
        "wall_geom": geometry_hash(wall),
        "gnomon": gnomon.model_dump(mode="json"),
        "range": dr.model_dump(mode="json"),
        "options": options.model_dump(mode="json"),
        "label_offset": [round(label_offset[0], 6), round(label_offset[1], 6)],
    }
    return sha256_text(canonical_json(payload))


def search_input_hash(
    wall: WallInput,
    dr: DateRange,
    direction: tuple[float, float, float],
    normal_offset: float,
    search,
    full_top: int = 3,
) -> str:
    """Hash of a search request.

    Every parameter that changes the response payload is included: in
    particular ``full_top`` controls how many candidates carry the full
    embedded :class:`DialResult`, so requests differing only in ``full_top``
    must hash differently.
    """
    payload = {
        "v": ALGORITHM_VERSION,
        "wall_geom": geometry_hash(wall),
        "range": dr.model_dump(mode="json"),
        "direction": [round(c, 9) for c in direction],
        "normal_offset": round(normal_offset, 9),
        "search": search.model_dump(mode="json"),
        "full_top": int(full_top),
    }
    return sha256_text(canonical_json(payload))


# ------------------------------------------------------------- pipeline --


def _format_local(dt: datetime, tz: TimezoneEngine) -> str:
    local, offset, dst = tz.local_clock(dt)
    sign = "+" if offset >= 0 else "-"
    oh, om = divmod(abs(offset), 60)
    return f"{local.strftime('%Y-%m-%dT%H:%M')} UTC{sign}{oh:02d}:{om:02d}{'/DST' if dst else ''}"


def _dst_events(tz: TimezoneEngine, d0, d1) -> list[DSTEvent]:
    start = datetime.combine(d0, datetime.min.time()) - timedelta(days=1)
    end = datetime.combine(d1, datetime.min.time()) + timedelta(days=2)
    out = []
    for t in tz.transitions_between(start, end):
        delta = t.offset_after - t.offset_before
        if t.kind == "start":
            before = t.utc + timedelta(minutes=t.offset_before)
            after = before + timedelta(minutes=delta)
            gap = f"{before:%H:%M}–{after:%H:%M}"
            overlap = ""
        else:
            before = t.utc + timedelta(minutes=t.offset_before)
            after = before - timedelta(minutes=delta)
            gap = ""
            overlap = f"{after:%H:%M}–{before:%H:%M}"
        out.append(DSTEvent(
            kind=t.kind,
            utc=t.utc.strftime("%Y-%m-%dT%H:%M:%SZ"),
            local_before=before.strftime("%Y-%m-%dT%H:%M"),
            local_after=(before + timedelta(minutes=delta)).strftime(
                "%Y-%m-%dT%H:%M") if t.kind == "start"
            else after.strftime("%Y-%m-%dT%H:%M"),
            gap_local=gap,
            overlap_local=overlap,
        ))
    return out


def generate_dial(
    wall: WallInput,
    gnomon_input: GnomonInput,
    dr: DateRange,
    options: GenerateOptions | None = None,
    label_offset: G.Pt = (0.0, 0.0),
) -> DialResult:
    options = options or GenerateOptions()
    frame = G.make_frame(wall.azimuth, wall.inclination)
    panel = [(p.x, p.y) for p in wall.panel]
    profiles = compile_contours(wall)
    gnomon = G.Gnomon(
        base=(gnomon_input.base.x, gnomon_input.base.y),
        direction=tuple(gnomon_input.direction),
        length=gnomon_input.length,
        normal_offset=gnomon_input.normal_offset,
    )

    samples, tz, raw_gaps = engine.build_samples(
        wall, dr.start, dr.end, options.sample_minutes
    )
    evaluated = engine.evaluate_samples(
        samples, frame, gnomon, panel, options, profiles
    )
    lines = engine.build_lines(
        wall, evaluated, options, gnomon, frame, panel,
        options.sample_minutes, dr.start, dr.end, tz=tz, gaps=raw_gaps,
        profiles=profiles,
    )

    # invalid intervals (wall-lit daytime reasons + DST gaps)
    merged = engine.merge_intervals(
        evaluated, timedelta(minutes=options.sample_minutes)
    )
    invalid: list[InvalidInterval] = []
    for reason, ob_name, a, b in merged:
        invalid.append(InvalidInterval(
            reason=reason,
            start_utc=a.strftime("%Y-%m-%dT%H:%M:%SZ"),
            end_utc=b.strftime("%Y-%m-%dT%H:%M:%SZ"),
            start_local=_format_local(a, tz),
            end_local=_format_local(b, tz),
            obstacle=ob_name,
        ))
    for ga, gb in raw_gaps:
        invalid.append(InvalidInterval(
            reason="dst_gap",
            start_utc=(ga - timedelta(minutes=tz.std)).strftime(
                "%Y-%m-%dT%H:%M:%SZ"),
            end_utc=(gb - timedelta(minutes=tz.std + tz.dst_delta)).strftime(
                "%Y-%m-%dT%H:%M:%SZ"),
            start_local=f"{ga:%Y-%m-%dT%H:%M} (wall clock, nonexistent)",
            end_local=f"{gb:%Y-%m-%dT%H:%M} (wall clock, nonexistent)",
        ))
    invalid.sort(key=lambda v: v.start_utc)

    # labels and checks
    boxes = engine.layout_labels(
        lines, label_offset, options.label_font_size
    )
    min_sp, spacing_issues = engine.check_spacing(
        lines, options.check_min_spacing
    )
    overlaps = engine.check_labels(boxes)
    broken = [{"line": l.label, "segments": len(l.segments),
               "ok_samples": l.ok_count} for l in lines if l.broken]
    margin_viol = engine.check_margins(lines, boxes, panel, margin=0.0)
    checks = CheckReport(
        broken_lines=broken,
        min_spacing=min_sp,
        spacing_issues=spacing_issues,
        label_overlaps=overlaps,
        dst_events=_dst_events(tz, dr.start, dr.end),
        dst_gap_intervals=[v for v in invalid if v.reason == "dst_gap"],
        margin_violations=margin_viol,
    )

    coverage = engine.coverage_sweep(
        wall, frame, gnomon, samples, options, profiles
    )
    coverage["panel_usage"] = engine.panel_usage(lines, panel)

    obstacle_report = _obstacle_report(
        samples, frame, profiles,
        timedelta(minutes=options.sample_minutes), tz,
        options.parallel_cos_threshold,
    )

    svg = render_svg(wall, lines, boxes, checks, gnomon_input,
                     obstacle_report=obstacle_report)

    ghash = geometry_hash(wall)
    ihash = input_hash(wall, gnomon_input, dr, options, label_offset)
    return DialResult(
        input_hash=ihash,
        geometry_hash=ghash,
        algorithm_version=ALGORITHM_VERSION,
        mode=options.time_mode,
        lines=lines,
        invalid_intervals=invalid,
        checks=checks,
        labels=boxes,
        coverage=coverage,
        obstacle_report=obstacle_report,
        svg=svg,
    )


def _obstacle_report(samples, frame, profiles, step, tz, eps) -> ObstacleReport | None:
    """Aggregate blocked samples/spans per named contour on the shared grid."""
    if not profiles:
        return None
    loss = engine.obstacle_loss_sweep(samples, frame, profiles, step, eps)
    losses: list[ObstacleLoss] = []
    total_samples = 0
    total_minutes = 0.0
    step_min = step.total_seconds() / 60.0
    for prof in profiles:
        rec = loss[prof.name]
        intervals = [
            InvalidInterval(
                reason=engine.BLOCKED,
                start_utc=a.strftime("%Y-%m-%dT%H:%M:%SZ"),
                end_utc=b.strftime("%Y-%m-%dT%H:%M:%SZ"),
                start_local=_format_local(a, tz),
                end_local=_format_local(b, tz),
                obstacle=prof.name,
            )
            for a, b in rec["intervals"]
        ]
        minutes = rec["count"] * step_min
        total_samples += rec["count"]
        total_minutes += minutes
        losses.append(ObstacleLoss(
            name=prof.name, blocked_samples=rec["count"],
            blocked_minutes=round(minutes, 2), intervals=intervals,
        ))
    return ObstacleReport(
        blocked_samples=total_samples,
        blocked_minutes=round(total_minutes, 2),
        losses=losses,
    )
