import math

import numpy as np
import pytest
import rasterio

from earthsim.geo import (GRID_LATTICE_M, GeoError, aoi_mask, grids_aligned, make_grid, read_aoi, read_raster,
                          reproject_to_grid, write_raster)
from tests.fixtures import synthetic as syn


def test_make_grid_snaps_to_shared_lattice():
    b = (556_451.3, 1_227_671.9, 627_233.0, 1_307_210.4)
    g20 = make_grid(b, "EPSG:32643", 20.0)
    g30 = make_grid(b, "EPSG:32643", 30.0)
    for g in (g20, g30):
        l, bb, r, t = g.bounds
        for v in (l, bb, r, t):
            assert abs(v / GRID_LATTICE_M - round(v / GRID_LATTICE_M)) < 1e-9
    # identical extents -> 20 m and 30 m grids nest exactly
    assert g20.bounds == g30.bounds
    assert g20.width * 20 == g30.width * 30


def test_window_of_larger_grid_is_aligned():
    big = make_grid((500_000, 1_180_000, 680_000, 1_360_000), "EPSG:32643", 30.0)
    small = make_grid((556_451.3, 1_227_671.9, 627_233.0, 1_307_210.4), "EPSG:32643", 30.0)
    assert grids_aligned(big, small)


def test_make_grid_rejects_geographic_crs_and_bad_res():
    with pytest.raises(GeoError):
        make_grid((75, 11, 76, 12), "EPSG:4326", 0.001)
    with pytest.raises(GeoError):
        make_grid((0, 0, 100, 100), "EPSG:32643", 0)


def test_write_read_roundtrip_preserves_georef_and_nodata(tmp_path):
    g = syn.grid()
    a = np.arange(g.width * g.height, dtype="float32").reshape(g.shape)
    a[0, 0] = np.nan
    p = write_raster(tmp_path / "x.tif", a, g)
    b, g2, tags = read_raster(p)
    assert g2.crs == g.crs and tuple(g2.transform)[:6] == tuple(g.transform)[:6]
    assert np.isnan(b[0, 0]) and np.allclose(b[1:], a[1:])
    with rasterio.open(p) as ds:
        assert math.isnan(ds.nodata)


def test_integer_nodata_declared(tmp_path):
    g = syn.grid()
    p = write_raster(tmp_path / "c.tif", np.ones(g.shape, "uint8"), g, nodata=0)
    with rasterio.open(p) as ds:
        assert ds.nodata == 0 and ds.dtypes[0] == "uint8"


def test_reproject_categorical_uses_mode_and_keeps_classes(tmp_path):
    g10 = syn.grid(res=10, w=60, h=60)
    cls = np.where(np.indices(g10.shape)[1] < 30, 10, 80).astype("float32")
    p = write_raster(tmp_path / "cls.tif", cls, g10, nodata=0)
    g30 = syn.grid(res=30, w=20, h=20)
    log = []
    out = reproject_to_grid(p, g30, "categorical", log=log)
    assert set(np.unique(out[np.isfinite(out)])) <= {10.0, 80.0}
    assert log[0]["resampling"] == "mode"


def test_reproject_continuous_average_conserves_mean(tmp_path):
    g10 = syn.grid(res=10, w=60, h=60)
    rng = np.random.default_rng(0)
    a = rng.random(g10.shape).astype("float32")
    p = write_raster(tmp_path / "a.tif", a, g10)
    out = reproject_to_grid(p, syn.grid(res=30, w=20, h=20), "continuous")
    assert abs(np.nanmean(out) - a.mean()) < 1e-4


def test_aoi_roundtrip_and_mask(tmp_path):
    g = syn.grid()
    aoi = read_aoi(syn.aoi_file(tmp_path, g))
    m = aoi_mask(aoi, g)
    assert m.sum() == (g.width - 6) * (g.height - 6)  # 90 m inset at 30 m = 3 px per side


def test_read_aoi_missing_file_actionable(tmp_path):
    with pytest.raises(GeoError, match="earthsim data download"):
        read_aoi(tmp_path / "nope.geojson")
