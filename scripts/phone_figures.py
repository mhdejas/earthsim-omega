"""Phone-readable (1080x1350) figures from real EARTHSIM-Omega outputs, each with a data-class badge.

Usage: python scripts/phone_figures.py [catchments|sar|hindcast|all]
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
import numpy as np  # noqa: E402
from matplotlib.colors import LightSource, ListedColormap  # noqa: E402
from matplotlib.patches import Patch, Rectangle  # noqa: E402

from earthsim.geo import read_aoi, read_raster  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
TD = ROOT / "data/processed/terrain"
FD = ROOT / "data/processed/flood"
HD = ROOT / "data/processed/hydraulics"
AOI = ROOT / "data/raw/aoi_kozhikode_geoboundaries/kozhikode_boundary.geojson"
OUT = ROOT / "figures/phone"
BADGE = {"OBSERVED": "#1b7837", "MODELLED": "#2166ac", "SIMULATED": "#b2182b"}
plt.rcParams.update({"font.size": 14, "axes.titlesize": 17, "font.family": "DejaVu Sans"})


def _fig():
    fig = plt.figure(figsize=(7.2, 9.0), dpi=150)
    return fig


def _badge(fig, kind: str, sub: str):
    fig.text(0.04, 0.965, f" {kind} ", fontsize=17, weight="bold", color="white", va="top",
             bbox=dict(boxstyle="round,pad=0.35", fc=BADGE[kind], ec="none"))
    fig.text(0.04, 0.915, sub, fontsize=12.5, va="top", color="#222")


def _footer(fig, text: str):
    fig.text(0.04, 0.012, text, fontsize=9.5, color="#444", va="bottom")


def _outline(ax, crs, **kw):
    g = read_aoi(AOI).to_crs(crs).gdf
    for geom in g.geometry:
        for p in getattr(geom, "geoms", [geom]):
            x, y = p.exterior.xy
            ax.plot(np.asarray(x) / 1000, np.asarray(y) / 1000, **({"color": "k", "lw": 1.3} | kw))


def _ext(grid):
    l, b, r, t = grid.bounds
    return [l / 1000, r / 1000, b / 1000, t / 1000]


def catchments():
    dem, grid, _ = read_raster(TD / "dem.tif")
    oc = read_raster(TD / "ocean_mask.tif")[0].astype(bool)
    bas = read_raster(TD / "basin_outlet_id.tif")[0]
    summ = json.loads((TD / "terrain_summary.json").read_text())
    val = json.loads((ROOT / "experiments/results/basin_area_validation.json").read_text())
    k = 4
    d = np.where(oc, 0, dem)[::k, ::k]
    hs = LightSource(315, 45).hillshade(d, vert_exag=2, dx=30 * k, dy=30 * k)
    b = bas[::k, ::k]
    ocd = oc[::k, ::k]

    def near(lon, lat):
        return min(summ["basins"], key=lambda x: (x["outlet_lon"] - lon) ** 2 + (x["outlet_lat"] - lat) ** 2)

    cha = near(75.8127, 11.1760)
    kut = near(75.5981, 11.5699)
    fig = _fig()
    _badge(fig, "MODELLED", "River catchments traced from the Copernicus 30 m elevation model\n"
                            "(observed satellite DEM), checked against published basin areas")
    ax = fig.add_axes([0.04, 0.27, 0.92, 0.6])
    ax.imshow(np.where(ocd, np.nan, hs), cmap="gray", extent=_ext(grid), vmin=0, vmax=1)
    ax.imshow(np.where(b == cha["outlet_index"], 1, np.nan), cmap=ListedColormap(["#4393c3"]), alpha=0.55,
              extent=_ext(grid), interpolation="nearest")
    ax.imshow(np.where(b == kut["outlet_index"], 1, np.nan), cmap=ListedColormap(["#f4a582"]), alpha=0.65,
              extent=_ext(grid), interpolation="nearest")
    _outline(ax, grid.crs, color="#b2182b", lw=1.8)
    ax.set_xticks([]); ax.set_yticks([])
    ax.legend(handles=[Patch(color="#4393c3", label="Chaliyar basin"), Patch(color="#f4a582", label="Kuttiyadi basin"),
                       Patch(fc="none", ec="#b2182b", lw=1.8, label="Kozhikode district")],
              loc="upper right", fontsize=11.5, framealpha=0.9)
    rows = {r["river"]: r for r in val["results"]}
    lines = []
    for name, bb in (("Chaliyar", cha), ("Kuttiyadi", kut)):
        r = rows.get(name, {})
        pub = r.get("published_area_km2")
        lines.append(f"{name}: modelled {bb['area_km2']:,.0f} km²" +
                     (f"  vs published {pub:,.0f} km²  ({100 * (bb['area_km2'] - pub) / pub:+.1f}%)" if pub else ""))
    fig.text(0.04, 0.215, "\n".join(lines), fontsize=13.5, va="top", weight="bold", color="#222")
    fig.text(0.04, 0.135, "Every land cell drains to the sea or the domain edge (0 artificial sinks).\n"
                          "The DEM is a surface model, so trees and buildings bias it upward.", fontsize=11.5,
             va="top", color="#333")
    _footer(fig, "EARTHSIM-Ω · Copernicus GLO-30 DEM © DLR e.V./Airbus, provided under COPERNICUS\n"
                 "Published areas: India-WRIS (Chaliyar), IJERT 2022 (Kuttiyadi) · UTM 43N")
    OUT.mkdir(parents=True, exist_ok=True)
    p = OUT / "01_catchments_MODELLED.png"
    fig.savefig(p)
    plt.close(fig)
    return p


def sar():
    c, grid, _ = read_raster(FD / "s1_flood_observed_2018-08-14.tif")
    summ = json.loads((FD / "flood_obs_summary.json").read_text())["events"]["2018-08-14"]
    from earthsim.pipelines import crop_to_grid

    dem = crop_to_grid(TD / "dem.tif", grid)
    hs = LightSource(315, 45).hillshade(np.nan_to_num(dem), vert_exag=2, dx=30, dy=30)
    flood = np.isfinite(c) & (c == 2)
    perm = np.isfinite(c) & (c == 3)
    from scipy import ndimage

    # zoom window: 12 km box with the most flooded pixels
    win = 400
    dens = ndimage.uniform_filter(flood.astype("float32"), size=win)
    r0, c0 = np.unravel_index(np.argmax(dens), dens.shape)
    r0, c0 = max(0, r0 - win // 2), max(0, c0 - win // 2)
    fig = _fig()
    _badge(fig, "OBSERVED", f"Sentinel-1 radar, 14 Aug 2018 (Kerala floods): newly flooded open water\n"
                            f"{summ['flooded_km2']} km² flooded beyond {summ['permanent_water_km2']} km² of normal water")
    ax = fig.add_axes([0.03, 0.2, 0.5, 0.68])
    ext = _ext(grid)
    big = ndimage.binary_dilation(flood, iterations=3)
    ax.imshow(hs, cmap="gray", extent=ext, vmin=0, vmax=1, alpha=0.55)
    ax.imshow(np.where(perm, 1, np.nan), cmap=ListedColormap(["#2166ac"]), extent=ext, interpolation="nearest")
    ax.imshow(np.where(big, 1, np.nan), cmap=ListedColormap(["#e6550d"]), extent=ext, interpolation="nearest")
    _outline(ax, grid.crs, lw=1.0)
    l, t = grid.transform * (c0, r0)
    ax.add_patch(Rectangle((l / 1000, (t - win * 30) / 1000), win * 30 / 1000, win * 30 / 1000, fill=False, ec="k", lw=2))
    ax.set_xticks([]); ax.set_yticks([])
    ax.set_title("Whole district\n(flood pixels enlarged ×7)", fontsize=12.5)
    ax2 = fig.add_axes([0.55, 0.36, 0.42, 0.42])
    sl = (slice(r0, r0 + win), slice(c0, c0 + win))
    ax2.imshow(hs[sl], cmap="gray", vmin=0, vmax=1, alpha=0.55)
    ax2.imshow(np.where(perm[sl], 1, np.nan), cmap=ListedColormap(["#2166ac"]), interpolation="nearest")
    ax2.imshow(np.where(flood[sl], 1, np.nan), cmap=ListedColormap(["#e6550d"]), interpolation="nearest")
    ax2.set_xticks([]); ax2.set_yticks([])
    ax2.set_title("12 km zoom, true 30 m pixels", fontsize=12.5)
    fig.legend(handles=[Patch(color="#e6550d", label="flooded (radar change)"),
                        Patch(color="#2166ac", label="normal water (dry season)")],
               loc="lower right", bbox_to_anchor=(0.97, 0.21), fontsize=11.5, framealpha=0.95)
    fig.text(0.04, 0.17, "Rule: backscatter fell ≥ 3 dB vs same-orbit Jan 2019 scenes and is below −15 dB.\n"
                         "Radar cannot see water under trees or between buildings, so this is a\n"
                         "lower bound, not the full flood.", fontsize=11.5, va="top", color="#333")
    _footer(fig, "EARTHSIM-Ω · Contains modified Copernicus Sentinel data 2018-2019\n"
                 "RTC via Microsoft Planetary Computer · hillshade: Copernicus GLO-30 · UTM 43N")
    OUT.mkdir(parents=True, exist_ok=True)
    p = OUT / "02_sar_flood_2018-08-14_OBSERVED.png"
    fig.savefig(p)
    plt.close(fig)
    return p


def hindcast(tag: str = "base"):
    h, grid, _ = read_raster(HD / f"{tag}_depth_at_overpass.tif")
    hm, _, _ = read_raster(HD / f"{tag}_depth_max.tif")
    s = json.loads((HD / f"{tag}_summary.json").read_text())
    from earthsim.scenario import coarse_terrain

    ct = coarse_terrain(TD, int(s["params"]["factor"]))
    oc = np.asarray(ct["ocean"], bool)
    hs = LightSource(315, 45).hillshade(np.where(oc, 0, ct["z_raw"]), vert_exag=3, dx=ct["dx"], dy=ct["dx"])
    fig = _fig()
    evp = HD / "hindcast_eval.json"
    ev = json.loads(evp.read_text())["runs"].get(tag, {}) if evp.exists() else {}
    _badge(fig, "SIMULATED", "Rain-driven 2-D flood simulation, 1–10 Aug 2019 (ERA5 rainfall)\n"
                             "Maximum water depth reached, 240 m cells. Not a forecast or hazard map.")
    ax = fig.add_axes([0.04, 0.24, 0.78, 0.63])
    ext = _ext(grid)
    ax.imshow(np.where(oc, np.nan, hs), cmap="gray", extent=ext, vmin=0, vmax=1)
    show = np.where(np.nan_to_num(hm) >= 0.3, hm, np.nan)
    im = ax.imshow(show, cmap="Blues", vmin=0.3, vmax=5, extent=ext, interpolation="nearest")
    _outline(ax, grid.crs, color="#b2182b", lw=1.6)
    l, b, r, t = read_aoi(AOI).to_crs(grid.crs).gdf.total_bounds
    ax.set_xlim((l - 4000) / 1000, (r + 4000) / 1000)
    ax.set_ylim((b - 4000) / 1000, (t + 4000) / 1000)
    ax.set_xticks([]); ax.set_yticks([])
    cb = fig.colorbar(im, cax=fig.add_axes([0.84, 0.3, 0.03, 0.5]), extend="max")
    cb.set_label("max depth (m), shown where ≥ 0.3 m", fontsize=11.5)
    lines = [f"Water balance closes to {s['mass']['relative_residual']:.0e} (relative error)",
             f"ERA5 rain {s['forcing']['total_rain_mm']:.0f} mm in 9 days × runoff coefficient {s['params']['runoff_c']}"]
    if ev:
        m = ev["model"]["depth_at_overpass>=0.3m"]
        hd_ = ev["comparators"]["hand<=0.5m"]
        ks = ev["comparators"]["ksdma_25yr_footprint"]
        lines.append(f"vs radar flood map 10 Aug 2019: caught {100 * m['recall']:.0f}% of observed flood pixels,\n"
                     f"but CSI only {m['csi']:.3f} (simple terrain rule {hd_['csi']:.3f}, KSDMA map {ks['csi']:.3f})")
    fig.text(0.04, 0.21, "\n".join(x for x in lines if x), fontsize=12, va="top", color="#222")
    _footer(fig, "EARTHSIM-Ω · local-inertial solver (Bates et al. 2010) · Copernicus GLO-30 DEM\n"
                 "ERA5 (C3S) via Open-Meteo · uncalibrated: no infiltration, channel bathymetry, tides or dams")
    OUT.mkdir(parents=True, exist_ok=True)
    p = OUT / "03_hindcast_aug2019_SIMULATED.png"
    fig.savefig(p)
    plt.close(fig)
    return p


if __name__ == "__main__":
    what = sys.argv[1] if len(sys.argv) > 1 else "all"
    for name, fn in (("catchments", catchments), ("sar", sar), ("hindcast", hindcast)):
        if what in (name, "all"):
            print(fn())


def fig_trigger_replay(root=None):
    """Three monsoons: ERA5 district-mean daily rain vs the archived 1-day-ahead ECMWF forecast, 50 mm trigger."""
    import json
    from datetime import date
    from pathlib import Path
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    import numpy as np
    root = Path(root or Path(__file__).resolve().parents[1])
    r = json.loads((root / "data/processed/warning/rain_trigger_verification.json").read_text())
    T = r["trigger_tuning_loyo"]["by_lead"]["day1"]["deploy_T_mm_all_years"]
    v = r["trigger_tuning_loyo"]["by_lead"]["day1"]["pooled_held_out"]
    fig, axes = plt.subplots(3, 1, figsize=(10.8, 13.5), dpi=100)
    fig.subplots_adjust(top=0.82, bottom=0.08, left=0.09, right=0.97, hspace=0.32)
    fig.text(0.5, 0.975, "Would a rain trigger have warned Kozhikode?", ha="center", va="top", fontsize=22, weight="bold")
    fig.text(0.5, 0.94, f"Archived ECMWF forecasts issued 1 day ahead vs ERA5 rain, June-September 2024-2026.\n"
             f"Warn if forecast >= {T} mm; event = ERA5 district mean >= 64.5 mm (IMD 'heavy').", ha="center",
             va="top", fontsize=12.5)
    fig.text(0.5, 0.893, f"Held-out result (leave-one-monsoon-out): caught {v['hits']} of {v['hits'] + v['misses']} heavy days, "
             f"{v['false_alarms']} false alarms", ha="center", va="top", fontsize=13.5, weight="bold", color="#b71c1c")
    for ax, y in zip(axes, ("2024", "2025", "2026")):
        days = [d for d in sorted(r["daily"]) if d[:4] == y and r["daily"][d]["precipitation_previous_day1"] is not None]
        x = np.array([date.fromisoformat(d).toordinal() for d in days])
        ob = np.array([r["daily"][d]["era5"] for d in days])
        fc = np.array([r["daily"][d]["precipitation_previous_day1"] for d in days])
        ax.bar(x, ob, width=1.0, color="#90a4ae", label="ERA5 rain (reanalysis)")
        ax.plot(x, fc, color="#1565c0", lw=1.4, label="forecast issued 1 day ahead")
        ax.axhline(64.5, color="#555", ls="--", lw=1); ax.axhline(T, color="#ef6c00", ls=":", lw=1.4)
        ev, wr = ob >= 64.5, fc >= T
        ax.scatter(x[ev & wr], ob[ev & wr] + 4, marker="v", s=70, color="#2e7d32", label="hit", zorder=3)
        ax.scatter(x[ev & ~wr], ob[ev & ~wr] + 4, marker="x", s=70, color="#c62828", label="missed", zorder=3)
        ax.scatter(x[~ev & wr], fc[~ev & wr] + 4, marker="o", s=30, facecolors="none", edgecolors="#ef6c00",
                   label="false alarm", zorder=3)
        if y == "2024":
            i = days.index("2024-07-30")
            ax.annotate("30 Jul 2024 (Vilangad landslides):\nforecast 29 mm, ERA5 69 mm", (x[i], ob[i] + 6),
                        (x[i] + 8, 112), fontsize=10, arrowprops=dict(arrowstyle="->"))
        ax.set_ylim(0, 130); ax.set_xlim(x[0] - 1, x[-1] + 1)
        ticks = [date(int(y), m, 1).toordinal() for m in (6, 7, 8, 9)]
        ax.set_xticks(ticks, ["Jun", "Jul", "Aug", "Sep"])
        ax.set_title(y, loc="left", fontsize=14, weight="bold"); ax.set_ylabel("mm per day (08:30 IST)")
        ax.text(x[-1], 66, "IMD heavy 64.5", ha="right", fontsize=9); ax.text(x[-1], T - 8, f"trigger {T}", ha="right",
                                                                               fontsize=9, color="#ef6c00")
    h, l = axes[0].get_legend_handles_labels()
    fig.legend(h, l, loc="upper center", bbox_to_anchor=(0.5, 0.862), ncol=5, fontsize=10, frameon=False)
    fig.text(0.5, 0.02, "REAL DATA, OBSERVED vs FORECAST. Rain = mean of the two 0.25 deg cells over Kozhikode (Open-Meteo "
             "previous-runs + ERA5 archive).\nGridded rain smooths local downpours; this is an experimental check, not "
             "an IMD verification. Markers use the final 50 mm trigger.", ha="center", fontsize=9.5, color="#333")
    out = root / "figures/phone/05_rain_trigger_replay_OBSERVED_vs_FORECAST.png"
    fig.savefig(out); plt.close(fig)
    return out


if __name__ == "__main__" and "replay" in __import__("sys").argv:
    print(fig_trigger_replay())


def fig_calibration_test():
    """Held-out flood-extent scores: calibrated solver vs baselines on two SAR events (CSI, block-bootstrap CI)."""
    d = json.load(open(ROOT / "data/processed/calibration/calibration_test.json"))
    sel = d["selected"]
    fig = _fig()
    _badge(fig, "SIMULATED", "Does calibrating the flood model help? Scored against\n"
                             "Sentinel-1 radar flood maps it never saw during calibration.")
    order = [("calibrated", "Solver, calibrated on\n14 Aug 2018", "#b2182b"),
             ("uncalibrated_default_C0.7_n0.08_0.3m", "Solver, default\nsettings", "#ef8a62"),
             ("hand<=0.5m", "Terrain rule\nHAND ≤ 0.5 m", "#2166ac"),
             ("ksdma_25yr_footprint", "KSDMA official\n25-yr hazard map", "#555555")]
    events = [("2019-08-10", "10 Aug 2019 (different year: the real test)"),
              ("2018-08-09", "9 Aug 2018 (same season, secondary check)")]
    axes = [fig.add_axes([0.36, 0.495, 0.56, 0.235]), fig.add_axes([0.36, 0.135, 0.56, 0.235])]
    tops = [0.772, 0.418]
    for ax, top, (ev, title) in zip(axes, tops, events):
        v = d["events"][ev]
        vals = [v["calibrated"]["csi"]] + [v["baselines"][k]["csi"] for k, _, _ in order[1:]]
        y = np.arange(len(order))[::-1]
        ax.barh(y, vals, color=[c for _, _, c in order], height=0.62)
        lo, hi = v["calibrated_bootstrap95"]["csi"]
        ax.errorbar(vals[0], y[0], xerr=[[vals[0] - lo], [hi - vals[0]]], fmt="none", ecolor="k", capsize=5, lw=1.4)
        for i, (yi, val) in enumerate(zip(y, vals)):
            xt = (hi if i == 0 else val) + 0.0008
            ax.text(xt, yi, f"{val:.3f}", va="center", fontsize=12, weight="bold" if i == 0 else None)
        ax.set_yticks(y, [lab for _, lab, _ in order], fontsize=11.5)
        ax.set_xlim(0, 0.045); ax.set_xlabel("Critical success index (higher is better; 1 = perfect)", fontsize=10.5)
        ax.tick_params(axis="x", labelsize=10.5); ax.set_xticks([0, 0.01, 0.02, 0.03, 0.04])
        fig.text(0.04, top, title, fontsize=13.5, weight="bold", va="bottom")
        fig.text(0.04, top - 0.006, f"{v['observed_flooded_km2']:.2f} km² seen flooded by radar, scored on "
                 f"{v['reference_pixels']:,} pixels", fontsize=10, color="#444", va="top")
        for s in ("top", "right"):
            ax.spines[s].set_visible(False)
    fig.text(0.04, 0.862, f"Chosen by a rule fixed before any run: runoff {sel['runoff_c']}, Manning n "
             f"{sel['n_land']}, depth ≥ {sel['threshold_m']} m.\nBlack bar = 95% range from resampling 5 km blocks. "
             "Scores are low for every method\nbecause radar sees only open water.", fontsize=10.5, color="#222",
             va="top")
    _footer(fig, "REAL DATA. 12 solver runs (240 m, ERA5 rain) on 14 Aug 2018 → 1 setting → scored on events above.\n"
                 "Calibration helps a little in 2019 (0.014 → 0.019) but the gain is within noise and KSDMA still wins.")
    p = OUT / "04_calibration_heldout_SIMULATED.png"
    fig.savefig(p); plt.close(fig)
    return p


if __name__ == "__main__" and "calibration" in sys.argv:
    print(fig_calibration_test())
