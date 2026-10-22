"""Reproducible benchmarks: solver correctness cases + performance scaling.

Run with ``earthsim run benchmarks``. Results go to benchmarks/results/benchmarks.json and a
figure benchmarks/results/benchmarks.png. Timings are wall-clock on the machine that runs them
(recorded with the hardware report), after a JIT warm-up; each timing is the median of repeats.
Run on an otherwise idle machine - concurrent jobs distort the numbers.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

import numpy as np


def _median_time(fn, repeats: int = 3) -> float:
    ts = []
    for _ in range(repeats):
        t = time.perf_counter()
        fn()
        ts.append(time.perf_counter() - t)
    return float(np.median(ts))


def correctness() -> dict[str, Any]:
    from . import hydraulics as hy

    out = {"lake_at_rest": hy.lake_at_rest(), "closed_basin_rain": hy.closed_basin_rain(), "wetting_front": []}
    for u in (1.0, 0.5, 0.25):
        r = hy.hunter_wetting_front(u=u)
        r["froude_at_boundary"] = u / np.sqrt(9.81 * r["h_boundary_m"])
        r["front_error_pct"] = 100 * r["front_error_m"] / r["front_analytic_m"]
        out["wetting_front"].append(r)
    out["wetting_front_grid_convergence"] = []
    for dx in (20.0, 10.0, 5.0):
        r = hy.hunter_wetting_front(dx=dx)
        out["wetting_front_grid_convergence"].append({"dx": dx, "rmse_m": r["rmse_m"], "front_error_m":
                                                      r["front_error_m"]})
    return out


def solver_scaling(n: int = 1000, steps_t: float = 600.0) -> list[dict[str, Any]]:
    import numba

    from . import hydraulics as hy

    rng = np.random.default_rng(0)
    z = rng.uniform(0, 1, (n, n)).cumsum(1) * 0.01
    cfg = hy.SolverConfig(dx=120.0, manning=0.06, dt_max=10.0, output_every_s=1e12)
    hy.run(z[:64, :64], cfg, 50.0, rain_rate=lambda t: 1e-5)  # JIT warm-up
    maxt = numba.config.NUMBA_NUM_THREADS
    rows = []
    for th in sorted({1, 2, 4, maxt}):
        if th > maxt:
            continue
        numba.set_num_threads(th)
        res = {}

        def go():
            res["r"] = hy.run(z, cfg, steps_t, rain_rate=lambda t: 2e-5)

        el = _median_time(go, repeats=3)
        steps = res["r"].steps
        rows.append({"threads": th, "cells": int(z.size), "steps": steps, "wall_s": el,
                     "ms_per_step": 1000 * el / steps, "mcell_steps_per_s": steps * z.size / el / 1e6,
                     "mass_rel_residual": res["r"].mass["relative_residual"]})
    numba.set_num_threads(maxt)
    base = rows[0]["wall_s"]
    for r in rows:
        r["speedup_vs_1"] = base / r["wall_s"]
    return rows


def hydro_core_timing(sizes=(1000, 2000, 4000)) -> list[dict[str, Any]]:
    from .hydro_core import accumulation, d8_flowdir, priority_flood_epsilon

    rows = []
    rng = np.random.default_rng(1)
    d = rng.normal(0, 1, (64, 64)).cumsum(0).cumsum(1)
    oc = np.zeros(d.shape, bool)
    oc[:, 0] = True
    z, _, _ = priority_flood_epsilon(d, oc)
    accumulation(z, d8_flowdir(z, oc, 30.0))  # warm-up
    for n in sizes:
        dem = rng.normal(0, 1, (n, n)).cumsum(0).cumsum(1) * 0.01 + np.linspace(0, 500, n)[None, :]
        ocean = np.zeros(dem.shape, bool)
        ocean[:, 0] = True
        t = time.perf_counter()
        zf, _, raised = priority_flood_epsilon(dem, ocean)
        t1 = time.perf_counter()
        fd = d8_flowdir(zf, ocean, 30.0)
        t2 = time.perf_counter()
        accumulation(zf, fd)
        t3 = time.perf_counter()
        rows.append({"cells": n * n, "priority_flood_s": t1 - t, "d8_s": t2 - t1, "accumulation_s": t3 - t2,
                     "raised_cells": raised, "mcells_per_s_total": n * n / (t3 - t) / 1e6})
    return rows


def run_all(out_dir: Path) -> dict[str, Any]:
    from .hardware import detect
    from .provenance import atomic_write_text

    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    t = time.time()
    hw = detect(check_internet=False).to_dict()
    res = {"hardware": {k: hw.get(k) for k in ("cpu_model", "cpu", "simd", "logical_cores", "physical_cores", "ram_total_bytes", "gpu", "os", "python", "libraries")},
           "correctness": correctness(), "solver_scaling": solver_scaling(), "hydro_core": hydro_core_timing()}
    for w in res["correctness"]["wetting_front"]:
        for k in ("profile_x", "profile_model", "profile_analytic"):
            w.setdefault("_" + k, w.pop(k, None))
    res["wall_s"] = time.time() - t
    atomic_write_text(out_dir / "benchmarks.json", json.dumps(res, indent=2, default=float))
    _figure(res, out_dir / "benchmarks.png")
    return {"json": str(out_dir / "benchmarks.json"), "figure": str(out_dir / "benchmarks.png"),
            "solver_scaling": res["solver_scaling"], "wall_s": res["wall_s"]}


def _figure(res: dict[str, Any], out: Path) -> None:
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    fig, ax = plt.subplots(1, 3, figsize=(17, 5), dpi=110)
    for w, c in zip(res["correctness"]["wetting_front"], ("C0", "C1", "C2")):
        x = np.asarray(w["_profile_x"]) / 1000
        ax[0].plot(x, w["_profile_analytic"], color=c, lw=2, alpha=0.5)
        ax[0].plot(x, w["_profile_model"], color=c, ls="--",
                   label=f"u={w['u']} m/s, Fr={w['froude_at_boundary']:.2f}: front {w['front_error_pct']:+.1f}%")
    ax[0].set(xlabel="x (km)", ylabel="depth (m)", title="Wetting front (Hunter 2005): solid analytic, dashed model")
    ax[0].legend(fontsize=8)
    sc = res["solver_scaling"]
    ax[1].plot([r["threads"] for r in sc], [r["mcell_steps_per_s"] for r in sc], "o-")
    for r in sc:
        ax[1].annotate(f"{r['speedup_vs_1']:.1f}x", (r["threads"], r["mcell_steps_per_s"]), textcoords="offset points",
                       xytext=(4, -12), fontsize=8)
    ax[1].set(xlabel="numba threads", ylabel="million cell-steps / s", title=f"Solver throughput, {sc[0]['cells']:,} cells")
    hc = res["hydro_core"]
    ax[2].bar([f"{r['cells'] / 1e6:.0f}M" for r in hc], [r["priority_flood_s"] for r in hc], label="Priority-Flood+eps")
    ax[2].bar([f"{r['cells'] / 1e6:.0f}M" for r in hc], [r["d8_s"] + r["accumulation_s"] for r in hc],
              bottom=[r["priority_flood_s"] for r in hc], label="D8 + accumulation")
    ax[2].set(xlabel="grid cells", ylabel="seconds", title="Terrain-routing core (single thread)")
    ax[2].legend(fontsize=8)
    fig.suptitle("EARTHSIM-Omega benchmarks (measured on this machine; see benchmarks.json for hardware)", fontsize=10)
    fig.tight_layout()
    fig.savefig(out)
    plt.close(fig)
