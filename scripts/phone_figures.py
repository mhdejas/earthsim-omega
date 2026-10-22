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
