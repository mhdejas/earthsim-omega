"""Core geospatial operations: AOI handling, analysis grids, reprojection,
alignment, clipping, windowed I/O, tiling, zonal statistics, rasterization,
polygonization and metric measurements.

Rules enforced here:
* nodata is carried as an explicit mask / NaN and never silently treated as 0;
* resampling is chosen by data *type* (continuous vs categorical) not by habit;
* areas and distances are computed in a projected CRS and labelled in metres.
"""

from __future__ import annotations

import math
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

import geopandas as gpd
import numpy as np
import rasterio
from affine import Affine
from pyproj import CRS
from rasterio.enums import Resampling
from rasterio.features import geometry_mask, rasterize, shapes
from rasterio.warp import reproject, transform_bounds
from rasterio.windows import Window
from shapely import make_valid
from shapely.geometry import mapping, shape
from shapely.validation import explain_validity

DataKind = Literal["continuous", "categorical", "count", "mask"]

RESAMPLING_RULES: dict[str, dict[str, Resampling]] = {
    # downsampling (target coarser than source) / upsampling
    "continuous": {"down": Resampling.average, "up": Resampling.bilinear},
    "categorical": {"down": Resampling.mode, "up": Resampling.nearest},
    "count": {"down": Resampling.sum, "up": Resampling.nearest},
    "mask": {"down": Resampling.max, "up": Resampling.nearest},
}


class GeoError(ValueError):
    """Raised for actionable geospatial input problems."""


# --------------------------------------------------------------------------- AOI
@dataclass
class AOI:
    gdf: gpd.GeoDataFrame
    source: str
    repair_log: list[dict[str, Any]]

    @property
    def geometry(self):
        return self.gdf.geometry.union_all()

    def to_crs(self, crs: str) -> AOI:
        return AOI(self.gdf.to_crs(crs), self.source, self.repair_log)

    def area_km2(self, crs: str) -> float:
        g = self.gdf.to_crs(crs)
        if not CRS.from_user_input(crs).is_projected:
            raise GeoError("area requested in a geographic CRS; use a projected CRS (metres)")
        return float(g.geometry.area.sum() / 1e6)


def validate_geometry(gdf: gpd.GeoDataFrame) -> list[dict[str, Any]]:
    rep = []
    for i, g in enumerate(gdf.geometry):
        rep.append({
            "index": i,
            "type": None if g is None else g.geom_type,
            "is_empty": bool(g is None or g.is_empty),
            "is_valid": bool(g is not None and g.is_valid),
            "reason": "missing" if g is None else explain_validity(g),
        })
    return rep


def repair_geometries(gdf: gpd.GeoDataFrame) -> tuple[gpd.GeoDataFrame, list[dict[str, Any]]]:
    """Repair invalid geometries with shapely.make_valid (documented, controlled).

    Records before/after validity, area and WKT length so the change is auditable.
    Non-polygonal debris produced by make_valid is dropped and recorded.
    """
    log = []
    out = gdf.copy()
    for i, g in enumerate(gdf.geometry):
        if g is None or g.is_empty:
            raise GeoError(f"feature {i} has empty geometry; cannot use as AOI")
        if g.is_valid:
            continue
        fixed = make_valid(g)
        if fixed.geom_type == "GeometryCollection":
            polys = [p for p in fixed.geoms if p.geom_type in ("Polygon", "MultiPolygon")]
            from shapely.ops import unary_union

            fixed = unary_union(polys)
        log.append({
            "index": i, "method": "shapely.make_valid", "before_reason": explain_validity(g),
            "before_area_native": g.area, "after_area_native": fixed.area,
            "before_wkt_chars": len(g.wkt), "after_valid": fixed.is_valid,
        })
        out.iloc[i, out.columns.get_loc(out.geometry.name)] = fixed
    return out, log


def read_aoi(path: str | Path, name_field: str | None = None, name: str | None = None) -> AOI:
    path = Path(path)
    if not path.exists():
        raise GeoError(f"AOI file not found: {path}. Run `earthsim data download aoi_kozhikode_geoboundaries`.")
    gdf = gpd.read_file(path)
    if gdf.crs is None:
        raise GeoError(f"AOI {path} has no CRS; refusing to guess")
    if name_field and name:
        gdf = gdf[gdf[name_field].str.lower() == name.lower()]
    if gdf.empty:
        raise GeoError(f"AOI selection empty in {path} ({name_field}={name})")
    gdf, log = repair_geometries(gdf)
    if not gdf.geometry.geom_type.isin(["Polygon", "MultiPolygon"]).all():
        raise GeoError("AOI must be polygonal")
    return AOI(gdf, str(path), log)


# ------------------------------------------------------------------------- Grid
@dataclass(frozen=True)
class Grid:
    crs: str
    transform: Affine
    width: int
    height: int

    @property
    def res(self) -> float:
        return float(self.transform.a)

    @property
    def shape(self) -> tuple[int, int]:
        return (self.height, self.width)

    @property
    def bounds(self) -> tuple[float, float, float, float]:
        t = self.transform
        return (t.c, t.f + t.e * self.height, t.c + t.a * self.width, t.f)

    def profile(self, dtype: str = "float32", nodata: float | int | None = np.nan, count: int = 1) -> dict:
        return {
            "driver": "GTiff", "crs": self.crs, "transform": self.transform, "width": self.width,
            "height": self.height, "count": count, "dtype": dtype, "nodata": nodata,
            "tiled": True, "blockxsize": 256, "blockysize": 256, "compress": "deflate",
            "predictor": 3 if dtype.startswith("float") else 2, "BIGTIFF": "IF_SAFER",
        }

    def to_dict(self) -> dict[str, Any]:
        return {"crs": self.crs, "transform": list(self.transform)[:6], "width": self.width, "height": self.height,
                "res_m": self.res, "bounds": self.bounds}


GRID_LATTICE_M = 60.0  # LCM of 10/20/30 m: every EARTHSIM grid edge lies on this lattice


def make_grid(bounds: tuple[float, float, float, float], crs: str, res: float, buffer: float = 0.0,
              lattice: float | None = GRID_LATTICE_M) -> Grid:
    """Grid whose edges are snapped to a shared lattice (default 60 m).

    Because 10, 20 and 30 m all divide 60 m, any two EARTHSIM grids in the same
    CRS nest exactly (a 30 m grid is an exact window of a larger 30 m grid, and
    each 60 m block holds whole 20 m and 30 m cells). If ``res`` does not divide
    the lattice, the grid falls back to snapping on multiples of ``res``.
    """
    if res <= 0:
        raise GeoError("resolution must be positive")
    if not CRS.from_user_input(crs).is_projected:
        raise GeoError(f"make_grid requires a projected CRS, got {crs}")
    snap = lattice if lattice and abs(lattice / res - round(lattice / res)) < 1e-9 else res
    minx, miny, maxx, maxy = bounds
    minx = math.floor((minx - buffer) / snap) * snap
    miny = math.floor((miny - buffer) / snap) * snap
    maxx = math.ceil((maxx + buffer) / snap) * snap
    maxy = math.ceil((maxy + buffer) / snap) * snap
    w, h = int(round((maxx - minx) / res)), int(round((maxy - miny) / res))
    if w <= 0 or h <= 0:
        raise GeoError("empty grid")
    return Grid(crs, Affine(res, 0, minx, 0, -res, maxy), w, h)


def grid_for_aoi(aoi: AOI, crs: str, res: float, buffer: float = 0.0) -> Grid:
    return make_grid(tuple(aoi.to_crs(crs).gdf.total_bounds), crs, res, buffer)


def grids_aligned(a: Grid, b: Grid, tol: float = 1e-6) -> bool:
    if CRS.from_user_input(a.crs) != CRS.from_user_input(b.crs):
        return False
    if abs(a.transform.a - b.transform.a) > tol or abs(a.transform.e - b.transform.e) > tol:
        return False
    dx = (a.transform.c - b.transform.c) / a.transform.a
    dy = (a.transform.f - b.transform.f) / a.transform.e
    return abs(dx - round(dx)) < tol and abs(dy - round(dy)) < tol


def choose_resampling(kind: DataKind, src_res: float, dst_res: float) -> Resampling:
    return RESAMPLING_RULES[kind]["down" if dst_res > src_res * 1.0001 else "up"]


def src_res_in_dst_units(src: rasterio.io.DatasetReader, dst_crs: str) -> float:
    """Approximate source pixel size in metres of the destination CRS."""
    if src.crs.is_projected:
        return float(abs(src.res[0]))
    l, b, r, t = transform_bounds(src.crs, dst_crs, *src.bounds)
    return float((r - l) / src.width)


def reproject_to_grid(
    sources: list[str | Path] | str | Path,
    grid: Grid,
    kind: DataKind,
    band: int = 1,
    src_nodata: float | None = None,
    scale: float = 1.0,
    offset: float = 0.0,
    log: list | None = None,
    open_options: dict[str, Any] | None = None,
) -> np.ndarray:
    """Reproject one band from one or many (mosaicked, first-valid) sources onto ``grid``.

    Returns float32 with NaN for nodata (continuous) or the source dtype-safe
    float32 with NaN for categorical data. Records the resampling used in ``log``.
    ``open_options`` go to rasterio.open (e.g. {"OVERVIEW_LEVEL": 0} reads a
    COG's first internal overview) and are recorded in the log.
    """
    if isinstance(sources, (str, Path)):
        sources = [sources]
    out = np.full(grid.shape, np.nan, dtype="float32")
    for s in sources:
        with rasterio.open(s, **(open_options or {})) as src:
            nd = src_nodata if src_nodata is not None else src.nodata
            rs = choose_resampling(kind, src_res_in_dst_units(src, grid.crs), grid.res)
            tmp = np.full(grid.shape, np.nan, dtype="float32")
            reproject(
                source=rasterio.band(src, band), destination=tmp, src_nodata=nd, dst_nodata=np.nan,
                dst_transform=grid.transform, dst_crs=grid.crs, resampling=rs,
            )
            fill = np.isnan(out) & ~np.isnan(tmp)
            out[fill] = tmp[fill]
            if log is not None:
                log.append({"source": str(s), "band": band, "resampling": rs.name, "kind": kind,
                            "src_crs": str(src.crs), "src_res": src.res, "dst_res_m": grid.res,
                            "open_options": open_options or {}})
    if scale != 1.0 or offset != 0.0:
        out = out * scale + offset
    return out


def aoi_mask(aoi: AOI, grid: Grid, all_touched: bool = False) -> np.ndarray:
    """True inside the AOI (pixel centre rule unless all_touched)."""
    g = aoi.to_crs(grid.crs).gdf
    return ~geometry_mask(g.geometry, out_shape=grid.shape, transform=grid.transform, all_touched=all_touched)


def write_raster(path: str | Path, arr: np.ndarray, grid: Grid, nodata=np.nan, tags: dict | None = None,
                 band_desc: list[str] | None = None, cog: bool = True) -> Path:
    """Write a georeferenced GeoTIFF (COG layout when ``cog``) atomically."""
    import os
    import tempfile

    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    a = arr[None] if arr.ndim == 2 else arr
    dtype = str(a.dtype)
    if dtype == "bool":
        a = a.astype("uint8")
        dtype = "uint8"
    if dtype.startswith("float") and nodata is not None and not (isinstance(nodata, float) and math.isnan(nodata)):
        a = np.where(np.isnan(a), nodata, a)
    prof = grid.profile(dtype=dtype, nodata=nodata, count=a.shape[0])
    fd, tmp = tempfile.mkstemp(dir=path.parent, suffix=".tif", prefix=".tmp_")
    os.close(fd)
    if cog:
        prof = {k: v for k, v in prof.items() if k not in ("tiled", "blockxsize", "blockysize")}
        prof.update(driver="COG", blocksize=256, overview_resampling="nearest" if not dtype.startswith("float") else "average")
        prof.pop("predictor", None)
        prof["predictor"] = "YES"
    with rasterio.open(tmp, "w", **prof) as dst:
        dst.write(a)
        if tags:
            dst.update_tags(**{k: str(v) for k, v in tags.items()})
        if band_desc:
            for i, d in enumerate(band_desc, 1):
                dst.set_band_description(i, d)
    os.replace(tmp, path)
    return path


def read_raster(path: str | Path, band: int = 1) -> tuple[np.ndarray, Grid, dict]:
    with rasterio.open(path) as src:
        a = src.read(band, masked=True).astype("float32").filled(np.nan)
        g = Grid(src.crs.to_string(), src.transform, src.width, src.height)
        return a, g, src.tags()


# ------------------------------------------------------------- Windows & tiles
def iter_windows(width: int, height: int, size: int, overlap: int = 0) -> Iterator[tuple[Window, Window]]:
    """Yield (read_window, core_window) pairs.

    ``read_window`` includes ``overlap`` pixels of halo (clipped at raster edges);
    ``core_window`` is the non-overlapping part to write. Core windows tile the
    raster exactly once, which is what tests verify.
    """
    if size <= 0 or overlap < 0:
        raise GeoError("invalid window size/overlap")
    for row in range(0, height, size):
        for col in range(0, width, size):
            cw = Window(col, row, min(size, width - col), min(size, height - row))
            r0, c0 = max(0, row - overlap), max(0, col - overlap)
            r1, c1 = min(height, row + cw.height + overlap), min(width, col + cw.width + overlap)
            yield Window(c0, r0, c1 - c0, r1 - r0), cw


def tile_origins(n: int, tile: int, stride: int) -> list[int]:
    """Origins covering [0, n) with given stride; last tile is flush with the edge."""
    if n <= tile:
        return [0]
    o = list(range(0, n - tile + 1, stride))
    if o[-1] != n - tile:
        o.append(n - tile)
    return o


# ------------------------------------------------------- Zonal & vector tools
def zonal_stats(values: np.ndarray, zones: np.ndarray, zone_ids: list[int] | None = None,
                pixel_area_m2: float | None = None) -> dict[int, dict[str, float]]:
    """NaN-aware zonal statistics. Zones <= 0 or NaN are ignored."""
    if values.shape != zones.shape:
        raise GeoError(f"shape mismatch values{values.shape} vs zones{zones.shape}")
    z = np.where(np.isnan(zones.astype("float64")), -1, zones).astype("int64")
    ids = zone_ids if zone_ids is not None else [int(i) for i in np.unique(z) if i > 0]
    out = {}
    for i in ids:
        m = z == i
        v = values[m]
        valid = v[~np.isnan(v)]
        rec = {"pixels": int(m.sum()), "valid": int(valid.size),
               "mean": float(valid.mean()) if valid.size else float("nan"),
               "min": float(valid.min()) if valid.size else float("nan"),
               "max": float(valid.max()) if valid.size else float("nan"),
               "sum": float(valid.sum()) if valid.size else 0.0}
        if pixel_area_m2:
            rec["area_km2"] = m.sum() * pixel_area_m2 / 1e6
        out[i] = rec
    return out


def rasterize_vector(gdf: gpd.GeoDataFrame, grid: Grid, value_field: str | None = None, fill: float = 0,
                     all_touched: bool = False, dtype: str = "float32") -> np.ndarray:
    if gdf.empty:
        return np.full(grid.shape, fill, dtype=dtype)
    g = gdf.to_crs(grid.crs)
    vals = g[value_field] if value_field else [1] * len(g)
    return rasterize(zip(g.geometry, vals, strict=True), out_shape=grid.shape, transform=grid.transform,
                     fill=fill, all_touched=all_touched, dtype=dtype)


def polygonize(mask: np.ndarray, grid: Grid, value: int = 1) -> gpd.GeoDataFrame:
    m = mask.astype("uint8")
    geoms = [shape(s) for s, v in shapes(m, mask=m == value, transform=grid.transform) if v == value]
    return gpd.GeoDataFrame({"value": [value] * len(geoms)}, geometry=geoms, crs=grid.crs)


def safe_intersection(a: gpd.GeoDataFrame, b: gpd.GeoDataFrame) -> gpd.GeoDataFrame:
    if a.crs is None or b.crs is None:
        raise GeoError("both layers need a CRS")
    if CRS.from_user_input(a.crs) != CRS.from_user_input(b.crs):
        raise GeoError(f"CRS mismatch {a.crs} vs {b.crs}; reproject deliberately first")
    if a.empty or b.empty:
        return a.iloc[0:0].copy()
    return gpd.overlay(a, b, how="intersection", keep_geom_type=True)


def length_km(gdf: gpd.GeoDataFrame, crs: str) -> float:
    if not CRS.from_user_input(crs).is_projected:
        raise GeoError("length in geographic CRS would be in degrees; use projected CRS")
    return float(gdf.to_crs(crs).geometry.length.sum() / 1000)


def geojson_mapping(geom) -> dict:
    return mapping(geom)
