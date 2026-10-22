# Model cards

## Land-cover random forest (2025)

- **Purpose.** Pixel classification at 20 m into WorldCover classes from the 2025 Q1 Sentinel-2 composite,
  elevation and slope. This is a demonstration of a spatially honest training and evaluation setup.
- **Training labels.** ESA WorldCover 2021, which is a product, not ground truth (about 77% global accuracy)
  and 4 years older than the imagery.
- **Evaluation.** 5-fold GroupKFold on 5 km blocks, 300k samples. The scores measure **agreement with
  WorldCover**:

| Model | Overall accuracy | Kappa | Macro-F1 (present classes) |
|---|---|---|---|
| Majority class (tree cover) | 0.947 | 0.00 | 0.11 |
| Index rules (NDVI/MNDWI/NDBI) | 0.795 | 0.23 | 0.24 |
| Random forest | 0.970 | 0.68 | 0.46 |

- **Per-class F1 (RF).** Tree cover 0.99, water 0.90, built-up 0.61, grassland 0.47, cropland 0.42,
  bare 0.39, wetland 0.36, shrubland and mangroves 0 (only 1 and 26 samples).
- **Known failure modes.** Kerala homestead agroforestry is labelled tree cover in WorldCover, so built-up
  and cropland are under-represented. Rare classes are not learnable from this sample.
- **Do not use for** official land-use statistics or change accounting.

## Flood susceptibility (HAND / distance / logistic)

- **Purpose.** A baseline indicator of where open-water flooding was observed by SAR, given terrain. It is
  not a hazard or return-period map.
- **Data.** Calibrated on the 2018-08-14 Sentinel-1 flood extent (2.26 km² flooded, 23.6 km² permanent water
  excluded). Tested on 2019-08-10, which covers only the eastern 14% of the district (307 flooded pixels).
- **Spatial CV results (2018):**

| Method | CSI | Recall | Area flagged |
|---|---|---|---|
| HAND ≤ 0.5 m | 0.009 | 0.94 | 9.3% |
| Logistic | 0.008 | 0.59 | — |
| Distance ≤ 90 m | 0.003 | 0.48 | — |
| KSDMA 10/25/50-yr (external) | 0.011–0.012 | 0.81 | 6.2–7.0% |

- **Temporal test (2019).** HAND has CSI 0.015 and recall 0.95. KSDMA-25 has CSI 0.023 and recall 0.96. Distance has CSI 0.003 and recall 0.48.
  Logistic has CSI 0.008.
- **Interpretation.** Recall is high but CSI is very low for every method, KSDMA included. The SAR reference
  is a severe undercount: it sees only open water, misses canopy and urban areas, and the 2018 scene was
  acquired before the rainfall peak. Low precision is therefore expected and is not evidence that the
  susceptibility surface is wrong. The logistic model does **not** beat the simple HAND rule.
- **Do not use for** planning or emergency decisions.

## Regional hindcast, Aug 2019 (local-inertial, 240 m)

See `docs/METHODOLOGY.md` §6–7 and `docs/REPORT.md` for the results and the evaluation against the
2019-08-10 SAR extent. The hindcast is a simulation driven by uniform ERA5 rainfall with an assumed runoff
coefficient. It has no infiltration, channel bathymetry, tides or dams, and it has not been calibrated.
