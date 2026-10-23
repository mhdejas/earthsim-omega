# CWC river gauge data (real observations)

Files are stored locally under `data/raw/cwc/` (not committed because of size); the event-window extracts used for scoring are in `experiments/results/gauges/`.

Retrieved 2026-10-23 from the National Water Data Portal (NWDP), National Water Informatics Centre,
Ministry of Jal Shakti. Producer: Central Water Commission (CWC).

| File | Source URL |
|---|---|
| `kl_discharge_daily_2001_2025.csv` (39.4 MB) | https://nwdp.nwic.gov.in/dataset/08fa3fd0-7861-471d-a295-27c1b239d1fa/resource/c3cbe74d-2442-4496-aab2-21e8b5faa12e/download/river_discharge_manual_daily_cwc_kl_2001_2025.csv |
| `rwl_manual_hr_cwc_031_1991_2020.csv` (259.7 MB, basin 031 "West flowing rivers from Tadri to Kanyakumari") | https://nwdp.nwic.gov.in/dataset/d951a09c-6cf8-470e-be77-e80116f13d34/resource/c94d9352-442c-4e99-ade4-537eebc251f4/download/rwl_manual_hr_cwc_031_1991_2020.csv |
| `wl_hourly_kozhikode_stations_1991_2020.csv` | subset of the file above (KUTTYADI, THOTTATHINKADAVU) |

Dataset pages: https://nwdp.nwic.gov.in/dataset/river-discharge-manual-dailly-central-water-commission-cwc ,
CKAN API https://nwdp.nwic.gov.in/api/3/action/package_show?id=river-water-level-manual-hourly-cwc-subernarekha

Station metadata: CWC "Details of Hydrological Observation Stations",
https://www.cwc.gov.in/sites/default/files/hydrological-network-details-of-cwc.pdf (p.35):
- Kuniyil, Malappuram, Chaliyar, catchment 1876 km2, GDSQ, 11°14'22"N 76°01'24"E, code KB000J3
- Kuttyadi, Kozhikode, Kuttyadi, catchment 238 km2, GDQ, 11°37'30"N 75°47'04"E, code KK000N2
  (NWDP lists Kuttyadi at 11.650833N 75.758333E, about 3.7 km away from the CWC PDF position)
- Karathodu, Malappuram, Kadalundi, 750 km2

Coverage found:
- Daily discharge (08:00-09:00 reading): KUNIYIL 2001-2025 and KUTTYADI 2001-2025 include Aug 2018 and Aug 2019
  (gaps on some days). THOTTATHINKADAVU (Iruvazhinjipuzha/Chaliyar) and PERUVAMPADAM start June 2019.
- Hourly water level: KUTTYADI 2009-2020 (both floods). THOTTATHINKADAVU from 2019-06-01.
  KUNIYIL is NOT in the hourly water-level file. RL_of_zeroGauge for KUTTYADI listed as 0.0 (MeanSeaLevel 8.0),
  so levels are used as relative stage only.
- Peaks: KUNIYIL 3236 m3/s on 2018-08-16, 2710 m3/s on 2019-08-08. KUTTYADI Q 480 m3/s 2018-08-09, 333 m3/s 2019-08-08;
  KUTTYADI stage 8.36 m 2018-08-16 07:00, 8.70 m 2019-08-08 22:00.

2018 water-balance diagnostic (computed before any 2019 analysis): 1-20 Aug 2018 observed runoff at KUNIYIL
= 1150 mm over 1876 km2; KUTTYADI = 924 mm (11 of 20 days observed). ERA5 uniform district rain over the same
period = 539 mm.

Regulation upstream of KUTTYADI (affects how its flows can be compared with a rain-only model):
- Peruvannamuzhi Dam (Kuttiyadi Irrigation Project), 11°36'45"N 75°49'20"E, catchment 108.78 km2, about 4 km
  upstream of the CWC gauge, i.e. 46 % of the gauge's 238 km2 catchment
  (Kerala Irrigation Dept, https://irrigation.kerala.gov.in/kuttiyadi-irrigation-project).
- Kakkayam Dam / Kuttiyadi main power house (225 MW, KSEB): tail water rejoins the river and feeds the
  Peruvannamuzhi project (https://en.wikipedia.org/wiki/Kakkayam_Dam ; KSEB O&M manual
  https://kseb.in/uploads/Downloadtemsuppy/O&M%20Kuttiyadi-16977843181748652933.pdf).
KUNIYIL (Chaliyar): no large storage dam above the gauge was found in this check (not exhaustive), so it is treated as the cleaner test.
