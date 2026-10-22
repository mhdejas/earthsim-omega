# Kozhikode flood outlook - EXPERIMENTAL

**EXPERIMENTAL research output - not an official warning. Follow IMD, CWC and KSDMA / Kozhikode DDMA advisories for decisions.**

Issued 2026-10-23T03:31:00 IST - run `20261023T0331_IST`

## Overall experimental level, next 72 h: YELLOW

| Day (24 h to 08:30 IST) | Forecast mm (ensemble p10-p50-p90) | P(heavy >=64.5) | P(very heavy >=115.6) | Level |
|---|---|---|---|---|
| 2026-10-23 (in progress) | 38.0 (12.1-20.2-39.5) | 0% | 0% | GREEN |
| 2026-10-24 | 28.4 (10.2-26.6-57.3) | 4% | 2% | GREEN |
| 2026-10-25 | 44.2 (17.6-38.7-108.8) | 28% | 10% | YELLOW |
| 2026-10-26 | 62.1 (6.0-22.2-73.1) | 16% | 6% | YELLOW |
| 2026-10-27 | 5.4 (0.6-3.2-27.3) | 0% | 0% | GREEN |
| 2026-10-28 | 1.4 (0.2-2.0-19.6) | 2% | 0% | GREEN |
| 2026-10-29 | 1.2 (0.3-2.1-16.5) | 2% | 0% | GREEN |

Rain in the last 5 days (model estimate): 67.4 mm.

## Flood footprint scenarios (rain-on-grid model, calibrated on 14 Aug 2018 radar)

| Scenario | 72 h rain mm | Wet area km2 (peak) | Buildings in wet cells | Major roads km | Schools / hospitals |
|---|---|---|---|---|---|
| deterministic | 142.2 | 62.1 | 1,122 | 42.6 | 17 / 4 |
| ens_p10_precipitation_member44 | 48.3 | 21.0 | 286 | 20.4 | 9 / 1 |
| ens_p50_precipitation_member13 | 93.2 | 45.3 | 793 | 32.7 | 14 / 4 |
| ens_p90_precipitation_member17 | 226.5 | 113.0 | 2,252 | 81.6 | 45 / 10 |

Wet = simulated depth at or above the calibrated threshold, 240 m cells. Building counts are OSM footprints whose centre falls in a wet cell, not damage estimates.

## How much to trust this

Day-1 rain trigger, out-of-sample over three monsoons: hit rate 0.60, false-alarm ratio 0.67, CSI 0.27. Ensemble probability cut-offs are not verified. Flood model: on the 14 Aug 2018 calibration event it predicted about 9x the radar-observed flooded area (radar misses water under trees and between buildings, so the truth lies in between); held-out CSI 0.019 on 10 Aug 2019 is not better than the official KSDMA hazard map. Treat footprints as 'where water collects first', not as a depth forecast. Details: docs/CALIBRATION.md.

The rain trigger was verified on June-September (south-west monsoon) days only; outside that season (for example the October-December north-east monsoon) its skill is unknown.
