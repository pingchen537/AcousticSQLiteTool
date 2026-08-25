"""Offline report command line entry point.

This module reads an existing acoustic SQLite database in read-only mode,
collects filtered report data, and delegates rendering to HTML, Excel, or PDF
modules. It never imports source files and never modifies the database.

Examples
--------
python report_generator.py --format html
python report_generator.py --format all --project 9DR9000B9.DEMO
python report_generator.py --format excel --date-from 2026-01-01 --date-to 2026-12-31
"""

from __future__ import annotations

import argparse
import math
import sqlite3
import statistics
import sys
from collections import defaultdict
from datetime import date, datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Set, Tuple

from report_queries import database_summary


SCRIPT_DIRECTORY = Path(__file__).resolve().parent
DEFAULT_DATABASE = SCRIPT_DIRECTORY / "database" / "acoustic_production.db"
DEFAULT_OUTPUT_DIRECTORY = SCRIPT_DIRECTORY / "reports"


def connect_readonly(database_path: Path) -> sqlite3.Connection:
    """Open an existing SQLite database without write permission."""
    resolved = database_path.expanduser().resolve()
    if not resolved.is_file():
        raise FileNotFoundError("找不到 SQLite Database：{}".format(resolved))
    connection = sqlite3.connect(
        "file:{}?mode=ro".format(resolved.as_posix()),
        uri=True,
    )
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only = ON")
    connection.execute("PRAGMA busy_timeout = 5000")
    return connection


def _rows(
    connection: sqlite3.Connection,
    sql: str,
    parameters: Sequence[Any] = (),
) -> List[Dict[str, Any]]:
    return [dict(row) for row in connection.execute(sql, tuple(parameters)).fetchall()]


def _validate_iso_date(value: Optional[str], argument_name: str) -> Optional[str]:
    if value is None:
        return None
    try:
        parsed = date.fromisoformat(value)
    except ValueError:
        raise ValueError("{} 必須使用 YYYY-MM-DD 格式".format(argument_name))
    return parsed.isoformat()


def _filter_clause(
    alias: str,
    project_code: Optional[str],
    station: Optional[str],
    sn: Optional[str],
    date_from: Optional[str],
    date_to: Optional[str],
) -> Tuple[str, List[Any]]:
    prefix = "{}.".format(alias) if alias else ""
    clauses = ["1 = 1"]
    parameters = []  # type: List[Any]
    if project_code:
        clauses.append("{}project_code = ?".format(prefix))
        parameters.append(project_code)
    if station:
        clauses.append("{}station = ?".format(prefix))
        parameters.append(station.upper())
    if sn:
        clauses.append("{}sn = ?".format(prefix))
        parameters.append(sn)
    if date_from:
        clauses.append("date({}anchor_time) >= date(?)".format(prefix))
        parameters.append(date_from)
    if date_to:
        clauses.append("date({}anchor_time) <= date(?)".format(prefix))
        parameters.append(date_to)
    return " AND ".join(clauses), parameters


def _pct(numerator: int, denominator: int) -> Optional[float]:
    if denominator == 0:
        return None
    return 100.0 * numerator / denominator


def _build_summary(tests: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
    grouped = defaultdict(list)  # type: Dict[Tuple[str, Any, str], List[Dict[str, Any]]]
    for row in tests:
        grouped[(row["project_code"], row.get("model_variant"), row["station"])].append(row)

    summaries = []  # type: List[Dict[str, Any]]
    sorted_groups = sorted(
        grouped.items(),
        key=lambda item: tuple(str(value or "") for value in item[0]),
    )
    for (project_code, model_variant, station), group_rows in sorted_groups:
        by_sn = defaultdict(list)  # type: Dict[str, List[Dict[str, Any]]]
        for row in group_rows:
            by_sn[row["sn"]].append(row)
        for values in by_sn.values():
            values.sort(key=lambda item: (item["anchor_time"], item["test_run_id"]))

        pass_attempts = sum(1 for row in group_rows if row["overall_result"] == "PASS")
        fail_attempts = sum(1 for row in group_rows if row["overall_result"] == "FAIL")
        unknown_attempts = len(group_rows) - pass_attempts - fail_attempts
        valid_attempts = pass_attempts + fail_attempts
        retested_sn = sum(1 for values in by_sn.values() if len(values) > 1)

        first_valid_results = []
        latest_valid_results = []
        for values in by_sn.values():
            valid = [row for row in values if row["overall_result"] in ("PASS", "FAIL")]
            if valid:
                first_valid_results.append(valid[0]["overall_result"])
                latest_valid_results.append(valid[-1]["overall_result"])

        first_pass_sn = sum(1 for result in first_valid_results if result == "PASS")
        final_pass_sn = sum(1 for result in latest_valid_results if result == "PASS")
        summaries.append(
            {
                "project_code": project_code,
                "model_variant": model_variant,
                "station": station,
                "total_attempts": len(group_rows),
                "total_sn": len(by_sn),
                "pass_attempts": pass_attempts,
                "fail_attempts": fail_attempts,
                "unknown_attempts": unknown_attempts,
                "attempt_pass_rate_pct": _pct(pass_attempts, valid_attempts),
                "first_pass_sn": first_pass_sn,
                "first_pass_yield_pct": _pct(first_pass_sn, len(first_valid_results)),
                "final_pass_sn": final_pass_sn,
                "final_yield_pct": _pct(final_pass_sn, len(latest_valid_results)),
                "retested_sn": retested_sn,
                "retest_rate_pct": _pct(retested_sn, len(by_sn)),
            }
        )
    return summaries


def _build_retest_rows(tests: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
    grouped = defaultdict(list)  # type: Dict[Tuple[Any, ...], List[Dict[str, Any]]]
    for row in tests:
        key = (
            row["project_code"],
            row.get("model_variant"),
            row["station"],
            row["sn"],
        )
        grouped[key].append(row)

    result = []  # type: List[Dict[str, Any]]
    for key, values in grouped.items():
        if len(values) <= 1:
            continue
        values.sort(key=lambda item: (item["anchor_time"], item["test_run_id"]))
        valid = [row for row in values if row["overall_result"] in ("PASS", "FAIL")]
        first_result = valid[0]["overall_result"] if valid else "UNKNOWN"
        latest_result = valid[-1]["overall_result"] if valid else "UNKNOWN"
        result.append(
            {
                "project_code": key[0],
                "model_variant": key[1],
                "station": key[2],
                "sn": key[3],
                "attempt_count": len(values),
                "first_test_time": values[0]["anchor_time"],
                "latest_test_time": values[-1]["anchor_time"],
                "first_result": first_result,
                "latest_result": latest_result,
                "fail_then_pass": int(first_result == "FAIL" and latest_result == "PASS"),
                "ever_passed": int(any(row["overall_result"] == "PASS" for row in values)),
            }
        )
    result.sort(
        key=lambda row: (
            row["project_code"],
            -int(row["attempt_count"]),
            row["latest_test_time"],
        )
    )
    return result


def _first_valid_test_ids(tests: Sequence[Dict[str, Any]]) -> Set[int]:
    grouped = defaultdict(list)  # type: Dict[Tuple[Any, ...], List[Dict[str, Any]]]
    for row in tests:
        if row["overall_result"] in ("PASS", "FAIL"):
            grouped[(row["project_code"], row["station"], row["sn"])].append(row)
    result = set()  # type: Set[int]
    for values in grouped.values():
        first = min(values, key=lambda item: (item["anchor_time"], item["test_run_id"]))
        result.add(int(first["test_run_id"]))
    return result


def _calculate_cpk_from_measurements(
    measurements: Iterable[Dict[str, Any]],
    dataset_name: str,
) -> List[Dict[str, Any]]:
    grouped = defaultdict(list)  # type: Dict[Tuple[Any, ...], List[float]]
    for row in measurements:
        value = row.get("measured_value")
        if value is None or (row.get("lsl") is None and row.get("usl") is None):
            continue
        key = (
            row["project_code"],
            row.get("model_variant"),
            row["station"],
            row["measurement_name"],
            row.get("value_unit"),
            row.get("weighting"),
            row.get("spec_revision"),
            row.get("lsl"),
            row.get("usl"),
        )
        grouped[key].append(float(value))

    results = []  # type: List[Dict[str, Any]]
    for key, values in sorted(grouped.items(), key=lambda item: tuple(str(x) for x in item[0])):
        mean_value = statistics.mean(values)
        stddev = statistics.stdev(values) if len(values) >= 2 else None
        cpl = None
        cpu = None
        cpk = None
        if stddev is not None and not math.isclose(stddev, 0.0, abs_tol=1e-15):
            if key[7] is not None:
                cpl = (mean_value - float(key[7])) / (3.0 * stddev)
            if key[8] is not None:
                cpu = (float(key[8]) - mean_value) / (3.0 * stddev)
            available = [value for value in (cpl, cpu) if value is not None]
            cpk = min(available) if available else None
        results.append(
            {
                "dataset": dataset_name,
                "project_code": key[0],
                "model_variant": key[1],
                "station": key[2],
                "measurement_name": key[3],
                "value_unit": key[4],
                "weighting": key[5],
                "spec_revision": key[6],
                "lsl": key[7],
                "usl": key[8],
                "n": len(values),
                "mean": mean_value,
                "sample_stddev": stddev,
                "cpl": cpl,
                "cpu": cpu,
                "cpk": cpk,
            }
        )
    return results


def collect_report_data(
    connection: sqlite3.Connection,
    database_path: Path,
    title: str = "ST01 聲學製程分析報告",
    project_code: Optional[str] = None,
    station: Optional[str] = "ST01",
    sn: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    detail_limit: int = 2000,
) -> Dict[str, Any]:
    """Collect one consistent snapshot for every output format."""
    date_from = _validate_iso_date(date_from, "--date-from")
    date_to = _validate_iso_date(date_to, "--date-to")
    if date_from and date_to and date_from > date_to:
        raise ValueError("--date-from 不可晚於 --date-to")

    where, parameters = _filter_clause(
        "v", project_code, station, sn, date_from, date_to
    )
    tests = _rows(
        connection,
        """
        SELECT v.test_run_id, v.project_code, v.model_variant, v.station,
               v.sn, v.anchor_time, v.start_time, v.overall_result,
               v.duration_sec, v.error_message, v.attempt_no,
               v.attempt_count, v.is_retest
          FROM v_test_overview AS v
         WHERE {where}
         ORDER BY v.project_code, v.station, v.sn, v.anchor_time, v.test_run_id
        """.format(where=where),
        parameters,
    )

    measurement_where, measurement_parameters = _filter_clause(
        "m", project_code, station, sn, date_from, date_to
    )
    measurements = _rows(
        connection,
        """
        SELECT m.test_run_id, m.project_code, m.model_variant, m.station,
               m.sn, m.anchor_time, m.attempt_no, m.attempt_count,
               m.is_retest, m.overall_result, m.measurement_name,
               m.measured_value, m.value_unit, m.weighting,
               m.measurement_result, m.target, m.lsl, m.usl,
               m.spec_revision, m.spec_status, m.evaluated_result
          FROM v_measurement_with_spec AS m
         WHERE {where}
         ORDER BY m.project_code, m.station, m.sn,
                  m.anchor_time, m.measurement_name
        """.format(where=measurement_where),
        measurement_parameters,
    )

    spec_clauses = ["1 = 1"]
    spec_parameters = []  # type: List[Any]
    if project_code:
        spec_clauses.append("project_code = ?")
        spec_parameters.append(project_code)
    if station:
        spec_clauses.append("station = ?")
        spec_parameters.append(station.upper())
    specs = _rows(
        connection,
        """
        SELECT project_code, station, measurement_name, value_unit, weighting,
               target, lsl, usl, spec_revision, effective_from, effective_to,
               spec_status, source_note
          FROM measurement_specs
         WHERE {where}
         ORDER BY project_code, station, measurement_name, spec_revision
        """.format(where=" AND ".join(spec_clauses)),
        spec_parameters,
    )

    first_valid_ids = _first_valid_test_ids(tests)
    first_measurements = [
        row for row in measurements if int(row["test_run_id"]) in first_valid_ids
    ]
    summary = _build_summary(tests)
    retests = _build_retest_rows(tests)
    pass_tests = [row for row in tests if row["overall_result"] == "PASS"]
    fail_tests = [row for row in tests if row["overall_result"] == "FAIL"]

    warnings = []  # type: List[str]
    if not tests:
        warnings.append("目前篩選條件沒有測試資料。")
    if not specs:
        warnings.append("目前篩選條件沒有規格資料，無法判定量測值是否符合 LSL/USL。")
    if measurements and not any(row.get("cpk") is not None for row in _calculate_cpk_from_measurements(measurements, "ALL_ATTEMPTS")):
        warnings.append("部分或全部 CPK 樣本數不足 2，或標準差為 0，因此 CPK 留白。")

    return {
        "metadata": {
            "title": title,
            "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
            "database_path": database_path.name,
            "filters": {
                "project_code": project_code,
                "station": station.upper() if station else None,
                "sn": sn,
                "date_from": date_from,
                "date_to": date_to,
            },
            "detail_limit": max(0, int(detail_limit)),
            "warnings": warnings,
            "database_counts": database_summary(connection),
        },
        "summary": summary,
        "tests": tests,
        "pass_tests": pass_tests,
        "fail_tests": fail_tests,
        "retests": retests,
        "cpk_first": _calculate_cpk_from_measurements(
            first_measurements, "FIRST_VALID_PER_SN"
        ),
        "cpk_all": _calculate_cpk_from_measurements(
            measurements, "ALL_ATTEMPTS"
        ),
        "measurements": measurements,
        "specifications": specs,
    }


def _resolve_path(value: str, base: Path) -> Path:
    path = Path(value).expanduser()
    return path.resolve() if path.is_absolute() else (base / path).resolve()


def build_argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="從本機 SQLite 產生完全離線 HTML、Excel 或 PDF 報表。"
    )
    parser.add_argument(
        "--database",
        default=str(DEFAULT_DATABASE),
        help="SQLite Database 路徑。",
    )
    parser.add_argument(
        "--format",
        choices=("html", "excel", "pdf", "all"),
        default="html",
        help="輸出格式，預設 html。",
    )
    parser.add_argument(
        "--output-dir",
        default=str(DEFAULT_OUTPUT_DIRECTORY),
        help="報表輸出資料夾。",
    )
    parser.add_argument("--output-name", help="輸出檔名（不含副檔名）。")
    parser.add_argument("--title", default="ST01 聲學製程分析報告")
    parser.add_argument("--project", dest="project_code", help="限定 project_code。")
    parser.add_argument("--station", default="ST01", help="限定測站，預設 ST01。")
    parser.add_argument("--sn", help="限定單一 SN。")
    parser.add_argument("--date-from", help="起始日期 YYYY-MM-DD。")
    parser.add_argument("--date-to", help="結束日期 YYYY-MM-DD。")
    parser.add_argument(
        "--detail-limit",
        type=int,
        default=2000,
        help="HTML/PDF 每類明細最多顯示筆數；Excel 保留完整資料。",
    )
    return parser


def generate_reports(arguments: argparse.Namespace) -> List[Path]:
    database_path = _resolve_path(arguments.database, SCRIPT_DIRECTORY)
    output_directory = _resolve_path(arguments.output_dir, SCRIPT_DIRECTORY)
    output_directory.mkdir(parents=True, exist_ok=True)
    stem = arguments.output_name or "ST01_acoustic_report_{}".format(
        datetime.now().strftime("%Y%m%d_%H%M%S")
    )
    if any(character in stem for character in '<>:"/\\|?*'):
        raise ValueError("--output-name 含有 Windows 不允許的檔名字元")

    connection = connect_readonly(database_path)
    try:
        report_data = collect_report_data(
            connection=connection,
            database_path=database_path,
            title=arguments.title,
            project_code=arguments.project_code,
            station=arguments.station,
            sn=arguments.sn,
            date_from=arguments.date_from,
            date_to=arguments.date_to,
            detail_limit=arguments.detail_limit,
        )
    finally:
        connection.close()

    formats = ("html", "excel", "pdf") if arguments.format == "all" else (arguments.format,)
    outputs = []  # type: List[Path]
    for output_format in formats:
        if output_format == "html":
            from html_report import generate_html_report

            outputs.append(
                generate_html_report(report_data, output_directory / (stem + ".html"))
            )
        elif output_format == "excel":
            from excel_report import generate_excel_report

            outputs.append(
                generate_excel_report(report_data, output_directory / (stem + ".xlsx"))
            )
        elif output_format == "pdf":
            from pdf_report import generate_pdf_report

            outputs.append(
                generate_pdf_report(report_data, output_directory / (stem + ".pdf"))
            )
    return outputs


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = build_argument_parser()
    arguments = parser.parse_args(argv)
    try:
        outputs = generate_reports(arguments)
    except (FileNotFoundError, ValueError, sqlite3.Error, ImportError) as error:
        print("報表產生失敗：{}".format(error), file=sys.stderr)
        return 2
    print("報表產生完成：")
    for path in outputs:
        print(path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
