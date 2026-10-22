# API

Start with `earthsim serve` (default `127.0.0.1:8000`). Interactive OpenAPI docs are at `/docs`. The web map is
served at `/`.

| Method | Path | Description |
|---|---|---|
| GET | `/api/health` | `{"status":"ok","version","rasterio","gdal","region"}` |
| GET | `/api/layers` | Layer registry: `id, title, data_class, units, cmap, available, bounds` (WGS84 `[[S,W],[N,E]]`). Layers that have not been produced are listed with `available:false`. |
| GET | `/api/layers/{id}.png` | The layer warped to EPSG:3857 and coloured, cached under `data/processed/web_cache/`. 404 if not produced. |
| GET | `/api/layers/{id}/value?lon=&lat=` | Point value read from the **source** raster in its native CRS. 422 if lon/lat is out of range. |
| GET | `/api/metrics` | Evaluation records: terrain QA, land-cover CV, SAR flood summary, susceptibility tests, hindcast evaluation |
| GET | `/api/provenance/{stage}` | Provenance JSON for `terrain, s2, landcover, flood_obs, flood_susceptibility` |
| POST | `/api/jobs` | Body `{"stage": "<name>"}` with `name ∈ {terrain, s2_indices, landcover, flood_obs, flood_susceptibility}` → 202 `{"id"}`. 422 unknown stage, 409 already running, 413 body > `api.max_request_bytes` |
| GET | `/api/jobs`, `/api/jobs/{id}` | Job status `queued/running/succeeded/failed` with result or traceback |

Layer ids: `dem, hand, ndvi, landcover, sar_2018_08_14, sar_2019_08_10, susceptibility, hindcast_depth, hindcast_hmax`.

Security posture (prototype): the server binds to localhost, has no authentication, uses allow-listed stage
names, caps request bodies and limits concurrency (`api.max_concurrent_jobs`). Do not expose it publicly as is.

Example:

```bash
curl -s localhost:8000/api/layers/hand/value?lon=75.80&lat=11.25
curl -s -X POST localhost:8000/api/jobs -H 'content-type: application/json' -d '{"stage":"flood_obs"}'
```
