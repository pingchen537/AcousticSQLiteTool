"""Reusable PASS/FAIL, SN, retest, and CPK queries.

This module returns Python dictionaries and prints console tables only. It does
not create CSV files.
"""

from __future__ import annotations

import math
import sqlite3
import statistics
from collections import defaultdict
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple


def _rows(connection: sqlite3.Connection, sql: str, parameters: Sequence[Any] = ()) -> List[Dict[str, Any]]:
    return [dict(row) for row in connection.execute(sql, tuple(parameters)).fetchall()]


def database_summary(connection: sqlite3.Connection) -> Dict[str, int]:
    names = (
        "source_files",
        "test_runs",
        "measurements",
        "measurement_points",
        "measurement_specs",
        "import_errors",
    )
    return {
        name: int(connection.execute("SELECT COUNT(*) FROM {}".format(name)).fetchone()[0])
        for name in names
    }


def query_tests_by_result(
    connection: sqlite3.Connection,
    result: str,
    limit: int = 100,
) -> List[Dict[str, Any]]:
    normalized = result.strip().upper()
    if normalized not in ("PASS", "FAIL"):
        raise ValueError("result 必須是 PASS 或 FAIL")
    return _rows(
        connection,
        """
        SELECT project_code, model_variant, station, sn, anchor_time,
               overall_result, duration_sec, attempt_no, attempt_count, is_retest
          FROM v_test_overview
         WHERE overall_result = ?
         ORDER BY anchor_time DESC
         LIMIT ?
        """,
        (normalized, int(limit)),
    )


def query_sn(connection: sqlite3.Connection, sn: str) -> Dict[str, List[Dict[str, Any]]]:
    serial_number = sn.strip()
    tests = _rows(
        connection,
        """
        SELECT * FROM v_test_overview
         WHERE sn = ?
         ORDER BY anchor_time, test_run_id
        """,
        (serial_number,),
    )
    scalars = _rows(
        connection,
        """
        SELECT project_code, station, sn, anchor_time, attempt_no,
               measurement_name, measured_value, value_unit, weighting,
               lsl, usl, spec_revision, evaluated_result
          FROM v_measurement_with_spec
         WHERE sn = ?
         ORDER BY anchor_time, measurement_name
        """,
        (serial_number,),
    )
    return {"tests": tests, "scalar_measurements": scalars}


def query_retested_sn(
    connection: sqlite3.Connection,
    project_code: Optional[str] = None,
) -> List[Dict[str, Any]]:
    if project_code:
        return _rows(
            connection,
            """
            SELECT * FROM v_retest_sn
             WHERE project_code = ?
             ORDER BY attempt_count DESC, latest_test_time DESC
            """,
            (project_code,),
        )
    return _rows(
        connection,
        """
        SELECT * FROM v_retest_sn
         ORDER BY project_code, attempt_count DESC, latest_test_time DESC
        """,
    )


def query_retest_rate(connection: sqlite3.Connection) -> List[Dict[str, Any]]:
    rows = _rows(
        connection,
        """
        SELECT
            project_code,
            model_variant,
            station,
            COUNT(*) AS total_sn,
            SUM(is_retested) AS retested_sn,
            ROUND(100.0 * SUM(is_retested) / NULLIF(COUNT(*), 0), 2) AS retest_rate_pct
        FROM v_sn_retest_summary
        GROUP BY project_code, model_variant, station
        ORDER BY project_code, station
        """,
    )
    return rows


def calculate_cpk(
    connection: sqlite3.Connection,
    mode: str = "first",
) -> List[Dict[str, Any]]:
    """Calculate CPK for first valid measurements or all attempts.

    ``mode='first'`` uses one first valid test per SN.
    ``mode='all'`` includes every valid PASS/FAIL attempt, including retests.
    Sample standard deviation (n-1) is used. CPK is blank when n < 2 or the
    standard deviation is zero.
    """
    normalized_mode = mode.strip().lower()
    if normalized_mode not in ("first", "all"):
        raise ValueError("mode 必須是 first 或 all")
    view_name = "v_cpk_first_valid" if normalized_mode == "first" else "v_cpk_all_tests"
    source_rows = _rows(
        connection,
        """
        SELECT project_code, model_variant, station, measurement_name,
               value_unit, weighting, spec_revision, lsl, usl, measured_value
          FROM {view_name}
         ORDER BY project_code, station, measurement_name, anchor_time
        """.format(view_name=view_name),
    )

    grouped = defaultdict(list)  # type: Dict[Tuple[Any, ...], List[float]]
    for row in source_rows:
        key = (
            row["project_code"],
            row["model_variant"],
            row["station"],
            row["measurement_name"],
            row["value_unit"],
            row["weighting"],
            row["spec_revision"],
            row["lsl"],
            row["usl"],
        )
        grouped[key].append(float(row["measured_value"]))

    results = []  # type: List[Dict[str, Any]]
    for key, values in sorted(grouped.items(), key=lambda item: tuple(str(x) for x in item[0])):
        (
            project_code,
            model_variant,
            station,
            measurement_name,
            value_unit,
            weighting,
            spec_revision,
            lsl,
            usl,
        ) = key
        count = len(values)
        mean_value = statistics.mean(values)
        stddev = statistics.stdev(values) if count >= 2 else None
        cpu = None
        cpl = None
        cpk = None
        if stddev is not None and not math.isclose(stddev, 0.0, abs_tol=1e-15):
            if usl is not None:
                cpu = (float(usl) - mean_value) / (3.0 * stddev)
            if lsl is not None:
                cpl = (mean_value - float(lsl)) / (3.0 * stddev)
            available = [value for value in (cpu, cpl) if value is not None]
            cpk = min(available) if available else None

        results.append(
            {
                "dataset": "FIRST_VALID_PER_SN" if normalized_mode == "first" else "ALL_ATTEMPTS",
                "project_code": project_code,
                "model_variant": model_variant,
                "station": station,
                "measurement_name": measurement_name,
                "value_unit": value_unit,
                "weighting": weighting,
                "spec_revision": spec_revision,
                "lsl": lsl,
                "usl": usl,
                "n": count,
                "mean": mean_value,
                "sample_stddev": stddev,
                "cpl": cpl,
                "cpu": cpu,
                "cpk": cpk,
            }
        )
    return results


def _display_value(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, float):
        return "{:.4f}".format(value)
    return str(value)


def print_table(rows: Iterable[Dict[str, Any]], columns: Optional[Sequence[str]] = None) -> None:
    data = list(rows)
    if not data:
        print("(沒有符合資料)")
        return
    selected = list(columns or data[0].keys())
    widths = {
        column: min(
            40,
            max(len(column), max(len(_display_value(row.get(column))) for row in data)),
        )
        for column in selected
    }
    print(" | ".join(column.ljust(widths[column]) for column in selected))
    print("-+-".join("-" * widths[column] for column in selected))
    for row in data:
        print(
            " | ".join(
                _display_value(row.get(column))[: widths[column]].ljust(widths[column])
                for column in selected
            )
        )
