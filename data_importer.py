"""Import RawData and Result folders into the normalized SQLite schema."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence, Tuple, Union

from database import record_import_error, register_source_file
from file_parser import (
    ParsedFileName,
    discover_station_directories,
    find_child_directory,
    infer_model_variant,
    infer_project_code,
    iso_datetime,
    parse_procedure,
    parse_raw_measurement,
    parse_source_filename,
    parse_status_info,
    result_from_prefix,
)


PathLike = Union[str, Path]


@dataclass
class ImportStatistics:
    station_directories: int = 0
    test_runs: int = 0
    procedure_files: int = 0
    status_files: int = 0
    raw_files: int = 0
    measurements: int = 0
    measurement_points: int = 0
    skipped_files: int = 0
    errors: int = 0

    def add(self, other: "ImportStatistics") -> None:
        for field_name in self.__dataclass_fields__:
            setattr(self, field_name, getattr(self, field_name) + getattr(other, field_name))


RunCandidate = Tuple[datetime, int, str]


def _make_run_key(project_code: str, station: str, sn: str, anchor: datetime) -> str:
    return "{}|{}|{}|{}".format(
        project_code,
        station,
        sn,
        anchor.isoformat(timespec="seconds"),
    )


def _upsert_test_run(
    connection: sqlite3.Connection,
    project_code: str,
    station: str,
    sn: str,
    anchor: datetime,
    source_prefix: Optional[str],
    overall_result: str = "UNKNOWN",
    start_time: Optional[datetime] = None,
    duration_sec: Optional[float] = None,
    error_message: Optional[str] = None,
    procedure_file_id: Optional[int] = None,
    status_file_id: Optional[int] = None,
) -> Tuple[int, bool]:
    run_key = _make_run_key(project_code, station, sn, anchor)
    existing = connection.execute(
        "SELECT id FROM test_runs WHERE run_key = ?", (run_key,)
    ).fetchone()
    connection.execute(
        """
        INSERT INTO test_runs(
            run_key, project_code, model_variant, station, sn, anchor_time,
            start_time, source_prefix, overall_result, duration_sec,
            error_message, procedure_file_id, status_file_id
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(run_key) DO UPDATE SET
            model_variant = COALESCE(excluded.model_variant, test_runs.model_variant),
            start_time = COALESCE(excluded.start_time, test_runs.start_time),
            source_prefix = COALESCE(excluded.source_prefix, test_runs.source_prefix),
            overall_result = CASE
                WHEN excluded.overall_result <> 'UNKNOWN' THEN excluded.overall_result
                ELSE test_runs.overall_result
            END,
            duration_sec = COALESCE(excluded.duration_sec, test_runs.duration_sec),
            error_message = COALESCE(excluded.error_message, test_runs.error_message),
            procedure_file_id = COALESCE(excluded.procedure_file_id, test_runs.procedure_file_id),
            status_file_id = COALESCE(excluded.status_file_id, test_runs.status_file_id),
            updated_at = CURRENT_TIMESTAMP
        """,
        (
            run_key,
            project_code,
            infer_model_variant(project_code),
            station,
            sn,
            iso_datetime(anchor),
            iso_datetime(start_time),
            source_prefix,
            overall_result,
            duration_sec,
            error_message,
            procedure_file_id,
            status_file_id,
        ),
    )
    row = connection.execute(
        "SELECT id FROM test_runs WHERE run_key = ?", (run_key,)
    ).fetchone()
    return int(row["id"]), existing is None


def _nearest_run(
    candidates: Sequence[RunCandidate],
    timestamp: datetime,
    maximum_seconds: float = 300.0,
) -> Optional[RunCandidate]:
    if not candidates:
        return None
    nearest = min(candidates, key=lambda item: abs((item[0] - timestamp).total_seconds()))
    if abs((nearest[0] - timestamp).total_seconds()) <= maximum_seconds:
        return nearest
    return None


def _parsed_files(directory: Optional[Path], suffix: str) -> List[Tuple[Path, ParsedFileName]]:
    if directory is None:
        return []
    result = []  # type: List[Tuple[Path, ParsedFileName]]
    for path in directory.rglob("*"):
        if not path.is_file():
            continue
        parsed = parse_source_filename(path)
        if parsed is not None and parsed.suffix.casefold() == suffix.casefold():
            result.append((path, parsed))
    result.sort(key=lambda item: (item[1].sn, item[1].timestamp, str(item[0]).casefold()))
    return result


def _raw_csv_files(directory: Optional[Path]) -> List[Tuple[Path, ParsedFileName]]:
    if directory is None:
        return []
    result = []  # type: List[Tuple[Path, ParsedFileName]]
    for path in directory.rglob("*.csv"):
        parsed = parse_source_filename(path)
        if parsed is not None:
            result.append((path, parsed))
    result.sort(key=lambda item: (item[1].sn, item[1].timestamp, item[1].suffix.casefold()))
    return result


def _attach_document(
    connection: sqlite3.Connection,
    test_run_id: int,
    document_type: str,
    source_file_id: int,
    content: str,
    encoding: str,
    changed: bool,
) -> bool:
    existing = connection.execute(
        "SELECT id FROM test_documents WHERE source_file_id = ?", (source_file_id,)
    ).fetchone()
    if existing is not None and not changed:
        return False
    connection.execute("DELETE FROM test_documents WHERE source_file_id = ?", (source_file_id,))
    connection.execute(
        """
        INSERT INTO test_documents(
            test_run_id, document_type, source_file_id, content, encoding
        ) VALUES (?, ?, ?, ?, ?)
        """,
        (test_run_id, document_type, source_file_id, content, encoding),
    )
    return True


def _import_measurement(
    connection: sqlite3.Connection,
    test_run_id: int,
    source_file_id: int,
    path: Path,
    changed: bool,
) -> Tuple[bool, int]:
    existing = connection.execute(
        "SELECT id FROM measurements WHERE source_file_id = ?", (source_file_id,)
    ).fetchone()
    if existing is not None and not changed:
        return False, 0

    measurement = parse_raw_measurement(path)
    connection.execute("DELETE FROM measurements WHERE source_file_id = ?", (source_file_id,))
    cursor = connection.execute(
        """
        INSERT INTO measurements(
            test_run_id, source_file_id, measurement_name, raw_measurement_name,
            value_unit, weighting, raw_result, scalar_value, sample_count,
            test_datetime, project_in_file
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            test_run_id,
            source_file_id,
            measurement.measurement_name,
            measurement.raw_measurement_name,
            measurement.value_unit,
            measurement.weighting,
            measurement.raw_result,
            measurement.scalar_value,
            len(measurement.points),
            iso_datetime(measurement.test_datetime),
            measurement.project_code,
        ),
    )
    measurement_id = int(cursor.lastrowid)
    connection.executemany(
        """
        INSERT INTO measurement_points(
            measurement_id, point_index, x_value, x_label, value
        ) VALUES (?, ?, ?, ?, ?)
        """,
        [
            (measurement_id, point_index, x_value, x_label, value)
            for point_index, x_value, x_label, value in measurement.points
        ],
    )
    return True, len(measurement.points)


def import_station(
    connection: sqlite3.Connection,
    root: Path,
    station_directory: Path,
) -> ImportStatistics:
    stats = ImportStatistics(station_directories=1)
    station = station_directory.name.upper()
    folder_project = infer_project_code(station_directory)
    result_directory = find_child_directory(station_directory, "Result")
    raw_directory = find_child_directory(station_directory, "RawData")

    procedure_files = _parsed_files(result_directory, "Procedure")
    status_files = _parsed_files(result_directory, "StatusInfo")
    raw_files = _raw_csv_files(raw_directory)
    runs_by_sn = {}  # type: Dict[str, List[RunCandidate]]

    for path, parsed in procedure_files:
        try:
            procedure = parse_procedure(path)
            project_code = procedure.project_code or folder_project
            if not project_code:
                raise ValueError("無法判斷 project_code")
            source_file_id, changed = register_source_file(
                connection, path, root, "PROCEDURE", procedure.encoding
            )
            test_run_id, created = _upsert_test_run(
                connection=connection,
                project_code=project_code,
                station=procedure.station or station,
                sn=parsed.sn,
                anchor=parsed.timestamp,
                source_prefix=parsed.prefix,
                overall_result=procedure.overall_result,
                start_time=procedure.start_time,
                duration_sec=procedure.duration_sec,
                error_message=procedure.error_message,
                procedure_file_id=source_file_id,
            )
            runs_by_sn.setdefault(parsed.sn, []).append(
                (parsed.timestamp, test_run_id, _make_run_key(project_code, station, parsed.sn, parsed.timestamp))
            )
            stats.test_runs += int(created)
            if _attach_document(
                connection,
                test_run_id,
                "PROCEDURE",
                source_file_id,
                procedure.content,
                procedure.encoding,
                changed,
            ):
                stats.procedure_files += 1
            else:
                stats.skipped_files += 1
        except Exception as error:
            record_import_error(connection, path, "PROCEDURE", error)
            stats.errors += 1

    for candidates in runs_by_sn.values():
        candidates.sort(key=lambda item: item[0])

    for path, parsed in status_files:
        try:
            content, encoding = parse_status_info(path)
            nearest = _nearest_run(runs_by_sn.get(parsed.sn, []), parsed.timestamp)
            project_code = folder_project
            if nearest is None:
                if not project_code:
                    raise ValueError("無法判斷 project_code")
                test_run_id, created = _upsert_test_run(
                    connection,
                    project_code,
                    station,
                    parsed.sn,
                    parsed.timestamp,
                    parsed.prefix,
                    result_from_prefix(parsed.prefix),
                )
                nearest = (parsed.timestamp, test_run_id, "")
                runs_by_sn.setdefault(parsed.sn, []).append(nearest)
                stats.test_runs += int(created)
            test_run_id = nearest[1]
            source_file_id, changed = register_source_file(
                connection, path, root, "STATUSINFO", encoding
            )
            connection.execute(
                "UPDATE test_runs SET status_file_id = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                (source_file_id, test_run_id),
            )
            if _attach_document(
                connection,
                test_run_id,
                "STATUSINFO",
                source_file_id,
                content,
                encoding,
                changed,
            ):
                stats.status_files += 1
            else:
                stats.skipped_files += 1
        except Exception as error:
            record_import_error(connection, path, "STATUSINFO", error)
            stats.errors += 1

    for path, parsed in raw_files:
        try:
            raw_measurement = parse_raw_measurement(path)
            project_code = raw_measurement.project_code or folder_project
            if not project_code:
                raise ValueError("無法判斷 project_code")
            nearest = _nearest_run(runs_by_sn.get(parsed.sn, []), parsed.timestamp)
            if nearest is None:
                test_run_id, created = _upsert_test_run(
                    connection,
                    project_code,
                    station,
                    parsed.sn,
                    parsed.timestamp,
                    parsed.prefix,
                    result_from_prefix(parsed.prefix) if parsed.prefix in ("O", "X") else raw_measurement.raw_result,
                )
                nearest = (parsed.timestamp, test_run_id, "")
                runs_by_sn.setdefault(parsed.sn, []).append(nearest)
                stats.test_runs += int(created)
            source_file_id, changed = register_source_file(
                connection, path, root, "RAWDATA", raw_measurement.encoding
            )
            imported, point_count = _import_measurement(
                connection, nearest[1], source_file_id, path, changed
            )
            if imported:
                stats.raw_files += 1
                stats.measurements += 1
                stats.measurement_points += point_count
            else:
                stats.skipped_files += 1
        except Exception as error:
            record_import_error(connection, path, "RAWDATA", error)
            stats.errors += 1

    connection.commit()
    return stats


def import_data(
    connection: sqlite3.Connection,
    roots: Iterable[PathLike],
    station: str = "ST01",
) -> Tuple[ImportStatistics, List[Path]]:
    """Import all discovered ST01 RawData and Result folders."""
    total = ImportStatistics()
    all_station_directories = []  # type: List[Path]
    seen = set()
    for root_value in roots:
        root = Path(root_value).expanduser().resolve()
        for station_directory in discover_station_directories(root, station):
            station_key = str(station_directory).casefold()
            if station_key in seen:
                continue
            seen.add(station_key)
            all_station_directories.append(station_directory)
            total.add(import_station(connection, root, station_directory))
    return total, all_station_directories
