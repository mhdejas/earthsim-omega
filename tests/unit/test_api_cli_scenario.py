"""API / CLI / scenario tests on synthetic fixtures (offline, no real data needed)."""

from __future__ import annotations

import json
import time

import numpy as np
import pytest
from click.testing import CliRunner

from earthsim.config import load_settings
from tests.fixtures.synthetic import grid, tilted_plane, write_fixture_raster


@pytest.fixture()
def settings(tmp_path):
    proc = tmp_path / "processed"
    (proc / "terrain").mkdir(parents=True)
    g = grid()
    write_fixture_raster(proc / "terrain", "hand.tif", tilted_plane(), g)
    return load_settings(overrides={"paths": {"processed_dir": str(proc), "jobs_db": str(tmp_path / "jobs.sqlite"),
                                              "runs_dir": str(tmp_path / "runs")}})


@pytest.fixture()
def client(settings):
    from fastapi.testclient import TestClient

    from earthsim.api import create_app

    return TestClient(create_app(settings))


def test_health(client):
    r = client.get("/api/health")
    assert r.status_code == 200 and r.json()["status"] == "ok"


def test_layers_registry_marks_missing_and_available(client):
    layers = {x["id"]: x for x in client.get("/api/layers").json()}
    assert layers["hand"]["available"] and len(layers["hand"]["bounds"]) == 2
    assert not layers["ndvi"]["available"]
    # every layer declares its data class (observation / model-derived / simulation)
    assert all(x["data_class"] in {"observation", "observation-derived", "model-derived", "simulation"}
               for x in layers.values())


def test_layer_png_and_missing_layer(client):
    r = client.get("/api/layers/hand.png")
    assert r.status_code == 200 and r.content[:8] == b"\x89PNG\r\n\x1a\n"
    assert client.get("/api/layers/ndvi.png").status_code == 404
    assert client.get("/api/layers/nope.png").status_code == 404


def test_point_query_reads_source_raster(client, settings):
    from rasterio.warp import transform

    g = grid()
    x, y = g.transform @ (5.5, 7.5)  # centre of row 7, col 5
    lon, lat = transform(g.crs, "EPSG:4326", [x], [y])
    v = client.get(f"/api/layers/hand/value?lon={lon[0]}&lat={lat[0]}").json()
    assert v["value"] == pytest.approx(tilted_plane()[7, 5], abs=1e-4)
    assert client.get("/api/layers/hand/value?lon=500&lat=0").status_code == 422


def test_jobs_validation_and_lifecycle(client, monkeypatch):
    import earthsim.api as api

    assert client.post("/api/jobs", json={"stage": "rm -rf /"}).status_code == 422
    monkeypatch.setattr(api, "_stage_registry", lambda: {"ok": lambda s: {"x": 1},
                                                         "boom": lambda s: (_ for _ in ()).throw(ValueError("bad"))})
    a = client.post("/api/jobs", json={"stage": "ok"}).json()["id"]
    b = client.post("/api/jobs", json={"stage": "boom"}).json()["id"]
    for _ in range(50):
        ja, jb = client.get(f"/api/jobs/{a}").json(), client.get(f"/api/jobs/{b}").json()
        if ja["status"] not in ("queued", "running") and jb["status"] not in ("queued", "running"):
            break
        time.sleep(0.05)
    assert ja["status"] == "succeeded" and json.loads(ja["result"]) == {"x": 1}
    assert jb["status"] == "failed" and "ValueError: bad" in jb["error"]
    assert client.get("/api/jobs/doesnotexist").status_code == 404


def test_request_body_cap(client):
    r = client.post("/api/jobs", content=b"x" * 2_000_000, headers={"content-type": "application/json"})
    assert r.status_code == 413


def test_jobs_marked_failed_after_restart(settings):
    from earthsim.api import JobStore

    js = JobStore(settings.p.jobs_db)
    with js._c() as c:
        c.execute("insert into jobs values ('x1','terrain','running',0,0,null,null,null)")
    js2 = JobStore(settings.p.jobs_db)
    assert js2.get("x1")["status"] == "failed"


def test_cli_validates_config_and_args():
    from earthsim.cli import main

    r = CliRunner().invoke(main, ["--set", "compute.workers=0", "data", "list"])
    assert r.exit_code == 2 and "invalid configuration" in r.output
    r = CliRunner().invoke(main, ["run", "hindcast", "--runoff-c", "1.5"])
    assert r.exit_code == 2 and "runoff-c" in r.output
    r = CliRunner().invoke(main, ["run", "not_a_stage"])
    assert r.exit_code != 0


def test_channel_preserving_aggregation(tmp_path):
    """A narrow gorge through a ridge must stay open after coarsening (block-mean would dam it)."""
    from earthsim.scenario import coarse_terrain

    h, w = 64, 64
    g = grid(w=w, h=h)
    rr, cc = np.indices((h, w))
    z = 5.0 + 0.5 * cc  # drains west
    z[:, 30:34] += 60.0  # ridge
    z[31:33, :] = 2.0 + 0.2 * cc[31:33, :]  # 2-cell gorge through the ridge
    ocean = np.zeros((h, w), "float32")
    ocean[:, 0] = 1
    streams = np.zeros((h, w), "float32")
    streams[31:33, :] = 1
    for name, a in (("dem.tif", z), ("ocean_mask.tif", ocean), ("streams.tif", streams)):
        write_fixture_raster(tmp_path, name, a, g)
    ct = coarse_terrain(tmp_path, 8)
    gorge_row = 31 // 8
    raw = ct["z_raw"][gorge_row]
    assert raw[30 // 8] < 20, "gorge cell should keep the thalweg elevation"
    assert ct["fill_depth_max_m"] < 1.0
