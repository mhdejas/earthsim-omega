"""Testing the flood solver against real CWC river gauges (protocol: configs/experiments/gauge_validation.yaml).

Command line::

    python -m earthsim.gauges extract              # event-window observation extracts from the NWDP CSVs
    python -m earthsim.gauges snap                 # place gauges on the 240 m grid (D4 upstream area)
    python -m earthsim.gauges simulate --season 2018 --f 1.25 --n 0.08
    python -m earthsim.gauges select               # 2018 only: choose (factor, n_land), then the map threshold
    python -m earthsim.gauges test                 # 2019 held-out: gauges + SAR map, scored once
"""
from __future__ import annotations

import argparse
import json
import math
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import numba as nb
import numpy as np
import pandas as pd
import yaml

from .calibration import PRIOR, Reference, _write_tif, coarse_terrain_cached
from .calibration import protocol as sar_protocol

SAR_SELECTED = {"factor": 0.9, "n_land": 0.16, "threshold_m": 2.0}
Q_COL = "Manual Daily River Water Discharge (m3/sec)"
WL_COL = "River Water Level Manual Hourly (meter)"


def _settings():
    from .config import load_settings
    return load_settings()


def protocol(s) -> dict[str, Any]:
    return yaml.safe_load((s.root / "configs/experiments/gauge_validation.yaml").read_text())


def res_dir(s) -> Path:
    d = s.root / "experiments" / "results" / "gauges"
    d.mkdir(parents=True, exist_ok=True)
    return d


def run_dir(s) -> Path:
    d = s.p.processed_dir / "gauges" / "runs"
    d.mkdir(parents=True, exist_ok=True)
    return d


def tag_for(season: str, f: float, n: float) -> str:
    return f"g{season}_f{int(round(f * 100)):03d}_n{int(round(n * 1000)):03d}"


# ------------------------------------------------------------------ observations
def extract(s) -> dict[str, Any]:
    """Cut the event windows (1-20 Aug 2018 and 2019) for the protocol gauges out of the raw NWDP files."""
    prot = protocol(s)
    raw = s.p.raw_dir / "cwc"
    names = list(prot["gauges"])
    q = pd.read_csv(raw / "kl_discharge_daily_2001_2025.csv", usecols=["Station", "Data Acquisition Time", Q_COL])
    q = q[q.Station.isin(names)]
    q["time_ist"] = pd.to_datetime(q["Data Acquisition Time"], format="%d-%m-%Y %H:%M")
    q = q.rename(columns={"Station": "station", Q_COL: "q_m3s"})[["station", "time_ist", "q_m3s"]]
    parts = []
    for ch in pd.read_csv(raw / "rwl_manual_hr_cwc_031_1991_2020.csv", chunksize=500_000,
                          usecols=["Station", "Data Acquisition Time", WL_COL]):
        parts.append(ch[ch.Station.isin(names)])
    w = pd.concat(parts)
    w["time_ist"] = pd.to_datetime(w["Data Acquisition Time"], format="%d-%m-%Y %H:%M")
    w = w.rename(columns={"Station": "station", WL_COL: "level_m"})[["station", "time_ist", "level_m"]]
    out = {}
    for season, (a, b) in prot["window"].items():
        for kind, df in (("discharge", q), ("stage", w)):
            sub = df[(df.time_ist >= a) & (df.time_ist < b)].sort_values(["station", "time_ist"])
            p = res_dir(s) / f"obs_{kind}_{season}.csv"
            sub.to_csv(p, index=False)
            out[f"{kind}_{season}"] = {st: int(n) for st, n in sub.groupby("station").size().items()}
    (res_dir(s) / "obs_counts.json").write_text(json.dumps(out, indent=2))
    return out


def obs(s, kind: str, season: str, station: str) -> pd.Series:
    df = pd.read_csv(res_dir(s) / f"obs_{kind}_{season}.csv", parse_dates=["time_ist"])
    x = df[df.station == station].set_index("time_ist").iloc[:, -1].astype(float)
    if kind == "discharge":
        x = x[x > 0]  # QC rule from the protocol: monsoon zeros at perennial rivers are missing-value codes
    return x


# ------------------------------------------------------------------ gauge placement
@nb.njit(cache=True)
def _d4(z, ocean):
    ny, nx = z.shape
    rec = -np.ones(ny * nx, np.int64)
    for i in range(ny):
        for j in range(nx):
            if ocean[i, j]:
                continue
            best, k = z[i, j], -1
            for di, dj in ((-1, 0), (1, 0), (0, -1), (0, 1)):
                a, b = i + di, j + dj
                if 0 <= a < ny and 0 <= b < nx and z[a, b] < best:
                    best, k = z[a, b], a * nx + b
            rec[i * nx + j] = k
    return rec


def _upstream_area(rec, z):
    order = np.argsort(-z.ravel(), kind="stable")
    acc = np.ones(rec.size)
    for c in order:
        r = rec[c]
        if r >= 0:
            acc[r] += acc[c]
    return acc


def _catchment(rec, cell, n):
    donors: dict[int, list[int]] = {}
    for c in range(n):
        r = int(rec[c])
        if r >= 0:
            donors.setdefault(r, []).append(c)
    m = np.zeros(n, bool)
    stack = [cell]
    while stack:
        c = stack.pop()
        if m[c]:
            continue
        m[c] = True
        stack.extend(donors.get(c, []))
    return m


def snap(s) -> dict[str, Any]:
    from pyproj import Transformer
    prot = protocol(s)
    ct = coarse_terrain_cached(s)
    z, ocean = ct["z"], ct["ocean"]
    ny, nx = z.shape
    rec = _d4(z, ocean)
    acc_km2 = _upstream_area(rec, z) * (ct["dx"] ** 2) / 1e6
    tr = Transformer.from_crs(4326, ct["crs"], always_xy=True)
    inv = ~ct["transform"]
    out = {}
    for name, g in prot["gauges"].items():
        x, y = tr.transform(g["lon"], g["lat"])
        c0, r0 = inv * (x, y)
        r0, c0 = int(r0), int(c0)
        rad = int(math.ceil((3000 if g["area_km2"] else 1000) / ct["dx"]))
        best = None
        for r in range(max(r0 - rad, 0), min(r0 + rad + 1, ny)):
            for c in range(max(c0 - rad, 0), min(c0 + rad + 1, nx)):
                if ocean[r, c] or math.hypot(r - r0, c - c0) * ct["dx"] > rad * ct["dx"]:
                    continue
                a = acc_km2[r * nx + c]
                key = abs(a - g["area_km2"]) if g["area_km2"] else -a
                if best is None or key < best[0]:
                    best = (key, r, c, a)
        _, r, c, a = best
        err = (a - g["area_km2"]) / g["area_km2"] if g["area_km2"] else None
        out[name] = {"row": int(r), "col": int(c), "model_area_km2": round(float(a), 1), "official_area_km2": g["area_km2"],
                     "area_error": None if err is None else round(float(err), 3),
                     "offset_m": round(math.hypot(r - r0, c - c0) * ct["dx"], 0),
                     "accepted": True if err is None else bool(abs(err) <= 0.20)}
    (res_dir(s) / "gauge_snapping.json").write_text(json.dumps(out, indent=2))
    return out


def _faces(s, sn: dict[str, Any]) -> dict[str, dict[str, np.ndarray]]:
    """Boundary faces of each gauge's D4 catchment, with outward sign, split into outlet and leakage."""
    ct = coarse_terrain_cached(s)
    z = ct["z"]
    ny, nx = z.shape
    rec = _d4(z, ct["ocean"])
    res = {}
    for name, g in sn.items():
        U = _catchment(rec, g["row"] * nx + g["col"], ny * nx).reshape(ny, nx)
        fx = []  # (i, j, sign) on qx: face between (i, j) and (i, j+1)
        a, b = U[:, :-1], U[:, 1:]
        for i, j in zip(*np.nonzero(a & ~b)):
            fx.append((i, j, 1.0))
        for i, j in zip(*np.nonzero(~a & b)):
            fx.append((i, j, -1.0))
        fy = []
        a, b = U[:-1, :], U[1:, :]
        for i, j in zip(*np.nonzero(a & ~b)):
            fy.append((i, j, 1.0))
        for i, j in zip(*np.nonzero(~a & b)):
            fy.append((i, j, -1.0))
        d = {}
        for key, f in (("x", fx), ("y", fy)):
            f = np.array(f, float).reshape(-1, 3)
            near = (np.abs(f[:, 0] - g["row"]) <= 2) & (np.abs(f[:, 1] - g["col"]) <= 2)
            for part, m in (("out", near), ("leak", ~near)):
                d[f"{key}_{part}_i"] = f[m, 0].astype(int)
                d[f"{key}_{part}_j"] = f[m, 1].astype(int)
                d[f"{key}_{part}_s"] = f[m, 2]
        d["area_km2"] = float(U.sum() * ct["dx"] ** 2 / 1e6)
        d["touches_edge"] = bool(U[0].any() or U[-1].any() or U[:, 0].any() or U[:, -1].any())
        res[name] = d
    return res


# ------------------------------------------------------------------ simulation
def simulate(s, season: str, factor: float, n_land: float, progress=print) -> dict[str, Any]:
    from . import hydraulics as hy
    from .scenario import era5_uniform_hyetograph
    t0 = time.time()
    prot, sp = protocol(s), sar_protocol(s)
    start, end = prot["window"][season]
    se = sp["seasons"][season]
    rain_file = s.p.raw_dir / "era5_rain_kozhikode_2018_2019" / se["rain_file"]
    hourly_mm, _, fmeta = era5_uniform_hyetograph(rain_file, start, end)
    tag = tag_for(season, factor, n_land)
    out = run_dir(s) / tag
    out.mkdir(parents=True, exist_ok=True)
    ct = coarse_terrain_cached(s)
    sn = json.loads((res_dir(s) / "gauge_snapping.json").read_text())
    faces = _faces(s, sn)
    t_start = datetime.fromisoformat(start)
    t_end = (datetime.fromisoformat(end) - t_start).total_seconds()
    caps = {e: (datetime.fromisoformat(sp["events"][e]["overpass_ist"]) - t_start).total_seconds() for e in se["events"]}
    rate = factor * np.asarray(hourly_mm, float) / 1000.0 / 3600.0
    nh = int(round(t_end / 3600))
    dx = ct["dx"]
    vol = {g: {"out": np.zeros(nh), "leak": np.zeros(nh)} for g in sn}
    depth = {g: np.full(nh, np.nan) for g in sn}

    def observer(t, dt, h, qx, qy):
        k = min(int((t - 0.5 * dt) // 3600), nh - 1)
        for g, f in faces.items():
            for part in ("out", "leak"):
                v = (qx[f[f"x_{part}_i"], f[f"x_{part}_j"]] * f[f"x_{part}_s"]).sum() + \
                    (qy[f[f"y_{part}_i"], f[f"y_{part}_j"]] * f[f"y_{part}_s"]).sum()
                vol[g][part][k] += v * dx * dt
            if abs(t / 3600 - round(t / 3600)) < 1e-6:
                kk = int(round(t / 3600)) - 1
                if 0 <= kk < nh:
                    depth[g][kk] = h[sn[g]["row"], sn[g]["col"]]

    def rain(t):
        return float(rate[min(int(t // 3600), rate.size - 1)])

    def prog(t, steps):
        if int(round(t / 3600)) % 24 == 0:
            progress(f"[{tag}] t={t / 3600:6.1f} h steps={steps} elapsed={time.time() - t0:5.0f}s")

    n = np.where(ct["acc_km2"] >= 25, sp["parameters"]["n_channel"], n_land)
    cfg = hy.SolverConfig(dx=dx, manning=n, alpha=0.7, dt_max=30.0, open_edges=True, output_every_s=3600.0,
                          wet_threshold_m=0.1)
    r = hy.run(ct["z"], cfg, t_end, rain_rate=rain, ocean=ct["ocean"], progress=prog,
               capture_at=tuple(caps.values()), observer=observer)
    times = [t_start + timedelta(hours=k) for k in range(nh)]
    ser = pd.DataFrame({"hour_start_ist": times, "rain_mm": hourly_mm[:nh]})
    for g in sn:
        ser[f"{g}_q_m3s"] = vol[g]["out"] / 3600.0
        ser[f"{g}_leak_m3s"] = vol[g]["leak"] / 3600.0
        ser[f"{g}_depth_end_m"] = depth[g]
    ser.to_csv(out / "gauge_series.csv", index=False)
    tags = {"data_class": "simulation", "effective_rain_factor": factor, "n_land": n_land, "start_ist": start}
    outputs = {}
    for e, ts in caps.items():
        p = out / f"depth_{e}.tif"
        _write_tif(p, r.captures[ts], ct, {**tags, "valid_time_ist": sp["events"][e]["overpass_ist"]})
        outputs[e] = str(p.relative_to(s.root))
    summ = {"tag": tag, "season": season, "factor": factor, "n_land": n_land, "window": [start, end],
            "forcing_total_mm": float(np.sum(hourly_mm)), "steps": r.steps, "mass": r.mass,
            "diagnostics": r.diagnostics, "outputs": outputs, "series": str((out / "gauge_series.csv").relative_to(s.root)),
            "catchments": {g: {"area_km2": f["area_km2"], "touches_edge": f["touches_edge"]} for g, f in faces.items()},
            "wall_s": round(time.time() - t0, 1)}
    (out / "summary.json").write_text(json.dumps(summ, indent=2, default=float))
    return summ


# ------------------------------------------------------------------ scoring
def kge(sim: np.ndarray, o: np.ndarray) -> dict[str, float]:
    r = float(np.corrcoef(sim, o)[0, 1]) if np.std(sim) > 0 and np.std(o) > 0 else float("nan")
    a = float(np.std(sim) / np.std(o))
    b = float(np.mean(sim) / np.mean(o))
    return {"kge": 1 - math.sqrt((r - 1) ** 2 + (a - 1) ** 2 + (b - 1) ** 2), "r": r, "alpha": a, "beta": b,
            "nse": float(1 - np.sum((sim - o) ** 2) / np.sum((o - o.mean()) ** 2)),
            "pbias_pct": float(100 * (sim.sum() - o.sum()) / o.sum())}


def score_run(s, tag: str) -> dict[str, Any]:
    summ = json.loads((run_dir(s) / tag / "summary.json").read_text())
    ser = pd.read_csv(s.root / summ["series"], parse_dates=["hour_start_ist"]).set_index("hour_start_ist")
    season = summ["season"]
    out: dict[str, Any] = {}
    for g in json.loads((res_dir(s) / "gauge_snapping.json").read_text()):
        res: dict[str, Any] = {}
        o = obs(s, "discharge", season, g)
        if len(o):
            sim = ser[f"{g}_q_m3s"].reindex(o.index.floor("h")).to_numpy()
            ok = np.isfinite(sim)
            oo, ss = o.to_numpy()[ok], sim[ok]
            m = kge(ss, oo)
            ip, io = int(np.argmax(ss)), int(np.argmax(oo))
            m.update({"n_obs": int(ok.sum()), "obs_peak_m3s": float(oo[io]), "obs_peak_time": str(o.index[ok][io]),
                      "sim_peak_at_obs_times_m3s": float(ss[ip]), "peak_ratio": float(ss.max() / oo.max()),
                      "sim_peak_hourly_m3s": float(ser[f"{g}_q_m3s"].max()),
                      "sim_peak_hourly_time": str(ser[f"{g}_q_m3s"].idxmax()),
                      "peak_timing_error_h": float((ser[f"{g}_q_m3s"].idxmax() - o.idxmax()).total_seconds() / 3600),
                      "leak_share": float(ser[f"{g}_leak_m3s"].abs().sum() /
                                                                     max(ser[f"{g}_q_m3s"].abs().sum(), 1e-9))})
            res["discharge"] = m
        w = obs(s, "stage", season, g)
        if len(w):
            wh = w.groupby(w.index.floor("h")).mean()
            d = ser[f"{g}_depth_end_m"].copy()
            d.index = d.index + pd.Timedelta(hours=1)  # depth is stored at the end of each hour
            j = pd.concat([wh, d], axis=1, join="inner").dropna()
            res["stage"] = {"n": int(len(j)), "pearson_r": float(np.corrcoef(j.iloc[:, 0], j.iloc[:, 1])[0, 1]),
                            "obs_peak_time": str(wh.idxmax()), "sim_peak_time": str(d.idxmax()),
                            "peak_timing_error_h": float((d.idxmax() - wh.idxmax()).total_seconds() / 3600),
                            "obs_rise_m": float(wh.max() - wh.min()), "sim_depth_range_m": float(d.max() - d.min())}
        out[g] = res
    return {"tag": tag, "season": season, "factor": summ["factor"], "n_land": summ["n_land"],
            "mass_relative_residual": summ["mass"]["relative_residual"], "gauges": out}


def _sar_rows(s, ref: Reference, tif: Path, thresholds) -> list[dict[str, Any]]:
    d = ref.depth(tif)
    return [{"threshold_m": th, **ref.score(d >= th)} for th in thresholds]


def select(s) -> dict[str, Any]:
    prot, sp = protocol(s), sar_protocol(s)
    grid = [(f, n) for f in prot["parameters"]["effective_rain_factor"] for n in prot["parameters"]["n_land"]]
    rows = []
    for f, n in grid:
        sc = score_run(s, tag_for("2018", f, n))
        ks = [sc["gauges"][g]["discharge"]["kge"] for g in ("KUNIYIL", "KUTTYADI")]
        rows.append({**sc, "mean_kge": float(np.mean(ks))})
    best = max(r["mean_kge"] for r in rows)
    tied = [r for r in rows if r["mean_kge"] >= best - 0.01]
    tied.sort(key=lambda r: (abs(r["factor"] - 1.0), -r["n_land"]))
    ch = tied[0]
    cal_event = next(e for e, v in sp["events"].items() if v["role"] == "calibration")
    ref = Reference(s, cal_event)
    summ = json.loads((run_dir(s) / ch["tag"] / "summary.json").read_text())
    sar = _sar_rows(s, ref, s.root / summ["outputs"][cal_event], sp["wet_depth_threshold_m"])
    bt = max(r["csi"] for r in sar)
    th = sorted([r for r in sar if r["csi"] >= bt - 0.001], key=lambda r: -r["threshold_m"])[0]
    prior = score_run(s, tag_for("2018", PRIOR["runoff_c"], PRIOR["n_land"]))
    out = {"grid": rows, "chosen": {"tag": ch["tag"], "factor": ch["factor"], "n_land": ch["n_land"],
                                    "threshold_m": th["threshold_m"], "mean_kge_2018": ch["mean_kge"]},
           "chosen_sar_2018_08_14": th, "sar_threshold_rows": sar, "prior_2018": prior}
    (res_dir(s) / "gauge_selection.json").write_text(json.dumps(out, indent=2, default=float))
    return out


def test(s) -> dict[str, Any]:
    from .calibration import tag_for as sar_tag
    sp = sar_protocol(s)
    sel = json.loads((res_dir(s) / "gauge_selection.json").read_text())["chosen"]
    settings = {"gauge_selected": (sel["factor"], sel["n_land"], sel["threshold_m"]),
                "sar_selected": (SAR_SELECTED["factor"], SAR_SELECTED["n_land"], SAR_SELECTED["threshold_m"]),
                "prior": (PRIOR["runoff_c"], PRIOR["n_land"], 0.3)}
    ev = "2019-08-10"
    ref = Reference(s, ev)
    out: dict[str, Any] = {"selected": sel, "event": ev, "settings": {}}
    for name, (f, n, th) in settings.items():
        tag = tag_for("2019", f, n)
        if not (run_dir(s) / tag / "summary.json").exists():
            simulate(s, "2019", f, n)
        summ = json.loads((run_dir(s) / tag / "summary.json").read_text())
        pred = ref.depth(s.root / summ["outputs"][ev]) >= th
        out["settings"][name] = {"factor": f, "n_land": n, "threshold_m": th, "gauges": score_run(s, tag)["gauges"],
                                 "sar": ref.score(pred), "sar_bootstrap95": ref.bootstrap(pred)}
    out["reference_existing"] = {"solver_flood_calibration_v1_csi": 0.019, "ksdma_25yr_csi": 0.023,
                                 "existing_run": sar_tag("2019", SAR_SELECTED["factor"], SAR_SELECTED["n_land"])}
    (res_dir(s) / "gauge_test_2019.json").write_text(json.dumps(out, indent=2, default=float))
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("extract"); sub.add_parser("snap"); sub.add_parser("select"); sub.add_parser("test")
    a = sub.add_parser("simulate"); a.add_argument("--season", required=True)
    a.add_argument("--f", type=float, required=True); a.add_argument("--n", type=float, required=True)
    b = sub.add_parser("score"); b.add_argument("--tag", required=True)
    args = ap.parse_args()
    s = _settings()
    if args.cmd == "extract":
        print(json.dumps(extract(s), indent=1))
    elif args.cmd == "snap":
        print(json.dumps(snap(s), indent=1))
    elif args.cmd == "simulate":
        r = simulate(s, args.season, args.f, args.n)
        print(json.dumps({"tag": r["tag"], "wall_s": r["wall_s"], "mass": r["mass"]["relative_residual"]}))
    elif args.cmd == "score":
        print(json.dumps(score_run(s, args.tag), indent=1, default=float))
    elif args.cmd == "select":
        print(json.dumps(select(s)["chosen"], default=float))
    else:
        r = test(s)
        print(json.dumps({k: {"sar_csi": v["sar"]["csi"]} for k, v in r["settings"].items()}, default=float))


if __name__ == "__main__":
    main()
