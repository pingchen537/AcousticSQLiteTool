"""Main entry point for the modular acoustic SQLite importer.

Default behavior:
1. Scan the configured Demo root for model/ST01 folders.
2. Incrementally import Result, RawData, and Spec.
3. Recreate query views and print an import/database summary.

No source CSV/TXT file is modified or deleted.
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path
from typing import List, Optional, Sequence

from data_importer import ImportStatistics, import_data
from database import connect_database, create_schema, refresh_views
from report_queries import (
    calculate_cpk,
    database_summary,
    print_table,
    query_retest_rate,
    query_retested_sn,
    query_sn,
    query_tests_by_result,
)
from spec_importer import SpecImportStatistics, import_specs


SCRIPT_DIRECTORY = Path(__file__).resolve().parent
DEFAULT_ROOT = SCRIPT_DIRECTORY / "Demo_Public"
DEFAULT_DATABASE = SCRIPT_DIRECTORY / "database" / "acoustic_production.db"


def _database_path(value: str) -> Path:
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = SCRIPT_DIRECTORY / path
    return path.resolve()


def build_argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="將 ST01 RawData、Result、Spec 匯入 SQLite，並提供查詢與 CPK。"
    )
    parser.add_argument(
        "--root",
        action="append",
        help="資料根目錄；可重複指定。未指定時使用目前的 Demo 路徑。",
    )
    parser.add_argument(
        "--database",
        default=str(DEFAULT_DATABASE),
        help="SQLite 路徑；相對路徑以程式所在資料夾為基準。",
    )
    parser.add_argument("--station", default="ST01", help="要匯入的測站，預設 ST01。")
    parser.add_argument(
        "--query-only",
        action="store_true",
        help="只查詢既有資料庫，不掃描或匯入來源檔案。",
    )
    parser.add_argument("--sn", help="查詢特定 SN 的測試與標量量測結果。")
    parser.add_argument(
        "--result",
        choices=("PASS", "FAIL", "pass", "fail"),
        help="列出 PASS 或 FAIL 測試。",
    )
    parser.add_argument(
        "--retest",
        action="store_true",
        help="顯示重測率與所有重測 SN。",
    )
    parser.add_argument(
        "--cpk",
        choices=("first", "all", "both"),
        help="計算第一次有效量測、全部測試或兩者的 CPK。",
    )
    parser.add_argument("--limit", type=int, default=100, help="PASS/FAIL 顯示筆數。")
    return parser


def _print_import_summary(
    data_stats: ImportStatistics,
    spec_stats: SpecImportStatistics,
) -> None:
    rows = [
        {"項目": "找到 ST01 資料夾", "新增/更新": data_stats.station_directories},
        {"項目": "測試紀錄", "新增/更新": data_stats.test_runs},
        {"項目": "Procedure", "新增/更新": data_stats.procedure_files},
        {"項目": "StatusInfo", "新增/更新": data_stats.status_files},
        {"項目": "RawData CSV", "新增/更新": data_stats.raw_files},
        {"項目": "量測點", "新增/更新": data_stats.measurement_points},
        {"項目": "Spec CSV", "新增/更新": spec_stats.spec_files},
        {"項目": "Spec 規格列", "新增/更新": spec_stats.spec_rows},
        {
            "項目": "未變更而略過",
            "新增/更新": data_stats.skipped_files + spec_stats.skipped_files,
        },
        {"項目": "匯入錯誤", "新增/更新": data_stats.errors + spec_stats.errors},
    ]
    print("\n匯入結果")
    print_table(rows)


def _print_database_summary(connection: sqlite3.Connection) -> None:
    summary = database_summary(connection)
    print("\n資料庫內容")
    print_table([{"資料表": name, "筆數": count} for name, count in summary.items()])


def _run_queries(connection: sqlite3.Connection, arguments: argparse.Namespace) -> None:
    if arguments.sn:
        result = query_sn(connection, arguments.sn)
        print("\nSN {}：測試紀錄".format(arguments.sn))
        print_table(result["tests"])
        print("\nSN {}：標量量測與規格".format(arguments.sn))
        print_table(result["scalar_measurements"])

    if arguments.result:
        normalized = arguments.result.upper()
        print("\n{} 測試".format(normalized))
        print_table(query_tests_by_result(connection, normalized, arguments.limit))

    if arguments.retest:
        print("\n重測率")
        print_table(query_retest_rate(connection))
        print("\n重測 SN")
        print_table(query_retested_sn(connection))

    if arguments.cpk:
        modes = ("first", "all") if arguments.cpk == "both" else (arguments.cpk,)
        for mode in modes:
            title = "每個 SN 第一次有效量測" if mode == "first" else "所有測試（包含重測）"
            print("\nCPK：{}".format(title))
            print_table(calculate_cpk(connection, mode))


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = build_argument_parser()
    arguments = parser.parse_args(argv)
    database_path = _database_path(arguments.database)
    roots = [Path(value) for value in (arguments.root or [str(DEFAULT_ROOT)])]

    print("Python：{}".format(sys.version.split()[0]))
    print("SQLite：{}".format(sqlite3.sqlite_version))
    print("資料庫：{}".format(database_path))

    connection = connect_database(database_path)
    try:
        create_schema(connection)
        if not arguments.query_only:
            data_stats, station_directories = import_data(
                connection, roots, arguments.station
            )
            spec_stats = import_specs(connection, roots, station_directories)
            refresh_views(connection)
            _print_import_summary(data_stats, spec_stats)
            if not station_directories:
                print("\n注意：指定路徑下沒有找到 {} 資料夾。".format(arguments.station))

        _print_database_summary(connection)
        _run_queries(connection, arguments)
        return 0
    except FileNotFoundError as error:
        print("錯誤：{}".format(error), file=sys.stderr)
        return 2
    except sqlite3.Error as error:
        print("SQLite 錯誤：{}".format(error), file=sys.stderr)
        return 3
    finally:
        connection.close()


if __name__ == "__main__":
    sys.exit(main())
