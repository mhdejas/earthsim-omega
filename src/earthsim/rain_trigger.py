"""Rainfall triggers (IMD 24 h categories) and their verification against ERA5 by forecast lead time.

IMD 24 h rainfall terminology (IMD Pune hazard atlas, https://imdpune.gov.in/hazardatlas/extreme.pdf):
heavy 64.5-115.5 mm, very heavy 115.6-204.4 mm, extremely heavy >= 204.5 mm. IMD applies these to
station rainfall in the 24 h ending 08:30 IST. Here they are applied to the mean of the two ERA5/IFS
0.25 deg cells over Kozhikode, which smooths point extremes, so a gridded "heavy" day is a stronger
signal than a single heavy station report. This is an experimental trigger, not an IMD warning.

Sources (Open-Meteo): forecast API (live, models=ecmwf_ifs025), ensemble API (ECMWF IFS 0.25 deg,
51 members), previous-runs API (archived forecasts by lead day, available from 2024), ERA5 archive
(verification truth).
"""
from __future__ import annotations

import json
import time
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

import numpy as np

CELLS = [(11.25, 76.0), (11.5, 75.75)]   # the two 0.25 deg cells the ERA5 hindcast used
IMD_CATEGORIES = [("heavy", 64.5), ("very_heavy", 115.6), ("extremely_heavy", 204.5)]
UA = "EARTHSIM-Omega/0.2 research prototype"


def get_json(url: str, params: dict[str, Any], cache: Path | None = None, retries: int = 4) -> Any:
    if cache is not None and cache.exists():
        return json.loads(cache.read_text())
    q = url + ("?" + urllib.parse.urlencode(params, safe=",") if params else "")
    err = None
    for k in range(retries):
        try:
            req = urllib.request.Request(q, headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=60) as r:
                d = json.loads(r.read().decode())
            if isinstance(d, dict) and d.get("error"):
                raise RuntimeError(d.get("reason"))
            if cache is not None:
                cache.parent.mkdir(parents=True, exist_ok=True)
                cache.write_text(json.dumps({"_url": q, "_fetched_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                                             "data": d}))
                return json.loads(cache.read_text())
            return {"_url": q, "data": d}
        except Exception as e:  # network/backoff
            err = e
            time.sleep(2 ** k * 3)
    raise RuntimeError(f"fetch failed after {retries} tries: {q}: {err}")


def _loc_params() -> dict[str, str]:
    return {"latitude": ",".join(str(a) for a, _ in CELLS), "longitude": ",".join(str(b) for _, b in CELLS),
            "timezone": "Asia/Kolkata"}


def cell_mean(resp: Any, var: str) -> tuple[list[str], np.ndarray]:
    d = resp["data"] if "data" in resp else resp
    d = d if isinstance(d, list) else [d]
    t = d[0]["hourly"]["time"]
    arr = np.array([[np.nan if v is None else v for v in c["hourly"][var]] for c in d], float)
    return t, np.nanmean(arr, 0) if np.isfinite(arr).any() else arr[0]


def daily_0830(times: list[str], mm: np.ndarray) -> dict[str, float]:
    """24 h totals for windows 09:00 (D-1) .. 08:00 (D) IST hourly bins, labelled D (IMD-like 08:30 day).
    Days with any missing hour are dropped."""
    from datetime import datetime, timedelta
    out: dict[str, list[float]] = {}
    for t, v in zip(times, mm):
        dt = datetime.fromisoformat(t)
        day = (dt + timedelta(hours=15)).date().isoformat()   # 09:00 -> next day label
        out.setdefault(day, []).append(v)
    return {k: float(np.sum(v)) for k, v in out.items() if len(v) == 24 and np.all(np.isfinite(v))}


def category(mm: float) -> str:
    c = "below_heavy"
    for name, th in IMD_CATEGORIES:
        if mm >= th:
            c = name
    return c


def contingency(fc: np.ndarray, ob: np.ndarray, th: float) -> dict[str, Any]:
    f, o = fc >= th, ob >= th
    a = int((f & o).sum()); b = int((f & ~o).sum()); c = int((~f & o).sum()); d = int((~f & ~o).sum())
    return {"threshold_mm": th, "days": int(f.size), "hits": a, "false_alarms": b, "misses": c, "correct_negatives": d,
            "pod_hit_rate": a / (a + c) if a + c else None, "far_false_alarm_ratio": b / (a + b) if a + b else None,
            "csi": a / (a + b + c) if a + b + c else None,
            "false_alarm_rate_pofd": b / (b + d) if b + d else None,
            "frequency_bias": (a + b) / (a + c) if a + c else None}


def verify(cache_dir: Path, years=(2024, 2025, 2026), leads=(1, 2, 3, 4, 5),
           model: str = "ecmwf_ifs025") -> dict[str, Any]:
    """Verify archived forecasts (by lead day) of 08:30-day district-mean rainfall against ERA5, JJAS."""
    fc_vars = ["precipitation"] + [f"precipitation_previous_day{k}" for k in leads]
    res: dict[str, Any] = {"model": model, "truth": "ERA5 (Open-Meteo archive, models=era5)", "cells": CELLS,
                           "season": "June-September", "years": list(years), "day_definition": "09:00-08:00 IST hourly bins",
                           "sources": {}, "by_lead": {}, "event_days": {}}
    obs_all: dict[str, float] = {}
    fc_all: dict[str, dict[str, float]] = {v: {} for v in fc_vars}
    for y in years:
        p = {**_loc_params(), "start_date": f"{y}-05-31", "end_date": f"{y}-09-30", "hourly": "precipitation"}
        era = get_json("https://archive-api.open-meteo.com/v1/archive", {**p, "models": "era5"},
                       cache_dir / f"era5_{y}_jjas.json")
        t, mm = cell_mean(era, "precipitation")
        obs_all.update(daily_0830(t, mm))
        pr = get_json("https://previous-runs-api.open-meteo.com/v1/forecast", {**p, "hourly": ",".join(fc_vars),
                                                                                "models": model},
                      cache_dir / f"previous_runs_{model}_{y}_jjas.json")
        res["sources"][str(y)] = {"era5": era["_url"], "forecast": pr["_url"]}
        for v in fc_vars:
            t, mm = cell_mean(pr, v)
            fc_all[v].update(daily_0830(t, mm))
    jjas = [d for d in sorted(obs_all) if d[5:7] in ("06", "07", "08", "09")]
    for v in fc_vars:
        lead = 0 if v == "precipitation" else int(v.rsplit("day", 1)[1])
        days = [d for d in jjas if d in fc_all[v]]
        fc = np.array([fc_all[v][d] for d in days]); ob = np.array([obs_all[d] for d in days])
        res["by_lead"][f"day{lead}"] = {
            "n_days": len(days), "bias_mm_per_day": float(np.mean(fc - ob)) if days else None,
            "mae_mm_per_day": float(np.mean(np.abs(fc - ob))) if days else None,
            "corr": float(np.corrcoef(fc, ob)[0, 1]) if len(days) > 2 else None,
            "thresholds": {name: contingency(fc, ob, th) for name, th in
                           [("moderate_35.5", 35.5)] + [(n, t) for n, t in IMD_CATEGORIES[:2]]}}
    res["event_days"] = {d: {"era5_mm": round(obs_all[d], 1),
                             **{f"fc_{k}": round(fc_all[k].get(d, np.nan), 1) for k in fc_vars}}
                         for d in jjas if obs_all[d] >= 64.5}
    res["daily"] = {d: {"era5": obs_all[d], **{k: fc_all[k].get(d) for k in fc_vars}} for d in jjas}
    res["era5_daily_jjas_quantiles_mm"] = {q: float(np.percentile([obs_all[d] for d in jjas], q)) for q in (50, 90, 95, 99)}
    return res


def tune_trigger_loyo(res: dict[str, Any], event_mm: float = 64.5, leads=(1, 2, 3),
                      candidates=tuple(range(25, 95, 5))) -> dict[str, Any]:
    """Leave-one-monsoon-out: choose the forecast trigger level T (warn if forecast >= T) on the other
    years by maximum CSI for ERA5 >= event_mm, score it on the held-out year, pool held-out counts."""
    days = sorted(res["daily"])
    years = sorted({d[:4] for d in days})
    out: dict[str, Any] = {"event_mm": event_mm, "rule": "warn if lead-L forecast >= T; T chosen by CSI on training years",
                           "candidates_mm": list(candidates), "by_lead": {}}
    for L in leads:
        key = f"precipitation_previous_day{L}"
        folds, pooled = [], {"hits": 0, "false_alarms": 0, "misses": 0, "correct_negatives": 0}
        for y in years:
            tr = [d for d in days if d[:4] != y and res["daily"][d][key] is not None]
            te = [d for d in days if d[:4] == y and res["daily"][d][key] is not None]
            ftr = np.array([res["daily"][d][key] for d in tr]); otr = np.array([res["daily"][d]["era5"] for d in tr])
            best = None
            for T in candidates:
                f, o = ftr >= T, otr >= event_mm
                a, b, c = (f & o).sum(), (f & ~o).sum(), (~f & o).sum()
                csi = a / (a + b + c) if a + b + c else 0
                if best is None or csi > best[1] + 1e-9:
                    best = (T, csi)
            fte = np.array([res["daily"][d][key] for d in te]); ote = np.array([res["daily"][d]["era5"] for d in te])
            f, o = fte >= best[0], ote >= event_mm
            cnt = {"hits": int((f & o).sum()), "false_alarms": int((f & ~o).sum()), "misses": int((~f & o).sum()),
                   "correct_negatives": int((~f & ~o).sum())}
            for k in pooled:
                pooled[k] += cnt[k]
            folds.append({"held_out_year": y, "chosen_T_mm": best[0], "train_csi": float(best[1]), "test": cnt})
        alltr = [d for d in days if res["daily"][d][key] is not None]
        fa = np.array([res["daily"][d][key] for d in alltr]); oa = np.array([res["daily"][d]["era5"] for d in alltr]) >= event_mm
        dep = max(candidates, key=lambda T: (((fa >= T) & oa).sum() / max(((fa >= T) | oa).sum(), 1), -T))
        a, b, c, d = pooled["hits"], pooled["false_alarms"], pooled["misses"], pooled["correct_negatives"]
        out["by_lead"][f"day{L}"] = {"folds": folds, "deploy_T_mm_all_years": int(dep), "pooled_held_out": {**pooled,
            "pod_hit_rate": a / (a + c) if a + c else None, "far_false_alarm_ratio": b / (a + b) if a + b else None,
            "csi": a / (a + b + c) if a + b + c else None, "false_alarm_rate_pofd": b / (b + d) if b + d else None}}
    return out


# ---------------------------------------------------------------- live inputs
def fetch_live(run_dir: Path, past_days: int = 5, forecast_days: int = 7, model: str = "ecmwf_ifs025") -> dict[str, Any]:
    """Fetch the latest deterministic forecast (with recent past) and the 51-member ensemble."""
    p = {**_loc_params(), "hourly": "precipitation", "past_days": past_days, "forecast_days": forecast_days,
         "models": model}
    det = get_json("https://api.open-meteo.com/v1/forecast", p, run_dir / "inputs" / "forecast_deterministic.json")
    ens = get_json("https://ensemble-api.open-meteo.com/v1/ensemble", p, run_dir / "inputs" / "forecast_ensemble.json")
    t, det_mm = cell_mean(det, "precipitation")
    d = ens["data"] if isinstance(ens["data"], list) else [ens["data"]]
    keys = [k for k in d[0]["hourly"] if k.startswith("precipitation")]
    tt = d[0]["hourly"]["time"]
    members = []
    for k in keys:
        arr = np.array([[np.nan if v is None else v for v in c["hourly"][k]] for c in d], float)
        members.append(np.nanmean(arr, 0))
    members = np.array(members)
    if tt != t:
        raise RuntimeError("deterministic and ensemble time axes differ")
    meta = {}
    for name, u in (("deterministic", "https://api.open-meteo.com/data/ecmwf_ifs025/static/meta.json"),
                    ("ensemble", "https://ensemble-api.open-meteo.com/data/ecmwf_ifs025_ensemble/static/meta.json")):
        m = get_json(u, {}, run_dir / "inputs" / f"model_meta_{name}.json")["data"]
        meta[name] = {k: time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(m[k])) for k in
                      ("last_run_initialisation_time", "last_run_availability_time") if k in m}
        meta[name]["temporal_resolution_s"] = m.get("temporal_resolution_seconds")
    return {"model_runs": meta, "times": t, "deterministic_mm": det_mm, "members_mm": members, "member_names": keys,
            "sources": {"deterministic": det["_url"], "ensemble": ens["_url"]},
            "fetched_utc": det.get("_fetched_utc")}


def main() -> None:
    import argparse
    from .config import load_settings
    ap = argparse.ArgumentParser(); ap.add_argument("cmd", choices=["verify"])
    a = ap.parse_args()
    s = load_settings()
    out = s.p.processed_dir / "warning"
    out.mkdir(parents=True, exist_ok=True)
    r = verify(s.p.raw_dir / "forecast_archive")
    r["trigger_tuning_loyo"] = tune_trigger_loyo(r)
    (out / "rain_trigger_verification.json").write_text(json.dumps(r, indent=2))
    for k, v in r["trigger_tuning_loyo"]["by_lead"].items():
        print("LOYO", k, [(f["held_out_year"], f["chosen_T_mm"]) for f in v["folds"]],
              {x: (round(y, 3) if isinstance(y, float) else y) for x, y in v["pooled_held_out"].items()})
    for k, v in r["by_lead"].items():
        h = v["thresholds"]["heavy"]
        print(k, v["n_days"], "bias", round(v["bias_mm_per_day"], 1), "corr", round(v["corr"], 2), "heavy:",
              {x: h[x] for x in ("hits", "false_alarms", "misses", "pod_hit_rate", "far_false_alarm_ratio", "csi")})
    print("event days", len(r["event_days"]), r["era5_daily_jjas_quantiles_mm"])


if __name__ == "__main__":
    main()
