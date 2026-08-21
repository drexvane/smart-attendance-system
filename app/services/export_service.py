"""Attendance export.

Ported from ``src/utils/helpers.py``.  Three changes: exports are streamed to the
client instead of written to a file on disk that nothing ever cleaned up, the row count
is bounded (the old ``/export_attendance`` ran an unbounded query and could pull the
whole table into memory), and ``openpyxl`` is now a declared dependency instead of an
optional import that silently downgraded XLSX requests to CSV.
"""

from __future__ import annotations

import csv
import io
import logging
from collections.abc import Sequence

from openpyxl import Workbook
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter

from app.models import AttendanceRecord
from app.utils.time import to_local

logger = logging.getLogger(__name__)

HEADERS = (
    "Date",
    "Student ID",
    "Student Name",
    "Time In",
    "Time Out",
    "Status",
    "Department",
    "Year",
    "Section",
    "Confidence",
    "Marked By",
)

_STATUS_FILLS = {
    "Present": "D4EDDA",
    "Absent": "F8D7DA",
    "Late": "FFF3CD",
    "On Leave": "D1ECF1",
}


def _row(record: AttendanceRecord) -> list[str]:
    student = record.student
    time_in = to_local(record.time_in)
    time_out = to_local(record.time_out)
    return [
        record.date.isoformat() if record.date else "",
        student.student_id if student else "",
        student.name if student else "",
        time_in.strftime("%H:%M:%S") if time_in else "",
        time_out.strftime("%H:%M:%S") if time_out else "",
        record.status or "",
        (student.department or "") if student else "",
        (student.year or "") if student else "",
        (student.section or "") if student else "",
        f"{record.confidence_score:.2f}" if record.confidence_score is not None else "",
        record.marked_by or "System",
    ]


def to_csv_bytes(records: Sequence[AttendanceRecord]) -> bytes:
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(HEADERS)
    for record in records:
        writer.writerow(_row(record))
    # utf-8-sig so Excel opens non-ASCII names correctly without a manual import step.
    return buffer.getvalue().encode("utf-8-sig")


def to_xlsx_bytes(records: Sequence[AttendanceRecord]) -> bytes:
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "Attendance Records"

    header_font = Font(bold=True, color="FFFFFF")
    header_fill = PatternFill("solid", start_color="366092", end_color="366092")
    centered = Alignment(horizontal="center", vertical="center")
    thin = Side(style="thin")
    border = Border(left=thin, right=thin, top=thin, bottom=thin)

    for column, header in enumerate(HEADERS, start=1):
        cell = sheet.cell(row=1, column=column, value=header)
        cell.font = header_font
        cell.fill = header_fill
        cell.alignment = centered
        cell.border = border

    status_column = HEADERS.index("Status") + 1
    widths = [len(header) for header in HEADERS]

    for row_index, record in enumerate(records, start=2):
        for column, value in enumerate(_row(record), start=1):
            cell = sheet.cell(row=row_index, column=column, value=value)
            cell.border = border
            cell.alignment = Alignment(horizontal="left", vertical="center")
            if column == status_column and value in _STATUS_FILLS:
                colour = _STATUS_FILLS[value]
                cell.fill = PatternFill("solid", start_color=colour, end_color=colour)
            widths[column - 1] = max(widths[column - 1], len(str(value)))

    for index, width in enumerate(widths, start=1):
        sheet.column_dimensions[get_column_letter(index)].width = min(width + 2, 50)

    sheet.freeze_panes = "A2"

    summary_row = len(records) + 3
    counts: dict[str, int] = {}
    for record in records:
        counts[record.status] = counts.get(record.status, 0) + 1

    sheet.cell(row=summary_row, column=1, value="Summary").font = Font(bold=True)
    sheet.cell(row=summary_row + 1, column=1, value=f"Total records: {len(records)}")
    for offset, (status, total) in enumerate(sorted(counts.items()), start=2):
        sheet.cell(row=summary_row + offset, column=1, value=f"{status}: {total}")

    buffer = io.BytesIO()
    workbook.save(buffer)
    return buffer.getvalue()


def build_export(records: Sequence[AttendanceRecord], fmt: str) -> tuple[bytes, str, str]:
    """Return ``(payload, mimetype, extension)`` for the requested format."""
    if fmt == "excel":
        return (
            to_xlsx_bytes(records),
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            "xlsx",
        )
    return to_csv_bytes(records), "text/csv; charset=utf-8", "csv"
