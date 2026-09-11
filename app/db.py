"""SQLite persistence for wall models, versions and selected schemes.

Versions are immutable: changing geometry inserts a new row. Generation
results are keyed by their input hash so repeating a request for the same
version returns byte-identical JSON.
"""

from __future__ import annotations

import json
import os
import sqlite3
from datetime import datetime, timezone

from . import service
from .schemas import (
    DateRange, GenerateOptions, GnomonInput, Point2, WallInput,
)

DEFAULT_DB = os.environ.get(
    "SUNDIAL_DB", os.path.join(os.path.dirname(__file__), "..", "sundial.db")
)

SCHEMA = """
CREATE TABLE IF NOT EXISTS walls (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    current_version INTEGER NOT NULL DEFAULT 1,
    created_utc TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS wall_versions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    wall_id INTEGER NOT NULL REFERENCES walls(id),
    version INTEGER NOT NULL,
    geometry_hash TEXT NOT NULL,
    geometry_json TEXT NOT NULL,
    created_utc TEXT NOT NULL,
    UNIQUE(wall_id, version)
);
CREATE TABLE IF NOT EXISTS schemes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    wall_version_id INTEGER NOT NULL REFERENCES wall_versions(id),
    label TEXT NOT NULL DEFAULT 'selected',
    base_json TEXT NOT NULL,
    direction_json TEXT NOT NULL,
    length REAL NOT NULL,
    normal_offset REAL NOT NULL,
    label_offset_json NOT NULL,
    options_json NOT NULL,
    date_range_json NOT NULL,
    input_hash TEXT NOT NULL,
    result_json TEXT NOT NULL,
    created_utc TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS generation_cache (
    input_hash TEXT PRIMARY KEY,
    wall_version_id INTEGER REFERENCES wall_versions(id),
    result_json TEXT NOT NULL,
    created_utc TEXT NOT NULL
);
"""


def now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def connect(db_path: str | None = None) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path or DEFAULT_DB)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    conn.commit()


# --------------------------------------------------------------- walls --


def _geometry_json(wall: WallInput) -> str:
    return service.canonical_json({
        "latitude": wall.latitude,
        "longitude": wall.longitude,
        "standard_offset_minutes": wall.standard_offset_minutes,
        "dst": wall.dst.model_dump(mode="json"),
        "azimuth": wall.azimuth,
        "inclination": wall.inclination,
        "panel": wall.panel.model_dump(mode="json")
        if hasattr(wall.panel, "model_dump") else
        [p.model_dump(mode="json") for p in wall.panel],
    })


def _row_to_wall_input(name: str, geometry_json: str) -> WallInput:
    g = json.loads(geometry_json)
    return WallInput(
        name=name,
        latitude=g["latitude"],
        longitude=g["longitude"],
        standard_offset_minutes=g["standard_offset_minutes"],
        dst=g["dst"],
        azimuth=g["azimuth"],
        inclination=g["inclination"],
        panel=g["panel"],
    )


def create_wall(conn: sqlite3.Connection, wall: WallInput) -> dict:
    init_db(conn)
    ts = now()
    cur = conn.execute(
        "INSERT INTO walls (name, current_version, created_utc) VALUES (?,1,?)",
        (wall.name, ts),
    )
    wall_id = cur.lastrowid
    gh = service.geometry_hash(wall)
    conn.execute(
        "INSERT INTO wall_versions (wall_id, version, geometry_hash, "
        "geometry_json, created_utc) VALUES (?,?,1,?,?)",
        (wall_id, gh, _geometry_json(wall), ts),
    )
    conn.commit()
    return {
        "id": wall_id, "name": wall.name, "current_version": 1,
        "geometry_hash": gh, "created_utc": ts,
    }


def list_walls(conn: sqlite3.Connection) -> list[dict]:
    init_db(conn)
    rows = conn.execute(
        "SELECT w.id, name, current_version, geometry_hash, w.created_utc "
        "FROM walls w JOIN wall_versions v ON v.wall_id=w.id "
        "AND v.version=w.current_version ORDER BY w.id"
    ).fetchall()
    return [dict(r) for r in rows]


def _get(conn, table, key, value):
    return conn.execute(
        f"SELECT * FROM {table} WHERE {key}=?", (value,)
    ).fetchone()


def get_wall(conn: sqlite3.Connection, wall_id: int) -> dict | None:
    init_db(conn)
    wall_row = _get(conn, "walls", "id", wall_id)
    if wall_row is None:
        return None
    v_row = conn.execute(
        "SELECT * FROM wall_versions WHERE wall_id=? AND version=?",
        (wall_id, wall_row["current_version"]),
    ).fetchone()
    return {
        "wall": dict(wall_row),
        "version": dict(v_row),
        "input": _row_to_wall_input(wall_row["name"], v_row["geometry_json"]),
    }


def wall_input_of_version(conn, version_id) -> tuple[dict, WallInput] | None:
    v = _get(conn, "wall_versions", "id", version_id)
    if v is None:
        return None
    w = _get(conn, "walls", "id", v["wall_id"])
    wall = _row_to_wall_input(w["name"], v["geometry_json"])
    return dict(v), wall


def update_wall_geometry(conn, wall_id, wall: WallInput) -> tuple[dict, bool]:
    """Insert a new version. Returns (version_row, created_new).

    If the geometry hash matches the current version, nothing is inserted.
    """
    info = get_wall(conn, wall_id)
    if info is None:
        raise KeyError(wall_id)
    gh = service.geometry_hash(wall)
    current = info["version"]
    if gh == current["geometry_hash"]:
        return dict(current), False
    new_version_no = info["wall"]["current_version"] + 1
    ts = now()
    conn.execute(
        "INSERT INTO wall_versions (wall_id, version, geometry_hash, "
        "geometry_json, created_utc) VALUES (?,?,?,?,?)",
        (wall_id, new_version_no, gh, _geometry_json(wall), ts),
    )
    conn.execute(
        "UPDATE walls SET name=?, current_version=? WHERE id=?",
        (wall.name, new_version_no, wall_id),
    )
    conn.commit()
    row = conn.execute(
        "SELECT * FROM wall_versions WHERE wall_id=? AND version=?",
        (wall_id, new_version_no),
    ).fetchone()
    return dict(row), True


def list_versions(conn, wall_id) -> list[dict]:
    init_db(conn)
    rows = conn.execute(
        "SELECT * FROM wall_versions WHERE wall_id=? ORDER BY version",
        (wall_id,),
    ).fetchall()
    return [dict(r) for r in rows]


# ------------------------------------------------------------ caching ---


def cached_generation(conn, input_hash_hex: int | str):
    init_db(conn)
    row = conn.execute(
        "SELECT result_json FROM generation_cache WHERE input_hash=?",
        (input_hash_hex,),
    ).fetchone()
    return json.loads(row["result_json"]) if row else None


def store_generation(conn, input_hash_hex, version_id, result_json):
    conn.execute(
        "INSERT OR REPLACE INTO generation_cache "
        "(input_hash, wall_version_id, result_json, created_utc) "
        "VALUES (?,?,?,?)",
        (input_hash_hex, version_id, result_json, now()),
    )
    conn.commit()


# ------------------------------------------------------------- schemes --


def save_scheme(conn, wall_version_id, label, gnomon: GnomonInput,
                label_offset: Point2, options: GenerateOptions,
                dr: DateRange, result_json: str, input_hash_hex: str) -> dict:
    ts = now()
    cur = conn.execute(
        "INSERT INTO schemes (wall_version_id, label, base_json, "
        "direction_json, length, normal_offset, label_offset_json, "
        "options_json, date_range_json, input_hash, result_json, created_utc) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            wall_version_id, label,
            json.dumps(gnomon.base.model_dump(mode="json"), sort_keys=True),
            json.dumps(list(gnomon.direction), sort_keys=True),
            gnomon.length, gnomon.normal_offset,
            json.dumps(label_offset.model_dump(mode="json"), sort_keys=True),
            service.canonical_json(options),
            service.canonical_json(dr),
            input_hash_hex, result_json, ts,
        ),
    )
    conn.commit()
    return dict(_get(conn, "schemes", "id", cur.lastrowid))


def list_schemes(conn, wall_version_id) -> list[dict]:
    init_db(conn)
    rows = conn.execute(
        "SELECT * FROM schemes WHERE wall_version_id=? ORDER BY id",
        (wall_version_id,),
    ).fetchall()
    return [dict(r) for r in rows]


def get_scheme(conn, scheme_id) -> dict | None:
    init_db(conn)
    row = _get(conn, "schemes", "id", scheme_id)
    return dict(row) if row else None
