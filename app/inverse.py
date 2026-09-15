"""Inverse solve: measured shadow positions back to candidate times.

A request references an immutable wall version and a saved gnomon scheme,
then submits chronologically ordered panel coordinates, the observation
date (or date range), a coordinate tolerance and the admissible interval
between adjacent observations.  The solver first sweeps the range on a
coarse UTC grid (``coarse_step_minutes``) to locate, for every observed
point, the brackets where the continuous shadow trajectory enters the
tolerance disk; bisection then finds the entry/exit roots of
``|P(t) - q| = tolerance`` and a golden-section refinement returns the
closest-approach instant.  Every bracket is kept, so a point near a
self-intersecting trajectory (the same shadow position on different dates,
or both DST folds) yields multiple candidates.  Multi-observation requests
keep only time-ordered chains whose adjacent gaps respect the submitted
interval; when no chain matches, the report centres on the first
observation and lists the exclusion reasons.

All shading rules are inherited unchanged from the forward engine
(``engine._classify``): DST spring-forward gaps, sun behind the wall,
parallel rays, intersections outside the panel and named obstacle skylines
all remove the affected instants from the trajectory.  The solve is a pure
function of the request — the stored scheme is only read, never rewritten.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date, datetime, timedelta

from . import engine, geometry as G, service
from .obstacles import compile_contours
from .schemas import (
    GenerateOptions, GnomonInput, InverseCandidate, InverseChain,
    InverseChainFailure, InverseFailure, InverseObservationResult,
    InverseSolveRequest, InverseSolveResponse, Point2,
    TrajectorySegmentRef, WallInput,
)
from .timezone_engine import TimezoneEngine

_FMT = "%Y-%m-%dT%H:%M:%SZ"


@dataclass
class Run:
    """One continuous readable stretch of the coarse sweep."""

    index: int
    times: list[datetime]
    points: list[G.Pt]
    prev_time: datetime | None  # last non-ok sample before the run
    next_time: datetime | None  # first non-ok sample after the run


@dataclass
class Candidate:
    utc: datetime
    residual: float
    point: G.Pt
    run: Run
    window: tuple[datetime, datetime]


# ------------------------------------------------------ trajectory ------


class TrajectoryEvaluator:
    """Continuous shadow trajectory P(t) under the forward shading rules."""

    def __init__(self, wall: WallInput, tz: TimezoneEngine, frame,
                 gnomon: G.Gnomon, panel: list[G.Pt], eps: float, profiles):
        self.wall = wall
        self.tz = tz
        self.frame = frame
        self.gnomon = gnomon
        self.panel = panel
        self.eps = eps
        self.profiles = profiles

    def status_point(self, t: datetime) -> tuple[str, G.Pt | None]:
        sample = engine._sample_at(self.wall, self.tz, t)
        ev = engine._classify(
            sample, self.frame, self.gnomon, self.panel, self.eps,
            self.profiles,
        )
        return ev.status, ev.point

    def distance(self, t: datetime, q: G.Pt) -> float:
        """Distance of the shadow from q; +inf where no readable shadow."""
        status, p = self.status_point(t)
        if status != engine.OK or p is None:
            return math.inf
        return math.hypot(p[0] - q[0], p[1] - q[1])


def _build_runs(evaluated) -> list[Run]:
    """Group consecutive ok samples into trajectory segments (runs)."""
    runs: list[Run] = []
    n = len(evaluated)
    i = 0
    while i < n:
        if evaluated[i].status != engine.OK:
            i += 1
            continue
        j = i
        while j + 1 < n and evaluated[j + 1].status == engine.OK:
            j += 1
        runs.append(Run(
            index=len(runs),
            times=[evaluated[k].sample.utc for k in range(i, j + 1)],
            points=[evaluated[k].point for k in range(i, j + 1)],
            prev_time=evaluated[i - 1].sample.utc if i > 0 else None,
            next_time=evaluated[j + 1].sample.utc if j + 1 < n else None,
        ))
        i = j + 1
    return runs


# ------------------------------------------------------ root finding ----


def _round_second(t: datetime) -> datetime:
    return (t + timedelta(seconds=0.5)).replace(microsecond=0)


def _crossing(ev: TrajectoryEvaluator, q: G.Pt, tol: float,
              t_in: datetime, t_out: datetime, eps_seconds: float
              ) -> datetime:
    """Root of ``|P(t) - q| = tol`` between an inside and an outside time."""
    while abs((t_out - t_in).total_seconds()) > eps_seconds:
        mid = t_in + (t_out - t_in) / 2
        if ev.distance(mid, q) <= tol:
            t_in = mid
        else:
            t_out = mid
    return t_in


def _status_crossing(ev: TrajectoryEvaluator, t_ok: datetime,
                     t_bad: datetime, eps_seconds: float) -> datetime:
    """Boundary between a non-ok and an ok instant (panel edge, obstacle
    skyline, parallel-ray threshold, horizon)."""
    while abs((t_bad - t_ok).total_seconds()) > eps_seconds:
        mid = t_ok + (t_bad - t_ok) / 2
        status, _ = ev.status_point(mid)
        if status == engine.OK:
            t_ok = mid
        else:
            t_bad = mid
    return t_ok


def _golden_min(ev: TrajectoryEvaluator, q: G.Pt, a: datetime, b: datetime,
                eps_seconds: float) -> tuple[datetime, float]:
    """Closest-approach time of the trajectory to q within [a, b]."""
    if not b > a:
        return a, ev.distance(a, q)
    total = (b - a).total_seconds()
    invphi = (math.sqrt(5.0) - 1.0) / 2.0

    def f(x: float) -> float:
        return ev.distance(a + timedelta(seconds=x), q)

    lo, hi = 0.0, total
    c = hi - invphi * (hi - lo)
    d = lo + invphi * (hi - lo)
    fc, fd = f(c), f(d)
    while hi - lo > eps_seconds:
        if fc < fd:
            hi = d
            d, fd = c, fc
            c = hi - invphi * (hi - lo)
            fc = f(c)
        else:
            lo = c
            c, fc = d, fd
            d = lo + invphi * (hi - lo)
            fd = f(d)
    best_x = (lo + hi) / 2.0
    best_d = f(best_x)
    # guard against interrupted brackets (a status flip the coarse grid
    # missed): probe a small fixed grid and keep the overall best
    for k in range(5):
        x = total * k / 4.0
        dx = f(x)
        if dx < best_d:
            best_x, best_d = x, dx
    return a + timedelta(seconds=best_x), best_d


# ------------------------------------------------- per-observation -----


def _brackets(run: Run, q: G.Pt, tol: float):
    """Time brackets of ``run`` where the polyline passes within tol of q.

    Returns ``(t_lo, t_hi, left_status_bound, right_status_bound)`` tuples;
    a bound flag means the bracket reaches the run edge, so the true window
    is delimited by a status transition rather than a disk crossing.
    """
    n = len(run.times)
    if n == 1:
        p = run.points[0]
        if math.hypot(p[0] - q[0], p[1] - q[1]) <= tol:
            return [(run.times[0], run.times[0], True, True)]
        return []
    flagged = [
        i for i in range(n - 1)
        if G.distance_to_segment(q, run.points[i], run.points[i + 1]) <= tol
    ]
    if not flagged:
        return []
    out = []
    start = prev = flagged[0]
    for i in list(flagged[1:]) + [None]:
        if i is not None and i == prev + 1:
            prev = i
            continue
        out.append((run.times[start], run.times[prev + 1],
                    start == 0, prev == n - 2))
        start = prev = i
    return out


def _solve_observation(ev: TrajectoryEvaluator, runs: list[Run], q: G.Pt,
                       tol: float, eps_seconds: float, max_candidates: int):
    """All candidate instants whose shadow passes within tol of q."""
    nearest_d = math.inf
    nearest_t: datetime | None = None
    found: list[Candidate] = []
    for run in runs:
        for t, p in zip(run.times, run.points):
            d = math.hypot(p[0] - q[0], p[1] - q[1])
            if d < nearest_d:
                nearest_d, nearest_t = d, t
        for t_lo, t_hi, left_edge, right_edge in _brackets(run, q, tol):
            t_min, d_min = _golden_min(ev, q, t_lo, t_hi, eps_seconds)
            for t, p in zip(run.times, run.points):
                if t_lo <= t <= t_hi:
                    d = math.hypot(p[0] - q[0], p[1] - q[1])
                    if d < d_min:
                        t_min, d_min = t, d
            if not d_min <= tol:
                continue  # chord passed within tol but the curve bulged away
            if left_edge:
                b_in = (_status_crossing(ev, t_lo, run.prev_time, eps_seconds)
                        if run.prev_time is not None else t_lo)
            else:
                b_in = _crossing(ev, q, tol, t_min, t_lo, eps_seconds)
            if right_edge:
                b_out = (_status_crossing(ev, t_hi, run.next_time,
                                          eps_seconds)
                         if run.next_time is not None else t_hi)
            else:
                b_out = _crossing(ev, q, tol, t_min, t_hi, eps_seconds)
            # report the instant at whole-second search precision
            t_final = _round_second(t_min)
            status, p_final = ev.status_point(t_final)
            if status != engine.OK or p_final is None:
                t_final = t_min
                status, p_final = ev.status_point(t_min)
                if status != engine.OK or p_final is None:
                    continue
            residual = math.hypot(p_final[0] - q[0], p_final[1] - q[1])
            if residual > tol:
                continue
            found.append(Candidate(
                utc=t_final, residual=residual, point=p_final, run=run,
                window=(_round_second(b_in), _round_second(b_out)),
            ))
    found.sort(key=lambda c: (c.utc, c.residual))
    # two disjoint brackets may collapse onto the same rounded second
    deduped: list[Candidate] = []
    for c in found:
        if deduped and deduped[-1].utc == c.utc:
            if c.residual < deduped[-1].residual:
                deduped[-1] = c
        else:
            deduped.append(c)
    truncated = len(deduped) > max_candidates
    return deduped[:max_candidates], nearest_d, nearest_t, truncated


# ---------------------------------------------------------- chains ------


def _gap_ok(t_prev: datetime, t_next: datetime,
            imin: float | None, imax: float | None) -> bool:
    gap = (t_next - t_prev).total_seconds() / 60.0
    if gap <= 0:
        return False
    if imin is not None and gap < imin - 1e-9:
        return False
    if imax is not None and gap > imax + 1e-9:
        return False
    return True


def _chains(per_obs: list[list[Candidate]], imin: float | None,
            imax: float | None, max_chains: int):
    """Time-ordered chains taking one candidate per observation."""
    n = len(per_obs)
    found: list[list[tuple[int, Candidate]]] = []
    hard_limit = 5000

    def rec(k: int, chosen: list[tuple[int, Candidate]]):
        if len(found) >= hard_limit:
            return
        if k == n:
            found.append(list(chosen))
            return
        for idx, c in enumerate(per_obs[k]):
            if chosen and not _gap_ok(chosen[-1][1].utc, c.utc, imin, imax):
                continue
            rec(k + 1, chosen + [(idx, c)])

    rec(0, [])
    total = len(found)
    found.sort(key=lambda ch: (
        round(sum(c.residual for _, c in ch), 6),
        ch[0][1].utc,
        tuple(i for i, _ in ch),
    ))
    return found[:max_chains], total


# -------------------------------------------------------- diagnostics ---


def _status_summary(evaluated) -> str:
    counts: dict[str, int] = {}
    blocked: dict[str, int] = {}
    for e in evaluated:
        counts[e.status] = counts.get(e.status, 0) + 1
        if e.status == engine.BLOCKED and e.obstacle is not None:
            blocked[e.obstacle.name] = blocked.get(e.obstacle.name, 0) + 1
    parts = []
    for st in (engine.OK, engine.OUTSIDE, engine.BEHIND, engine.PARALLEL,
               engine.BLOCKED, engine.DST_GAP, engine.BELOW):
        c = counts.get(st, 0)
        if not c:
            continue
        if st == engine.BLOCKED and blocked:
            names = ", ".join(f"{k}: {v}" for k, v in sorted(blocked.items()))
            parts.append(f"{c} {st} ({names})")
        else:
            parts.append(f"{c} {st}")
    return "search-range samples: " + ", ".join(parts)


def _failure(req: InverseSolveRequest,
             obs_models: list[InverseObservationResult],
             per_obs: list[list[Candidate]], evaluated) -> InverseFailure:
    """Exclusion report centred on the first observation."""
    reasons: list[str] = []
    chain_failures: list[InverseChainFailure] = []
    tol = req.tolerance
    first = obs_models[0]
    if not per_obs[0]:
        obs = req.observations[0]
        reasons.append(
            f"observation 0 at ({obs.x}, {obs.y}): no shadow trajectory "
            f"point within tolerance {tol} m"
        )
        if first.nearest_distance_m is not None:
            reasons.append(
                f"nearest readable shadow passes "
                f"{first.nearest_distance_m:.4f} m away at "
                f"{first.nearest_utc} (tolerance {tol} m)"
            )
        else:
            reasons.append(
                "the shadow never lands on the panel during the search "
                "range"
            )
        reasons.append(_status_summary(evaluated))
        return InverseFailure(
            observation_index=0, reasons=reasons, chain_failures=[],
        )

    imin = req.interval_min_minutes
    imax = req.interval_max_minutes
    reasons.append(
        f"no consistent time chain: none of the "
        f"{len(per_obs[0])} candidate(s) of observation 0 can be extended "
        f"through all {len(req.observations)} observations within the "
        f"interval [{imin}, {imax}] min"
    )
    for c0 in per_obs[0][:10]:
        prev = c0
        for k in range(1, len(req.observations)):
            feas = [c for c in per_obs[k]
                    if _gap_ok(prev.utc, c.utc, imin, imax)]
            if feas:
                prev = min(feas, key=lambda c: c.utc)
                continue
            if not per_obs[k]:
                detail = (
                    f"observation {k} has no candidate at all (nearest "
                    f"approach {obs_models[k].nearest_distance_m} m"
                )
                if obs_models[k].nearest_utc:
                    detail += f" at {obs_models[k].nearest_utc}"
                detail += ")"
            else:
                nearest_c = min(
                    per_obs[k],
                    key=lambda c: abs(
                        (c.utc - prev.utc).total_seconds()),
                )
                gap = (nearest_c.utc - prev.utc).total_seconds() / 60.0
                detail = (
                    f"nearest candidate of observation {k} is at "
                    f"{nearest_c.utc.strftime(_FMT)} ({gap:+.1f} min from "
                    f"{prev.utc.strftime(_FMT)}, outside "
                    f"[{imin}, {imax}] min)"
                )
            chain_failures.append(InverseChainFailure(
                first_candidate_utc=c0.utc.strftime(_FMT),
                failed_at_observation=k,
                reason=detail,
            ))
            break
    return InverseFailure(
        observation_index=0, reasons=reasons,
        chain_failures=chain_failures,
    )


# ------------------------------------------------------------ solve -----


def _overlap_intervals(tz: TimezoneEngine, d0: date, d1: date):
    """Local-clock intervals repeated by fall-back transitions."""
    start = datetime.combine(d0, datetime.min.time()) - timedelta(days=2)
    end = datetime.combine(d1, datetime.min.time()) + timedelta(days=3)
    out = []
    for t in tz.transitions_between(start, end):
        if t.kind != "end":
            continue
        out.append((t.utc + timedelta(minutes=t.offset_after),
                    t.utc + timedelta(minutes=t.offset_before)))
    return out


def _candidate_model(wall: WallInput, tz: TimezoneEngine, c: Candidate,
                     overlaps) -> InverseCandidate:
    s = engine._sample_at(wall, tz, c.utc)
    return InverseCandidate(
        utc=c.utc.strftime(_FMT),
        local_clock=s.local.strftime("%Y-%m-%dT%H:%M")
        + ("D" if s.dst else "S"),
        solar_time_min=round(s.solar_min, 3),
        dst=s.dst,
        dst_overlap=any(a <= s.local < b for a, b in overlaps),
        residual_m=round(c.residual, 6),
        point=Point2(x=round(c.point[0], 5), y=round(c.point[1], 5)),
        segment=TrajectorySegmentRef(
            index=c.run.index,
            date=c.run.times[0].date(),
            start_utc=c.run.times[0].strftime(_FMT),
            end_utc=c.run.times[-1].strftime(_FMT),
        ),
        window_start_utc=c.window[0].strftime(_FMT),
        window_end_utc=c.window[1].strftime(_FMT),
    )


def solve_inverse(wall: WallInput, wall_id: int, version_row: dict,
                  scheme_row: dict, gnomon_input: GnomonInput,
                  req: InverseSolveRequest) -> InverseSolveResponse:
    dr = req.effective_range()
    gen_opts = GenerateOptions()
    eps = gen_opts.parallel_cos_threshold
    frame = G.make_frame(wall.azimuth, wall.inclination)
    panel = [(p.x, p.y) for p in wall.panel]
    gnomon = G.Gnomon(
        base=(gnomon_input.base.x, gnomon_input.base.y),
        direction=tuple(gnomon_input.direction),
        length=gnomon_input.length,
        normal_offset=gnomon_input.normal_offset,
    )
    profiles = compile_contours(wall)
    opts = req.options

    samples, tz, _gaps = engine.build_samples(
        wall, dr.start, dr.end, opts.coarse_step_minutes
    )
    evaluated = engine.evaluate_samples(
        samples, frame, gnomon, panel, gen_opts, profiles
    )
    runs = _build_runs(evaluated)
    ev = TrajectoryEvaluator(wall, tz, frame, gnomon, panel, eps, profiles)
    overlaps = _overlap_intervals(tz, dr.start, dr.end)

    obs_models: list[InverseObservationResult] = []
    per_obs: list[list[Candidate]] = []
    for idx, obs in enumerate(req.observations):
        q = (obs.x, obs.y)
        cands, nd, nt, truncated = _solve_observation(
            ev, runs, q, req.tolerance, opts.root_epsilon_seconds,
            opts.max_candidates_per_observation,
        )
        per_obs.append(cands)
        obs_models.append(InverseObservationResult(
            index=idx,
            observed=Point2(x=obs.x, y=obs.y),
            candidate_count=len(cands),
            truncated=truncated,
            nearest_distance_m=round(nd, 6) if math.isfinite(nd) else None,
            nearest_utc=nt.strftime(_FMT) if nt is not None else None,
            candidates=[_candidate_model(wall, tz, c, overlaps)
                        for c in cands],
        ))

    chains_raw, chain_total = _chains(
        per_obs, req.interval_min_minutes, req.interval_max_minutes,
        opts.max_chains,
    )
    chain_models = [
        InverseChain(
            candidate_indices=[i for i, _ in ch],
            utc=[c.utc.strftime(_FMT) for _, c in ch],
            intervals_minutes=[
                round(
                    (ch[k + 1][1].utc - ch[k][1].utc).total_seconds() / 60.0,
                    3,
                )
                for k in range(len(ch) - 1)
            ],
            total_residual_m=round(sum(c.residual for _, c in ch), 6),
        )
        for ch in chains_raw
    ]
    matched = chain_total > 0
    failure = None
    if not matched:
        failure = _failure(req, obs_models, per_obs, evaluated)

    ihash = service.inverse_input_hash(
        wall, gnomon_input, req.scheme_id, req.observations, dr,
        req.tolerance, req.interval_min_minutes, req.interval_max_minutes,
        opts, eps,
    )
    return InverseSolveResponse(
        input_hash=ihash,
        geometry_hash=service.geometry_hash(wall),
        algorithm_version=service.ALGORITHM_VERSION,
        wall_id=wall_id,
        version=version_row["version"],
        version_id=version_row["id"],
        scheme_id=scheme_row["id"],
        matched=matched,
        chain_count=chain_total,
        precision={
            "coarse_step_minutes": opts.coarse_step_minutes,
            "root_epsilon_seconds": opts.root_epsilon_seconds,
            "tolerance_m": req.tolerance,
        },
        observations=obs_models,
        chains=chain_models,
        failure=failure,
    )
