"""Import manually maintained specification CSV files from ST01/Spec."""

from __future__ import annotations

import csv
import io
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence, Tuple, Union

from database import record_import_error, register_source_file
from file_parser import (
    canonical_measurement_name,
    find_child_directory,
    infer_project_code,
    read_text_with_encoding,
)


PathLike = Union[str, Path]


REQUIRED_COLUMNS = {
    "project_code",
    "station",
    "measurement_name",
    "spec_revision",
}


@dataclass
class SpecImportStatistics:
    spec_files: int = 0
    spec_rows: int = 0
    skipped_files: int = 0
    errors: int = 0


def _optional_text(value: Optional[str]) -> Optional[str]:
    text = (value or "").strip()
    return text or None


def _optional_float(value: Optional[str]) -> Optional[float]:
    text = (value or "").strip()
    if not text:
        return None
    return float(text)


def _parse_spec_file(
    path: Path,
    default_project: Optional[str],
    default_station: str,
) -> Tuple[List[Dict[str, object]], str]:
    text, encoding = read_text_with_encoding(path)
    reader = csv.DictReader(io.StringIO(text))
    if reader.fieldnames is None:
        raise ValueError("規格 CSV 沒有標題列")
    normalized_headers = {header.strip() for header in reader.fieldnames if header}
    missing = REQUIRED_COLUMNS - normalized_headers
    if missing:
        raise ValueError("規格 CSV 缺少欄位：{}".format(", ".join(sorted(missing))))

    rows = []  # type: List[Dict[str, object]]
    for line_number, raw in enumerate(reader, start=2):
        if not any((value or "").strip() for value in raw.values()):
            continue
        project_code = _optional_text(raw.get("project_code")) or default_project
        station = (_optional_text(raw.get("station")) or default_station).upper()
        measurement_name_raw = _optional_text(raw.get("measurement_name"))
        spec_revision = _optional_text(raw.get("spec_revision"))
        if not project_code or not measurement_name_raw or not spec_revision:
            raise ValueError("第 {} 列的 project/measurement/revision 不完整".format(line_number))

        lsl = _optional_float(raw.get("lsl"))
        usl = _optional_float(raw.get("usl"))
        if lsl is not None and usl is not None and lsl > usl:
            raise ValueError("第 {} 列 LSL 大於 USL".format(line_number))

        rows.append(
            {
                "project_code": project_code,
                "station": station,
                "measurement_name": canonical_measurement_name(measurement_name_raw),
                "value_unit": _optional_text(raw.get("value_unit")),
                "weighting": _optional_text(raw.get("weighting")),
                "target": _optional_float(raw.get("target")),
                "lsl": lsl,
                "usl": usl,
                "spec_revision": spec_revision,
                "effective_from": _optional_text(raw.get("effective_from")),
                "effective_to": _optional_text(raw.get("effective_to")),
                "spec_status": (_optional_text(raw.get("spec_status")) or "ENGINEERING").upper(),
                "source_note": _optional_text(raw.get("source_note")),
            }
        )
    if not rows:
        raise ValueError("規格 CSV 沒有有效資料列")
    return rows, encoding


def import_spec_file(
    connection: sqlite3.Connection,
    root: Path,
    station_directory: Path,
    path: Path,
) -> Tuple[bool, int]:
    rows, encoding = _parse_spec_file(
        path,
        infer_project_code(station_directory),
        station_directory.name.upper(),
    )
    source_file_id, changed = register_source_file(
        connection, path, root, "SPEC", encoding
    )
    existing_count = connection.execute(
        "SELECT COUNT(*) FROM measurement_specs WHERE source_file_id = ?",
        (source_file_id,),
    ).fetchone()[0]
    if not changed and existing_count:
        return False, 0

    connection.execute(
        "DELETE FROM measurement_specs WHERE source_file_id = ?", (source_file_id,)
    )
    for row in rows:
        connection.execute(
            """
            INSERT INTO measurement_specs(
                project_code, station, measurement_name, value_unit, weighting,
                target, lsl, usl, spec_revision, effective_from, effective_to,
                spec_status, source_note, source_file_id
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(project_code, station, measurement_name, spec_revision)
            DO UPDATE SET
                value_unit = excluded.value_unit,
                weighting = excluded.weighting,
                target = excluded.target,
                lsl = excluded.lsl,
                usl = excluded.usl,
                effective_from = excluded.effective_from,
                effective_to = excluded.effective_to,
                spec_status = excluded.spec_status,
                source_note = excluded.source_note,
                source_file_id = excluded.source_file_id,
                updated_at = CURRENT_TIMESTAMP
            """,
            (
                row["project_code"],
                row["station"],
                row["measurement_name"],
                row["value_unit"],
                row["weighting"],
                row["target"],
                row["lsl"],
                row["usl"],
                row["spec_revision"],
                row["effective_from"],
                row["effective_to"],
                row["spec_status"],
                row["source_note"],
                source_file_id,
            ),
        )
    return True, len(rows)


def import_specs(
    connection: sqlite3.Connection,
    roots: Iterable[PathLike],
    station_directories: Sequence[Path],
) -> SpecImportStatistics:
    """Import all CSV files immediately under each ST01/Spec directory."""
    stats = SpecImportStatistics()
    root_paths = [Path(value).expanduser().resolve() for value in roots]
    for station_directory in station_directories:
        spec_directory = find_child_directory(station_directory, "Spec")
        if spec_directory is None:
            continue
        root = next(
            (candidate for candidate in root_paths if candidate in station_directory.parents or candidate == station_directory),
            station_directory,
        )
        for path in sorted(spec_directory.glob("*.csv"), key=lambda item: item.name.casefold()):
            try:
                imported, row_count = import_spec_file(
                    connection, root, station_directory, path
                )
                if imported:
                    stats.spec_files += 1
                    stats.spec_rows += row_count
                else:
                    stats.skipped_files += 1
            except Exception as error:
                record_import_error(connection, path, "SPEC", error)
                stats.errors += 1
    connection.commit()
    return stats
