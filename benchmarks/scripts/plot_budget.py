"""Chart: MemDerive held-out accuracy against maintenance tokens, as a share of eager's.

Offline: reads the saved answers and contexts, no model calls. Each point's 95% CI is a bootstrap
over questions (each drawn with all its checkpoints), as in the report.

Run from benchmarks/: uv run --extra bench --with matplotlib python scripts/plot_budget.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import memderive as md
from longmemeval import cluster_bootstrap

ANSWERER = "cursor:composer-2.5"
SYSTEMS = ("facts", "priority_15", "priority_40", "untracked", "eager")
OUT = Path(__file__).resolve().parent.parent / "results" / "memderive_budget.png"
INK, MUTED, GRID, SERIES = "#0b0b0b", "#52514e", "#e4e3df", "#2a78d6"


def accuracy(system: str, worlds: list[md.World]) -> tuple[float, tuple[float, float]]:
    cells = []
    for w in worlds:
        path = md.OUT / "answers" / f"{w.name}__{system}__{md.slug(ANSWERER)}.json"
        cells += md.score_cells(w, md.SPECS[system], md.read_json(path))
    acc = sum(c["correct"] for c in cells) / len(cells)
    lo, hi = cluster_bootstrap([((c["world"], c["qid"]), int(c["correct"]), 0) for c in cells])
    return acc, (lo, hi)


def main() -> int:
    worlds = [md.load_world(w) for w in md.HELDOUT_WORLDS]
    rep = json.loads((OUT.parent / "memderive_heldout_p40.json").read_text())
    full = json.loads((OUT.parent / "memderive_heldout.json").read_text())
    eager_tokens = rep["cost"]["eager"]["maintenance_tokens"]
    points = []
    for s in SYSTEMS:
        acc, (lo, hi) = accuracy(s, worlds)
        share = rep["cost"].get(s, {}).get("maintenance_tokens", 0) / eager_tokens
        points.append((s, share, acc, lo, hi))
    fc = full["by_answerer"][ANSWERER]["full_context"]["accuracy"]["rate"]

    plt.rcParams.update({"font.size": 9, "axes.edgecolor": MUTED, "text.color": INK})
    fig, ax = plt.subplots(figsize=(6.4, 3.8), dpi=200)
    fig.patch.set_facecolor("#fcfcfb")
    ax.set_facecolor("#fcfcfb")
    ax.grid(axis="y", color=GRID, linewidth=0.8)
    ax.set_axisbelow(True)
    for side in ("top", "right", "left"):
        ax.spines[side].set_visible(False)
    ax.tick_params(colors=MUTED, length=0)
    ax.axhline(100 * fc, color=MUTED, linestyle=(0, (4, 3)), linewidth=1.2)
    ax.text(
        0.99,
        100 * fc - 0.2,
        "full_context: no maintenance, 2.7x query tokens",
        transform=ax.get_yaxis_transform(),
        color=MUTED,
        ha="right",
        va="top",
        fontsize=8,
    )
    for name, share, acc, lo, hi in points:
        ax.errorbar(
            100 * share,
            100 * acc,
            yerr=[[100 * (acc - lo)], [100 * (hi - acc)]],
            fmt="o",
            color=SERIES,
            markersize=6,
            elinewidth=1.4,
            capsize=0,
            markeredgecolor="#fcfcfb",
            markeredgewidth=1.5,
        )
        dx, ha = (2.0, "left") if name not in ("untracked",) else (-2.0, "right")
        ax.annotate(name, (100 * share + dx, 100 * acc), color=INK, ha=ha, va="center", fontsize=8)
    ax.set_xlim(-6, 110)
    ax.set_xlabel("Page maintenance tokens, % of eager", color=MUTED)
    ax.set_ylabel("Accuracy, % of 4,267 cells", color=MUTED)
    ax.set_title(
        "MemDerive held-out: accuracy against maintenance cost",
        loc="left",
        pad=16,
        fontsize=10,
        color=INK,
    )
    ax.text(
        0,
        1.01,
        "7 worlds, composer-2.5 answers; bars are 95% CIs over questions",
        transform=ax.transAxes,
        color=MUTED,
        fontsize=8,
        va="bottom",
    )
    fig.tight_layout()
    fig.savefig(OUT, facecolor=fig.get_facecolor())
    for p in points:
        print(
            f"{p[0]:12s} share {100 * p[1]:5.1f}%  acc {100 * p[2]:.1f} "
            f"[{100 * p[3]:.1f}, {100 * p[4]:.1f}]"
        )
    print(f"full_context {100 * fc:.1f}; wrote {OUT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
