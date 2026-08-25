"""Shared read-only SQLite queries for Excel export and acoustic plots.

The module converts the normalized SQLite schema back into analysis-friendly
Pandas DataFrames.  It never modifies the database or the original RawData.
Compatible with Python 3.8, Pandas 2.0, and the modular importer schema.
"""

from __future__ import annotations

import re
import sqlite3
from datetime import date
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple, Union

import pandas as pd


PathLike = Union[str, Path]


CANONICAL_TO_RAW_NAME = {
    "AVERAGE_LEVEL": "Ave_Level_meter",
    "SELECTED_FREQUENCY": "Hz",
    "SELECTED_FREQUENCY_LEVEL": "Hz_dB",
    "FREQRESP_IN_1": "Freqresp_in_1",
    "LEVEL_METER_IN_1": "level_Meter_in_1",
}


SCALAR_VALUE_COLUMNS = {
    "AVERAGE_LEVEL": "Ave. Level meter",
    "SELECTED_FREQUENCY": "Hz",
    "SELECTED_FREQUENCY_LEVEL": "Hz-dB",
}


POINT_MEASUREMENTS = {"FREQRESP_IN_1", "LEVEL_METER_IN_1"}


def connect_readonly(database_path: PathLike) -> sqlite3.Connection:
    """Open an existing database in read-only/query-only mode."""
    path = Path(database_path).expanduser().resolve()
    if not path.is_file():
        raise FileNotFoundError("找不到 SQLite Database：{}".format(path))
    connection = sqlite3.connect(
        "file:{}?mode=ro".format(path.as_posix()),
        uri=True,
    )
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only = ON")
    connection.execute("PRAGMA busy_timeout = 5000")
    _validate_schema(connection)
    return connection


def _validate_schema(connection: sqlite3.Connection) -> None:
    required = {
        "v_test_overview",
        "v_first_valid_test",
        "measurements",
        "measurement_points",
        "measurement_specs",
        "source_files",
    }
    available = {
        str(row[0])
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type IN ('table', 'view')"
        )
    }
    missing = sorted(required - available)
    if missing:
        raise sqlite3.DatabaseError(
            "Database 缺少必要資料表或 View：{}。請先重新執行 "
            "acoustic_sqlite_importer.py。".format(", ".join(missing))
        )


def validate_iso_date(value: Optional[str], argument_name: str) -> Optional[str]:
    if not value:
        return None
    try:
        return date.fromisoformat(value).isoformat()
    except ValueError:
        raise ValueError("{} 必須使用 YYYY-MM-DD 格式".format(argument_name))


def normalize_measurement_name(value: str) -> str:
    normalized = re.sub(r"[^a-z0-9]+", "_", value.strip().lower()).strip("_")
    aliases = {
        "ave_level_meter": "AVERAGE_LEVEL",
        "average_level": "AVERAGE_LEVEL",
        "hz": "SELECTED_FREQUENCY",
        "hz_db": "SELECTED_FREQUENCY_LEVEL",
        "freqresp_in_1": "FREQRESP_IN_1",
        "level_meter_in_1": "LEVEL_METER_IN_1",
    }
    return aliases.get(normalized, normalized.upper())


def raw_measurement_name(measurement_name: str) -> str:
    canonical = normalize_measurement_name(measurement_name)
    return CANONICAL_TO_RAW_NAME.get(canonical, canonical)


def model_label(project_code: Any, model_variant: Any) -> str:
    variant = str(model_variant or "").strip().upper()
    if variant:
        return variant
    project = str(project_code or "").strip().upper()
    match = re.search(r"2512([A-Z])", project)
    return match.group(1) if match else project


def _listify(values: Optional[Iterable[str]]) -> List[str]:
    if values is None:
        return []
    return [str(value).strip() for value in values if str(value).strip()]


def _filter_clause(
    alias: str,
    projects: Optional[Iterable[str]] = None,
    station: Optional[str] = "ST01",
    sn: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    dataset_mode: str = "all",
    measurement_names: Optional[Iterable[str]] = None,
) -> Tuple[str, List[Any]]:
    date_from = validate_iso_date(date_from, "--date-from")
    date_to = validate_iso_date(date_to, "--date-to")
    if date_from and date_to and date_from > date_to:
        raise ValueError("--date-from 不可晚於 --date-to")

    mode = dataset_mode.strip().lower()
    if mode not in ("all", "first"):
        raise ValueError("dataset_mode 必須是 all 或 first")

    prefix = alias + "." if alias else ""
    clauses = ["1 = 1"]
    parameters = []  # type: List[Any]

    project_values = _listify(projects)
    if project_values:
        placeholders = ", ".join("?" for _ in project_values)
        clauses.append("{}project_code IN ({})".format(prefix, placeholders))
        parameters.extend(project_values)
    if station:
        clauses.append("{}station = ?".format(prefix))
        parameters.append(station.strip().upper())
    if sn:
        clauses.append("{}sn = ?".format(prefix))
        parameters.append(sn.strip())
    if date_from:
        clauses.append("date({}anchor_time) >= date(?)".format(prefix))
        parameters.append(date_from)
    if date_to:
        clauses.append("date({}anchor_time) <= date(?)".format(prefix))
        parameters.append(date_to)
    if mode == "first":
        clauses.append(
            "{}test_run_id IN (SELECT test_run_id FROM v_first_valid_test)".format(
                prefix
            )
        )

    names = [normalize_measurement_name(value) for value in _listify(measurement_names)]
    if names:
        placeholders = ", ".join("?" for _ in names)
        clauses.append("m.measurement_name IN ({})".format(placeholders))
        parameters.extend(names)
    return " AND ".join(clauses), parameters


def _frame(connection: sqlite3.Connection, sql: str, parameters: Sequence[Any]) -> pd.DataFrame:
    return pd.read_sql_query(sql, connection, params=list(parameters))


def _add_common_columns(frame: pd.DataFrame) -> pd.DataFrame:
    result = frame.copy()
    if result.empty:
        return result
    result["Model"] = [
        model_label(project, variant)
        for project, variant in zip(result["project_code"], result["model_variant"])
    ]
    timestamp_source = result["anchor_time"]
    if "measurement_time" in result.columns:
        measurement_times = result["measurement_time"].replace("", pd.NA)
        timestamp_source = measurement_times.fillna(result["anchor_time"])
    timestamps = pd.to_datetime(timestamp_source, errors="coerce")
    result["TestDate"] = timestamps.dt.date
    result["TestTime"] = timestamps.dt.time
    result["SerialNumber"] = result["sn"].astype(str)
    result["Result"] = result["overall_result"].astype(str).str.upper()
    if "source_path" in result.columns:
        result["SourceFile"] = result["source_path"].fillna("").map(
            lambda value: Path(str(value)).name if str(value) else ""
        )
    else:
        result["SourceFile"] = ""
    return result


def query_scalar_measurements(
    connection: sqlite3.Connection,
    projects: Optional[Iterable[str]] = None,
    station: str = "ST01",
    sn: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    dataset_mode: str = "all",
    measurement_names: Optional[Iterable[str]] = None,
) -> pd.DataFrame:
    """Return one row per scalar measurement."""
    where, parameters = _filter_clause(
        "v",
        projects,
        station,
        sn,
        date_from,
        date_to,
        dataset_mode,
        measurement_names,
    )
    result = _frame(
        connection,
        """
        SELECT
            v.test_run_id, v.project_code, v.model_variant, v.station,
            v.sn, v.anchor_time, v.overall_result, v.attempt_no,
            v.attempt_count, v.is_retest,
            m.measurement_name, m.raw_measurement_name, m.test_datetime AS measurement_time,
            m.value_unit,
            m.weighting, m.raw_result AS measurement_result,
            m.scalar_value AS measured_value,
            sf.source_path
        FROM v_test_overview AS v
        JOIN measurements AS m ON m.test_run_id = v.test_run_id
        LEFT JOIN source_files AS sf ON sf.id = m.source_file_id
        WHERE {where}
          AND m.scalar_value IS NOT NULL
        ORDER BY v.project_code, v.station, v.sn, v.anchor_time,
                 v.test_run_id, m.measurement_name
        """.format(where=where),
        parameters,
    )
    return _add_common_columns(result)


def query_measurement_points(
    connection: sqlite3.Connection,
    projects: Optional[Iterable[str]] = None,
    station: str = "ST01",
    sn: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    dataset_mode: str = "all",
    measurement_names: Optional[Iterable[str]] = None,
) -> pd.DataFrame:
    """Return long-format measurement points."""
    where, parameters = _filter_clause(
        "v",
        projects,
        station,
        sn,
        date_from,
        date_to,
        dataset_mode,
        measurement_names,
    )
    result = _frame(
        connection,
        """
        SELECT
            v.test_run_id, v.project_code, v.model_variant, v.station,
            v.sn, v.anchor_time, v.overall_result, v.attempt_no,
            v.attempt_count, v.is_retest,
            m.id AS measurement_id, m.measurement_name,
            m.raw_measurement_name, m.test_datetime AS measurement_time,
            m.value_unit, m.weighting,
            m.raw_result AS measurement_result,
            p.point_index, p.x_value, p.x_label, p.value,
            sf.source_path
        FROM v_test_overview AS v
        JOIN measurements AS m ON m.test_run_id = v.test_run_id
        JOIN measurement_points AS p ON p.measurement_id = m.id
        LEFT JOIN source_files AS sf ON sf.id = m.source_file_id
        WHERE {where}
        ORDER BY v.project_code, v.station, v.sn, v.anchor_time,
                 v.test_run_id, m.measurement_name, p.point_index
        """.format(where=where),
        parameters,
    )
    return _add_common_columns(result)


def query_current_specs(
    connection: sqlite3.Connection,
    projects: Optional[Iterable[str]] = None,
    station: str = "ST01",
) -> pd.DataFrame:
    clauses = ["1 = 1"]
    parameters = []  # type: List[Any]
    project_values = _listify(projects)
    if project_values:
        placeholders = ", ".join("?" for _ in project_values)
        clauses.append("project_code IN ({})".format(placeholders))
        parameters.extend(project_values)
    if station:
        clauses.append("station = ?")
        parameters.append(station.strip().upper())
    result = _frame(
        connection,
        """
        SELECT project_code, station, measurement_name, value_unit, weighting,
               target, lsl, usl, spec_revision, effective_from, effective_to,
               spec_status, source_note
          FROM v_current_specs
         WHERE {where}
         ORDER BY project_code, station, measurement_name
        """.format(where=" AND ".join(clauses)),
        parameters,
    )
    if not result.empty:
        result["Model"] = [
            model_label(project, None) for project in result["project_code"]
        ]
    return result


def common_spec_limits(
    specs: pd.DataFrame,
    measurement_name: str,
) -> Tuple[Optional[float], Optional[float], Optional[float]]:
    """Return target/LSL/USL only when selected projects share one value."""
    if specs.empty:
        return None, None, None
    canonical = normalize_measurement_name(measurement_name)
    selected = specs.loc[specs["measurement_name"] == canonical]
    if selected.empty:
        return None, None, None

    def unique_number(column: str) -> Optional[float]:
        values = pd.to_numeric(selected[column], errors="coerce").dropna().unique()
        return float(values[0]) if len(values) == 1 else None

    return unique_number("target"), unique_number("lsl"), unique_number("usl")


def list_export_groups(
    connection: sqlite3.Connection,
    projects: Optional[Iterable[str]] = None,
    station: str = "ST01",
    sn: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    dataset_mode: str = "all",
    measurement_names: Optional[Iterable[str]] = None,
) -> pd.DataFrame:
    where, parameters = _filter_clause(
        "v",
        projects,
        station,
        sn,
        date_from,
        date_to,
        dataset_mode,
        measurement_names,
    )
    result = _frame(
        connection,
        """
        SELECT
            date(v.anchor_time) AS test_date,
            m.measurement_name,
            COALESCE(NULLIF(m.raw_measurement_name, ''), m.measurement_name)
                AS raw_measurement_name,
            m.value_unit,
            m.weighting,
            COUNT(DISTINCT m.id) AS measurement_count
        FROM v_test_overview AS v
        JOIN measurements AS m ON m.test_run_id = v.test_run_id
        WHERE {where}
        GROUP BY date(v.anchor_time), m.measurement_name,
                 COALESCE(NULLIF(m.raw_measurement_name, ''), m.measurement_name),
                 m.value_unit, m.weighting
        ORDER BY test_date, m.measurement_name
        """.format(where=where),
        parameters,
    )
    return result


def _metadata_columns(frame: pd.DataFrame) -> List[str]:
    candidates = [
        "test_run_id",
        "project_code",
        "Model",
        "SerialNumber",
        "TestDate",
        "TestTime",
        "Result",
        "attempt_no",
        "attempt_count",
        "is_retest",
        "SourceFile",
        "measurement_name",
        "raw_measurement_name",
        "value_unit",
        "weighting",
    ]
    return [column for column in candidates if column in frame.columns]


def points_to_wide(points: pd.DataFrame) -> pd.DataFrame:
    """Pivot long measurement points into one row per test/measurement."""
    if points.empty:
        return pd.DataFrame()
    index_columns = _metadata_columns(points)
    working = points.copy()
    for column in index_columns:
        if working[column].isna().any():
            working[column] = working[column].fillna("")
    working["PointColumn"] = working["x_value"].astype(object)
    missing_x = working["PointColumn"].isna()
    working.loc[missing_x, "PointColumn"] = working.loc[missing_x, "x_label"]
    pivoted = working.pivot(
        index=index_columns,
        columns="PointColumn",
        values="value",
    ).reset_index()
    pivoted.columns.name = None

    metadata = set(index_columns)
    point_columns = [column for column in pivoted.columns if column not in metadata]
    point_columns = sorted(
        point_columns,
        key=lambda value: (
            0,
            float(value),
        )
        if _is_number(value)
        else (1, str(value)),
    )
    return pivoted[index_columns + point_columns]


def _is_number(value: Any) -> bool:
    try:
        float(value)
        return True
    except (TypeError, ValueError):
        return False


def build_export_frame(
    connection: sqlite3.Connection,
    measurement_name: str,
    test_date: str,
    projects: Optional[Iterable[str]] = None,
    station: str = "ST01",
    sn: Optional[str] = None,
    dataset_mode: str = "all",
    include_audit_columns: bool = False,
) -> pd.DataFrame:
    """Build an attachment-compatible daily export DataFrame."""
    canonical = normalize_measurement_name(measurement_name)
    if canonical in POINT_MEASUREMENTS:
        long_frame = query_measurement_points(
            connection,
            projects=projects,
            station=station,
            sn=sn,
            date_from=test_date,
            date_to=test_date,
            dataset_mode=dataset_mode,
            measurement_names=[canonical],
        )
        frame = points_to_wide(long_frame)
    else:
        scalar = query_scalar_measurements(
            connection,
            projects=projects,
            station=station,
            sn=sn,
            date_from=test_date,
            date_to=test_date,
            dataset_mode=dataset_mode,
            measurement_names=[canonical],
        )
        if scalar.empty:
            return pd.DataFrame()
        value_column = SCALAR_VALUE_COLUMNS.get(canonical, raw_measurement_name(canonical))
        frame = scalar.copy()
        frame[value_column] = pd.to_numeric(frame["measured_value"], errors="coerce")

    if frame.empty:
        return frame
    result = pd.DataFrame(
        {
            "project": frame["project_code"],
            "SN": frame["SerialNumber"],
            "TestDate": frame["TestDate"],
            "TestTime": frame["TestTime"],
            "Result": frame["Result"].str.title(),
        }
    )
    if canonical in POINT_MEASUREMENTS:
        value_columns = [column for column in frame.columns if _is_number(column)]
    else:
        value_columns = [value_column]
    for column in value_columns:
        result[column] = frame[column].values

    if include_audit_columns:
        result.insert(5, "AttemptNo", frame["attempt_no"].values)
        result.insert(6, "IsRetest", frame["is_retest"].values)
        result.insert(7, "SourceFile", frame["SourceFile"].values)
        result.insert(8, "TestRunID", frame["test_run_id"].values)
    sort_columns = ["TestDate", "TestTime", "project", "SN"]
    return result.sort_values(sort_columns, kind="stable").reset_index(drop=True)


def visualization_frames(
    connection: sqlite3.Connection,
    projects: Optional[Iterable[str]] = None,
    station: str = "ST01",
    sn: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    dataset_mode: str = "all",
) -> Dict[str, pd.DataFrame]:
    scalar = query_scalar_measurements(
        connection,
        projects=projects,
        station=station,
        sn=sn,
        date_from=date_from,
        date_to=date_to,
        dataset_mode=dataset_mode,
        measurement_names=list(SCALAR_VALUE_COLUMNS),
    )
    result = {}  # type: Dict[str, pd.DataFrame]
    for measurement_name, value_column in SCALAR_VALUE_COLUMNS.items():
        selected = scalar.loc[scalar["measurement_name"] == measurement_name].copy()
        if not selected.empty:
            selected[value_column] = pd.to_numeric(
                selected["measured_value"], errors="coerce"
            )
        result[measurement_name] = selected

    freqresp_long = query_measurement_points(
        connection,
        projects=projects,
        station=station,
        sn=sn,
        date_from=date_from,
        date_to=date_to,
        dataset_mode=dataset_mode,
        measurement_names=["FREQRESP_IN_1"],
    )
    result["FREQRESP_IN_1"] = points_to_wide(freqresp_long)
    result["SPECS"] = query_current_specs(connection, projects, station)
    return result
