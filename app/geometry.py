"""Wall-frame geometry, shadow intersection and planar utilities.

Frames
------
Local world frame is ENU = (east, north, up). The wall panel is described by:

* ``azimuth``  A: direction the panel faces, degrees clockwise from north;
* ``inclination`` i: 0 = vertical wall, 90 = horizontal skylight,
  negative = overhanging panel;
* panel polygon vertices ``(x, y)`` in metres: +x to the right of an
  observer facing the wall, +y upward on the panel.

The outward unit normal, panel-right and panel-up axes in ENU are::

    n = (cos i sin A, cos i cos A, sin i)
    r = (cos A, -sin A, 0)
    u = r x n = (-sin i sin A, -sin i cos A, cos i)

so that ``u x r = n`` (right/up/out is right-handed).

The nodus (gnomon tip) is at ``base3 + length * dir_hat + normal_offset * n``.
Sunlight travels along ``-s`` where ``s`` is the unit vector toward the Sun;
the shadow point solves ``(P - t s) . n = 0``, hence ``t = P.n / s.n``.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

Vec3 = tuple[float, float, float]
Pt = tuple[float, float]

RAD = math.pi / 180.0
_EPS = 1e-12


# ------------------------------------------------------------- vectors --


def _add(a: Vec3, b: Vec3) -> Vec3:
    return (a[0] + b[0], a[1] + b[1], a[2] + b[2])


def _scale(a: Vec3, k: float) -> Vec3:
    return (a[0] * k, a[1] * k, a[2] * k)


def dot(a: Vec3, b: Vec3) -> float:
    return a[0] * b[0] + a[1] * b[1] + a[2] * b[2]


def cross(a: Vec3, b: Vec3) -> Vec3:
    return (
        a[1] * b[2] - a[2] * b[1],
        a[2] * b[0] - a[0] * b[2],
        a[0] * b[1] - a[1] * b[0],
    )


def norm(a: Vec3) -> float:
    return math.sqrt(dot(a, a))


def normalize(a: Vec3) -> Vec3:
    n = norm(a)
    if n < _EPS:
        raise ValueError("zero vector")
    return _scale(a, 1.0 / n)


# --------------------------------------------------------- wall frames --


@dataclass(frozen=True)
class WallFrame:
    azimuth: float
    inclination: float
    right: Vec3   # panel +x in ENU
    up: Vec3      # panel +y in ENU
    normal: Vec3  # outward in ENU

    def to_world(self, p: Pt) -> Vec3:
        return _add(_scale(self.right, p[0]), _scale(self.up, p[1]))

    def to_panel(self, v: Vec3) -> Pt:
        return (dot(v, self.right), dot(v, self.up))


def make_frame(azimuth_deg: float, inclination_deg: float) -> WallFrame:
    a = azimuth_deg * RAD
    i = inclination_deg * RAD
    ca, sa, ci, si = math.cos(a), math.sin(a), math.cos(i), math.sin(i)
    n = (ci * sa, ci * ca, si)
    r = (ca, -sa, 0.0)
    u = cross(r, n)
    return WallFrame(azimuth_deg, inclination_deg, r, u, n)


# ------------------------------------------------------------- gnomon ---


@dataclass(frozen=True)
class Gnomon:
    base: Pt
    direction: Vec3
    length: float
    normal_offset: float

    def tip(self, frame: WallFrame) -> Vec3:
        d = normalize(self.direction)
        p = self.to_world_base(frame)
        p = _add(p, _scale(d, self.length))
        p = _add(p, _scale(frame.normal, self.normal_offset))
        return p

    def to_world_base(self, frame: WallFrame) -> Vec3:
        return frame.to_world(self.base)


# --------------------------------------------------------- intersection --


@dataclass(frozen=True)
class ShadowHit:
    point: Pt | None
    status: str  # ok | sun_behind_wall | shadow_parallel | nodus_in_wall
    sdotn: float


def shadow_intersection(
    frame: WallFrame, tip: Vec3, sun_enu: Vec3, parallel_eps: float
) -> ShadowHit:
    sdotn = dot(sun_enu, frame.normal)
    pdotn = dot(tip, frame.normal)
    if pdotn <= _EPS:
        return ShadowHit(None, "nodus_in_wall", sdotn)
    if sdotn <= 0.0:
        return ShadowHit(None, "sun_behind_wall", sdotn)
    if sdotn < parallel_eps:
        return ShadowHit(None, "shadow_parallel", sdotn)
    t = pdotn / sdotn
    hit_world = _add(tip, _scale(sun_enu, -t))
    return ShadowHit(frame.to_panel(hit_world), "ok", sdotn)


# ----------------------------------------------------------- polygons ---


def point_in_polygon(p: Pt, poly: list[Pt]) -> bool:
    x, y = p
    inside = False
    j = len(poly) - 1
    for i in range(len(poly)):
        xi, yi = poly[i]
        xj, yj = poly[j]
        if (yi > y) != (yj > y) and x < (xj - xi) * (y - yi) / (yj - yi) + xi:
            inside = not inside
        j = i
    return inside


def polygon_area(poly: list[Pt]) -> float:
    s = 0.0
    for i in range(len(poly)):
        x1, y1 = poly[i]
        x2, y2 = poly[(i + 1) % len(poly)]
        s += x1 * y2 - x2 * y1
    return abs(s) * 0.5


def polygon_bbox(poly: list[Pt]) -> tuple[float, float, float, float]:
    xs = [p[0] for p in poly]
    ys = [p[1] for p in poly]
    return min(xs), min(ys), max(xs), max(ys)


def distance_to_segment(p: Pt, a: Pt, b: Pt) -> float:
    px, py = p
    ax, ay = a
    vx, vy = b[0] - ax, b[1] - ay
    l2 = vx * vx + vy * vy
    if l2 < _EPS:
        return math.hypot(px - ax, py - ay)
    t = max(0.0, min(1.0, ((px - ax) * vx + (py - ay) * vy) / l2))
    qx, qy = ax + t * vx, ay + t * vy
    return math.hypot(px - qx, py - qy)


def distance_to_polygon_boundary(p: Pt, poly: list[Pt]) -> float:
    return min(
        distance_to_segment(p, poly[i], poly[(i + 1) % len(poly)])
        for i in range(len(poly))
    )


def point_in_eroded_polygon(p: Pt, poly: list[Pt], margin: float) -> bool:
    if not point_in_polygon(p, poly):
        return False
    if margin <= 0:
        return True
    return distance_to_polygon_boundary(p, poly) >= margin


def grid_points_inside(
    poly: list[Pt], step: float, margin: float, cap: int
) -> list[Pt]:
    """Deterministic grid of candidate base points inside the eroded panel."""
    minx, miny, maxx, maxy = polygon_bbox(poly)
    nx = max(1, int(math.floor((maxx - minx) / step)) + 1)
    ny = max(1, int(math.floor((maxy - miny) / step)) + 1)
    pts: list[Pt] = []
    for iy in range(ny):
        for ix in range(nx):
            p = (minx + margin + ix * step, miny + margin + iy * step)
            if point_in_eroded_polygon(p, poly, margin):
                pts.append(p)
    if len(pts) > cap:
        stride = len(pts) / cap
        pts = [pts[int(k * stride)] for k in range(cap)]
    return pts


# ------------------------------------------------------- point metrics --


def polyline_length(pts: list[Pt]) -> float:
    return sum(
        math.hypot(pts[i + 1][0] - pts[i][0], pts[i + 1][1] - pts[i][1])
        for i in range(len(pts) - 1)
    )


def convex_hull(points: list[Pt]) -> list[Pt]:
    """Andrew's monotone chain."""
    pts = sorted(set(points))
    if len(pts) <= 1:
        return pts

    def cross2(o: Pt, a: Pt, b: Pt) -> float:
        return (a[0] - o[0]) * (b[1] - o[1]) - (a[1] - o[1]) * (b[0] - o[0])

    lower: list[Pt] = []
    for p in pts:
        while len(lower) >= 2 and cross2(lower[-2], lower[-1], p) <= 0:
            lower.pop()
        lower.append(p)
    upper: list[Pt] = []
    for p in reversed(pts):
        while len(upper) >= 2 and cross2(upper[-2], upper[-1], p) <= 0:
            upper.pop()
        upper.append(p)
    return lower[:-1] + upper[:-1]


def segment_distance(a: Pt, b: Pt, c: Pt, d: Pt) -> float:
    """Minimum distance between two segments."""
    def clamp(x, lo, hi):
        return max(lo, min(hi, x))

    r = (b[0] - a[0], b[1] - a[1])
    s = (d[0] - c[0], d[1] - c[1])
    rlen2 = r[0] ** 2 + r[1] ** 2
    slen2 = s[0] ** 2 + s[1] ** 2
    rxs = r[0] * s[1] - r[1] * s[0]

    if abs(rxs) > _EPS:
        qp = (c[0] - a[0], c[1] - a[1])
        t = clamp((qp[0] * s[1] - qp[1] * s[0]) / rxs, 0.0, 1.0)
        u = (qp[0] * r[1] - qp[1] * r[0]) / rxs
        if 0.0 <= u <= 1.0:
            return math.hypot(a[0] + t * r[0] - (c[0] + u * s[0]),
                              a[1] + t * r[1] - (c[1] + u * s[1]))
    return min(
        distance_to_segment(a, c, d), distance_to_segment(b, c, d),
        distance_to_segment(c, a, b), distance_to_segment(d, a, b),
    )


def obb_corners(cx: float, cy: float, w: float, h: float,
                angle_deg: float) -> list[Pt]:
    a = angle_deg * RAD
    ca, sa = math.cos(a), math.sin(a)
    corners = []
    for dx, dy in ((-w / 2, -h / 2), (w / 2, -h / 2),
                   (w / 2, h / 2), (-w / 2, h / 2)):
        corners.append((cx + dx * ca - dy * sa, cy + dx * sa + dy * ca))
    return corners


def polygons_overlap(a: list[Pt], b: list[Pt]) -> bool:
    """SAT test for two convex polygons."""
    for poly in (a, b):
        for i in range(len(poly)):
            p, q = poly[i], poly[(i + 1) % len(poly)]
            nx, ny = -(q[1] - p[1]), q[0] - p[0]
            pa = [x * nx + y * ny for x, y in a]
            pb = [x * nx + y * ny for x, y in b]
            if max(pa) < min(pb) or max(pb) < min(pa):
                return False
    return True
