"""Deterministic hashing and the end-to-end generation pipeline."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta

from . import engine, geometry as G
from .schemas import (
    CheckReport, DateRange, DialResult, DSTEvent, GenerateOptions,
    GnomonInput, InvalidInterval, WallInput,
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
    """Hash of the engraved geometry only (name excluded)."""
    payload = {
        "lat": round(wall.latitude, 9),
        "lon": round(wall.longitude, 9),
        "std_offset": wall.standard_offset_minutes,
        "dst": wall.dst.model_dump(mode="json"),
        "azimuth": round(wall.azimuth, 9),
        "inclination": round(wall.inclination, 9),
        "panel": [(round(p.x, 6), round(p.y, 6)) for p in wall.panel],
    }
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
) -> str:
    """Hash of a search request; changes with every search parameter."""
    payload = {
        "v": ALGORITHM_VERSION,
        "wall_geom": geometry_hash(wall),
        "range": dr.model_dump(mode="json"),
        "direction": [round(c, 9) for c in direction],
        "normal_offset": round(normal_offset, 9),
        "search": search.model_dump(mode="json"),
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
    gnomon = G.Gnomon(
        base=(gnomon_input.base.x, gnomon_input.base.y),
        direction=tuple(gnomon_input.direction),
        length=gnomon_input.length,
        normal_offset=gnomon_input.normal_offset,
    )

    samples, tz, raw_gaps = engine.build_samples(
        wall, dr.start, dr.end, options.sample_minutes
    )
    evaluated = engine.evaluate_samples(samples, frame, gnomon, panel, options)
    lines = engine.build_lines(
        wall, evaluated, options, gnomon, frame, panel,
        options.sample_minutes, dr.start, dr.end, tz=tz, gaps=raw_gaps,
    )

    # invalid intervals (wall-lit daytime reasons + DST gaps)
    merged = engine.merge_intervals(
        evaluated, timedelta(minutes=options.sample_minutes)
    )
    invalid: list[InvalidInterval] = []
    for reason, a, b in merged:
        invalid.append(InvalidInterval(
            reason=reason,
            start_utc=a.strftime("%Y-%m-%dT%H:%M:%SZ"),
            end_utc=b.strftime("%Y-%m-%dT%H:%M:%SZ"),
            start_local=_format_local(a, tz),
            end_local=_format_local(b, tz),
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
        wall, frame, gnomon, samples, options
    )
    coverage["panel_usage"] = engine.panel_usage(lines, panel)

    svg = render_svg(wall, lines, boxes, checks, gnomon_input)

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
        svg=svg,
    )
