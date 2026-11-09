"""Parameterized reads and director-only review actions."""

from __future__ import annotations

from datetime import date
from typing import Any

from .db import connect
from .domain import LABELS, SPECS, normalize, parse_int, quality

DAILY_PLAN_FIELDS = {
    "preparation": "plan_qty",
    "welding_need": "need_qty",
    "documentation": "plan_docs",
}


def _source(source: str) -> str:
    if source not in SPECS:
        raise ValueError("Неизвестный участок")
    return source


def list_rows(source: str, start: date, end: date, employee: str | None = None) -> list[dict]:
    source = _source(source)
    sql = f"SELECT * FROM {source} WHERE work_date BETWEEN %s AND %s"
    params: list[Any] = [start, end]
    if employee is not None:
        if source == "welding_need":
            return []
        sql += " AND employee=%s"
        params.append(employee)
    sql += " ORDER BY work_date,record_id"
    with connect() as conn:
        return conn.execute(sql, params).fetchall()


def pending_rows() -> list[dict]:
    with connect() as conn:
        return conn.execute(
            "SELECT 'preparation' AS source_type,record_id,work_date,issue_reason FROM preparation WHERE review_status='pending' "
            "UNION ALL SELECT 'welding_need',record_id,work_date,issue_reason FROM welding_need WHERE review_status='pending' "
            "UNION ALL SELECT 'documentation',record_id,work_date,issue_reason FROM documentation WHERE review_status='pending' "
            "ORDER BY work_date,source_type,record_id"
        ).fetchall()


def rejected_rows() -> list[dict]:
    with connect() as conn:
        return conn.execute(
            "SELECT i.source_type,i.filename,r.source_row,r.record_id,r.reason,r.raw_data "
            "FROM rejected_rows r JOIN imports i ON i.id=r.import_id ORDER BY r.id"
        ).fetchall()


def get_record(source: str, record_id: str) -> dict:
    source = _source(source)
    with connect() as conn:
        row = conn.execute(f"SELECT * FROM {source} WHERE record_id=%s", (record_id,)).fetchone()
    if row is None:
        raise ValueError("Запись не найдена")
    return row


def update_record(source: str, record_id: str, raw: dict, username: str) -> tuple[str, str]:
    source = _source(source)
    data = normalize(source, {**raw, "record_id": record_id})
    status, reason, _ = quality(source, data)
    fields = ["work_date", *[key for key in SPECS[source] if key not in ("record_id", "date")], "review_status", "issue_reason"]
    values = [data["date"], *[data[key] for key in SPECS[source] if key not in ("record_id", "date")], status, reason]
    assignments = ",".join(f"{field}=%s" for field in fields)
    with connect() as conn:
        conn.execute(f"UPDATE {source} SET {assignments} WHERE record_id=%s", [*values, record_id])
        conn.execute(
            "INSERT INTO review_log(source_type,record_id,username,action) VALUES (%s,%s,%s,%s)",
            (source, record_id, username, "changed"),
        )
    return status, reason


def accept_record(source: str, record_id: str, username: str) -> None:
    source = _source(source)
    row = get_record(source, record_id)
    raw = {"record_id": row["record_id"], "date": row["work_date"]}
    raw.update({key: row[key] for key in SPECS[source] if key not in ("record_id", "date")})
    status, reason, hard = quality(source, raw)
    if status != "pending" or hard:
        raise ValueError("Эту запись нужно исправить; принять без изменения нельзя")
    with connect() as conn:
        conn.execute(f"UPDATE {source} SET review_status='approved',issue_reason='' WHERE record_id=%s", (record_id,))
        conn.execute(
            "INSERT INTO review_log(source_type,record_id,username,action) VALUES (%s,%s,%s,'accepted')",
            (source, record_id, username),
        )


def batch_rows(start: date, end: date) -> list[dict]:
    with connect() as conn:
        return conn.execute(
            """
            WITH p AS (
                SELECT batch_id,
                       BOOL_OR(review_status='pending') AS has_pending,
                       CASE WHEN BOOL_AND(review_status='approved') THEN SUM(plan_qty) END AS plan_qty,
                       CASE WHEN BOOL_AND(review_status='approved' AND fact_qty IS NOT NULL)
                            THEN SUM(fact_qty) END AS fact_qty
                FROM preparation WHERE work_date BETWEEN %s AND %s GROUP BY batch_id
            ), w AS (
                SELECT batch_id,
                       BOOL_OR(review_status='pending') AS has_pending,
                       CASE WHEN BOOL_AND(review_status='approved' AND need_qty IS NOT NULL)
                            THEN SUM(need_qty) END AS need_qty
                FROM welding_need WHERE work_date BETWEEN %s AND %s GROUP BY batch_id
            )
            SELECT COALESCE(p.batch_id,w.batch_id) AS batch_id,p.plan_qty,p.fact_qty,w.need_qty,
                   CASE WHEN p.fact_qty IS NOT NULL AND w.need_qty IS NOT NULL
                        THEN GREATEST(w.need_qty-p.fact_qty,0) END AS possible_gap,
                   CASE WHEN p.batch_id IS NULL THEN 'Нет заготовки'
                        WHEN w.batch_id IS NULL THEN 'Нет потребности'
                        WHEN p.has_pending OR w.has_pending OR w.need_qty IS NULL THEN 'Требует проверки'
                        WHEN p.fact_qty IS NULL THEN 'Ожидает факт'
                        ELSE 'Подтверждено' END AS link_status
            FROM p FULL OUTER JOIN w ON p.batch_id=w.batch_id
            ORDER BY batch_id
            """,
            (start, end, start, end),
        ).fetchall()


def dashboard(start: date, end: date) -> dict:
    batches = batch_rows(start, end)
    with connect() as conn:
        prep = conn.execute(
            "SELECT COUNT(*) AS rows,COUNT(*) FILTER (WHERE review_status='approved') AS confirmed, "
            "COALESCE(SUM(plan_qty) FILTER (WHERE review_status='approved'),0) AS plan_qty, "
            "COALESCE(SUM(fact_qty) FILTER (WHERE review_status='approved'),0) AS fact_qty "
            "FROM preparation WHERE work_date BETWEEN %s AND %s", (start, end)
        ).fetchone()
        welding = conn.execute(
            "SELECT COUNT(*) AS rows,COUNT(*) FILTER (WHERE review_status='approved') AS confirmed, "
            "COALESCE(SUM(need_qty) FILTER (WHERE review_status='approved'),0) AS need_qty "
            "FROM welding_need WHERE work_date BETWEEN %s AND %s", (start, end)
        ).fetchone()
        docs = conn.execute(
            "SELECT COUNT(*) AS rows,COUNT(*) FILTER (WHERE review_status='approved') AS confirmed, "
            "COALESCE(SUM(plan_docs) FILTER (WHERE review_status='approved'),0) AS plan_docs, "
            "COALESCE(SUM(fact_docs) FILTER (WHERE review_status='approved'),0) AS fact_docs "
            "FROM documentation WHERE work_date BETWEEN %s AND %s", (start, end)
        ).fetchone()
    return {
        "preparation": prep,
        "welding_need": welding,
        "documentation": docs,
        "batches": batches,
        "batch_totals": {
            "plan_qty": sum(row["plan_qty"] or 0 for row in batches),
            "fact_qty": sum(row["fact_qty"] or 0 for row in batches),
            "need_qty": sum(row["need_qty"] or 0 for row in batches),
            "possible_gap": sum(row["possible_gap"] or 0 for row in batches),
        },
        "prep_percent": (prep["fact_qty"] / prep["plan_qty"] * 100) if prep["plan_qty"] else None,
        "docs_percent": (docs["fact_docs"] / docs["plan_docs"] * 100) if docs["plan_docs"] else None,
        "known_gap": sum(row["possible_gap"] or 0 for row in batches),
        "unknown_need": sum(row["need_qty"] or 0 for row in batches if row["fact_qty"] is None),
    }


def latest_review_id() -> int:
    """Cursor captured before rendering, so concurrent changes are seen on the next poll."""
    with connect() as conn:
        row = conn.execute(
            "SELECT COALESCE(MAX(id),0) AS id FROM review_log WHERE action='fact_changed'"
        ).fetchone()
    return row["id"]


def staff_fact_events(after: int, through: int, start: date, end: date) -> list[dict]:
    """Only fact edits on dates visible in the director's selected period."""
    with connect() as conn:
        return conn.execute(
            "SELECT source_type,record_id,result_status,issue_reason FROM review_log "
            "WHERE id>%s AND id<=%s AND action='fact_changed' "
            "AND work_date BETWEEN %s AND %s ORDER BY id",
            (after, through, start, end),
        ).fetchall()


def daily_plan_rows(day: date) -> dict[str, list[dict]]:
    """Show actual source rows rather than a second set of plan targets."""
    with connect() as conn:
        return {
            source: conn.execute(
                f"SELECT * FROM {source} WHERE work_date=%s ORDER BY record_id", (day,)
            ).fetchall()
            for source in DAILY_PLAN_FIELDS
        }


def save_daily_plans(day: date, form: dict, username: str) -> int:
    """Create or update plans in all three source tables in one transaction."""
    changes = 0
    had_rows = False
    with connect() as conn:
        conn.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", (f"daily-plan:{day.isoformat()}",))
        for source, plan_field in DAILY_PLAN_FIELDS.items():
            rows = conn.execute(
                f"SELECT * FROM {source} WHERE work_date=%s ORDER BY record_id FOR UPDATE", (day,)
            ).fetchall()
            if rows:
                had_rows = True
                for index, row in enumerate(rows):
                    if form.get(f"id_{source}_{index}") != row["record_id"]:
                        raise ValueError("Строки за этот день изменились. Обновите страницу и повторите сохранение.")
                    value = parse_int(form.get(f"plan_{source}_{index}"))
                    if value is None or value < 0:
                        raise ValueError(f"{LABELS[source]} · {row['record_id']}: план должен быть целым числом от нуля")
                    batch = row.get("batch_id")
                    if source != "documentation":
                        batch = str(form.get(f"batch_{source}_{index}") or "").strip()
                        if not batch:
                            raise ValueError(f"{LABELS[source]} · {row['record_id']}: укажите партию")
                    if row[plan_field] == value and (source == "documentation" or row["batch_id"] == batch):
                        continue
                    proposed = {**row, plan_field: value}
                    if source != "documentation":
                        proposed["batch_id"] = batch
                    if source != "welding_need" and row["fact_qty" if source == "preparation" else "fact_docs"] is None and row["review_status"] == "approved":
                        status, reason = "approved", ""
                    else:
                        status, reason, _ = quality(source, proposed)
                    if source == "documentation":
                        conn.execute(
                            "UPDATE documentation SET plan_docs=%s,review_status=%s,issue_reason=%s WHERE record_id=%s",
                            (value, status, reason, row["record_id"]),
                        )
                    else:
                        conn.execute(
                            f"UPDATE {source} SET {plan_field}=%s,batch_id=%s,review_status=%s,issue_reason=%s WHERE record_id=%s",
                            (value, batch, status, reason, row["record_id"]),
                        )
                    conn.execute(
                        "INSERT INTO review_log(source_type,record_id,username,action) VALUES (%s,%s,%s,'plan_changed')",
                        (source, row["record_id"], username),
                    )
                    changes += 1
            else:
                value = parse_int(form.get(f"new_{source}"))
                if value is None:
                    continue
                if value < 0:
                    raise ValueError(f"{LABELS[source]}: план должен быть целым числом от нуля")
                batch = str(form.get(f"batch_{source}") or "").strip()
                if source != "documentation" and not batch:
                    raise ValueError(f"{LABELS[source]}: укажите партию для новой строки")
                prefix = {"preparation": "P", "welding_need": "W", "documentation": "D"}[source]
                record_id = f"PLAN-{prefix}-{day:%Y%m%d}"
                if conn.execute(f"SELECT 1 FROM {source} WHERE record_id=%s", (record_id,)).fetchone():
                    raise ValueError(f"{LABELS[source]}: ID {record_id} уже занят. Откройте таблицу и проверьте запись.")
                if source == "preparation":
                    conn.execute(
                        "INSERT INTO preparation(record_id,work_date,batch_id,employee,plan_qty,fact_qty,review_status) "
                        "VALUES (%s,%s,%s,'',%s,NULL,'approved')",
                        (record_id, day, batch, value),
                    )
                elif source == "welding_need":
                    conn.execute(
                        "INSERT INTO welding_need(record_id,work_date,batch_id,need_qty,review_status) "
                        "VALUES (%s,%s,%s,%s,'approved')",
                        (record_id, day, batch, value),
                    )
                else:
                    conn.execute(
                        "INSERT INTO documentation(record_id,work_date,employee,plan_docs,fact_docs,review_status) "
                        "VALUES (%s,%s,'',%s,NULL,'approved')",
                        (record_id, day, value),
                    )
                conn.execute(
                    "INSERT INTO review_log(source_type,record_id,username,action) VALUES (%s,%s,%s,'plan_created')",
                    (source, record_id, username),
                )
                changes += 1
        if changes == 0 and not had_rows:
            raise ValueError("Укажите план хотя бы для одного участка")
    return changes


def daily_series(source: str, start: date, end: date) -> list[dict]:
    source = _source(source)
    if source == "preparation":
        plan, fact = "plan_qty", "fact_qty"
    elif source == "documentation":
        plan, fact = "plan_docs", "fact_docs"
    else:
        plan, fact = "need_qty", "need_qty"
    with connect() as conn:
        return conn.execute(
            f"SELECT work_date,COALESCE(SUM({plan}) FILTER (WHERE review_status='approved'),0) AS plan, "
            f"COALESCE(SUM({fact}) FILTER (WHERE review_status='approved'),0) AS fact "
            f"FROM {source} WHERE work_date BETWEEN %s AND %s GROUP BY work_date "
            "HAVING COUNT(*) FILTER (WHERE review_status='approved') > 0 ORDER BY work_date",
            (start, end),
        ).fetchall()


def date_bounds() -> tuple[date, date]:
    with connect() as conn:
        row = conn.execute(
            "SELECT MIN(work_date) AS min_date,MAX(work_date) AS max_date FROM ("
            "SELECT work_date FROM preparation UNION ALL SELECT work_date FROM welding_need "
            "UNION ALL SELECT work_date FROM documentation) x"
        ).fetchone()
    today = date.today()
    return row["min_date"] or today, row["max_date"] or today
