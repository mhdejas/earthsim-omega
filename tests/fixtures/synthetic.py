"""Small SYNTHETIC fixtures for offline tests.

Everything here is generated in-memory or in pytest tmp dirs and is labelled
synthetic in tags. Nothing here is, or may be presented as, real imagery.
"""

from __future__ import annotations

from pathlib import Path

import geopandas as gpd
import numpy as np
from affine import Affine
from shapely.geometry import box

from earthsim.geo import Grid, write_raster

CRS = "EPSG:32643"


def grid(res: float = 30.0, w: int = 40, h: int = 30, x0: float = 600_000.0, y0: float = 1_260_000.0) -> Grid:
    return Grid(CRS, Affine(res, 0, x0, 0, -res, y0), w, h)


def tilted_plane(h: int = 30, w: int = 40, slope_x: float = 0.5, slope_y: float = 0.2) -> np.ndarray:
    """Elevation decreasing toward column 0 (west) and row h-1 (south)."""
    rr, cc = np.indices((h, w))
    return (10.0 + slope_x * cc + slope_y * (h - 1 - rr)).astype("float64")


def v_valley(h: int = 31, w: int = 31, side: float = 1.0, down: float = 0.1) -> np.ndarray:
    """V-shaped valley along the centre column draining to row h-1."""
    rr, cc = np.indices((h, w))
    return (side * np.abs(cc - w // 2) + down * (h - 1 - rr) + 5.0).astype("float64")


def with_pit(z: np.ndarray, r: int, c: int, depth: float = 3.0) -> np.ndarray:
    z = z.copy()
    z[r, c] -= depth
    return z


def write_fixture_raster(tmp: Path, name: str, arr: np.ndarray, g: Grid, nodata=np.nan) -> Path:
    return write_raster(tmp / name, arr, g, nodata=nodata, tags={"data_class": "SYNTHETIC test fixture"})


def aoi_file(tmp: Path, g: Grid, inset: float = 90.0) -> Path:
    l, b, r, t = g.bounds
    gdf = gpd.GeoDataFrame({"shapeName": ["Synthetic"]}, geometry=[box(l + inset, b + inset, r - inset, t - inset)],
                           crs=CRS).to_crs("EPSG:4326")
    p = tmp / "aoi.geojson"
    gdf.to_file(p, driver="GeoJSON")
    return p
