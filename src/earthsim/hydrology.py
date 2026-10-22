"""Terrain & drainage preprocessing (flood LAYER 1).

Algorithms (all from the established ``pysheds`` library, v0.5, MIT licence):
* ``fill_pits`` - single-cell pits raised to lowest neighbour;
* ``fill_depressions`` - morphological reconstruction (Soille 2004) priority fill;
* ``resolve_flats`` - Garbrecht & Martz (1997) flat-surface gradients;
* ``flowdir`` D8 (ESRI encoding 64,128,1,2,4,8,16,32), ``accumulation``,
  ``compute_hand`` (Height Above Nearest Drainage, Nobre et al. 2011).

DEM caveat: Copernicus GLO-30 is a *surface* model (DSM). In Kozhikode's dense
coconut/areca canopy and urban fabric the surface is metres above bare earth,
biasing slope, HAND and flow paths. This is a documented limitation, not fixed.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import numpy as np
from scipy import ndimage

from .geo import Grid, write_raster

# Compatibility shim: pysheds 0.5 calls ``np.in1d`` which NumPy 2.x removed.
# ``in1d(a, b)`` is defined as ``isin(a, b).ravel()`` - numerically identical.
# Verified by tests/unit/test_hydrology.py::test_in1d_shim.
if not hasattr(np, "in1d"):
    np.in1d = lambda ar1, ar2, assume_unique=False, invert=False: np.isin(  # type: ignore[attr-defined]
        ar1, ar2, assume_unique=assume_unique, invert=invert).ravel()

D8_DIRMAP = (64, 128, 1, 2, 4, 8, 16, 32)  # N, NE, E, SE, S, SW, W, NW (ESRI)


def ocean_mask(dem: np.ndarray, sea_level: float = 0.0) -> np.ndarray:
    """Cells at/below ``sea_level`` that are 8-connected to the raster border.

    Copernicus DEM sets ocean to 0 m via its water-body mask. Inland cells at or
    below 0 m (e.g. reclaimed kole/paddy land) are NOT marked ocean because they
    are not border-connected.
    """
    low = np.isfinite(dem) & (dem <= sea_level)
    lab, _ = ndimage.label(low, structure=np.ones((3, 3)))
    border = np.unique(np.concatenate([lab[0], lab[-1], lab[:, 0], lab[:, -1]]))
    border = border[border > 0]
    return np.isin(lab, border)


def slope_degrees(dem: np.ndarray, res: float) -> np.ndarray:
    """Slope from central differences (Horn-equivalent for smooth fields), degrees."""
    z = dem.astype("float64")
    gy, gx = np.gradient(z, res, res)
    return np.degrees(np.arctan(np.hypot(gx, gy))).astype("float32")


def dem_quality(dem: np.ndarray, res: float) -> dict[str, Any]:
    v = dem[np.isfinite(dem)]
    return {"valid_px": int(v.size), "nan_px": int((~np.isfinite(dem)).sum()), "min_m": float(v.min()),
            "max_m": float(v.max()), "mean_m": float(v.mean()), "res_m": res,
            "zero_or_below_px": int((v <= 0).sum()),
            "vertical_datum": "EGM2008 geoid (Copernicus DEM spec)", "units": "m", "surface_type": "DSM"}


def pysheds_crosscheck(dem: np.ndarray, ocean: np.ndarray, grid: Grid, workdir: Path) -> dict[str, Any]:
    """Run pysheds 0.5 conditioning (float32) and count unresolved flats/pits on land.

    Kept as an independent comparison; on Kozhikode it leaves inland sinks on
    main channels, which is why EARTHSIM uses Priority-Flood+eps (hydro_core).
    """
    from pysheds.grid import Grid as PGrid

    workdir.mkdir(parents=True, exist_ok=True)
    z = dem.copy()
    z[ocean] = -1.0
    tmp = write_raster(workdir / "_dem_for_routing.tif", z.astype("float32"), grid, nodata=-9999.0, cog=False)
    pg = PGrid.from_raster(str(tmp))
    r = pg.read_raster(str(tmp))
    inflated = pg.resolve_flats(pg.fill_depressions(pg.fill_pits(r)))
    fdir = np.asarray(pg.flowdir(inflated, dirmap=D8_DIRMAP))
    land = ~ocean
    tmp.unlink(missing_ok=True)
    return {"library": "pysheds 0.5", "input_dtype": "float32",
            "unresolved_flat_cells_on_land": int(((fdir == -1) & land).sum()),
            "unresolved_pit_cells_on_land": int(((fdir == -2) & land).sum()),
            "verdict": "rejected: leaves inland terminal cells" if ((fdir < 0) & land).any() else "ok"}


def condition_and_route(dem: np.ndarray, grid: Grid, workdir: Path, crosscheck: bool = True) -> dict[str, Any]:
    """Priority-Flood+eps conditioning (float64) + D8 steepest descent + accumulation."""
    from . import hydro_core as hc

    ocean = ocean_mask(dem)
    if not np.all(np.isfinite(dem[~ocean])):
        raise ValueError(f"{int((~np.isfinite(dem) & ~ocean).sum())} land cells lack elevation; extend DEM coverage")
    zc, seed, raised = hc.priority_flood_epsilon(dem, ocean)
    terminal = ocean | seed
    fd = hc.d8_flowdir(zc, terminal, grid.res)
    land = ~ocean
    no_out = (fd == 0) & land & ~seed
    acc = hc.accumulation(zc, fd)
    fill = (zc - dem.astype("float64"))
    filled = fill > 1e-3
    diag = {
        "algorithm": "Priority-Flood+epsilon (Barnes et al. 2014), float64; D8 steepest descent; "
                     "accumulation in descending elevation order",
        "cells_raised": raised, "depression_filled_px_gt_1mm": int((filled & land).sum()),
        "max_fill_depth_m": float(fill[land].max()),
        "mean_fill_depth_where_filled_m": float(fill[filled & land].mean()) if (filled & land).any() else 0.0,
        "ocean_px": int(ocean.sum()), "outlet_seed_px": int(seed.sum()),
        "land_cells_without_downslope_receiver": int(no_out.sum()),
    }
    if crosscheck:
        try:
            diag["pysheds_crosscheck"] = pysheds_crosscheck(dem, ocean, grid, workdir)
        except Exception as ex:  # crosscheck must never break the stage
            diag["pysheds_crosscheck"] = {"error": repr(ex)}
    return {"zc": zc, "fdir": fd, "acc": acc, "filled": zc.astype("float32"), "ocean": ocean, "seed": seed,
            "diag": diag}


def hand(route: dict, streams: np.ndarray) -> np.ndarray:
    from .hydro_core import hand_from_routing

    h, _ = hand_from_routing(route["zc"], route["fdir"], streams | route["ocean"])
    return h


def twi(acc_cells: np.ndarray, slope_deg: np.ndarray, res: float, min_slope_deg: float = 0.1) -> np.ndarray:
    """Topographic Wetness Index ln(a / tan b), a = specific catchment area (m^2/m)."""
    a = (acc_cells * res * res) / res
    b = np.radians(np.maximum(slope_deg, min_slope_deg))
    return np.log(a / np.tan(b)).astype("float32")


def distance_to(mask: np.ndarray, res: float) -> np.ndarray:
    """Euclidean distance (metres, projected grid) from each cell to nearest True cell."""
    return (ndimage.distance_transform_edt(~mask) * res).astype("float32")


def stream_order_free_summary(streams: np.ndarray, acc: np.ndarray, res: float) -> dict[str, Any]:
    return {"stream_px": int(streams.sum()), "stream_length_km_approx": float(streams.sum() * res / 1000),
            "max_catchment_km2": float(acc.max() * res * res / 1e6)}


_D8_OFFSETS = {64: (-1, 0), 128: (-1, 1), 1: (0, 1), 2: (1, 1), 4: (1, 0), 8: (1, -1), 16: (0, -1), 32: (-1, -1)}


def downstream_index(fdir: np.ndarray, terminal_mask: np.ndarray | None = None) -> tuple[np.ndarray, np.ndarray]:
    """Flat index of each cell's D8 receiver; self for terminals.

    Returns (next_idx, offgrid) where ``offgrid`` marks cells whose receiver
    would fall outside the raster (drainage leaves the modelled domain).
    """
    h, w = fdir.shape
    rr, cc = np.indices((h, w))
    nr, nc = rr.copy(), cc.copy()
    for code, (dr, dc) in _D8_OFFSETS.items():
        m = fdir == code
        nr[m] += dr
        nc[m] += dc
    off = (nr < 0) | (nr >= h) | (nc < 0) | (nc >= w)
    nr = np.where(off, rr, nr)
    nc = np.where(off, cc, nc)
    nxt = (nr * w + nc).ravel().astype("int64")
    if terminal_mask is not None:
        t = terminal_mask.ravel()
        nxt[t] = np.flatnonzero(t)
    return nxt, off


def label_terminals(nxt: np.ndarray, max_iter: int = 64) -> np.ndarray:
    """Terminal (outlet) flat index for every cell via pointer doubling (O(n log L))."""
    p = nxt.copy()
    for _ in range(max_iter):
        q = p[p]
        if np.array_equal(q, p):
            return p
        p = q
    raise RuntimeError("flow graph did not converge: cycle in flow directions?")


def basin_catalog(fdir: np.ndarray, ocean: np.ndarray, aoi: np.ndarray, dem: np.ndarray, grid: Grid,
                  min_aoi_km2: float = 1.0) -> tuple[np.ndarray, list[dict[str, Any]], dict[str, Any]]:
    """Label basins by outlet and test that every basin draining the AOI is complete.

    A basin is *truncated* if any of its land cells lies on the domain border
    (upstream area could exist beyond the DEM extent) or its outlet drains off-grid.
    """
    from pyproj import Transformer

    res = grid.res
    nxt, off = downstream_index(fdir, terminal_mask=ocean)
    term = label_terminals(nxt)
    h, w = fdir.shape
    labels = term.reshape(h, w)
    land = ~ocean
    cell_km2 = res * res / 1e6
    border = np.zeros((h, w), bool)
    border[0, :] = border[-1, :] = border[:, 0] = border[:, -1] = True
    lab_land = labels[land]
    uniq, inv = np.unique(lab_land, return_inverse=True)
    area = np.bincount(inv) * cell_km2
    in_aoi = np.bincount(inv, weights=aoi[land].astype(float)) * cell_km2
    on_border = np.bincount(inv, weights=border[land].astype(float))
    zmax = np.full(uniq.size, -np.inf)
    np.maximum.at(zmax, inv, np.nan_to_num(dem[land], nan=-np.inf))
    tf = Transformer.from_crs(grid.crs, "EPSG:4326", always_xy=True)
    offflat = off.ravel()
    oceanflat = ocean.ravel()
    rows = []
    for k in np.flatnonzero(in_aoi >= min_aoi_km2):
        t = int(uniq[k])
        r, c = divmod(t, w)
        x, y = grid.transform * (c + 0.5, r + 0.5)
        lon, lat = tf.transform(x, y)
        kind = ("ocean" if oceanflat[t] else "off_grid" if offflat[t]
                else "domain_edge" if border.ravel()[t] else "internal_sink")
        rows.append({"outlet_index": t, "outlet_lon": round(lon, 5), "outlet_lat": round(lat, 5),
                     "outlet_type": kind, "area_km2": round(float(area[k]), 2),
                     "area_in_district_km2": round(float(in_aoi[k]), 2),
                     "max_elev_m": round(float(zmax[k]), 1), "border_cells": int(on_border[k]),
                     "truncated": bool(on_border[k] > 0 or kind in ("off_grid", "domain_edge"))})
    rows.sort(key=lambda r: -r["area_km2"])
    summary = {
        "basins_draining_district": len(rows),
        "truncated_basins": sum(r["truncated"] for r in rows),
        "district_area_in_truncated_basins_km2": round(sum(r["area_in_district_km2"] for r in rows if r["truncated"]), 2),
        "district_area_in_basins_listed_km2": round(sum(r["area_in_district_km2"] for r in rows), 2),
        "internal_sink_basins": sum(r["outlet_type"] == "internal_sink" for r in rows),
        "total_upstream_area_km2": round(sum(r["area_km2"] for r in rows), 1),
    }
    return labels, rows, summary
