"""Nearby-building / eaves / evergreen obstruction profiles.

An obstruction profile is a *named* piecewise-linear skyline given by
``(solar azimuth, solar altitude)`` control points: at any azimuth covered
by the profile the Sun is hidden whenever its altitude is at or below the
interpolated profile altitude.  Azimuths use the same convention as the
solar engine (degrees clockwise from north, ``0 <= az < 360``).

Profiles come in two flavours:

* open (``wrap=False``): the skyline is defined between the first and last
  control point only — there is no obstacle outside that azimuth span;
* closed (``wrap=True``): the profile is a skyline ring.  The segment
  between the last and first control point crosses 0° (north), i.e. the
  profile covers the whole azimuth circle and interpolation across the
  seam wraps through 360°/0°.

All interpolation is linear in angle space and fully deterministic.
"""

from __future__ import annotations

import bisect
import math
from dataclasses import dataclass

RAD = math.pi / 180.0


@dataclass(frozen=True)
class ObstacleHit:
    """Result of comparing one solar position against the skyline set."""

    name: str
    profile_altitude_deg: float
    margin_deg: float  # profile altitude minus sun altitude (>= 0 when blocked)


@dataclass(frozen=True)
class CompiledContour:
    name: str
    wrap: bool
    az: list[float]       # sorted control-point azimuths, strictly increasing
    alt: list[float]      # matching altitudes
    az_ext: list[float]  # az with wrap-around points (wrap mode only)
    alt_ext: list[float]

    def altitude_at(self, azimuth_deg: float) -> float | None:
        """Piecewise-linear profile altitude at the given azimuth.

        Returns ``None`` where the open profile does not cover the azimuth.
        """
        az = azimuth_deg % 360.0
        if not self.wrap:
            if az < self.az[0] or az > self.az[-1]:
                return None
            return _interp(az, self.az, self.alt)
        return _interp(az, self.az_ext, self.alt_ext)


def _interp(x: float, xs: list[float], ys: list[float]) -> float:
    """Linear interpolation on a strictly increasing grid (x in range)."""
    if x <= xs[0]:
        return ys[0]
    if x >= xs[-1]:
        return ys[-1]
    k = bisect.bisect_right(xs, x) - 1
    x0, x1 = xs[k], xs[k + 1]
    t = (x - x0) / (x1 - x0)
    return ys[k] + t * (ys[k + 1] - ys[k])


def compile_contours(wall) -> list[CompiledContour]:
    """Compile the validated ``wall.obstacles`` models into lookup tables."""
    out: list[CompiledContour] = []
    for ob in getattr(wall, "obstacles", None) or []:
        pts = sorted(ob.points, key=lambda p: p.azimuth_deg)
        az = [p.azimuth_deg for p in pts]
        alt = [p.altitude_deg for p in pts]
        if ob.wrap:
            # Repeat the end points across the seam so that interpolation
            # on [last, 360] and [0, first] follows the closing segment.
            az_ext = [az[-1] - 360.0] + az + [az[0] + 360.0]
            alt_ext = [alt[-1]] + alt + [alt[0]]
        else:
            az_ext, alt_ext = az, alt
        out.append(CompiledContour(
            name=ob.name, wrap=ob.wrap, az=az, alt=alt,
            az_ext=az_ext, alt_ext=alt_ext,
        ))
    return out


def blocking_obstacle(
    profiles: list[CompiledContour],
    azimuth_deg: float,
    altitude_deg: float,
) -> ObstacleHit | None:
    """First (highest) profile hiding the Sun at this position.

    The Sun is hidden when its altitude is at or below a profile's
    interpolated altitude.  The profile reaching highest wins; ties keep
    the contour's (request) order, which is deterministic.
    """
    best: ObstacleHit | None = None
    for prof in profiles:
        h = prof.altitude_at(azimuth_deg)
        if h is None or altitude_deg > h:
            continue
        margin = h - altitude_deg
        if best is None or h > best.profile_altitude_deg:
            best = ObstacleHit(
                name=prof.name,
                profile_altitude_deg=h,
                margin_deg=margin,
            )
    return best


def sun_faces_wall(sun_enu, frame, eps: float) -> bool:
    """Whether the wall is lit: ``s·n`` clears the parallel-ray threshold."""
    from . import geometry as G
    return G.dot(sun_enu, frame.normal) >= eps
