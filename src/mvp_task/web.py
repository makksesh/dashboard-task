"""Server-rendered alpha UI with direct row links and bounded pagination."""

from __future__ import annotations

import os
import secrets
from hashlib import sha256
from datetime import date
from pathlib import Path
from urllib.parse import urlencode
from zipfile import BadZipFile

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.datastructures import UploadFile
from starlette.middleware.sessions import SessionMiddleware

from .db import authenticate, clear_business_data, connect, data_counts
from .domain import LABELS, SPECS
from .ingest import import_file
from .report import make_report
from .repository import (
    DAILY_PLAN_FIELDS, daily_plan_rows, daily_series, dashboard, date_bounds,
    latest_review_id, save_daily_plans, staff_fact_events,
)
from .table_service import (
    DEFAULT_PAGE_SIZE,
    PAGE_SIZES,
    accept_edited_row,
    checked_source,
    checked_table_options,
    delete_problem_row,
    employee_options,
    issue_count,
    issue_page,
    page_for_item,
    table_page,
    update_staff_fact,
)

ROOT = Path(__file__).resolve().parent
ASSET_VERSION = sha256(b"".join(
    (ROOT / "static" / filename).read_bytes()
    for filename in ("style.css", "compact.css", "app.js")
)).hexdigest()[:12]
SESSION_SECRET = os.environ.get("MVP_SESSION_SECRET")
if not SESSION_SECRET:
    raise RuntimeError("MVP_SESSION_SECRET не задан. Запустите проект через ./start.sh")

app = FastAPI(title="Производство — альфа", docs_url=None, redoc_url=None)
app.add_middleware(SessionMiddleware, secret_key=SESSION_SECRET, same_site="lax", session_cookie="mvp_session")
app.mount("/static", StaticFiles(directory=ROOT / "static"), name="static")
templates = Jinja2Templates(directory=ROOT / "templates")

FIELD_LABELS = {
    "record_id": "ID записи", "date": "Дата", "batch_id": "Партия", "employee": "Сотрудник",
    "plan_qty": "План", "fact_qty": "Факт", "need_qty": "Потребность",
    "plan_docs": "План документов", "fact_docs": "Факт документов",
}


def _user(request: Request) -> dict | None:
    username = request.session.get("username")
    if not username:
        return None
    with connect() as conn:
        return conn.execute(
            "SELECT username,role,employee_name FROM users WHERE username=%s", (username,)
        ).fetchone()


def _csrf(request: Request) -> str:
    if "csrf" not in request.session:
        request.session["csrf"] = secrets.token_urlsafe(24)
    return request.session["csrf"]


def _check_csrf(request: Request, value: str | None) -> None:
    expected = request.session.get("csrf", "")
    if not value or not secrets.compare_digest(value, expected):
        raise HTTPException(status_code=403, detail="Неверный код формы. Обновите страницу.")


def _access(request: Request, source: str, director_only: bool = False) -> dict | RedirectResponse:
    try:
        checked_source(source)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    user = _user(request)
    if not user:
        return RedirectResponse("/login", status_code=303)
    if director_only and user["role"] != "director":
        raise HTTPException(status_code=403, detail="Доступно только директору")
    if user["role"] not in ("director", source):
        raise HTTPException(status_code=403, detail="Нет доступа к этой таблице")
    return user


def _base(request: Request, user: dict | None, active: str) -> dict:
    return {
        "request": request,
        "user": user,
        "active": active,
        "labels": LABELS,
        "csrf": _csrf(request),
        "flash": request.session.pop("flash", None),
        "asset_version": ASSET_VERSION,
    }


def _flash(request: Request, kind: str, text: str) -> None:
    request.session["flash"] = {"kind": kind, "text": text}


def _table_url(source: str, *, page: int | None = None, issue_page_number: int = 1,
               focus: str | None = None, sort: str = "date", direction: str = "asc",
               employee: str = "", page_size: int = DEFAULT_PAGE_SIZE) -> str:
    params = {"sort": sort, "direction": direction, "page_size": page_size,
              "issue_page": issue_page_number}
    if employee:
        params["employee"] = employee
    if page is not None:
        params["page"] = page
    if focus:
        params["focus"] = focus
    return f"/tables/{source}?{urlencode(params)}"


def _posted_table_options(source: str, form) -> tuple[str, str, str, int]:
    sort = str(form.get("sort", "date"))
    direction = str(form.get("direction", "asc"))
    employee = str(form.get("employee", ""))
    try:
        page_size = int(str(form.get("page_size", DEFAULT_PAGE_SIZE)))
        checked_table_options(source, sort, direction, employee, page_size)
    except (ValueError, TypeError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return sort, direction, employee, page_size


def _parse_date(value: str | None, default: date) -> date:
    if not value:
        return default
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="Укажите дату в формате ГГГГ-ММ-ДД") from exc


def _chart(source: str, start: date, end: date) -> list[dict]:
    rows = daily_series(source, start, end)
    maximum = max((max(row["plan"], row["fact"]) for row in rows), default=0) or 1
    return [
        {
            "date": row["work_date"].isoformat(),
            "plan": row["plan"], "fact": row["fact"],
            "plan_width": round(row["plan"] / maximum * 100, 1),
            "fact_width": round(row["fact"] / maximum * 100, 1),
        }
        for row in rows
    ]


def _dashboard_data(first: date, last: date) -> dict:
    data = dashboard(first, last)
    display_batches = sorted(
        data["batches"],
        key=lambda row: (row["link_status"] == "Подтверждено", -(row["possible_gap"] or 0), row["batch_id"]),
    )[:12]
    return {
        "start": first,
        "end": last,
        "data": data,
        "issues": {source: issue_count(source) for source in SPECS},
        "display_batches": display_batches,
        "prep_chart": _chart("preparation", first, last),
        "docs_chart": _chart("documentation", first, last),
        "labels": LABELS,
    }


@app.get("/health")
def health() -> dict:
    with connect() as conn:
        conn.execute("SELECT 1")
    return {"ok": True}


@app.get("/")
def root(request: Request):
    user = _user(request)
    if not user:
        return RedirectResponse("/login", status_code=303)
    return RedirectResponse("/dashboard" if user["role"] == "director" else f"/tables/{user['role']}", status_code=303)


@app.get("/login", response_class=HTMLResponse)
def login_page(request: Request):
    if _user(request):
        return RedirectResponse("/", status_code=303)
    return templates.TemplateResponse(request=request, name="login.html", context={**_base(request, None, "login"), "error": None})


@app.post("/login", response_class=HTMLResponse)
async def login(request: Request):
    form = await request.form()
    username = str(form.get("username", "")).strip().lower()
    password = str(form.get("password", ""))
    user = authenticate(username, password)
    if not user:
        return templates.TemplateResponse(
            request=request, name="login.html",
            context={**_base(request, None, "login"), "error": "Неверный логин или пароль"},
            status_code=401,
        )
    request.session.clear()
    request.session["username"] = user["username"]
    _csrf(request)
    return RedirectResponse("/", status_code=303)


@app.post("/logout")
async def logout(request: Request):
    form = await request.form()
    _check_csrf(request, str(form.get("csrf", "")))
    request.session.clear()
    return RedirectResponse("/login", status_code=303)


@app.get("/admin/clear-data", response_class=HTMLResponse)
def clear_data_page(request: Request):
    user = _user(request)
    if not user:
        return RedirectResponse("/login", status_code=303)
    if user["role"] != "director":
        raise HTTPException(status_code=403, detail="Доступно только директору")
    return templates.TemplateResponse(
        request=request, name="clear_data.html",
        context={**_base(request, user, "clear-data"), "counts": data_counts()},
    )


@app.post("/admin/clear-data")
async def clear_data(request: Request):
    user = _user(request)
    if not user:
        return RedirectResponse("/login", status_code=303)
    if user["role"] != "director":
        raise HTTPException(status_code=403, detail="Доступно только директору")
    form = await request.form()
    _check_csrf(request, str(form.get("csrf", "")))
    if str(form.get("confirmation", "")) != "ОЧИСТИТЬ":
        _flash(request, "error", "Для очистки введите ОЧИСТИТЬ точно, без дополнительных символов")
        return RedirectResponse("/admin/clear-data", status_code=303)
    counts = clear_business_data()
    total = sum(counts.values())
    _flash(request, "success", f"Данные очищены: удалено {total} записей. Пять демо-пользователей сохранены.")
    return RedirectResponse("/dashboard", status_code=303)


@app.get("/dashboard", response_class=HTMLResponse)
def director_dashboard(request: Request, start: str | None = None, end: str | None = None):
    user = _user(request)
    if not user:
        return RedirectResponse("/login", status_code=303)
    if user["role"] != "director":
        raise HTTPException(status_code=403, detail="Доступно только директору")
    minimum, maximum = date_bounds()
    first, last = _parse_date(start, minimum), _parse_date(end, maximum)
    if first > last:
        raise HTTPException(status_code=400, detail="Начало периода позже конца")
    live_cursor = latest_review_id()
    return templates.TemplateResponse(
        request=request, name="dashboard.html",
        context={
            **_base(request, user, "dashboard"), "start": first, "end": last,
            "live_cursor": live_cursor, **_dashboard_data(first, last),
        },
    )


@app.get("/dashboard/live")
def director_dashboard_live(request: Request, start: str, end: str, after: int = 0):
    user = _user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Войдите снова")
    if user["role"] != "director":
        raise HTTPException(status_code=403, detail="Доступно только директору")
    first, last = _parse_date(start, date.today()), _parse_date(end, date.today())
    if first > last or after < 0:
        raise HTTPException(status_code=400, detail="Некорректный период или номер изменения")
    cursor = latest_review_id()
    events = staff_fact_events(after, cursor, first, last)
    result = {"cursor": cursor, "changed": bool(events)}
    if events:
        result["html"] = templates.env.get_template("dashboard_live.html").render(
            _dashboard_data(first, last)
        )
        errors = [event for event in events if event["result_status"] == "pending"]
        result["errors"] = len(errors)
        result["error_reason"] = errors[0]["issue_reason"] if errors else ""
    return JSONResponse(result, headers={"Cache-Control": "no-store"})


@app.get("/plans", response_class=HTMLResponse)
def plan_page(request: Request, day: str | None = None):
    user = _user(request)
    if not user:
        return RedirectResponse("/login", status_code=303)
    if user["role"] != "director":
        raise HTTPException(status_code=403, detail="Доступно только директору")
    _, latest = date_bounds()
    selected_day = _parse_date(day, latest)
    return templates.TemplateResponse(
        request=request, name="plans.html",
        context={
            **_base(request, user, "plans"),
            "day": selected_day, "daily_rows": daily_plan_rows(selected_day), "plan_fields": DAILY_PLAN_FIELDS,
        },
    )


@app.post("/daily-plans")
async def set_daily_plans(request: Request):
    user = _user(request)
    if not user:
        return RedirectResponse("/login", status_code=303)
    if user["role"] != "director":
        raise HTTPException(status_code=403, detail="Доступно только директору")
    form = await request.form()
    _check_csrf(request, str(form.get("csrf", "")))
    day = _parse_date(str(form.get("day", "")), date.today())
    try:
        changed = save_daily_plans(day, form, user["username"])
        if changed:
            _flash(request, "success", f"Планы на {day:%d.%m.%Y} сохранены: {changed} строк")
        else:
            _flash(request, "info", "Планы уже соответствуют указанным значениям")
    except ValueError as exc:
        _flash(request, "error", str(exc))
    return RedirectResponse(f"/plans?{urlencode({'day': day.isoformat()})}#daily-plan", status_code=303)


@app.get("/tables/{source}", response_class=HTMLResponse)
def table_view(request: Request, source: str, page: int = 1, issue_page: int = 1,
               focus: str | None = None, sort: str = "date", direction: str = "asc",
               employee: str = "", page_size: int = DEFAULT_PAGE_SIZE):
    user = _access(request, source)
    if isinstance(user, RedirectResponse):
        return user
    try:
        checked_table_options(source, sort, direction, employee, page_size)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    def table_url(*, page: int | None = None, issue_page_number: int = 1, focus: str | None = None) -> str:
        return _table_url(source, page=page, issue_page_number=issue_page_number, focus=focus,
                          sort=sort, direction=direction, employee=employee, page_size=page_size)
    if focus and ":" in focus:
        kind, item_id = focus.split(":", 1)
        found_page = page_for_item(source, kind, item_id, sort=sort, direction=direction,
                                   employee=employee, page_size=page_size)
        if found_page:
            page = found_page
    rows, page, pages, table_total = table_page(source, page, sort=sort, direction=direction,
                                                employee=employee, page_size=page_size)
    issues, issue_page, issue_pages = issue_page_for_source(source, issue_page, sort=sort,
                                                            direction=direction, employee=employee, page_size=page_size)
    for issue in issues:
        issue["href"] = table_url(issue_page_number=issue_page, focus=issue["row_key"]) + "#" + issue["dom_id"]
    sort_options = [("date", "Дата"), ("plan", "Потребность" if source == "welding_need" else "План")]
    if source != "welding_need":
        sort_options.append(("fact", "Факт"))
    sort_options.append(("status", "Статус"))
    return templates.TemplateResponse(
        request=request, name="table.html",
        context={
            **_base(request, user, source), "source": source, "title": LABELS[source],
            "fields": [{"name": field, "label": FIELD_LABELS[field]} for field in SPECS[source]],
            "rows": rows, "page": page, "pages": pages, "issues": issues,
            "issue_page": issue_page, "issue_pages": issue_pages,
            "issue_total": issue_count(source, employee), "focus": focus,
            "table_total": table_total, "sort": sort, "direction": direction,
            "employee": employee, "page_size": page_size, "page_sizes": PAGE_SIZES,
            "employee_options": employee_options(source),
            "sort_options": sort_options,
            "table_url": table_url,
        },
    )


def issue_page_for_source(source: str, number: int, **options) -> tuple[list[dict], int, int]:
    return issue_page(source, number, **options)


@app.post("/tables/{source}/upload")
async def upload(request: Request, source: str):
    user = _access(request, source, director_only=True)
    if isinstance(user, RedirectResponse):
        return user
    form = await request.form()
    _check_csrf(request, str(form.get("csrf", "")))
    file = form.get("file")
    if not isinstance(file, UploadFile) or not file.filename:
        _flash(request, "error", "Выберите CSV или XLSX файл")
    else:
        content = await file.read()
        if len(content) > 20 * 1024 * 1024:
            _flash(request, "error", "Файл больше 20 МБ. Для этого демо загрузите файл меньшего размера.")
        else:
            try:
                result = import_file(file.filename, content, expected_source=source)
                if result["already_imported"]:
                    _flash(request, "info", f"{file.filename}: этот файл уже загружен")
                else:
                    _flash(
                        request, "success",
                        f"{file.filename}: принято без проверки {result['inserted'] - result['pending']}, "
                        f"ожидают проверки {result['pending']}, не добавлены {result['rejected']}. "
                        f"Проблемные строки показаны справа и в таблице.",
                    )
            except (ValueError, BadZipFile, UnicodeDecodeError) as exc:
                _flash(request, "error", f"{file.filename}: {exc}")
    return RedirectResponse(_table_url(source), status_code=303)


@app.post("/tables/{source}/decision")
async def row_decision(request: Request, source: str):
    user = _access(request, source, director_only=True)
    if isinstance(user, RedirectResponse):
        return user
    form = await request.form()
    _check_csrf(request, str(form.get("csrf", "")))
    kind = str(form.get("kind", ""))
    item_id = str(form.get("item_id", ""))
    action = str(form.get("action", ""))
    try:
        page = max(1, int(str(form.get("page", "1"))))
        issue_number = max(1, int(str(form.get("issue_page", "1"))))
    except ValueError:
        raise HTTPException(status_code=400, detail="Некорректная страница")
    sort, direction, employee, page_size = _posted_table_options(source, form)
    focus = f"{kind}:{item_id}"
    try:
        if action == "accept":
            raw = {field: form.get(field) for field in SPECS[source]}
            new_id = accept_edited_row(source, kind, item_id, raw, user["username"])
            focus = f"record:{new_id}"
            _flash(request, "success", f"Строка {new_id} сохранена и включена в подтверждённые расчёты")
        elif action == "delete":
            delete_problem_row(source, kind, item_id, user["username"])
            focus = None
            _flash(request, "success", "Проблемная строка удалена из рабочей таблицы")
        else:
            raise ValueError("Неизвестное действие")
    except ValueError as exc:
        _flash(request, "error", str(exc))
    target = _table_url(source, page=page, issue_page_number=issue_number, focus=focus,
                        sort=sort, direction=direction, employee=employee, page_size=page_size)
    if focus:
        from hashlib import sha256
        target += "#row-" + sha256(focus.encode()).hexdigest()[:16]
    return RedirectResponse(target, status_code=303)


@app.post("/tables/{source}/fact")
async def staff_fact(request: Request, source: str):
    user = _access(request, source)
    if isinstance(user, RedirectResponse):
        return user
    if user["role"] != source or source not in ("preparation", "documentation"):
        raise HTTPException(status_code=403, detail="Факт можно менять только в таблице своего участка")
    form = await request.form()
    _check_csrf(request, str(form.get("csrf", "")))
    record_id = str(form.get("record_id", ""))
    try:
        page = max(1, int(str(form.get("page", "1"))))
        issue_number = max(1, int(str(form.get("issue_page", "1"))))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="Некорректная страница") from exc
    sort, direction, employee, page_size = _posted_table_options(source, form)
    try:
        status = update_staff_fact(source, record_id, str(form.get("fact", "")), user["username"], user["employee_name"])
        if status == "approved":
            _flash(request, "success", "Факт сохранён и учтён в показателях")
        else:
            _flash(request, "info", "Факт сохранён, но строка требует проверки директора и пока не учитывается")
    except ValueError as exc:
        _flash(request, "error", str(exc))
    focus = f"record:{record_id}"
    target = _table_url(source, page=page, issue_page_number=issue_number, focus=focus,
                        sort=sort, direction=direction, employee=employee, page_size=page_size)
    from hashlib import sha256
    return RedirectResponse(target + "#row-" + sha256(focus.encode()).hexdigest()[:16], status_code=303)


@app.get("/report")
def download_report(request: Request, start: str | None = None, end: str | None = None):
    user = _user(request)
    if not user:
        return RedirectResponse("/login", status_code=303)
    if user["role"] != "director":
        raise HTTPException(status_code=403, detail="Доступно только директору")
    minimum, maximum = date_bounds()
    first, last = _parse_date(start, minimum), _parse_date(end, maximum)
    if first > last:
        raise HTTPException(status_code=400, detail="Начало периода позже конца")
    filename = f"report_{first:%Y%m%d}_{last:%Y%m%d}.xlsx"
    return Response(
        make_report(first, last),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
