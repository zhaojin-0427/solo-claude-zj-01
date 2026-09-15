"""FastAPI application: local sundial wall-dial planning service.

Run with ``uvicorn app.main:app``. Everything runs offline; the only fixed
algorithm is the bundled NOAA solar calculation.
"""

from __future__ import annotations

import json
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

from . import db, inverse as inverse_mod, search as search_mod, service
from .schemas import (
    DateRange, DialResult, GenerateOptions, GnomonInput, InverseSolveRequest,
    InverseSolveResponse, Point2, SearchOptions, SearchResponse, WallInput,
)


class GenerateBody(BaseModel):
    wall: WallInput
    date_range: DateRange
    gnomon: GnomonInput
    options: GenerateOptions = Field(default_factory=GenerateOptions)
    label_offset: Point2 = Field(default_factory=lambda: Point2(x=0.0, y=0.0))


class AnalyzeBody(GenerateBody):
    pass


class SearchBody(BaseModel):
    wall: WallInput
    date_range: DateRange
    direction: tuple[float, float, float]
    normal_offset: float = 0.0
    search: SearchOptions
    full_top: int = Field(3, ge=0, le=10)


class CreateWallBody(BaseModel):
    wall: WallInput


class UpdateWallBody(BaseModel):
    wall: WallInput


class VersionGenerateBody(BaseModel):
    date_range: DateRange
    gnomon: GnomonInput
    options: GenerateOptions = Field(default_factory=GenerateOptions)
    label_offset: Point2 = Field(default_factory=lambda: Point2(x=0.0, y=0.0))
    use_cache: bool = True


class SaveSchemeBody(VersionGenerateBody):
    label: str = "selected"


class WallSearchBody(BaseModel):
    date_range: DateRange
    direction: tuple[float, float, float]
    normal_offset: float = 0.0
    search: SearchOptions
    full_top: int = Field(3, ge=0, le=10)


def get_conn():
    conn = db.connect()
    db.init_db(conn)
    return conn


@asynccontextmanager
async def lifespan(app: FastAPI):
    conn = get_conn()
    conn.close()
    yield


app = FastAPI(
    title="Wall Sundial Planner",
    version="1.0.0",
    description="Offline solar-dial line calculation, checks and search.",
    lifespan=lifespan,
)


# ------------------------------------------------------------- health ---


@app.get("/health")
def health():
    return {"status": "ok", "algorithm": service.ALGORITHM_VERSION,
            "online_services": "none"}


# -------------------------------------------------- stateless generation --


@app.post("/dial/generate", response_model=DialResult)
def dial_generate(body: GenerateBody):
    return service.generate_dial(
        body.wall, body.gnomon, body.date_range, body.options,
        label_offset=(body.label_offset.x, body.label_offset.y),
    )


@app.post("/dial/analyze", response_model=DialResult)
def dial_analyze(body: AnalyzeBody):
    """Alias of generate emphasizing the check report."""
    return dial_generate(body)


@app.post("/dial/search", response_model=SearchResponse)
def dial_search(body: SearchBody):
    candidates, total = search_mod.search_candidates(
        body.wall, body.date_range, tuple(body.direction),
        body.normal_offset, body.search, full_top=body.full_top,
    )
    ih = service.search_input_hash(
        body.wall, body.date_range, tuple(body.direction),
        body.normal_offset, body.search, body.full_top,
    )
    return SearchResponse(
        input_hash=ih,
        geometry_hash=service.geometry_hash(body.wall),
        candidates=candidates, searched=total,
    )


# ------------------------------------------------------------- walls ----


@app.post("/walls")
def create_wall(body: CreateWallBody):
    conn = get_conn()
    try:
        return db.create_wall(conn, body.wall)
    finally:
        conn.close()


@app.get("/walls")
def list_walls():
    conn = get_conn()
    try:
        return db.list_walls(conn)
    finally:
        conn.close()


@app.get("/walls/{wall_id}")
def get_wall(wall_id: int):
    conn = get_conn()
    try:
        info = db.get_wall(conn, wall_id)
        if info is None:
            raise HTTPException(404, "wall not found")
        w = info["wall"]
        v = info["version"]
        return {
            "id": w["id"], "name": w["name"],
            "current_version": w["current_version"],
            "geometry_hash": v["geometry_hash"],
            "created_utc": w["created_utc"],
            "wall": json.loads(v["geometry_json"]),
        }
    finally:
        conn.close()


@app.put("/walls/{wall_id}")
def update_wall(wall_id: int, body: UpdateWallBody):
    """Change geometry: a new immutable version is created unless the
    geometry hash is unchanged."""
    conn = get_conn()
    try:
        try:
            row, created = db.update_wall_geometry(conn, wall_id, body.wall)
        except KeyError:
            raise HTTPException(404, "wall not found")
        return {
            "wall_id": wall_id, "version": row["version"],
            "geometry_hash": row["geometry_hash"],
            "created_new_version": created, "created_utc": row["created_utc"],
        }
    finally:
        conn.close()


@app.get("/walls/{wall_id}/versions")
def list_versions(wall_id: int):
    conn = get_conn()
    try:
        return db.list_versions(conn, wall_id)
    finally:
        conn.close()


# -------------------------------------------------- version generation --


def _version_or_404(conn, wall_id, version_id):
    found = db.wall_input_of_version(conn, version_id)
    if found is None or found[0]["wall_id"] != wall_id:
        raise HTTPException(404, "version not found")
    return found


@app.post("/walls/{wall_id}/versions/{version_id}/generate",
          response_model=DialResult)
def version_generate(wall_id: int, version_id: int, body: VersionGenerateBody):
    conn = get_conn()
    try:
        found = _version_or_404(conn, wall_id, version_id)
        vrow, wall = found
        ih = service.input_hash(
            wall, body.gnomon, body.date_range, body.options,
            (body.label_offset.x, body.label_offset.y),
        )
        if body.use_cache:
            cached = db.cached_generation(conn, ih)
            if cached is not None:
                return DialResult.model_validate(cached)
        result = service.generate_dial(
            wall, body.gnomon, body.date_range, body.options,
            label_offset=(body.label_offset.x, body.label_offset.y),
        )
        db.store_generation(conn, ih, version_id,
                            result.model_dump_json())
        return result
    finally:
        conn.close()


@app.post("/walls/{wall_id}/versions/{version_id}/search",
          response_model=SearchResponse)
def version_search(wall_id: int, version_id: int, body: WallSearchBody):
    conn = get_conn()
    try:
        found = _version_or_404(conn, wall_id, version_id)
        _, wall = found
        candidates, total = search_mod.search_candidates(
            wall, body.date_range, tuple(body.direction),
            body.normal_offset, body.search, full_top=body.full_top,
        )
        ih = service.search_input_hash(
            wall, body.date_range, tuple(body.direction),
            body.normal_offset, body.search, body.full_top,
        )
        return SearchResponse(
            input_hash=ih,
            geometry_hash=service.geometry_hash(wall),
            candidates=candidates, searched=total,
        )
    finally:
        conn.close()


# -------------------------------------------------------------- inverse --


@app.post("/walls/{wall_id}/versions/{version_id}/inverse",
          response_model=InverseSolveResponse)
def version_inverse(wall_id: int, version_id: int, body: InverseSolveRequest):
    """Inverse lookup: measured shadow points -> candidate times.

    Reads the immutable wall version and the referenced gnomon scheme; the
    scheme and the wall version are never modified by the solve.
    """
    conn = get_conn()
    try:
        vrow, wall = _version_or_404(conn, wall_id, version_id)
        scheme = db.get_scheme(conn, body.scheme_id)
        if scheme is None:
            raise HTTPException(404, "scheme not found")
        if scheme["wall_version_id"] != version_id:
            raise HTTPException(
                422,
                f"scheme {body.scheme_id} belongs to wall version "
                f"{scheme['wall_version_id']}, not version {version_id}",
            )
        gnomon = GnomonInput(
            base=Point2(**json.loads(scheme["base_json"])),
            direction=tuple(json.loads(scheme["direction_json"])),
            length=scheme["length"],
            normal_offset=scheme["normal_offset"],
        )
        return inverse_mod.solve_inverse(
            wall, wall_id, vrow, scheme, gnomon, body
        )
    finally:
        conn.close()


# -------------------------------------------------------------- schemes --


@app.post("/walls/{wall_id}/versions/{version_id}/schemes",
          response_model=DialResult)
def save_scheme(wall_id: int, version_id: int, body: SaveSchemeBody):
    conn = get_conn()
    try:
        _, wall = _version_or_404(conn, wall_id, version_id)
        result = service.generate_dial(
            wall, body.gnomon, body.date_range, body.options,
            label_offset=(body.label_offset.x, body.label_offset.y),
        )
        db.save_scheme(
            conn, version_id, body.label, body.gnomon, body.label_offset,
            body.options, body.date_range, result.model_dump_json(),
            result.input_hash,
        )
        return result
    finally:
        conn.close()


@app.get("/walls/{wall_id}/versions/{version_id}/schemes")
def list_schemes(wall_id: int, version_id: int):
    conn = get_conn()
    try:
        _version_or_404(conn, wall_id, version_id)
        rows = db.list_schemes(conn, version_id)
        for r in rows:
            r["base"] = json.loads(r.pop("base_json"))
            r["direction"] = json.loads(r.pop("direction_json"))
            r["label_offset"] = json.loads(r.pop("label_offset_json"))
            r["options"] = json.loads(r.pop("options_json"))
            r["date_range"] = json.loads(r.pop("date_range_json"))
        return rows
    finally:
        conn.close()


@app.get("/schemes/{scheme_id}")
def get_scheme(scheme_id: int):
    conn = get_conn()
    try:
        row = db.get_scheme(conn, scheme_id)
        if row is None:
            raise HTTPException(404, "scheme not found")
        row["result"] = json.loads(row.pop("result_json"))
        row["base"] = json.loads(row.pop("base_json"))
        row["direction"] = json.loads(row.pop("direction_json"))
        row["label_offset"] = json.loads(row.pop("label_offset_json"))
        row["options"] = json.loads(row.pop("options_json"))
        row["date_range"] = json.loads(row.pop("date_range_json"))
        return row
    finally:
        conn.close()
