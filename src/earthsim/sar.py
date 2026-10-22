"""Sentinel-1 SAR flood-extent observation (observation-derived reference).

Data: Microsoft Planetary Computer ``sentinel-1-rtc`` (Sentinel-1 GRD,
radiometrically terrain corrected gamma0, linear power, 10 m; CC BY 4.0,
contains modified Copernicus Sentinel data).

Method (change detection, after Twele et al. 2016 / Martinis et al. 2015, simplified):
1. Read VV gamma0 (linear) onto the analysis grid with AVERAGE resampling
   (averaging in linear power, never in dB), then 3x3 boxcar (speckle).
2. Dry-season reference = per-pixel median of same-relative-orbit scenes.
3. Water threshold T = Otsu on the post-event VV dB histogram within the
   evaluation region, clamped to [-23, -15] dB.
4. Flooded = post_dB < T  AND  (post_dB - ref_dB) <= -3 dB  AND ref_dB >= T.
   Permanent/seasonal open water = ref_dB < T.
Deliberately NOT applied: HAND / slope exclusion masks - they would make a
validation of terrain-based flood models circular.

Physical limits (stated in every output): C-band SAR does not see water under
dense canopy (coconut/areca), urban flooding often *raises* backscatter (double
bounce), and the image is a snapshot at overpass time (about 00:40 UTC, 06:10 IST),
usually after or before the peak. The result is an incomplete, conservative
observed extent: missing detections are expected, so model 'false alarms'
against it are partly SAR omissions.
"""

from __future__ import annotations

import concurrent.futures as cf
from pathlib import Path
from typing import Any

import numpy as np
from scipy import ndimage

from .geo import Grid, reproject_to_grid, write_raster
from .provenance import sha256_file, utcnow


def to_db(lin: np.ndarray, floor: float = 1e-5) -> np.ndarray:
    lin = np.asarray(lin, dtype="float32")
    out = 10.0 * np.log10(np.maximum(lin, floor))
    out[~np.isfinite(lin)] = np.nan
    return out


def boxcar(lin: np.ndarray, size: int = 3) -> np.ndarray:
    """NaN-aware mean filter in linear power."""
    v = np.where(np.isfinite(lin), lin, 0.0)
    w = np.isfinite(lin).astype("float32")
    num = ndimage.uniform_filter(v, size=size, mode="nearest")
    den = ndimage.uniform_filter(w, size=size, mode="nearest")
    with np.errstate(invalid="ignore", divide="ignore"):
        out = num / den
    out[den < 0.5] = np.nan
    return out.astype("float32")


def otsu_threshold(x: np.ndarray, bins: int = 256, lo: float = -23.0, hi: float = -15.0) -> float:
    """Otsu (1979) threshold of finite values, clamped to [lo, hi]."""
    v = x[np.isfinite(x)]
    if v.size < 100:
        raise ValueError("too few valid pixels for Otsu threshold")
    hist, edges = np.histogram(np.clip(v, -35, 5), bins=bins)
    centers = 0.5 * (edges[:-1] + edges[1:])
    w0 = np.cumsum(hist)
    w1 = w0[-1] - w0
    m = np.cumsum(hist * centers)
    mu0 = m / np.maximum(w0, 1)
    mu1 = (m[-1] - m) / np.maximum(w1, 1)
    var = w0 * w1 * (mu0 - mu1) ** 2
    t = float(centers[int(np.argmax(var))])
    return float(min(max(t, lo), hi))


def detect_flood(post_db: np.ndarray, ref_db: np.ndarray, region: np.ndarray, drop_db: float = 3.0,
                 threshold: float | None = None) -> dict[str, Any]:
    t = threshold if threshold is not None else otsu_threshold(np.where(region, post_db, np.nan))
    valid = np.isfinite(post_db) & np.isfinite(ref_db)
    perm = valid & (ref_db < t)
    flood = valid & (post_db < t) & ((post_db - ref_db) <= -drop_db) & ~perm
    return {"flood": flood, "permanent_water": perm, "valid": valid, "threshold_db": t, "drop_db": drop_db}


# ------------------------------------------------------------ acquisition
def download_sentinel1_rtc(catalog, d: dict[str, Any], out: Path, grid: Grid | None = None, bbox_wgs84=None,
                           scenes: list[dict[str, Any]] | None = None, workers: int = 3, **_: Any):
    """Read VV/VH of the named scenes (by date + relative orbit) onto ``grid``.

    ``scenes``: list of {"role": "post_2019"|"ref"|..., "date": "YYYY-MM-DD", "orbit": int}.
    """
    import planetary_computer as pc
    from pystac_client import Client

    if grid is None or bbox_wgs84 is None or not scenes:
        raise ValueError("download_sentinel1_rtc needs grid, bbox_wgs84 and scenes")
    client = Client.open(d["urls"][0], modifier=pc.sign_inplace)
    jobs = []
    for sc in scenes:
        items = list(client.search(collections=[d["collection"]], bbox=bbox_wgs84,
                                   datetime=f"{sc['date']}T00:00:00Z/{sc['date']}T23:59:59Z").items())
        items = [i for i in items if i.properties.get("sat:relative_orbit") == sc["orbit"]]
        if not items:
            raise RuntimeError(f"no sentinel-1-rtc item for {sc}")
        jobs.append((sc, items))

    pols = list(d.get("polarisations", ["vv"]))
    ovr = d.get("overview_level", 0)  # 0 = first internal overview (20 m for 10 m RTC)
    gdal_env = {"GDAL_DISABLE_READDIR_ON_OPEN": "EMPTY_DIR", "GDAL_HTTP_MERGE_CONSECUTIVE_RANGES": "YES",
                "GDAL_HTTP_MULTIPLEX": "YES", "GDAL_HTTP_VERSION": "2", "GDAL_HTTP_MAX_RETRY": "6",
                "GDAL_HTTP_RETRY_DELAY": "3", "GDAL_HTTP_TIMEOUT": "120", "VSI_CACHE": "TRUE"}

    def one(job):
        import logging
        import time as _t

        import rasterio

        sc, items = job
        dest = out / f"s1rtc_{sc['date']}_o{sc['orbit']}_grid.tif"
        last = None
        for attempt in range(1, 4):
            log: list[dict[str, Any]] = []
            t0 = _t.time()
            try:
                with rasterio.Env(**gdal_env):
                    bands = {}
                    for pol in pols:
                        # re-sign on each attempt (SAS tokens expire)
                        hrefs = [pc.sign(it.assets[pol].href) for it in items]
                        bands[pol] = reproject_to_grid(hrefs, grid, "continuous", src_nodata=None, log=log,
                                                       open_options=None if ovr is None else {"OVERVIEW_LEVEL": ovr})
                for pol in pols:  # asset nodata is -32768 (honoured above); also drop non-positive power
                    bands[pol] = np.where(bands[pol] > 0, bands[pol], np.nan).astype("float32")
                break
            except Exception as e:  # truncated tiles / resets on a slow link
                last = f"{type(e).__name__}: {str(e)[:200]}"
                logging.getLogger("earthsim").warning("S1 %s attempt %d failed: %s", sc["date"], attempt, last)
                _t.sleep(5 * attempt)
        else:
            raise RuntimeError(f"S1 scene {sc} failed after 3 attempts: {last}")
        write_raster(dest, np.stack([bands[p] for p in pols]), grid, band_desc=[f"{p}_gamma0_lin" for p in pols],
                     tags={"date": sc["date"], "orbit": sc["orbit"], "role": sc["role"],
                           "items": ",".join(i.id for i in items), "units": "gamma0 linear power (RTC)",
                           "source_overview_level": str(ovr), "data_class": "observation"})
        print(f"[s1] {sc['date']} o{sc['orbit']} {sc['role']}: {len(items)} item(s) in {_t.time()-t0:.0f}s "
              f"(attempt {attempt})", flush=True)
        return {"path": str(dest), "role": sc["role"], "date": sc["date"], "relative_orbit": sc["orbit"],
                "item_ids": [i.id for i in items], "orbit_state": items[0].properties.get("sat:orbit_state"),
                "platform": items[0].properties.get("platform"), "polarisations": pols,
                "source_overview_level": ovr, "resampling_log": log, "attempts": attempt,
                "seconds": round(_t.time() - t0, 1),
                "sha256": sha256_file(dest), "bytes": dest.stat().st_size, "origin": "download",
                "retrieved_utc": utcnow()}

    with cf.ThreadPoolExecutor(max_workers=workers) as ex:
        files = list(ex.map(one, jobs))
    return files, {"grid": grid.to_dict(), "scenes": scenes, "endpoint": d["urls"][0]}
