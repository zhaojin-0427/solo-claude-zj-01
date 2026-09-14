"""Regression tests covering the reported bugs and core guarantees."""

import os
import tempfile
from datetime import date, datetime

import pytest

# isolate DB before importing the app
_TMP_DB = tempfile.mktemp(suffix=".db")
os.environ["SUNDIAL_DB"] = _TMP_DB

from fastapi.testclient import TestClient  # noqa: E402

from app import engine, geometry as G, service  # noqa: E402
from app.main import app  # noqa: E402
from app.schemas import (  # noqa: E402
    DateRange, DSTRule, GenerateOptions, GnomonInput, Point2, SearchOptions,
    WallInput, Weekday,
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


YEAR = DateRange(start=date(2026, 1, 1), end=date(2026, 12, 31))


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


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
