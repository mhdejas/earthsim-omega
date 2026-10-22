# EARTHSIM-Ω: first-release report (Kozhikode, Kerala)

Every number in this report was produced by code in this repository from real, public data. All of them can be
regenerated with the commands in §9. Each layer and figure is labelled **OBSERVED** (satellite measurement),
**MODELLED** (derived from observations by an algorithm or statistical model) or **SIMULATED** (output of the
physics solver driven by reanalysis rain).

## 1. Executive summary

EARTHSIM-Ω is a working, tested geospatial research prototype for Kozhikode district (2,367.75 km²), not a
finished digital twin. It acquires real open data with checksums and provenance, builds a 30 m hydrological
terrain model, maps observed floods from Sentinel-1 radar, classifies 2025 land cover from Sentinel-2, scores
flood susceptibility with leakage-safe spatial validation, and runs a mass-conserving 2-D flood solver as a
hindcast of August 2019. Results are served through an API and a web map.

What the results say:

- The terrain model reproduces published basin areas to within 4%: Chaliyar −3.8%, Kuttiyadi −2.4%.
- The solver passes its analytical tests. Mass closes to about 1e-15 in every run.
- Against radar-observed flooding, every method has high recall (0.81–0.97) and very low CSI (0.008–0.023). This
  includes KSDMA's official hazard map. The low scores mostly show how much radar misses (water under canopy, in
  built-up areas, and the peak timing), not that the models are wrong.
- Neither the logistic model nor the hindcast beats a one-line terrain rule (HAND ≤ 0.5 m). KSDMA's map scores
  best. I report this as it is.

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
| API, web map, CLI | IMPLEMENTED AND TESTED | API tests; screenshots in `figures/` |

## 5. Features partially completed

- The **hindcast** uses spatially uniform ERA5 rain from 2 cells and a fixed runoff coefficient. It has no
  infiltration, channel bathymetry, tides or dams, and no Froude limiter: 0.09% of cells reach unphysical
  speeds above 5 m/s on steep slopes.
- **Docker**: a Dockerfile is written but never built, because Docker is not available on this machine.
- **OSM exposure**: 240,990 building centroids, 3,565 road ways and 3,520 facilities were acquired, but no
  impact or exposure stage uses them yet.
- The **Landsat 1990–2025 NDVI** composites are registered in the catalog but not analysed in this release.

## 6. Features not implemented (PLANNED)

- Forecasting, real-time ingestion and alerts.
- Coupled ocean, tide, groundwater, landslide and urban drainage models.
- GPU kernels and deep-learning segmentation.
- Multi-user authentication and vector tiles.
- Calibration (e.g. GLUE or Bayesian) of the hindcast.

## 7. Files and artifacts created

- Code is in `src/earthsim/` (18 modules) and tests are in `tests/` (42 tests).
- Configs are `configs/default.yaml`, `configs/datasets.yaml` and `configs/experiments/`.
- Docs are `README.md` and `docs/` (ARCHITECTURE, API, DATA_SOURCES, METHODOLOGY, MODEL_CARDS, this REPORT).
- Figures:
  - Phone-sized, labelled: `figures/phone/01_catchments_MODELLED.png`,
    `02_sar_flood_2018-08-14_OBSERVED.png` and `03_hindcast_aug2019_SIMULATED.png`.
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
earthsim run benchmarks
earthsim serve                        # then open http://127.0.0.1:8000
```

## 10. Exact test commands and results

```bash
python -m pytest -q tests      # 42 passed in 9.62 s (2026-10-23 02:19 IST)
```

The tests cover:

- grid alignment and resampling rules;
- Priority-Flood with 8- and 4-connectivity, D8, accumulation, HAND and basins;
- spectral indices, SAR thresholding, the land-cover block CV and the config validators;
- flood metrics and leakage-safe splits;
- the API endpoints and job queue, the CLI, and hindcast aggregation.

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
- The hindcast is uncalibrated: uniform rain, no infiltration, channels, tides or dams.
- 393 hindcast cells exceed 5 m/s.
- The land-cover "accuracy" is agreement with a 2021 product.
- The Dockerfile is untested.
- None of this is suitable for emergency or planning decisions.

## 15. Remaining high-priority tasks

1. Add a Froude limiter to the solver and recheck the steep-slope speeds.
2. Use a bare-earth DEM, such as FABDEM (non-commercial licence) or the Kerala LiDAR DEM, if accessible.
3. Add more and better flood references: Sentinel-1 for 16–20 Aug 2018, and the KSDMA or Copernicus EMS
   EMSR-301 2018 delineations.
4. Use spatially varying rain (IMD gridded data) and a simple infiltration model.
5. Add the OSM exposure stage (buildings within the observed or simulated flood).
6. Build and test the Docker image on a Docker host.

## 16. Recommended next development milestone

**v0.2, "validated hindcast of the August 2018 floods":**

- Froude limiter plus channel geometry.
- IMD rainfall.
- Calibration against EMS-301 and multi-date Sentinel-1, scored on held-out basins.
- An exposure summary from OSM buildings.

The target is to beat the HAND baseline on held-out basins, and to report the result honestly if it does not.
