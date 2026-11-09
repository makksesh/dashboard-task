"""Input normalization and transparent quality rules."""

from __future__ import annotations

from datetime import date, datetime
from typing import Any

from openpyxl.utils.datetime import from_excel

SPECS = {
    "preparation": ("record_id", "date", "batch_id", "employee", "plan_qty", "fact_qty"),
    "welding_need": ("record_id", "date", "batch_id", "need_qty"),
    "documentation": ("record_id", "date", "employee", "plan_docs", "fact_docs"),
}

LABELS = {
    "preparation": "Заготовка",
    "welding_need": "Потребность сварки",
    "documentation": "Документация",
}


def detect_source(headers: list[str]) -> str:
    actual = {str(value).strip().lower() for value in headers if value is not None}
    matches = [name for name, fields in SPECS.items() if set(fields).issubset(actual)]
    if len(matches) != 1:
        raise ValueError("Не удалось определить участок по заголовкам. Ожидаются исходные английские имена столбцов.")
    return matches[0]


def parse_date(value: Any) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, (int, float)):
        return from_excel(value).date()
    text = str(value or "").strip()
    try:
        return datetime.fromisoformat(text).date()
    except ValueError:
        pass
    for fmt in ("%Y-%m-%d", "%d.%m.%Y", "%d/%m/%Y"):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            pass
    raise ValueError(f"Некорректная дата: {text or 'пусто'}")


def parse_int(value: Any) -> int | None:
    if value is None or str(value).strip() == "":
        return None
    try:
        number = float(str(value).replace(",", "."))
    except ValueError as exc:
        raise ValueError(f"Ожидалось целое число: {value}") from exc
    if not number.is_integer():
        raise ValueError(f"Ожидалось целое число: {value}")
    return int(number)


def normalize(source: str, raw: dict[str, Any]) -> dict[str, Any]:
    fields = SPECS[source]
    clean = {key: raw.get(key) for key in fields}
    for key in ("record_id", "batch_id", "employee"):
        if key in clean:
            clean[key] = str(clean[key] or "").strip()
            if not clean[key] and key != "employee":
                raise ValueError(f"Пустое обязательное поле {key}")
    clean["date"] = parse_date(clean["date"])
    for key in ("plan_qty", "fact_qty", "need_qty", "plan_docs", "fact_docs"):
        if key in clean:
            clean[key] = parse_int(clean[key])
    return clean


def quality(source: str, row: dict[str, Any]) -> tuple[str, str, bool]:
    """Return status, reason and whether acceptance without correction is forbidden."""
    if source == "welding_need":
        need = row["need_qty"]
        if need is None or need < 0:
            return "pending", "Потребность отсутствует или отрицательная", True
    else:
        if not row["employee"]:
            return "pending", "Не указан сотрудник", True
        plan_key, fact_key = ("plan_qty", "fact_qty") if source == "preparation" else ("plan_docs", "fact_docs")
        plan, fact = row[plan_key], row[fact_key]
        if plan is None or plan < 0:
            return "pending", "План отсутствует или отрицательный", True
        if fact is None or fact < 0:
            return "pending", "Факт отсутствует или отрицательный", True
        if source == "preparation" and plan > 0 and fact > 3 * plan:
            return "pending", "Факт больше плана более чем в 3 раза — требуется подтверждение", False
    return "approved", "", False
