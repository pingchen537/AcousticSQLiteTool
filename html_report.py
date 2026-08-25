"""Generate a single-file, fully offline HTML acoustic report."""

from __future__ import annotations

import html
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple, Union


PathLike = Union[str, Path]


LABELS = {
    "project_code": "機種",
    "model_variant": "型號",
    "station": "測站",
    "sn": "SN",
    "anchor_time": "測試時間",
    "start_time": "開始時間",
    "overall_result": "整機結果",
    "duration_sec": "測試秒數",
    "attempt_no": "測試次序",
    "attempt_count": "總測試次數",
    "is_retest": "是否重測",
    "total_attempts": "測試次數",
    "total_sn": "SN 數",
    "pass_attempts": "PASS 次數",
    "fail_attempts": "FAIL 次數",
    "unknown_attempts": "未知次數",
    "attempt_pass_rate_pct": "測試 PASS 率(%)",
    "first_pass_sn": "首次 PASS SN",
    "first_pass_yield_pct": "首次良率(%)",
    "final_pass_sn": "最終 PASS SN",
    "final_yield_pct": "最終良率(%)",
    "retested_sn": "重測 SN",
    "retest_rate_pct": "重測率(%)",
    "first_test_time": "首次測試",
    "latest_test_time": "最後測試",
    "first_result": "首次結果",
    "latest_result": "最後結果",
    "fail_then_pass": "FAIL 後 PASS",
    "ever_passed": "曾經 PASS",
    "measurement_name": "量測項目",
    "measured_value": "量測值",
    "value_unit": "單位",
    "weighting": "加權",
    "target": "Target",
    "lsl": "LSL",
    "usl": "USL",
    "spec_revision": "規格版本",
    "spec_status": "規格狀態",
    "source_note": "規格備註",
    "effective_from": "生效日期",
    "effective_to": "失效日期",
    "evaluated_result": "規格判定",
    "dataset": "資料集",
    "n": "樣本數",
    "mean": "平均值",
    "sample_stddev": "樣本標準差",
    "cpl": "CPL",
    "cpu": "CPU",
    "cpk": "CPK",
}


SUMMARY_COLUMNS = (
    "project_code",
    "station",
    "total_attempts",
    "total_sn",
    "pass_attempts",
    "fail_attempts",
    "attempt_pass_rate_pct",
    "first_pass_yield_pct",
    "final_yield_pct",
    "retested_sn",
    "retest_rate_pct",
)

TEST_COLUMNS = (
    "project_code",
    "station",
    "sn",
    "anchor_time",
    "overall_result",
    "duration_sec",
    "attempt_no",
    "attempt_count",
)

RETEST_COLUMNS = (
    "project_code",
    "station",
    "sn",
    "attempt_count",
    "first_test_time",
    "latest_test_time",
    "first_result",
    "latest_result",
    "fail_then_pass",
)

CPK_COLUMNS = (
    "project_code",
    "station",
    "measurement_name",
    "value_unit",
    "weighting",
    "spec_revision",
    "lsl",
    "usl",
    "n",
    "mean",
    "sample_stddev",
    "cpl",
    "cpu",
    "cpk",
)

SPEC_COLUMNS = (
    "project_code",
    "station",
    "measurement_name",
    "value_unit",
    "weighting",
    "target",
    "lsl",
    "usl",
    "spec_revision",
    "spec_status",
    "effective_from",
    "effective_to",
    "source_note",
)

MEASUREMENT_COLUMNS = (
    "project_code",
    "station",
    "sn",
    "anchor_time",
    "attempt_no",
    "measurement_name",
    "measured_value",
    "value_unit",
    "weighting",
    "lsl",
    "usl",
    "evaluated_result",
)


def _format_value(value: Any, key: Optional[str] = None) -> str:
    if value is None:
        return "-"
    if key in ("is_retest", "fail_then_pass", "ever_passed"):
        return "是" if bool(value) else "否"
    if isinstance(value, float):
        if key and key.endswith("_pct"):
            return "{:.2f}%".format(value)
        return "{:.4f}".format(value)
    return str(value)


def _status_class(row: Dict[str, Any]) -> str:
    status = str(
        row.get("evaluated_result")
        or row.get("overall_result")
        or row.get("latest_result")
        or ""
    ).upper()
    if status == "PASS":
        return "row-pass"
    if status == "FAIL":
        return "row-fail"
    return ""


def _table(
    rows: Sequence[Dict[str, Any]],
    columns: Sequence[str],
    limit: Optional[int] = None,
) -> str:
    if not rows:
        return '<p class="empty">沒有符合資料</p>'
    displayed = list(rows[:limit]) if limit is not None else list(rows)
    parts = ['<div class="table-wrap"><table><thead><tr>']
    parts.extend(
        "<th>{}</th>".format(html.escape(LABELS.get(column, column)))
        for column in columns
    )
    parts.append("</tr></thead><tbody>")
    for row in displayed:
        parts.append('<tr class="{}">'.format(_status_class(row)))
        for column in columns:
            text = _format_value(row.get(column), column)
            cell_class = ""
            if column == "cpk" and row.get(column) is not None:
                cell_class = " cpk-low" if float(row[column]) < 1.33 else " cpk-ok"
            if column in ("overall_result", "evaluated_result", "first_result", "latest_result"):
                status = str(row.get(column) or "UNKNOWN").upper()
                text = '<span class="badge badge-{}">{}</span>'.format(
                    status.lower(), html.escape(status)
                )
            else:
                text = html.escape(text)
            parts.append('<td class="{}">{}</td>'.format(cell_class.strip(), text))
        parts.append("</tr>")
    parts.append("</tbody></table></div>")
    if limit is not None and len(rows) > limit:
        parts.append(
            '<p class="note">共 {:,} 筆，本頁顯示前 {:,} 筆；完整明細請查看 Excel。</p>'.format(
                len(rows), limit
            )
        )
    return "".join(parts)


def _summary_totals(summary: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    total_attempts = sum(int(row.get("total_attempts") or 0) for row in summary)
    total_sn = sum(int(row.get("total_sn") or 0) for row in summary)
    pass_attempts = sum(int(row.get("pass_attempts") or 0) for row in summary)
    fail_attempts = sum(int(row.get("fail_attempts") or 0) for row in summary)
    retested_sn = sum(int(row.get("retested_sn") or 0) for row in summary)
    final_pass_sn = sum(int(row.get("final_pass_sn") or 0) for row in summary)
    return {
        "total_attempts": total_attempts,
        "total_sn": total_sn,
        "pass_attempts": pass_attempts,
        "fail_attempts": fail_attempts,
        "retest_rate_pct": 100.0 * retested_sn / total_sn if total_sn else None,
        "final_yield_pct": 100.0 * final_pass_sn / total_sn if total_sn else None,
    }


def _kpi_cards(summary: Sequence[Dict[str, Any]]) -> str:
    totals = _summary_totals(summary)
    cards = [
        ("測試次數", totals["total_attempts"], "blue"),
        ("不重複 SN", totals["total_sn"], "blue"),
        ("PASS 次數", totals["pass_attempts"], "green"),
        ("FAIL 次數", totals["fail_attempts"], "red"),
        ("最終良率", _format_value(totals["final_yield_pct"], "final_yield_pct"), "green"),
        ("重測率", _format_value(totals["retest_rate_pct"], "retest_rate_pct"), "amber"),
    ]
    return '<div class="kpi-grid">{}</div>'.format(
        "".join(
            '<div class="kpi {}"><div class="kpi-label">{}</div><div class="kpi-value">{}</div></div>'.format(
                color, html.escape(label), html.escape(str(value))
            )
            for label, value, color in cards
        )
    )


def _pass_fail_svg(summary: Sequence[Dict[str, Any]]) -> str:
    if not summary:
        return ""
    width = 900
    label_width = 180
    chart_width = 620
    row_height = 48
    height = 55 + row_height * len(summary)
    maximum = max(
        1,
        max(int(row.get("pass_attempts") or 0) + int(row.get("fail_attempts") or 0) for row in summary),
    )
    svg = [
        '<svg class="chart" viewBox="0 0 {} {}" role="img" aria-label="各機種 PASS 與 FAIL 次數">'.format(
            width, height
        ),
        '<text x="{}" y="22" class="chart-title">PASS / FAIL 次數</text>'.format(label_width),
    ]
    for index, row in enumerate(summary):
        y = 45 + index * row_height
        passed = int(row.get("pass_attempts") or 0)
        failed = int(row.get("fail_attempts") or 0)
        pass_width = chart_width * passed / maximum
        fail_width = chart_width * failed / maximum
        label = "{} / {}".format(row.get("project_code", ""), row.get("station", ""))
        svg.append('<text x="4" y="{}" class="chart-label">{}</text>'.format(y + 17, html.escape(label)))
        svg.append('<rect x="{}" y="{}" width="{}" height="24" rx="4" fill="#16A34A"/>'.format(label_width, y, pass_width))
        svg.append('<rect x="{}" y="{}" width="{}" height="24" rx="4" fill="#DC2626"/>'.format(label_width + pass_width, y, fail_width))
        svg.append('<text x="{}" y="{}" class="bar-value">PASS {} / FAIL {}</text>'.format(label_width + 8, y + 17, passed, failed))
    svg.append("</svg>")
    return "".join(svg)


def _cpk_compare_rows(data: Dict[str, Any]) -> List[Dict[str, Any]]:
    combined = {}  # type: Dict[Tuple[Any, ...], Dict[str, Any]]
    for key_name, output_key in (("cpk_first", "cpk_first"), ("cpk_all", "cpk_all")):
        for row in data.get(key_name, []):
            key = (row.get("project_code"), row.get("station"), row.get("measurement_name"))
            combined.setdefault(
                key,
                {
                    "project_code": key[0],
                    "station": key[1],
                    "measurement_name": key[2],
                    "cpk_first": None,
                    "cpk_all": None,
                },
            )[output_key] = row.get("cpk")
    return [combined[key] for key in sorted(combined, key=lambda item: tuple(str(x) for x in item))]


def _cpk_svg(rows: Sequence[Dict[str, Any]]) -> str:
    available = [
        row for row in rows if row.get("cpk_first") is not None or row.get("cpk_all") is not None
    ]
    if not available:
        return '<p class="empty">CPK 樣本數不足，暫無圖表。</p>'
    width = 900
    label_width = 260
    chart_width = 540
    row_height = 54
    height = 65 + row_height * len(available)
    max_value = max(
        1.33,
        max(float(value) for row in available for value in (row.get("cpk_first"), row.get("cpk_all")) if value is not None),
    )
    max_value *= 1.1
    threshold_x = label_width + chart_width * 1.33 / max_value
    svg = [
        '<svg class="chart" viewBox="0 0 {} {}" role="img" aria-label="CPK 比較">'.format(width, height),
        '<text x="{}" y="22" class="chart-title">CPK：第一次有效量測 vs. 全部測試</text>'.format(label_width),
        '<line x1="{}" y1="36" x2="{}" y2="{}" stroke="#D97706" stroke-width="2" stroke-dasharray="5 4"/>'.format(threshold_x, threshold_x, height - 10),
        '<text x="{}" y="50" class="threshold">1.33</text>'.format(threshold_x + 4),
    ]
    for index, row in enumerate(available):
        y = 58 + index * row_height
        project_label = "{} / {}".format(row.get("project_code", ""),row.get("station", ""),)
        measurement_label = str(row.get("measurement_name", ""))
        svg.append('<text x="12" y="{}" class="chart-label">''<tspan x="12" dy="0">{}</tspan>''<tspan x="12" dy="15" class="chart-label-measurement">{}</tspan>''</text>'.format(y + 10,
        html.escape(project_label),
        html.escape(measurement_label),
    )
)
        for offset, key, color in ((0, "cpk_first", "#2563EB"), (20, "cpk_all", "#7C3AED")):
            value = row.get(key)
            if value is None:
                continue
            bar_width = chart_width * max(0.0, float(value)) / max_value
            svg.append('<rect x="{}" y="{}" width="{}" height="15" rx="3" fill="{}"/>'.format(label_width, y + offset, bar_width, color))
            svg.append('<text x="{}" y="{}" class="bar-number">{:.3f}</text>'.format(label_width + bar_width + 5, y + offset + 12, float(value)))
    svg.append("</svg>")
    return "".join(svg)


def _filters_text(metadata: Dict[str, Any]) -> str:
    filters = metadata.get("filters", {})
    pieces = []
    labels = {
        "project_code": "機種",
        "station": "測站",
        "sn": "SN",
        "date_from": "起始日期",
        "date_to": "結束日期",
    }
    for key in ("project_code", "station", "sn", "date_from", "date_to"):
        if filters.get(key):
            pieces.append("{}：{}".format(labels[key], filters[key]))
    return "；".join(pieces) if pieces else "全部資料"


def _section(title: str, content: str, subtitle: Optional[str] = None) -> str:
    return '<section><div class="section-head"><h2>{}</h2>{}</div>{}</section>'.format(
        html.escape(title),
        '<p>{}</p>'.format(html.escape(subtitle)) if subtitle else "",
        content,
    )


def generate_html_report(report_data: Dict[str, Any], output_path: PathLike) -> Path:
    """Write a self-contained HTML file with no network dependencies."""
    path = Path(output_path).expanduser().resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    metadata = report_data["metadata"]
    detail_limit = int(metadata.get("detail_limit") or 0)
    limit = detail_limit if detail_limit > 0 else None
    cpk_compare = _cpk_compare_rows(report_data)

    warnings_html = ""
    if metadata.get("warnings"):
        warnings_html = '<div class="warning"><strong>注意事項</strong><ul>{}</ul></div>'.format(
            "".join("<li>{}</li>".format(html.escape(item)) for item in metadata["warnings"])
        )

    body = [
        (
            '<header><div><div class="eyebrow">OFFLINE ACOUSTIC QUALITY REPORT</div>'
            '<h1>{}</h1>'
            '<p class="meta">產生時間：{}<br>篩選條件：{}<br>資料庫：{}</p></div>'
            '<div class="offline">完全離線</div></header>'
        ).format(
            html.escape(metadata["title"]),
            html.escape(metadata["generated_at"]),
            html.escape(_filters_text(metadata)),
            html.escape(metadata["database_path"]),
        ),
        warnings_html,
        _section(
            "製程摘要",
            _kpi_cards(report_data["summary"])
            + _pass_fail_svg(report_data["summary"])
            + _table(report_data["summary"], SUMMARY_COLUMNS),
        ),
        _section(
            "CPK 比較",
            _cpk_svg(cpk_compare)
            + '<h3>每個 SN 第一次有效量測</h3>'
            + _table(report_data["cpk_first"], CPK_COLUMNS)
            + '<h3>所有測試（包含重測）</h3>'
            + _table(report_data["cpk_all"], CPK_COLUMNS),
            "CPK 使用樣本標準差；樣本數不足 2 或標準差為 0 時留白。",
        ),
        _section("FAIL 測試", _table(report_data["fail_tests"], TEST_COLUMNS, limit)),
        _section("重測 SN", _table(report_data["retests"], RETEST_COLUMNS, limit)),
        _section("PASS 測試", _table(report_data["pass_tests"], TEST_COLUMNS, limit)),
        _section("量測值與規格判定", _table(report_data["measurements"], MEASUREMENT_COLUMNS, limit)),
        _section("規格版本", _table(report_data["specifications"], SPEC_COLUMNS)),
        '<footer>此報告由本機 SQLite 產生，不包含任何網路資源。報告為產生當下的資料快照。</footer>',
    ]

    css = """
    :root {
        --navy: #12304a;
        --blue: #2563eb;
        --green: #15803d;
        --red: #b91c1c;
        --amber: #b45309;
        --ink: #1f2937;
        --muted: #64748b;
        --line: #dbe3eb;
        --paper: #ffffff;
        --bg: #f3f6f9;
    }

    * {
        box-sizing: border-box;
    }

    body {
        margin: 0;
        background: var(--bg);
        color: var(--ink);
        font-family:
            "Microsoft JhengHei",
            "Noto Sans TC",
            "Segoe UI",
            Arial,
            sans-serif;
        line-height: 1.5;
    }

    main {
        max-width: 1440px;
        margin: 0 auto;
        padding: 28px;
    }

    header {
        display: flex;
        justify-content: space-between;
        align-items: flex-start;
        gap: 24px;
        padding: 30px 34px;
        color: #ffffff;
        background: linear-gradient(135deg, #12304a, #1e4d70);
        border-radius: 16px;
        box-shadow: 0 12px 30px #0f274033;
    }

    h1 {
        margin: 4px 0 10px;
        font-size: 30px;
    }

    h3 {
        margin: 22px 0 8px;
        color: var(--navy);
        font-size: 15px;
    }

    .eyebrow {
        color: #b9d8ee;
        font-size: 12px;
        letter-spacing: 0.16em;
    }

    .meta {
        margin: 0;
        color: #dceaf4;
        font-size: 13px;
    }

    .offline {
        padding: 8px 14px;
        white-space: nowrap;
        background: #ffffff1c;
        border: 1px solid #ffffff55;
        border-radius: 999px;
    }

    section {
        margin-top: 22px;
        padding: 24px;
        background: var(--paper);
        border-radius: 14px;
        box-shadow: 0 4px 18px #243b5320;
        break-inside: avoid;
    }

    .section-head {
        display: flex;
        justify-content: space-between;
        align-items: baseline;
        gap: 16px;
        margin-bottom: 18px;
        border-bottom: 1px solid var(--line);
    }

    .section-head h2 {
        margin: 0 0 10px;
        color: var(--navy);
        font-size: 20px;
    }

    .section-head p {
        margin: 0 0 10px;
        color: var(--muted);
        font-size: 12px;
    }

    .kpi-grid {
        display: grid;
        grid-template-columns: repeat(6, minmax(130px, 1fr));
        gap: 12px;
    }

    .kpi {
        padding: 15px;
        background: #f8fafc;
        border-left: 5px solid var(--blue);
        border-radius: 10px;
    }

    .kpi.green {
        border-color: var(--green);
    }

    .kpi.red {
        border-color: var(--red);
    }

    .kpi.amber {
        border-color: var(--amber);
    }

    .kpi-label {
        color: var(--muted);
        font-size: 12px;
    }

    .kpi-value {
        margin-top: 4px;
        color: var(--navy);
        font-size: 24px;
        font-weight: 700;
    }

    .table-wrap {
        overflow: auto;
        border: 1px solid var(--line);
        border-radius: 10px;
    }

    table {
        width: 100%;
        border-collapse: collapse;
        font-size: 12px;
    }

    th {
        position: sticky;
        top: 0;
        padding: 9px 10px;
        color: #ffffff;
        text-align: left;
        white-space: nowrap;
        background: var(--navy);
    }

    td {
        padding: 8px 10px;
        white-space: nowrap;
        border-bottom: 1px solid #e9eef3;
    }

    tr:nth-child(even) {
        background: #f8fafc;
    }

    .row-fail {
        background: #fff1f2 !important;
    }

    .badge {
        display: inline-block;
        padding: 2px 8px;
        font-size: 11px;
        font-weight: 700;
        border-radius: 999px;
    }

    .badge-pass {
        color: #166534;
        background: #dcfce7;
    }

    .badge-fail {
        color: #991b1b;
        background: #fee2e2;
    }

    .badge-unknown,
    .badge-no_spec,
    .badge-no_value {
        color: #475569;
        background: #e2e8f0;
    }

    .cpk-low {
        color: #b91c1c;
        font-weight: 700;
    }

    .cpk-ok {
        color: #15803d;
        font-weight: 700;
    }

    .chart {
        display: block;
        width: 100%;
        max-width: 1050px;
        margin: 18px 0;
        padding: 8px;
        background: #f8fafc;
        border: 1px solid var(--line);
        border-radius: 10px;
    }

    .chart-title {
        fill: #12304a;
        font-size: 16px;
        font-weight: 700;
    }

    .chart-label {
        fill: #334155;
        font-size: 11px;
    }

    .chart-label-measurement {
        fill: #64748b;
        font-size: 10px;
        font-weight: 600;
    }

    .bar-value {
        fill: #ffffff;
        font-size: 11px;
        font-weight: 700;
    }

    .bar-number,
    .threshold {
        fill: #475569;
        font-size: 10px;
    }

    .warning {
        margin-top: 20px;
        padding: 14px 18px;
        color: #9a3412;
        background: #fff7ed;
        border: 1px solid #fed7aa;
        border-radius: 10px;
    }

    .warning ul {
        margin: 6px 0 0;
    }

    .empty,
    .note {
        color: var(--muted);
        font-size: 12px;
    }

    .note {
        margin: 8px 2px;
    }

    footer {
        padding: 28px;
        color: var(--muted);
        font-size: 11px;
        text-align: center;
    }

    @media (max-width: 1000px) {
        main {
            padding: 14px;
        }

        .kpi-grid {
            grid-template-columns: repeat(2, 1fr);
        }

        header {
            flex-direction: column;
        }

        .section-head {
            display: block;
        }
    }

    @media print {
        body {
            background: #ffffff;
        }

        main {
            max-width: none;
            padding: 0;
        }

        header,
        section {
            border: 1px solid #ccd6df;
            border-radius: 0;
            box-shadow: none;
        }

        section {
            break-inside: auto;
        }

        .table-wrap {
            overflow: visible;
        }

        th {
            position: static;
        }

        .offline {
            display: none;
        }

        @page {
            size: A4 landscape;
            margin: 10mm;
        }
    }
    """

    document = """<!doctype html><html lang="zh-Hant"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>{title}</title><style>{css}</style></head><body><main>{body}</main></body></html>
    """.format(title=html.escape(metadata["title"]), css=css, body="".join(body))
    path.write_text(document, encoding="utf-8")
    return path
