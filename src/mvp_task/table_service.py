"""Paginated table rows, issue links and inline director decisions."""

from __future__ import annotations

import hashlib
from math import ceil

from psycopg.errors import UniqueViolation

from .db import connect
from .domain import SPECS, normalize, parse_int, quality

DEFAULT_PAGE_SIZE = 20
PAGE_SIZES = (10, 20, 50, 100)
ISSUES_PER_PAGE = 30
SORT_FIELDS = ("date", "plan", "fact", "status")
PLAN_FIELDS = {"preparation": "plan_qty", "welding_need": "need_qty", "documentation": "plan_docs"}
FACT_FIELDS = {"preparation": "fact_qty", "documentation": "fact_docs"}
SORT_COLUMNS = {"date": "sort_date", "plan": "sort_plan", "fact": "sort_fact", "status": "status_rank"}


def checked_source(source: str) -> str:
    if source not in SPECS:
        raise ValueError("Неизвестная таблица")
    return source


def checked_table_options(source: str, sort: str, direction: str, employee: str, page_size: int) -> None:
    checked_source(source)
    if sort not in SORT_FIELDS or (source == "welding_need" and sort == "fact"):
        raise ValueError("Недоступное поле сортировки")
    if direction not in ("asc", "desc"):
        raise ValueError("Направление сортировки должно быть asc или desc")
    if page_size not in PAGE_SIZES:
        raise ValueError("Выберите 10, 20, 50 или 100 строк на странице")
    if source == "welding_need" and employee:
        raise ValueError("В таблице потребности сварки нет сотрудников")


def employee_options(source: str) -> list[str]:
    source = checked_source(source)
    if source == "welding_need":
        return []
    with connect() as conn:
        rows = conn.execute(
            "SELECT employee_name AS employee FROM users WHERE role=%s AND employee_name IS NOT NULL "
            f"UNION SELECT employee FROM {source} WHERE employee<>'' "
            "UNION SELECT r.raw_data->>'employee' FROM rejected_rows r "
            "JOIN imports i ON i.id=r.import_id WHERE i.source_type=%s AND r.resolution IS NULL "
            "ORDER BY employee",
            (source, source),
        ).fetchall()
    return [row["employee"] for row in rows if row["employee"] and row["employee"].strip()]


def _items_sql(source: str, employee: str = "") -> str:
    source = checked_source(source)
    plan = PLAN_FIELDS[source]
    fact = f"{FACT_FIELDS[source]}::numeric" if source in FACT_FIELDS else "NULL::numeric"
    staff = "employee" if source != "welding_need" else "NULL::text"
    filter_sql = "WHERE employee_name=%s" if employee else ""
    return (
        "WITH items AS ("
        "SELECT 'record'::text AS kind,record_id::text AS item_id,record_id::text AS record_id,"
        f"import_id,source_row,work_date AS sort_date,{plan}::numeric AS sort_plan,"
        f"{fact} AS sort_fact,CASE review_status WHEN 'approved' THEN 0 ELSE 1 END AS status_rank,"
        f"{staff} AS employee_name,issue_reason AS reason FROM {source} "
        "UNION ALL "
        "SELECT 'rejected'::text,r.id::text,r.record_id::text,r.import_id,r.source_row,"
        "NULL::date,NULL::numeric,NULL::numeric,2,r.raw_data->>'employee',r.reason "
        "FROM rejected_rows r JOIN imports i ON i.id=r.import_id "
        "WHERE i.source_type=%s AND r.resolution IS NULL), "
        f"filtered AS (SELECT * FROM items {filter_sql}) "
    )


def _base_params(source: str, employee: str) -> tuple[str, ...]:
    return (source, employee) if employee else (source,)


def _order_clause(sort: str, direction: str) -> str:
    return (
        f"{SORT_COLUMNS[sort]} {direction.upper()} NULLS LAST,"
        "import_id NULLS LAST,source_row NULLS LAST,kind,item_id"
    )


def table_count(source: str, employee: str = "") -> int:
    with connect() as conn:
        return conn.execute(
            _items_sql(source, employee) + "SELECT COUNT(*) AS n FROM filtered",
            _base_params(source, employee),
        ).fetchone()["n"]


def page_for_item(source: str, kind: str, item_id: str, *, sort: str = "date",
                  direction: str = "asc", employee: str = "", page_size: int = DEFAULT_PAGE_SIZE) -> int | None:
    checked_table_options(source, sort, direction, employee, page_size)
    with connect() as conn:
        row = conn.execute(
            _items_sql(source, employee) +
            "SELECT rn FROM (SELECT kind,item_id,ROW_NUMBER() OVER "
            f"(ORDER BY {_order_clause(sort, direction)}) AS rn FROM filtered) ranked "
            "WHERE kind=%s AND item_id=%s",
            (*_base_params(source, employee), kind, item_id),
        ).fetchone()
    return ceil(row["rn"] / page_size) if row else None


def table_page(source: str, page: int, *, sort: str = "date", direction: str = "asc",
               employee: str = "", page_size: int = DEFAULT_PAGE_SIZE) -> tuple[list[dict], int, int, int]:
    checked_table_options(source, sort, direction, employee, page_size)
    total = table_count(source, employee)
    pages = max(1, ceil(total / page_size))
    page = max(1, min(page, pages))
    with connect() as conn:
        keys = conn.execute(
            _items_sql(source, employee) +
            "SELECT kind,item_id FROM filtered "
            f"ORDER BY {_order_clause(sort, direction)} LIMIT %s OFFSET %s",
            (*_base_params(source, employee), page_size, (page - 1) * page_size),
        ).fetchall()
        rows = [_load_item(conn, source, key["kind"], key["item_id"]) for key in keys]
    return [row for row in rows if row], page, pages, total


def _load_item(conn, source: str, kind: str, item_id: str) -> dict | None:
    source = checked_source(source)
    if kind == "record":
        row = conn.execute(f"SELECT * FROM {source} WHERE record_id=%s", (item_id,)).fetchone()
        if not row:
            return None
        values = {field: row["work_date"].isoformat() if field == "date" else row[field]
                  for field in SPECS[source]}
        status = row["review_status"]
        reason = row["issue_reason"]
        source_row = row["source_row"]
    elif kind == "rejected":
        row = conn.execute(
            "SELECT r.* FROM rejected_rows r JOIN imports i ON i.id=r.import_id "
            "WHERE r.id=%s AND i.source_type=%s AND r.resolution IS NULL",
            (int(item_id), source),
        ).fetchone()
        if not row:
            return None
        values = {field: row["raw_data"].get(field) for field in SPECS[source]}
        status = "rejected"
        reason = row["reason"]
        source_row = row["source_row"]
    else:
        raise ValueError("Неизвестный тип строки")
    row_key = f"{kind}:{item_id}"
    return {
        "kind": kind, "item_id": item_id, "row_key": row_key,
        "dom_id": "row-" + hashlib.sha256(row_key.encode()).hexdigest()[:16],
        "values": values, "status": status, "reason": reason, "source_row": source_row,
    }


def get_item(source: str, kind: str, item_id: str) -> dict | None:
    with connect() as conn:
        return _load_item(conn, source, kind, item_id)


def issue_count(source: str, employee: str = "") -> int:
    source = checked_source(source)
    with connect() as conn:
        return conn.execute(
            _items_sql(source, employee) +
            "SELECT COUNT(*) AS n FROM filtered WHERE status_rank<>0",
            _base_params(source, employee),
        ).fetchone()["n"]


def issue_page(source: str, page: int, *, sort: str = "date", direction: str = "asc",
               employee: str = "", page_size: int = DEFAULT_PAGE_SIZE) -> tuple[list[dict], int, int]:
    checked_table_options(source, sort, direction, employee, page_size)
    total = issue_count(source, employee)
    pages = max(1, ceil(total / ISSUES_PER_PAGE))
    page = max(1, min(page, pages))
    with connect() as conn:
        rows = conn.execute(
            _items_sql(source, employee) +
            "SELECT kind,item_id,record_id,reason,source_row FROM filtered WHERE status_rank<>0 "
            f"ORDER BY {_order_clause(sort, direction)} LIMIT %s OFFSET %s",
            (*_base_params(source, employee), ISSUES_PER_PAGE, (page - 1) * ISSUES_PER_PAGE),
        ).fetchall()
    for row in rows:
        row["row_key"] = f"{row['kind']}:{row['item_id']}"
        row["dom_id"] = "row-" + hashlib.sha256(row["row_key"].encode()).hexdigest()[:16]
    return rows, page, pages


def accept_edited_row(source: str, kind: str, item_id: str, raw: dict, username: str) -> str:
    source = checked_source(source)
    data = normalize(source, raw)
    _, reason, hard = quality(source, data)
    if hard:
        raise ValueError(reason + ". Исправьте значение перед принятием.")
    fields = ["record_id", "work_date", *[field for field in SPECS[source] if field not in ("record_id", "date")]]
    values = [data["record_id"], data["date"], *[data[field] for field in SPECS[source] if field not in ("record_id", "date")]]
    try:
        with connect() as conn:
            if kind == "record":
                assignments = ",".join(f"{field}=%s" for field in fields)
                changed = conn.execute(
                    f"UPDATE {source} SET {assignments},review_status='approved',issue_reason='' "
                    "WHERE record_id=%s RETURNING record_id",
                    [*values, item_id],
                ).fetchone()
                if not changed:
                    raise ValueError("Строка уже удалена или её ID изменился. Обновите страницу.")
            elif kind == "rejected":
                staged = conn.execute(
                    "SELECT r.* FROM rejected_rows r JOIN imports i ON i.id=r.import_id "
                    "WHERE r.id=%s AND i.source_type=%s AND r.resolution IS NULL FOR UPDATE OF r",
                    (int(item_id), source),
                ).fetchone()
                if not staged:
                    raise ValueError("Строка уже обработана. Обновите страницу.")
                insert_fields = [*fields, "review_status", "issue_reason", "import_id", "source_row"]
                insert_values = [*values, "approved", "", staged["import_id"], staged["source_row"]]
                conn.execute(
                    f"INSERT INTO {source} ({','.join(insert_fields)}) VALUES ({','.join(['%s'] * len(insert_fields))})",
                    insert_values,
                )
                conn.execute("UPDATE rejected_rows SET resolution='accepted',resolved_at=now() WHERE id=%s", (int(item_id),))
            else:
                raise ValueError("Неизвестный тип строки")
            conn.execute(
                "INSERT INTO review_log(source_type,record_id,username,action) VALUES (%s,%s,%s,'accepted')",
                (source, data["record_id"], username),
            )
    except UniqueViolation as exc:
        raise ValueError(f"ID {data['record_id']} уже занят. Укажите другой record_id или удалите дубликат.") from exc
    return data["record_id"]


def delete_problem_row(source: str, kind: str, item_id: str, username: str) -> None:
    source = checked_source(source)
    with connect() as conn:
        if kind == "record":
            removed = conn.execute(
                f"DELETE FROM {source} WHERE record_id=%s AND review_status='pending' RETURNING record_id",
                (item_id,),
            ).fetchone()
            if not removed:
                raise ValueError("Удалять можно только строку на проверке; обновите страницу.")
            record_id = item_id
        elif kind == "rejected":
            removed = conn.execute(
                "UPDATE rejected_rows r SET resolution='deleted',resolved_at=now() "
                "FROM imports i WHERE r.import_id=i.id AND r.id=%s AND i.source_type=%s "
                "AND r.resolution IS NULL RETURNING r.record_id",
                (int(item_id), source),
            ).fetchone()
            if not removed:
                raise ValueError("Строка уже обработана; обновите страницу.")
            record_id = removed["record_id"] or f"staged:{item_id}"
        else:
            raise ValueError("Неизвестный тип строки")
        conn.execute(
            "INSERT INTO review_log(source_type,record_id,username,action) VALUES (%s,%s,%s,'deleted')",
            (source, record_id, username),
        )


def update_staff_fact(source: str, record_id: str, raw_fact: str, username: str, employee_name: str) -> str:
    """Only the server-selected fact field and last editor may change."""
    if source not in ("preparation", "documentation"):
        raise ValueError("Для этой таблицы нет редактирования факта сотрудником")
    fact_field = "fact_qty" if source == "preparation" else "fact_docs"
    fact = parse_int(raw_fact)
    if fact is None or fact < 0:
        raise ValueError("Факт должен быть целым числом не меньше нуля")
    if not employee_name:
        raise ValueError("У пользователя не указано имя сотрудника")
    with connect() as conn:
        # Serialize fact events so the dashboard cursor follows commit order.
        conn.execute("SELECT pg_advisory_xact_lock(hashtext('mvp-staff-fact-events'))")
        row = conn.execute(f"SELECT * FROM {source} WHERE record_id=%s FOR UPDATE", (record_id,)).fetchone()
        if not row:
            raise ValueError("Строка не найдена. Обновите страницу.")
        proposed = {**row, fact_field: fact, "employee": employee_name}
        status, reason, _ = quality(source, proposed)
        conn.execute(
            f"UPDATE {source} SET {fact_field}=%s,employee=%s,review_status=%s,issue_reason=%s "
            "WHERE record_id=%s",
            (fact, employee_name, status, reason, record_id),
        )
        conn.execute(
            "INSERT INTO review_log(source_type,record_id,username,action,work_date,result_status,issue_reason) "
            "VALUES (%s,%s,%s,'fact_changed',%s,%s,%s)",
            (source, record_id, username, row["work_date"], status, reason),
        )
    return status
