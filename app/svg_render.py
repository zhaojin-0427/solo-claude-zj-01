"""Deterministic SVG engraving-plan renderer.

Panel metres map directly to SVG user units (1 m = 200 units) with the
upward panel axis flipped to SVG's downward axis. The renderer never
invents geometry: it draws only what the engine produced.
"""

from __future__ import annotations

from xml.sax.saxutils import escape

from . import geometry as G
from .schemas import CheckReport, DialLine, GnomonInput, LabelBox, WallInput

SCALE = 200.0          # user units per metre
MARGIN = 60.0          # canvas padding
HOUR_COLOR = "#163a63"
SEASON_COLOR = "#a8531f"
PANEL_FILL = "#f8f4e9"
PANEL_STROKE = "#5b5347"
BASE_COLOR = "#b3261e"
LABEL_COLOR = "#222222"
WARN_COLOR = "#9a6a00"


def _x(xm: float) -> float:
    return MARGIN + xm * SCALE


def _y(ym: float, height_m: float) -> float:
    return MARGIN + (height_m - ym) * SCALE


def render_svg(
    wall: WallInput,
    lines: list[DialLine],
    labels: list[LabelBox],
    checks: CheckReport,
    gnomon: GnomonInput,
    obstacle_report=None,
) -> str:
    poly = [(p.x, p.y) for p in wall.panel]
    minx, miny, maxx, maxy = G.polygon_bbox(poly)
    width_m = maxx - minx
    height_m = maxy - miny
    extra_strip = 22.0 if obstacle_report is not None else 0.0
    W = width_m * SCALE + 2 * MARGIN
    H = height_m * SCALE + 2 * MARGIN + 120 + extra_strip  # legend strip

    def P(x, y):
        return f"{_x(x - minx):.2f},{_y(y - miny, height_m):.2f}"

    out: list[str] = []
    out.append(
        f'<svg xmlns="http://www.w3.org/2000/svg" '
        f'viewBox="0 0 {W:.1f} {H:.1f}" '
        f'font-family="Helvetica, Arial, sans-serif">'
    )
    out.append(f'<rect width="100%" height="100%" fill="#ffffff"/>')
    out.append(
        f'<text x="{MARGIN}" y="28" font-size="20" fill="{LABEL_COLOR}">'
        f"{escape(wall.name)} — sundial engraving plan</text>"
    )
    out.append(
        f'<text x="{MARGIN}" y="48" font-size="12" fill="#666">'
        f"lat {wall.latitude:.4f}°, lon {wall.longitude:.4f}°, "
        f"wall az {wall.azimuth:.1f}°, tilt {wall.inclination:.1f}° "
        f"(metres)</text>"
    )

    # panel outline
    pts = " ".join(P(x, y) for x, y in poly)
    out.append(
        f'<polygon points="{pts}" fill="{PANEL_FILL}" '
        f'stroke="{PANEL_STROKE}" stroke-width="2"/>'
    )

    # hour + season polylines
    def draw_line(line: DialLine, color: str, width: float, dash: str = ""):
        dash_attr = f' stroke-dasharray="{dash}"' if dash else ""
        for seg in line.segments:
            if len(seg) == 1:
                x, y = seg[0].x, seg[0].y
                out.append(
                    f'<circle cx="{_x(x-minx):.2f}" cy="{_y(y-miny,height_m):.2f}"'
                    f' r="{width*1.4:.1f}" fill="{color}"/>'
                )
                continue
            d = "M " + " L ".join(P(p.x, p.y) for p in seg)
            out.append(
                f'<path d="{d}" fill="none" stroke="{color}" '
                f'stroke-width="{width}"{dash_attr} stroke-linecap="round"/>'
            )

    for line in lines:
        if line.kind == "hour":
            draw_line(line, HOUR_COLOR, 1.6)
    for line in lines:
        if line.kind == "season":
            draw_line(line, SEASON_COLOR, 1.2, dash="6 4")

    # gnomon base + projection marker
    bx, by = gnomon.base.x, gnomon.base.y
    out.append(
        f'<circle cx="{_x(bx-minx):.2f}" cy="{_y(by-miny,height_m):.2f}" '
        f'r="4" fill="{BASE_COLOR}"/>'
    )
    out.append(
        f'<text x="{_x(bx-minx)+8:.2f}" y="{_y(by-miny,height_m)-6:.2f}" '
        f'font-size="11" fill="{BASE_COLOR}">nodus base</text>'
    )

    # labels (rotated with the local stroke angle; SVG y is flipped)
    fs = 11
    for b in labels:
        transform = (
            f"rotate({-b.angle_deg:.2f} "
            f"{_x(b.x-minx):.2f} {_y(b.y-miny,height_m):.2f})"
        )
        out.append(
            f'<text x="{_x(b.x-minx):.2f}" y="{_y(b.y-miny,height_m):.2f}"'
            f' font-size="{fs}" fill="{LABEL_COLOR}" '
            f'transform="{transform}">{escape(b.line_label)}</text>'
        )

    # legend / status strip
    ly = H - 78
    out.append(
        f'<line x1="{MARGIN}" y1="{ly-12}" x2="{W-MARGIN}" y2="{ly-12}" '
        f'stroke="#ccc"/>'
    )
    legends = [
        (HOUR_COLOR, "hour lines"),
        (SEASON_COLOR, "season date lines"),
        (BASE_COLOR, "gnomon base"),
    ]
    x = MARGIN
    for color, name in legends:
        out.append(
            f'<line x1="{x:.1f}" y1="{ly:.1f}" x2="{x+24:.1f}" y2="{ly:.1f}"'
            f' stroke="{color}" stroke-width="3"/>'
        )
        out.append(
            f'<text x="{x+30:.1f}" y="{ly+4:.1f}" font-size="12" '
            f'fill="{LABEL_COLOR}">{escape(name)}</text>'
        )
        x += 30 + 8 * len(name)

    # quality summary
    broken = len(checks.broken_lines)
    spacing = len(checks.spacing_issues)
    overlap = len(checks.label_overlaps)
    dstn = len(checks.dst_events)
    margin_n = len(checks.margin_violations)
    summary = (
        f"broken lines: {broken} | spacing violations: {spacing} | "
        f"label overlaps: {overlap} | DST transitions in range: {dstn} | "
        f"margin violations: {margin_n} | min line spacing: "
        f"{checks.min_spacing if checks.min_spacing is not None else 'n/a'} m"
    )
    out.append(
        f'<text x="{MARGIN}" y="{ly+30}" font-size="12" fill="{WARN_COLOR}">'
        f"{escape(summary)}</text>"
    )
    if checks.dst_gap_intervals:
        gaps = "; ".join(
            f"{g.start_local[5:]}–{g.end_local[5:]} ({g.reason})"
            for g in checks.dst_gap_intervals
        )
        out.append(
            f'<text x="{MARGIN}" y="{ly+50}" font-size="11" fill="#777">'
            f"unreachable clock times: {escape(gaps)}</text>"
        )
    if obstacle_report is not None:
        # segments are already split at blocked spans; the summary names
        # the skylines responsible and the total hidden wall-lit time
        detail = "; ".join(
            f"{escape(l.name)} {l.blocked_minutes:.0f} min / "
            f"{l.blocked_samples} samples"
            for l in obstacle_report.losses if l.blocked_samples
        ) or "no blocking samples in range"
        out.append(
            f'<text x="{MARGIN}" y="{ly+68 + (0 if checks.dst_gap_intervals else -18)}"'
            f' font-size="11" fill="#7a3b00">'
            f"blocked by obstacle ({obstacle_report.blocked_minutes:.0f} min "
            f"total): {detail}</text>"
        )
    out.append("</svg>")
    return "".join(out)
