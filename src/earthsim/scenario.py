"""Regional rain-on-grid hindcast with the local-inertial solver, evaluated against SAR.

Scenario ``aug2019_hindcast``: 2019-08-01 00:00 IST -> 2019-08-10 06:10 IST
(the Sentinel-1B overpass, orbit 165, 00:40 UTC). Forcing: ERA5 hourly
precipitation (Open-Meteo archive, models=era5, times in Asia/Kolkata), mean
of the distinct ERA5 cells returned for the five requested points (Open-Meteo
snaps to the 0.25 deg grid, so the 5 points collapse to 2 cells). Spatially
uniform over the domain - a strong simplification over the Western Ghats,
where ERA5 is known to under-represent orographic extremes.

Losses: net rain = C * rain (runoff coefficient C; saturated-monsoon
assumption). No evaporation, no sewer/drain network, no river bathymetry, no
tides, no dam operations (Kakkayam/Peruvannamuzhi on the Kuttiyadi are not
modelled). Terrain: Copernicus GLO-30 DSM aggregated to ``dx`` with channel
preservation (stream cells take the block's lowest stream elevation, others
the block mean) and Priority-Flood+epsilon conditioned with 4-connectivity (matching the
solver's face fluxes) to remove DSM artefact pits (canopy,
bridges). Manning n: channel cells (upstream area >= 25 km2) n_channel, all
other land n_land.

This is a *hindcast experiment* to test whether the solver + open data
reproduce the observed pattern; it is not a calibrated operational model.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import numpy as np


def era5_uniform_hyetograph(path: Path, start: str, end: str) -> tuple[np.ndarray, list[str], dict[str, Any]]:
    """Hourly mm series (IST) averaged over distinct ERA5 cells; returns (mm per hour, times, meta)."""
    d = json.loads(Path(path).read_text())
    d = d if isinstance(d, list) else [d]
    cells: dict[tuple[float, float], dict] = {}
    for x in d:
        cells.setdefault((x["latitude"], x["longitude"]), x)
    times = d[0]["hourly"]["time"]
    sel = [i for i, t in enumerate(times) if start <= t < end]
    arr = np.array([[c["hourly"]["precipitation"][i] or 0.0 for i in sel] for c in cells.values()], float)
    meta = {"distinct_era5_cells": [list(k) for k in cells], "requested_points": len(d),
            "timezone": d[0].get("timezone"), "per_cell_total_mm": arr.sum(1).round(1).tolist()}
    return arr.mean(0), [times[i] for i in sel], meta


def coarse_terrain(terrain_dir: Path, factor: int) -> dict[str, Any]:
    from .geo import read_raster
    from .hydro_core import accumulation, carve_diagonal_steps, d8_flowdir, priority_flood_epsilon

    dem, grid, _ = read_raster(terrain_dir / "dem.tif")
    ocean, _, _ = read_raster(terrain_dir / "ocean_mask.tif")
    ny, nx = (dem.shape[0] // factor) * factor, (dem.shape[1] // factor) * factor
    d = dem[:ny, :nx].reshape(ny // factor, factor, nx // factor, factor)
    o = ocean[:ny, :nx].reshape(ny // factor, factor, nx // factor, factor)
    st, _, _ = read_raster(terrain_dir / "streams.tif")
    s = (np.nan_to_num(st[:ny, :nx]) > 0).reshape(ny // factor, factor, nx // factor, factor)
    oc = np.nanmean(o, axis=(1, 3)) >= 0.5
    with np.errstate(all="ignore"):
        zmean = np.nanmean(d, axis=(1, 3))
        # channel-preserving aggregation: a coarse cell containing a mapped 30 m stream takes the lowest
        # stream-cell elevation in its block (the thalweg); other cells take the block mean. Block-mean alone
        # turns narrow Ghats gorges into artificial dams (v1 run: 21k cells filled > 1 m, 30-46 m pileups).
        zstream = np.nanmin(np.where(s, d, np.nan), axis=(1, 3))
    has_stream = s.any(axis=(1, 3))
    zc = np.where(has_stream & np.isfinite(zstream), zstream, zmean)
    zc = np.where(oc, 0.0, np.where(np.isfinite(zc), zc, 0.0))
    zc_raw = zc.copy()
    # routing (for channel Manning n) uses the 8-connected surface; the solver gets a 4-connected one
    z8, _, _ = priority_flood_epsilon(zc, oc, connectivity=8)
    fd8 = d8_flowdir(z8, oc, grid.res * factor)
    acc = accumulation(z8, fd8)
    zcarved, carved = carve_diagonal_steps(np.where(oc, zc, z8), fd8)
    zf, _, raised = priority_flood_epsilon(zcarved, oc, connectivity=4)
    zc_raw = np.minimum(zc_raw, zcarved)  # fill depth is measured against the carved surface
    from affine import Affine

    t = grid.transform
    cgrid_t = Affine(t.a * factor, t.b, t.c, t.d, t.e * factor, t.f)
    return {"z": zf, "z_raw": zc_raw, "ocean": oc, "channel_cells": int(has_stream.sum()), "diagonal_carved_cells": carved, "acc_km2": acc * (grid.res * factor) ** 2 / 1e6,
            "transform": cgrid_t, "crs": grid.crs, "dx": grid.res * factor, "raised_cells": raised,
            "fill_depth_max_m": float((zf - zc_raw).max()), "fill_cells_gt_1m": int(((zf - zc_raw) > 1).sum())}


def run_hindcast(terrain_dir: Path, rain_path: Path, out_dir: Path, factor: int = 8, runoff_c: float = 0.7,
                 n_land: float = 0.08, n_channel: float = 0.04, start: str = "2019-08-01T00:00",
                 overpass: str = "2019-08-10T06:10", tag: str = "base", progress=print) -> dict[str, Any]:
    import time

    import rasterio

    from . import hydraulics as hy

    t0 = time.time()
    ct = coarse_terrain(terrain_dir, factor)
    end_hour = overpass[:11] + f"{int(overpass[11:13]) + 1:02d}:00"
    mm, times, meta = era5_uniform_hyetograph(rain_path, start, end_hour)
    from datetime import datetime

    t_end = (datetime.fromisoformat(overpass) - datetime.fromisoformat(start)).total_seconds()
    rate = runoff_c * mm / 1000.0 / 3600.0  # m/s per hour bin

    def rain(t):
        return float(rate[min(int(t // 3600), rate.size - 1)])

    n = np.where(ct["acc_km2"] >= 25, n_channel, n_land)
    cfg = hy.SolverConfig(dx=ct["dx"], manning=n, alpha=0.7, dt_max=30.0, open_edges=True,
                          output_every_s=6 * 3600.0, wet_threshold_m=0.1)
    active = np.ones(ct["z"].shape, bool)

    def prog(t, steps):
        progress(f"[hydro {tag}] t={t / 3600:6.1f} h  steps={steps}  elapsed={time.time() - t0:5.0f}s")

    r = hy.run(ct["z"], cfg, t_end, rain_rate=rain, ocean=ct["ocean"], active=active, progress=prog)
    out_dir.mkdir(parents=True, exist_ok=True)
    prof = {"driver": "GTiff", "height": ct["z"].shape[0], "width": ct["z"].shape[1], "count": 1,
            "dtype": "float32", "crs": ct["crs"], "transform": ct["transform"], "nodata": np.nan,
            "compress": "deflate", "tiled": True}
    paths = {}
    for name, arr in (("depth_at_overpass", r.h_final), ("depth_max", r.h_max), ("speed_max", r.v_max),
                      ("arrival_time_h", r.t_arrival / 3600.0), ("terrain_conditioned", ct["z"])):
        p = out_dir / f"{tag}_{name}.tif"
        a = np.where(ct["ocean"], np.nan, arr).astype("float32") if name != "terrain_conditioned" else arr
        with rasterio.open(p, "w", **prof) as ds:
            ds.write(np.asarray(a, "float32"), 1)
            ds.update_tags(scenario="aug2019_hindcast", tag=tag, data_class="simulation", runoff_c=runoff_c,
                           n_land=n_land, n_channel=n_channel, start_ist=start, end_ist=overpass,
                           forcing="ERA5 hourly precipitation via Open-Meteo, spatially uniform")
        paths[name] = str(p)
    summary = {"tag": tag, "params": {"dx_m": ct["dx"], "factor": factor, "runoff_c": runoff_c, "n_land": n_land,
                                      "n_channel": n_channel, "alpha": cfg.alpha, "start_ist": start,
                                      "end_ist": overpass, "t_end_s": t_end},
               "forcing": {**meta, "hours": int(mm.size), "total_rain_mm": float(mm.sum()),
                           "max_hourly_mm": float(mm.max()), "net_total_mm": float(mm.sum() * runoff_c)},
               "terrain": {k: ct[k] for k in ("raised_cells", "fill_depth_max_m", "fill_cells_gt_1m", "channel_cells", "diagonal_carved_cells")},
               "aggregation": "channel-preserving (stream: block-min of stream cells; else block-mean)",
               "grid": {"crs": ct["crs"], "transform": list(ct["transform"])[:6], "shape": list(ct["z"].shape)},
               "steps": r.steps, "mass": r.mass, "diagnostics": r.diagnostics, "outputs": paths,
               "wall_s": round(time.time() - t0, 1),
               "cell_steps_per_s": round(r.steps * ct["z"].size / max(time.time() - t0, 1e-9))}
    (out_dir / f"{tag}_summary.json").write_text(json.dumps(summary, indent=2, default=float))
    return summary


def evaluate_hindcast(summary: dict[str, Any], obs_path: Path, district_grid, aoi, extra: dict[str, np.ndarray],
                      thresholds=(0.1, 0.3)) -> dict[str, Any]:
    """Score simulated depth at the overpass time against the SAR-observed extent on the district grid.
    Reference pixels: valid SAR inside the district, excluding permanent water. ``extra`` = other binary
    predictors (HAND threshold, KSDMA footprint) scored on exactly the same pixels for comparison."""
    import rasterio

    from .floodsus import binary_metrics
    from .geo import reproject_to_grid

    with rasterio.open(obs_path) as ds:
        obs = ds.read(1)
    ref = aoi & (obs >= 1) & (obs != 3)
    log: list = []
    depth = reproject_to_grid(summary["outputs"]["depth_at_overpass"], district_grid, "continuous", log=log)
    hmax = reproject_to_grid(summary["outputs"]["depth_max"], district_grid, "continuous", log=log)
    out: dict[str, Any] = {"reference_pixels": int(ref.sum()), "observed_flooded_px": int((ref & (obs == 2)).sum()),
                           "resampling_log": log, "model": {}, "comparators": {}}
    o = obs[ref] == 2
    for th in thresholds:
        out["model"][f"depth_at_overpass>={th}m"] = binary_metrics(np.nan_to_num(depth[ref]) >= th, o)
        out["model"][f"depth_max>={th}m"] = binary_metrics(np.nan_to_num(hmax[ref]) >= th, o)
    for k, v in extra.items():
        out["comparators"][k] = binary_metrics(v[ref], o)
    out["simulated_wet_km2_district_at_overpass_0.3m"] = float(((np.nan_to_num(depth) >= 0.3) & aoi).sum()
                                                             * district_grid.res ** 2 / 1e6)
    return out
