"""EARTHSIM-Omega HTTP API (FastAPI) + static web map.

Endpoints
  GET  /api/health                  liveness + versions
  GET  /api/layers                  layer registry (id, title, data_class, units, legend, WGS84 bounds)
  GET  /api/layers/{id}.png         layer rendered as a Web-Mercator-warped PNG overlay (cached)
  GET  /api/layers/{id}/value       ?lon=&lat= point query on the source raster (native CRS)
  GET  /api/metrics                 evaluation records (flood, land cover, terrain, hydraulics)
  GET  /api/provenance/{stage}      provenance JSON of a pipeline stage
  POST /api/jobs                    {"stage": "..."} run an allow-listed pipeline stage in the background
  GET  /api/jobs, /api/jobs/{id}    job status (persisted in SQLite)
Security posture (prototype): binds to 127.0.0.1 by default, no auth, stage names are
allow-listed (no arbitrary code/paths), request bodies capped, concurrency limited.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
import traceback
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import numpy as np

from pydantic import BaseModel, Field

from .config import Settings, load_settings


class JobReq(BaseModel):
    """Body of POST /api/jobs; ``stage`` must be one of the allow-listed pipeline stages."""

    stage: str = Field(..., max_length=64)

LAYERS: list[dict[str, Any]] = [
    {"id": "dem", "title": "Elevation (Copernicus GLO-30 DSM)", "path": "terrain/dem.tif", "data_class": "observation",
     "units": "m (EGM2008)", "cmap": "terrain", "vmin": 0, "vmax": 1800, "crop": True},
    {"id": "hand", "title": "Height Above Nearest Drainage", "path": "terrain/hand.tif", "data_class": "model-derived",
     "units": "m", "cmap": "viridis_r", "vmin": 0, "vmax": 30, "crop": True},
    {"id": "ndvi", "title": "NDVI, Sentinel-2 median Jan-Mar 2025", "path": "remote_sensing/ndvi.tif",
     "data_class": "observation-derived", "units": "-", "cmap": "RdYlGn", "vmin": -0.2, "vmax": 0.9},
    {"id": "landcover", "title": "Land cover 2025 (RF on Sentinel-2, WorldCover labels)",
     "path": "landcover/landcover_2025_rf.tif", "data_class": "model-derived", "units": "WorldCover class",
     "cmap": "worldcover"},
    {"id": "sar_2018_08_14", "title": "SAR observed flood 2018-08-14 (S1A orbit 63)",
     "path": "flood/s1_flood_observed_2018-08-14.tif", "data_class": "observation-derived", "units": "class",
     "cmap": "flood"},
    {"id": "sar_2019_08_10", "title": "SAR observed flood 2019-08-10 (S1B orbit 165, east only)",
     "path": "flood/s1_flood_observed_2019-08-10.tif", "data_class": "observation-derived", "units": "class",
     "cmap": "flood"},
    {"id": "susceptibility", "title": "Terrain flood-susceptibility score (not a probability)",
     "path": "flood/flood_susceptibility_score.tif", "data_class": "model-derived", "units": "score 0-1",
     "cmap": "YlGnBu", "vmin": 0, "vmax": 1},
    {"id": "hindcast_depth", "title": "Simulated depth at 2019-08-10 06:10 IST (ERA5-forced hindcast)",
     "path": "hydraulics/base_depth_at_overpass.tif", "data_class": "simulation", "units": "m", "cmap": "Blues",
     "vmin": 0.1, "vmax": 3, "mask_below": 0.1},
    {"id": "hindcast_hmax", "title": "Simulated max depth 2019-08-01..10 (hindcast)",
     "path": "hydraulics/base_depth_max.tif", "data_class": "simulation", "units": "m", "cmap": "Blues",
     "vmin": 0.1, "vmax": 3, "mask_below": 0.1},
]
WORLDCOVER = {10: "#006400", 20: "#ffbb22", 30: "#ffff4c", 40: "#f096ff", 50: "#fa0000", 60: "#b4b4b4",
              70: "#f0f0f0", 80: "#0064c8", 90: "#0096a0", 95: "#00cf75", 100: "#fae6a0"}
FLOOD = {1: None, 2: "#e6550d", 3: "#08519c"}


def _stage_registry() -> dict[str, Any]:
    from . import pipelines as P

    return {"terrain": P.stage_terrain, "s2_indices": P.stage_s2_indices, "landcover": P.stage_landcover,
            "flood_obs": P.stage_flood_obs, "flood_susceptibility": P.stage_flood_susceptibility,
            "hindcast_eval": P.stage_hindcast_eval}


class JobStore:
    def __init__(self, path: Path, max_workers: int = 2):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self.lock = threading.Lock()
        with self._c() as c:
            c.execute("create table if not exists jobs (id text primary key, stage text, status text, "
                      "created real, started real, finished real, result text, error text)")
            # jobs left 'running' by a crashed process are marked failed on restart
            c.execute("update jobs set status='failed', error='server restarted during run' where status in "
                      "('queued','running')")
        self.pool = ThreadPoolExecutor(max_workers=max_workers)

    def _c(self):
        return sqlite3.connect(self.path, timeout=30)

    def submit(self, stage: str, fn, s: Settings) -> str:
        jid = uuid.uuid4().hex[:12]
        with self.lock, self._c() as c:
            if c.execute("select count(*) from jobs where stage=? and status in ('queued','running')",
                         (stage,)).fetchone()[0]:
                raise RuntimeError(f"a '{stage}' job is already queued or running")
            c.execute("insert into jobs values (?,?,?,?,?,?,?,?)", (jid, stage, "queued", time.time(), None, None,
                                                                  None, None))

        def work():
            with self._c() as c:
                c.execute("update jobs set status='running', started=? where id=?", (time.time(), jid))
            try:
                r = fn(s)
                st, res, err = "succeeded", json.dumps(r, default=str)[:200_000], None
            except Exception as e:  # noqa: BLE001 - recorded, surfaced through the API
                st, res, err = "failed", None, f"{type(e).__name__}: {e}\n{traceback.format_exc()[-4000:]}"
            with self._c() as c:
                c.execute("update jobs set status=?, finished=?, result=?, error=? where id=?",
                          (st, time.time(), res, err, jid))

        self.pool.submit(work)
        return jid

    def get(self, jid: str | None = None):
        with self._c() as c:
            c.row_factory = sqlite3.Row
            if jid:
                r = c.execute("select * from jobs where id=?", (jid,)).fetchone()
                return dict(r) if r else None
            return [dict(r) for r in c.execute("select id,stage,status,created,started,finished,error from jobs "
                                               "order by created desc limit 50")]


def render_png(layer: dict[str, Any], processed: Path, cache: Path, s: Settings) -> tuple[Path, list]:
    """Warp a layer to EPSG:3857 and colour it; returns (png path, [[south, west], [north, east]])."""
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    import rasterio
    from PIL import Image
    from rasterio.warp import Resampling, calculate_default_transform, reproject, transform_bounds

    src_path = processed / layer["path"]
    if not src_path.exists():
        raise FileNotFoundError(f"layer '{layer['id']}' not produced yet ({layer['path']})")
    cache.mkdir(parents=True, exist_ok=True)
    png = cache / f"{layer['id']}.png"
    meta = cache / f"{layer['id']}.json"
    if png.exists() and meta.exists() and png.stat().st_mtime >= src_path.stat().st_mtime:
        return png, json.loads(meta.read_text())["bounds"]
    with rasterio.open(src_path) as ds:
        if layer.get("crop"):
            from .pipelines import district_grid, load_aoi  # noqa: F401
            from rasterio.windows import from_bounds

            g = district_grid(s)
            w = from_bounds(*g.bounds, transform=ds.transform).round_offsets().round_lengths()
            a = ds.read(1, window=w).astype("float32")
            tr = ds.window_transform(w)
            h, wd = a.shape
        else:
            a = ds.read(1).astype("float32")
            tr, h, wd = ds.transform, ds.height, ds.width
        if ds.nodata is not None and not np.isnan(ds.nodata):
            a[a == ds.nodata] = np.nan
        crs = ds.crs
    from rasterio.transform import array_bounds

    b = array_bounds(h, wd, tr)
    categorical = layer["cmap"] in ("worldcover", "flood")
    dst_t, dw, dh = calculate_default_transform(crs, "EPSG:3857", wd, h, *b)
    scale = max(1, int(max(dw, dh) / 2400))
    dw, dh = dw // scale, dh // scale
    dst_t = dst_t @ dst_t.scale(scale, scale)
    out = np.full((dh, dw), np.nan, "float32")
    reproject(a, out, src_transform=tr, src_crs=crs, dst_transform=dst_t, dst_crs="EPSG:3857", src_nodata=np.nan,
              dst_nodata=np.nan, resampling=Resampling.nearest if categorical else Resampling.bilinear)
    rgba = np.zeros((dh, dw, 4), "uint8")
    if categorical:
        table = WORLDCOVER if layer["cmap"] == "worldcover" else FLOOD
        for v, col in table.items():
            if col is None:
                continue
            m = out == v
            rgba[m, :3] = [int(col[i:i + 2], 16) for i in (1, 3, 5)]
            rgba[m, 3] = 210
    else:
        cm = plt.get_cmap(layer["cmap"])
        x = (out - layer["vmin"]) / (layer["vmax"] - layer["vmin"])
        c = (cm(np.clip(np.nan_to_num(x), 0, 1)) * 255).astype("uint8")
        rgba[..., :3] = c[..., :3]
        ok = np.isfinite(out)
        if "mask_below" in layer:
            ok &= out >= layer["mask_below"]
        rgba[..., 3] = np.where(ok, 200, 0)
    Image.fromarray(rgba, "RGBA").save(png, optimize=True)
    left, bottom, right, top = rasterio.transform.array_bounds(dh, dw, dst_t)
    w, s_, e, n = transform_bounds("EPSG:3857", "EPSG:4326", left, bottom, right, top)
    bounds = [[s_, w], [n, e]]
    meta.write_text(json.dumps({"bounds": bounds, "source": str(src_path)}))
    return png, bounds


def create_app(s: Settings | None = None):
    from fastapi import FastAPI, HTTPException, Request
    from fastapi.responses import FileResponse, JSONResponse
    from fastapi.staticfiles import StaticFiles
    s = s or load_settings()
    processed = s.p.processed_dir
    cache = processed / "web_cache"
    jobs = JobStore(s.p.jobs_db, max_workers=s.api.max_concurrent_jobs)
    app = FastAPI(title="EARTHSIM-Omega", version="0.1.0")

    @app.middleware("http")
    async def cap_body(request: Request, call_next):
        cl = request.headers.get("content-length")
        if cl and int(cl) > s.api.max_request_bytes:
            return JSONResponse({"detail": "request too large"}, status_code=413)
        return await call_next(request)

    @app.get("/api/health")
    def health():
        import rasterio

        return {"status": "ok", "version": "0.1.0", "rasterio": rasterio.__version__,
                "gdal": rasterio.__gdal_version__, "region": s.region.name}

    @app.get("/api/layers")
    def layers():
        out = []
        for L in LAYERS:
            p = processed / L["path"]
            item = {k: L[k] for k in ("id", "title", "data_class", "units", "cmap")} | {"available": p.exists()}
            if p.exists():
                try:
                    _, item["bounds"] = render_png(L, processed, cache, s)
                except Exception as e:  # noqa: BLE001
                    item["error"] = str(e)
            out.append(item)
        return out

    def _layer(lid: str):
        for L in LAYERS:
            if L["id"] == lid:
                return L
        raise HTTPException(404, f"unknown layer {lid}")

    @app.get("/api/layers/{lid}.png")
    def layer_png(lid: str):
        try:
            png, _ = render_png(_layer(lid), processed, cache, s)
        except FileNotFoundError as e:
            raise HTTPException(404, str(e)) from e
        return FileResponse(png, media_type="image/png")

    @app.get("/api/layers/{lid}/value")
    def layer_value(lid: str, lon: float, lat: float):
        import rasterio
        from rasterio.warp import transform

        if not (-180 <= lon <= 180 and -90 <= lat <= 90):
            raise HTTPException(422, "lon/lat out of range")
        L = _layer(lid)
        p = processed / L["path"]
        if not p.exists():
            raise HTTPException(404, "layer not produced yet")
        with rasterio.open(p) as ds:
            xs, ys = transform("EPSG:4326", ds.crs, [lon], [lat])
            r, c = ds.index(xs[0], ys[0])
            if not (0 <= r < ds.height and 0 <= c < ds.width):
                return {"layer": lid, "value": None, "note": "outside layer extent"}
            v = ds.read(1, window=((r, r + 1), (c, c + 1)))[0, 0]
        v = None if (v is None or (isinstance(v, float) and np.isnan(v)) or (np.issubdtype(type(v), np.floating)
                                                                            and np.isnan(v))) else float(v)
        return {"layer": lid, "lon": lon, "lat": lat, "value": v, "units": L["units"], "data_class": L["data_class"],
                "x": xs[0], "y": ys[0], "crs": str(ds.crs)}

    @app.get("/api/metrics")
    def metrics():
        out = {}
        for k, rel in {"terrain": "terrain/terrain_summary.json", "landcover": "landcover/landcover_summary.json",
                       "flood_obs": "flood/flood_obs_summary.json",
                       "flood_susceptibility": "flood/flood_susceptibility_eval.json",
                       "hindcast": "hydraulics/hindcast_eval.json"}.items():
            p = processed / rel
            if p.exists():
                d = json.loads(p.read_text())
                if k == "terrain":
                    d = {x: d.get(x) for x in ("dem_quality", "conditioning", "domain", "aoi_stats")}
                if k == "flood_susceptibility":
                    d = {"tests": {e: v["test"] for e, v in d.get("tests", {}).items()},
                         "spatial_cv": {e: v["calibration_event_spatial_cv"] for e, v in d.get("tests", {}).items()}}
                out[k] = d
        return out

    @app.get("/api/provenance/{stage}")
    def provenance(stage: str):
        allowed = {"terrain": "terrain/provenance.json", "s2": "remote_sensing/provenance.json",
                   "landcover": "landcover/provenance.json", "flood_obs": "flood/provenance_flood_obs.json",
                   "flood_susceptibility": "flood/provenance_flood_susceptibility.json",
                   "hindcast_eval": "hydraulics/provenance_hindcast_eval.json"}
        if stage not in allowed or not (processed / allowed[stage]).exists():
            raise HTTPException(404, f"no provenance for '{stage}'; known: {sorted(allowed)}")
        return json.loads((processed / allowed[stage]).read_text())

    @app.post("/api/jobs", status_code=202)
    def submit(req: JobReq):
        reg = _stage_registry()
        if req.stage not in reg:
            raise HTTPException(422, f"unknown stage; allowed: {sorted(reg)}")
        try:
            return {"id": jobs.submit(req.stage, reg[req.stage], s)}
        except RuntimeError as e:
            raise HTTPException(409, str(e)) from e

    @app.get("/api/jobs")
    def list_jobs():
        return jobs.get()

    @app.get("/api/jobs/{jid}")
    def get_job(jid: str):
        j = jobs.get(jid)
        if not j:
            raise HTTPException(404, "unknown job")
        return j

    web = Path(__file__).resolve().parents[2] / "web"
    if web.exists():
        app.mount("/", StaticFiles(directory=web, html=True), name="web")
    return app
