"""Earth-observation engine: spectral indices, Sentinel-2 L2A acquisition from a
public STAC API, quality masking, compositing and quality summaries.

Sentinel-2 L2A (Collection 1 on Earth Search) conventions used here:
* reflectance = DN * scale + offset, with scale/offset read from each asset's
  ``raster:bands`` metadata (0.0001 and -0.1 for processing baseline >= 04.00);
* DN == 0 is nodata;
* Scene Classification Layer (SCL) classes 4 (vegetation), 5 (not vegetated),
  6 (water) are accepted; 0-3, 7-11 (nodata, saturated, dark/shadow, cloud
  shadow, unclassified, clouds, cirrus, snow) are rejected. This is an optical
  mask and must never be applied to SAR.
"""

from __future__ import annotations

import concurrent.futures as cf
import json
from pathlib import Path
from typing import Any

import numpy as np

from .geo import Grid, reproject_to_grid
from .provenance import sha256_file, utcnow

S2_BANDS = ["blue", "green", "red", "nir", "swir16", "swir22"]
SCL_VALID = (4, 5, 6)
SCL_NAMES = {0: "no_data", 1: "saturated_defective", 2: "dark_area/topographic_shadow", 3: "cloud_shadow",
             4: "vegetation", 5: "not_vegetated", 6: "water", 7: "unclassified", 8: "cloud_medium_prob",
             9: "cloud_high_prob", 10: "thin_cirrus", 11: "snow_ice"}


# ----------------------------------------------------------------- indices
def normalized_difference(a: np.ndarray, b: np.ndarray) -> np.ndarray:
    """(a - b) / (a + b) with NaN where either input is NaN or a+b == 0.

    Inputs must be surface reflectance (not DN). Result is in [-1, 1] whenever
    both inputs are non-negative; pixels with negative reflectance are returned
    as NaN because the ratio is not physically meaningful there.
    """
    a = np.asarray(a, dtype="float32")
    b = np.asarray(b, dtype="float32")
    with np.errstate(divide="ignore", invalid="ignore"):
        den = a + b
        out = (a - b) / den
    bad = (den == 0) | np.isnan(a) | np.isnan(b) | (a < 0) | (b < 0)
    out[bad] = np.nan
    return out


def ndvi(red, nir):
    """NDVI = (NIR - Red) / (NIR + Red)  (Rouse et al. 1974)."""
    return normalized_difference(nir, red)


def ndwi(green, nir):
    """NDWI (McFeeters 1996) = (Green - NIR) / (Green + NIR); open-water index."""
    return normalized_difference(green, nir)


def mndwi(green, swir16):
    """MNDWI (Xu 2006) = (Green - SWIR1) / (Green + SWIR1)."""
    return normalized_difference(green, swir16)


def ndbi(swir16, nir):
    """NDBI (Zha et al. 2003) = (SWIR1 - NIR) / (SWIR1 + NIR); built-up indicator, confounded by bare soil."""
    return normalized_difference(swir16, nir)


def ndmi(nir, swir16):
    """NDMI (Gao 1996 NDWI-SWIR) = (NIR - SWIR1) / (NIR + SWIR1); canopy moisture."""
    return normalized_difference(nir, swir16)


INDEX_FUNCS = {
    "ndvi": (ndvi, ("red", "nir")),
    "ndwi": (ndwi, ("green", "nir")),
    "mndwi": (mndwi, ("green", "swir16")),
    "ndbi": (ndbi, ("swir16", "nir")),
    "ndmi": (ndmi, ("nir", "swir16")),
}


def compute_indices(bands: dict[str, np.ndarray], names: list[str] | None = None) -> dict[str, np.ndarray]:
    out = {}
    for n in names or list(INDEX_FUNCS):
        fn, req = INDEX_FUNCS[n]
        missing = [r for r in req if r not in bands]
        if missing:
            raise KeyError(f"index {n} needs bands {req}; missing {missing}")
        out[n] = fn(*(bands[r] for r in req))
    return out


def scl_valid_mask(scl: np.ndarray, valid: tuple[int, ...] = SCL_VALID) -> np.ndarray:
    s = np.where(np.isnan(scl), 0, scl).astype("int16")
    return np.isin(s, valid)


def median_composite(stack: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Per-pixel NaN-median over axis 0 and number of valid observations."""
    count = np.sum(~np.isnan(stack), axis=0).astype("uint16")
    with np.errstate(all="ignore"):
        import warnings

        with warnings.catch_warnings():
            warnings.simplefilter("ignore", RuntimeWarning)
            med = np.nanmedian(stack, axis=0).astype("float32")
    return med, count


# ------------------------------------------------------- Sentinel-2 adapter
def _band_scale_offset(asset: dict[str, Any]) -> tuple[float, float, float | None]:
    rb = (asset.get("raster:bands") or [{}])[0]
    return float(rb.get("scale", 1.0)), float(rb.get("offset", 0.0)), rb.get("nodata", 0)


def search_sentinel2(d: dict[str, Any], bbox_wgs84: tuple[float, float, float, float], start: str, end: str,
                     max_cloud: float) -> list[dict[str, Any]]:
    from pystac_client import Client

    client = Client.open(d["urls"][0])
    search = client.search(collections=[d["collection"]], bbox=bbox_wgs84, datetime=f"{start}/{end}",
                           query={"eo:cloud_cover": {"lt": max_cloud}}, max_items=500)
    return [it.to_dict() for it in search.items()]


def select_items(items: list[dict[str, Any]], per_tile: int) -> list[dict[str, Any]]:
    """Keep the ``per_tile`` lowest-cloud items for each MGRS tile (deterministic)."""
    by_tile: dict[str, list[dict[str, Any]]] = {}
    for it in items:
        tile = it["properties"].get("grid:code") or it["properties"].get("s2:mgrs_tile") or it["id"].split("_")[1]
        by_tile.setdefault(tile, []).append(it)
    sel = []
    for tile, lst in sorted(by_tile.items()):
        lst.sort(key=lambda x: (x["properties"].get("eo:cloud_cover", 100), x["properties"]["datetime"]))
        sel.extend(lst[:per_tile])
    return sel


def download_sentinel2(catalog, d: dict[str, Any], out: Path, grid: Grid | None = None, bbox_wgs84=None,
                       start: str = "2025-01-01", end: str = "2025-03-31", max_cloud: float = 30.0,
                       per_tile: int = 3, workers: int = 4, **_: Any):
    """Search Earth Search, then read each selected item's bands *directly onto
    the analysis grid* (windowed COG reads over HTTPS, no full-scene download).

    Saved per item: float32 reflectance stack (6 bands) + SCL, nodata NaN.
    """
    if grid is None or bbox_wgs84 is None:
        raise ValueError("download_sentinel2 needs grid and bbox_wgs84 (use `earthsim remote-sensing acquire`)")
    items = search_sentinel2(d, bbox_wgs84, start, end, max_cloud)
    sel = select_items(items, per_tile)
    (out / "stac_search_items.json").write_text(json.dumps(items))
    files = []

    def one(it: dict[str, Any]) -> dict[str, Any]:
        import rasterio

        from .geo import write_raster

        dest = out / f"{it['id']}_grid.tif"
        log: list[dict[str, Any]] = []
        arrs = []
        with rasterio.Env(GDAL_DISABLE_READDIR_ON_OPEN="EMPTY_DIR", GDAL_HTTP_MAX_RETRY="4",
                          GDAL_HTTP_RETRY_DELAY="2", CPL_VSIL_CURL_ALLOWED_EXTENSIONS=".tif"):
            for b in S2_BANDS:
                a = it["assets"][b]
                sc, off, nd = _band_scale_offset(a)
                arr = reproject_to_grid(a["href"], grid, "continuous", src_nodata=nd, log=log)
                arrs.append(arr * sc + off)
                log[-1].update(scale=sc, offset=off, nodata=nd)
            scl = reproject_to_grid(it["assets"]["scl"]["href"], grid, "categorical", src_nodata=0, log=log)
        stack = np.stack(arrs + [scl]).astype("float32")
        write_raster(dest, stack, grid, nodata=np.nan, band_desc=S2_BANDS + ["scl"],
                     tags={"item_id": it["id"], "datetime": it["properties"]["datetime"],
                           "cloud_cover": it["properties"].get("eo:cloud_cover"),
                           "processing_baseline": it["properties"].get("s2:processing_baseline"),
                           "units": "surface reflectance (unitless); band 7 SCL class"})
        return {"path": str(dest), "item_id": it["id"], "datetime": it["properties"]["datetime"],
                "cloud_cover": it["properties"].get("eo:cloud_cover"),
                "processing_baseline": it["properties"].get("s2:processing_baseline"),
                "self_href": next((lk["href"] for lk in it.get("links", []) if lk["rel"] == "self"), None),
                "assets": {b: it["assets"][b]["href"] for b in S2_BANDS + ["scl"]},
                "resampling_log": log, "sha256": sha256_file(dest), "bytes": dest.stat().st_size,
                "origin": "download", "retrieved_utc": utcnow()}

    with cf.ThreadPoolExecutor(max_workers=workers) as ex:
        for r in ex.map(one, sel):
            files.append(r)
    extra = {"search": {"endpoint": d["urls"][0], "collection": d["collection"], "bbox": bbox_wgs84,
                        "datetime": f"{start}/{end}", "max_cloud": max_cloud, "items_found": len(items),
                        "items_selected": len(sel), "selection_rule": f"lowest eo:cloud_cover, {per_tile} per MGRS tile"},
             "grid": grid.to_dict()}
    return files, extra


def build_composite(item_files: list[str | Path], grid: Grid, aoi_mask_arr: np.ndarray | None = None) -> dict[str, Any]:
    """Median composite of SCL-valid observations + per-pixel valid count + QA."""
    import rasterio

    n = len(item_files)
    stacks = {b: np.full((n, *grid.shape), np.nan, dtype="float32") for b in S2_BANDS}
    scl_counts: dict[int, int] = {}
    per_item = []
    for i, f in enumerate(item_files):
        with rasterio.open(f) as src:
            a = src.read().astype("float32")
            tags = src.tags()
        scl = a[6]
        valid = scl_valid_mask(scl)
        observed = ~np.isnan(scl) & (scl > 0)
        u, c = np.unique(scl[observed].astype("int16"), return_counts=True)
        for k, v in zip(u, c, strict=True):
            scl_counts[int(k)] = scl_counts.get(int(k), 0) + int(v)
        refl_ok = np.all(a[:6] > 0, axis=0)  # reject non-positive reflectance (offset artefacts)
        use = valid & refl_ok
        for j, b in enumerate(S2_BANDS):
            stacks[b][i][use] = a[j][use]
        per_item.append({"item": tags.get("item_id"), "datetime": tags.get("datetime"),
                         "observed_px": int(observed.sum()), "valid_px": int(use.sum()),
                         "nonpositive_reflectance_px": int((valid & ~refl_ok).sum())})
    comp, counts = {}, None
    for b in S2_BANDS:
        comp[b], c = median_composite(stacks[b])
        counts = c if counts is None else np.minimum(counts, c)
        del stacks[b]
    qa = {"items": per_item, "scl_class_pixel_counts": {SCL_NAMES.get(k, k): v for k, v in sorted(scl_counts.items())}}
    if aoi_mask_arr is not None:
        inside = aoi_mask_arr
        qa["aoi_pixels"] = int(inside.sum())
        qa["aoi_pixels_with_valid_obs"] = int((counts[inside] > 0).sum())
        qa["aoi_valid_fraction"] = qa["aoi_pixels_with_valid_obs"] / max(1, qa["aoi_pixels"])
        vals, cnts = np.unique(counts[inside], return_counts=True)
        qa["valid_obs_histogram"] = {int(v): int(k) for v, k in zip(vals, cnts, strict=True)}
    return {"bands": comp, "valid_count": counts, "qa": qa}
