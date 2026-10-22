import json

import numpy as np
import pytest

from earthsim import landcover as lc
from earthsim import sar
from earthsim.remote_sensing import compute_indices, median_composite, ndvi, normalized_difference, scl_valid_mask


# ---------------------------------------------------------------- indices
def test_ndvi_known_values():
    red = np.array([0.1, 0.05, 0.0, 0.2], "float32")
    nir = np.array([0.5, 0.05, 0.0, 0.1], "float32")
    out = ndvi(red, nir)
    assert np.isclose(out[0], (0.5 - 0.1) / 0.6)
    assert out[1] == 0
    assert np.isnan(out[2])  # 0/0
    assert np.isclose(out[3], -1 / 3)


def test_normalized_difference_rejects_negative_and_nan():
    out = normalized_difference(np.array([-0.1, np.nan, 0.3]), np.array([0.2, 0.2, 0.1]))
    assert np.isnan(out[0]) and np.isnan(out[1]) and np.isclose(out[2], 0.5)


def test_indices_bounded_for_valid_reflectance():
    rng = np.random.default_rng(1)
    b = {k: rng.random(1000).astype("float32") for k in ("blue", "green", "red", "nir", "swir16", "swir22")}
    for v in compute_indices(b).values():
        assert np.nanmin(v) >= -1 and np.nanmax(v) <= 1


def test_compute_indices_missing_band_is_actionable():
    with pytest.raises(KeyError, match="needs bands"):
        compute_indices({"red": np.zeros(3)}, ["ndvi"])


def test_scl_mask_and_median_composite():
    scl = np.array([4, 5, 6, 8, 9, 3, np.nan])
    assert scl_valid_mask(scl).tolist() == [True, True, True, False, False, False, False]
    stack = np.array([[1.0, np.nan], [3.0, np.nan], [2.0, 5.0]])
    med, cnt = median_composite(stack)
    assert med.tolist() == [2.0, 5.0] and cnt.tolist() == [3, 1]


# -------------------------------------------------------------------- SAR
def test_to_db_and_boxcar():
    assert np.allclose(sar.to_db(np.array([1.0, 0.1, 0.01])), [0, -10, -20])
    a = np.ones((5, 5), "float32")
    a[2, 2] = np.nan
    assert np.allclose(sar.boxcar(a)[np.isfinite(sar.boxcar(a))], 1.0)


def test_otsu_separates_bimodal_backscatter():
    rng = np.random.default_rng(0)
    x = np.concatenate([rng.normal(-24, 1.5, 5000), rng.normal(-8, 2, 20000)])
    t = sar.otsu_threshold(x, lo=-30, hi=0)
    assert -20 < t < -12


def test_detect_flood_requires_drop_and_excludes_permanent_water():
    post = np.array([-22.0, -18.5, -10.0, -22.0])
    ref = np.array([-10.0, -16.5, -10.0, -24.0])  # land->water, dark but drop < 3 dB, unchanged land, permanent water
    r = sar.detect_flood(post, ref, np.ones(4, bool), threshold=-18.0)
    assert r["flood"].tolist() == [True, False, False, False]
    assert r["permanent_water"].tolist() == [False, False, False, True]


# -------------------------------------------------------------- landcover
def test_block_ids_group_neighbours():
    r = np.array([0, 10, 260, 0])
    c = np.array([0, 10, 0, 260])
    g = lc.block_ids(r, c, 250, 1000)
    assert g[0] == g[1] and len({g[0], g[2], g[3]}) == 3


def test_metrics_perfect_and_majority():
    y = np.array([10, 10, 80, 50])
    m = lc.metrics(y, y, [10, 50, 80])
    assert m["overall_accuracy"] == 1 and m["kappa"] == 1
    m2 = lc.metrics(y, np.full(4, 10), [10, 50, 80])
    assert m2["overall_accuracy"] == 0.5 and abs(m2["kappa"]) < 1e-9


# ------------------------------------------------------- config/provenance
def test_config_rejects_unknown_key_and_geographic_crs():
    from pydantic import ValidationError

    from earthsim.config import load_settings

    with pytest.raises(ValidationError):
        load_settings(overrides={"processing": {"no_such_key": 1}})
    with pytest.raises(ValidationError):
        load_settings(overrides={"region": {"analysis_crs": "EPSG:4326"}})


def test_runrecord_and_secret_redaction(tmp_path, monkeypatch):
    from earthsim.provenance import RunRecord, redact_env

    monkeypatch.setenv("EARTHSIM_TEST_API_KEY", "supersecret")
    env = redact_env()
    assert "supersecret" not in json.dumps(env)
    f = tmp_path / "in.txt"
    f.write_text("x")
    rr = RunRecord("unit", {"a": 1})
    rr.add_input(f, "fixture")
    rr.finish(metric=1.0)
    p = rr.write(tmp_path / "prov.json")
    d = json.loads(p.read_text())
    assert d["inputs"][0]["sha256"] and d["status"] == "succeeded"
