"""One-time CSV/XLSX imports. Source files are not read by the running app afterwards."""

from __future__ import annotations

import csv
import hashlib
import io
import json
from pathlib import Path
from typing import Any

from openpyxl import load_workbook
from psycopg.types.json import Jsonb

from .db import connect
from .domain import LABELS, SPECS, detect_source, normalize, quality


def _validate_headers(headers: list[str], expected_source: str | None) -> str:
    names = [name for name in headers if name]
    duplicates = sorted({name for name in names if names.count(name) > 1})
    if duplicates:
        raise ValueError(f"Повторяются названия столбцов: {', '.join(duplicates)}")
    if expected_source is None:
        return detect_source(headers)
    if expected_source not in SPECS:
        raise ValueError("Неизвестная таблица для загрузки")
    required = set(SPECS[expected_source])
    missing = sorted(required - set(names))
    if missing:
        try:
            detected = detect_source(headers)
        except ValueError:
            detected = None
        detail = f"Не хватает столбцов для «{LABELS[expected_source]}»: {', '.join(missing)}."
        if detected and detected != expected_source:
            detail += f" Похоже, выбран файл таблицы «{LABELS[detected]}»."
        detail += f" Найдены столбцы: {', '.join(names) or 'нет заголовков'}."
        raise ValueError(detail)
    return expected_source


def read_upload(filename: str, content: bytes, expected_source: str | None = None) -> tuple[str, list[tuple[int, dict[str, Any]]]]:
    extension = Path(filename).suffix.lower()
    if extension == ".xlsx":
        workbook = load_workbook(io.BytesIO(content), read_only=True, data_only=True)
        sheet = workbook["Данные"] if "Данные" in workbook.sheetnames else workbook.active
        values = sheet.iter_rows(values_only=True)
        try:
            header_row = next(values)
        except StopIteration as exc:
            raise ValueError("Файл пустой: нет строки заголовков") from exc
        headers = [str(value or "").strip().lower() for value in header_row]
        source = _validate_headers(headers, expected_source)
        rows = [(number, dict(zip(headers, values_row))) for number, values_row in enumerate(values, start=2)]
        workbook.close()
        return source, rows
    if extension == ".csv":
        try:
            text = content.decode("utf-8-sig")
        except UnicodeDecodeError:
            text = content.decode("cp1251")
        try:
            dialect = csv.Sniffer().sniff(text[:4096], delimiters=",;")
        except csv.Error:
            dialect = csv.excel
        reader = csv.DictReader(io.StringIO(text), dialect=dialect)
        headers = [str(value or "").strip().lower() for value in (reader.fieldnames or [])]
        source = _validate_headers(headers, expected_source)
        rows = [
            (number, {str(key or "").strip().lower(): value for key, value in row.items()})
            for number, row in enumerate(reader, start=2)
        ]
        return source, rows
    raise ValueError("Поддерживаются только CSV и XLSX")


def import_file(filename: str, content: bytes, expected_source: str | None = None) -> dict[str, Any]:
    source, rows = read_upload(filename, content, expected_source)
    result = {"source": source, "inserted": 0, "pending": 0, "rejected": 0, "already_imported": False}
    file_hash = hashlib.sha256(content).hexdigest()
    with connect() as conn:
        imported = conn.execute(
            "INSERT INTO imports(source_type,filename,file_hash) VALUES (%s,%s,%s) "
            "ON CONFLICT (source_type,file_hash) DO NOTHING RETURNING id",
            (source, filename, file_hash),
        ).fetchone()
        if imported is None:
            result["already_imported"] = True
            return result
        import_id = imported["id"]
        for row_number, raw in rows:
            if not any(value not in (None, "") for value in raw.values()):
                continue
            try:
                data = normalize(source, raw)
            except ValueError as exc:
                _reject(conn, import_id, row_number, raw, str(exc))
                result["rejected"] += 1
                continue
            status, reason, _ = quality(source, data)
            fields = ["record_id", "work_date", *[key for key in SPECS[source] if key not in ("record_id", "date")],
                      "review_status", "issue_reason", "import_id", "source_row"]
            values = [data["record_id"], data["date"], *[data[key] for key in SPECS[source] if key not in ("record_id", "date")],
                      status, reason, import_id, row_number]
            placeholders = ",".join(["%s"] * len(fields))
            inserted = conn.execute(
                f"INSERT INTO {source} ({','.join(fields)}) VALUES ({placeholders}) "
                "ON CONFLICT (record_id) DO NOTHING RETURNING record_id",
                values,
            ).fetchone()
            if inserted is None:
                _reject(conn, import_id, row_number, raw, "Повторяющийся record_id: запись не добавлена")
                result["rejected"] += 1
            else:
                result["inserted"] += 1
                result["pending"] += status == "pending"
    return result


def _reject(conn, import_id: int, row_number: int, raw: dict, reason: str) -> None:
    safe_raw = json.loads(json.dumps(raw, ensure_ascii=False, default=str))
    conn.execute(
        "INSERT INTO rejected_rows(import_id,source_row,record_id,reason,raw_data) VALUES (%s,%s,%s,%s,%s)",
        (import_id, row_number, str(raw.get("record_id") or ""), reason, Jsonb(safe_raw)),
    )
