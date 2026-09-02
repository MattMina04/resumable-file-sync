"""Makes the two diagrams for the design document.

    python3 make_diagrams.py

high-level.png is the overall picture, low-level.png is the message flow.
"""

import matplotlib
matplotlib.use("Agg")

import matplotlib.pyplot as plt
from matplotlib.patches import FancyArrowPatch, FancyBboxPatch, Rectangle

INK = "#1b2733"
MUTED = "#5b6b7f"
ACCENT = "#2f4a72"
WARM = "#8a5a2b"
GREEN = "#3a6b52"


def box(ax, x, y, w, h, text, fs=10, fc="#ffffff", ec=ACCENT, tc=INK, weight="normal"):
    ax.add_patch(FancyBboxPatch(
        (x, y), w, h, boxstyle="round,pad=0,rounding_size=0.03",
        facecolor=fc, edgecolor=ec, linewidth=1.2, zorder=2))
    ax.text(x + w / 2, y + h / 2, text, ha="center", va="center", fontsize=fs,
            color=tc, zorder=3, linespacing=1.4, fontweight=weight)


def arrow(ax, x1, y1, x2, y2, colour=ACCENT, lw=1.4):
    ax.add_patch(FancyArrowPatch(
        (x1, y1), (x2, y2), arrowstyle="-|>", mutation_scale=12,
        color=colour, linewidth=lw, zorder=4, shrinkA=0, shrinkB=0))


# ------------------------------------------------------------- the big picture
fig, ax = plt.subplots(figsize=(8.6, 2.4))
ax.set_xlim(0, 10)
ax.set_ylim(0, 2.6)
ax.axis("off")

ax.add_patch(Rectangle((0.05, 0.1), 3.5, 2.4, facecolor="#f5f7fa",
                       edgecolor="#dbe1ea", linewidth=1, zorder=0))
ax.text(0.22, 2.3, "YOUR MACHINE", fontsize=8, color=MUTED, fontweight="bold")

box(ax, 0.3, 1.62, 3.0, 0.48, "Watched folders")
box(ax, 0.3, 0.92, 3.0, 0.48, "Sync agent", fc="#eef3fa", weight="bold")
box(ax, 0.3, 0.25, 3.0, 0.48, "What the server has", fs=9, tc=MUTED, ec="#c8d2de")

arrow(ax, 1.8, 1.62, 1.8, 1.42, colour="#9fb0c6")
arrow(ax, 1.8, 0.92, 1.8, 0.75, colour="#9fb0c6")

ax.text(5.0, 1.72, "slow link", fontsize=10, color=WARM, ha="center", fontweight="bold")
arrow(ax, 3.65, 1.35, 6.35, 1.35, colour=WARM, lw=1.8)
ax.text(5.0, 0.92, "only what is new,\nin small pieces", fontsize=9, color=MUTED,
        ha="center", linespacing=1.45)

ax.add_patch(Rectangle((6.45, 0.1), 3.5, 2.4, facecolor="#faf7f3",
                       edgecolor="#e6ddd2", linewidth=1, zorder=0))
ax.text(6.62, 2.3, "COLLECTION SERVER", fontsize=8, color=WARM, fontweight="bold")

box(ax, 6.7, 1.62, 3.0, 0.48, "Checks each file matches", fs=9.5, ec=GREEN, fc="#eef5f1")
box(ax, 6.7, 0.92, 3.0, 0.48, "Saves it", fs=9.5, ec="#d8c7b2")
box(ax, 6.7, 0.25, 3.0, 0.48, "One copy of each file", fs=9, tc=MUTED, ec="#d8c7b2")

arrow(ax, 8.2, 1.62, 8.2, 1.42, colour="#c9b49b")
arrow(ax, 8.2, 0.92, 8.2, 0.75, colour="#c9b49b")

fig.savefig("high-level.png", dpi=220, bbox_inches="tight", facecolor="white")
plt.close(fig)

# ------------------------------------------------------------ the message flow
fig, ax = plt.subplots(figsize=(9.0, 2.9))
ax.set_xlim(0, 10)
ax.set_ylim(0.0, 3.15)
ax.axis("off")

LX, RX = 2.0, 8.0
box(ax, 0.9, 2.72, 2.2, 0.38, "Client", fs=10, fc="#eef3fa", weight="bold")
box(ax, 6.9, 2.72, 2.2, 0.38, "Server", fs=10, fc="#faf1e6", ec=WARM, weight="bold")

for x in (LX, RX):
    ax.plot([x, x], [0.08, 2.70], color="#aab6c4", linewidth=1,
            linestyle=(0, (3, 3)), zorder=1)


def step(y, text, right=True, colour=ACCENT):
    x1, x2 = (LX, RX) if right else (RX, LX)
    arrow(ax, x1, y, x2, y, colour=colour)
    ax.text((LX + RX) / 2, y + 0.10, text, fontsize=9, color=INK, ha="center")


step(2.42, "1.  Here is a file: name, size, fingerprint")
step(2.08, "2.  Skip it / I have it already / send from byte N", right=False, colour=WARM)

ax.add_patch(Rectangle((1.3, 1.08), 7.4, 0.80, facecolor="#f7f9fc",
                       edgecolor="#c8d2de", linewidth=1, linestyle=(0, (4, 3)), zorder=0))
step(1.62, "3.  A 1 MB piece, starting at byte N")
step(1.28, "4.  Got it, I am now at byte N + 1 MB", right=False, colour=WARM)
ax.text(1.42, 0.88, "repeats until done, and picks up from that number after a drop",
        fontsize=8.5, color=MUTED, style="italic")

step(0.50, "5.  That is all of it")
step(0.14, "6.  Checked it matches. Saved.", right=False, colour=GREEN)

fig.savefig("low-level.png", dpi=220, bbox_inches="tight", facecolor="white")
plt.close(fig)

print("wrote high-level.png and low-level.png")
