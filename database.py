"""SQLite schema, indexes, views, and low-level database helpers.

Compatible with Python 3.8 and SQLite 3.35+ (tested with SQLite 3.45.3).
"""

from __future__ import annotations

import hashlib
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Optional, Tuple, Union


PathLike = Union[str, Path]
SCHEMA_VERSION = "2"


SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS schema_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS source_files (
    id INTEGER PRIMARY KEY,
    source_path TEXT NOT NULL UNIQUE,
    relative_path TEXT,
    file_kind TEXT NOT NULL,
    sha256 TEXT NOT NULL,
    size_bytes INTEGER NOT NULL,
    mtime_ns INTEGER,
    encoding TEXT,
    imported_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS test_runs (
    id INTEGER PRIMARY KEY,
    run_key TEXT NOT NULL UNIQUE,
    project_code TEXT NOT NULL,
    model_variant TEXT,
    station TEXT NOT NULL,
    sn TEXT NOT NULL,
    anchor_time TEXT NOT NULL,
    start_time TEXT,
    source_prefix TEXT,
    overall_result TEXT NOT NULL DEFAULT 'UNKNOWN'
        CHECK (overall_result IN ('PASS', 'FAIL', 'UNKNOWN')),
    duration_sec REAL,
    error_message TEXT,
    procedure_file_id INTEGER REFERENCES source_files(id),
    status_file_id INTEGER REFERENCES source_files(id),
    imported_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS test_documents (
    id INTEGER PRIMARY KEY,
    test_run_id INTEGER NOT NULL REFERENCES test_runs(id) ON DELETE CASCADE,
    document_type TEXT NOT NULL CHECK (document_type IN ('PROCEDURE', 'STATUSINFO')),
    source_file_id INTEGER NOT NULL UNIQUE REFERENCES source_files(id),
    content TEXT NOT NULL,
    encoding TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS measurements (
    id INTEGER PRIMARY KEY,
    test_run_id INTEGER NOT NULL REFERENCES test_runs(id) ON DELETE CASCADE,
    source_file_id INTEGER NOT NULL UNIQUE REFERENCES source_files(id),
    measurement_name TEXT NOT NULL,
    raw_measurement_name TEXT,
    value_unit TEXT,
    weighting TEXT,
    raw_result TEXT NOT NULL DEFAULT 'UNKNOWN'
        CHECK (raw_result IN ('PASS', 'FAIL', 'UNKNOWN')),
    scalar_value REAL,
    sample_count INTEGER NOT NULL DEFAULT 0,
    test_datetime TEXT,
    project_in_file TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS measurement_points (
    measurement_id INTEGER NOT NULL REFERENCES measurements(id) ON DELETE CASCADE,
    point_index INTEGER NOT NULL,
    x_value REAL,
    x_label TEXT,
    value REAL,
    PRIMARY KEY (measurement_id, point_index)
);

CREATE TABLE IF NOT EXISTS measurement_specs (
    id INTEGER PRIMARY KEY,
    project_code TEXT NOT NULL,
    station TEXT NOT NULL,
    measurement_name TEXT NOT NULL,
    value_unit TEXT,
    weighting TEXT,
    target REAL,
    lsl REAL,
    usl REAL,
    spec_revision TEXT NOT NULL,
    effective_from TEXT,
    effective_to TEXT,
    spec_status TEXT NOT NULL DEFAULT 'ENGINEERING',
    source_note TEXT,
    source_file_id INTEGER REFERENCES source_files(id),
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (project_code, station, measurement_name, spec_revision)
);

CREATE TABLE IF NOT EXISTS import_errors (
    id INTEGER PRIMARY KEY,
    source_path TEXT NOT NULL,
    stage TEXT NOT NULL,
    error_message TEXT NOT NULL,
    occurred_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_test_runs_project_station_time
    ON test_runs(project_code, station, anchor_time);
CREATE INDEX IF NOT EXISTS idx_test_runs_sn_time
    ON test_runs(sn, anchor_time);
CREATE INDEX IF NOT EXISTS idx_test_runs_result
    ON test_runs(overall_result);
CREATE INDEX IF NOT EXISTS idx_measurements_run_name
    ON measurements(test_run_id, measurement_name);
CREATE INDEX IF NOT EXISTS idx_measurement_points_x
    ON measurement_points(measurement_id, x_value);
CREATE INDEX IF NOT EXISTS idx_specs_lookup
    ON measurement_specs(project_code, station, measurement_name, effective_from);
"""


VIEWS_SQL = """
DROP VIEW IF EXISTS v_cpk_first_valid;
DROP VIEW IF EXISTS v_cpk_all_tests;
DROP VIEW IF EXISTS v_measurement_with_spec;
DROP VIEW IF EXISTS v_measurement_points;
DROP VIEW IF EXISTS v_current_specs;
DROP VIEW IF EXISTS v_retest_sn;
DROP VIEW IF EXISTS v_sn_retest_summary;
DROP VIEW IF EXISTS v_latest_test;
DROP VIEW IF EXISTS v_first_valid_test;
DROP VIEW IF EXISTS v_fail_tests;
DROP VIEW IF EXISTS v_pass_tests;
DROP VIEW IF EXISTS v_test_overview;

CREATE VIEW v_test_overview AS
WITH ranked AS (
    SELECT
        tr.*,
        ROW_NUMBER() OVER (
            PARTITION BY tr.project_code, tr.station, tr.sn
            ORDER BY tr.anchor_time, tr.id
        ) AS attempt_no,
        COUNT(*) OVER (
            PARTITION BY tr.project_code, tr.station, tr.sn
        ) AS attempt_count
    FROM test_runs AS tr
)
SELECT
    id AS test_run_id,
    run_key,
    project_code,
    model_variant,
    station,
    sn,
    anchor_time,
    start_time,
    overall_result,
    duration_sec,
    error_message,
    attempt_no,
    attempt_count,
    CASE WHEN attempt_no > 1 THEN 1 ELSE 0 END AS is_retest
FROM ranked;

CREATE VIEW v_pass_tests AS
SELECT * FROM v_test_overview WHERE overall_result = 'PASS';

CREATE VIEW v_fail_tests AS
SELECT * FROM v_test_overview WHERE overall_result = 'FAIL';

CREATE VIEW v_first_valid_test AS
WITH valid_ranked AS (
    SELECT
        v.*,
        ROW_NUMBER() OVER (
            PARTITION BY v.project_code, v.station, v.sn
            ORDER BY v.anchor_time, v.test_run_id
        ) AS valid_rank
    FROM v_test_overview AS v
    WHERE v.overall_result IN ('PASS', 'FAIL')
)
SELECT * FROM valid_ranked WHERE valid_rank = 1;

CREATE VIEW v_latest_test AS
WITH latest_ranked AS (
    SELECT
        v.*,
        ROW_NUMBER() OVER (
            PARTITION BY v.project_code, v.station, v.sn
            ORDER BY v.anchor_time DESC, v.test_run_id DESC
        ) AS latest_rank
    FROM v_test_overview AS v
)
SELECT * FROM latest_ranked WHERE latest_rank = 1;

CREATE VIEW v_sn_retest_summary AS
SELECT
    project_code,
    model_variant,
    station,
    sn,
    COUNT(*) AS attempt_count,
    MIN(anchor_time) AS first_test_time,
    MAX(anchor_time) AS latest_test_time,
    MAX(CASE WHEN attempt_no = 1 THEN overall_result END) AS first_result,
    MAX(CASE WHEN attempt_no = attempt_count THEN overall_result END) AS latest_result,
    MAX(CASE WHEN overall_result = 'PASS' THEN 1 ELSE 0 END) AS ever_passed,
    CASE WHEN COUNT(*) > 1 THEN 1 ELSE 0 END AS is_retested
FROM v_test_overview
GROUP BY project_code, model_variant, station, sn;

CREATE VIEW v_retest_sn AS
SELECT * FROM v_sn_retest_summary WHERE is_retested = 1;

CREATE VIEW v_current_specs AS
WITH ranked_specs AS (
    SELECT
        ms.*,
        ROW_NUMBER() OVER (
            PARTITION BY ms.project_code, ms.station, ms.measurement_name
            ORDER BY
                CASE UPPER(ms.spec_status)
                    WHEN 'ACTIVE' THEN 0
                    WHEN 'ENGINEERING' THEN 1
                    ELSE 2
                END,
                COALESCE(ms.effective_from, '0001-01-01') DESC,
                ms.id DESC
        ) AS spec_rank
    FROM measurement_specs AS ms
    WHERE UPPER(ms.spec_status) NOT IN ('OBSOLETE', 'DISABLED')
)
SELECT * FROM ranked_specs WHERE spec_rank = 1;

CREATE VIEW v_measurement_points AS
SELECT
    tr.project_code,
    tr.model_variant,
    tr.station,
    tr.sn,
    tr.anchor_time,
    tr.overall_result,
    m.id AS measurement_id,
    m.measurement_name,
    m.value_unit,
    m.weighting,
    m.raw_result AS measurement_result,
    mp.point_index,
    mp.x_value,
    mp.x_label,
    mp.value
FROM measurement_points AS mp
JOIN measurements AS m ON m.id = mp.measurement_id
JOIN test_runs AS tr ON tr.id = m.test_run_id;

CREATE VIEW v_measurement_with_spec AS
SELECT
    v.test_run_id,
    v.project_code,
    v.model_variant,
    v.station,
    v.sn,
    v.anchor_time,
    v.attempt_no,
    v.attempt_count,
    v.is_retest,
    v.overall_result,
    m.id AS measurement_id,
    m.measurement_name,
    m.scalar_value AS measured_value,
    COALESCE(s.value_unit, m.value_unit) AS value_unit,
    COALESCE(s.weighting, m.weighting) AS weighting,
    m.raw_result AS measurement_result,
    s.target,
    s.lsl,
    s.usl,
    s.spec_revision,
    s.spec_status,
    CASE
        WHEN m.scalar_value IS NULL THEN 'NO_VALUE'
        WHEN s.id IS NULL THEN 'NO_SPEC'
        WHEN s.lsl IS NOT NULL AND m.scalar_value < s.lsl THEN 'FAIL'
        WHEN s.usl IS NOT NULL AND m.scalar_value > s.usl THEN 'FAIL'
        ELSE 'PASS'
    END AS evaluated_result
FROM v_test_overview AS v
JOIN measurements AS m ON m.test_run_id = v.test_run_id
LEFT JOIN measurement_specs AS s
  ON s.id = (
      SELECT s2.id
      FROM measurement_specs AS s2
      WHERE s2.project_code = v.project_code
        AND s2.station = v.station
        AND s2.measurement_name = m.measurement_name
        AND UPPER(s2.spec_status) NOT IN ('OBSOLETE', 'DISABLED')
        AND (s2.effective_from IS NULL OR date(s2.effective_from) <= date(v.anchor_time))
        AND (s2.effective_to IS NULL OR date(s2.effective_to) >= date(v.anchor_time))
      ORDER BY
          CASE UPPER(s2.spec_status)
              WHEN 'ACTIVE' THEN 0
              WHEN 'ENGINEERING' THEN 1
              ELSE 2
          END,
          COALESCE(s2.effective_from, '0001-01-01') DESC,
          s2.id DESC
      LIMIT 1
  )
WHERE m.scalar_value IS NOT NULL;

CREATE VIEW v_cpk_all_tests AS
SELECT *
FROM v_measurement_with_spec
WHERE measured_value IS NOT NULL
  AND (lsl IS NOT NULL OR usl IS NOT NULL)
  AND overall_result IN ('PASS', 'FAIL');

CREATE VIEW v_cpk_first_valid AS
SELECT c.*
FROM v_cpk_all_tests AS c
JOIN v_first_valid_test AS f ON f.test_run_id = c.test_run_id;
"""


def connect_database(database_path: PathLike) -> sqlite3.Connection:
    """Open the SQLite database with safe defaults for a local importer."""
    path = Path(database_path).expanduser().resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(str(path))
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA journal_mode = WAL")
    connection.execute("PRAGMA synchronous = NORMAL")
    connection.execute("PRAGMA busy_timeout = 5000")
    return connection


def create_schema(connection: sqlite3.Connection) -> None:
    """Create or update schema objects without deleting imported data."""
    connection.executescript(SCHEMA_SQL)
    connection.execute(
        """
        INSERT INTO schema_meta(key, value) VALUES('schema_version', ?)
        ON CONFLICT(key) DO UPDATE SET value = excluded.value
        """,
        (SCHEMA_VERSION,),
    )
    connection.executescript(VIEWS_SQL)
    connection.commit()


def file_sha256(path: PathLike) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def register_source_file(
    connection: sqlite3.Connection,
    path: PathLike,
    root: Optional[PathLike],
    file_kind: str,
    encoding: Optional[str] = None,
) -> Tuple[int, bool]:
    """Register a source file and return ``(source_file_id, changed)``.

    Re-running the importer skips unchanged files. If a file was edited at the
    same path, its metadata is updated and the caller replaces dependent rows.
    """
    file_path = Path(path).resolve()
    stat = file_path.stat()
    digest = file_sha256(file_path)
    source_path = str(file_path)
    relative_path = None
    if root is not None:
        try:
            relative_path = str(file_path.relative_to(Path(root).resolve()))
        except ValueError:
            relative_path = file_path.name

    existing = connection.execute(
        "SELECT id, sha256 FROM source_files WHERE source_path = ?",
        (source_path,),
    ).fetchone()
    now = datetime.now().isoformat(timespec="seconds")

    if existing is not None:
        changed = existing["sha256"] != digest
        connection.execute(
            """
            UPDATE source_files
               SET relative_path = ?, file_kind = ?, sha256 = ?, size_bytes = ?,
                   mtime_ns = ?, encoding = COALESCE(?, encoding), updated_at = ?
             WHERE id = ?
            """,
            (
                relative_path,
                file_kind,
                digest,
                stat.st_size,
                getattr(stat, "st_mtime_ns", int(stat.st_mtime * 1e9)),
                encoding,
                now,
                existing["id"],
            ),
        )
        return int(existing["id"]), changed

    cursor = connection.execute(
        """
        INSERT INTO source_files(
            source_path, relative_path, file_kind, sha256,
            size_bytes, mtime_ns, encoding, imported_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            source_path,
            relative_path,
            file_kind,
            digest,
            stat.st_size,
            getattr(stat, "st_mtime_ns", int(stat.st_mtime * 1e9)),
            encoding,
            now,
            now,
        ),
    )
    return int(cursor.lastrowid), True


def record_import_error(
    connection: sqlite3.Connection,
    source_path: PathLike,
    stage: str,
    error: Exception,
) -> None:
    connection.execute(
        """
        INSERT INTO import_errors(source_path, stage, error_message)
        VALUES (?, ?, ?)
        """,
        (str(Path(source_path)), stage, "{}: {}".format(type(error).__name__, error)),
    )


def refresh_views(connection: sqlite3.Connection) -> None:
    connection.executescript(VIEWS_SQL)
    connection.commit()
