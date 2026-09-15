"""Pydantic request/response models.

Geometry convention
-------------------
* Coordinates on the wall panel are 2D ``(x, y)`` in metres; ``+x`` is the
  panel's horizontal "right" direction and ``+y`` points up the panel.
* The panel outward normal is derived from ``azimuth`` (direction the wall
  faces, measured clockwise from north) and ``inclination`` (0 = vertical,
  90 = horizontal facing up, negative = overhang).
* Gnomon ``direction`` is a 3D vector in local ENU (east, north, up); the
  nodus (needle tip) is ``base + length * direction_hat`` pushed out of the
  wall by ``normal_offset`` metres.
* Longitude is positive east, latitude positive north.
"""

from __future__ import annotations

import datetime as _dt
import math
from datetime import date
from enum import Enum
from typing import Literal

from pydantic import (
    BaseModel, Field, field_validator, model_serializer, model_validator,
)


# ---------------------------------------------------------------- inputs --


class Point2(BaseModel):
    x: float
    y: float


Vec3 = tuple[float, float, float]
Polygon = list[Point2]


class Weekday(str, Enum):
    mon = "mon"
    tue = "tue"
    wed = "wed"
    thu = "thu"
    fri = "fri"
    sat = "sat"
    sun = "sun"


class DSTRule(BaseModel):
    """Daylight-saving rule. ``None`` on either side disables DST.

    The transition is taken to happen at ``at_local`` clock time on the
    selected day in *standard* time for ``start`` and *daylight* time for
    ``end`` (the conventional wall-clock instant).
    """

    mode: Literal["none", "fixed", "nth_weekday"] = "none"
    start_month: int | None = Field(None, ge=1, le=12)
    start_day: int | None = Field(None, ge=1, le=31)
    start_weekday: Weekday | None = None
    start_week: int | None = Field(None, ge=1, le=5, description="1..4 or 5=last")
    start_at_local: str = Field("02:00", description="HH:MM wall clock time")
    end_month: int | None = Field(None, ge=1, le=12)
    end_day: int | None = Field(None, ge=1, le=31)
    end_weekday: Weekday | None = None
    end_week: int | None = Field(None, ge=1, le=5)
    end_at_local: str = Field("03:00")
    dst_offset_minutes: int = Field(60, ge=0, le=180)

    @field_validator("start_at_local", "end_at_local")
    @classmethod
    def _hhmm(cls, v: str) -> str:
        parts = v.split(":")
        if len(parts) != 2:
            raise ValueError("expected HH:MM")
        h, m = int(parts[0]), int(parts[1])
        if not (0 <= h <= 23 and 0 <= m <= 59):
            raise ValueError("HH:MM out of range")
        return f"{h:02d}:{m:02d}"

    @model_validator(mode="after")
    def _check_mode(self):
        if self.mode == "none":
            return self
        if self.start_month is None or self.end_month is None:
            raise ValueError("DST start/end months required when mode != none")
        if self.mode == "fixed":
            if self.start_day is None or self.end_day is None:
                raise ValueError("fixed DST rule needs start_day/end_day")
        else:
            if None in (self.start_weekday, self.start_week,
                        self.end_weekday, self.end_week):
                raise ValueError("nth_weekday DST rule needs weekday/week")
        return self


class ObstaclePoint(BaseModel):
    """One skyline control point: Sun at ``azimuth_deg`` is hidden below
    ``altitude_deg``.  Azimuth is degrees clockwise from north, ``[0, 360)``;
    0° and 360° are the same azimuth and 360 is rejected."""

    azimuth_deg: float = Field(..., ge=0.0, lt=360.0)
    altitude_deg: float = Field(
        ..., ge=0.0, le=90.0,
        description="obstacle top edge altitude above the horizon (degrees)",
    )


class ObstacleContour(BaseModel):
    """Named obstruction skyline (building, eaves, evergreen crown ...).

    Control points must be listed by strictly increasing solar azimuth.
    With ``wrap=False`` the skyline exists only between the first and last
    point.  With ``wrap=True`` the closing segment between the last and
    first point crosses 0° (north): the contour must span the seam, i.e.
    the first azimuth must be > 0 and the last < 360.
    """

    name: str = Field(..., min_length=1, max_length=80)
    points: list[ObstaclePoint] = Field(..., min_length=2)
    wrap: bool = Field(
        False,
        description="closed skyline ring: closing segment crosses 0° azimuth",
    )

    @model_validator(mode="after")
    def _check_profile(self):
        name = self.name.strip()
        if not name:
            raise ValueError("obstacle name must not be blank")
        self.name = name
        azs = [p.azimuth_deg for p in self.points]
        for a, b in zip(azs, azs[1:]):
            if b <= a:
                raise ValueError(
                    "obstacle control points must be ordered by strictly "
                    "increasing solar azimuth (no duplicates)"
                )
        if self.wrap:
            if not (azs[0] > 0.0 and azs[-1] < 360.0):
                raise ValueError(
                    "a wrap-around obstacle must span 0°: first azimuth > 0 "
                    "and last azimuth < 360"
                )
        return self


class WallInput(BaseModel):
    name: str = Field(..., min_length=1, max_length=120)
    latitude: float = Field(..., ge=-90, le=90)
    longitude: float = Field(..., ge=-180, le=180)
    standard_offset_minutes: int = Field(
        ..., ge=-720, le=840,
        description="UTC offset of standard (non-DST) time in minutes",
    )
    dst: DSTRule = Field(default_factory=lambda: DSTRule(mode="none"))
    azimuth: float = Field(
        ..., ge=0, lt=360,
        description="direction the wall faces, degrees clockwise from north",
    )
    inclination: float = Field(
        0.0, ge=-90, le=90,
        description="wall tilt: 0 vertical, 90 horizontal up, negative overhang",
    )
    panel: list[Point2] = Field(..., min_length=3)
    obstacles: list[ObstacleContour] = Field(
        default_factory=list,
        description="named obstruction skylines frozen with the wall version",
    )

    @model_validator(mode="after")
    def _check_panel(self):
        if len(self.panel) < 3:
            raise ValueError("panel needs at least 3 vertices")
        return self

    @model_validator(mode="after")
    def _check_obstacle_names(self):
        names = [o.name for o in self.obstacles]
        dup = {n for n in names if names.count(n) > 1}
        if dup:
            raise ValueError(
                "obstacle names must be unique; duplicated: "
                + ", ".join(sorted(dup))
            )
        return self


class GnomonInput(BaseModel):
    base: Point2
    direction: Vec3 = Field(..., description="ENU vector, need not be unit length")
    length: float = Field(..., gt=0)
    normal_offset: float = Field(
        0.0, ge=0,
        description="distance of the nodus outward from the wall plane (m)",
    )

    @model_validator(mode="after")
    def _check_dir(self):
        if len(self.direction) != 3:
            raise ValueError("direction must be [east, north, up]")
        if all(abs(c) < 1e-12 for c in self.direction):
            raise ValueError("direction must not be a zero vector")
        return self


class DateRange(BaseModel):
    start: date
    end: date

    @model_validator(mode="after")
    def _ordered(self):
        if self.end < self.start:
            raise ValueError("end before start")
        if (self.end - self.start).days > 370:
            raise ValueError("date range too long (max 370 days)")
        return self


class SeasonDates(BaseModel):
    """Seasonal date lines to draw (ISO calendar dates)."""

    dates: list[date] = Field(
        default_factory=lambda: [
            date(2026, 3, 20), date(2026, 6, 21),
            date(2026, 9, 22), date(2026, 12, 21),
        ],
    )


class GenerateOptions(BaseModel):
    time_mode: Literal["solar", "civil"] = "solar"
    hours: list[int] = Field(
        default_factory=lambda: list(range(6, 19)),
        description="hour labels (solar: 6..18; civil: wall-clock hours)",
    )
    season_dates: list[date] = Field(
        default_factory=lambda: [
            date(2026, 3, 20), date(2026, 6, 21),
            date(2026, 9, 22), date(2026, 12, 21),
        ],
    )
    sample_minutes: int = Field(20, ge=5, le=120)
    check_min_spacing: float = Field(
        0.01, gt=0,
        description="hour lines closer than this (m) are flagged",
    )
    parallel_cos_threshold: float = Field(
        0.03, gt=0, lt=0.5,
        description="s·n below this => shadow parallel to wall",
    )
    label_font_size: float = Field(0.03, gt=0, le=1.0)


class SearchOptions(BaseModel):
    time_mode: Literal["solar", "civil"] = "solar"
    hours: list[int] = Field(
        default_factory=lambda: list(range(6, 19)),
        description="hour labels to evaluate (solar or wall-clock)",
    )
    season_dates: list[date] = Field(
        default_factory=lambda: [
            date(2026, 3, 20), date(2026, 6, 21),
            date(2026, 9, 22), date(2026, 12, 21),
        ],
    )
    candidate_lengths: list[float] = Field(..., min_length=1)
    min_spacing: float = Field(..., gt=0)
    margin: float = Field(0.05, ge=0)
    base_grid_step: float = Field(0.05, gt=0)
    label_offsets: list[Point2] = Field(
        default_factory=lambda: [
            Point2(x=0.0, y=0.0), Point2(x=0.03, y=0.0),
            Point2(x=-0.03, y=0.0), Point2(x=0.0, y=0.03),
            Point2(x=0.0, y=-0.03),
        ],
    )
    search_sample_minutes: int = Field(30, ge=10, le=120)

    @model_validator(mode="after")
    def _check_lengths(self):
        if any(l <= 0 for l in self.candidate_lengths):
            raise ValueError("candidate lengths must be positive")
        if len(set(self.candidate_lengths)) != len(self.candidate_lengths):
            raise ValueError("duplicate candidate lengths")
        return self


class GenerateRequest(BaseModel):
    date_range: DateRange
    gnomon: GnomonInput
    options: GenerateOptions = Field(default_factory=GenerateOptions)


# --------------------------------------------------------- inverse solve --


class InverseSolveOptions(BaseModel):
    """Tuning knobs for the measured-shadow inverse lookup.

    ``coarse_step_minutes`` is the UTC grid used to locate brackets; the
    root refinement (bisection / golden-section) stops at
    ``root_epsilon_seconds``.  Features narrower than the coarse step can be
    missed, so both values are reported back as the search precision.
    """

    coarse_step_minutes: int = Field(10, ge=1, le=60)
    root_epsilon_seconds: float = Field(2.0, gt=0, le=300)
    max_candidates_per_observation: int = Field(64, ge=1, le=200)
    max_chains: int = Field(10, ge=1, le=100)


class InverseSolveRequest(BaseModel):
    """Measured-shadow inverse lookup against a stored wall version + scheme.

    ``observations`` are panel coordinates in chronological order; exactly
    one of ``date`` / ``date_range`` bounds the search.  With two or more
    observations the admissible interval between adjacent observations
    (minutes) is required so candidate chains can be filtered consistently.
    """

    scheme_id: int = Field(..., ge=1)
    observations: list[Point2] = Field(..., min_length=1, max_length=50)
    # ``date`` shadows the datetime type in the class namespace, so the
    # annotation must qualify it through the module alias
    date: _dt.date | None = None
    date_range: DateRange | None = None
    tolerance: float = Field(
        ..., gt=0, le=1.0,
        description="coordinate tolerance of each observation (m)",
    )
    interval_min_minutes: float | None = Field(None, gt=0, le=10080)
    interval_max_minutes: float | None = Field(None, gt=0, le=10080)
    options: InverseSolveOptions = Field(default_factory=InverseSolveOptions)

    @model_validator(mode="after")
    def _check_inverse(self):
        if (self.date is None) == (self.date_range is None):
            raise ValueError("exactly one of date / date_range is required")
        for o in self.observations:
            if not (math.isfinite(o.x) and math.isfinite(o.y)):
                raise ValueError("observation coordinates must be finite")
        lo, hi = self.interval_min_minutes, self.interval_max_minutes
        if (lo is None) != (hi is None):
            raise ValueError(
                "interval_min_minutes and interval_max_minutes must be "
                "given together"
            )
        if lo is not None and lo > hi:
            raise ValueError(
                "interval_min_minutes exceeds interval_max_minutes"
            )
        if len(self.observations) >= 2 and lo is None:
            raise ValueError(
                "adjacent-observation interval is required with two or "
                "more observations"
            )
        return self

    def effective_range(self) -> "DateRange":
        return self.date_range or DateRange(start=self.date, end=self.date)


# --------------------------------------------------------------- outputs --


class ObstacleHitInfo(BaseModel):
    """Which skyline hides a sample and by how much."""

    name: str
    azimuth_deg: float
    profile_altitude_deg: float
    margin_deg: float


class LinePoint(BaseModel):
    date: date
    hour_label: int | None = None
    utc: str
    local_clock: str
    solar_time_min: float
    dst: bool
    altitude_deg: float
    azimuth_deg: float
    declination_deg: float
    eot_min: float
    sun_enu: Vec3
    shadow: Point2 | None
    status: str
    reason: str
    note: str = ""
    obstacle: ObstacleHitInfo | None = None
    """one of: ok, below_horizon, sun_behind_wall, shadow_parallel,
    shadow_outside_panel, blocked_by_obstacle, dst_gap"""

    @model_serializer(mode="wrap")
    def _serialize(self, handler):
        # Omit the obstacle key entirely for requests that carry no
        # obstruction profiles, keeping legacy payloads byte-identical.
        data = handler(self)
        if data.get("obstacle") is None:
            data.pop("obstacle", None)
        return data


class DialLine(BaseModel):
    kind: Literal["hour", "season"]
    label: str
    hour: int | None = None
    season_date: date | None = None
    segments: list[list[Point2]]
    sample_basis: list[LinePoint]
    sample_count: int
    ok_count: int
    outside_count: int
    behind_count: int
    parallel_count: int
    below_count: int
    blocked_count: int = 0
    gap_count: int
    broken: bool
    gaps: list[dict]

    @model_serializer(mode="wrap")
    def _serialize(self, handler):
        data = handler(self)
        if not data.get("blocked_count"):
            data.pop("blocked_count", None)
        return data


class InvalidInterval(BaseModel):
    reason: str
    start_utc: str
    end_utc: str
    start_local: str
    end_local: str
    obstacle: str | None = None

    @model_serializer(mode="wrap")
    def _serialize(self, handler):
        data = handler(self)
        if data.get("obstacle") is None:
            data.pop("obstacle", None)
        return data


class DSTEvent(BaseModel):
    kind: Literal["start", "end"]
    utc: str
    local_before: str
    local_after: str
    gap_local: str
    overlap_local: str


class SpacingIssue(BaseModel):
    hour_a: int
    hour_b: int
    distance: float
    at: Point2


class LabelBox(BaseModel):
    line_label: str
    x: float
    y: float
    width: float
    height: float
    angle_deg: float


class ObstacleLoss(BaseModel):
    """Readable time lost to one named skyline over the date range."""

    name: str
    blocked_samples: int
    blocked_minutes: float
    intervals: list[InvalidInterval]


class ObstacleReport(BaseModel):
    """Aggregate blockage summary for all named obstruction profiles."""

    blocked_samples: int
    blocked_minutes: float
    losses: list[ObstacleLoss]


class CheckReport(BaseModel):
    broken_lines: list[dict]
    min_spacing: float | None
    spacing_issues: list[SpacingIssue]
    label_overlaps: list[dict]
    dst_events: list[DSTEvent]
    dst_gap_intervals: list[InvalidInterval]
    margin_violations: list[dict]


class DialResult(BaseModel):
    input_hash: str
    geometry_hash: str
    algorithm_version: str
    mode: str
    lines: list[DialLine]
    invalid_intervals: list[InvalidInterval]
    checks: CheckReport
    labels: list[LabelBox]
    coverage: dict
    obstacle_report: ObstacleReport | None = None
    svg: str

    @model_serializer(mode="wrap")
    def _serialize(self, handler):
        data = handler(self)
        if data.get("obstacle_report") is None:
            data.pop("obstacle_report", None)
        return data


class SearchCandidate(BaseModel):
    rank: int
    base: Point2
    length: float
    label_offset: Point2
    coverage: float
    readable_coverage: float
    min_spacing: float | None
    spacing_ok: bool
    panel_usage: float
    label_overlaps: int
    margin_ok: bool
    score: list[float]
    obstacle_losses: list[ObstacleLoss] | None = None
    """readable time lost to each skyline (gnomon-independent); omitted when
    the wall version defines no obstruction profiles"""
    result: "DialResult | None" = None

    @model_serializer(mode="wrap")
    def _serialize(self, handler):
        data = handler(self)
        if not data.get("obstacle_losses"):
            data.pop("obstacle_losses", None)
        return data


class SearchResponse(BaseModel):
    input_hash: str
    geometry_hash: str
    candidates: list[SearchCandidate]
    searched: int


# --------------------------------------------------- inverse solve out --


class TrajectorySegmentRef(BaseModel):
    """One continuous readable stretch of the shadow trajectory."""

    index: int
    date: date
    start_utc: str
    end_utc: str


class InverseCandidate(BaseModel):
    """One instant whose shadow falls within tolerance of the observation."""

    utc: str
    local_clock: str
    solar_time_min: float
    dst: bool
    dst_overlap: bool
    """the civil reading also occurs at another UTC instant (fall-back)"""
    residual_m: float
    point: Point2
    segment: TrajectorySegmentRef
    window_start_utc: str
    window_end_utc: str
    """root-refined interval during which the shadow stays within tolerance"""


class InverseObservationResult(BaseModel):
    index: int
    observed: Point2
    candidate_count: int
    truncated: bool
    nearest_distance_m: float | None
    nearest_utc: str | None
    candidates: list[InverseCandidate]


class InverseChain(BaseModel):
    """One consistent time chain: one candidate per observation, in order."""

    candidate_indices: list[int]
    utc: list[str]
    intervals_minutes: list[float]
    total_residual_m: float


class InverseChainFailure(BaseModel):
    """Why one first-observation candidate cannot be extended to a chain."""

    first_candidate_utc: str
    failed_at_observation: int
    reason: str


class InverseFailure(BaseModel):
    """Diagnostics when no consistent chain matches; centred on the first
    observation."""

    observation_index: int
    reasons: list[str]
    chain_failures: list[InverseChainFailure]


class InverseSolveResponse(BaseModel):
    input_hash: str
    geometry_hash: str
    algorithm_version: str
    wall_id: int
    version: int
    version_id: int
    scheme_id: int
    matched: bool
    chain_count: int
    precision: dict
    observations: list[InverseObservationResult]
    chains: list[InverseChain]
    failure: InverseFailure | None = None

    @model_serializer(mode="wrap")
    def _serialize(self, handler):
        data = handler(self)
        if data.get("failure") is None:
            data.pop("failure", None)
        return data


class SelectedScheme(BaseModel):
    id: int
    wall_version_id: int
    base: Point2
    direction: Vec3
    length: float
    normal_offset: float
    label_offset: Point2
    options_json: str
    result_json: str
    input_hash: str
    created_utc: str


class WallVersionOut(BaseModel):
    id: int
    wall_id: int
    version: int
    geometry_hash: str
    geometry_json: str
    created_utc: str


class WallOut(BaseModel):
    id: int
    name: str
    current_version: int
    geometry_hash: str
    created_utc: str


SearchCandidate.model_rebuild()
