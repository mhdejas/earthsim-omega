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

## Experimental forecast

These endpoints are read-only. Cycles are started by `earthsim forecast run` or `scripts/forecast_scheduler.py`, not over HTTP.

| Method | Path | Returns |
|---|---|---|
| GET | `/api/forecast/latest` | `alert.json` of the latest *successful* cycle (404 before the first one) |
| GET | `/api/forecast/runs` | Up to 50 cycles, newest first: `run_id`, `status` (OK/FAILED), level, wall time, error |
| GET | `/api/forecast/{run_id}/outlook.png` | Outlook figure of one cycle (run ids are validated: letters, digits, `_`, `-`) |

`alert.json` includes `disclaimer` (always present), `issued_ist`, `overall_level_next_72h` (GREEN / YELLOW / ORANGE / RED) and `rain_outlook.days[]` (deterministic mm, ensemble p10/p50/p90, probabilities of IMD heavy, very heavy and extremely heavy rain, and the level with its reasons). It also includes `flood_scenarios.members` (wet km², OSM buildings, schools and hospitals, and major-road km at +24/+48/+72 h for four rain scenarios) and `verified_skill` (held-out trigger scores).

CLI equivalents: `earthsim forecast run [--no-hydraulics] [--workers N]`, `earthsim forecast latest` and `earthsim forecast verify-triggers`.
