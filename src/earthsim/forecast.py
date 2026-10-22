"""Experimental flood decision-support cycle for Kozhikode (NOT an official warning).

One cycle (``earthsim forecast run``):
 1. fetch live ECMWF IFS 0.25 deg rainfall (deterministic with the last 5 days, plus the 51-member ensemble)
    from Open-Meteo, and store the raw responses with their URLs and SHA-256;
 2. rain outlook per 08:30-IST day: deterministic total, ensemble p10/p50/p90 and the probability of
    IMD heavy / very heavy / extremely heavy rain (district mean of the two 0.25 deg cells);
 3. rain-on-grid flood runs with the calibrated parameters for the deterministic forecast and the
    ensemble members nearest the p10/p50/p90 of the 72 h total (antecedent: the last 5 days);
 4. flood footprint and exposure (OSM buildings, schools/hospitals, major roads) at +24/+48/+72 h;
 5. experimental alert level, bulletin, map, machine-readable alert.json and a run manifest.

Official flood and rain warnings for Kozhikode come from IMD (rain), CWC (river forecasts) and
KSDMA / the District Disaster Management Authority (action). This output is experimental decision
support for research; it must not be used in place of those warnings.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import subprocess
import time
from concurrent.futures import ProcessPoolExecutor
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import numpy as np

DISCLAIMER = ("EXPERIMENTAL research output - not an official warning. Follow IMD, CWC and KSDMA / "
              "Kozhikode DDMA advisories for decisions.")
LEVELS = ["GREEN", "YELLOW", "ORANGE", "RED"]
HYDRO_HORIZON_H = 72


def _sha(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def _git(root: Path) -> str:
    try:
        return subprocess.run(["git", "-C", str(root), "rev-parse", "--short", "HEAD"], capture_output=True,
                              text=True, timeout=10).stdout.strip() or "unknown"
    except Exception:
        return "unknown"


# ------------------------------------------------------------------ rain outlook
def rain_outlook(live: dict[str, Any], now: datetime, trigger_mm: float) -> dict[str, Any]:
    from .rain_trigger import IMD_CATEGORIES, category, daily_0830
    det = daily_0830(live["times"], live["deterministic_mm"])
    mem = [daily_0830(live["times"], m) for m in live["members_mm"]]
    days = []
    for d in sorted(det):
        win_start = datetime.fromisoformat(d) - timedelta(hours=15)
        if win_start + timedelta(hours=24) <= now:
            continue                                   # fully in the past
        vals = np.array([m[d] for m in mem if d in m])
        if vals.size < 10:
            continue
        lead = (datetime.fromisoformat(d) - datetime.fromisoformat(now.date().isoformat())).days
        days.append({"day_ending_0830_ist": d, "lead_day": lead, "in_progress": win_start <= now,
                     "deterministic_mm": round(det[d], 1), "deterministic_imd_category": category(det[d]),
                     "ensemble_members": int(vals.size),
                     "ensemble_p10_p50_p90_mm": [round(float(np.percentile(vals, q)), 1) for q in (10, 50, 90)],
                     **{f"prob_{n}": round(float((vals >= th).mean()), 3) for n, th in IMD_CATEGORIES},
                     "deterministic_ge_trigger": bool(det[d] >= trigger_mm)})
    past = [v for d, v in det.items() if datetime.fromisoformat(d) <= now]
    return {"days": days, "trigger_mm": trigger_mm, "antecedent_5day_mm": round(float(sum(past[-5:])), 1)}


def rain_level(day: dict[str, Any]) -> tuple[str, str]:
    """Experimental colour from rain alone. Probability cut-offs are NOT verified (no archived ensemble)."""
    if day["prob_very_heavy"] >= 0.5 or day["prob_extremely_heavy"] >= 0.2:
        return "RED", "P(very heavy) >= 50% or P(extremely heavy) >= 20%"
    if day["prob_heavy"] >= 0.5 or day["prob_very_heavy"] >= 0.2:
        return "ORANGE", "P(heavy) >= 50% or P(very heavy) >= 20%"
    if day["prob_heavy"] >= 0.2 or day["deterministic_ge_trigger"]:
        return "YELLOW", "P(heavy) >= 20% or deterministic >= verified trigger"
    return "GREEN", "below all rain triggers"


# ------------------------------------------------------------------ exposure
class Exposure:
    def __init__(self, s, ct: dict[str, Any]):
        from pyproj import Transformer
        from .geo import Grid, aoi_mask
        from .pipelines import load_aoi
        self.ct = ct
        ny, nx = ct["z"].shape
        self.grid = Grid(ct["crs"], ct["transform"], nx, ny)
        self.aoi = aoi_mask(load_aoi(s), self.grid) & ~ct["ocean"]
        tr = Transformer.from_crs("EPSG:4326", ct["crs"], always_xy=True)
        osm = s.p.raw_dir / "osm_kozhikode"
        cache = s.p.processed_dir / "calibration" / "exposure_index.npz"
        inv = ~ct["transform"]

        def rc(lon, lat):
            x, y = tr.transform(np.asarray(lon), np.asarray(lat))
            c, r = inv * (x, y)
            r, c = np.floor(r).astype(int), np.floor(c).astype(int)
            ok = (r >= 0) & (r < ny) & (c >= 0) & (c < nx)
            return r, c, ok

        if cache.exists():
            z = np.load(cache, allow_pickle=True)
            self.bld = z["bld"]; self.road_km = z["road_km"]; self.fac = list(z["fac"])
        else:
            b = json.loads((osm / "osm_buildings.json").read_text())["elements"]
            lon = [e["center"]["lon"] if "center" in e else e.get("lon") for e in b]
            lat = [e["center"]["lat"] if "center" in e else e.get("lat") for e in b]
            keep = [i for i, v in enumerate(lon) if v is not None]
            r, c, ok = rc([lon[i] for i in keep], [lat[i] for i in keep])
            self.bld = np.zeros((ny, nx), np.int32)
            np.add.at(self.bld, (r[ok], c[ok]), 1)
            roads = json.loads((osm / "osm_roads.json").read_text())["elements"]
            self.road_km = np.zeros((ny, nx), np.float32)
            for w in roads:
                g = w.get("geometry") or []
                if len(g) < 2:
                    continue
                x, y = tr.transform([p["lon"] for p in g], [p["lat"] for p in g])
                x, y = np.asarray(x), np.asarray(y)
                seg = np.hypot(np.diff(x), np.diff(y)) / 1000.0
                cc, rr = inv * ((x[:-1] + x[1:]) / 2, (y[:-1] + y[1:]) / 2)
                rr, cc = np.floor(rr).astype(int), np.floor(cc).astype(int)
                ok2 = (rr >= 0) & (rr < ny) & (cc >= 0) & (cc < nx)
                np.add.at(self.road_km, (rr[ok2], cc[ok2]), seg[ok2])
            f = json.loads((osm / "osm_facilities.json").read_text())["elements"]
            pts = [(e.get("lon") or e.get("center", {}).get("lon"), e.get("lat") or e.get("center", {}).get("lat"),
                    e.get("tags", {}).get("amenity", "?"), e.get("tags", {}).get("name", "")) for e in f]
            pts = [p for p in pts if p[0] is not None]
            r, c, ok = rc([p[0] for p in pts], [p[1] for p in pts])
            self.fac = [(int(r[i]), int(c[i]), pts[i][2], pts[i][3], pts[i][0], pts[i][1]) for i in range(len(pts)) if ok[i]]
            np.savez_compressed(cache, bld=self.bld, road_km=self.road_km, fac=np.array(self.fac, dtype=object))

    def assess(self, depth: np.ndarray, th: float) -> dict[str, Any]:
        wet = (np.nan_to_num(depth) >= th) & self.aoi
        cell_km2 = self.grid.res ** 2 / 1e6
        fac = [f for f in self.fac if wet[int(f[0]), int(f[1])]]
        by = {}
        for f in fac:
            by[f[2]] = by.get(f[2], 0) + 1
        return {"wet_km2": round(float(wet.sum() * cell_km2), 1), "buildings": int(self.bld[wet].sum()),
                "major_road_km": round(float(self.road_km[wet].sum()), 1), "facilities": by,
                "facility_examples": [{"type": f[2], "name": f[3], "lon": round(float(f[4]), 5), "lat": round(float(f[5]), 5)}
                                      for f in fac if f[2] in ("hospital", "clinic")][:10]}


# ------------------------------------------------------------------ hydraulic members
def _member_job(args: tuple) -> dict[str, Any]:
    import numba
    numba.set_num_threads(int(os.environ.get("FORECAST_MEMBER_THREADS", "2")))
    from .calibration import simulate_season
    from .config import load_settings
    name, mm, start, caps, c, n, out = args
    s = load_settings()
    log = logging.getLogger(f"member.{name}")
    summ = simulate_season(s, "forecast", c, n, hourly_mm=np.asarray(mm), start=start, capture_times=caps,
                           out=Path(out), tag=name, progress=lambda m: None)
    log.info("member %s done in %.0fs", name, summ["wall_s"])
    return summ


def run_cycle(s, hydraulics: bool = True, workers: int = 4, now: datetime | None = None, spinup_h: int = 120,
              horizon_h: int = HYDRO_HORIZON_H, params: dict[str, float] | None = None,
              runs_root: Path | None = None) -> dict[str, Any]:
    from .calibration import cal_dir, coarse_terrain_cached
    from .rain_trigger import fetch_live
    t0 = time.time()
    now = now or datetime.now().replace(second=0, microsecond=0)   # sandbox clock is IST
    run_id = now.strftime("%Y%m%dT%H%M") + "_IST"
    root = (runs_root or s.p.processed_dir.parent / "forecast" / "runs") / run_id
    (root / "logs").mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger("earthsim.forecast")
    logger.setLevel(logging.INFO)
    fh = logging.FileHandler(root / "logs" / "run.log")
    fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
    logger.addHandler(fh)
    manifest: dict[str, Any] = {"run_id": run_id, "issued_local_ist": now.isoformat(), "status": "RUNNING",
                                "code_version": _git(s.root), "disclaimer": DISCLAIMER, "stages": {}}
    try:
        ts = time.time()
        live = fetch_live(root)
        manifest["inputs"] = {"model_runs": live["model_runs"], "sources": live["sources"], "fetched_utc": live["fetched_utc"],
                              "files": {p.name: _sha(p) for p in sorted((root / "inputs").glob("*.json"))},
                              "members": len(live["member_names"])}
        manifest["stages"]["fetch_s"] = round(time.time() - ts, 1)
        logger.info("fetched %d ensemble members, %d hours", len(live["member_names"]), len(live["times"]))
        ver = json.loads((s.p.processed_dir / "warning" / "rain_trigger_verification.json").read_text())
        trig = ver["trigger_tuning_loyo"]["by_lead"]["day1"]
        trigger_mm = float(trig["deploy_T_mm_all_years"])
        outlook = rain_outlook(live, now, trigger_mm)
        for d in outlook["days"]:
            d["rain_level"], d["rain_level_reason"] = rain_level(d)
        manifest["stages"]["rain_s"] = round(time.time() - ts, 1)
        flood: dict[str, Any] = {"run": False}
        if hydraulics:
            ts = time.time()
            sel = params or json.loads((cal_dir(s) / "calibration_selection.json").read_text())["chosen"]
            c, n, th = sel["runoff_c"], sel["n_land"], sel["threshold_m"]
            times = live["times"]
            i_now = max(i for i, t in enumerate(times) if datetime.fromisoformat(t) <= now)
            i_end = min(i_now + horizon_h, len(times))
            i0 = max(0, i_now - spinup_h)
            start = times[i0]
            leads = [h for h in (24, 48, 72) if h <= horizon_h] or [horizon_h]
            caps = {f"+{h}h": (datetime.fromisoformat(times[i_now]) + timedelta(hours=h)).isoformat(timespec="minutes")
                    for h in leads}
            tot = live["members_mm"][:, i_now:i_end].sum(1)
            picks = {"deterministic": live["deterministic_mm"]}
            for q in (10, 50, 90):
                k = int(np.argmin(np.abs(tot - np.percentile(tot, q))))
                picks[f"ens_p{q}_{live['member_names'][k]}"] = np.concatenate(
                    [live["deterministic_mm"][:i_now], live["members_mm"][k, i_now:]])
            jobs = [(name, np.nan_to_num(mm[i0:i_end]).tolist(), start, caps, c, n, str(root / "members" / name))
                    for name, mm in picks.items()]
            with ProcessPoolExecutor(max_workers=workers) as ex:
                summs = list(ex.map(_member_job, jobs))
            ct = coarse_terrain_cached(s)
            ex_ = Exposure(s, ct)
            import rasterio
            members = {}
            for (name, mm, *_), sm in zip(jobs, summs):
                rec = {"forecast_72h_mm": round(float(np.sum(mm[i_now - i0:])), 1), "mass_relative_residual": sm["mass"]["relative_residual"],
                       "wall_s": sm["wall_s"], "by_lead": {}}
                peak = None
                for lab in caps:
                    with rasterio.open(s.root / sm["outputs"][lab]) as ds:
                        d = ds.read(1)
                    peak = d if peak is None else np.fmax(peak, d)
                    rec["by_lead"][lab] = ex_.assess(d, th)
                rec["peak_of_snapshots"] = ex_.assess(peak, th)
                members[name] = rec
            from .calibration import tag_for
            ref = {}
            for ev, season in (("2018-08-14", "2018"), ("2019-08-10", "2019")):
                p = cal_dir(s) / "runs" / tag_for(season, c, n) / f"depth_{ev}.tif"
                if p.exists():
                    with rasterio.open(p) as ds:
                        r_ = ex_.assess(ds.read(1), th)
                    ref[ev] = {"wet_km2": r_["wet_km2"], "buildings": r_["buildings"]}
            flood = {"run": True, "params": {"runoff_c": c, "n_land": n, "wet_threshold_m": th, "dx_m": ct["dx"],
                                             "spinup_h": i_now - i0, "horizon_h": i_end - i_now},
                     "capture_times_ist": caps, "spinup_start_ist": start, "members": members,
                     "reference_hindcast_wet_km2": ref}
            manifest["stages"]["hydraulics_s"] = round(time.time() - ts, 1)
        level = "GREEN"
        for d in outlook["days"][:3]:
            if LEVELS.index(d["rain_level"]) > LEVELS.index(level):
                level = d["rain_level"]
        alert = {"run_id": run_id, "issued_ist": now.isoformat(), "product": "EARTHSIM-Omega experimental flood decision support",
                 "official": False, "disclaimer": DISCLAIMER, "overall_level_next_72h": level,
                 "level_basis": "rain triggers (IMD categories, ensemble probability, verified 50 mm trigger); flood model gives footprint and exposure",
                 "rain_outlook": outlook, "flood_scenarios": flood,
                 "verified_skill": {"trigger_mm": trigger_mm, "lead_day1_held_out": trig["pooled_held_out"],
                                    "note": "leave-one-monsoon-out, JJAS 2024-2026, ERA5 >= 64.5 mm district-mean days"}}
        (root / "alert.json").write_text(json.dumps(alert, indent=2, default=float))
        (root / "bulletin.md").write_text(bulletin(alert))
        try:
            from .viz import forecast_figure
            forecast_figure(alert, root, s)
        except Exception as e:   # figure is not critical
            logger.warning("figure failed: %s", e)
        manifest["status"] = "OK"
        manifest["overall_level_next_72h"] = level
    except Exception as e:
        manifest["status"] = "FAILED"
        manifest["error"] = f"{type(e).__name__}: {e}"
        logger.exception("cycle failed")
    manifest["wall_s"] = round(time.time() - t0, 1)
    manifest["outputs"] = {p.name: _sha(p) for p in sorted(root.glob("*")) if p.is_file()}
    (root / "manifest.json").write_text(json.dumps(manifest, indent=2, default=float))
    latest = root.parent / "latest"
    if manifest["status"] == "OK" and runs_root is None:
        if latest.is_symlink() or latest.exists():
            latest.unlink()
        latest.symlink_to(root.name)
    logger.removeHandler(fh)
    return manifest


def bulletin(a: dict[str, Any]) -> str:
    L = [f"# Kozhikode flood outlook - EXPERIMENTAL", "", f"**{a['disclaimer']}**", "",
         f"Issued {a['issued_ist']} IST - run `{a['run_id']}`", "",
         f"## Overall experimental level, next 72 h: {a['overall_level_next_72h']}", "",
         "| Day (24 h to 08:30 IST) | Forecast mm (ensemble p10-p50-p90) | P(heavy >=64.5) | P(very heavy >=115.6) | Level |",
         "|---|---|---|---|---|"]
    for d in a["rain_outlook"]["days"]:
        p = d["ensemble_p10_p50_p90_mm"]
        L.append(f"| {d['day_ending_0830_ist']}{' (in progress)' if d['in_progress'] else ''} | {d['deterministic_mm']} "
                 f"({p[0]}-{p[1]}-{p[2]}) | {d['prob_heavy']:.0%} | {d['prob_very_heavy']:.0%} | {d['rain_level']} |")
    L += ["", f"Rain in the last 5 days (model estimate): {a['rain_outlook']['antecedent_5day_mm']} mm.", ""]
    f = a["flood_scenarios"]
    if f.get("run"):
        L += ["## Flood footprint scenarios (rain-on-grid model, calibrated on 14 Aug 2018 radar)", "",
              "| Scenario | 72 h rain mm | Wet area km2 (peak) | Buildings in wet cells | Major roads km | Schools / hospitals |",
              "|---|---|---|---|---|---|"]
        for k, m in f["members"].items():
            pk = m["peak_of_snapshots"]
            L.append(f"| {k} | {m['forecast_72h_mm']} | {pk['wet_km2']} | {pk['buildings']:,} | {pk['major_road_km']} | "
                     f"{pk['facilities'].get('school', 0)} / {pk['facilities'].get('hospital', 0)} |")
        L += ["", "Wet = simulated depth at or above the calibrated threshold, 240 m cells. Building counts are OSM "
              "footprints whose centre falls in a wet cell, not damage estimates."]
    v = a["verified_skill"]["lead_day1_held_out"]
    L += ["", "## How much to trust this", "",
          f"Day-1 rain trigger, out-of-sample over three monsoons: hit rate {v['pod_hit_rate']:.2f}, false-alarm "
          f"ratio {v['far_false_alarm_ratio']:.2f}, CSI {v['csi']:.2f}. Ensemble probability cut-offs are not verified. "
          "Flood model: on the 14 Aug 2018 calibration event it predicted about 9x the radar-observed flooded "
          "area (radar misses water under trees and between buildings, so the truth lies in between); held-out "
          "CSI 0.019 on 10 Aug 2019 is not better than the official KSDMA hazard map. Treat footprints as "
          "'where water collects first', not as a depth forecast. Details: docs/CALIBRATION.md.",
          "", "The rain trigger was verified on June-September (south-west monsoon) days only; outside that season "
          "(for example the October-December north-east monsoon) its skill is unknown."]
    return "\n".join(L) + "\n"


def main() -> None:
    import argparse
    from .config import load_settings
    ap = argparse.ArgumentParser(); ap.add_argument("cmd", choices=["run"])
    ap.add_argument("--no-hydraulics", action="store_true"); ap.add_argument("--workers", type=int, default=4)
    a = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    m = run_cycle(load_settings(), hydraulics=not a.no_hydraulics, workers=a.workers)
    print(json.dumps({k: m.get(k) for k in ("run_id", "status", "overall_level_next_72h", "wall_s", "stages", "error")}))


if __name__ == "__main__":
    main()
