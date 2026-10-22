# Methodology

All grids use EPSG:32643 (WGS 84 / UTM 43N) and snap to a shared 60 m lattice, so the 20 m optical and 30 m
terrain grids align exactly. Resampling is chosen by data kind: categorical data uses mode or nearest,
continuous data uses bilinear. Every summary JSON logs the resampling used.

## 1. Terrain and hydrology (`earthsim run terrain`)

- **Input.** Copernicus GLO-30 DSM, 6 tiles, mosaicked and warped to 30 m on the district plus a 45 km buffer
  (25,963 km², 5228×5518 px). The buffer is there so the Chaliyar and Kuttiyadi catchments are complete.
- **Ocean mask.** Cells at or below 0 m that are 8-connected to the raster border. Inland cells below sea
  level, such as reclaimed paddy land, are kept as land.
- **Conditioning.** Priority-Flood+ε in float64 (Barnes et al. 2014), written in numba
  (`hydro_core.py`). Result: 0 land cells without a strictly lower D8 neighbour. pysheds 0.5 was run as a
  cross-check and rejected, because it left 96 flat and 949 pit cells on land.
- **Derived layers.** D8 steepest descent; accumulation in descending-elevation order; streams at 1 km²;
  HAND (height above the nearest stream cell along the flow path); slope; TWI; basins labelled by outlet.
- **Validation against published basin areas.** The modelled Chaliyar basin is 2820 km² against India-WRIS's
  about 2,933 km² (−3.9%). The modelled Kuttiyadi basin is 641.5 km² against 657 km² (−2.4%). Of the 27
  basins that drain the district, none is an internal sink and one (a 7 km² Kabini headwater) is truncated
  at the domain edge.

## 2. Sentinel-2 composite and indices (`earthsim run s2_indices`)

- **Scenes.** 12 L2A scenes (collection 1, baseline 05.11), Jan–Mar 2025, cloud below 30%, the 3 lowest-cloud
  scenes per MGRS tile. Reflectance is computed as DN × scale + offset, using each asset's own raster metadata,
  so the post-04.00 BOA offset is handled.
- **Masking.** Only SCL classes 4, 5 and 6 (vegetation, not vegetated, water) are kept. Shadow, cloud,
  cirrus, snow, dark-area, unclassified, saturated and no-data pixels are dropped, as are pixels with
  non-positive reflectance.
- **Composite.** Per-pixel median. 99.46% of district pixels have at least one valid observation, and most
  have 2–3.
- **Indices.** NDVI, NDWI, MNDWI, NDBI, NDMI. District NDVI median is 0.73 (p5 0.35, p95 0.87), with no
  values out of range.

## 3. Land cover (`earthsim run landcover`)

- **Features.** 6 bands, 4 indices, elevation and slope. **Labels:** ESA WorldCover 2021, mode-resampled
  to 20 m.
- **Validation.** 5-fold GroupKFold on 5 km spatial blocks (127 blocks, 300k samples), so test pixels are
  never adjacent to training pixels.
- **Comparison.** Random forest against a majority-class baseline and an index-rule baseline. Accuracy alone
  is misleading here because tree cover is 95% of pixels, so kappa and macro-F1 are reported too.

## 4. Sentinel-1 flood observation (`earthsim run flood_obs`)

- **Data.** Planetary Computer `sentinel-1-rtc`, VV gamma-0, read at the 20 m overview and warped to the 30 m
  district grid.
- **References.** Each event scene is compared with the median of 3 dry-season scenes from the **same relative
  orbit**, to avoid geometry differences.
- **Flood rule.** A pixel is flooded when its event backscatter is below T, it dropped by at least 3 dB from
  the reference, and the reference is at or above T. T comes from Otsu thresholding clamped to [−23, −15] dB;
  it hit −15 dB for every scene, which means the histograms are not bimodal.
- **Exclusions.** Permanent water (below T in the reference) and SAR nodata.
- **Limitation.** Open-water C-band misses flooding under canopy and in built-up areas. The extents are
  therefore lower bounds and an incomplete reference.

## 5. Flood susceptibility (`earthsim run flood_susceptibility`)

- **Calibration event.** 2018-08-14 (orbit 63, full district). **Temporal test:** 2019-08-10 (orbit 165,
  eastern 14% of the district).
- **Models.** A HAND threshold, a distance-to-stream threshold, and a logistic model on HAND, log upstream
  area, slope, elevation and distance. The KSDMA 10/25/50-yr maps are external comparators.
- **Leakage control.** 5 km spatial blocks with GroupKFold. Thresholds are tuned only on training blocks of
  the calibration event. The 2019 test is never used for fitting.
- **Metrics.** CSI, precision, recall, flagged fraction and ROC-AUC (logistic only).

## 6. Hydraulic solver (`hydraulics.py`)

- **Scheme.** Local-inertial approximation of the shallow-water equations (Bates, Horritt & Fewtrell 2010),
  the scheme used by LISFLOOD-FP. Fluxes cross the 4 cell faces. The time step is adaptive (CFL α = 0.7),
  with Manning friction and a wet/dry threshold of 1 mm.
- **Verification.**
  1. Lake at rest: velocities stay exactly 0.
  2. Closed-basin mass conservation: relative error 1.7e-15.
  3. Hunter et al. (2005) analytical wetting front. The front lag scales with Froude number
     (−7.4% / −3.6% / −0.6% for u = 1 / 0.5 / 0.25 m/s) and is insensitive to dx and dt. This is the known
     limitation of the local-inertial scheme at higher Froude numbers.

## 7. Regional hindcast, Aug 2019 (`earthsim run hindcast`)

- **Terrain.** The 30 m DSM is aggregated ×8 to 240 m. Cells on the stream network keep the block minimum
  (channel-preserving); others take the block mean.
- **Two conditioning bugs found and fixed.**
  - v1 (block-mean only) produced 30–46 m artificial pileups in Western Ghats gorges.
  - v2 used 8-connected filling, which leaves cells whose only lower neighbour is diagonal. Those are closed
    pits for a 4-face solver, so v2 peaked at 67 m.
  - v3 carves every D8 diagonal step into a face-connected path (57,505 cells) and then fills with
    4-connected Priority-Flood. It leaves 0 face pits. v1 and v2 outputs are kept for comparison.
- **Forcing.** Spatially uniform ERA5 hourly rainfall, times a runoff coefficient (0.7, with 0.5 and 0.9 as
  sensitivity runs), from 1 Aug 00:00 IST to the Sentinel-1B overpass on 10 Aug 06:10 IST.
- **Boundaries.** Ocean cells are a sink. Domain edges are open.
- **Evaluation.** Simulated depth at the overpass, at ≥ 0.1 m and ≥ 0.3 m, is compared with the observed SAR
  flood on valid SAR pixels inside the district (`earthsim run hindcast_eval`). Results: CSI 0.013–0.014 and
  recall 0.81–0.97 for runoff coefficients 0.5–0.9, against HAND ≤ 0.5 m at 0.015 and KSDMA-25 at 0.023 on
  the same pixels. Mass closes to about 1e-15 in every run.
- **Known artefact.** There is no Froude limiter, so 393 cells (0.09%) exceed 5 m/s in thin flow on steep
  slopes. Where depth is at least 0.3 m, the 99th-percentile speed is 3.0 m/s.
- **What this is.** A demonstration of the end-to-end chain, not a calibrated flood model. There is no
  infiltration model, no river cross-sections (the DSM sits above the water surface), no tides and no dams.
