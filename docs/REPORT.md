# EARTHSIM-Ω: first-release report (Kozhikode, Kerala)

Every number in this report was produced by code in this repository from real, public data. All of them can be
regenerated with the commands in §9. Each layer and figure is labelled **OBSERVED** (satellite measurement),
**MODELLED** (derived from observations by an algorithm or statistical model) or **SIMULATED** (output of the
physics solver driven by reanalysis rain).

## 1. Executive summary

EARTHSIM-Ω is a working, tested geospatial research prototype for Kozhikode district (2,367.75 km²), not a
finished digital twin. It acquires real open data with checksums and provenance, builds a 30 m hydrological
terrain model, maps observed floods from Sentinel-1 radar, classifies 2025 land cover from Sentinel-2, scores
flood susceptibility with leakage-safe spatial validation, and runs a mass-conserving 2-D flood solver. The
solver was calibrated under a protocol committed before any run, and then tested on a flood year it never saw.
On top of that sits an experimental decision-support cycle. It pulls the live ECMWF ensemble forecast, sets a
rain-trigger level that was verified over three monsoons, runs flood scenarios and counts the exposed
buildings, schools and roads. Results are served through an API and a web map.

What the results say:

- The terrain model reproduces published basin areas to within 4%: Chaliyar −3.8%, Kuttiyadi −2.4%.
- The solver passes its analytical tests. Mass closes to about 1e-15 in every run.
- Against radar-observed flooding, every method has high recall (0.81–0.97) and very low CSI (0.008–0.023). This
  includes KSDMA's official hazard map. The low scores mostly show how much radar misses (water under canopy, in
  built-up areas, and the peak timing), not that the models are wrong.
- The uncalibrated hindcast and the logistic model do not beat a one-line terrain rule (HAND ≤ 0.5 m).
- Calibration (12 runs on 14 Aug 2018) lifts the solver's held-out CSI on 10 Aug 2019 from 0.014 to 0.019, just
  above HAND (0.015). The 95% interval is 0.001–0.036, so the gain is not resolved, and KSDMA's map (0.023)
  still scores best. On 9 Aug 2018 the calibrated model is the worst of the four methods (0.003).
- Real CWC river gauges (Chaliyar at Kuniyil, Kuttiyadi at Kuttyadi) were added as a second test. Calibrating
  on August 2018 gauges raised held-out August 2019 river-flow skill (mean KGE 0.21 uncalibrated, 0.32 old
  setting, 0.52 gauge-calibrated; 0.09 vs −0.22 on an untouched third gauge), but it over-shoots 2019 peaks
  1.6–2× and does not improve the flood map (CSI 0.018 vs 0.019). See `docs/GAUGE_VALIDATION.md`.
- The rain trigger is the part with measurable skill. Out-of-sample over 366 monsoon days (2024–2026, 15 heavy
  days), the 1-day-ahead trigger has hit rate 0.60, false-alarm ratio 0.67 and CSI 0.27. On 30 Jul 2024
  (Vilangad landslides) it missed: 29 mm forecast against 69 mm in ERA5.
- The first live cycle (23 Oct 2026 03:31 IST, ECMWF run of 22 Oct) ran end to end in 476 s and gave
  experimental level YELLOW.

## 2. Actual hardware and software environment

Measured by `earthsim doctor` (`experiments/results/hardware_report.json`):

- Ubuntu 24.04 on KVM, 8 vCPUs (4 cores × 2 threads), inferred Intel Cascade Lake with AVX-512 and VNNI but no
  BF16 or AMX.
- 23.6 GiB RAM, **no GPU**, no Docker, no GDAL CLI (GDAL 3.12.2 comes bundled with rasterio).
- Python 3.12.3, numpy 2.5.4 (AVX-512 dispatch active), numba 0.68, rasterio 1.5.2, geopandas 1.2.0,
  scikit-learn 1.9.1, pysheds 0.5, FastAPI 0.161. The full lock file is `requirements-lock.txt`.

The design follows from these limits. Everything is CPU-only with numba kernels, and there are no deep-learning
claims because there is no GPU and no labelled ground truth.

## 3. Architecture implemented

Full detail is in `docs/ARCHITECTURE.md`.

- `config` holds pydantic-validated settings.
- `catalog` reads a dataset manifest and runs download adapters with sha256 checksums.
- `geo` provides grid alignment on a shared 60 m lattice in EPSG:32643, with resampling rules by data kind.
- Science modules: `hydro_core` (numba Priority-Flood+ε, D8, accumulation), `hydrology`, `remote_sensing`,
  `sar`, `landcover`, `floodsus`, `hydraulics` (local-inertial solver) and `scenario` (hindcast).
- `pipelines` runs the stages, and each stage writes provenance JSON.
- `api` (FastAPI) and `web/index.html` (Leaflet) serve the results. `cli` is the `earthsim` command.

## 4. Features completed

| Feature | Status | Evidence |
|---|---|---|
| Data catalog, checksums, provenance | IMPLEMENTED AND TESTED | `data/raw/*/_acquisition.json`, tests |
| Hardware detection, profiles | IMPLEMENTED AND TESTED | `earthsim doctor` |
| Terrain and hydrology (30 m, 26k km² domain) | IMPLEMENTED AND TESTED | 0 land cells without a receiver; basin areas −3.8% / −2.4% vs published |
| Sentinel-1 observed flood extents (3 dates) | IMPLEMENTED BUT NOT FULLY VALIDATED | no independent flood survey available |
| Sentinel-2 composite and indices, 2025 Q1 | IMPLEMENTED AND TESTED | 12 scenes, SCL masking, QA in `remote_sensing/` |
| Land-cover RF with spatial block CV | IMPLEMENTED BUT NOT FULLY VALIDATED | measures agreement with WorldCover (OA 0.970, κ 0.68), not ground truth |
| Flood susceptibility, leakage-safe evaluation | IMPLEMENTED AND TESTED | spatial CV plus a temporal test, §11 |
| Local-inertial 2-D solver | IMPLEMENTED AND TESTED | lake-at-rest exact; mass ~1e-15; wetting front vs analytical |
| Aug 2019 hindcast with sensitivity runs | IMPLEMENTED BUT NOT FULLY VALIDATED | uncalibrated; scored against SAR |
| Pre-registered calibration + held-out test | IMPLEMENTED AND TESTED | protocol committed before runs; `docs/CALIBRATION.md`; bootstrap CIs |
| Rain-trigger verification by lead time | IMPLEMENTED AND TESTED | 366 JJAS days 2024–26, leave-one-monsoon-out; `docs/EARLY_WARNING.md` |
| Live forecast cycle (rain outlook, flood scenarios, exposure, bulletin) | IMPLEMENTED BUT NOT FULLY VALIDATED | one live run, 476 s; ensemble cut-offs unverified |
| OSM exposure counts | IMPLEMENTED BUT NOT FULLY VALIDATED | buildings/facilities/roads in wet 240 m cells; not damage |
| API, web map, CLI | IMPLEMENTED AND TESTED | API tests; screenshots in `figures/` |

## 5. Features partially completed

- The **hindcast** uses spatially uniform ERA5 rain from 2 cells and a fixed runoff coefficient. It has no
  infiltration, channel bathymetry, tides or dams, and no Froude limiter: 0.09% of cells reach unphysical
  speeds above 5 m/s on steep slopes.
- **Docker**: a Dockerfile is written but never built, because Docker is not available on this machine.
- **Forecast scheduler**: `scripts/forecast_scheduler.py` detects new ECMWF runs and retries failures. It was
  run once (`--once`) and is not left running as a service.
- **Early-warning levels**: ORANGE and RED use ensemble probabilities that cannot be verified, because no past
  ensembles are archived. The rain trigger is verified for June–September only, and today's run is in October.
- The **Landsat 1990–2025 NDVI** composites are registered in the catalog but not analysed in this release.

## 6. Features not implemented (PLANNED)

- Delivery of alerts to people (SMS, WhatsApp or email). Bulletins are only written to disk and served by the API.
- Coupled ocean, tide, groundwater, landslide and urban drainage models.
- GPU kernels and deep-learning segmentation.
- Multi-user authentication and vector tiles.
- Probabilistic (GLUE or Bayesian) calibration. Only a pre-registered grid search was done.

## 7. Files and artifacts created

- Code is in `src/earthsim/` (22 modules) and tests are in `tests/` (56 tests).
- Configs are `configs/default.yaml`, `configs/datasets.yaml` and `configs/experiments/`.
- Docs are `README.md` and `docs/` (ARCHITECTURE, API, CALIBRATION, DATA_SOURCES, EARLY_WARNING, METHODOLOGY,
  MODEL_CARDS, this REPORT).
- Figures:
  - Phone-sized, labelled: `figures/phone/01_catchments_MODELLED.png`,
    `02_sar_flood_2018-08-14_OBSERVED.png`, `03_hindcast_aug2019_SIMULATED.png`,
    `04_calibration_heldout_SIMULATED.png`, `05_rain_trigger_replay_OBSERVED_vs_FORECAST.png` and
    `06_live_outlook_2026-10-23_FORECAST.png`.
  - The first live cycle's alert, bulletin and manifest: `experiments/results/forecast_*_20261023T0331_IST.*`.
  - Web map screenshots: `figures/webmap_sar2018.png` and `figures/webmap_hindcast.png`.
  - Overview figures: `figures/terrain_hydrology.png` and `figures/flood_observed_vs_susceptibility.png`.
- Benchmarks are in `benchmarks/results/benchmarks.json` and `.png`.
- The implementation log is `PROJECT_STATUS.md`.
- Processed rasters (about 2.6 GB) are not committed. They regenerate from the commands below.

## 8. Installation

```bash
git clone https://github.com/mhdejas/earthsim-omega && cd earthsim-omega
uv venv .venv --python 3.12 && source .venv/bin/activate
uv pip install -r requirements-lock.txt && uv pip install -e .
earthsim doctor
```

## 9. Commands to run the system

```bash
for d in aoi_kozhikode_geoboundaries copernicus_dem_glo30 esa_worldcover_2021 sentinel2_l2a_2025q1 \
         osm_kozhikode era5_rain_kozhikode_2018_2019 sentinel1_rtc_floods; do
  earthsim data download $d           # about 1.5 GB in total
done                                  # ksdma_flood_hazard and landsat_ndvi are local files (LOCAL mode)
earthsim run terrain && earthsim run s2_indices && earthsim run landcover
earthsim run flood_obs && earthsim run flood_susceptibility
earthsim run hindcast --tag base --runoff-c 0.7
earthsim run hindcast --tag c050 --runoff-c 0.5
earthsim run hindcast --tag c090 --runoff-c 0.9
earthsim run hindcast_eval
earthsim calibrate terrain            # then the 12 runs, select and test: see docs/CALIBRATION.md
earthsim calibrate select && earthsim calibrate test
earthsim forecast verify-triggers     # replay of archived ECMWF forecasts vs ERA5, 2024-2026
earthsim forecast run                 # one live cycle; or: python scripts/forecast_scheduler.py
earthsim run benchmarks
earthsim serve                        # then open http://127.0.0.1:8000
```

## 10. Exact test commands and results

```bash
python -m pytest -v tests      # 57 passed, 0 failed, 268 warnings in 9.34 s (2026-10-23 17:59 IST)
```

The tests cover:

- grid alignment and resampling rules;
- Priority-Flood with 8- and 4-connectivity, D8, accumulation, HAND and basins;
- spectral indices, SAR thresholding, the land-cover block CV and the config validators;
- flood metrics and leakage-safe splits;
- the API endpoints and job queue, the CLI, and hindcast aggregation;
- rain-trigger scoring, alert levels, bulletin text and the forecast API endpoints;
- gauge flow accounting (water balance closes within 0.1% on a synthetic valley).

The tests run on synthetic fixtures and need no network.

## 11. Scientific validation evidence

**Terrain (MODELLED).** Priority-Flood+ε in float64 leaves 0 land cells without a downslope receiver. pysheds
0.5 left 96 flat and 949 pit cells, so I did not use it.

| Basin | Modelled | Published | Error |
|---|---|---|---|
| Chaliyar | 2,820 km² | 2,933 km² ([India-WRIS](https://indiawris.gov.in/wiki/doku.php?id=chaliyar)) | −3.8% |
| Kuttiyadi | 642 km² | 657 km² ([IJERT 2022](https://www.ijert.org/impact-of-land-use-land-cover-change-on-runoff-in-kuttiyadi-river-basin)) | −2.4% |

**Observed floods (OBSERVED, Sentinel-1 RTC VV).** The flood rule is a drop of at least 3 dB from same-orbit
dry-season references, plus post-event backscatter below −15 dB.

- 2018-08-14 (orbit 63, full district): 2.26 km² newly flooded beyond 23.6 km² of normal water.
- 2019-08-10 (orbit 165, which covers only the eastern 14% of the district): 0.28 km².
- The 2018 overpass came before the 14–16 Aug rain peak (ERA5: 74–103 mm/day), so it misses most of the flood.

**Susceptibility (MODELLED).** Fit on 2018 with 5 km block CV, tested on 2019 with 307 flooded pixels.

| Method | CSI (CV) | Recall (CV) | CSI (2019 test) | Recall (2019 test) |
|---|---|---|---|---|
| HAND ≤ 0.5 m | 0.009 | 0.94 | 0.015 | 0.95 |
| Logistic | 0.008 | 0.59 | 0.008 | 0.64 |
| KSDMA 25-yr map (external) | 0.011 | 0.81 | 0.023 | 0.96 |

**Solver (SIMULATED).**

- Lake at rest: maximum |Δh| = 0.0.
- Closed-basin rain: relative mass error 1.7e-15.
- Wetting front against the Hunter/Bates analytical solution:
  - u = 1 m/s: RMSE 0.071 m, front −7.4%;
  - u = 0.5: front −3.6%;
  - u = 0.25: front −0.6%.

  The front lag grows with Froude number and does not shrink with finer dx or dt, which is a known property of
  the inertial scheme.

**Hindcast, 1–10 Aug 2019 (SIMULATED), 240 m.** ERA5 rain was 410 mm (mean of 2 cells). Each run is scored
against the 2019-08-10 SAR extent on the same 369,017 reference pixels (310 flooded).

| Runoff coeff. | Wall time | Mass rel. error | h_max | CSI (depth ≥ 0.3 m) | Recall |
|---|---|---|---|---|---|
| 0.5 | 220 s | 6.2e-15 | 8.5 m | 0.014 | 0.81 |
| 0.7 (base) | 206 s | 2.9e-15 | 10.1 m | 0.014 | 0.91 |
| 0.9 | 261 s | 4.1e-15 | 11.4 m | 0.013 | 0.97 |

On the same pixels, the HAND rule scores CSI 0.015 and KSDMA-25 scores 0.023. The skill score is insensitive to
the runoff coefficient. Visually, the simulated channels follow the real rivers on the OpenStreetMap basemap
(`figures/webmap_hindcast.png`).

Two earlier hindcast versions failed. v1 used block-mean aggregation and produced 30–46 m artefact pile-ups.
v2 had diagonal-only outlets that trapped water up to 67 m deep. Both were diagnosed, fixed and archived under
`data/processed/hydraulics/` (see `PROJECT_STATUS.md`).

**Calibration and held-out test (SIMULATED).** The protocol
(`configs/experiments/flood_calibration.yaml`) was committed before the first run. The grid was runoff C
{0.3, 0.5, 0.7, 0.9} × Manning's n {0.04, 0.08, 0.16} × 9 depth thresholds, selected on 14 Aug 2018 SAR. The
chosen setting was C 0.9, n 0.16, ≥2.0 m, with calibration CSI 0.0195. All three values sit at the grid edge,
and the response surface is flat: 0.0145–0.0195 across the 12 pairs. A checkerboard split of the district
gives different choices on each half.

| Method | CSI 10 Aug 2019 (held-out year) | CSI 9 Aug 2018 |
|---|---|---|
| Solver, calibrated | 0.019 (95% 5-km block bootstrap 0.001–0.036) | 0.003 |
| Solver, default settings | 0.014 | 0.005 |
| HAND ≤ 0.5 m | 0.015 | 0.010 |
| KSDMA 25-yr map | 0.023 | 0.015 |

On the calibration event the model predicted 20.9 km² against 2.26 km² observed by radar, about 9× more.

**Real river gauges (OBSERVED vs SIMULATED), protocol `configs/experiments/gauge_validation.yaml`.**
CWC daily discharge and hourly water level (India National Water Data Portal) for Kuniyil (Chaliyar, 1,876 km²)
and Kuttyadi (Kuttiyadi, 238 km²) were matched to the model network within +1.9% and 0.0% of the published
catchment areas. Eight 2018 settings were scored with KGE. The chosen setting (rain factor 1.75, n 0.08, depth
threshold 2.0 m) had 2018 KGE 0.71 at Kuniyil and −0.02 at Kuttyadi. On held-out August 2019:

| Setting | Kuniyil KGE | Kuttyadi KGE | Thottathinkadavu KGE | CSI 10 Aug 2019 |
| --- | --- | --- | --- | --- |
| Uncalibrated | 0.26 | 0.15 | −0.22 | 0.014 |
| Old radar-calibrated | 0.36 | 0.28 | −0.18 | 0.019 |
| Gauge-calibrated | 0.34 | 0.69 | 0.09 | 0.018 (95%: 0.000–0.035) |

The gauge-calibrated run over-shoots the 2019 peaks (4,247 vs 2,710 m³/s at Kuniyil; 658 vs 333 at Kuttyadi).
Full write-up: `docs/GAUGE_VALIDATION.md`.

**Rain trigger (OBSERVED vs FORECAST).** Archived ECMWF IFS forecasts (Open-Meteo previous-runs; 2021–2023 are
not archived) were compared with ERA5 district-mean rain over 366 June–September days in 2024–2026. Fifteen of
those days reached IMD "heavy" (≥64.5 mm).

- Using 64.5 mm directly, 1 day ahead: hit rate 0.40, false-alarm ratio 0.54, CSI 0.27.
- Leave-one-monsoon-out tuning, 1 day ahead: hit rate 0.60 (9/15), false-alarm ratio 0.67, CSI 0.27, about one
  false alarm per 20 dry days. The final trigger is 50 mm.

**First live cycle (FORECAST + SIMULATED), 23 Oct 2026 03:31 IST.**

- Inputs: ECMWF IFS deterministic run initialised 22 Oct 12 UTC, and the 51-member ensemble from 22 Oct 06 UTC.
- Wall time: 476 s (fetch 12 s, four hydraulic scenarios 463 s).
- Level: YELLOW. On 25 Oct, 28% of ensemble members reach heavy rain. On 26 Oct the deterministic forecast is
  62 mm, above the 50 mm trigger.
- Flood scenarios over 72 h, at ≥2.0 m in 240 m cells:
  - deterministic, 142 mm: 62 km² wet, containing 1,122 OSM buildings and 17 schools;
  - ensemble 10th percentile, 48 mm: 21 km² wet;
  - ensemble 90th percentile, 227 mm: 113 km² wet, containing 2,252 buildings.
- An independent recheck found that 36 of the deterministic 62 km² lie on cells with less than 10% mapped water,
  so the wet area is mostly land, not rivers.
- Given the 9× over-prediction and the October season, these are illustrations of the pipeline, not a forecast
  anyone should act on.

## 12. Benchmark methodology and measured results

Each benchmark is the median of 3 runs after a JIT warm-up, on the hardware in §2
(`benchmarks/results/benchmarks.json`).

| Threads | Solver on 1 M cells | Speed-up |
|---|---|---|
| 1 | 15.5 M cell-steps/s | 1.0× |
| 2 | 27.6 M cell-steps/s | 1.8× |
| 4 | 46.7 M cell-steps/s | 3.0× |
| 8 | 62.1 M cell-steps/s | 4.0× (4 physical cores + SMT) |

Hydrology core timings (Priority-Flood + D8 + accumulation):

| Grid | Total time |
|---|---|
| 1 M cells | 0.43 s |
| 4 M cells | 1.95 s |
| 16 M cells | 8.7 s |

At the full 30 m terrain scale, the terrain stage takes 173 s with a peak RSS of 3.3 GiB.

## 13. Data sources and licensing considerations

Full detail is in `docs/DATA_SOURCES.md`.

- Copernicus GLO-30 DEM: © DLR e.V. 2010–2014 and © Airbus 2014–2018, provided under COPERNICUS.
- Sentinel-1 and Sentinel-2: Copernicus Sentinel data 2018–2025 (free and open). Sentinel-1 RTC comes via
  Microsoft Planetary Computer. Sentinel-2 L2A comes via Earth Search (Element 84).
- ESA WorldCover 2021: CC BY 4.0.
- ERA5 (C3S), via the Open-Meteo archive.
- OpenStreetMap: ODbL.
- geoBoundaries: CC BY 4.0 / ODbL.
- KSDMA hazard rasters: provided by the owner. Their units are undocumented and they are used only as a footprint.

## 14. Known limitations

- The DEM is a DSM, so it includes trees and buildings. This biases HAND and the hindcast.
- SAR sees only open water, so the observed extents are lower bounds.
- Orbit-165 scenes cover 14% of the district.
- The 2019 test set is small (307–310 flooded pixels), so its scores are noisy.
- The flood model has uniform rain and no infiltration, channel bathymetry, tides or dams. Its calibration is
  edge-of-grid, unstable across space, and about 9× over-predicts radar extent. Its held-out gain is unresolved.
- The rain trigger rests on 15 heavy days. It misses local cloudbursts such as 30 Jul 2024, and it is unverified
  outside June–September.
- Gauge test: the model starts with dry rivers (no baseflow), ERA5 rain mistimes the 2018 peaks, the 2018 rain
  factor over-shoots in 2019, Kuttyadi is dam-regulated, Kuttyadi's 2018 discharge stops on 11 Aug, and the
  Thottathinkadavu 2019 water-level record has inconsistent readings (up to 129.6 m) and is not trusted.
- Building counts mean "centroid in a wet 240 m cell". They are not damage estimates.
- 393 hindcast cells exceed 5 m/s.
- The land-cover "accuracy" is agreement with a 2021 product.
- The Dockerfile is untested.
- None of this is suitable for emergency or planning decisions.

## 15. Remaining high-priority tasks

1. Add a Froude limiter to the solver and recheck the steep-slope speeds.
2. Use a bare-earth DEM, such as FABDEM (non-commercial licence) or the Kerala LiDAR DEM, if accessible.
3. Add more and better flood references: Sentinel-1 for 16–20 Aug 2018, and the KSDMA or Copernicus EMS
   EMSR-301 2018 delineations.
4. Use spatially varying rain (IMD gridded data) and a simple infiltration model; start runs from observed
   July baseflow and represent Peruvannamuzhi releases (gauge test findings).
5. Verify the ensemble ORANGE/RED cut-offs once enough live cycles are archived, and replay the north-east
   monsoon (Oct–Dec).
6. Use IMD station or gridded rain as the trigger's truth instead of ERA5, which smooths cloudbursts.
7. Build and test the Docker image on a Docker host.

## 16. Recommended next development milestone

**v0.2, "validated hindcast of the August 2018 floods":**

- Froude limiter plus channel geometry.
- IMD rainfall.
- Re-calibration with a wider parameter grid against EMS-301 and multi-date Sentinel-1, scored on held-out
  basins.
- An archive of live cycles, so the ensemble alert levels can be verified.

The target is to beat the HAND baseline on held-out basins, and to report the result honestly if it does not.
