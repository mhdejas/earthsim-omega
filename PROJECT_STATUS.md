# EARTHSIM-Ω project status (running log)

Last updated: 2026-10-23 ~00:58 IST. Project root: /workspace/earthsim-omega. Evidence PNGs: /workspace/earthsim-omega-shots/

## Environment (measured by `earthsim doctor`)
- Ubuntu 24.04 on KVM. 8 vCPUs (4 reported cores × 2 threads). Family 6 / model 85 / stepping 7, inferred Cascade Lake (AVX512_VNNI present, no BF16/AMX). The model string "Intel Xeon Processor @ 2.50GHz" comes from the hypervisor; the host SKU and sustained clock are unknown. L3 35.8 MiB.
- 23.6 GiB RAM (about 20 GiB available). 37 GiB free disk. **No GPU**. No Docker. No GDAL CLI (GDAL 3.12.2 is bundled in rasterio).
- numpy runtime dispatch targets active: X86_V3, X86_V4 (AVX-512). torch ATen capability: AVX512. VNNI is not used (no int8 kernels).
- venv: .venv (Python 3.12.3). numpy 2.5.4, rasterio 1.5.2, geopandas 1.2.0, torch 2.14.1+cpu, sklearn 1.9.1, pysheds 0.5 (+ np.in1d shim), numba 0.68.

## Data acquired (data/raw/<id>/_acquisition.json holds checksums)
- aoi_kozhikode_geoboundaries: local copy of geoBoundaries IND ADM2 at commit 9469f09 (sha256 matches the pinned 8bef69...). URL https://github.com/wmgeolab/geoBoundaries/raw/9469f09/releaseData/gbOpen/IND/ADM2/geoBoundaries-IND-ADM2.geojson. ODbL. Area 2367.75 km² in EPSG:32643.
- copernicus_dem_glo30: tiles N11E075 and N11E076 from https://copernicus-dem-30m.s3.amazonaws.com/ (DSM, EGM2008).
- esa_worldcover_2021: v200 tile N09E075 from https://esa-worldcover.s3.eu-central-1.amazonaws.com/ (CC BY 4.0).
- era5_rain: Open-Meteo archive API (models=era5), 5 points, Aug 2018 and Aug 2019, hourly mm.
- osm_kozhikode: Overpass (overpass-api.de, needs a User-Agent header; the default client got 406 / disconnect). 3565 road ways, 3520 facilities, 240,990 building centroids. OSM base 2026-10-22T19:2x Z.
- ksdma_flood_hazard: local KSDMA 10/25/50-year rasters (EPSG:4326, about 90 m, units undocumented). Used only as a binary footprint; it is an external model, not an observation.
- landsat_ndvi_1990_2025: 12 three-year NDVI composites from /workspace/kozhikode_longterm.
- sentinel2_l2a_2025q1: Earth Search sentinel-2-c1-l2a, 2025-01-01/2025-03-31, cloud below 30%. 62 items found, 12 selected (3 lowest-cloud per tile: 43PEN, 43PEP, 43PFN, 43PFP), baseline 05.11. Stored on the 20 m UTM43 grid as float reflectance plus SCL.

## Results so far
- Terrain (30 m, 2 km buffer): 35.6 s, peak RSS 1.03 GiB. 538k depression-filled cells, max fill 28 m. 1502 km of streams at 1 km². Max catchment only 433 km² (Chaliyar truncated), so I'm widening the domain to 30 km and adding N10 DEM tiles.

## Known issues / decisions
- whitebox was dropped (its binary download hung). Using pysheds.
- The Copernicus DEM is a DSM (canopy and building bias). FABDEM would be a bare-earth alternative but it is CC BY-NC-SA.

## Update 01:16 IST: terrain v2 and external references
The terrain domain now uses a 45 km buffer, 6 Copernicus tiles (N10, N11 and N12, each at E075 and E076), and bbox 75.1227–76.5651 E, 10.7127–12.2136 N (25,963 km², 5228×5518 px at 30 m). Every grid is snapped to a 60 m lattice in EPSG:32643. Conditioning is now Priority-Flood+ε in float64 (src/earthsim/hydro_core.py, numba), leaving 0 land cells without a receiver. pysheds 0.5 left 96 flats and 949 pits, so it was rejected. Of the 27 basins that drain the district, 0 are internal sinks and 1 is truncated: the east-flowing Kabini headwater, which holds 6.98 km² of the district and has an edge outlet at 76.564 E, 12.103 N. The run took 173 s with a peak RSS of 3.3 GiB.

Basin validation is in experiments/results/basin_area_validation.json.

| Basin | Modelled area | Published area | Difference | Source |
|---|---|---|---|---|
| Chaliyar (outlet 75.8127 E, 11.1760 N, Beypore) | 2820.16 km² | 2933 km² | −3.85% | India-WRIS, https://indiawris.gov.in/wiki/doku.php?id=chaliyar ("drains a total area of about 2,933 sq km, of which 388 sq km lies in Tamil Nadu") |
| Kuttiyadi (outlet 75.5981 E, 11.5699 N) | 641.5 km² | 657 km² | −2.36% | IJERT 2022, https://www.ijert.org/impact-of-land-use-land-cover-change-on-runoff-in-kuttiyadi-river-basin |

Other sources give 583 to 667 km² for the Kuttiyadi.

Sentinel-1 RTC on Planetary Computer (https://planetarycomputer.microsoft.com/api/stac/v1, collection sentinel-1-rtc, signed with the planetary-computer package):

| Role | Orbit 165 (descending, about 00:40 UTC) | Orbit 63 (descending) |
|---|---|---|
| Flood-season scenes | 2018-07-28, 2018-08-09, 2018-08-21, 2018-09-02, 2019-07-29, 2019-08-10, 2019-08-22 | 2018-08-02, 2018-08-14, 2018-08-26, 2019-08-03, 2019-08-21, 2019-08-27 |
| Dry-season references | 2019-01-12, 2019-01-24, 2019-02-05 (plus many in Jan–Mar 2018) | 2019-01-05, 2019-01-17, 2019-01-29 |

## Update 01:58 IST: SAR floods, susceptibility, solver
- S1 RTC (Planetary Computer sentinel-1-rtc, VV, 20 m overview -> 30 m district grid). Asset nodata = -32768. v1 overrode it with src_nodata=0 (bug, fixed; logs/acquire_s1_v1_nodata_bug.log). Re-acquired 7 scenes in 248 s, 295 MB.
- Orbit 165 covers only 14% of the district (east, from 599130 E). Orbit 63 covers 100%.
- Observed flood (VV change: post < T, drop >= 3 dB, ref >= T; Otsu clamped to [-23,-15], hit -15 every time): 2018-08-14 o63 = 2.26 km2 (permanent water 23.6 km2); 2019-08-10 o165 = 0.28 km2 on 335 km2 valid; 2018-08-09 o165 = 0.18 km2.
- ERA5 (Open-Meteo, IST): the 5 points collapse to 2 ERA5 cells (11.25N 76.0E; 11.5N 75.75E). 2018 daily max on 14-16 Aug (74-103 mm/d), so the 14 Aug 06:18 IST overpass is before most of the peak. 2019: 7-10 Aug (66-101 mm/d).
- Susceptibility (fit 2018-08-14, 5 km GroupKFold, 127 blocks, 2.6M px). Spatial CV: HAND<=0.5 m CSI 0.009, recall 0.94, flags 9.3%; logistic CSI 0.008, recall 0.59; dist<=90 m CSI 0.003; KSDMA 10/25/50 CSI 0.011-0.012, recall 0.81, flags 6.2-7.0%. 2019 test (307 positive px): HAND CSI 0.015, recall 0.95; KSDMA-25 CSI 0.023, recall 0.96; logistic 0.008. Low CSI mostly reflects how incomplete the SAR reference is.
- Solver (local inertial, Bates 2010): lake-at-rest exact (0.0); closed-basin mass rel err 1.7e-15; Hunter wetting front u=1 RMSE 0.071 m, front -7.4%; u=0.5 -3.6%; u=0.25 -0.6% (Froude effect; insensitive to dx and dt). 63 Mcell-steps/s on 8 threads.
- Hindcast v1 (block-mean 240 m DSM): 441 s, mass 9e-15, but 30-46 m artefact pileups in Ghats gorges (21k cells filled > 1 m). Fixing with channel-preserving aggregation. v1 is kept in data/processed/hydraulics/v1_mean_aggregation.
- Hindcast v2 (channel-preserving, 8-connected PF): 429 s, mass 1.7e-15, hmax 67 m. Root cause: the solver uses 4 face fluxes, and cells whose only lower neighbour is diagonal were closed pits for it (water surface 40-110 m above a diagonal neighbour, v ~0). Fix (v3): carve each D8 diagonal step into a face path (57,505 cells), then 4-connected Priority-Flood. Result: 0 land cells without a lower face neighbour, 8,774 cells filled > 1 m, max fill 19.5 m. New test test_priority_flood_4_connectivity_opens_diagonal_only_outlets. v1 and v2 outputs kept under data/processed/hydraulics/.
## Update 02:15 IST: hindcast v3 results
- base (C=0.7): 205.9 s wall, 33,879 steps, 74 M cell-steps/s, mass rel residual 2.86e-15, 0 negative or NaN cells, h_max 10.09 m. v_max 26.7 m/s is unphysical; 393 cells (0.09%) exceed 5 m/s, all thin flow on ~9° slopes (local-inertial supercritical weakness; LISFLOOD-FP uses a Froude limiter, not implemented here). Where depth >= 0.3 m, the p99 speed is 3.0 m/s.
- Rain: ERA5 2 cells, 428.4 and 391.8 mm (mean 410 mm), 1 Aug 00:00 to 10 Aug 06:10 IST; net 287 mm at C=0.7. Ocean outflow 4.17e9 m3, edge outflow 0.93e9 m3, stored 2.23e9 m3.
- Eval vs SAR 2019-08-10 (369,017 reference px, 310 flooded): depth_at_overpass>=0.3 m CSI 0.0139, recall 0.913; >=0.1 m CSI 0.0077, recall 0.961; HAND<=0.5 CSI 0.0147, recall 0.952; KSDMA-25 CSI 0.0233, recall 0.948. Simulated wet area in the district at the overpass (>=0.3 m) is 268 km², against 0.28 km² observed by SAR on 335 km² valid. The SAR reference undercounts strongly.
- Phone figures: figures/phone/01_catchments_MODELLED.png and 02_sar_flood_2018-08-14_OBSERVED.png (1080x1350).
## Update 02:25 IST: final docs and publishing
- Sensitivity: c050 (C=0.5) 219.5 s, mass 6.2e-15, hmax 8.48 m, CSI 0.0143, recall 0.81. c090 (C=0.9) 261.4 s, mass 4.1e-15, hmax 11.44 m, CSI 0.0127, recall 0.968. Eval: experiments/results/hindcast_eval.json.
- Benchmarks (benchmarks/results/benchmarks.json): solver on 1M cells 15.5/27.6/46.7/62.1 Mcell-steps/s at 1/2/4/8 threads (4.0x). Hydro core 1M 0.43 s, 4M 1.95 s, 16M 8.7 s.
- Tests: 42 passed in 9.62 s.
- docs/REPORT.md written (section-29 format). Phone figures in figures/phone/; web map screenshots figures/webmap_sar2018.png and figures/webmap_hindcast.png (headless Chrome against the live server, real OSM tiles).
- Git: local commit 0a66d6b on main (70 files; logs/ and data/ excluded; secret/SAS scan clean). The GitHub connector (GitHub App, user mhdejas id 286408735) gets 403 on repo creation (GraphQL createRepository and REST POST user/repos). It has ADMIN on existing repos. Plain gh is not logged in.
