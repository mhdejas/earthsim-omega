# Architecture

EARTHSIM-Ω is one Python package (`earthsim`) with a CLI, a FastAPI service and a static Leaflet front end.
Each stage reads inputs from `data/raw` or earlier stages and writes georeferenced GeoTIFFs and JSON summaries
under `data/processed/<stage>/`, plus a `provenance.json`. Nothing is shared through global state. The
filesystem, with checksums and provenance, is the contract between stages.

```
             configs/default.yaml + --set / EARTHSIM_* env  ──►  config.Settings (pydantic, strict)
                                    │
 configs/datasets.yaml ──► catalog.Catalog ──► adapters (http / s3 COG / STAC / Overpass / Open-Meteo / local)
                                    │              └─ data/raw/<id>/ + _acquisition.json (URL, sha256, time)
                                    ▼
 geo (AOI, Grid on a shared 60 m lattice, reproject_to_grid with explicit resampling rules, write_raster)
   │
   ├─ hydro_core (numba): Priority-Flood+ε, D8, accumulation ─► hydrology: streams, HAND, TWI, basins
   ├─ remote_sensing: Sentinel-2 L2A (SCL mask, offset handling), median composite, indices
   ├─ landcover: RF + baselines, GroupKFold spatial blocks
   ├─ sar: Sentinel-1 RTC change detection (event vs same-orbit dry references)
   ├─ floodsus: HAND / distance / logistic susceptibility + leakage-safe evaluation
   ├─ hydraulics (numba): local-inertial 2-D solver + analytical test cases
   ├─ scenario: coarse terrain (channel-preserving) + ERA5 forcing + hindcast + SAR evaluation
   ├─ calibration: pre-registered 12-run calibration on 2018, held-out 2019 test, block bootstrap
   ├─ rain_trigger: live ECMWF fetch (Open-Meteo) + lead-time verification vs ERA5, LOYO trigger tuning
   └─ forecast: one decision-support cycle (rain outlook -> 4 flood members -> OSM exposure -> alert)
                                    │                     └ scripts/forecast_scheduler.py (new model run -> cycle, lock, retries)
           pipelines.stage_* (one function per stage, writes provenance.RunRecord)
                                    │
         cli (click)  ◄──────────────┴──────────────►  api (FastAPI) ──► web/index.html (Leaflet)
                                                         └ jobs: SQLite + thread pool, allow-listed stages
```

## Key design decisions

| Decision | Reason | Evidence |
|---|---|---|
| Analysis CRS EPSG:32643 (UTM 43N). All grids snap to a 60 m lattice | Metric units, and 20/30 m grids align exactly | `tests/unit/test_geo.py` |
| Explicit resampling per data kind (categorical → mode/nearest, continuous → bilinear) | Prevents silent label mixing | `geo.RESAMPLING_RULES`, resampling logs in every summary |
| In-house numba Priority-Flood+ε instead of pysheds routing | pysheds 0.5 left 96 flats and 949 pits on land. Ours leaves 0 | `terrain_summary.json → conditioning` |
| Terrain domain = district + 45 km buffer | The Chaliyar and Kuttiyadi catchments extend well outside the district | Basin areas within 4% of India-WRIS / published values |
| Local-inertial solver written in numba (not LISFLOOD-FP/HEC-RAS) | No established solver installable offline here. The scheme is the same one LISFLOOD-FP uses (Bates 2010) | Analytical tests in `benchmarks/results` |
| Hindcast at 240 m with channel-preserving aggregation | 8 vCPU budget. Block-mean alone produced artificial gorge dams (v1, kept for comparison) | `data/processed/hydraulics/v1_mean_aggregation` |
| Jobs in SQLite, allow-listed stage names | Survives restarts (interrupted jobs → failed), no arbitrary execution | `tests/unit/test_api_cli_scenario.py` |
| Forecast cycles write to a new run folder and move `latest` only on success | A failed cycle can never replace a good bulletin. Every run keeps its raw inputs and SHA-256 hashes | `data/forecast/runs/<id>/manifest.json` |
| The alert level comes from verified rain triggers, not the flood model | The flood model is checked on only two events, the rain trigger on 366 days | `docs/EARLY_WARNING.md` |
| Calibration rule fixed and committed before any run | Prevents tuning on the test event | `configs/experiments/flood_calibration.yaml`, git history |
| The web map only serves real outputs, and every layer carries a data class | Keeps observations, model-derived products and simulations visibly separate | `/api/layers` |

## Data classes

Every output is tagged with one of four classes: **observation** (DEM), **observation-derived** (SAR flood extent,
Sentinel-2 indices), **model-derived** (HAND, susceptibility, land cover) or **simulation** (hindcast depth), plus **forecast** (scenario footprints driven by forecast rain).
The KSDMA hazard rasters are an external model and are used only as comparators.

## Not implemented (see REPORT.md)

There is no 3-D view, GPU path (no GPU on this machine), distributed or out-of-core execution beyond windowed
COG reads, or authentication. The Docker files are written but untested because Docker is not available here.
