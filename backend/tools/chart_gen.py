"""Chart generation tool — matplotlib-based data visualization.

Generates real charts (bar, line, pie, scatter, etc.) from structured data.
Returns PNG image path. Supports Chinese labels.
"""
from __future__ import annotations

from pathlib import Path

OUTPUT_DIR = Path(__file__).parent.parent.parent / "outputs"
OUTPUT_DIR.mkdir(exist_ok=True)

# Try to use a CJK font
try:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    import matplotlib.font_manager as fm

    # Find available CJK fonts — prefer Noto CJK (installed in Docker)
    _cjk_fonts = [f.name for f in fm.fontManager.ttflist
                  if any(k in f.name.lower() for k in ("noto sans cjk", "noto cjk", "simhei",
                                                         "microsoft yahei", "wenquanyi", "wqy",
                                                         "zen hei", "source han", "arial unicode"))]
    if _cjk_fonts:
        plt.rcParams["font.family"] = _cjk_fonts[0]
        plt.rcParams["font.sans-serif"] = _cjk_fonts
    else:
        # Fallback: force rebuild font cache
        fm._load_fontmanager(try_read_cache=False)
        plt.rcParams["font.sans-serif"] = ["DejaVu Sans"]
    plt.rcParams["axes.unicode_minus"] = False
    MATPLOTLIB_OK = True
except ImportError:
    MATPLOTLIB_OK = False


async def generate_chart(
    chart_type: str,
    title: str,
    labels: list[str],
    datasets: list[dict],
    x_label: str = "",
    y_label: str = "",
    width: int = 10,
    height: int = 6,
) -> dict:
    """Generate a chart image from structured data.

    Args:
        chart_type: bar | line | pie | scatter | horizontal_bar | area | multi_line
        title: chart title
        labels: x-axis labels (or slice labels for pie)
        datasets: [{"label": "Series1", "values": [1,2,3]}, ...]
        x_label: x-axis label
        y_label: y-axis label
        width: figure width in inches
        height: figure height in inches

    Returns: {url: local_path, error: ...}
    """
    if not MATPLOTLIB_OK:
        return {"error": "matplotlib not available", "url": "", "local_path": ""}

    try:
        fig, ax = plt.subplots(figsize=(width, height))
        colors = ["#6366f1", "#f59e0b", "#10b981", "#ef4444", "#8b5cf6",
                   "#06b6d4", "#f97316", "#84cc16"]

        if chart_type == "pie":
            values = datasets[0]["values"] if datasets else []
            wedges, texts, autotexts = ax.pie(
                values, labels=labels, autopct="%1.1f%%",
                colors=colors[:len(labels)], startangle=90,
            )
            for t in autotexts:
                t.set_fontsize(9)
        elif chart_type == "horizontal_bar":
            y_pos = range(len(labels))
            for i, ds in enumerate(datasets):
                offset = sum(d["values"] for d in datasets[:i]) if i > 0 else 0
                ax.barh(y_pos, ds["values"], left=offset, label=ds.get("label", ""),
                        color=colors[i % len(colors)], height=0.6)
            ax.set_yticks(y_pos)
            ax.set_yticklabels(labels)
            if x_label:
                ax.set_xlabel(x_label)
            ax.legend(loc="lower right")
        elif chart_type == "scatter":
            for i, ds in enumerate(datasets):
                ax.scatter(labels, ds["values"], label=ds.get("label", ""),
                           color=colors[i % len(colors)], s=80, alpha=0.8)
            if x_label:
                ax.set_xlabel(x_label)
            if y_label:
                ax.set_ylabel(y_label)
            ax.legend()
        elif chart_type == "area":
            for i, ds in enumerate(datasets):
                ax.fill_between(range(len(labels)), ds["values"], alpha=0.3,
                                color=colors[i % len(colors)], label=ds.get("label", ""))
                ax.plot(range(len(labels)), ds["values"],
                        color=colors[i % len(colors)], linewidth=2)
            ax.set_xticks(range(len(labels)))
            ax.set_xticklabels(labels, rotation=45, ha="right")
            if y_label:
                ax.set_ylabel(y_label)
            ax.legend()
        elif chart_type == "multi_line":
            for i, ds in enumerate(datasets):
                ax.plot(labels, ds["values"], marker="o", linewidth=2,
                        color=colors[i % len(colors)], label=ds.get("label", ""))
            if x_label:
                ax.set_xlabel(x_label)
            if y_label:
                ax.set_ylabel(y_label)
            ax.legend()
            plt.setp(ax.get_xticklabels(), rotation=45, ha="right")
        elif chart_type == "line":
            for i, ds in enumerate(datasets):
                ax.plot(labels, ds["values"], marker="o", linewidth=2,
                        color=colors[i % len(colors)], label=ds.get("label", ""))
            if x_label:
                ax.set_xlabel(x_label)
            if y_label:
                ax.set_ylabel(y_label)
            ax.legend()
            plt.setp(ax.get_xticklabels(), rotation=45, ha="right")
        else:  # bar (default)
            x_pos = range(len(labels))
            bar_width = 0.8 / max(len(datasets), 1)
            for i, ds in enumerate(datasets):
                offset = (i - (len(datasets) - 1) / 2) * bar_width
                ax.bar([p + offset for p in x_pos], ds["values"], bar_width,
                       label=ds.get("label", ""), color=colors[i % len(colors)])
            ax.set_xticks(x_pos)
            ax.set_xticklabels(labels, rotation=45, ha="right")
            if y_label:
                ax.set_ylabel(y_label)
            ax.legend()

        ax.set_title(title, fontsize=14, fontweight="bold")
        plt.tight_layout()

        # Save
        filename = f"chart_{hash(title) & 0xFFFF}_{chart_type}.png"
        filepath = OUTPUT_DIR / filename
        fig.savefig(filepath, dpi=150, bbox_inches="tight", facecolor="white")
        plt.close(fig)

        return {
            "url": f"/outputs/{filename}",
            "local_path": str(filepath),
            "chart_type": chart_type,
            "title": title,
        }
    except Exception as e:
        plt.close("all")
        return {"error": str(e), "url": "", "local_path": ""}
