"""Flood-model calibration against Sentinel-1 flood extents with an event hold-out.

Protocol: ``configs/experiments/flood_calibration.yaml`` (committed before any run). The
selection rule is applied mechanically here; the held-out test event is scored only once,
with the selected setting.

Command line::

    python -m earthsim.calibration simulate --season 2018 --c 0.7 --n 0.08
    python -m earthsim.calibration select            # calibration + spatial check
    python -m earthsim.calibration test              # held-out event, baselines, bootstrap
"""
from __future__ import annotations

import argparse
import json
import time
from datetime import datetime
from pathlib import Path
from typing import Any

import numpy as np
import yaml

PRIOR = {"runoff_c": 0.7, "n_land": 0.08}


def _rel(p, root):
    """Path relative to the project root when inside it, else absolute."""
    p = Path(p)
    try:
        return p.relative_to(root)
    except ValueError:
        return p


def tag_for(season: str, c: float, n: float) -> str:
    return f"s{season}_c{int(round(c * 100)):03d}_n{int(round(n * 1000)):03d}"


def _settings():
    from .config import load_settings
    return load_settings()


def protocol(s) -> dict[str, Any]:
    return yaml.safe_load((s.root / "configs/experiments/flood_calibration.yaml").read_text())


def cal_dir(s) -> Path:
    d = s.p.processed_dir / "calibration"
    (d / "runs").mkdir(parents=True, exist_ok=True)
    return d


# ------------------------------------------------------------------ simulation
def coarse_terrain_cached(s, factor: int = 8) -> dict[str, Any]:
    from affine import Affine
    from .scenario import coarse_terrain
    p = cal_dir(s) / f"coarse_terrain_f{factor}.npz"
    if p.exists():
        d = np.load(p, allow_pickle=False)
        meta = json.loads(str(d["meta"]))
        return {"z": d["z"], "ocean": d["ocean"], "acc_km2": d["acc_km2"], "dx": meta["dx"],
                "crs": meta["crs"], "transform": Affine(*meta["transform"]), "meta": meta}
    ct = coarse_terrain(s.p.processed_dir / "terrain", factor)
    meta = {"dx": ct["dx"], "crs": ct["crs"], "transform": list(ct["transform"])[:6],
            **{k: ct[k] for k in ("raised_cells", "fill_depth_max_m", "fill_cells_gt_1m", "channel_cells",
                                   "diagonal_carved_cells")}}
    tmp = p.with_suffix(".tmp.npz")
    np.savez_compressed(tmp, z=ct["z"], ocean=ct["ocean"], acc_km2=ct["acc_km2"], meta=json.dumps(meta))
    tmp.rename(p)
    ct["meta"] = meta
    return ct


def _write_tif(path: Path, arr: np.ndarray, ct: dict[str, Any], tags: dict[str, Any]) -> None:
    import rasterio
    prof = {"driver": "GTiff", "height": arr.shape[0], "width": arr.shape[1], "count": 1, "dtype": "float32",
            "crs": ct["crs"], "transform": ct["transform"], "nodata": np.nan, "compress": "deflate", "tiled": True}
    with rasterio.open(path, "w", **prof) as ds:
        ds.write(np.where(ct["ocean"], np.nan, arr).astype("float32"), 1)
        ds.update_tags(**{k: str(v) for k, v in tags.items()})


def simulate_season(s, season: str, runoff_c: float, n_land: float, hourly_mm: np.ndarray | None = None,
                    start: str | None = None, capture_times: dict[str, str] | None = None,
                    out: Path | None = None, tag: str | None = None, n_channel: float | None = None,
                    progress=print) -> dict[str, Any]:
    """Run one rain-on-grid simulation and store depth at each capture time (IST ISO strings).
    With ``hourly_mm`` omitted, ERA5 for the protocol season is used (hindcast mode)."""
    from . import hydraulics as hy
    from .scenario import era5_uniform_hyetograph
    t0 = time.time()
    prot = protocol(s)
    n_channel = prot["parameters"]["n_channel"] if n_channel is None else n_channel
    forcing_meta: dict[str, Any] = {}
    if hourly_mm is None:
        se = prot["seasons"][season]
        start = se["start_ist"]
        capture_times = {e: prot["events"][e]["overpass_ist"] for e in se["events"]}
        last = max(capture_times.values())
        end_hour = last[:11] + f"{int(last[11:13]) + 1:02d}:00"
        rain_file = s.p.raw_dir / "era5_rain_kozhikode_2018_2019" / se["rain_file"]
        hourly_mm, _, forcing_meta = era5_uniform_hyetograph(rain_file, start, end_hour)
        forcing_meta["source"] = f"ERA5 via Open-Meteo archive ({se['rain_file']})"
    assert start is not None and capture_times
    tag = tag or tag_for(season, runoff_c, n_land)
    out = out or (cal_dir(s) / "runs" / tag)
    out.mkdir(parents=True, exist_ok=True)
    ct = coarse_terrain_cached(s)
    t_start = datetime.fromisoformat(start)
    caps = {e: (datetime.fromisoformat(v) - t_start).total_seconds() for e, v in capture_times.items()}
    t_end = max(caps.values())
    rate = runoff_c * np.asarray(hourly_mm, float) / 1000.0 / 3600.0

    def rain(t: float) -> float:
        return float(rate[min(int(t // 3600), rate.size - 1)])

    n = np.where(ct["acc_km2"] >= 25, n_channel, n_land)
    cfg = hy.SolverConfig(dx=ct["dx"], manning=n, alpha=0.7, dt_max=30.0, open_edges=True,
                          output_every_s=12 * 3600.0, wet_threshold_m=0.1)

    def prog(t, steps):
        progress(f"[{tag}] t={t / 3600:6.1f} h steps={steps} elapsed={time.time() - t0:5.0f}s")

    r = hy.run(ct["z"], cfg, t_end, rain_rate=rain, ocean=ct["ocean"], progress=prog,
               capture_at=tuple(caps.values()))
    tags = {"data_class": "simulation", "runoff_c": runoff_c, "n_land": n_land, "n_channel": n_channel,
            "start_ist": start, "forcing": forcing_meta.get("source", "supplied hourly series")}
    outputs = {}
    for e, ts in caps.items():
        p = out / f"depth_{e}.tif"
        _write_tif(p, r.captures[ts], ct, {**tags, "valid_time_ist": capture_times[e]})
        outputs[e] = str(_rel(p, s.root))
    p = out / "depth_max.tif"
    _write_tif(p, r.h_max, ct, tags)
    outputs["depth_max"] = str(_rel(p, s.root))
    summ = {"tag": tag, "season": season, "params": {"runoff_c": runoff_c, "n_land": n_land, "n_channel": n_channel,
                                                      "dx_m": ct["dx"], "start_ist": start, "t_end_s": t_end},
            "capture_times_ist": capture_times, "forcing": {**forcing_meta, "hours": int(np.size(hourly_mm)),
                                                           "total_rain_mm": float(np.sum(hourly_mm))},
            "steps": r.steps, "mass": r.mass, "diagnostics": r.diagnostics, "outputs": outputs,
            "wall_s": round(time.time() - t0, 1)}
    (out / "summary.json").write_text(json.dumps(summ, indent=2, default=float))
    return summ


# ------------------------------------------------------------------ scoring
def metrics(pred: np.ndarray, obs: np.ndarray, px_km2: float) -> dict[str, float]:
    tp = int((pred & obs).sum()); fp = int((pred & ~obs).sum()); fn = int((~pred & obs).sum())
    return {"tp": tp, "fp": fp, "fn": fn,
            "csi": tp / (tp + fp + fn) if tp + fp + fn else float("nan"),
            "pod_hit_rate": tp / (tp + fn) if tp + fn else float("nan"),
            "far_false_alarm_ratio": fp / (tp + fp) if tp + fp else float("nan"),
            "frequency_bias": (tp + fp) / (tp + fn) if tp + fn else float("nan"),
            "predicted_km2": (tp + fp) * px_km2, "observed_km2": (tp + fn) * px_km2,
            "false_alarm_km2": fp * px_km2, "missed_km2": fn * px_km2}


class Reference:
    """SAR reference for one event on the 30 m district grid (permanent water excluded)."""

    def __init__(self, s, event: str):
        import rasterio
        from .geo import aoi_mask
        from .pipelines import district_grid, load_aoi
        self.grid = district_grid(s)
        with rasterio.open(s.p.processed_dir / "flood" / f"s1_flood_observed_{event}.tif") as ds:
            obs = ds.read(1)
        aoi = aoi_mask(load_aoi(s), self.grid)
        self.ref = aoi & (obs >= 1) & (obs != 3)
        self.obs = obs[self.ref] == 2
        self.px_km2 = self.grid.res ** 2 / 1e6
        rows, cols = np.nonzero(self.ref)
        t = self.grid.transform
        x = t.c + (cols + 0.5) * t.a
        y = t.f + (rows + 0.5) * t.e
        self.block10 = ((np.floor(x / 10000).astype(int) + np.floor(y / 10000).astype(int)) % 2 == 0)
        bx, by = np.floor(x / 5000).astype(int), np.floor(y / 5000).astype(int)
        _, self.block5 = np.unique(bx * 100000 + by, return_inverse=True)

    def depth(self, tif: Path) -> np.ndarray:
        from .geo import reproject_to_grid
        return np.nan_to_num(reproject_to_grid(tif, self.grid, "continuous"))[self.ref]

    def score(self, pred: np.ndarray, subset: np.ndarray | None = None) -> dict[str, float]:
        if subset is None:
            return metrics(pred, self.obs, self.px_km2)
        return metrics(pred[subset], self.obs[subset], self.px_km2)

    def bootstrap(self, pred: np.ndarray, n: int = 1000, seed: int = 0) -> dict[str, Any]:
        nb = int(self.block5.max()) + 1
        tp = np.bincount(self.block5, pred & self.obs, nb); fp = np.bincount(self.block5, pred & ~self.obs, nb)
        fn = np.bincount(self.block5, ~pred & self.obs, nb)
        rng = np.random.default_rng(seed)
        out = {"csi": [], "pod_hit_rate": [], "far_false_alarm_ratio": []}
        for _ in range(n):
            i = rng.integers(0, nb, nb)
            a, b, c = tp[i].sum(), fp[i].sum(), fn[i].sum()
            out["csi"].append(a / (a + b + c) if a + b + c else np.nan)
            out["pod_hit_rate"].append(a / (a + c) if a + c else np.nan)
            out["far_false_alarm_ratio"].append(b / (a + b) if a + b else np.nan)
        return {"blocks_5km": nb, "resamples": n,
                **{k: [float(np.nanpercentile(v, 2.5)), float(np.nanpercentile(v, 97.5))] for k, v in out.items()}}


def _runs(s, season: str) -> list[dict[str, Any]]:
    res = []
    for p in sorted((cal_dir(s) / "runs").glob(f"s{season}_*/summary.json")):
        res.append(json.loads(p.read_text()))
    return res


def _pick(rows: list[dict[str, Any]], key: str) -> dict[str, Any]:
    best = max(r[key]["csi"] for r in rows)
    tied = [r for r in rows if r[key]["csi"] >= best - 0.001]
    tied.sort(key=lambda r: (abs(r["runoff_c"] - PRIOR["runoff_c"]) + abs(np.log2(r["n_land"] / PRIOR["n_land"])) * 0.1,
                             -r["threshold_m"]))
    return tied[0]


def select(s) -> dict[str, Any]:
    prot = protocol(s)
    cal_event = next(e for e, v in prot["events"].items() if v["role"] == "calibration")
    ref = Reference(s, cal_event)
    rows = []
    for summ in _runs(s, cal_event[:4]):
        d = ref.depth(s.root / summ["outputs"][cal_event])
        for th in prot["wet_depth_threshold_m"]:
            pred = d >= th
            rows.append({"tag": summ["tag"], "runoff_c": summ["params"]["runoff_c"], "n_land": summ["params"]["n_land"],
                         "threshold_m": th, "all": ref.score(pred), "blockA": ref.score(pred, ref.block10),
                         "blockB": ref.score(pred, ~ref.block10)})
    if len(rows) != len(prot["parameters"]["runoff_c"]) * len(prot["parameters"]["n_land"]) * len(prot["wet_depth_threshold_m"]):
        raise RuntimeError(f"incomplete calibration grid: {len(rows)} rows")
    chosen = _pick(rows, "all")
    a, b = _pick(rows, "blockA"), _pick(rows, "blockB")
    spatial = {"select_on_A": {k: a[k] for k in ("runoff_c", "n_land", "threshold_m")}, "score_on_B": a["blockB"],
               "select_on_B": {k: b[k] for k in ("runoff_c", "n_land", "threshold_m")}, "score_on_A": b["blockA"],
               "chosen_scored_on_A": chosen["blockA"], "chosen_scored_on_B": chosen["blockB"]}
    out = {"calibration_event": cal_event, "reference_pixels": int(ref.ref.sum()),
           "observed_flooded_km2": float(ref.obs.sum() * ref.px_km2), "grid_rows": rows,
           "chosen": {k: chosen[k] for k in ("tag", "runoff_c", "n_land", "threshold_m")},
           "chosen_calibration_scores": chosen["all"], "spatial_check": spatial}
    (cal_dir(s) / "calibration_selection.json").write_text(json.dumps(out, indent=2, default=float))
    return out


def test(s, run_missing: bool = True) -> dict[str, Any]:
    """Score the selected setting once on the held-out event (and the secondary validation event)."""
    from .geo import reproject_to_grid
    from .pipelines import catalog, crop_to_grid
    prot = protocol(s)
    sel = json.loads((cal_dir(s) / "calibration_selection.json").read_text())["chosen"]
    c, n, th = sel["runoff_c"], sel["n_land"], sel["threshold_m"]
    out: dict[str, Any] = {"selected": sel, "events": {}}
    for ev, info in prot["events"].items():
        if info["role"] == "calibration":
            continue
        season = ev[:4]
        tag = tag_for(season, c, n)
        summ_p = cal_dir(s) / "runs" / tag / "summary.json"
        if not summ_p.exists():
            if not run_missing:
                raise FileNotFoundError(summ_p)
            simulate_season(s, season, c, n)
        summ = json.loads(summ_p.read_text())
        dflt_tag = tag_for(season, PRIOR["runoff_c"], PRIOR["n_land"])
        dflt_p = cal_dir(s) / "runs" / dflt_tag / "summary.json"
        if not dflt_p.exists() and run_missing:
            simulate_season(s, season, PRIOR["runoff_c"], PRIOR["n_land"])
        dflt = json.loads(dflt_p.read_text())
        ref = Reference(s, ev)
        pred = ref.depth(s.root / summ["outputs"][ev]) >= th
        res = {"role": info["role"], "reference_pixels": int(ref.ref.sum()),
               "observed_flooded_km2": float(ref.obs.sum() * ref.px_km2),
               "calibrated": ref.score(pred), "calibrated_bootstrap95": ref.bootstrap(pred), "baselines": {}}
        dp = ref.depth(s.root / dflt["outputs"][ev]) >= 0.3
        res["baselines"]["uncalibrated_default_C0.7_n0.08_0.3m"] = ref.score(dp)
        hand = np.nan_to_num(crop_to_grid(s.p.processed_dir / "terrain" / "hand.tif", ref.grid), nan=1e9)[ref.ref]
        res["baselines"]["hand<=0.5m"] = ref.score(hand <= 0.5)
        for f in catalog(s).files("ksdma_flood_hazard"):
            if "_25_" in f.name:
                a = reproject_to_grid(f, ref.grid, "mask")[ref.ref]
                res["baselines"]["ksdma_25yr_footprint"] = ref.score(np.isfinite(a) & (a > 0))
        res["baselines"]["all_dry"] = ref.score(np.zeros_like(pred))
        res["mass_relative_residual"] = summ["mass"]["relative_residual"]
        out["events"][ev] = res
    (cal_dir(s) / "calibration_test.json").write_text(json.dumps(out, indent=2, default=float))
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    a = sub.add_parser("simulate"); a.add_argument("--season", required=True); a.add_argument("--c", type=float, required=True)
    a.add_argument("--n", type=float, required=True)
    sub.add_parser("select"); sub.add_parser("test"); sub.add_parser("terrain")
    args = ap.parse_args()
    s = _settings()
    if args.cmd == "simulate":
        r = simulate_season(s, args.season, args.c, args.n)
        print(json.dumps({"tag": r["tag"], "wall_s": r["wall_s"], "mass": r["mass"]["relative_residual"],
                          "h_max": r["diagnostics"]["h_max_m"]}))
    elif args.cmd == "terrain":
        print(coarse_terrain_cached(s)["meta"])
    elif args.cmd == "select":
        r = select(s)
        print(json.dumps({"chosen": r["chosen"], "scores": r["chosen_calibration_scores"]}, default=float))
    else:
        r = test(s)
        print(json.dumps({e: v["calibrated"] for e, v in r["events"].items()}, default=float))


if __name__ == "__main__":
    main()
