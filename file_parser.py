"""Filename and source-file parsers for the acoustic ST01 station."""

from __future__ import annotations

import csv
import io
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple, Union


PathLike = Union[str, Path]


FILE_NAME_RE = re.compile(
    # Match the 14-digit timestamp as the stable delimiter so public/demo SNs
    # such as DEMO_B_000001 can safely contain underscores.
    r"^(?P<prefix>[^_]+)_(?P<sn>.+?)_(?P<timestamp>\d{14})_"
    r"(?P<suffix>.+)\.(?P<extension>csv|txt)$",
    re.IGNORECASE,
)


# Project codes have historically started with 0DR, but production folders can
# also use 1DR or another numeric revision prefix.  Keep the DR token as the
# stable part of the naming convention instead of hard-coding the first digit.
PROJECT_CODE_RE = re.compile(
    r"(?<![A-Z0-9])(?P<code>[0-9]+DR[0-9A-Z][0-9A-Z.]*)",
    re.IGNORECASE,
)

PROJECT_FIELD_RE = re.compile(
    r"(?im)^\s*(?:project(?:\s*(?:name|code))?|product(?:\s*(?:name|code))?)"
    r"\s*[:=]\s*(?P<code>[A-Z0-9][A-Z0-9._-]*)\s*$"
)

NON_PROJECT_FOLDER_NAMES = {
    "demo",
    "rawdata",
    "result",
    "spec",
    "database",
    "reports",
}


MEASUREMENT_ALIASES = {
    "freqresp_in_1": "FREQRESP_IN_1",
    "ave_level_meter": "AVERAGE_LEVEL",
    "average_level": "AVERAGE_LEVEL",
    "hz": "SELECTED_FREQUENCY",
    "hz_db": "SELECTED_FREQUENCY_LEVEL",
    "level_meter_in_1": "LEVEL_METER_IN_1",
}


@dataclass(frozen=True)
class ParsedFileName:
    prefix: str
    sn: str
    timestamp: datetime
    suffix: str
    extension: str


@dataclass
class ProcedureInfo:
    project_code: Optional[str]
    station: Optional[str]
    overall_result: str
    duration_sec: Optional[float]
    start_time: Optional[datetime]
    error_message: Optional[str]
    content: str
    encoding: str


@dataclass
class RawMeasurement:
    project_code: Optional[str]
    test_datetime: Optional[datetime]
    raw_measurement_name: str
    measurement_name: str
    value_unit: Optional[str]
    weighting: Optional[str]
    raw_result: str
    scalar_value: Optional[float]
    points: List[Tuple[int, Optional[float], str, Optional[float]]]
    encoding: str


def read_text_with_encoding(path: PathLike) -> Tuple[str, str]:
    """Read legacy station files without losing Traditional Chinese text."""
    data = Path(path).read_bytes()
    for encoding in ("utf-8-sig", "cp950", "big5", "utf-8", "latin-1"):
        try:
            return data.decode(encoding), encoding
        except UnicodeDecodeError:
            continue
    return data.decode("latin-1", errors="replace"), "latin-1"


def parse_source_filename(path: PathLike) -> Optional[ParsedFileName]:
    match = FILE_NAME_RE.match(Path(path).name)
    if match is None:
        return None
    return ParsedFileName(
        prefix=match.group("prefix").upper(),
        sn=match.group("sn").strip(),
        timestamp=datetime.strptime(match.group("timestamp"), "%Y%m%d%H%M%S"),
        suffix=match.group("suffix"),
        extension=match.group("extension").lower(),
    )


def normalize_result(value: Optional[str]) -> str:
    text = (value or "").strip().upper()
    if text in ("PASS", "PASSED", "OK", "O"):
        return "PASS"
    if text in ("FAIL", "FAILED", "NG", "X"):
        return "FAIL"
    return "UNKNOWN"


def result_from_prefix(prefix: Optional[str]) -> str:
    return normalize_result(prefix)


def canonical_measurement_name(raw_name: str) -> str:
    normalized = re.sub(r"[^a-z0-9]+", "_", raw_name.lower()).strip("_")
    return MEASUREMENT_ALIASES.get(normalized, normalized.upper())


def normalize_project_code(value: Optional[str]) -> Optional[str]:
    """Clean one project-code candidate without assuming a 0DR/1DR prefix."""
    candidate = (value or "").strip().strip("\"'[](){}<>")
    candidate = candidate.rstrip(".,;:")
    if not candidate or not any(character.isalpha() for character in candidate):
        return None
    if not any(character.isdigit() for character in candidate):
        return None
    return candidate.upper()


def extract_project_code(text: Optional[str]) -> Optional[str]:
    """Extract a project code from labelled text or any numeric ``DR`` token."""
    content = text or ""
    field_match = PROJECT_FIELD_RE.search(content)
    if field_match:
        normalized = normalize_project_code(field_match.group("code"))
        if normalized:
            return normalized
    token_match = PROJECT_CODE_RE.search(content)
    if token_match:
        return normalize_project_code(token_match.group("code"))
    return None


def measurement_metadata(
    measurement_name: str,
    raw_unit: Optional[str],
) -> Tuple[Optional[str], Optional[str]]:
    """Return canonical unit and weighting.

    The ST01 level-meter time series is the source of the confirmed A-weighted
    average, so both LEVEL_METER_IN_1 and AVERAGE_LEVEL are stored as dBA/A.
    Main-frequency level remains dBSPL because its source specification only
    states dB and does not identify A-weighting.
    """
    if measurement_name in ("AVERAGE_LEVEL", "LEVEL_METER_IN_1"):
        return "dBA", "A"
    if measurement_name in ("FREQRESP_IN_1", "SELECTED_FREQUENCY_LEVEL"):
        return "dBSPL", None
    if measurement_name == "SELECTED_FREQUENCY":
        return "Hz", None
    cleaned = (raw_unit or "").strip() or None
    return cleaned, None


def _to_float(value: Optional[str]) -> Optional[float]:
    text = (value or "").strip()
    if not text or text.upper() in ("NA", "N/A", "NULL", "#DIV/0!"):
        return None
    try:
        return float(text)
    except ValueError:
        return None


def _parse_datetime_text(date_text: str, time_text: str) -> Optional[datetime]:
    date_value = date_text.strip()
    time_value = time_text.strip()
    is_pm = "下午" in time_value
    is_am = "上午" in time_value
    time_value = time_value.replace("上午", "").replace("下午", "").strip()

    candidate = "{} {}".format(date_value, time_value)
    for date_format in ("%Y/%m/%d", "%Y-%m-%d", "%Y.%m.%d"):
        for time_format in ("%H:%M:%S", "%I:%M:%S"):
            try:
                parsed = datetime.strptime(candidate, "{} {}".format(date_format, time_format))
                if is_pm and parsed.hour < 12:
                    parsed = parsed.replace(hour=parsed.hour + 12)
                elif is_am and parsed.hour == 12:
                    parsed = parsed.replace(hour=0)
                return parsed
            except ValueError:
                continue
    return None


def parse_raw_measurement(path: PathLike) -> RawMeasurement:
    parsed_name = parse_source_filename(path)
    if parsed_name is None:
        raise ValueError("無法解析 RawData 檔名：{}".format(Path(path).name))

    text, encoding = read_text_with_encoding(path)
    rows = list(csv.reader(io.StringIO(text)))
    if len(rows) < 2:
        raise ValueError("RawData CSV 沒有資料列")

    header = [cell.strip() for cell in rows[0]]
    data_row = None
    for candidate in rows[1:]:
        if any(cell.strip() for cell in candidate):
            data_row = candidate
            break
    if data_row is None or len(data_row) < 6:
        raise ValueError("RawData CSV 欄位不足，至少需要 6 欄")

    if len(data_row) < len(header):
        data_row = data_row + [""] * (len(header) - len(data_row))

    measurement_name = canonical_measurement_name(parsed_name.suffix)
    value_unit, weighting = measurement_metadata(measurement_name, data_row[3])
    test_datetime = _parse_datetime_text(data_row[0], data_row[1])
    if test_datetime is None:
        test_datetime = parsed_name.timestamp

    points = []  # type: List[Tuple[int, Optional[float], str, Optional[float]]]
    for point_index, (x_label, raw_value) in enumerate(
        zip(header[5:], data_row[5:])
    ):
        points.append((point_index, _to_float(x_label), x_label, _to_float(raw_value)))

    scalar_value = None
    if len(points) == 1:
        scalar_value = points[0][3]

    return RawMeasurement(
        project_code=normalize_project_code(data_row[2]),
        test_datetime=test_datetime,
        raw_measurement_name=parsed_name.suffix,
        measurement_name=measurement_name,
        value_unit=value_unit,
        weighting=weighting,
        raw_result=normalize_result(data_row[4]),
        scalar_value=scalar_value,
        points=points,
        encoding=encoding,
    )


def parse_procedure(path: PathLike) -> ProcedureInfo:
    text, encoding = read_text_with_encoding(path)
    parsed_name = parse_source_filename(path)

    result_matches = re.findall(
        r"Test\s*Result\s*:\s*(PASS|FAIL)", text, flags=re.IGNORECASE
    )
    overall_result = normalize_result(result_matches[-1] if result_matches else None)
    if overall_result == "UNKNOWN" and parsed_name is not None:
        overall_result = result_from_prefix(parsed_name.prefix)

    duration_matches = re.findall(
        r"(?:^|\n)\s*Test\s*time\s*:\s*([0-9]+(?:\.[0-9]+)?)\s*sec",
        text,
        flags=re.IGNORECASE,
    )
    duration_sec = float(duration_matches[-1]) if duration_matches else None

    start_time = None
    start_match = re.search(
        r"<Test\s*start>\s*start\s*time\s*:\s*"
        r"(\d{4}[./-]\d{1,2}[./-]\d{1,2})\s+(\d{1,2}:\d{2}:\d{2})",
        text,
        flags=re.IGNORECASE,
    )
    if start_match:
        start_time = _parse_datetime_text(start_match.group(1), start_match.group(2))

    project_code = extract_project_code(text)
    station_match = re.search(r"(?m)^\s*(F\d+)\s*$", text, flags=re.IGNORECASE)
    station = station_match.group(1).upper() if station_match else None

    issue_lines = []
    for line in text.splitlines():
        stripped = line.strip()
        if "[Warning MSG]" in stripped or stripped.lower().startswith("error:"):
            if stripped not in issue_lines:
                issue_lines.append(stripped)
    error_message = " | ".join(issue_lines[:10]) or None

    return ProcedureInfo(
        project_code=project_code,
        station=station,
        overall_result=overall_result,
        duration_sec=duration_sec,
        start_time=start_time,
        error_message=error_message,
        content=text,
        encoding=encoding,
    )


def parse_status_info(path: PathLike) -> Tuple[str, str]:
    return read_text_with_encoding(path)


def find_child_directory(parent: PathLike, expected_name: str) -> Optional[Path]:
    parent_path = Path(parent)
    if not parent_path.is_dir():
        return None
    expected = expected_name.casefold()
    for child in parent_path.iterdir():
        if child.is_dir() and child.name.casefold() == expected:
            return child
    return None


def discover_station_directories(root: PathLike, station: str = "ST01") -> List[Path]:
    root_path = Path(root).expanduser().resolve()
    if not root_path.exists():
        raise FileNotFoundError("找不到資料根目錄：{}".format(root_path))
    if root_path.is_dir() and root_path.name.casefold() == station.casefold():
        return [root_path]
    result = [
        path
        for path in root_path.rglob("*")
        if path.is_dir() and path.name.casefold() == station.casefold()
    ]
    return sorted(set(result), key=lambda item: str(item).casefold())


def infer_project_code(station_directory: PathLike) -> Optional[str]:
    """Infer project code from the folders above ST01.

    Priority:
    1. A numeric DR token such as 0DR..., 1DR..., or 12DR....
    2. The direct parent folder name when it looks like a project identifier.

    Pure dates and generic folder names are deliberately rejected.
    """
    station_path = Path(station_directory)
    candidates = [station_path.parent.name, station_path.parent.parent.name]
    for candidate in candidates:
        project_code = extract_project_code(candidate)
        if project_code:
            return project_code

    direct_parent = station_path.parent.name.strip()
    normalized_parent = normalize_project_code(direct_parent)
    if normalized_parent is None:
        return None
    if direct_parent.casefold() in NON_PROJECT_FOLDER_NAMES:
        return None
    if re.fullmatch(r"\d{8}", direct_parent):
        return None
    if direct_parent.casefold() == station_path.name.casefold():
        return None
    return normalized_parent


def infer_model_variant(project_code: Optional[str]) -> Optional[str]:
    if not project_code:
        return None
    match = re.search(r"(?:\d+)?DR\d+([A-Z])", project_code.upper())
    return match.group(1) if match else None


def iso_datetime(value: Optional[datetime]) -> Optional[str]:
    return value.isoformat(timespec="seconds") if value is not None else None


def group_paths_by_sn(paths: Sequence[PathLike]) -> Dict[str, List[Path]]:
    grouped = {}  # type: Dict[str, List[Path]]
    for path_value in paths:
        path = Path(path_value)
        parsed = parse_source_filename(path)
        if parsed is not None:
            grouped.setdefault(parsed.sn, []).append(path)
    for values in grouped.values():
        values.sort(key=lambda item: parse_source_filename(item).timestamp)  # type: ignore
    return grouped
