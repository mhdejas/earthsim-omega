"""Stage pipelines that tie catalog, geospatial core and science modules together.

Each stage writes outputs under ``data/processed/<stage>/`` plus a provenance
record, and returns a summary dict. Stages are idempotent and config-driven.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import numpy as np
from pyproj import Transformer

from .catalog import Catalog
from .config import Settings
from .geo import AOI, Grid, aoi_mask, grid_for_aoi, read_aoi, reproject_to_grid, write_raster
from .provenance import RunRecord, atomic_write_text


def catalog(s: Settings) -> Catalog:
    return Catalog(s.p.manifest, s.p.raw_dir, s.p.processed_dir)


def load_aoi(s: Settings) -> AOI:
    c = catalog(s)
    acq = c.acquisition(s.region.boundary_dataset) or c.acquire(s.region.boundary_dataset)
    f = next(x["path"] for x in acq["files"] if x.get("role") == "aoi")
    return read_aoi(f)


def region_grid(s: Settings, res: float, aoi: AOI | None = None, buffer: float | None = None) -> Grid:
    aoi = aoi or load_aoi(s)
    return grid_for_aoi(aoi, s.region.analysis_crs, res,
                        buffer=s.region.fetch_buffer_m if buffer is None else buffer)


def grid_bbox_wgs84(grid: Grid) -> tuple[float, float, float, float]:
    t = Transformer.from_crs(grid.crs, "EPSG:4326", always_xy=True)
    l, b, r, tp = grid.bounds
    xs, ys = t.transform([l, l, r, r], [b, tp, b, tp])
    return (min(xs), min(ys), max(xs), max(ys))


def out_dir(s: Settings, stage: str) -> Path:
    d = s.p.processed_dir / stage
    d.mkdir(parents=True, exist_ok=True)
    return d


# ----------------------------------------------------------- Sentinel-2
def stage_s2_acquire(s: Settings, per_tile: int = 3, workers: int = 4, force: bool = False) -> dict[str, Any]:
    pr = s.processing
    grid = region_grid(s, pr.optical_resolution_m)
    c = catalog(s)
    acq = c.acquire("sentinel2_l2a_2025q1", mode="download", force=force, grid=grid,
                    bbox_wgs84=grid_bbox_wgs84(grid), start=pr.composite_start, end=pr.composite_end,
                    max_cloud=pr.max_scene_cloud_pct, per_tile=per_tile, workers=workers)
    return {"items": len(acq["files"]), "search": acq.get("search")}


def stage_s2_indices(s: Settings) -> dict[str, Any]:
    from .remote_sensing import S2_BANDS, build_composite, compute_indices

    rr = RunRecord("remote_sensing_indices", s.processing.model_dump())
    c = catalog(s)
    acq = c.acquisition("sentinel2_l2a_2025q1")
    if not acq:
        raise RuntimeError("Sentinel-2 not acquired; run `earthsim remote-sensing acquire` first")
    aoi = load_aoi(s)
    grid = Grid(**{k: v for k, v in _grid_from_acq(acq).items()})
    files = [f["path"] for f in acq["files"]]
    for f in acq["files"]:
        rr.add_input(f["path"], "sentinel2_l2a_2025q1", checksum=False, item_id=f["item_id"], sha256=f["sha256"])
    m = aoi_mask(aoi, grid)
    res = build_composite(files, grid, m)
    od = out_dir(s, "remote_sensing")
    stack = np.stack([res["bands"][b] for b in S2_BANDS])
    p = write_raster(od / "s2_median_composite.tif", stack, grid, band_desc=S2_BANDS,
                     tags={"units": "surface reflectance", "composite": "per-pixel median of SCL-valid obs",
                           "period": f"{s.processing.composite_start}/{s.processing.composite_end}",
                           "data_class": "observation-derived composite"})
    rr.add_output(p, "raster.reflectance_composite")
    p = write_raster(od / "s2_valid_obs_count.tif", res["valid_count"], grid, nodata=None,
                     tags={"units": "count of valid observations"})
    rr.add_output(p, "raster.valid_count")
    idx = compute_indices(res["bands"])
    stats = {}
    for name, arr in idx.items():
        p = write_raster(od / f"{name}.tif", arr, grid, tags={"index": name, "units": "unitless [-1,1]"})
        rr.add_output(p, f"raster.index.{name}")
        v = arr[m & ~np.isnan(arr)]
        stats[name] = {"valid_px": int(v.size), "mean": float(v.mean()), "p5": float(np.percentile(v, 5)),
                       "p50": float(np.percentile(v, 50)), "p95": float(np.percentile(v, 95)),
                       "min": float(v.min()), "max": float(v.max()),
                       "out_of_range_px": int(((v < -1) | (v > 1)).sum())}
    qa = res["qa"] | {"index_stats_in_aoi": stats, "grid": grid.to_dict()}
    atomic_write_text(od / "quality_summary.json", json.dumps(qa, indent=2))
    rr.add_output(od / "quality_summary.json", "json.quality")
    rr.data["data_classification"] = "observation-derived (Sentinel-2 L2A median composite)"
    rr.finish(**{"aoi_valid_fraction": qa.get("aoi_valid_fraction")})
    rr.write(od / "provenance.json")
    return {"outputs": str(od), "qa": {k: qa[k] for k in ("aoi_valid_fraction", "valid_obs_histogram")}, "stats": stats}


def _grid_from_acq(acq: dict[str, Any]) -> dict[str, Any]:
    from affine import Affine

    g = acq["grid"]
    return {"crs": g["crs"], "transform": Affine(*g["transform"]), "width": g["width"], "height": g["height"]}


# ------------------------------------------------------------- Terrain
def stage_terrain(s: Settings) -> dict[str, Any]:
    from . import hydrology as hy
    from .geo import reproject_to_grid

    res = s.processing.dem_resolution_m
    rr = RunRecord("terrain_hydrology", {"dem_resolution_m": res, "stream_threshold_km2": s.processing.stream_threshold_km2})
    c = catalog(s)
    acq = c.acquisition("copernicus_dem_glo30") or c.acquire("copernicus_dem_glo30")
    aoi = load_aoi(s)
    grid = region_grid(s, res, aoi, buffer=s.processing.terrain_buffer_m)
    files = [f["path"] for f in acq["files"]]
    for f in acq["files"]:
        rr.add_input(f["path"], "copernicus_dem_glo30", checksum=False, sha256=f["sha256"])
    rlog: list = []
    dem = reproject_to_grid(files, grid, "continuous", log=rlog)
    rr.event("dem reprojected", resampling=rlog)
    od = out_dir(s, "terrain")
    q = hy.dem_quality(dem, res)
    route = hy.condition_and_route(dem, grid, od)
    ocean = route["ocean"]
    thr_cells = s.processing.stream_threshold_km2 * 1e6 / (res * res)
    streams = (route["acc"] >= thr_cells) & ~ocean
    hand = hy.hand(route, streams)
    slope = hy.slope_degrees(dem, res)
    twi = hy.twi(route["acc"], slope, res)
    dist = hy.distance_to(streams | ocean, res)
    for arr in (hand, slope, twi, dist):
        arr[ocean] = np.nan
    m = aoi_mask(aoi, grid)
    outs = {
        "dem.tif": (dem, "m (EGM2008), Copernicus GLO-30 DSM bilinear to 30 m UTM43N"),
        "dem_filled.tif": (route["filled"], "m, depression-filled (pysheds)"),
        "flow_dir_d8.tif": (route["fdir"].astype("int16"), "ESRI D8 code"),
        "flow_accumulation.tif": (route["acc"].astype("float32"), "upstream cells (count)"),
        "streams.tif": (streams.astype("uint8"), f"1 = accumulation >= {s.processing.stream_threshold_km2} km2"),
        "hand.tif": (hand, "m above nearest drainage (HAND)"),
        "slope.tif": (slope, "degrees"),
        "twi.tif": (twi, "ln(m) topographic wetness index"),
        "dist_to_stream.tif": (dist, "m Euclidean to nearest stream/ocean cell"),
        "ocean_mask.tif": (ocean.astype("uint8"), "1 = border-connected cells <= 0 m"),
        "aoi_mask.tif": (m.astype("uint8"), "1 = inside district (pixel-centre rule)"),
    }
    for name, (arr, units) in outs.items():
        nd = None if arr.dtype.kind in "iu" else np.nan
        p = write_raster(od / name, arr, grid, nodata=nd, tags={"units": units})
        rr.add_output(p, f"raster.terrain.{name[:-4]}")
    summ = {"dem_quality": q, "conditioning": route["diag"],
            "streams": hy.stream_order_free_summary(streams & m, route["acc"], res),
            "stream_threshold_cells": thr_cells, "grid": grid.to_dict(),
            "aoi_stats": {"hand_median_m": float(np.nanmedian(hand[m])), "slope_median_deg": float(np.nanmedian(slope[m])),
                          "dem_aoi_min_m": float(np.nanmin(dem[m])), "dem_aoi_max_m": float(np.nanmax(dem[m]))}}
    atomic_write_text(od / "terrain_summary.json", json.dumps(summ, indent=2, default=str))
    rr.add_output(od / "terrain_summary.json", "json.summary")
    labels, basins, bsum = hy.basin_catalog(route["fdir"], ocean, m, dem, grid)
    from pyproj import Transformer

    tf = Transformer.from_crs(grid.crs, "EPSG:4326", always_xy=True)
    l, b, r_, t_ = grid.bounds
    xs, ys = tf.transform([l, r_, l, r_], [b, b, t_, t_])
    summ["domain"] = {"crs": grid.crs, "res_m": res, "bounds_utm": grid.bounds,
                      "bbox_lonlat": [round(min(xs), 4), round(min(ys), 4), round(max(xs), 4), round(max(ys), 4)],
                      "size_px": [grid.width, grid.height], "area_km2": grid.width * grid.height * res * res / 1e6,
                      "buffer_m": s.processing.terrain_buffer_m}
    summ["basins_summary"] = bsum
    summ["basins"] = basins
    atomic_write_text(od / "terrain_summary.json", json.dumps(summ, indent=2, default=str))
    p = write_raster(od / "basin_outlet_id.tif", labels.astype("int64").astype("float64"), grid, nodata=None,
                     tags={"units": "flat index of D8 outlet cell (basin label)"})
    rr.add_output(p, "raster.terrain.basins")
    rr.data["data_classification"] = "derived from reference DEM product"
    rr.finish(**bsum)
    rr.write(od / "provenance.json")
    return summ


# ------------------------------------------------------------- Land cover
def stage_landcover(s: Settings, n_sample: int = 300_000, block_m: float = 5000.0, k: int = 5) -> dict[str, Any]:
    """RF land cover from the S2 composite, spatial-block CV vs ESA WorldCover 2021."""
    from sklearn.ensemble import RandomForestClassifier

    from . import landcover as lc
    from .remote_sensing import S2_BANDS

    rr = RunRecord("landcover", s.processing.model_dump() | {"n_sample": n_sample, "block_m": block_m, "k": k})
    rs_dir = out_dir(s, "remote_sensing")
    comp, grid, _ = None, None, None
    import rasterio

    with rasterio.open(rs_dir / "s2_median_composite.tif") as ds:
        comp = ds.read().astype("float32")
        from affine import Affine

        grid = Grid(ds.crs.to_string(), Affine(*tuple(ds.transform)[:6]), ds.width, ds.height)
    rr.add_input(rs_dir / "s2_median_composite.tif", "s2_composite")
    c = catalog(s)
    wc_files = c.files("esa_worldcover_2021")
    log: list = []
    wc = reproject_to_grid(wc_files, grid, "categorical", log=log)
    for f in wc_files:
        rr.add_input(f, "esa_worldcover_2021", checksum=False)
    td = out_dir(s, "terrain")
    dem = reproject_to_grid(td / "dem.tif", grid, "continuous", log=log)
    slope = reproject_to_grid(td / "slope.tif", grid, "continuous", log=log)
    bands = {b: comp[i] for i, b in enumerate(S2_BANDS)}
    X3, names = lc.feature_stack(bands, {"elevation": dem, "slope": slope})
    aoi = load_aoi(s)
    m = aoi_mask(aoi, grid) & np.all(np.isfinite(X3), axis=0) & np.isfinite(wc) & (wc > 0)
    rows, cols = np.nonzero(m)
    rng = np.random.default_rng(s.seed if hasattr(s, "seed") else 42)
    pick = rng.choice(rows.size, size=min(n_sample, rows.size), replace=False)
    r_, c_ = rows[pick], cols[pick]
    X = X3[:, r_, c_].T
    y = wc[r_, c_].astype("int16")
    groups = lc.block_ids(r_, c_, int(block_m / grid.res), grid.width)
    labels = sorted(int(v) for v in np.unique(y))
    base = {n: X[:, names.index(n)] for n in ("ndvi", "mndwi", "ndbi")}
    import time

    t0 = time.time()
    cv = lc.spatial_cv(X, y, groups, base, labels, k=k)
    t_cv = time.time() - t0
    # final model on all samples -> wall-to-wall map
    t0 = time.time()
    rf = RandomForestClassifier(n_estimators=150, min_samples_leaf=2, n_jobs=-1, random_state=42,
                                class_weight="balanced_subsample", max_features="sqrt").fit(X, y)
    t_fit = time.time() - t0
    t0 = time.time()
    lcmap = np.zeros(grid.shape, dtype="uint8")
    valid = aoi_mask(aoi, grid) & np.all(np.isfinite(X3), axis=0)
    vr, vc = np.nonzero(valid)
    for i in range(0, vr.size, 1_000_000):
        sl = slice(i, i + 1_000_000)
        lcmap[vr[sl], vc[sl]] = rf.predict(X3[:, vr[sl], vc[sl]].T).astype("uint8")
    t_pred = time.time() - t0
    od = out_dir(s, "landcover")
    p = write_raster(od / "landcover_2025_rf.tif", lcmap, grid, nodata=0,
                     tags={"classes": json.dumps(lc.WC_CLASSES), "nodata": "0 = outside district / no valid obs",
                           "data_class": "model-derived (RF trained on WorldCover 2021 labels)"})
    rr.add_output(p, "raster.landcover")
    wc_u8 = np.where(np.isfinite(wc), wc, 0).astype("uint8")
    p = write_raster(od / "worldcover_2021_on_grid.tif", wc_u8, grid, nodata=0,
                     tags={"resampling": "mode (10 m -> 20 m)", "data_class": "reference_product"})
    rr.add_output(p, "raster.worldcover_ref")
    area = {lc.WC_CLASSES[int(v)]: round(float((lcmap == v).sum() * grid.res ** 2 / 1e6), 2)
            for v in np.unique(lcmap) if v}
    area_wc = {lc.WC_CLASSES[int(v)]: round(float(((wc_u8 == v) & valid).sum() * grid.res ** 2 / 1e6), 2)
               for v in np.unique(wc_u8[valid]) if v}
    imp = sorted(zip(names, rf.feature_importances_.round(4).tolist()), key=lambda x: -x[1])
    summ = {"grid": grid.to_dict(), "features": names, "n_samples": int(y.size), "block_m": block_m, "folds": k,
            "blocks": int(np.unique(groups).size), "cv": cv, "feature_importance": imp,
            "area_km2_rf_2025": area, "area_km2_worldcover_2021": area_wc,
            "timing_s": {"spatial_cv": round(t_cv, 1), "final_fit": round(t_fit, 1), "predict": round(t_pred, 1)},
            "resampling_log": log,
            "caveat": "Agreement with WorldCover 2021 (itself ~77% accurate globally; 4 years older), not accuracy vs ground truth."}
    atomic_write_text(od / "landcover_summary.json", json.dumps(summ, indent=2, default=str))
    rr.add_output(od / "landcover_summary.json", "json.landcover")
    rr.data["data_classification"] = "model-derived"
    rr.finish(rf_oa=cv["random_forest"]["overall_accuracy"])
    rr.write(od / "provenance.json")
    return summ


# ------------------------------------------------------------- SAR flood observation
S1_SCENES = [
    {"role": "event_2018-08-09", "date": "2018-08-09", "orbit": 165},
    {"role": "event_2018-08-14", "date": "2018-08-14", "orbit": 63},
    {"role": "event_2019-08-10", "date": "2019-08-10", "orbit": 165},
    {"role": "ref", "date": "2019-01-12", "orbit": 165},
    {"role": "ref", "date": "2019-01-24", "orbit": 165},
    {"role": "ref", "date": "2019-01-05", "orbit": 63},
    {"role": "ref", "date": "2019-01-17", "orbit": 63},
]


def district_grid(s: Settings, res: float = 30.0) -> Grid:
    """30 m district grid (AOI + fetch buffer); an exact window of the terrain grid (60 m lattice)."""
    return region_grid(s, res)


def stage_s1_acquire(s: Settings, force: bool = False, workers: int = 4) -> dict[str, Any]:
    grid = district_grid(s)
    acq = catalog(s).acquire("sentinel1_rtc_floods", mode="download", force=force, grid=grid,
                             bbox_wgs84=grid_bbox_wgs84(grid), scenes=S1_SCENES, workers=workers)
    return {"files": [(f["role"], f["date"], f["relative_orbit"], f["item_ids"], f["bytes"]) for f in acq["files"]]}


def crop_to_grid(path: str | Path, grid: Grid, band: int = 1) -> np.ndarray:
    """Exact window read of a raster whose grid contains ``grid`` (same CRS/res, lattice-aligned)."""
    import rasterio
    from rasterio.windows import from_bounds

    from .geo import GeoError, grids_aligned

    with rasterio.open(path) as ds:
        src = Grid(ds.crs.to_string(), ds.transform, ds.width, ds.height)
        if not grids_aligned(src, grid):
            raise GeoError(f"{path} is not aligned with the target grid; reproject instead of cropping")
        w = from_bounds(*grid.bounds, transform=ds.transform).round_offsets().round_lengths()
        a = ds.read(band, window=w, masked=True)
        return a.astype("float32").filled(np.nan)


FLOOD_EVENTS = {"2018-08-09": 165, "2018-08-14": 63, "2019-08-10": 165}


def stage_flood_obs(s: Settings) -> dict[str, Any]:
    """Observed open-water flood extents from Sentinel-1 RTC change detection (observation-derived)."""
    import rasterio

    from . import sar

    rr = RunRecord("flood_obs", s.processing.model_dump() | {"events": FLOOD_EVENTS})
    acq = catalog(s).acquisition("sentinel1_rtc_floods")
    if not acq:
        raise RuntimeError("sentinel1_rtc_floods not acquired; run stage_s1_acquire first")
    grid = district_grid(s)
    aoi = aoi_mask(load_aoi(s), grid)
    by = {(f["date"], f["relative_orbit"]): f for f in acq["files"]}
    refs = {}
    for f in acq["files"]:
        if f["role"] == "ref":
            refs.setdefault(f["relative_orbit"], []).append(f)
    od = out_dir(s, "flood")
    summ: dict[str, Any] = {"grid": grid.to_dict(), "events": {}}

    def vv(f):
        rr.add_input(f["path"], "sentinel1_rtc_floods")
        with rasterio.open(f["path"]) as ds:
            return ds.read(1).astype("float32")

    for date, orbit in FLOOD_EVENTS.items():
        post = sar.boxcar(vv(by[(date, orbit)]))
        ref_stack = np.stack([vv(f) for f in refs[orbit]])
        with np.errstate(all="ignore"):
            import warnings

            with warnings.catch_warnings():
                warnings.simplefilter("ignore", RuntimeWarning)
                ref = sar.boxcar(np.nanmedian(ref_stack, axis=0))
        post_db, ref_db = sar.to_db(post), sar.to_db(ref)
        r = sar.detect_flood(post_db, ref_db, aoi & np.isfinite(post_db))
        cls = np.zeros(grid.shape, "uint8")  # 0 no data / outside district
        inside = aoi & r["valid"]
        cls[inside] = 1
        cls[inside & r["flood"]] = 2
        cls[inside & r["permanent_water"]] = 3
        p = write_raster(od / f"s1_flood_observed_{date}.tif", cls, grid, nodata=0,
                         tags={"classes": "1=not flooded (SAR), 2=flooded open water, 3=permanent/seasonal water in "
                                          "dry-season reference, 0=no data/outside district",
                               "data_class": "observation-derived (Sentinel-1 VV change detection)",
                               "threshold_db": round(r["threshold_db"], 2), "drop_db": r["drop_db"],
                               "reference_dates": ",".join(f["date"] for f in refs[orbit]),
                               "limitations": "misses water under canopy and in built-up areas; snapshot at ~06:10 IST"})
        rr.add_output(p, "raster.flood_observed")
        px = grid.res ** 2 / 1e6
        summ["events"][date] = {
            "relative_orbit": orbit, "post_item_ids": by[(date, orbit)]["item_ids"],
            "reference_dates": [f["date"] for f in refs[orbit]], "threshold_db": round(r["threshold_db"], 2),
            "drop_db": r["drop_db"], "district_valid_km2": round(float(inside.sum() * px), 1),
            "district_valid_fraction": round(float(inside.sum() / aoi.sum()), 4),
            "flooded_km2": round(float((cls == 2).sum() * px), 2),
            "permanent_water_km2": round(float((cls == 3).sum() * px), 2),
            "post_vv_db_median_district": round(float(np.nanmedian(post_db[aoi])), 2),
            "ref_vv_db_median_district": round(float(np.nanmedian(ref_db[aoi])), 2)}
    atomic_write_text(od / "flood_obs_summary.json", json.dumps(summ, indent=2, default=str))
    rr.add_output(od / "flood_obs_summary.json", "json.flood_obs")
    rr.data["data_classification"] = "observation-derived"
    rr.finish()
    rr.write(od / "provenance_flood_obs.json")
    return summ


def stage_flood_susceptibility(s: Settings, cal_event: str = "2018-08-14",
                               test_events: tuple[str, ...] = ("2019-08-10",),
                               block_m: float = 5000.0, k: int = 5) -> dict[str, Any]:
    """Interpretable terrain susceptibility; fitted on cal_event, scored on test_events in held-out blocks."""
    import rasterio
    from sklearn.linear_model import LogisticRegression
    from sklearn.pipeline import make_pipeline
    from sklearn.preprocessing import StandardScaler

    from . import floodsus as fs
    from .landcover import block_ids

    rr = RunRecord("flood_susceptibility", {"cal_event": cal_event, "test_events": list(test_events),
                                            "block_m": block_m, "k": k})
    grid = district_grid(s)
    td = out_dir(s, "terrain")
    od = out_dir(s, "flood")
    for n in ("hand", "flow_accumulation", "slope", "dem", "dist_to_stream"):
        rr.add_input(td / f"{n}.tif", f"terrain.{n}")
    F = {"hand": crop_to_grid(td / "hand.tif", grid),
         "log_upstream_km2": np.log10(np.maximum(crop_to_grid(td / "flow_accumulation.tif", grid), 1)
                                      * grid.res ** 2 / 1e6),
         "slope_deg": crop_to_grid(td / "slope.tif", grid), "elevation": crop_to_grid(td / "dem.tif", grid),
         "dist_to_stream_m": crop_to_grid(td / "dist_to_stream.tif", grid)}
    obs = {}
    for e in (cal_event, *test_events):
        with rasterio.open(od / f"s1_flood_observed_{e}.tif") as ds:
            obs[e] = ds.read(1)
        rr.add_input(od / f"s1_flood_observed_{e}.tif", "flood_observed")
    log: list = []
    ks = {}
    for f in catalog(s).files("ksdma_flood_hazard"):
        rp = "10" if "_10_" in f.name else "25" if "_25_" in f.name else "50"
        a = reproject_to_grid(f, grid, "mask", log=log)
        ks[f"ksdma_{rp}yr_footprint"] = np.isfinite(a) & (a > 0)
    aoi = aoi_mask(load_aoi(s), grid)
    feat_ok = np.all(np.stack([np.isfinite(v) for v in F.values()]), axis=0)
    perm = np.zeros(grid.shape, bool)
    for e in obs:
        perm |= obs[e] == 3
    base = aoi & feat_ok & ~perm
    rows, cols = np.nonzero(base)
    groups = block_ids(rows, cols, int(block_m / grid.res), grid.width)
    f1d = {n: v[rows, cols] for n, v in F.items()}
    ext = {n: v[rows, cols] for n, v in ks.items()}
    cal = obs[cal_event][rows, cols]
    results: dict[str, Any] = {"grid": grid.to_dict(), "pixels": int(rows.size), "blocks": int(np.unique(groups).size),
                               "excluded": {"permanent_water_any_date_px": int((aoi & perm).sum()),
                                            "missing_terrain_px": int((aoi & ~feat_ok).sum())},
                               "calibration_event": cal_event, "tests": {}, "resampling_log": log}
    score = None
    for te in test_events:
        tst = obs[te][rows, cols]
        res, sc = fs.evaluate_models(f1d, cal == 2, tst == 2, cal > 0, tst > 0, groups, ext, k=k)
        results["tests"][te] = res
        score = sc if score is None else score
    # wall-to-wall susceptibility score from a model fitted on all calibration pixels
    X = np.column_stack([f1d[n] for n in results["tests"][test_events[0]]["features"]])
    m = cal > 0
    lr = make_pipeline(StandardScaler(), LogisticRegression(max_iter=500, class_weight="balanced", random_state=42))
    lr.fit(X[m], cal[m] == 2)
    full = np.full(grid.shape, np.nan, "float32")
    full[rows, cols] = lr.predict_proba(X)[:, 1]
    p = write_raster(od / "flood_susceptibility_score.tif", full, grid,
                     tags={"meaning": "relative terrain flood-susceptibility score (logistic, class-balanced). "
                                      "NOT a probability, NOT a depth, NOT a forecast.",
                           "fitted_on": f"Sentinel-1 observed flood {cal_event} (district, all blocks)",
                           "features": ",".join(results["tests"][test_events[0]]["features"]),
                           "data_class": "model-derived"})
    rr.add_output(p, "raster.flood_susceptibility")
    results["final_model_std_coefs"] = dict(zip(results["tests"][test_events[0]]["features"],
                                                lr[-1].coef_[0].round(4).tolist()))
    atomic_write_text(od / "flood_susceptibility_eval.json", json.dumps(results, indent=2, default=str))
    rr.add_output(od / "flood_susceptibility_eval.json", "json.flood_eval")
    rr.data["data_classification"] = "model-derived; evaluated against observation-derived SAR extents"
    rr.finish()
    rr.write(od / "provenance_flood_susceptibility.json")
    return results


def stage_hindcast_eval(s: Settings, event: str = "2019-08-10", tags: tuple[str, ...] = ("base", "c050", "c090"),
                        hand_threshold_m: float = 0.5) -> dict[str, Any]:
    """Score each hindcast run against the SAR-observed extent of ``event`` on the district grid, together with
    a HAND<=threshold rule and the KSDMA footprints scored on exactly the same reference pixels."""
    from .scenario import evaluate_hindcast

    grid = district_grid(s)
    td, od, hd = out_dir(s, "terrain"), out_dir(s, "flood"), out_dir(s, "hydraulics")
    rr = RunRecord("hindcast_eval", {"event": event, "tags": list(tags), "hand_threshold_m": hand_threshold_m})
    aoi = aoi_mask(load_aoi(s), grid)
    hand = crop_to_grid(td / "hand.tif", grid)
    extra = {f"hand<={hand_threshold_m}m": np.nan_to_num(hand, nan=1e9) <= hand_threshold_m}
    log: list = []
    for f in catalog(s).files("ksdma_flood_hazard"):
        if "_25_" in f.name:
            a = reproject_to_grid(f, grid, "mask", log=log)
            extra["ksdma_25yr_footprint"] = np.isfinite(a) & (a > 0)
    obs_path = od / f"s1_flood_observed_{event}.tif"
    rr.add_input(obs_path, "flood_observed")
    runs = {}
    for tag in tags:
        p = hd / f"{tag}_summary.json"
        if not p.exists():
            continue
        summ = json.loads(p.read_text())
        summ["outputs"] = {k: str(s.root / v) if not Path(v).is_absolute() else v
                           for k, v in summ["outputs"].items()}
        rr.add_input(Path(summ["outputs"]["depth_at_overpass"]), f"hindcast.{tag}")
        ev = evaluate_hindcast(summ, obs_path, grid, aoi, extra)
        ev["params"] = summ["params"]
        ev["mass_relative_residual"] = summ["mass"]["relative_residual"]
        ev["h_max_m"] = summ["diagnostics"]["h_max_m"]
        ev["v_max_ms"] = summ["diagnostics"]["v_max_ms"]
        runs[tag] = ev
    out = {"event": event, "note": "SAR reference covers only valid pixels of orbit 165 (eastern ~14% of the "
                                     "district) and sees open water only; scores are indicative.",
           "runs": runs}
    (hd / "hindcast_eval.json").write_text(json.dumps(out, indent=2, default=str))
    rr.finish(runs=list(runs))
    rr.write(hd / "provenance_hindcast_eval.json")
    return out
