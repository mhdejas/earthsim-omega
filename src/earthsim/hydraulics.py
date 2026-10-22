"""2D local-inertial shallow-water solver (Bates, Horritt & Fewtrell 2010).

Governing equations
-------------------
Continuity:   dh/dt + dqx/dx + dqy/dy = R(t)            (R = net rainfall rate, m/s)
Momentum (local inertial: advection term dropped):
              dq/dt + g h_f d(h+z)/dx + g n^2 q|q| / h_f^(7/3) = 0

Discretisation (Bates et al. 2010, eq. 11; semi-implicit friction):
    q^{t+dt} = (q^t - g h_f dt * S) / (1 + g dt n^2 |q^t| / h_f^(7/3))
    S   = (eta_j - eta_i) / dx  (water-surface slope across the face)
    h_f = max(eta_i, eta_j) - max(z_i, z_j)   (flow depth at the face)
    h^{t+dt} = h^t + dt * (sum q_in - sum q_out) / dx + R dt
Stability (CFL-type, Bates 2010 eq. 12):  dt = alpha * dx / sqrt(g * h_max),
alpha = 0.7 by default, capped at dt_max.

Assumptions and limits (stated, not hidden)
* Advection is neglected: valid for gradually varied, subcritical floodplain
  flow; NOT for dam breaks, hydraulic jumps or steep supercritical channels.
* Faces with h_f <= h_min (1e-3 m) carry no flow (wet/dry treatment).
* Positivity: if a cell's outgoing volume in a step exceeds its stored water,
  all its outgoing face fluxes are scaled down so depth never goes negative.
* Boundaries: ``ocean`` cells hold h=0 at mean sea level and absorb inflow
  (volume tallied). Domain edges are closed unless ``open_edges`` (free
  outflow at the critical-depth rate q = h*sqrt(g*h)), with volume tallied.
* Bathymetry is absent in the DEM (Copernicus GLO-30 is a DSM surface), so
  river channel conveyance is underestimated; Manning n is a scalar or a grid.

Mass balance: every step tracks rain input, ocean absorption and edge outflow,
so  V(t) - V(0) = V_rain - V_ocean - V_edge  is checked to rounding error.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Callable

import numba as nb
import numpy as np

G = 9.81


@nb.njit(parallel=True, cache=True)
def _fluxes(h, z, n2, qx, qy, dt, dx, hmin):
    ny, nx = h.shape
    for i in nb.prange(ny):
        for j in range(nx - 1):
            e1 = h[i, j] + z[i, j]
            e2 = h[i, j + 1] + z[i, j + 1]
            hf = max(e1, e2) - max(z[i, j], z[i, j + 1])
            if hf > hmin:
                s = (e2 - e1) / dx
                nn = 0.5 * (n2[i, j] + n2[i, j + 1])
                q0 = qx[i, j]
                qx[i, j] = (q0 - G * hf * dt * s) / (1.0 + G * dt * nn * abs(q0) / hf ** (7.0 / 3.0))
            else:
                qx[i, j] = 0.0
    for i in nb.prange(ny - 1):
        for j in range(nx):
            e1 = h[i, j] + z[i, j]
            e2 = h[i + 1, j] + z[i + 1, j]
            hf = max(e1, e2) - max(z[i, j], z[i + 1, j])
            if hf > hmin:
                s = (e2 - e1) / dx
                nn = 0.5 * (n2[i, j] + n2[i + 1, j])
                q0 = qy[i, j]
                qy[i, j] = (q0 - G * hf * dt * s) / (1.0 + G * dt * nn * abs(q0) / hf ** (7.0 / 3.0))
            else:
                qy[i, j] = 0.0


@nb.njit(parallel=True, cache=True)
def _limit(h, qx, qy, dt, dx, fac):
    """Positivity: fac[i,j] in (0,1] scales the outgoing fluxes of cell (i,j)."""
    ny, nx = h.shape
    for i in nb.prange(ny):
        for j in range(nx):
            out = 0.0
            if j < nx - 1 and qx[i, j] > 0:
                out += qx[i, j]
            if j > 0 and qx[i, j - 1] < 0:
                out -= qx[i, j - 1]
            if i < ny - 1 and qy[i, j] > 0:
                out += qy[i, j]
            if i > 0 and qy[i - 1, j] < 0:
                out -= qy[i - 1, j]
            vol_out = out * dt / dx  # depth units
            fac[i, j] = 1.0 if vol_out <= h[i, j] or vol_out <= 0 else h[i, j] / vol_out
    for i in nb.prange(ny):
        for j in range(nx - 1):
            if qx[i, j] > 0:
                qx[i, j] *= fac[i, j]
            elif qx[i, j] < 0:
                qx[i, j] *= fac[i, j + 1]
    for i in nb.prange(ny - 1):
        for j in range(nx):
            if qy[i, j] > 0:
                qy[i, j] *= fac[i, j]
            elif qy[i, j] < 0:
                qy[i, j] *= fac[i + 1, j]


@nb.njit(parallel=True, cache=True)
def _update(h, qx, qy, dt, dx, rain_depth, ocean, active):
    ny, nx = h.shape
    absorbed = np.zeros(ny)
    for i in nb.prange(ny):
        acc = 0.0
        for j in range(nx):
            if not active[i, j]:
                continue
            dq = 0.0
            if j > 0:
                dq += qx[i, j - 1]
            if j < nx - 1:
                dq -= qx[i, j]
            if i > 0:
                dq += qy[i - 1, j]
            if i < ny - 1:
                dq -= qy[i, j]
            hn = h[i, j] + dt * dq / dx + rain_depth
            if hn < 0.0:
                hn = 0.0  # only rounding-level negatives survive the limiter
            if ocean[i, j]:
                acc += hn
                hn = 0.0
            h[i, j] = hn
        absorbed[i] = acc
    return absorbed.sum()


@nb.njit(cache=True)
def _edge_outflow(h, z, dt, dx, hmin):
    """Free outflow across the 4 domain edges at critical-depth rate; returns removed depth-sum."""
    ny, nx = h.shape
    rem = 0.0
    for i in range(ny):
        for j in (0, nx - 1):
            if h[i, j] > hmin:
                q = h[i, j] * math.sqrt(G * h[i, j])
                d = min(h[i, j], q * dt / dx)
                h[i, j] -= d
                rem += d
    for j in range(1, nx - 1):
        for i in (0, ny - 1):
            if h[i, j] > hmin:
                q = h[i, j] * math.sqrt(G * h[i, j])
                d = min(h[i, j], q * dt / dx)
                h[i, j] -= d
                rem += d
    return rem


@dataclass
class SolverConfig:
    dx: float
    manning: float | np.ndarray = 0.05
    alpha: float = 0.7
    dt_max: float = 30.0
    h_min: float = 1e-3
    open_edges: bool = False
    output_every_s: float = 3600.0
    wet_threshold_m: float = 0.1


@dataclass
class SolverResult:
    t: list[float] = field(default_factory=list)
    snapshots: list[np.ndarray] = field(default_factory=list)
    h_max: np.ndarray | None = None
    v_max: np.ndarray | None = None
    t_arrival: np.ndarray | None = None
    h_final: np.ndarray | None = None
    steps: int = 0
    mass: dict[str, float] = field(default_factory=dict)
    diagnostics: dict[str, Any] = field(default_factory=dict)


def run(z: np.ndarray, cfg: SolverConfig, t_end: float, rain_rate: Callable[[float], float] | None = None,
        h0: np.ndarray | None = None, ocean: np.ndarray | None = None, active: np.ndarray | None = None,
        bc: Callable[[float, np.ndarray], None] | None = None, progress: Callable[[float, int], None] | None = None
        ) -> SolverResult:
    """Run the solver. ``rain_rate(t)`` returns net rainfall in m/s (spatially uniform over ``active``).
    ``bc(t, h)`` may impose depths in place (e.g. an inflow hydrograph boundary); its volume is tallied."""
    z = np.ascontiguousarray(z, dtype=np.float64)
    if not np.all(np.isfinite(z)):
        raise ValueError("terrain contains NaN/inf; fill or mask before simulation")
    ny, nx = z.shape
    h = np.zeros_like(z) if h0 is None else np.ascontiguousarray(h0, dtype=np.float64).copy()
    ocean = np.zeros(z.shape, np.bool_) if ocean is None else np.ascontiguousarray(ocean, np.bool_)
    active = np.ones(z.shape, np.bool_) if active is None else np.ascontiguousarray(active, np.bool_)
    n2 = np.broadcast_to(np.asarray(cfg.manning, np.float64) ** 2, z.shape).copy()
    qx = np.zeros((ny, nx - 1 if nx > 1 else 1), np.float64)
    qy = np.zeros((ny - 1 if ny > 1 else 1, nx), np.float64)
    qx = np.zeros((ny, nx), np.float64)  # last column unused
    qy = np.zeros((ny, nx), np.float64)  # last row unused
    fac = np.ones_like(z)
    dx = float(cfg.dx)
    cell = dx * dx
    res = SolverResult()
    res.h_max = h.copy()
    res.v_max = np.zeros_like(z)
    res.t_arrival = np.full(z.shape, np.nan)
    v0 = float(h[active].sum() * cell)
    v_rain = v_ocean = v_edge = v_bc = 0.0
    t, next_out, steps, dts = 0.0, cfg.output_every_s, 0, []
    n_active = int(active.sum())
    while t < t_end - 1e-9:
        hm = float(h.max())
        dt = cfg.dt_max if hm <= cfg.h_min else min(cfg.dt_max, cfg.alpha * dx / math.sqrt(G * hm))
        dt = min(dt, t_end - t, max(next_out - t, 1e-6))
        if bc is not None:
            before = float(h[active].sum())
            bc(t, h)
            v_bc += (float(h[active].sum()) - before) * cell
        _fluxes(h, z, n2, qx, qy, dt, dx, cfg.h_min)
        _limit(h, qx, qy, dt, dx, fac)
        r = rain_rate(t) * dt if rain_rate is not None else 0.0
        v_rain += r * n_active * cell
        v_ocean += _update(h, qx, qy, dt, dx, r, ocean, active) * cell
        if cfg.open_edges:
            v_edge += _edge_outflow(h, z, dt, dx, cfg.h_min) * cell
        t += dt
        steps += 1
        dts.append(dt)
        np.maximum(res.h_max, h, out=res.h_max)
        if steps % 10 == 0 or t >= t_end - 1e-9:
            with np.errstate(divide="ignore", invalid="ignore"):
                ux = np.abs(qx) / np.maximum(h, cfg.h_min)
                uy = np.abs(qy) / np.maximum(h, cfg.h_min)
                v = np.where(h > cfg.wet_threshold_m, np.sqrt(ux ** 2 + uy ** 2), 0.0)
            np.maximum(res.v_max, v, out=res.v_max)
            newly = np.isnan(res.t_arrival) & (h > cfg.wet_threshold_m)
            res.t_arrival[newly] = t
        if t >= next_out - 1e-9:
            res.t.append(t)
            res.snapshots.append(h.astype(np.float32))
            next_out += cfg.output_every_s
            if progress:
                progress(t, steps)
        if not np.isfinite(hm):
            raise FloatingPointError(f"non-finite depth at t={t:.1f}s step {steps}")
    v1 = float(h[active].sum() * cell)
    res.h_final = h
    res.steps = steps
    err = (v1 - v0) - (v_rain + v_bc - v_ocean - v_edge)
    denom = max(v_rain + v_bc + v0, 1e-12)
    res.mass = {"v_initial_m3": v0, "v_final_m3": v1, "v_rain_m3": v_rain, "v_boundary_in_m3": v_bc,
                "v_ocean_out_m3": v_ocean, "v_edge_out_m3": v_edge, "residual_m3": err,
                "relative_residual": abs(err) / denom}
    res.diagnostics = {"dt_min_s": float(min(dts)) if dts else 0.0, "dt_mean_s": float(np.mean(dts)) if dts else 0.0,
                       "dt_max_s": float(max(dts)) if dts else 0.0, "negative_depth_cells": int((h < 0).sum()),
                       "nan_cells": int((~np.isfinite(h)).sum()), "h_max_m": float(res.h_max.max()),
                       "v_max_ms": float(res.v_max.max()), "cells": int(z.size), "active_cells": n_active}
    return res


# ------------------------------------------------------------ benchmark cases
def hunter_wetting_front(u: float = 1.0, n: float = 0.01, dx: float = 10.0, length: float = 5000.0,
                         t_end: float = 3600.0, alpha: float = 0.7) -> dict[str, Any]:
    """Hunter et al. (2005) / Bates et al. (2010) test 1: a wetting front advancing at
    constant speed u over a horizontal frictional plane. Analytical depth:
        h(x, t) = [ -(7/3) n^2 u^2 (x - u t) ]^(3/7)   for x <= u t, else 0,
    driven by the upstream boundary h(0, t) = [ (7/3) n^2 u^3 t ]^(3/7)."""
    nx = int(length / dx)
    z = np.zeros((1, nx))
    x = (np.arange(nx) + 0.5) * dx

    def analytic(t):
        a = -(7.0 / 3.0) * n * n * u * u * (x - u * t)
        return np.where(a > 0, np.maximum(a, 0) ** (3.0 / 7.0), 0.0)

    def bc(t, h):
        h[0, 0] = ((7.0 / 3.0) * n * n * u ** 3 * max(t, 1e-6)) ** (3.0 / 7.0)

    cfg = SolverConfig(dx=dx, manning=n, alpha=alpha, dt_max=10.0, output_every_s=t_end)
    r = run(z, cfg, t_end, bc=bc)
    hs, ha = r.h_final[0], analytic(t_end)
    front_model = x[np.flatnonzero(hs > 0.01).max()] if (hs > 0.01).any() else 0.0
    front_true = u * t_end
    m = ha > 0.01
    return {"case": "Hunter 2005 / Bates 2010 wetting front", "u": u, "n": n, "dx": dx, "t_end": t_end,
            "rmse_m": float(np.sqrt(np.mean((hs[m] - ha[m]) ** 2))), "max_abs_err_m": float(np.abs(hs[m] - ha[m]).max()),
            "h_boundary_m": float(ha[0]), "front_model_m": float(front_model), "front_analytic_m": front_true,
            "front_error_m": float(front_model - front_true), "steps": r.steps, "mass": r.mass,
            "profile_x": x[::10].tolist(), "profile_model": hs[::10].tolist(), "profile_analytic": ha[::10].tolist()}


def lake_at_rest(n: int = 60, seed: int = 0) -> dict[str, Any]:
    """Well-balancedness: still water over rough terrain must stay still."""
    rng = np.random.default_rng(seed)
    z = rng.uniform(0, 5, (n, n))
    eta = 6.0
    h0 = np.maximum(eta - z, 0)
    r = run(z, SolverConfig(dx=10.0, manning=0.03, output_every_s=600), 600.0, h0=h0)
    return {"case": "lake at rest", "max_abs_dh_m": float(np.abs(r.h_final - h0).max()),
            "v_max_ms": float(r.v_max.max()), "mass": r.mass}


def closed_basin_rain(n: int = 50) -> dict[str, Any]:
    """Mass conservation: rain on a closed bowl; all volume must remain."""
    yy, xx = np.indices((n, n))
    z = 0.01 * ((xx - n / 2) ** 2 + (yy - n / 2) ** 2)
    rate = 50e-3 / 3600  # 50 mm/h
    r = run(z, SolverConfig(dx=20.0, manning=0.05, output_every_s=1800), 3600.0, rain_rate=lambda t: rate)
    expected = rate * 3600 * n * n * 400.0
    return {"case": "closed basin rain", "expected_m3": expected, "stored_m3": r.mass["v_final_m3"],
            "relative_error": abs(r.mass["v_final_m3"] - expected) / expected, "mass": r.mass}
