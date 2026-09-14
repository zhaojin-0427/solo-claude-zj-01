"""Regression tests covering the reported bugs and core guarantees."""

import os
import tempfile
from datetime import date, datetime

import pytest

# isolate DB before importing the app
_TMP_DB = tempfile.mktemp(suffix=".db")
os.environ["SUNDIAL_DB"] = _TMP_DB

from fastapi.testclient import TestClient  # noqa: E402

from app import engine, geometry as G, search as search_candidates_mod, service  # noqa: E402
from app.main import app  # noqa: E402
from app.schemas import (  # noqa: E402
    DateRange, DSTRule, GenerateOptions, GnomonInput, ObstacleContour,
    ObstaclePoint, Point2, SearchOptions, WallInput, Weekday,
)
from app.solar import sun_position, utc_for_solar_time  # noqa: E402
from app.timezone_engine import TimezoneEngine  # noqa: E402

client = TestClient(app)


# ------------------------------------------------------------- fixtures --


def eu_dst():
    return DSTRule(
        mode="nth_weekday",
        start_month=3, start_weekday=Weekday.sun, start_week=5,
        start_at_local="02:00",
        end_month=10, end_weekday=Weekday.sun, end_week=5,
        end_at_local="03:00",
    )


def berlin_wall(az=180, dst=None):
    return WallInput(
        name="Berlin south wall", latitude=52.52, longitude=13.40,
        standard_offset_minutes=60, dst=dst or DSTRule(mode="none"),
        azimuth=az, inclination=0,
        panel=[Point2(x=-1, y=0), Point2(x=1, y=0),
               Point2(x=1, y=1.2), Point2(x=-1, y=1.2)],
    )


def gnomon(base=(0.0, 0.4), length=0.25):
    return GnomonInput(base=Point2(x=base[0], y=base[1]),
                       direction=(0, -1, 0), length=length)


def hedge(alt=40.0, az0=120.0, az1=240.0, name="hedge", wrap=False):
    return ObstacleContour(
        name=name, wrap=wrap,
        points=[ObstaclePoint(azimuth_deg=az0, altitude_deg=alt),
                ObstaclePoint(azimuth_deg=(az0 + az1) / 2, altitude_deg=alt),
                ObstaclePoint(azimuth_deg=az1, altitude_deg=alt)],
    )


YEAR = DateRange(start=date(2026, 1, 1), end=date(2026, 12, 31))
SPRING = DateRange(start=date(2026, 3, 20), end=date(2026, 3, 22))


# --------------------------------------------------------------- solar ---


class TestSolar:
    def test_equinox_morning_sun_in_east(self):
        m = sun_position(datetime(2026, 3, 20, 6, 0), 45.0, 0.0)
        assert 60 < m.azimuth_deg < 120  # east
        assert m.sun_enu[0] > 0.9

    def test_equinox_evening_sun_in_west(self):
        e = sun_position(datetime(2026, 3, 20, 18, 0), 45.0, 0.0)
        assert 240 < e.azimuth_deg < 300
        assert e.sun_enu[0] < -0.9

    def test_noon_south_high_summer_london(self):
        day = datetime(2026, 6, 21)
        noon, achieved = utc_for_solar_time(day, -0.13, 720.0)
        p = sun_position(noon, 51.5, -0.13)
        assert abs(p.altitude_deg - 61.93) < 0.1
        assert abs(p.azimuth_deg - 180) < 0.01
        assert abs(p.declination_deg - 23.44) < 0.05
        assert abs(achieved - 720) < 0.01

    def test_declination_solstices(self):
        ws = sun_position(datetime(2026, 12, 21, 12), 0.0, 0.0)
        assert abs(ws.declination_deg + 23.44) < 0.05


# ------------------------------------------------------------- timezone --


class TestDST:
    def test_eu_transition_instants(self):
        eng = TimezoneEngine(60, eu_dst())
        tr = {t.kind: t for t in eng.transitions_for_year(2026)}
        assert tr["start"].utc == datetime(2026, 3, 29, 1, 0)
        assert tr["end"].utc == datetime(2026, 10, 25, 1, 0)

    def test_spring_forward_gap(self):
        eng = TimezoneEngine(60, eu_dst())
        tr = eng.transitions_for_year(2026)
        _, status = eng.to_utc(datetime(2026, 3, 29, 2, 30), tr)
        assert status == "skipped"
        u_ok, s_ok = eng.to_utc(datetime(2026, 3, 29, 3, 0), tr)
        assert s_ok == "ok" and u_ok.hour == 1

    def test_fall_back_overlap_folds(self):
        eng = TimezoneEngine(60, eu_dst())
        tr = eng.transitions_for_year(2026)
        d = datetime(2026, 10, 25, 2, 30)
        u0, s0 = eng.to_utc(d, tr, fold=0)
        u1, s1 = eng.to_utc(d, tr, fold=1)
        assert s0 == "ambiguous" and s1 == "ambiguous"
        assert u0 == datetime(2026, 10, 25, 0, 30)
        assert u1 == datetime(2026, 10, 25, 1, 30)

    def test_summer_dst_flag(self):
        eng = TimezoneEngine(60, eu_dst())
        assert eng.is_dst(datetime(2026, 7, 15, 12))
        assert not eng.is_dst(datetime(2026, 1, 15, 12))


# ---------------------------------------------------------- generation ---


class TestGeneration:
    def test_single_day_does_not_mix_adjacent_dates(self):
        dr = DateRange(start=date(2026, 6, 21), end=date(2026, 6, 21))
        opts = GenerateOptions(sample_minutes=20,
                               season_dates=[date(2026, 6, 21)])
        res = service.generate_dial(berlin_wall(), gnomon(), dr, opts)
        for line in res.lines:
            assert {s.date for s in line.sample_basis} <= {date(2026, 6, 21)}
            if line.kind == "hour":
                assert line.sample_count == 1

    def test_hour_lines_one_sample_per_date(self):
        opts = GenerateOptions(sample_minutes=20)
        res = service.generate_dial(berlin_wall(), gnomon(), YEAR, opts)
        for line in res.lines:
            if line.kind != "hour":
                continue
            ds = [s.date for s in line.sample_basis]
            assert ds == sorted(set(ds))
            assert len(ds) == 365

    def test_invalid_intervals_dont_merge_across_night(self):
        opts = GenerateOptions(sample_minutes=20)
        res = service.generate_dial(berlin_wall(), gnomon(), YEAR, opts)
        for iv in res.invalid_intervals:
            start = datetime.fromisoformat(iv.start_utc[:-1])
            end = datetime.fromisoformat(iv.end_utc[:-1])
            assert (end - start).total_seconds() / 3600 <= 12

    def test_noon_summer_outside_winter_inside_is_real_break(self):
        opts = GenerateOptions(sample_minutes=20, hours=[12])
        res = service.generate_dial(berlin_wall(), gnomon(), YEAR, opts)
        noon = next(l for l in res.lines if l.label == "12:00")
        statuses = [s.status for s in noon.sample_basis]
        assert "shadow_outside_panel" in statuses
        assert "ok" in statuses
        assert noon.broken

    def test_below_horizon_not_in_line_gaps(self):
        opts = GenerateOptions(sample_minutes=20, hours=[12])
        res = service.generate_dial(berlin_wall(), gnomon(), YEAR, opts)
        noon = next(l for l in res.lines if l.label == "12:00")
        assert all(g["status"] != "below_horizon" for g in noon.gaps)

    def test_civil_dst_gap_marked_on_02_line(self):
        dr = DateRange(start=date(2026, 3, 1), end=date(2026, 4, 30))
        opts = GenerateOptions(time_mode="civil", hours=[2, 12],
                               sample_minutes=20)
        res = service.generate_dial(
            berlin_wall(dst=eu_dst()), gnomon(), dr, opts
        )
        l02 = next(l for l in res.lines if l.label == "02:00 L")
        assert any(s.status == "dst_gap"
                   and s.date == date(2026, 3, 29)
                   for s in l02.sample_basis)
        assert any(g["status"] == "dst_gap" for g in l02.gaps)
        assert len(res.checks.dst_events) == 1
        assert res.checks.dst_events[0].gap_local == "02:00–03:00"

    def test_reproducible_same_hash_and_svg(self):
        opts = GenerateOptions(sample_minutes=30)
        r1 = service.generate_dial(berlin_wall(), gnomon(), YEAR, opts)
        r2 = service.generate_dial(berlin_wall(), gnomon(), YEAR, opts)
        assert r1.input_hash == r2.input_hash
        assert r1.svg == r2.svg
        assert r1.model_dump() == r2.model_dump()


# ------------------------------------------------------------ obstacles --


class TestObstacleValidation:
    def _pts(self, az_alts):
        return [ObstaclePoint(azimuth_deg=a, altitude_deg=h)
                for a, h in az_alts]

    def test_azimuth_range_rejects_360_and_negative(self):
        for bad in (-0.1, 360.0, 400.0):
            with pytest.raises(Exception):
                ObstaclePoint(azimuth_deg=bad, altitude_deg=10)
        for bad in (-1.0, 90.1):
            with pytest.raises(Exception):
                ObstaclePoint(azimuth_deg=180, altitude_deg=bad)

    def test_needs_two_points(self):
        with pytest.raises(Exception):
            ObstacleContour(name="x", points=self._pts([(10, 5)]))

    def test_azimuths_must_increase_without_duplicates(self):
        with pytest.raises(Exception):
            ObstacleContour(name="x", points=self._pts([(120, 5), (100, 5)]))
        with pytest.raises(Exception):
            ObstacleContour(name="x", points=self._pts([(100, 5), (100, 8)]))

    def test_wrap_must_span_zero(self):
        with pytest.raises(Exception):
            ObstacleContour(name="x", wrap=True,
                            points=self._pts([(0, 5), (100, 8)]))
        # a proper ring across 0° validates and round-trips
        c = ObstacleContour(name="ring", wrap=True,
                            points=self._pts([(90, 10), (180, 5), (270, 10)]))
        assert c.wrap

    def test_contour_names_unique_and_nonblank(self):
        with pytest.raises(Exception):
            ObstacleContour(name="   ", points=[
                ObstaclePoint(azimuth_deg=10, altitude_deg=5),
                ObstaclePoint(azimuth_deg=20, altitude_deg=5)])
        # model_copy skips validators, so validate explicitly
        data = berlin_wall().model_dump(mode="json")
        data["obstacles"] = [
            {"name": "a", "points": [
                {"azimuth_deg": 10, "altitude_deg": 5},
                {"azimuth_deg": 20, "altitude_deg": 5}]},
            {"name": "a", "points": [
                {"azimuth_deg": 30, "altitude_deg": 5},
                {"azimuth_deg": 40, "altitude_deg": 5}]},
        ]
        with pytest.raises(Exception):
            WallInput.model_validate(data)


class TestObstacleInterpolation:
    @staticmethod
    def _profiles(w):
        from app.obstacles import compile_contours
        return compile_contours(w)

    def test_piecewise_linear_values(self):
        from app.obstacles import blocking_obstacle
        w = berlin_wall().model_copy(update={"obstacles": [
            ObstacleContour(name="b", points=[
                ObstaclePoint(azimuth_deg=100, altitude_deg=20),
                ObstaclePoint(azimuth_deg=120, altitude_deg=30)])]})
        prof = self._profiles(w)[0]
        assert prof.altitude_at(110) == 25.0
        assert prof.altitude_at(100) == 20.0
        assert prof.altitude_at(120) == 30.0
        assert prof.altitude_at(90) is None
        assert prof.altitude_at(130) is None
        hit = blocking_obstacle([prof], 110, 24.0)
        assert hit.name == "b" and abs(hit.margin_deg - 1.0) < 1e-9
        assert blocking_obstacle([prof], 110, 26.0) is None

    def test_open_profile_covers_only_its_span(self):
        w = berlin_wall().model_copy(update={"obstacles": [
            ObstacleContour(name="b", points=[
                ObstaclePoint(azimuth_deg=100, altitude_deg=20),
                ObstaclePoint(azimuth_deg=120, altitude_deg=30)])]})
        prof = self._profiles(w)[0]
        assert prof.altitude_at(99.999) is None
        assert prof.altitude_at(120.001) is None

    def test_wrap_profile_interpolates_across_zero(self):
        w = berlin_wall().model_copy(update={"obstacles": [
            ObstacleContour(name="ring", wrap=True, points=[
                ObstaclePoint(azimuth_deg=90, altitude_deg=10),
                ObstaclePoint(azimuth_deg=180, altitude_deg=5),
                ObstaclePoint(azimuth_deg=270, altitude_deg=10)])]})
        prof = self._profiles(w)[0]
        assert prof.altitude_at(0) == 10.0
        assert prof.altitude_at(359) == 10.0
        assert abs(prof.altitude_at(135) - 7.5) < 1e-9
        assert prof.altitude_at(450.0) == 10.0  # wrapped query

    def test_highest_profile_wins_with_stable_order(self):
        from app.obstacles import blocking_obstacle
        ring = ObstacleContour(name="ring", wrap=True, points=[
            ObstaclePoint(azimuth_deg=90, altitude_deg=8),
            ObstaclePoint(azimuth_deg=270, altitude_deg=8)])
        tower = ObstacleContour(name="tower", points=[
            ObstaclePoint(azimuth_deg=170, altitude_deg=20),
            ObstaclePoint(azimuth_deg=190, altitude_deg=20)])
        w = berlin_wall().model_copy(update={"obstacles": [ring, tower]})
        profs = self._profiles(w)
        assert blocking_obstacle(profs, 0.0, 5.0).name == "ring"
        assert blocking_obstacle(profs, 180.0, 15.0).name == "tower"
        assert blocking_obstacle(profs, 180.0, 25.0) is None


class TestObstacleGeneration:
    def test_plain_wall_payload_has_no_obstacle_keys(self):
        res = service.generate_dial(
            berlin_wall(), gnomon(), SPRING,
            GenerateOptions(sample_minutes=30),
        )
        d = res.model_dump(mode="json")
        assert "obstacle_report" not in d
        assert all("blocked_count" not in line
                   for line in d["lines"])
        assert all("obstacle" not in point
                   for line in d["lines"] for point in line["sample_basis"])
        assert all("obstacle" not in iv for iv in d["invalid_intervals"])

    def test_geometry_hash_unchanged_without_obstacles(self):
        w = berlin_wall()
        h1 = service.geometry_hash(w)
        h2 = service.geometry_hash(
            w.model_copy(update={"obstacles": []}))
        assert h1 == h2

    def test_geometry_hash_changes_with_profile(self):
        w = berlin_wall()
        h1 = service.geometry_hash(w)
        h2 = service.geometry_hash(
            w.model_copy(update={"obstacles": [hedge(40)]}))
        h3 = service.geometry_hash(
            w.model_copy(update={"obstacles": [hedge(41)]}))
        assert h1 != h2 != h3 and h1 != h3

    def test_blocked_samples_traceable_and_lines_split(self):
        w = berlin_wall().model_copy(update={"obstacles": [hedge(40)]})
        opts = GenerateOptions(sample_minutes=20,
                               hours=[6, 9, 12, 15, 18],
                               season_dates=[date(2026, 3, 20)])
        res = service.generate_dial(w, gnomon(), SPRING, opts)
        season = next(l for l in res.lines
                      if l.kind == "season" and l.season_date == date(2026, 3, 20))
        blocked = [s for s in season.sample_basis
                   if s.status == "blocked_by_obstacle"]
        assert blocked
        assert season.blocked_count == len(blocked)
        assert season.broken and len(season.segments) >= 2
        sample = blocked[0]
        assert sample.shadow is None
        assert sample.obstacle is not None
        assert sample.obstacle.name == "hedge"
        assert sample.obstacle.margin_deg >= 0
        # original sun sample stays traceable
        assert sample.altitude_deg > 0 and sample.azimuth_deg
        assert "wall-plane intersection" in sample.note
        assert any(g["status"] == "blocked_by_obstacle"
                   and g.get("obstacle") == "hedge"
                   for g in season.gaps)

    def test_blocked_invalid_intervals_named_and_not_cross_night(self):
        w = berlin_wall().model_copy(update={"obstacles": [hedge(40)]})
        opts = GenerateOptions(sample_minutes=20)
        res = service.generate_dial(w, gnomon(), SPRING, opts)
        ivs = [iv for iv in res.invalid_intervals
               if iv.reason == "blocked_by_obstacle"]
        assert ivs
        for iv in ivs:
            assert iv.obstacle == "hedge"
            a = datetime.fromisoformat(iv.start_utc[:-1])
            b = datetime.fromisoformat(iv.end_utc[:-1])
            assert b > a
            # single-day daytime run: never stretches across the night
            assert (b - a).total_seconds() / 3600 <= 12

    def test_obstacle_report_and_coverage(self):
        w = berlin_wall().model_copy(update={"obstacles": [hedge(40)]})
        res = service.generate_dial(
            w, gnomon(), SPRING, GenerateOptions(sample_minutes=20))
        report = res.obstacle_report
        assert report.blocked_samples > 0
        assert report.losses[0].name == "hedge"
        assert report.losses[0].blocked_minutes > 0
        assert report.losses[0].intervals
        cov = res.coverage
        assert cov["samples_blocked_by_obstacle"] > 0
        assert "samples_eligible" in cov
        assert 0.0 <= cov["readable_of_eligible"] <= 1.0
        assert "blocked by obstacle" in res.svg

    def test_hour_line_blocked_days_break_engraving(self):
        # 20° skyline band south: the winter noon Sun is below it and the
        # summer noon clears it, so a full-year line is genuinely broken;
        # over a winter-only range the same blockage simply truncates the
        # engraved line at its start.
        ob = ObstacleContour(name="block", points=[
            ObstaclePoint(azimuth_deg=170, altitude_deg=20),
            ObstaclePoint(azimuth_deg=190, altitude_deg=20)])
        w = berlin_wall().model_copy(update={"obstacles": [ob]})
        dr = DateRange(start=date(2026, 1, 1), end=date(2026, 2, 28))
        opts = GenerateOptions(sample_minutes=60, hours=[12],
                               season_dates=[])
        res = service.generate_dial(w, gnomon(), dr, opts)
        noon = next(l for l in res.lines if l.label == "12:00")
        statuses = [s.status for s in noon.sample_basis]
        assert "blocked_by_obstacle" in statuses
        assert noon.blocked_count == statuses.count("blocked_by_obstacle")
        bs = next(s for s in noon.sample_basis
                  if s.status == "blocked_by_obstacle")
        assert bs.obstacle.name == "block"
        assert "UTC solved from solar time" in bs.note
        assert any(g.get("obstacle") == "block" for g in noon.gaps)

        dr_year = DateRange(start=date(2026, 1, 1), end=date(2026, 12, 31))
        res_year = service.generate_dial(
            w, gnomon(), dr_year,
            GenerateOptions(sample_minutes=60, hours=[12], season_dates=[]))
        noon_year = next(l for l in res_year.lines if l.label == "12:00")
        assert noon_year.broken and len(noon_year.segments) >= 2

    def test_sun_behind_wall_is_not_obstacle_loss(self):
        # Blockage loss is gnomon-independent and only counts while the
        # wall is lit: a skyline covering azimuths the wall never faces
        # (sun on the far side) must not produce any blocked samples.
        from app.obstacles import compile_contours
        from datetime import timedelta
        back = ObstacleContour(name="north-block", points=[
            ObstaclePoint(azimuth_deg=0, altitude_deg=89),
            ObstaclePoint(azimuth_deg=90, altitude_deg=89)])
        w = berlin_wall(az=180).model_copy(update={"obstacles": [back]})
        frame = G.make_frame(w.azimuth, w.inclination)
        profiles = compile_contours(w)
        samples, _tz, _gaps = engine.build_samples(w, SPRING.start,
                                                   SPRING.end, 30)
        loss = engine.obstacle_loss_sweep(
            samples, frame, profiles, timedelta(minutes=30),
            GenerateOptions().parallel_cos_threshold,
        )
        # the south wall faces az ~90..270 only; the 0..90 skyline sits on
        # the wall's back side and hides nothing readable
        assert loss["north-block"]["count"] == 0


# -------------------------------------------------------------- search ---


class TestSearch:
    def _search(self, mode, dst=None):
        return SearchOptions(
            time_mode=mode,
            candidate_lengths=[0.2, 0.3], min_spacing=0.02,
            base_grid_step=0.25, search_sample_minutes=60,
        )

    def test_civil_search_returns_civil_results(self):
        dr = DateRange(start=date(2026, 3, 1), end=date(2026, 4, 30))
        from app.search import search_candidates
        cs, _ = search_candidates(
            berlin_wall(dst=eu_dst()), dr, (0, -1, 0), 0.0,
            self._search("solar"), full_top=1,
        )
        cc, _ = search_candidates(
            berlin_wall(dst=eu_dst()), dr, (0, -1, 0), 0.0,
            self._search("civil"), full_top=1,
        )
        assert cs[0].result.mode == "solar"
        assert cc[0].result.mode == "civil"

    def test_top_candidate_has_coverage(self):
        dr = DateRange(start=date(2026, 3, 1), end=date(2026, 4, 30))
        from app.search import search_candidates
        cc, total = search_candidates(
            berlin_wall(), dr, (0, -1, 0), 0.0,
            self._search("solar"), full_top=1,
        )
        assert total > 0
        assert cc[0].coverage > 0.5

    def test_hash_changes_with_search_params(self):
        dr = DateRange(start=date(2026, 3, 1), end=date(2026, 4, 30))
        w = berlin_wall()
        s1 = self._search("solar")
        s2 = self._search("civil")
        s3 = SearchOptions(
            time_mode="civil", candidate_lengths=[0.2, 0.4],
            min_spacing=0.05, base_grid_step=0.25,
            search_sample_minutes=60,
        )
        h1 = service.search_input_hash(w, dr, (0, -1, 0), 0.0, s1)
        h2 = service.search_input_hash(w, dr, (0, -1, 0), 0.0, s2)
        h3 = service.search_input_hash(w, dr, (0, -1, 0), 0.0, s3)
        assert len({h1, h2, h3}) == 3

    def test_hash_changes_with_full_top(self):
        dr = DateRange(start=date(2026, 3, 1), end=date(2026, 4, 30))
        w = berlin_wall(dst=eu_dst())
        s = SearchOptions(
            time_mode="civil", candidate_lengths=[0.2, 0.3],
            min_spacing=0.02, base_grid_step=0.25,
            search_sample_minutes=60,
        )
        h0 = service.search_input_hash(w, dr, (0, -1, 0), 0.0, s, 0)
        h1 = service.search_input_hash(w, dr, (0, -1, 0), 0.0, s, 1)
        h1b = service.search_input_hash(w, dr, (0, -1, 0), 0.0, s, 1)
        assert h0 != h1
        assert h1 == h1b  # identical complete request stays stable

    def test_obstacles_reduce_ranked_coverage_and_list_losses(self):
        dr = DateRange(start=date(2026, 3, 20), end=date(2026, 4, 5))
        s = SearchOptions(
            candidate_lengths=[0.25], min_spacing=0.02,
            base_grid_step=0.5, search_sample_minutes=30,
        )
        plain, _ = search_candidates_mod.search_candidates(
            berlin_wall(), dr, (0, -1, 0), 0.0, s, full_top=1)
        blocked, _ = search_candidates_mod.search_candidates(
            berlin_wall().model_copy(update={"obstacles": [hedge(45)]}),
            dr, (0, -1, 0), 0.0, s, full_top=1)
        d_plain = plain[0].model_dump(mode="json")
        d_block = blocked[0].model_dump(mode="json")
        assert "obstacle_losses" not in d_plain
        assert d_block["coverage"] < d_plain["coverage"]
        losses = d_block["obstacle_losses"]
        assert losses[0]["name"] == "hedge"
        assert losses[0]["blocked_minutes"] > 0
        # embedded full result names the same skyline
        rep = d_block["result"]["obstacle_report"]
        assert rep["losses"][0]["name"] == "hedge"

    def test_search_hash_changes_with_obstacle_geometry(self):
        dr = DateRange(start=date(2026, 3, 20), end=date(2026, 4, 5))
        s = SearchOptions(
            candidate_lengths=[0.25], min_spacing=0.02,
            base_grid_step=0.5, search_sample_minutes=30,
        )
        h0 = service.search_input_hash(
            berlin_wall(), dr, (0, -1, 0), 0.0, s)
        h1 = service.search_input_hash(
            berlin_wall().model_copy(update={"obstacles": [hedge(45)]}),
            dr, (0, -1, 0), 0.0, s)
        assert h0 != h1


# ------------------------------------------------------------ HTTP + DB --


class TestHTTP:
    def test_health(self):
        r = client.get("/health")
        assert r.status_code == 200
        assert r.json()["online_services"] == "none"

    def test_wall_create_list_get_update_versioning(self):
        body = berlin_wall().model_dump(mode="json")
        r = client.post("/walls", json={"wall": body})
        assert r.status_code == 200, r.text
        wid = r.json()["id"]

        listed = client.get("/walls").json()
        assert any(w["id"] == wid for w in listed)

        g = client.get(f"/walls/{wid}")
        assert g.status_code == 200
        assert g.json()["current_version"] == 1

        # identical geometry -> no new version
        same = client.put(f"/walls/{wid}", json={"wall": body})
        assert same.json()["created_new_version"] is False

        # changed azimuth -> new immutable version
        changed = body
        changed["azimuth"] = 200
        u = client.put(f"/walls/{wid}", json={"wall": changed})
        assert u.status_code == 200
        assert u.json()["created_new_version"] is True
        assert u.json()["version"] == 2

        versions = client.get(f"/walls/{wid}/versions").json()
        assert [v["version"] for v in versions] == [1, 2]
        assert versions[0]["geometry_hash"] != versions[1]["geometry_hash"]

    def test_version_generate_cache_consistency(self):
        body = berlin_wall().model_dump(mode="json")
        wid = client.post("/walls", json={"wall": body}).json()["id"]
        versions = client.get(f"/walls/{wid}/versions").json()
        vid = versions[-1]["id"]
        payload = {
            "date_range": YEAR.model_dump(mode="json"),
            "gnomon": gnomon().model_dump(mode="json"),
            "options": GenerateOptions(sample_minutes=40).model_dump(
                mode="json"),
        }
        r1 = client.post(
            f"/walls/{wid}/versions/{vid}/generate", json=payload)
        r2 = client.post(
            f"/walls/{wid}/versions/{vid}/generate", json=payload)
        assert r1.status_code == 200
        assert r1.json()["input_hash"] == r2.json()["input_hash"]
        assert r1.json()["svg"] == r2.json()["svg"]

    def test_search_endpoint_hash_matches_request(self):
        body = {
            "wall": berlin_wall(dst=eu_dst()).model_dump(mode="json"),
            "date_range": DateRange(
                start=date(2026, 3, 1), end=date(2026, 4, 30)
            ).model_dump(mode="json"),
            "direction": [0, -1, 0],
            "search": SearchOptions(
                time_mode="civil", candidate_lengths=[0.2, 0.3],
                min_spacing=0.02, base_grid_step=0.25,
                search_sample_minutes=60,
            ).model_dump(mode="json"),
            "full_top": 1,
        }
        r = client.post("/dial/search", json=body)
        assert r.status_code == 200, r.text
        data = r.json()
        assert data["candidates"][0]["result"]["mode"] == "civil"

        body["search"]["min_spacing"] = 0.04
        r2 = client.post("/dial/search", json=body)
        assert r2.json()["input_hash"] != data["input_hash"]

    def test_search_full_top_changes_hash_and_payload(self):
        base = {
            "wall": berlin_wall(dst=eu_dst()).model_dump(mode="json"),
            "date_range": DateRange(
                start=date(2026, 3, 1), end=date(2026, 4, 30)
            ).model_dump(mode="json"),
            "direction": [0, -1, 0],
            "search": SearchOptions(
                time_mode="civil", candidate_lengths=[0.2, 0.3],
                min_spacing=0.02, base_grid_step=0.25,
                search_sample_minutes=60,
            ).model_dump(mode="json"),
        }

        b0 = {**base, "full_top": 0}
        b1 = {**base, "full_top": 1}
        r0 = client.post("/dial/search", json=b0)
        r1 = client.post("/dial/search", json=b1)
        assert r0.status_code == r1.status_code == 200
        d0, d1 = r0.json(), r1.json()

        # hashes distinguish the two response shapes
        assert d0["input_hash"] != d1["input_hash"]
        assert d0["geometry_hash"] == d1["geometry_hash"]

        # payloads really do differ as full_top promises
        assert d0["candidates"][0]["result"] is None
        assert d1["candidates"][0]["result"] is not None
        assert d1["candidates"][0]["result"]["mode"] == "civil"

        # identical complete request is stable across repeats
        r1b = client.post("/dial/search", json=b1)
        assert r1b.json()["input_hash"] == d1["input_hash"]
        assert r1b.json()["candidates"] == d1["candidates"]

    def test_missing_wall_404(self):
        assert client.get("/walls/999999").status_code == 404
        assert client.put("/walls/999999", json={
            "wall": berlin_wall().model_dump(mode="json")
        }).status_code == 404

    def test_obstacles_freeze_with_version_and_generate(self):
        plain = berlin_wall().model_dump(mode="json")
        wid = client.post("/walls", json={"wall": plain}).json()["id"]
        with_ob = {**plain, "obstacles": [{
            "name": "hedge", "wrap": False,
            "points": [{"azimuth_deg": 120, "altitude_deg": 40},
                       {"azimuth_deg": 180, "altitude_deg": 40},
                       {"azimuth_deg": 240, "altitude_deg": 40}],
        }]}
        u = client.put(f"/walls/{wid}", json={"wall": with_ob})
        assert u.status_code == 200 and u.json()["created_new_version"]

        # old version round-trips without obstacles
        versions = client.get(f"/walls/{wid}/versions").json()
        assert versions[0]["geometry_hash"] != versions[1]["geometry_hash"]

        vid2 = versions[-1]["id"]
        payload = {
            "date_range": SPRING.model_dump(mode="json"),
            "gnomon": gnomon().model_dump(mode="json"),
            "options": GenerateOptions(sample_minutes=30).model_dump(
                mode="json"),
        }
        r = client.post(
            f"/walls/{wid}/versions/{vid2}/generate", json=payload)
        assert r.status_code == 200, r.text
        data = r.json()
        assert data["obstacle_report"]["losses"][0]["name"] == "hedge"
        assert data["obstacle_report"]["blocked_samples"] > 0
        assert any(iv["reason"] == "blocked_by_obstacle"
                   and iv["obstacle"] == "hedge"
                   for iv in data["invalid_intervals"])

        # same request is served from cache identically
        r2 = client.post(
            f"/walls/{wid}/versions/{vid2}/generate", json=payload)
        assert r2.json()["input_hash"] == data["input_hash"]
        assert r2.json()["svg"] == data["svg"]

    def test_obstacle_validation_rejected_over_http(self):
        body = berlin_wall().model_dump(mode="json")
        body["obstacles"] = [{
            "name": "bad",
            "points": [{"azimuth_deg": 200, "altitude_deg": 10},
                       {"azimuth_deg": 100, "altitude_deg": 10}],
        }]
        r = client.post("/walls", json={"wall": body})
        assert r.status_code == 422
        assert "increasing" in r.text


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
