# Data sources, provenance and licensing

The machine-readable manifest is `configs/datasets.yaml`. Each acquisition writes
`data/raw/<id>/_acquisition.json` with the URLs, file sizes, sha256 checksums, query parameters and
timestamps. Each processing stage writes `data/processed/<stage>/provenance.json` with input checksums,
parameters, software versions and the git state.

| Dataset id | What | Source | Class | Licence / attribution |
|---|---|---|---|---|
| `aoi_kozhikode_geoboundaries` | District boundary (2367.75 km² in EPSG:32643) | [geoBoundaries IND ADM2, commit 9469f09](https://github.com/wmgeolab/geoBoundaries) | reference | ODbL 1.0; geoBoundaries (Runfola et al. 2020) |
| `copernicus_dem_glo30` | 6 tiles N10–N12 × E075–E076, 30 m **DSM**, EGM2008 | [AWS Open Data copernicus-dem-30m](https://copernicus-dem-30m.s3.amazonaws.com/) | observation | Copernicus DEM licence; © DLR/Airbus, provided under COPERNICUS by EU/ESA |
| `esa_worldcover_2021` | v200 tile N09E075, 10 m | [ESA WorldCover on AWS](https://esa-worldcover.s3.eu-central-1.amazonaws.com/) | model-derived product | CC BY 4.0; © ESA WorldCover project 2021 |
| `sentinel2_l2a_2025q1` | 12 L2A scenes, 2025-01-05 to 03-11, tiles 43PEN/PEP/PFN/PFP, baseline 05.11 | [Earth Search STAC](https://earth-search.aws.element84.com/v1), `sentinel-2-c1-l2a` | observation | Copernicus Sentinel terms; "Contains modified Copernicus Sentinel data 2025" |
| `sentinel1_rtc_floods` | 7 RTC scenes (VV), orbits 63 and 165, events in Aug 2018/2019 and Jan 2019 references | [Planetary Computer STAC](https://planetarycomputer.microsoft.com/api/stac/v1), `sentinel-1-rtc` | observation | CC BY 4.0 (PC terms); modified Copernicus Sentinel data |
| `era5_rain_kozhikode_2018_2019` | Hourly precipitation, Aug 2018 and Aug 2019, 5 points that resolve to 2 ERA5 cells | [Open-Meteo archive API](https://archive-api.open-meteo.com/v1/archive) (`models=era5`) | reanalysis (model) | ERA5 Copernicus licence (Hersbach et al. 2023); Open-Meteo CC BY 4.0 |
| `osm_kozhikode` | 3565 road ways, 3520 facilities, 240,990 building centroids | Overpass API | volunteered observation | ODbL 1.0; © OpenStreetMap contributors |
| `ksdma_flood_hazard` | 10/25/50-yr flood hazard rasters (about 90 m, units undocumented) | Local copy from earlier Kozhikode work (KSDMA) | **external model** | Terms not stated. Internal research use only; do not redistribute |
| `landsat_ndvi_1990_2025` | 12 three-year Landsat NDVI composites | Earlier project `/workspace/kozhikode_longterm` | observation-derived | USGS Landsat, public domain |

External reference values used for validation:

- Chaliyar basin area of about 2,933 km², from [India-WRIS](https://indiawris.gov.in/wiki/doku.php?id=chaliyar).
- Kuttiyadi basin area of 657 km², from [IJERT 2022](https://www.ijert.org/impact-of-land-use-land-cover-change-on-runoff-in-kuttiyadi-river-basin). Other sources give 583–667 km².

## Caveats

The DEM is a **surface** model, so canopy and buildings bias it upward and inflate HAND in forested valleys.
FABDEM (bare-earth) exists but is CC BY-NC-SA, so it was not used.

WorldCover labels are a product with a stated global accuracy of about 77%, not ground truth. The land-cover
scores therefore measure agreement with WorldCover.

ERA5 at 0.25° does not resolve Western Ghats orographic rainfall. IMD gridded rain or station data would be
better but were not available through open, keyless endpoints in this session.

The KSDMA rasters' value semantics and licence are undocumented, so they are used only as binary footprints
for comparison.
