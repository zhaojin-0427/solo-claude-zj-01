"""Search gnomon base / length / label-layout combinations.

The search reuses one shared sample set (sun positions depend only on place,
not on the gnomon) and evaluates each combination cheaply. Candidates are
ranked lexicographically by:

1. readable-time coverage (fraction of sunlit samples landing in panel),
2. minimum spacing quality,
3. panel occupancy (engraved density),

hard problems (spacing below the threshold, label collisions, points
crossing the kept margin) demote a candidate below any clean one.
"""

from __future__ import annotations

import math

from . import engine, geometry as G
from .schemas import (
    DateRange, GenerateOptions, Point2, SearchCandidate, SearchOptions,
    WallInput,
)
from .service import generate_dial


def _as_gnomon_model(base, direction, length, normal_offset):
    from .schemas import GnomonInput
    return GnomonInput(
        base=Point2(x=base[0], y=base[1]),
        direction=tuple(direction),
        length=length,
        normal_offset=normal_offset,
    )


def search_candidates(
    wall: WallInput,
    dr: DateRange,
    direction: tuple[float, float, float],
    normal_offset: float,
    search: SearchOptions,
    full_top: int = 3,
) -> tuple[list[SearchCandidate], int]:
    frame = G.make_frame(wall.azimuth, wall.inclination)
    panel = [(p.x, p.y) for p in wall.panel]
    d_hat = G.normalize(tuple(direction))
    offsets = [(o.x, o.y) for o in search.label_offsets]

    n_combos = len(search.candidate_lengths) * len(offsets)
    base_cap = max(8, math.ceil(1200 / n_combos))
    bases = G.grid_points_inside(
        panel, search.base_grid_step,
        max(search.margin, 1e-9), base_cap,
    )

    coarse = GenerateOptions(
        time_mode=search.time_mode,
        hours=list(search.hours),
        season_dates=list(search.season_dates),
        sample_minutes=search.search_sample_minutes,
        check_min_spacing=search.min_spacing,
    )
    samples, tz, raw_gaps = engine.build_samples(
        wall, dr.start, dr.end, search.search_sample_minutes
    )

    combos = []
    for base in bases:
        for length in search.candidate_lengths:
            gnomon = G.Gnomon(base, d_hat, length, normal_offset)
            evaluated = engine.evaluate_samples(
                samples, frame, gnomon, panel, coarse
            )
            ok_points = [e.point for e in evaluated if e.status == engine.OK]
            above = [e for e in evaluated
                     if e.sample.alt_deg > 0]
            # Wall-lit = sun above horizon AND ray actually reaches the wall
            # plane (not behind / parallel). Readable coverage is measured
            # against the times the wall is lit, which is the quantity the
            # dial maker can actually use.
            wall_lit = [
                e for e in above
                if e.status in (engine.OK, engine.OUTSIDE)
            ]
            # primary ranking metric: readable share of wall-lit time
            coverage = (
                len(ok_points) / len(wall_lit) if wall_lit else 0.0
            )
            readable_of_lit = coverage
            # margin gate: every readable point must respect the margin.
            # A base with NO readable points is never acceptable
            # (all(... on empty) is vacuously true), so guard explicitly.
            lines = engine.build_lines(
                wall, evaluated, coarse, gnomon, frame, panel,
                search.search_sample_minutes, dr.start, dr.end,
                tz=tz, gaps=raw_gaps,
            )
            has_lines = any(
                seg for line in lines for seg in line.segments
            )
            if not ok_points:
                margin_ok = False
            else:
                margin_ok = all(
                    G.point_in_eroded_polygon(p, panel, search.margin)
                    for p in ok_points
                )

            min_sp, sp_issues = engine.check_spacing(lines, search.min_spacing)
            spacing_ok = len(sp_issues) == 0
            usage = engine.panel_usage(lines, panel)

            for loff in offsets:
                boxes = engine.layout_labels(lines, loff, coarse.label_font_size)
                overlaps = engine.check_labels(boxes)
                label_viol = len(overlaps) + sum(
                    0 if G.point_in_eroded_polygon(
                        (b.x, b.y), panel, search.margin
                    ) else 1
                    for b in boxes
                )
                # Quality tier reflects the required ordering:
                # readable coverage first, then spacing, margin and labels.
                # Only truly unusable designs (no readable time at all) are
                # demoted to the bottom tier.
                if coverage <= 0.0 or not has_lines:
                    tier = 9
                elif not spacing_ok:
                    tier = 2
                elif not margin_ok or label_viol:
                    tier = 1
                else:
                    tier = 0
                penalties = tier
                combos.append({
                    "base": base,
                    "length": length,
                    "label_offset": loff,
                    "coverage": round(coverage, 4),
                    "readable_coverage": round(readable_of_lit, 4),
                    "min_spacing": min_sp,
                    "spacing_ok": spacing_ok,
                    "panel_usage": usage,
                    "label_overlaps": len(overlaps),
                    "margin_ok": margin_ok,
                    "penalties": penalties,
                })

    # deterministic ordering
    combos.sort(key=lambda c: (
        c["penalties"],
        -c["coverage"],
        -(c["min_spacing"] or -1.0),
        -c["panel_usage"],
        round(c["base"][0], 6), round(c["base"][1], 6),
        c["length"], c["label_offset"][0], c["label_offset"][1],
    ))

    total = len(combos)
    out: list[SearchCandidate] = []
    for rank, c in enumerate(combos[:max(full_top, 10)], start=1):
        result = None
        if rank <= full_top:
            gnomon_model = _as_gnomon_model(
                c["base"], d_hat, c["length"], normal_offset
            )
            full_options = GenerateOptions(
                time_mode=search.time_mode,
                hours=list(search.hours),
                season_dates=list(search.season_dates),
            )
            result = generate_dial(
                wall, gnomon_model, dr, full_options,
                label_offset=c["label_offset"],
            )
        out.append(SearchCandidate(
            rank=rank,
            base=Point2(x=round(c["base"][0], 5), y=round(c["base"][1], 5)),
            length=c["length"],
            label_offset=Point2(x=c["label_offset"][0],
                                y=c["label_offset"][1]),
            coverage=c["coverage"],
            readable_coverage=c["readable_coverage"],
            min_spacing=c["min_spacing"],
            spacing_ok=c["spacing_ok"],
            panel_usage=c["panel_usage"],
            label_overlaps=c["label_overlaps"],
            margin_ok=c["margin_ok"],
            score=[c["penalties"], c["coverage"],
                   c["min_spacing"] or 0.0, c["panel_usage"]],
            result=result,
        ))
    return out, total
