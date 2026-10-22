"""Static map figures (matplotlib) for EARTHSIM-Omega outputs.

Figures always state data source, CRS, units and data class in the caption.
"""

from __future__ import annotations

import json
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
import numpy as np  # noqa: E402
from matplotlib.colors import LightSource, ListedColormap  # noqa: E402

from .geo import read_aoi, read_raster  # noqa: E402


def _extent(grid):
    l, b, r, t = grid.bounds
    return [l / 1000, r / 1000, b / 1000, t / 1000]


def _decimate(a: np.ndarray, k: int) -> np.ndarray:
    return a[::k, ::k] if k > 1 else a


def _aoi_outline(ax, aoi_path: str | Path, crs: str, **kw):
    g = read_aoi(aoi_path).to_crs(crs).gdf
    for geom in g.geometry:
        polys = getattr(geom, "geoms", [geom])
        for p in polys:
            x, y = p.exterior.xy
            ax.plot(np.asarray(x) / 1000, np.asarray(y) / 1000, **({"color": "k", "lw": 1.2} | kw))


def terrain_figure(terrain_dir: str | Path, aoi_path: str | Path, out: str | Path, k: int = 3) -> Path:
    td = Path(terrain_dir)
    dem, grid, _ = read_raster(td / "dem.tif")
    streams, _, _ = read_raster(td / "streams.tif")
    acc, _, _ = read_raster(td / "flow_accumulation.tif")
    ocean, _, _ = read_raster(td / "ocean_mask.tif")
    basins, _, _ = read_raster(td / "basin_outlet_id.tif")
    hand, _, _ = read_raster(td / "hand.tif")
    summ = json.loads((td / "terrain_summary.json").read_text())
    oc = ocean.astype(bool)
    ext = _extent(grid)
    fig, axs = plt.subplots(1, 2, figsize=(17, 9), dpi=110)
    # Panel A: hillshade + streams + district + basins draining district
    ls = LightSource(azdeg=315, altdeg=45)
    d = _decimate(np.where(oc, 0, dem), k)
    hs = ls.hillshade(d, vert_exag=2, dx=30 * k, dy=30 * k)
    ax = axs[0]
    ax.imshow(np.where(_decimate(oc, k), np.nan, hs), cmap="gray", extent=ext, vmin=0, vmax=1)
    ax.imshow(np.where(_decimate(oc, k), np.nan, d), cmap="terrain", alpha=0.45, extent=ext, vmin=0, vmax=2000)
    keep = [b["outlet_index"] for b in summ["basins"] if b["area_in_district_km2"] >= 50]
    bmask = np.isin(_decimate(basins, k), keep)
    ax.contour(np.flipud(bmask.astype(float)), levels=[0.5], colors="royalblue", linewidths=0.8,
               extent=ext, origin="lower")
    from scipy import ndimage

    big = _decimate(ndimage.maximum_filter(((acc * 900 / 1e6 >= 25) & ~oc).astype("uint8"), size=k + 2) > 0, k)
    ax.imshow(np.where(big, 1, np.nan), cmap=ListedColormap(["#0050ff"]), extent=ext, interpolation="nearest")
    _aoi_outline(ax, aoi_path, grid.crs, color="red", lw=1.4)
    ax.set_title("A. Copernicus GLO-30 DSM hillshade; channels >= 25 km2 (blue);\n"
                 "outlines of basins with >= 50 km2 in district (blue line); district (red)", fontsize=10)
    ax.set_xlabel("Easting (km, EPSG:32643)")
    ax.set_ylabel("Northing (km)")
    # Panel B: HAND clipped to district view
    ax = axs[1]
    hv = _decimate(hand, k)
    im = ax.imshow(np.where(_decimate(oc, k), np.nan, hv), cmap="YlGnBu_r", vmin=0, vmax=40, extent=ext)
    _aoi_outline(ax, aoi_path, grid.crs, color="red", lw=1.2)
    l, b, r, t = read_aoi(aoi_path).to_crs(grid.crs).gdf.total_bounds
    ax.set_xlim((l - 3000) / 1000, (r + 3000) / 1000)
    ax.set_ylim((b - 3000) / 1000, (t + 3000) / 1000)
    plt.colorbar(im, ax=ax, fraction=0.04, label="HAND (m above nearest drainage, 1 km2 network)")
    ax.set_title("B. Height Above Nearest Drainage (derived; DSM-based, canopy bias)", fontsize=10)
    ax.set_xlabel("Easting (km, EPSG:32643)")
    bs = summ["basins_summary"]
    cond = summ["conditioning"]
    fig.text(0.01, 0.01,
             f"Conditioning: {cond['algorithm']}.\nLand cells without downslope receiver: "
             f"{cond['land_cells_without_downslope_receiver']} (pysheds 0.5 crosscheck left "
             f"{cond['pysheds_crosscheck'].get('unresolved_flat_cells_on_land', '?')}+"
             f"{cond['pysheds_crosscheck'].get('unresolved_pit_cells_on_land', '?')} flat+pit cells).  "
             f"Basins draining district: {bs['basins_draining_district']}, internal sinks: {bs['internal_sink_basins']}, "
             f"truncated: {bs['truncated_basins']} ({bs['district_area_in_truncated_basins_km2']} km2 of district).",
             fontsize=8.5)
    fig.tight_layout(rect=(0, 0.05, 1, 1))
    out = Path(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(out)
    plt.close(fig)
    return out


def flood_figure(flood_dir: str | Path, terrain_dir: str | Path, aoi_path: str | Path, out: str | Path,
                 events: tuple[str, ...] = ("2018-08-09", "2018-08-14", "2019-08-10")) -> Path:
    """Observed SAR flood extents (real Sentinel-1) + modelled susceptibility with held-out scores."""
    fd = Path(flood_dir)
    summ = json.loads((fd / "flood_obs_summary.json").read_text())
    ev = json.loads((fd / "flood_susceptibility_eval.json").read_text())
    sc, grid, _ = read_raster(fd / "flood_susceptibility_score.tif")
    ext = _extent(grid)
    from .pipelines import crop_to_grid

    dem = crop_to_grid(Path(terrain_dir) / "dem.tif", grid)
    ls = LightSource(azdeg=315, altdeg=45)
    hs = ls.hillshade(np.nan_to_num(dem), vert_exag=2, dx=30, dy=30)
    cmap = ListedColormap(["#d9d9d9", "#e6550d", "#08519c"])
    fig, axs = plt.subplots(1, len(events) + 1, figsize=(6 * (len(events) + 1), 8.2), dpi=105)
    for ax, e in zip(axs, events):
        c, _, _ = read_raster(fd / f"s1_flood_observed_{e}.tif")
        ax.imshow(hs, cmap="gray", extent=ext, vmin=0, vmax=1, alpha=0.6)
        show = np.where(np.isfinite(c) & (c >= 1), c - 1, np.nan)
        ax.imshow(show, cmap=cmap, vmin=0, vmax=2, extent=ext, interpolation="nearest", alpha=0.85)
        s = summ["events"][e]
        ax.set_title(f"OBSERVED (Sentinel-1 VV) {e}\nflooded {s['flooded_km2']} km2 | threshold "
                     f"{s['threshold_db']} dB", fontsize=10)
        _aoi_outline(ax, aoi_path, grid.crs, color="k", lw=0.7)
        ax.set_xticks([]); ax.set_yticks([])
    ax = axs[-1]
    ax.imshow(hs, cmap="gray", extent=ext, vmin=0, vmax=1, alpha=0.6)
    im = ax.imshow(sc, cmap="YlGnBu", vmin=0, vmax=1, extent=ext, alpha=0.85)
    t = ev["tests"]["2019-08-10"]["test"]
    ax.set_title("MODEL: terrain susceptibility score (not probability/depth)\n"
                 f"held-out 2019 CSI: logistic {t['logistic']['csi']:.2f}, HAND {t['hand_threshold']['csi']:.2f}, "
                 f"KSDMA-25yr {t['ksdma_25yr_footprint']['csi']:.2f}", fontsize=10)
    _aoi_outline(ax, aoi_path, grid.crs, color="k", lw=0.7)
    ax.set_xticks([]); ax.set_yticks([])
    fig.colorbar(im, ax=ax, fraction=0.035, pad=0.01, label="relative susceptibility score")
    from matplotlib.patches import Patch

    axs[0].legend(handles=[Patch(color="#e6550d", label="flooded open water (SAR change)"),
                           Patch(color="#08519c", label="permanent/seasonal water (dry-season ref.)"),
                           Patch(color="#d9d9d9", label="not flooded per SAR")], loc="lower left", fontsize=8)
    fig.suptitle("EARTHSIM-Omega Kozhikode | Observed flood extent vs terrain susceptibility. SAR misses water "
                 "under tree canopy and in built-up areas, so observed extents are conservative. "
                 "Contains modified Copernicus Sentinel data 2018-2019.", fontsize=9.5, y=0.02)
    fig.tight_layout(rect=(0, 0.04, 1, 1))
    out = Path(out)
    fig.savefig(out)
    plt.close(fig)
    return out


def forecast_figure(alert: dict, run_dir, s) -> str:
    """Two-panel experimental outlook: daily rain fan with IMD thresholds, and simulated wet footprint."""
    import json as _json
    from pathlib import Path as _P
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    import numpy as _np
    import rasterio
    colors = {"GREEN": "#2e7d32", "YELLOW": "#f9a825", "ORANGE": "#ef6c00", "RED": "#c62828"}
    f = alert["flood_scenarios"]
    fig = plt.figure(figsize=(10.8, 13.5 if f.get("run") else 7.2), dpi=100)
    fig.patch.set_facecolor("white")
    fig.text(0.5, 0.985, "Kozhikode flood outlook - EXPERIMENTAL, not an official warning", ha="center", va="top",
             fontsize=17, weight="bold", color="#b71c1c")
    fig.text(0.5, 0.985 - 0.42 / fig.get_figheight(), f"Issued {alert['issued_ist'][:16].replace('T', ' ')} IST  |  overall level next 72 h: "
             f"{alert['overall_level_next_72h']}  |  official advisories: IMD, CWC, KSDMA", ha="center", va="top", fontsize=11)
    ax = fig.add_axes([0.09, 0.62 if f.get("run") else 0.12, 0.86, 0.29 if f.get("run") else 0.75])
    days = alert["rain_outlook"]["days"]
    x = _np.arange(len(days))
    for i, d in enumerate(days):
        p10, p50, p90 = d["ensemble_p10_p50_p90_mm"]
        ax.bar(i, p90 - p10, bottom=p10, width=0.6, color=colors[d["rain_level"]], alpha=0.45)
        ax.plot([i - 0.3, i + 0.3], [p50, p50], color="k", lw=2)
        ax.plot(i, d["deterministic_mm"], "D", color="#1565c0", ms=8)
        ax.text(i, p90 + 3, f"P(heavy) {d['prob_heavy']:.0%}", ha="center", fontsize=9)
    for y, lab in ((64.5, "IMD heavy 64.5"), (115.6, "IMD very heavy 115.6"), (alert["rain_outlook"]["trigger_mm"], "verified trigger 50")):
        ax.axhline(y, ls="--", lw=1, color="#555")
        ax.text(len(days) - 0.45, y + 1.5, lab, ha="right", fontsize=9, color="#333")
    ax.set_xticks(x, [d["day_ending_0830_ist"][5:] + ("*" if d["in_progress"] else "") for d in days])
    ax.set_ylabel("District-mean rain, 24 h to 08:30 IST (mm)")
    ax.set_title("ECMWF IFS 51-member ensemble: bar = p10-p90 (colour = experimental level), line = median, "
                 "diamond = deterministic", fontsize=10)
    ax.set_ylim(0, max(130, max(d["ensemble_p10_p50_p90_mm"][2] for d in days) + 15))
    if f.get("run"):
        from .calibration import coarse_terrain_cached
        from .geo import Grid, aoi_mask
        from .pipelines import load_aoi
        ct = coarse_terrain_cached(s)
        ny, nx = ct["z"].shape
        aoi = aoi_mask(load_aoi(s), Grid(ct["crs"], ct["transform"], nx, ny))
        th = f["params"]["wet_threshold_m"]
        mem = f["members"]
        wettest = max(mem, key=lambda k: mem[k]["peak_of_snapshots"]["wet_km2"])
        med = next((k for k in mem if k.startswith("ens_p50")), wettest)
        def peak(name):
            arr = None
            for lab in f["capture_times_ist"]:
                with rasterio.open(_P(run_dir) / "members" / name / f"depth_{lab}.tif") as ds:
                    d = ds.read(1)
                arr = d if arr is None else _np.fmax(arr, d)
            return (_np.nan_to_num(arr) >= th) & aoi
        show = _np.where(aoi, 0, _np.nan)
        show[peak(wettest)] = 1
        show[peak(med)] = 2
        ax2 = fig.add_axes([0.03, 0.05, 0.62, 0.50])
        rr, cc = _np.where(aoi)
        sl = (slice(max(rr.min() - 3, 0), rr.max() + 4), slice(max(cc.min() - 3, 0), cc.max() + 4))
        hs = _np.where(aoi, ct["z"], _np.nan)[sl]
        show = show[sl]; aoi = aoi[sl]
        ax2.imshow(hs, cmap="Greys", alpha=0.35)
        from matplotlib.colors import ListedColormap
        ax2.imshow(show, cmap=ListedColormap(["#f5f5f5", "#64b5f6", "#0d47a1"]), vmin=0, vmax=2, alpha=0.9)
        ax2.contour(aoi.astype(float), levels=[0.5], colors="#b71c1c", linewidths=1)
        ax2.set_axis_off()
        ax2.set_title(f"SIMULATED wet cells (depth >= {th} m) at peak of +24/48/72 h\n"
                      f"dark = ensemble median member, light = wettest scenario", fontsize=11)
        tx = fig.add_axes([0.66, 0.05, 0.32, 0.52]); tx.set_axis_off()
        lines = ["Scenario (72 h rain)", ""]
        for k, m in mem.items():
            pk = m["peak_of_snapshots"]
            lines += [f"{k.split('_member')[0]} ({m['forecast_72h_mm']:.0f} mm)",
                      f"  wet {pk['wet_km2']:.0f} km2, {pk['buildings']:,} bldgs",
                      f"  roads {pk['major_road_km']:.0f} km, schools {pk['facilities'].get('school', 0)},",
                      f"  hospitals {pk['facilities'].get('hospital', 0)}", ""]
        ref = f.get("reference_hindcast_wet_km2", {})
        if ref:
            lines += ["Same model, past floods:"] + [f"  {e}: {v['wet_km2']:.0f} km2" for e, v in ref.items()]
        tx.text(0, 1, "\n".join(lines), va="top", fontsize=10, family="monospace")
    v = alert["verified_skill"]["lead_day1_held_out"]
    fig.text(0.5, 0.012, f"Rain: ECMWF IFS 0.25 deg via Open-Meteo. Day-1 trigger skill (held-out, JJAS 2024-26): hit rate "
             f"{v['pod_hit_rate']:.2f}, false-alarm ratio {v['far_false_alarm_ratio']:.2f}. Probability cut-offs unverified.\n"
             "Flood model: ~9x the radar-observed area on its calibration event; held-out skill not better than the "
             "KSDMA hazard map. Footprints show where water collects first, not depths.",
             ha="center", fontsize=8.5, color="#333")
    out = _P(run_dir) / "outlook.png"
    fig.savefig(out); plt.close(fig)
    return str(out)
