"""Render a milestone evidence snapshot PNG from REAL log/command output and,
optionally, a real output figure produced by the pipeline.

Usage:
  python scripts/snapshot.py NAME "Title" --log logs/x.log [--cmd "earthsim doctor"] [--image fig.png]

The text panel shows the literal bytes of the log file / command output
(tail-truncated if long); nothing is typed in by hand.
"""

from __future__ import annotations

import argparse
import subprocess
import textwrap
from datetime import datetime
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.image as mpimg  # noqa: E402
import matplotlib.pyplot as plt  # noqa: E402

SHOTS = Path("/workspace/earthsim-omega-shots")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("name")
    ap.add_argument("title")
    ap.add_argument("--log", action="append", default=[])
    ap.add_argument("--cmd", action="append", default=[])
    ap.add_argument("--image")
    ap.add_argument("--max-lines", type=int, default=70)
    a = ap.parse_args()

    chunks = []
    for c in a.cmd:
        r = subprocess.run(c, shell=True, capture_output=True, text=True, cwd="/workspace/earthsim-omega",
                           executable="/bin/bash")
        chunks.append(f"$ {c}\n{r.stdout}{r.stderr}".rstrip() + f"\n[exit {r.returncode}]")
    for lf in a.log:
        chunks.append(f"==> {lf} <==\n" + Path(lf).read_text(errors="replace").rstrip())
    lines = []
    for ch in chunks:
        for ln in ch.splitlines():
            lines.extend(textwrap.wrap(ln, 118, subsequent_indent="    ") or [""])
    if len(lines) > a.max_lines:
        lines = [f"... ({len(lines) - a.max_lines} earlier lines truncated) ..."] + lines[-a.max_lines:]

    has_img = bool(a.image and Path(a.image).exists())
    fig = plt.figure(figsize=(22 if has_img else 14, 12), dpi=110)
    if has_img:
        ax_t = fig.add_axes([0.01, 0.02, 0.50, 0.90])
        ax_i = fig.add_axes([0.52, 0.02, 0.47, 0.90])
        ax_i.imshow(mpimg.imread(a.image))
        ax_i.set_axis_off()
        ax_i.set_title(f"Output figure: {a.image}", fontsize=9, family="monospace")
    else:
        ax_t = fig.add_axes([0.01, 0.02, 0.98, 0.90])
    ax_t.set_axis_off()
    ax_t.set_facecolor("#111")
    ax_t.text(0, 1, "\n".join(lines), va="top", ha="left", family="monospace", fontsize=7.6, color="#111")
    stamp = datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S %Z")
    fig.suptitle(f"EARTHSIM-\u03a9 milestone: {a.title}   [{stamp}, captured from real output]",
                 fontsize=13, x=0.01, ha="left", y=0.985)
    SHOTS.mkdir(parents=True, exist_ok=True)
    out = SHOTS / f"{a.name}.png"
    fig.savefig(out, facecolor="white")
    print(out)


if __name__ == "__main__":
    main()
