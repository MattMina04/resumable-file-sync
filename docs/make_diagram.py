"""Generate the architecture diagram used in the design document."""

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.patches import FancyArrowPatch, FancyBboxPatch

INK = "#1f2933"
EDGE = "#7b8794"
CLIENT_FILL = "#eef2f7"
SERVER_FILL = "#f3efe9"
ACCENT = "#3d5a80"

fig, ax = plt.subplots(figsize=(9.4, 3.15), dpi=300)
ax.set_xlim(0, 9.4)
ax.set_ylim(0, 3.15)
ax.axis("off")


def box(x, y, w, h, text, fill, fs=7.0, weight="normal"):
    ax.add_patch(FancyBboxPatch(
        (x, y), w, h, boxstyle="round,pad=0.015,rounding_size=0.05",
        linewidth=0.8, edgecolor=EDGE, facecolor=fill, zorder=2))
    ax.text(x + w / 2, y + h / 2, text, ha="center", va="center",
            fontsize=fs, color=INK, zorder=3, linespacing=1.35, fontweight=weight)


def arrow(x1, y1, x2, y2, style="-|>", colour=EDGE, lw=0.9, rad=0.0, ls="-"):
    ax.add_patch(FancyArrowPatch(
        (x1, y1), (x2, y2), arrowstyle=style, mutation_scale=8,
        linewidth=lw, color=colour, zorder=1, linestyle=ls,
        connectionstyle=f"arc3,rad={rad}"))


# ---- lanes -----------------------------------------------------------------
ax.add_patch(FancyBboxPatch((0.05, 1.72), 9.3, 1.32,
                            boxstyle="round,pad=0.01,rounding_size=0.04",
                            linewidth=0, facecolor="#fafbfc", zorder=0))
ax.add_patch(FancyBboxPatch((0.05, 0.16), 9.3, 1.18,
                            boxstyle="round,pad=0.01,rounding_size=0.04",
                            linewidth=0, facecolor="#fdfbf8", zorder=0))
ax.text(0.14, 2.92, "CLIENT AGENT   unprivileged, one machine", fontsize=6.6,
        color=ACCENT, fontweight="bold", va="center")
ax.text(0.14, 1.20, "COLLECTION SERVER", fontsize=6.6, color="#8a5a2b",
        fontweight="bold", va="center")

# ---- client row ------------------------------------------------------------
y, h = 2.10, 0.62
box(0.12, y, 1.30, h, "Monitored\ndirectories", CLIENT_FILL)
box(1.62, y, 2.05, h, "Event watcher for latency\n+ periodic stat scan for truth", CLIENT_FILL)
box(3.87, y, 1.55, h, "Local manifest\nSQLite: seen vs synced", CLIENT_FILL)
box(5.62, y, 1.62, h, "Candidate check:\nstat differs → SHA-256", CLIENT_FILL)
box(7.44, y, 1.82, h, "Chunk + upload worker\nretry, backoff, resume", CLIENT_FILL)

for x1, x2 in [(1.42, 1.62), (3.67, 3.87), (5.42, 5.62), (7.24, 7.44)]:
    arrow(x1, y + h / 2, x2, y + h / 2)

# ---- boundary --------------------------------------------------------------
ax.plot([0.05, 9.35], [1.55, 1.55], linestyle=(0, (4, 3)), linewidth=0.8, color=ACCENT)
ax.text(4.70, 1.60, "TLS   ·   authenticated client   ·   per-client namespace",
        fontsize=6.4, color=ACCENT, ha="center", va="bottom")

arrow(8.35, y, 8.35, 1.08, colour=ACCENT, lw=1.1)
ax.text(8.22, 1.88, "plan → chunks → finalise", fontsize=6.4, color=ACCENT,
        ha="right", va="center")

# ---- server row ------------------------------------------------------------
ys, hs = 0.42, 0.62
box(7.34, ys, 1.92, hs, "Per-chunk SHA-256\nverified on arrival", SERVER_FILL)
box(5.10, ys, 2.02, hs, "Append to temp part file\nlength = resume offset", SERVER_FILL)
box(2.86, ys, 2.02, hs, "Finalise: whole-file\nSHA-256 + size check", SERVER_FILL)
box(0.12, ys, 2.52, hs, "Atomic publish\nblob store + path index", SERVER_FILL)

for x1, x2 in [(7.34, 7.12), (5.10, 4.88), (2.86, 2.64)]:
    arrow(x1, ys + hs / 2, x2, ys + hs / 2)

ax.text(3.87, 0.20, "mismatch → discarded, nothing published", fontsize=6.2,
        color="#a03e2f", ha="center", va="center", style="italic")

arrow(0.55, ys + hs, 0.55, 2.10, colour="#4a7c59", lw=1.1, rad=0.0)
ax.text(0.68, 1.87, "commit confirmed → mark synced", fontsize=6.4,
        color="#4a7c59", ha="left", va="center")

fig.savefig("/root/resumable-file-sync/docs/architecture.png",
            bbox_inches="tight", pad_inches=0.06, facecolor="white")
print("written")
