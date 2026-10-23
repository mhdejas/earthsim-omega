"""Phone-readable figures for the CWC gauge test (real gauge readings vs the solver).

    python scripts/gauge_figures.py calib   # 2018 calibration hydrographs for the grid
    python scripts/gauge_figures.py test    # 2019 held-out hydrographs + flood-map CSI comparison
Outputs go to figures/phone/. Set GAUGE_FIGURES_COPY_DIR to also copy them to a snapshot folder.
"""
import json
import os
import shutil
import sys
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.dates as mdates  # noqa: E402
import matplotlib.pyplot as plt  # noqa: E402
import pandas as pd  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from earthsim.config import load_settings  # noqa: E402
from earthsim.gauges import obs, res_dir, run_dir, tag_for  # noqa: E402

SHOTS = Path(os.environ["GAUGE_FIGURES_COPY_DIR"]) if os.environ.get("GAUGE_FIGURES_COPY_DIR") else None
OUT = ROOT / "figures" / "phone"
LAB = {"KUNIYIL": "Chaliyar at Kuniyil (1,876 km²)", "KUTTYADI": "Kuttiyadi at Kuttyadi (238 km², dam-regulated)",
       "THOTTATHINKADAVU": "Iruvazhinjipuzha at Thottathinkadavu (new test gauge)"}


def _save(fig, name):
    OUT.mkdir(parents=True, exist_ok=True)
    p = OUT / name
    fig.savefig(p, dpi=130)
    plt.close(fig)
    if SHOTS is not None and SHOTS.exists():
        shutil.copy(p, SHOTS / name)
    print(p)


def _series(s, tag):
    return pd.read_csv(run_dir(s) / tag / "gauge_series.csv", parse_dates=["hour_start_ist"]).set_index("hour_start_ist")


def calib(s):
    sel = json.loads((res_dir(s) / "gauge_selection.json").read_text())
    ch = sel["chosen"]
    fig, ax = plt.subplots(2, 1, figsize=(8, 8.5), constrained_layout=True)
    for a, g in zip(ax, ("KUNIYIL", "KUTTYADI")):
        o = obs(s, "discharge", "2018", g)
        for f, col in ((0.9, "#9aa9bd"), (1.25, "#7f97c2"), (1.75, "#4f72b8"), (2.5, "#1c3f8f")):
            ser = _series(s, tag_for("2018", f, ch["n_land"]))
            a.plot(ser.index, ser[f"{g}_q_m3s"], color=col, lw=1.6 if f == ch["factor"] else 1.0,
                   label=f"model, rain factor {f}" + (" (chosen)" if f == ch["factor"] else ""))
        a.plot(o.index, o.values, "o", color="#c0392b", ms=6, label="CWC gauge reading", zorder=5)
        a.set_title(LAB[g], fontsize=12, fontweight="bold")
        a.set_ylabel("river flow (m³/s)", fontsize=11)
        a.grid(alpha=.3)
        a.xaxis.set_major_formatter(mdates.DateFormatter("%d %b"))
    ax[0].legend(fontsize=9, loc="upper left")
    fig.suptitle("Calibration year, August 2018: model river flow vs real gauge readings\n"
                 f"chosen by gauge KGE: rain factor {ch['factor']}, land roughness {ch['n_land']}", fontsize=11)
    _save(fig, "gauge_calibration_2018.png")


def test(s):
    t = json.loads((res_dir(s) / "gauge_test_2019.json").read_text())
    sets = t["settings"]
    names = {"gauge_selected": "gauge-calibrated", "sar_selected": "old SAR-calibrated", "prior": "uncalibrated"}
    cols = {"gauge_selected": "#1c3f8f", "sar_selected": "#8fa3c7", "prior": "#bbbbbb"}
    gs = [g for g in ("KUNIYIL", "KUTTYADI", "THOTTATHINKADAVU") if "discharge" in sets["gauge_selected"]["gauges"].get(g, {})]
    fig, ax = plt.subplots(len(gs), 1, figsize=(8, 3.2 * len(gs) + 0.8), constrained_layout=True)
    for a, g in zip(ax, gs):
        for k in ("prior", "sar_selected", "gauge_selected"):
            v = sets[k]
            ser = _series(s, tag_for("2019", v["factor"], v["n_land"]))
            kg = v["gauges"][g]["discharge"]["kge"]
            a.plot(ser.index, ser[f"{g}_q_m3s"], color=cols[k], lw=1.8 if k == "gauge_selected" else 1.1,
                   label=f"{names[k]} (KGE {kg:.2f})")
        o = obs(s, "discharge", "2019", g)
        a.plot(o.index, o.values, "o", color="#c0392b", ms=6, label="CWC gauge reading", zorder=5)
        a.set_title(LAB[g], fontsize=12, fontweight="bold")
        a.set_ylabel("river flow (m³/s)", fontsize=11)
        a.grid(alpha=.3)
        a.legend(fontsize=9, loc="upper right")
        a.xaxis.set_major_formatter(mdates.DateFormatter("%d %b"))
    fig.suptitle("Held-out test, August 2019 (never used for tuning)\nmodel river flow vs real CWC gauge readings",
                 fontsize=11)
    _save(fig, "gauge_test_2019.png")

    fig, a = plt.subplots(figsize=(8, 4.8), constrained_layout=True)
    rows = [(names[k], sets[k]["sar"]["csi"], sets[k]["sar_bootstrap95"]["csi"], cols[k]) for k in
            ("prior", "sar_selected", "gauge_selected")]
    rows.append(("KSDMA 25-yr hazard map", t["reference_existing"]["ksdma_25yr_csi"], None, "#d4a017"))
    for i, (n, v, ci, c) in enumerate(rows):
        a.barh(i, v, color=c)
        if ci:
            a.plot(ci, [i, i], color="k", lw=1.5)
        a.text(v + 0.001, i + 0.25, f"{v:.3f}", fontsize=11)
    a.set_yticks(range(len(rows)), [r[0] for r in rows], fontsize=11)
    a.set_xlabel("CSI vs radar-observed flooding, 10 Aug 2019 (higher is better; line = 95% range)", fontsize=10)
    a.set_title("Flood-map skill on the held-out 2019 event", fontsize=12, fontweight="bold")
    a.grid(alpha=.3, axis="x")
    _save(fig, "gauge_flood_map_csi_2019.png")


if __name__ == "__main__":
    s = load_settings()
    {"calib": calib, "test": test}[sys.argv[1]](s)
