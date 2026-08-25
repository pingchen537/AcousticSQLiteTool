"""Create acoustic PNG charts and summary CSV files directly from SQLite.

This is the database-backed successor to visualize_merged_acoustic_data.py.
It does not require Merged_*.csv files.  The plotting dataset can be either all
attempts (including retests) or the first valid test per SN.

Compatible with Python 3.8, Pandas 2.0.3, and Matplotlib 3.7.5 or newer.

All statistics and CSV summaries use the complete filtered dataset.  When the
database contains many samples, only the charts are reduced to a deterministic
subset so that PNG dimensions, legends, and rendering time remain practical.
"""

from __future__ import annotations

import argparse
import math
import sqlite3
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
from matplotlib.lines import Line2D
from matplotlib.ticker import LogLocator, MultipleLocator

from measurement_data_service import (
    common_spec_limits,
    connect_readonly,
    visualization_frames,
)


SCRIPT_DIRECTORY = Path(__file__).resolve().parent
DEFAULT_DATABASE = SCRIPT_DIRECTORY / "database" / "acoustic_production_v2.db"
DEFAULT_OUTPUT_DIRECTORY = SCRIPT_DIRECTORY / "reports" / "visualizations"

MODEL_COLORS = {"B": "#15803D", "K": "#2563EB"}
FALLBACK_COLORS = ["#7C3AED", "#B45309", "#0891B2", "#BE185D"]
PASS_COLOR = "#15803D"
FAIL_COLOR = "#B91C1C"
WARNING_COLOR = "#B45309"
GRID_COLOR = "#CBD5E1"
MINOR_GRID_COLOR = "#B8C0CA"
MUTED = "#64748B"

LEGEND_LOCATION = "best"
LEGEND_BBOX_TO_ANCHOR = None  # type: Optional[Tuple[float, float]]
MAX_X_LABELS = 40

AVE_LEVEL_Y_MIN = 55.0
AVE_LEVEL_Y_MAX = 75.5
HZ_DB_Y_MIN = 35.0
HZ_DB_Y_MAX = 72.0
FREQRESP_X_MIN = 2800.0
FREQRESP_X_MAX = 10000.0
FREQRESP_Y_MIN = 0.0
FREQRESP_Y_MAX = 70.0
MAIN_FREQ_SEARCH_MIN = 2800.0
MAIN_FREQ_SEARCH_MAX = 3600.0
HARMONIC_TOLERANCE_HZ = 100.0


PLOT_NAMES = [
    "Sample_AveLevel_BarChart.png",
    "Sample_AveLevel_Scatter.png",
    "Sample_MainFrequency_Scatter.png",
    "AveLevel_Histogram.png",
    "AveLevel_SpecMargin.png",
    "HzdB_Histogram.png",
    "Sample_HzdB_BarChart.png",
    "HzdB_SpecMargin.png",
    "Sample_Metric_Heatmap.png",
    "AveLevel_Raw_Distribution_BySample.png",
    "Freqresp_LogX_BySample.png",
    "HzdB_Average_Distribution_BySample.png",
    "HzdB_Raw_Distribution_BySample.png",
]


def _resolve_path(value: str, base: Path) -> Path:
    path = Path(value).expanduser()
    return path.resolve() if path.is_absolute() else (base / path).resolve()


def _configure_style(
    font_scale: float,
    title_font_size: Optional[float] = None,
    axis_label_font_size: Optional[float] = None,
    tick_font_size: Optional[float] = None,
    legend_font_size: Optional[float] = None,
    legend_location: str = "best",
    legend_anchor_x: Optional[float] = None,
    legend_anchor_y: Optional[float] = None,
    max_x_labels: int = 40,
) -> None:
    global LEGEND_LOCATION, LEGEND_BBOX_TO_ANCHOR, MAX_X_LABELS
    base = max(0.7, float(font_scale))
    title_size = title_font_size if title_font_size is not None else 14 * base
    label_size = axis_label_font_size if axis_label_font_size is not None else 11 * base
    tick_size = tick_font_size if tick_font_size is not None else 9 * base
    legend_size = legend_font_size if legend_font_size is not None else 9 * base
    plt.rcParams.update(
        {
            "font.size": 10 * base,
            "axes.titlesize": title_size,
            "axes.labelsize": label_size,
            "xtick.labelsize": tick_size,
            "ytick.labelsize": tick_size,
            "legend.fontsize": legend_size,
            "legend.title_fontsize": legend_size,
            "figure.facecolor": "white",
            "axes.facecolor": "white",
            "axes.unicode_minus": False,
        }
    )
    if legend_location == "outside-right":
        LEGEND_LOCATION = "center left"
        LEGEND_BBOX_TO_ANCHOR = (1.02, 0.5)
    else:
        LEGEND_LOCATION = legend_location
        LEGEND_BBOX_TO_ANCHOR = (
            (legend_anchor_x, legend_anchor_y)
            if legend_anchor_x is not None and legend_anchor_y is not None
            else None
        )
    MAX_X_LABELS = max(1, int(max_x_labels))


def _natural_key(value: Any) -> List[Tuple[int, Any]]:
    import re

    parts = re.split(r"(\d+)", str(value))
    result = []  # type: List[Tuple[int, Any]]
    for part in parts:
        if not part:
            continue
        result.append((0, int(part)) if part.isdigit() else (1, part.casefold()))
    return result


def _model_color(model: Any, index: int = 0) -> str:
    value = str(model or "").upper()
    return MODEL_COLORS.get(value, FALLBACK_COLORS[index % len(FALLBACK_COLORS)])


def _grid(axis: Any) -> None:
    """Apply visible major and minor grid lines to every numeric chart."""
    axis.set_axisbelow(True)
    axis.minorticks_on()
    axis.grid(True, which="major", color=GRID_COLOR, linestyle="--", linewidth=0.9, alpha=0.80)
    axis.grid(True, which="minor", color=MINOR_GRID_COLOR, linestyle=":", linewidth=0.65, alpha=0.75)
    axis.tick_params(which="major", length=5, width=0.8)
    axis.tick_params(which="minor", length=3, width=0.6)
    axis.spines["top"].set_visible(False)
    axis.spines["right"].set_visible(False)


def _legend(axis: Any, *args: Any, **kwargs: Any) -> Any:
    """Create a legend using the command-line placement settings."""
    kwargs.setdefault("frameon", False)
    kwargs.setdefault("loc", LEGEND_LOCATION)
    if LEGEND_BBOX_TO_ANCHOR is not None:
        kwargs.setdefault("bbox_to_anchor", LEGEND_BBOX_TO_ANCHOR)
    return axis.legend(*args, **kwargs)


def _sample_figure_width(sample_count: int) -> float:
    return min(32.0, max(12.0, sample_count * 0.45))


def _set_sample_ticks(axis: Any, positions: Sequence[float], labels: Sequence[str]) -> None:
    count = len(labels)
    step = max(1, int(math.ceil(count / float(MAX_X_LABELS))))
    visible = [label if index % step == 0 else "" for index, label in enumerate(labels)]
    axis.set_xticks(positions)
    axis.set_xticklabels(visible, rotation=45, ha="right")


def _select_plot_keys(frames: Sequence[pd.DataFrame], max_samples: int) -> pd.DataFrame:
    """Choose evenly distributed sample keys for charts only."""
    keys = ["project_code", "Model", "SerialNumber"]
    available = [frame[keys] for frame in frames if not frame.empty and all(key in frame.columns for key in keys)]
    if not available:
        return pd.DataFrame(columns=keys)
    combined = pd.concat(available, ignore_index=True).drop_duplicates()
    combined = combined.sort_values(keys, kind="stable").reset_index(drop=True)
    if max_samples <= 0 or len(combined) <= max_samples:
        return combined
    indices = np.linspace(0, len(combined) - 1, num=max_samples, dtype=int)
    return combined.iloc[np.unique(indices)].reset_index(drop=True)


def _filter_plot_frame(frame: pd.DataFrame, plot_keys: pd.DataFrame) -> pd.DataFrame:
    if frame.empty or plot_keys.empty:
        return frame.iloc[0:0].copy()
    keys = ["project_code", "Model", "SerialNumber"]
    return frame.merge(plot_keys, on=keys, how="inner", sort=False)


def _spec_lines_horizontal(axis: Any, lsl: Optional[float], usl: Optional[float]) -> None:
    if lsl is not None:
        axis.axhline(lsl, color=FAIL_COLOR, linestyle="--", linewidth=1.5, label="LSL = {:g}".format(lsl))
    if usl is not None:
        axis.axhline(usl, color=FAIL_COLOR, linestyle="--", linewidth=1.5, label="USL = {:g}".format(usl))


def _spec_lines_vertical(axis: Any, lsl: Optional[float], usl: Optional[float]) -> None:
    if lsl is not None:
        axis.axvline(lsl, color=FAIL_COLOR, linestyle="--", linewidth=1.5, label="LSL = {:g}".format(lsl))
    if usl is not None:
        axis.axvline(usl, color=FAIL_COLOR, linestyle="--", linewidth=1.5, label="USL = {:g}".format(usl))


def _save(figure: Any, path: Path, dpi: int, extra_artists: Sequence[Any] = ()) -> Path:
    figure.tight_layout()
    kwargs = {"dpi": dpi, "bbox_inches": "tight"}
    if extra_artists:
        kwargs["bbox_extra_artists"] = tuple(extra_artists)
    figure.savefig(str(path), **kwargs)
    plt.close(figure)
    return path


def _placeholder(path: Path, title: str, message: str, dpi: int) -> Path:
    figure, axis = plt.subplots(figsize=(12, 7))
    axis.axis("off")
    axis.set_title(title, loc="left", color="#12304A", pad=18)
    axis.text(0.5, 0.52, message, ha="center", va="center", color=MUTED, fontsize=14)
    return _save(figure, path, dpi)


def _status(mean_value: float, min_value: float, max_value: float, lsl: Optional[float], usl: Optional[float]) -> str:
    if pd.isna(mean_value):
        return "NO_DATA"
    if lsl is not None and mean_value < lsl:
        return "FAIL"
    if usl is not None and mean_value > usl:
        return "FAIL"
    if lsl is not None and min_value < lsl:
        return "WARNING"
    if usl is not None and max_value > usl:
        return "WARNING"
    return "PASS" if lsl is not None or usl is not None else "NO_SPEC"


def summarize_scalar(
    frame: pd.DataFrame,
    value_column: str,
    lsl: Optional[float],
    usl: Optional[float],
) -> pd.DataFrame:
    columns = [
        "project_code",
        "Model",
        "SerialNumber",
        "N",
        "Mean",
        "StdDev",
        "Min",
        "Max",
        "Status",
    ]
    if frame.empty or value_column not in frame.columns:
        return pd.DataFrame(columns=columns)
    working = frame.copy()
    working[value_column] = pd.to_numeric(working[value_column], errors="coerce")
    working = working.dropna(subset=[value_column])
    if working.empty:
        return pd.DataFrame(columns=columns)
    summary = (
        working.groupby(["project_code", "Model", "SerialNumber"], as_index=False)[value_column]
        .agg(["count", "mean", "std", "min", "max"])
        .reset_index()
        .rename(columns={"count": "N", "mean": "Mean", "std": "StdDev", "min": "Min", "max": "Max"})
    )
    summary["Status"] = [
        _status(mean, minimum, maximum, lsl, usl)
        for mean, minimum, maximum in zip(summary["Mean"], summary["Min"], summary["Max"])
    ]
    summary = summary.sort_values(
        ["Model", "SerialNumber"],
        key=lambda series: series.map(lambda value: str(_natural_key(value))),
        kind="stable",
    ).reset_index(drop=True)
    return summary[columns]


def _sample_labels(summary: pd.DataFrame) -> List[str]:
    return summary["SerialNumber"].astype(str).tolist()


def _sample_colors(summary: pd.DataFrame) -> List[str]:
    return [_model_color(model, index) for index, model in enumerate(summary["Model"])]


def _model_spec_handles(
    frame: pd.DataFrame,
    lsl: Optional[float],
    usl: Optional[float],
) -> List[Any]:
    models = sorted(frame["Model"].astype(str).unique()) if not frame.empty else []
    handles = [
        Line2D([0], [0], marker="o", linestyle="none", color=_model_color(model, index), label=model)
        for index, model in enumerate(models)
    ]
    if lsl is not None:
        handles.append(Line2D([0], [0], color=FAIL_COLOR, linestyle="--", label="LSL = {:g}".format(lsl)))
    if usl is not None:
        handles.append(Line2D([0], [0], color=FAIL_COLOR, linestyle="--", label="USL = {:g}".format(usl)))
    return handles


def plot_sample_bar(
    summary: pd.DataFrame,
    title: str,
    ylabel: str,
    output_path: Path,
    lsl: Optional[float],
    usl: Optional[float],
    dpi: int,
    ylim: Optional[Tuple[float, float]] = None,
) -> Path:
    if summary.empty:
        return _placeholder(output_path, title, "No matching sample data", dpi)
    x = np.arange(len(summary))
    figure, axis = plt.subplots(figsize=(_sample_figure_width(len(summary)), 7))
    axis.bar(x, summary["Mean"], color=_sample_colors(summary), alpha=0.82)
    _spec_lines_horizontal(axis, lsl, usl)
    _set_sample_ticks(axis, x, _sample_labels(summary))
    axis.set_title(title)
    axis.set_xlabel("Serial Number")
    axis.set_ylabel(ylabel)
    if ylim:
        axis.set_ylim(*ylim)
    _grid(axis)
    _legend(axis, handles=_model_spec_handles(summary, lsl, usl), title="Model / Specification")
    return _save(figure, output_path, dpi)


def plot_sample_scatter(
    summary: pd.DataFrame,
    title: str,
    ylabel: str,
    output_path: Path,
    lsl: Optional[float],
    usl: Optional[float],
    dpi: int,
    ylim: Optional[Tuple[float, float]] = None,
) -> Path:
    if summary.empty:
        return _placeholder(output_path, title, "No matching sample data", dpi)
    x = np.arange(1, len(summary) + 1)
    figure, axis = plt.subplots(figsize=(_sample_figure_width(len(summary)), 7))
    axis.scatter(x, summary["Mean"], color=_sample_colors(summary), s=64, zorder=3)
    _spec_lines_horizontal(axis, lsl, usl)
    _set_sample_ticks(axis, x, _sample_labels(summary))
    axis.set_title(title)
    axis.set_xlabel("Serial Number")
    axis.set_ylabel(ylabel)
    if ylim:
        axis.set_ylim(*ylim)
    _grid(axis)
    _legend(axis, handles=_model_spec_handles(summary, lsl, usl), title="Model / Specification")
    return _save(figure, output_path, dpi)


def _histogram_edges(values: pd.Series, lsl: Optional[float], usl: Optional[float], width: float) -> np.ndarray:
    numeric = pd.to_numeric(values, errors="coerce").dropna()
    candidates = numeric.tolist()
    if lsl is not None:
        candidates.append(lsl)
    if usl is not None:
        candidates.append(usl)
    lower = min(candidates)
    upper = max(candidates)
    start = math.floor(lower / width) * width
    stop = math.ceil(upper / width) * width
    if math.isclose(start, stop):
        stop = start + width
    return np.arange(start, stop + width * 1.01, width)


def _capability(values: pd.Series, lsl: Optional[float], usl: Optional[float]) -> Dict[str, Any]:
    numeric = pd.to_numeric(values, errors="coerce").dropna()
    count = int(numeric.count())
    mean = float(numeric.mean()) if count else None
    stddev = float(numeric.std(ddof=1)) if count >= 2 else None
    cpl = cpu = cpk = None
    if stddev is not None and not math.isclose(stddev, 0.0, abs_tol=1e-15):
        if lsl is not None:
            cpl = (mean - lsl) / (3.0 * stddev)
        if usl is not None:
            cpu = (usl - mean) / (3.0 * stddev)
        available = [value for value in (cpl, cpu) if value is not None]
        cpk = min(available) if available else None
    return {"N": count, "Mean": mean, "StdDev": stddev, "Cpl": cpl, "Cpu": cpu, "Cpk": cpk}


def plot_histogram(
    raw: pd.DataFrame,
    value_column: str,
    title: str,
    xlabel: str,
    output_path: Path,
    lsl: Optional[float],
    usl: Optional[float],
    bin_width: float,
    dpi: int,
) -> Path:
    if raw.empty or value_column not in raw.columns:
        return _placeholder(output_path, title, "No matching raw data", dpi)
    working = raw.copy()
    working[value_column] = pd.to_numeric(working[value_column], errors="coerce")
    working = working.dropna(subset=[value_column])
    if working.empty:
        return _placeholder(output_path, title, "No numeric values", dpi)
    edges = _histogram_edges(working[value_column], lsl, usl, bin_width)
    figure, axis = plt.subplots(figsize=(12, 7))
    for index, model in enumerate(sorted(working["Model"].astype(str).unique())):
        values = working.loc[working["Model"].astype(str) == model, value_column]
        axis.hist(values, bins=edges, alpha=0.45, color=_model_color(model, index), label=model, edgecolor="white")
    _spec_lines_vertical(axis, lsl, usl)
    stats = _capability(working[value_column], lsl, usl)
    cpk_text = "-" if stats["Cpk"] is None else "{:.3f}".format(stats["Cpk"])
    axis.text(
        0.98,
        0.96,
        "N = {}\nMean = {}\nSD = {}\nCpk = {}".format(
            stats["N"],
            "-" if stats["Mean"] is None else "{:.3f}".format(stats["Mean"]),
            "-" if stats["StdDev"] is None else "{:.3f}".format(stats["StdDev"]),
            cpk_text,
        ),
        transform=axis.transAxes,
        ha="right",
        va="top",
        bbox={"boxstyle": "round", "facecolor": "white", "edgecolor": GRID_COLOR, "alpha": 0.9},
    )
    axis.set_title(title)
    axis.set_xlabel(xlabel)
    axis.set_ylabel("Count")
    _grid(axis)
    _legend(axis, title="Model / Specification")
    return _save(figure, output_path, dpi)


def plot_spec_margin(
    summary: pd.DataFrame,
    title: str,
    output_path: Path,
    lsl: Optional[float],
    usl: Optional[float],
    dpi: int,
) -> Path:
    if summary.empty or (lsl is None and usl is None):
        return _placeholder(output_path, title, "No common LSL/USL for selected projects", dpi)
    margins = []
    for row in summary.itertuples(index=False):
        values = []
        if lsl is not None:
            values.append(float(row.Min) - lsl)
        if usl is not None:
            values.append(usl - float(row.Max))
        margins.append(min(values))
    colors = [PASS_COLOR if value >= 0 else FAIL_COLOR for value in margins]
    x = np.arange(len(summary))
    figure, axis = plt.subplots(figsize=(_sample_figure_width(len(summary)), 7))
    axis.bar(x, margins, color=colors, alpha=0.82)
    axis.axhline(0.0, color="#111827", linewidth=1.2)
    _set_sample_ticks(axis, x, _sample_labels(summary))
    axis.set_title(title)
    axis.set_xlabel("Serial Number")
    axis.set_ylabel("Nearest observed margin")
    _grid(axis)
    return _save(figure, output_path, dpi)


def plot_raw_distribution(
    raw: pd.DataFrame,
    value_column: str,
    title: str,
    ylabel: str,
    output_path: Path,
    lsl: Optional[float],
    usl: Optional[float],
    dpi: int,
    ylim: Optional[Tuple[float, float]] = None,
) -> Path:
    if raw.empty or value_column not in raw.columns:
        return _placeholder(output_path, title, "No matching raw data", dpi)
    working = raw.copy()
    working[value_column] = pd.to_numeric(working[value_column], errors="coerce")
    working = working.dropna(subset=[value_column])
    sample_pairs = working[["Model", "SerialNumber"]].drop_duplicates().values.tolist()
    sample_pairs = sorted(sample_pairs, key=lambda pair: (str(pair[0]), _natural_key(pair[1])))
    sample_order = [str(pair[1]) for pair in sample_pairs]
    figure, axis = plt.subplots(figsize=(_sample_figure_width(len(sample_order)), 7))
    rng = np.random.default_rng(62)
    for index, serial in enumerate(sample_order, start=1):
        selected = working.loc[working["SerialNumber"].astype(str) == serial]
        jitter = rng.uniform(-0.18, 0.18, len(selected)) if len(selected) > 1 else np.array([0.0])
        model = str(selected["Model"].iloc[0])
        axis.scatter(np.full(len(selected), index) + jitter, selected[value_column], s=38, color=_model_color(model, index), alpha=0.85, zorder=3)
    _spec_lines_horizontal(axis, lsl, usl)
    _set_sample_ticks(axis, np.arange(1, len(sample_order) + 1), sample_order)
    axis.set_title(title)
    axis.set_xlabel("Serial Number")
    axis.set_ylabel(ylabel)
    if ylim:
        axis.set_ylim(*ylim)
    _grid(axis)
    _legend(axis, handles=_model_spec_handles(working, lsl, usl), title="Model / Specification")
    return _save(figure, output_path, dpi)


def _frequency_columns(frame: pd.DataFrame) -> List[Tuple[float, Any]]:
    result = []  # type: List[Tuple[float, Any]]
    for column in frame.columns:
        try:
            frequency = float(column)
        except (TypeError, ValueError):
            continue
        result.append((frequency, column))
    return sorted(result)


def plot_freqresp(
    frame: pd.DataFrame,
    output_path: Path,
    dpi: int,
    max_legend_items: int,
) -> Path:
    title = "Frequency Response by Sample (Log Scale)"
    frequency_columns = _frequency_columns(frame)
    if frame.empty or not frequency_columns:
        return _placeholder(output_path, title, "No FREQRESP_IN_1 points", dpi)
    sample_pairs = frame[["Model", "SerialNumber"]].drop_duplicates().values.tolist()
    sample_pairs = sorted(sample_pairs, key=lambda pair: (str(pair[0]), _natural_key(pair[1])))
    cmap = plt.get_cmap("tab20")
    figure, axis = plt.subplots(figsize=(14, 8))
    frequencies = [value for value, _ in frequency_columns]
    for index, (_, serial) in enumerate(sample_pairs):
        selected = frame.loc[frame["SerialNumber"].astype(str) == str(serial)]
        levels = [pd.to_numeric(selected[column], errors="coerce").mean() for _, column in frequency_columns]
        label = str(serial) if max_legend_items <= 0 or index < max_legend_items else "_nolegend_"
        axis.plot(frequencies, levels, color=cmap(index % 20), linewidth=1.5, alpha=0.72, label=label)
    axis.set_xscale("log", base=10)
    axis.set_xlim(FREQRESP_X_MIN, FREQRESP_X_MAX)
    axis.set_ylim(FREQRESP_Y_MIN, FREQRESP_Y_MAX)
    axis.xaxis.set_major_locator(LogLocator(base=10))
    axis.xaxis.set_minor_locator(LogLocator(base=10, subs=np.arange(2, 10) * 0.1))
    axis.set_title(title)
    axis.set_xlabel("Frequency (Hz, log scale)")
    axis.set_ylabel("Level (dBSPL)")
    _grid(axis)
    shown = len(sample_pairs) if max_legend_items <= 0 else min(len(sample_pairs), max_legend_items)
    legend_title = "Serial Number"
    if shown < len(sample_pairs):
        legend_title += " ({} of {})".format(shown, len(sample_pairs))
    legend = _legend(axis, title=legend_title)
    return _save(figure, output_path, dpi, [legend] if legend is not None else [])


def harmonic_summary(frame: pd.DataFrame) -> pd.DataFrame:
    columns = [
        "project_code",
        "Model",
        "SerialNumber",
        "N",
        "MainFrequency_Hz",
        "MainLevel_dB",
        "SecondHarmonic_Hz",
        "SecondDifference_dB",
        "ThirdHarmonic_Hz",
        "ThirdDifference_dB",
    ]
    frequency_columns = _frequency_columns(frame)
    if frame.empty or not frequency_columns:
        return pd.DataFrame(columns=columns)
    frequencies = np.asarray([frequency for frequency, _ in frequency_columns], dtype=float)
    value_columns = [column for _, column in frequency_columns]
    matrix = frame[value_columns].apply(pd.to_numeric, errors="coerce").to_numpy(dtype=float)
    main_mask = (frequencies >= MAIN_FREQ_SEARCH_MIN) & (frequencies <= MAIN_FREQ_SEARCH_MAX)
    records = []  # type: List[Dict[str, Any]]
    metadata = frame[["project_code", "Model", "SerialNumber"]].reset_index(drop=True)
    for row_index, levels in enumerate(matrix):
        valid_main = main_mask & np.isfinite(levels)
        if not np.any(valid_main):
            continue
        main_candidates = np.flatnonzero(valid_main)
        main_index = int(main_candidates[np.argmax(levels[main_candidates])])
        main_frequency = float(frequencies[main_index])
        main_level = float(levels[main_index])

        def harmonic(multiplier: int) -> Tuple[Optional[float], Optional[float]]:
            target = main_frequency * multiplier
            valid = (np.abs(frequencies - target) <= HARMONIC_TOLERANCE_HZ) & np.isfinite(levels)
            if not np.any(valid):
                return None, None
            candidates = np.flatnonzero(valid)
            best_index = int(candidates[np.argmax(levels[candidates])])
            return float(frequencies[best_index]), float(levels[best_index])

        second_frequency, second_level = harmonic(2)
        third_frequency, third_level = harmonic(3)
        row_metadata = metadata.iloc[row_index]
        records.append(
            {
                "project_code": row_metadata["project_code"],
                "Model": row_metadata["Model"],
                "SerialNumber": row_metadata["SerialNumber"],
                "MainFrequency_Hz": main_frequency,
                "MainLevel_dB": main_level,
                "SecondHarmonic_Hz": second_frequency,
                "SecondDifference_dB": None if second_level is None else main_level - second_level,
                "ThirdHarmonic_Hz": third_frequency,
                "ThirdDifference_dB": None if third_level is None else main_level - third_level,
            }
        )
    raw = pd.DataFrame(records)
    if raw.empty:
        return pd.DataFrame(columns=columns)
    summary = (
        raw.groupby(["project_code", "Model", "SerialNumber"], as_index=False)
        .agg(
            N=("MainFrequency_Hz", "count"),
            MainFrequency_Hz=("MainFrequency_Hz", "mean"),
            MainLevel_dB=("MainLevel_dB", "mean"),
            SecondHarmonic_Hz=("SecondHarmonic_Hz", "mean"),
            SecondDifference_dB=("SecondDifference_dB", "mean"),
            ThirdHarmonic_Hz=("ThirdHarmonic_Hz", "mean"),
            ThirdDifference_dB=("ThirdDifference_dB", "mean"),
        )
    )
    return summary[columns]


def build_metric_summary(
    ave: pd.DataFrame,
    main_frequency: pd.DataFrame,
    hzdb: pd.DataFrame,
    harmonic: pd.DataFrame,
) -> pd.DataFrame:
    keys = ["project_code", "Model", "SerialNumber"]
    frames = []
    if not ave.empty:
        frames.append(ave[keys + ["Mean"]].rename(columns={"Mean": "AverageLevel_dBA"}))
    if not main_frequency.empty:
        frames.append(main_frequency[keys + ["Mean"]].rename(columns={"Mean": "MainFrequency_Hz"}))
    if not hzdb.empty:
        frames.append(hzdb[keys + ["Mean"]].rename(columns={"Mean": "MainFrequencyLevel_dBSPL"}))
    if not harmonic.empty:
        frames.append(harmonic[keys + ["SecondDifference_dB", "ThirdDifference_dB"]])
    if not frames:
        return pd.DataFrame(columns=keys)
    result = frames[0]
    for frame in frames[1:]:
        result = result.merge(frame, on=keys, how="outer")
    return result.sort_values(["Model", "SerialNumber"], kind="stable").reset_index(drop=True)


def plot_heatmap(frame: pd.DataFrame, output_path: Path, dpi: int) -> Path:
    title = "Sample Metric Heatmap (column z-score)"
    metric_columns = [column for column in frame.columns if column not in ("project_code", "Model", "SerialNumber")]
    if frame.empty or not metric_columns:
        return _placeholder(output_path, title, "No combined metric data", dpi)
    numeric = frame[metric_columns].apply(pd.to_numeric, errors="coerce")
    standardized = numeric.copy()
    for column in metric_columns:
        mean = numeric[column].mean()
        stddev = numeric[column].std(ddof=0)
        standardized[column] = 0.0 if pd.isna(stddev) or math.isclose(float(stddev), 0.0) else (numeric[column] - mean) / stddev
    figure, axis = plt.subplots(
        figsize=(max(10, len(metric_columns) * 2.2), min(32, max(6, len(frame) * 0.36)))
    )
    image = axis.imshow(standardized.fillna(0).to_numpy(), aspect="auto", cmap="coolwarm", vmin=-2.5, vmax=2.5)
    axis.set_xticks(np.arange(len(metric_columns)))
    axis.set_xticklabels(metric_columns, rotation=30, ha="right")
    axis.set_yticks(np.arange(len(frame)))
    axis.set_yticklabels(frame["SerialNumber"].astype(str))
    axis.set_title(title)
    if len(frame) <= 60:
        annotation_size = max(6.0, float(plt.rcParams["font.size"]) * 0.8)
        for row_index in range(len(frame)):
            for column_index, column in enumerate(metric_columns):
                value = numeric.iloc[row_index, column_index]
                text = "-" if pd.isna(value) else "{:.2f}".format(value)
                axis.text(
                    column_index,
                    row_index,
                    text,
                    ha="center",
                    va="center",
                    fontsize=annotation_size,
                    color="#111827",
                )
    axis.set_xticks(np.arange(-0.5, len(metric_columns), 1), minor=True)
    axis.set_yticks(np.arange(-0.5, len(frame), 1), minor=True)
    axis.grid(which="minor", color=MINOR_GRID_COLOR, linestyle=":", linewidth=0.65, alpha=0.80)
    axis.tick_params(which="minor", bottom=False, left=False)
    figure.colorbar(image, ax=axis, label="z-score")
    return _save(figure, output_path, dpi)


def capability_summary(
    scalar_frames: Dict[str, pd.DataFrame],
    specs: pd.DataFrame,
    dataset_mode: str,
) -> pd.DataFrame:
    mapping = {
        "AVERAGE_LEVEL": "Ave. Level meter",
        "SELECTED_FREQUENCY": "Hz",
        "SELECTED_FREQUENCY_LEVEL": "Hz-dB",
    }
    records = []  # type: List[Dict[str, Any]]
    for measurement_name, value_column in mapping.items():
        frame = scalar_frames.get(measurement_name, pd.DataFrame())
        if frame.empty:
            continue
        for (project, model), group in frame.groupby(["project_code", "Model"]):
            selected_spec = specs.loc[
                (specs["project_code"] == project)
                & (specs["measurement_name"] == measurement_name)
            ]
            lsl = None
            usl = None
            revision = None
            if not selected_spec.empty:
                spec_row = selected_spec.iloc[0]
                lsl = None if pd.isna(spec_row["lsl"]) else float(spec_row["lsl"])
                usl = None if pd.isna(spec_row["usl"]) else float(spec_row["usl"])
                revision = spec_row["spec_revision"]
            stats = _capability(group[value_column], lsl, usl)
            records.append(
                {
                    "Dataset": "FIRST_VALID_PER_SN" if dataset_mode == "first" else "ALL_ATTEMPTS",
                    "project_code": project,
                    "Model": model,
                    "Measurement": measurement_name,
                    "SpecRevision": revision,
                    "LSL": lsl,
                    "USL": usl,
                    **stats,
                }
            )
    return pd.DataFrame(records)


def _write_csv(frame: pd.DataFrame, path: Path) -> Path:
    frame.to_csv(path, index=False, encoding="utf-8-sig")
    return path


def generate_visualizations(
    database_path: Path,
    output_directory: Path,
    projects: Sequence[str],
    station: str,
    sn: Optional[str],
    date_from: Optional[str],
    date_to: Optional[str],
    dataset_mode: str,
    dpi: int,
    font_scale: float,
    title_font_size: Optional[float],
    axis_label_font_size: Optional[float],
    tick_font_size: Optional[float],
    legend_font_size: Optional[float],
    legend_location: str,
    legend_anchor_x: Optional[float],
    legend_anchor_y: Optional[float],
    max_x_labels: int,
    max_plot_samples: int,
    max_legend_items: int,
) -> List[Path]:
    _configure_style(
        font_scale=font_scale,
        title_font_size=title_font_size,
        axis_label_font_size=axis_label_font_size,
        tick_font_size=tick_font_size,
        legend_font_size=legend_font_size,
        legend_location=legend_location,
        legend_anchor_x=legend_anchor_x,
        legend_anchor_y=legend_anchor_y,
        max_x_labels=max_x_labels,
    )
    image_directory = output_directory / "Images"
    summary_directory = output_directory / "Summary"
    image_directory.mkdir(parents=True, exist_ok=True)
    summary_directory.mkdir(parents=True, exist_ok=True)

    connection = connect_readonly(database_path)
    try:
        frames = visualization_frames(
            connection,
            projects=projects,
            station=station,
            sn=sn,
            date_from=date_from,
            date_to=date_to,
            dataset_mode=dataset_mode,
        )
    finally:
        connection.close()

    specs = frames["SPECS"]
    _, ave_lsl, ave_usl = common_spec_limits(specs, "AVERAGE_LEVEL")
    _, hz_lsl, hz_usl = common_spec_limits(specs, "SELECTED_FREQUENCY")
    _, hzdb_lsl, hzdb_usl = common_spec_limits(specs, "SELECTED_FREQUENCY_LEVEL")

    ave_raw = frames["AVERAGE_LEVEL"]
    hz_raw = frames["SELECTED_FREQUENCY"]
    hzdb_raw = frames["SELECTED_FREQUENCY_LEVEL"]
    freqresp = frames["FREQRESP_IN_1"]
    ave_summary = summarize_scalar(ave_raw, "Ave. Level meter", ave_lsl, ave_usl)
    mainfreq_summary = summarize_scalar(hz_raw, "Hz", hz_lsl, hz_usl)
    hzdb_summary = summarize_scalar(hzdb_raw, "Hz-dB", hzdb_lsl, hzdb_usl)
    harmonic = harmonic_summary(freqresp)
    metrics = build_metric_summary(ave_summary, mainfreq_summary, hzdb_summary, harmonic)
    capability = capability_summary(frames, specs, dataset_mode)

    sample_sources = [ave_summary, mainfreq_summary, hzdb_summary, harmonic, freqresp]
    all_plot_keys = _select_plot_keys(sample_sources, 0)
    plot_keys = _select_plot_keys(sample_sources, max_plot_samples)
    total_sample_count = len(all_plot_keys)
    plotted_sample_count = len(plot_keys)
    if plotted_sample_count < total_sample_count:
        print(
            "[INFO] 圖片取樣：{} / {} 個樣本；CSV、CPK 與統計仍使用完整資料。".format(
                plotted_sample_count,
                total_sample_count,
            )
        )

    plot_ave_summary = _filter_plot_frame(ave_summary, plot_keys)
    plot_mainfreq_summary = _filter_plot_frame(mainfreq_summary, plot_keys)
    plot_hzdb_summary = _filter_plot_frame(hzdb_summary, plot_keys)
    plot_metrics = _filter_plot_frame(metrics, plot_keys)
    plot_ave_raw = _filter_plot_frame(ave_raw, plot_keys)
    plot_hzdb_raw = _filter_plot_frame(hzdb_raw, plot_keys)
    plot_freqresp_frame = _filter_plot_frame(freqresp, plot_keys)

    outputs = []  # type: List[Path]
    outputs.append(plot_sample_bar(plot_ave_summary, "Average Level by Sample", "Average level (dBA)", image_directory / "Sample_AveLevel_BarChart.png", ave_lsl, ave_usl, dpi, (AVE_LEVEL_Y_MIN, AVE_LEVEL_Y_MAX)))
    outputs.append(plot_sample_scatter(plot_ave_summary, "Average Level Scatter by Sample", "Average level (dBA)", image_directory / "Sample_AveLevel_Scatter.png", ave_lsl, ave_usl, dpi, (AVE_LEVEL_Y_MIN, AVE_LEVEL_Y_MAX)))
    outputs.append(plot_sample_scatter(plot_mainfreq_summary, "Main Frequency by Sample", "Frequency (Hz)", image_directory / "Sample_MainFrequency_Scatter.png", hz_lsl, hz_usl, dpi))
    outputs.append(plot_histogram(ave_raw, "Ave. Level meter", "Average Level Histogram", "Average level (dBA)", image_directory / "AveLevel_Histogram.png", ave_lsl, ave_usl, 0.5, dpi))
    outputs.append(plot_spec_margin(plot_ave_summary, "Average Level Spec Margin", image_directory / "AveLevel_SpecMargin.png", ave_lsl, ave_usl, dpi))
    outputs.append(plot_histogram(hzdb_raw, "Hz-dB", "Main Frequency Level Histogram", "Level (dBSPL)", image_directory / "HzdB_Histogram.png", hzdb_lsl, hzdb_usl, 1.0, dpi))
    outputs.append(plot_sample_bar(plot_hzdb_summary, "Main Frequency Level by Sample", "Level (dBSPL)", image_directory / "Sample_HzdB_BarChart.png", hzdb_lsl, hzdb_usl, dpi, (HZ_DB_Y_MIN, HZ_DB_Y_MAX)))
    outputs.append(plot_spec_margin(plot_hzdb_summary, "Main Frequency Level Spec Margin", image_directory / "HzdB_SpecMargin.png", hzdb_lsl, hzdb_usl, dpi))
    outputs.append(plot_heatmap(plot_metrics, image_directory / "Sample_Metric_Heatmap.png", dpi))
    outputs.append(plot_raw_distribution(plot_ave_raw, "Ave. Level meter", "Average Level Raw Distribution by Sample", "Average level (dBA)", image_directory / "AveLevel_Raw_Distribution_BySample.png", ave_lsl, ave_usl, dpi, (AVE_LEVEL_Y_MIN, AVE_LEVEL_Y_MAX)))
    outputs.append(plot_freqresp(plot_freqresp_frame, image_directory / "Freqresp_LogX_BySample.png", dpi, max_legend_items))
    outputs.append(plot_sample_scatter(plot_hzdb_summary, "Average Main Frequency Level by Sample", "Level (dBSPL)", image_directory / "HzdB_Average_Distribution_BySample.png", hzdb_lsl, hzdb_usl, dpi, (HZ_DB_Y_MIN, HZ_DB_Y_MAX)))
    outputs.append(plot_raw_distribution(plot_hzdb_raw, "Hz-dB", "Main Frequency Level Raw Distribution by Sample", "Level (dBSPL)", image_directory / "HzdB_Raw_Distribution_BySample.png", hzdb_lsl, hzdb_usl, dpi, (HZ_DB_Y_MIN, HZ_DB_Y_MAX)))

    outputs.extend(
        [
            _write_csv(ave_summary, summary_directory / "Sample_AveLevel_Summary.csv"),
            _write_csv(mainfreq_summary, summary_directory / "Sample_MainFrequency_Summary.csv"),
            _write_csv(hzdb_summary, summary_directory / "Sample_HzdB_Summary.csv"),
            _write_csv(harmonic, summary_directory / "Sample_Harmonic_Summary.csv"),
            _write_csv(metrics, summary_directory / "Sample_Metric_Summary.csv"),
            _write_csv(capability, summary_directory / "Capability_Summary.csv"),
        ]
    )
    metadata = summary_directory / "Run_Metadata.txt"
    metadata.write_text(
        "\n".join(
            [
                "GeneratedAt={}".format(datetime.now().astimezone().isoformat(timespec="seconds")),
                "Database={}".format(database_path),
                "Dataset={}".format("FIRST_VALID_PER_SN" if dataset_mode == "first" else "ALL_ATTEMPTS"),
                "Projects={}".format(",".join(projects) if projects else "ALL"),
                "Station={}".format(station),
                "SN={}".format(sn or "ALL"),
                "DateFrom={}".format(date_from or "ALL"),
                "DateTo={}".format(date_to or "ALL"),
                "TotalSamples={}".format(total_sample_count),
                "PlottedSamples={}".format(plotted_sample_count),
                "MaxPlotSamples={}".format(max_plot_samples),
                "MaxLegendItems={}".format(max_legend_items),
                "LegendLocation={}".format(legend_location),
                "AverageLevelSpec={},{}".format(ave_lsl, ave_usl),
                "MainFrequencySpec={},{}".format(hz_lsl, hz_usl),
                "MainFrequencyLevelSpec={},{}".format(hzdb_lsl, hzdb_usl),
            ]
        ),
        encoding="utf-8",
    )
    outputs.append(metadata)
    return outputs


def build_argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="從聲學 SQLite 直接產生 PNG 圖片與統計摘要。"
    )
    parser.add_argument("--database", default=str(DEFAULT_DATABASE))
    parser.add_argument("--output-dir", default=str(DEFAULT_OUTPUT_DIRECTORY))
    parser.add_argument("--project", action="append", default=[], help="可重複指定 project_code。")
    parser.add_argument("--station", default="ST01")
    parser.add_argument("--sn")
    parser.add_argument("--date-from")
    parser.add_argument("--date-to")
    parser.add_argument(
        "--dataset",
        choices=("all", "first"),
        default="all",
        help="all 包含重測；first 每個 SN 只取第一次有效測試。",
    )
    parser.add_argument("--dpi", type=int, default=200)
    parser.add_argument(
        "--font-scale",
        type=float,
        default=1.0,
        help="整體文字縮放倍率，預設 1.0。",
    )
    parser.add_argument("--title-font-size", type=float, help="圖表標題字級。")
    parser.add_argument("--axis-label-font-size", type=float, help="X/Y 軸標題字級。")
    parser.add_argument("--tick-font-size", type=float, help="座標刻度字級。")
    parser.add_argument("--legend-font-size", type=float, help="圖例文字字級。")
    parser.add_argument(
        "--legend-location",
        choices=(
            "best",
            "upper right",
            "upper left",
            "lower left",
            "lower right",
            "center left",
            "center right",
            "lower center",
            "upper center",
            "center",
            "outside-right",
        ),
        default="best",
        help="圖例位置；含空白的位置請加雙引號。",
    )
    parser.add_argument("--legend-anchor-x", type=float, help="自訂圖例錨點 X（需與 Y 同時指定）。")
    parser.add_argument("--legend-anchor-y", type=float, help="自訂圖例錨點 Y（需與 X 同時指定）。")
    parser.add_argument(
        "--max-x-labels",
        type=int,
        default=40,
        help="X 軸最多顯示的 SN 文字數；格線仍保留全部樣本位置。",
    )
    parser.add_argument(
        "--max-plot-samples",
        type=int,
        default=120,
        help="圖片最多繪製的樣本數；0 表示不限。統計與 CSV 永遠使用完整資料。",
    )
    parser.add_argument(
        "--max-legend-items",
        type=int,
        default=20,
        help="頻率響應圖最多顯示的 SN 圖例數；0 表示不限。",
    )
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = build_argument_parser()
    arguments = parser.parse_args(argv)
    try:
        if (arguments.legend_anchor_x is None) != (arguments.legend_anchor_y is None):
            raise ValueError("--legend-anchor-x 與 --legend-anchor-y 必須同時指定")
        for option_name, option_value in (
            ("--font-scale", arguments.font_scale),
            ("--title-font-size", arguments.title_font_size),
            ("--axis-label-font-size", arguments.axis_label_font_size),
            ("--tick-font-size", arguments.tick_font_size),
            ("--legend-font-size", arguments.legend_font_size),
        ):
            if option_value is not None and option_value <= 0:
                raise ValueError("{} 必須大於 0".format(option_name))
        if arguments.max_x_labels <= 0:
            raise ValueError("--max-x-labels 必須大於 0")
        if arguments.max_plot_samples < 0:
            raise ValueError("--max-plot-samples 不可小於 0")
        if arguments.max_legend_items < 0:
            raise ValueError("--max-legend-items 不可小於 0")
        database_path = _resolve_path(arguments.database, SCRIPT_DIRECTORY)
        output_directory = _resolve_path(arguments.output_dir, SCRIPT_DIRECTORY)
        outputs = generate_visualizations(
            database_path=database_path,
            output_directory=output_directory,
            projects=arguments.project,
            station=arguments.station,
            sn=arguments.sn,
            date_from=arguments.date_from,
            date_to=arguments.date_to,
            dataset_mode=arguments.dataset,
            dpi=max(72, arguments.dpi),
            font_scale=arguments.font_scale,
            title_font_size=arguments.title_font_size,
            axis_label_font_size=arguments.axis_label_font_size,
            tick_font_size=arguments.tick_font_size,
            legend_font_size=arguments.legend_font_size,
            legend_location=arguments.legend_location,
            legend_anchor_x=arguments.legend_anchor_x,
            legend_anchor_y=arguments.legend_anchor_y,
            max_x_labels=arguments.max_x_labels,
            max_plot_samples=arguments.max_plot_samples,
            max_legend_items=arguments.max_legend_items,
        )
        print("圖片與摘要產生完成：")
        for path in outputs:
            print(path)
        return 0
    except (FileNotFoundError, ValueError, sqlite3.Error, ImportError) as error:
        print("視覺化失敗：{}".format(error), file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
