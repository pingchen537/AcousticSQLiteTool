"""Generate a fully synthetic public Demo dataset for AcousticSQLiteTool.

Every identifier, timestamp, specification, status message, and measurement
value produced by this script is fictional.  The generated files are intended
only for importer, query, report, and visualization demonstrations.
"""

from __future__ import annotations

import argparse
import csv
import math
import random
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, Iterable, List, Sequence


PROJECTS = {
    "B": "9DR9000B9.DEMO",
    "K": "9DR9000K9.DEMO",
}

SPEC_LIMITS = {
    "AVERAGE_LEVEL": (None, 60.0, 76.0, "dBA", "A"),
    "SELECTED_FREQUENCY": (3000.0, 2700.0, 3300.0, "Hz", ""),
    "SELECTED_FREQUENCY_LEVEL": (None, 40.0, 70.0, "dBSPL", ""),
}


@dataclass(frozen=True)
class Attempt:
    model: str
    sn: str
    attempt_no: int
    timestamp: datetime
    overall_result: str
    average_level: float
    selected_frequency: float
    selected_frequency_level: float
    purpose: str


ATTEMPTS = [
    Attempt("B", "DEMO_B_000001", 1, datetime(2025, 1, 15, 9, 0, 0), "PASS", 67.2, 3000.0, 58.0, "Normal PASS"),
    Attempt("B", "DEMO_B_000002", 1, datetime(2025, 1, 15, 9, 10, 0), "FAIL", 78.2, 3000.0, 59.5, "Initial FAIL: average level above USL"),
    Attempt("B", "DEMO_B_000002", 2, datetime(2025, 1, 15, 9, 20, 0), "PASS", 68.1, 3000.0, 58.9, "Retest PASS"),
    Attempt("B", "DEMO_B_000003", 1, datetime(2025, 1, 15, 9, 30, 0), "FAIL", 66.8, 3500.0, 57.0, "Initial FAIL: frequency above USL"),
    Attempt("B", "DEMO_B_000003", 2, datetime(2025, 1, 15, 9, 40, 0), "FAIL", 58.7, 3000.0, 57.8, "Retest FAIL: average level below LSL"),
    Attempt("K", "DEMO_K_000001", 1, datetime(2025, 1, 16, 9, 0, 0), "PASS", 69.5, 2900.0, 60.0, "Second model PASS"),
    Attempt("K", "DEMO_K_000002", 1, datetime(2025, 1, 16, 9, 10, 0), "PASS", 70.1, 3000.0, 61.2, "Initial PASS"),
    Attempt("K", "DEMO_K_000002", 2, datetime(2025, 1, 16, 9, 20, 0), "PASS", 69.8, 3000.0, 60.9, "Repeated PASS measurement"),
    Attempt("K", "DEMO_K_000003", 1, datetime(2025, 1, 16, 9, 30, 0), "FAIL", 68.8, 3000.0, 72.5, "Initial FAIL: selected-frequency level above USL"),
    Attempt("K", "DEMO_K_000003", 2, datetime(2025, 1, 16, 9, 40, 0), "PASS", 68.6, 3000.0, 62.0, "Retest PASS"),
]


def _result(value: float, lsl: float, usl: float) -> str:
    return "PASS" if lsl <= value <= usl else "FAIL"


def _write_csv(path: Path, rows: Iterable[Sequence[object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.writer(stream, lineterminator="\n")
        writer.writerows(rows)


def _write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8-sig")


def _frequency_response(attempt: Attempt) -> Dict[int, float]:
    seed = sum(ord(character) for character in attempt.sn) + attempt.attempt_no * 101
    generator = random.Random(seed)
    values = {}
    for frequency in range(500, 8001, 100):
        baseline = 18.0 + 2.5 * math.sin(frequency / 620.0)
        primary = (attempt.selected_frequency_level - baseline) * math.exp(
            -0.5 * ((frequency - attempt.selected_frequency) / 230.0) ** 2
        )
        harmonic_frequency = attempt.selected_frequency * 2.0
        harmonic = 18.0 * math.exp(-0.5 * ((frequency - harmonic_frequency) / 320.0) ** 2)
        jitter = generator.uniform(-0.7, 0.7)
        values[frequency] = round(baseline + primary + harmonic + jitter, 3)
    selected_key = int(attempt.selected_frequency)
    if selected_key in values:
        values[selected_key] = attempt.selected_frequency_level
    return values


def _level_series(attempt: Attempt) -> List[float]:
    seed = sum(ord(character) for character in attempt.sn) + attempt.attempt_no * 313
    generator = random.Random(seed)
    return [round(attempt.average_level + generator.uniform(-0.18, 0.18), 3) for _ in range(30)]


def _raw_header(x_labels: Sequence[object], unit_header: str) -> List[object]:
    return ["TestDate", "TestTime", "project", unit_header, "Result"] + list(x_labels)


def _raw_row(
    attempt: Attempt,
    project_code: str,
    raw_unit: str,
    result: str,
    values: Sequence[object],
) -> List[object]:
    return [
        attempt.timestamp.strftime("%Y/%m/%d"),
        attempt.timestamp.strftime("%H:%M:%S"),
        project_code,
        raw_unit,
        result.title(),
    ] + list(values)


def _write_raw_files(root: Path, attempt: Attempt) -> None:
    project_code = PROJECTS[attempt.model]
    raw_directory = root / project_code / "ST01" / "RawData"
    prefix = "O" if attempt.overall_result == "PASS" else "X"
    base_stamp = attempt.timestamp.strftime("%Y%m%d%H%M%S")

    response = _frequency_response(attempt)
    frequencies = list(response)
    _write_csv(
        raw_directory / f"{prefix}_{attempt.sn}_{base_stamp}_Freqresp_in_1.csv",
        [
            _raw_header(frequencies, "Hz"),
            _raw_row(attempt, project_code, "dBSPL", "PASS", [response[value] for value in frequencies]),
        ],
    )

    scalar_stamp = (attempt.timestamp + timedelta(seconds=1)).strftime("%Y%m%d%H%M%S")
    average_result = _result(attempt.average_level, 60.0, 76.0)
    frequency_result = _result(attempt.selected_frequency, 2700.0, 3300.0)
    level_result = _result(attempt.selected_frequency_level, 40.0, 70.0)

    _write_csv(
        raw_directory / f"{prefix}_{attempt.sn}_{scalar_stamp}_Ave_Level_meter.csv",
        [
            _raw_header(["Ave. Level meter"], "dBA"),
            _raw_row(attempt, project_code, "dBA", average_result, [attempt.average_level]),
        ],
    )
    _write_csv(
        raw_directory / f"{prefix}_{attempt.sn}_{scalar_stamp}_Hz.csv",
        [
            _raw_header(["Hz"], "Hz"),
            _raw_row(attempt, project_code, "Hz", frequency_result, [attempt.selected_frequency]),
        ],
    )
    _write_csv(
        raw_directory / f"{prefix}_{attempt.sn}_{scalar_stamp}_Hz_dB.csv",
        [
            _raw_header(["Hz-dB"], "dBSPL"),
            _raw_row(attempt, project_code, "dBSPL", level_result, [attempt.selected_frequency_level]),
        ],
    )

    time_labels = [round(index / 10.0, 1) for index in range(1, 31)]
    _write_csv(
        raw_directory / f"{prefix}_{attempt.sn}_{scalar_stamp}_level_Meter_in_1.csv",
        [
            _raw_header(time_labels, "s"),
            _raw_row(attempt, project_code, "dBA", average_result, _level_series(attempt)),
        ],
    )


def _write_result_files(root: Path, attempt: Attempt) -> None:
    project_code = PROJECTS[attempt.model]
    result_directory = root / project_code / "ST01" / "Result"
    prefix = "O" if attempt.overall_result == "PASS" else "X"
    procedure_time = attempt.timestamp + timedelta(seconds=3)
    status_time = attempt.timestamp + timedelta(seconds=4)
    warning = ""
    if attempt.overall_result == "FAIL":
        warning = "[Warning MSG] DEMO threshold exceeded; synthetic failure only.\n"

    procedure = (
        "PUBLIC DEMO DATA - SYNTHETIC VALUES ONLY\n"
        f"Project Code: {project_code}\n"
        "ST01\n"
        f"<Test start> start time : {attempt.timestamp.strftime('%Y.%m.%d %H:%M:%S')}\n"
        "========== DEMO ACOUSTIC TEST ==========\n"
        f"SN: {attempt.sn}\n"
        f"Attempt: {attempt.attempt_no}\n"
        f"Purpose: {attempt.purpose}\n"
        f"{warning}"
        f"Test Result: {attempt.overall_result}\n"
        "Test time: 20.50 sec\n"
        "No actual customer, product, equipment, firmware, or production process is represented.\n"
    )
    status = (
        "PUBLIC DEMO DATA - SYNTHETIC STATUS LOG\n"
        f"Project: {project_code}\n"
        "Station: ST01\n"
        f"SN: {attempt.sn}\n"
        f"Attempt: {attempt.attempt_no}\n"
        f"Result: {attempt.overall_result}\n"
        "Message: Demonstration record completed without access to production hardware.\n"
    )
    _write_text(
        result_directory / f"{prefix}_{attempt.sn}_{procedure_time.strftime('%Y%m%d%H%M%S')}_Procedure.txt",
        procedure,
    )
    _write_text(
        result_directory / f"{prefix}_{attempt.sn}_{status_time.strftime('%Y%m%d%H%M%S')}_StatusInfo.txt",
        status,
    )


def _write_spec(root: Path, model: str) -> None:
    project_code = PROJECTS[model]
    rows = [[
        "project_code", "station", "measurement_name", "value_unit", "weighting",
        "target", "lsl", "usl", "spec_revision", "effective_from", "effective_to",
        "spec_status", "source_note",
    ]]
    for measurement_name, (target, lsl, usl, unit, weighting) in SPEC_LIMITS.items():
        rows.append([
            project_code, "ST01", measurement_name, unit, weighting,
            "" if target is None else target, lsl, usl, "DEMO_SPEC_V1", "", "",
            "DEMO", "Synthetic public demonstration specification",
        ])
    _write_csv(root / project_code / "ST01" / "Spec" / f"{project_code}_ST01_Spec.csv", rows)


def _write_manifest(root: Path) -> None:
    rows = [[
        "project_code", "model", "sn", "attempt_no", "timestamp", "expected_result",
        "average_level_dba", "selected_frequency_hz", "selected_frequency_level_dbspl", "purpose",
    ]]
    for attempt in ATTEMPTS:
        rows.append([
            PROJECTS[attempt.model], attempt.model, attempt.sn, attempt.attempt_no,
            attempt.timestamp.isoformat(timespec="seconds"), attempt.overall_result,
            attempt.average_level, attempt.selected_frequency,
            attempt.selected_frequency_level, attempt.purpose,
        ])
    _write_csv(root / "DEMO_EXPECTED_RESULTS.csv", rows)


def _write_readme(root: Path) -> None:
    content = """# AcousticSQLiteTool Public Demo Data

All identifiers, timestamps, specifications, measurements, and results in this
folder are synthetic. They do not represent any actual customer, product,
station, supplier, employee, equipment, firmware, or production process.

## Included scenarios

- `DEMO_B_000001`: PASS
- `DEMO_B_000002`: FAIL, then PASS retest
- `DEMO_B_000003`: FAIL, then FAIL retest
- `DEMO_K_000001`: PASS
- `DEMO_K_000002`: PASS, then repeated PASS measurement
- `DEMO_K_000003`: FAIL, then PASS retest

## Import example

```bat
python acoustic_sqlite_importer.py ^
  --root "Demo_Public" ^
  --database "database\\acoustic_demo.db" ^
  --retest ^
  --cpk both
```

The dataset intentionally contains passing and failing scalar measurements so
PASS/FAIL, SN history, retest-rate, specification, CPK, Excel, HTML, and chart
features can be demonstrated.
"""
    _write_text(root / "README.md", content)


def generate(output_directory: Path) -> None:
    output_directory.mkdir(parents=True, exist_ok=True)
    for model in PROJECTS:
        _write_spec(output_directory, model)
    for attempt in ATTEMPTS:
        _write_raw_files(output_directory, attempt)
        _write_result_files(output_directory, attempt)
    _write_manifest(output_directory)
    _write_readme(output_directory)


def main() -> int:
    parser = argparse.ArgumentParser(description="Generate sanitized public Demo acoustic files.")
    parser.add_argument("--output", default="Demo_Public", help="Output directory")
    arguments = parser.parse_args()
    output_directory = Path(arguments.output).expanduser().resolve()
    generate(output_directory)
    print(f"Generated public Demo data: {output_directory}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
