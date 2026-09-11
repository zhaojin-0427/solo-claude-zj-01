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

from datetime import date
from enum import Enum
from typing import Literal

from pydantic import BaseModel, Field, field_validator, model_validator


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

    @model_validator(mode="after")
    def _check_panel(self):
        if len(self.panel) < 3:
            raise ValueError("panel needs at least 3 vertices")
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


# --------------------------------------------------------------- outputs --


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
    """one of: ok, below_horizon, sun_behind_wall, shadow_parallel,
    shadow_outside_panel, dst_gap"""


# ---------------------------------------------------------------- outputs --


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
    gap_count: int
    broken: bool
    gaps: list[dict]


class InvalidInterval(BaseModel):
    reason: str
    start_utc: str
    end_utc: str
    start_local: str
    end_local: str


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
    svg: str


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
    result: "DialResult | None" = None


class SearchResponse(BaseModel):
    input_hash: str
    geometry_hash: str
    candidates: list[SearchCandidate]
    searched: int


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
