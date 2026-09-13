"""Fixed solar-position algorithm (NOAA Solar Calculations, low accuracy).

No network calls. Accuracy ~0.3-0.5 degrees for 1900-2100, which is ample
for engraving a wall dial.

Reference: NOAA Earth System Research Laboratories, Solar Position Algorithm
spreadsheet (the standard "truncated" formula set),
https://gml.noaa.gov/grad/solcalc/calcdetails.html
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

RAD = math.pi / 180.0
DEG = 180.0 / math.pi
J1970 = 2440588.0
DAY_S = 86400.0


def julian_day(dt: datetime) -> float:
    """Julian day for a UTC ``datetime`` (naive values are assumed UTC)."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    epoch = datetime(1970, 1, 1, tzinfo=timezone.utc)
    return J1970 + (dt - epoch).total_seconds() / DAY_S


def julian_century(jd: float) -> float:
    return (jd - 2451545.0) / 36525.0


@dataclass(frozen=True)
class SolarPosition:
    declination_deg: float
    eot_minutes: float
    altitude_deg: float
    azimuth_deg: float  # clockwise from north, 0..360
    sun_enu: tuple[float, float, float]


@dataclass(frozen=True)
class SolarTerms:
    """Date-level terms (declination + equation of time)."""

    declination_deg: float
    eot_minutes: float


def _wrap_into(x: float, period: float) -> float:
    """Wrap x into [0, period)."""
    return x - math.floor(x / period) * period


def solar_terms(dt_utc: datetime) -> SolarTerms:
    jd = julian_day(dt_utc)
    jc = julian_century(jd)

    # Geometric mean longitude / anomaly of the Sun (degrees)
    geom_mean_long = _wrap_into(
        280.46646 + jc * (36000.76983 + jc * 0.0003032), 360.0
    )
    geom_mean_anom = 357.52911 + jc * (35999.05029 - 0.0001537 * jc)
    ecc = 0.016708634 - jc * (0.000042037 + 0.0000001267 * jc)

    # Equation of centre (degrees); the anomaly inside the sines is in rad
    g = geom_mean_anom * RAD
    eq_centre = (
        math.sin(g) * (1.914602 - jc * (0.004817 + 0.000014 * jc))
        + math.sin(2 * g) * (0.019993 - 0.000101 * jc)
        + math.sin(3 * g) * 0.000289
    )
    sun_true_long = geom_mean_long + eq_centre

    # Apparent longitude with nutation/aberration (degrees)
    omega = 125.04 - 1934.136 * jc
    sun_app_long = sun_true_long - 0.00569 - 0.00478 * math.sin(omega * RAD)

    # Mean & corrected obliquity (degrees)
    sec = (21.448 - jc * (46.815 + jc * (0.00059 - jc * 0.001813))) / 60.0
    mean_obliq = 23.0 + (26.0 + sec / 60.0) / 60.0
    obliq_corr = mean_obliq + 0.00256 * math.cos(omega * RAD)

    decl = DEG * math.asin(
        math.sin(obliq_corr * RAD) * math.sin(sun_app_long * RAD)
    )

    # Equation of time (minutes) -- standard NOAA form
    y = math.tan(obliq_corr * RAD / 2.0) ** 2
    l0 = geom_mean_long * RAD
    eot = 4.0 * DEG * (
        y * math.sin(2 * l0)
        - 2 * ecc * math.sin(g)
        + 4 * ecc * y * math.sin(g) * math.cos(2 * l0)
        - 0.5 * y * y * math.sin(4 * l0)
        - 1.25 * ecc * ecc * math.sin(2 * g)
    )
    return SolarTerms(declination_deg=decl, eot_minutes=eot)


def sun_position(dt_utc: datetime, latitude_deg: float,
                 longitude_deg: float, terms: SolarTerms | None = None) -> SolarPosition:
    """Solar position at an instant.

    Longitude is positive east (geographic convention); NOAA uses positive
    west, which is handled internally.
    """
    if terms is None:
        terms = solar_terms(dt_utc)
    time_mins = (
        dt_utc.hour * 60
        + dt_utc.minute
        + dt_utc.second / 60.0
        + dt_utc.microsecond / 60_000_000.0
    )
    # True solar time (minutes from local midnight)
    tst = _wrap_into(
        time_mins + terms.eot_minutes + 4.0 * (-longitude_deg), 1440.0
    )
    # Hour angle: 15 degrees per hour, NEGATIVE before solar noon (east of
    # the meridian), positive in the afternoon (astronomical sign used by
    # the ENU conversion below).
    ha = tst / 4.0 - 180.0

    lat = latitude_deg * RAD
    dec = terms.declination_deg * RAD
    h = ha * RAD
    # Equatorial -> local ENU (x east, y north, z up)
    e = -math.cos(dec) * math.sin(h)
    n = (
        math.cos(lat) * math.sin(dec)
        - math.sin(lat) * math.cos(dec) * math.cos(h)
    )
    u = (
        math.sin(lat) * math.sin(dec)
        + math.cos(lat) * math.cos(dec) * math.cos(h)
    )
    norm = math.hypot(math.hypot(e, n), u)
    e, n, u = e / norm, n / norm, u / norm

    altitude = math.asin(max(-1.0, min(1.0, u))) * DEG
    azimuth = math.atan2(e, n) * DEG % 360.0
    return SolarPosition(
        declination_deg=terms.declination_deg,
        eot_minutes=terms.eot_minutes,
        altitude_deg=altitude,
        azimuth_deg=azimuth,
        sun_enu=(e, n, u),
    )


def solar_time_minutes(dt_utc: datetime, longitude_deg: float,
                       terms: SolarTerms) -> float:
    """Apparent solar time of day in minutes [0, 1440)."""
    time_mins = (
        dt_utc.hour * 60
        + dt_utc.minute
        + dt_utc.second / 60.0
        + dt_utc.microsecond / 60_000_000.0
    )
    return _wrap_into(
        time_mins + terms.eot_minutes + 4.0 * (-longitude_deg), 1440.0
    )


def utc_for_solar_time(day_utc: datetime, longitude_deg: float,
                       solar_min: float) -> tuple[datetime, float]:
    """Invert :func:`solar_time_minutes` with one fixed-point refinement.

    Returns ``(utc_datetime, achieved_solar_minutes)``. The equation of time
    changes by <0.01 min within an hour, so one refinement is ample.
    """
    t0 = solar_terms(day_utc)
    utc_min = _wrap_into(
        solar_min - t0.eot_minutes - 4.0 * (-longitude_deg), 1440.0
    )
    cand = day_utc.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(
        minutes=utc_min
    )
    t1 = solar_terms(cand)
    utc_min2 = _wrap_into(
        solar_min - t1.eot_minutes - 4.0 * (-longitude_deg), 1440.0
    )
    result = day_utc.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(
        minutes=utc_min2
    )
    achieved = solar_time_minutes(result, longitude_deg, t1)
    return result, achieved
