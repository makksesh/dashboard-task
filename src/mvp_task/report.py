"""Generate a period report entirely from PostgreSQL."""

from __future__ import annotations

from datetime import date, datetime
from io import BytesIO

from openpyxl import Workbook
from openpyxl.styles import Font, PatternFill

from .domain import LABELS
from .repository import dashboard, list_rows


def make_report(start: date, end: date) -> bytes:
    data = dashboard(start, end)
    book = Workbook()
    summary = book.active
    summary.title = "Сводка"
    summary.append(["Период", f"{start.isoformat()} — {end.isoformat()}"])
    summary.append(["Показатель", "Значение"])
    prep = data["preparation"]
    weld = data["welding_need"]
    docs = data["documentation"]
    for item in (
        ("Заготовка: подтверждённый план", prep["plan_qty"]),
        ("Заготовка: подтверждённый факт", prep["fact_qty"]),
        ("Заготовка: выполнение подтверждённого плана", f"{data['prep_percent']:.1f}%" if data["prep_percent"] is not None else "Не рассчитано"),
        ("Заготовка: подтверждено строк", f"{prep['confirmed']} из {prep['rows']}"),
        ("Сварка: подтверждённая потребность", weld["need_qty"]),
        ("Возможное непокрытие по проверенным партиям", data["known_gap"]),
        ("Потребность без подтверждённого факта заготовки", data["unknown_need"]),
        ("Документация: подтверждённый план", docs["plan_docs"]),
        ("Документация: подтверждённый факт", docs["fact_docs"]),
        ("Документация: подтверждено строк", f"{docs['confirmed']} из {docs['rows']}"),
    ):
        summary.append(item)

    for source in ("preparation", "welding_need", "documentation"):
        sheet = book.create_sheet(LABELS[source])
        rows = list_rows(source, start, end)
        if rows:
            fields = [key for key in rows[0] if key not in ("import_id", "source_row")]
            sheet.append(fields)
            for row in rows:
                sheet.append([_cell(row[key]) for key in fields])
        else:
            sheet.append(["Нет записей за период"])
    batches = book.create_sheet("Связь партий")
    headers = ("batch_id", "plan_qty", "fact_qty", "need_qty", "possible_gap", "link_status")
    batches.append(headers)
    for row in data["batches"]:
        batches.append([row[key] for key in headers])
    totals = data["batch_totals"]
    batches.append(["ИТОГО ЗА ПЕРИОД", totals["plan_qty"], totals["fact_qty"], totals["need_qty"], totals["possible_gap"], ""])
    total_row = batches.max_row
    for cell in batches[total_row]:
        cell.font = Font(bold=True, color="193044")
        cell.fill = PatternFill("solid", fgColor="E8F3F8")
    if data["unknown_need"]:
        batches.append([f"Потребность без подтверждённого факта заготовки: {data['unknown_need']}"])

    for sheet in book:
        for cell in sheet[1]:
            cell.font = Font(bold=True, color="FFFFFF")
            cell.fill = PatternFill("solid", fgColor="24547A")
        sheet.freeze_panes = "A2"
        sheet.auto_filter.ref = sheet.dimensions
        for column in sheet.columns:
            letter = column[0].column_letter
            width = min(54, max(14, max(len(str(cell.value or "")) for cell in column) + 2))
            sheet.column_dimensions[letter].width = width
    buffer = BytesIO()
    book.save(buffer)
    return buffer.getvalue()


def _cell(value):
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, str) and value.startswith(("=", "+", "-", "@")):
        return "'" + value
    return value
