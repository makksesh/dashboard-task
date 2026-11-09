"""PostgreSQL schema and demo accounts."""

from __future__ import annotations

import hashlib
import os
import secrets

import psycopg
from psycopg.rows import dict_row


def connect():
    url = os.environ.get("DATABASE_URL")
    if not url:
        raise RuntimeError("DATABASE_URL не задан. Запустите проект через ./start.sh")
    return psycopg.connect(url, row_factory=dict_row)


SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    username text PRIMARY KEY,
    salt text NOT NULL,
    password_hash text NOT NULL,
    role text NOT NULL CHECK (role IN ('director', 'preparation', 'documentation')),
    employee_name text
);
CREATE TABLE IF NOT EXISTS imports (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    source_type text NOT NULL,
    filename text NOT NULL,
    file_hash text NOT NULL,
    imported_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (source_type, file_hash)
);
CREATE TABLE IF NOT EXISTS preparation (
    record_id text PRIMARY KEY,
    work_date date NOT NULL,
    batch_id text NOT NULL,
    employee text NOT NULL,
    plan_qty integer,
    fact_qty integer,
    review_status text NOT NULL CHECK (review_status IN ('approved', 'pending')),
    issue_reason text NOT NULL DEFAULT '',
    import_id bigint REFERENCES imports(id),
    source_row integer
);
CREATE TABLE IF NOT EXISTS welding_need (
    record_id text PRIMARY KEY,
    work_date date NOT NULL,
    batch_id text NOT NULL,
    need_qty integer,
    review_status text NOT NULL CHECK (review_status IN ('approved', 'pending')),
    issue_reason text NOT NULL DEFAULT '',
    import_id bigint REFERENCES imports(id),
    source_row integer
);
CREATE TABLE IF NOT EXISTS documentation (
    record_id text PRIMARY KEY,
    work_date date NOT NULL,
    employee text NOT NULL,
    plan_docs integer,
    fact_docs integer,
    review_status text NOT NULL CHECK (review_status IN ('approved', 'pending')),
    issue_reason text NOT NULL DEFAULT '',
    import_id bigint REFERENCES imports(id),
    source_row integer
);
CREATE TABLE IF NOT EXISTS rejected_rows (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    import_id bigint NOT NULL REFERENCES imports(id),
    source_row integer NOT NULL,
    record_id text,
    reason text NOT NULL,
    raw_data jsonb NOT NULL,
    resolution text,
    resolved_at timestamptz
);
ALTER TABLE rejected_rows ADD COLUMN IF NOT EXISTS resolution text;
ALTER TABLE rejected_rows ADD COLUMN IF NOT EXISTS resolved_at timestamptz;
CREATE TABLE IF NOT EXISTS review_log (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    source_type text NOT NULL,
    record_id text NOT NULL,
    username text NOT NULL,
    action text NOT NULL,
    changed_at timestamptz NOT NULL DEFAULT now()
);
ALTER TABLE review_log ADD COLUMN IF NOT EXISTS work_date date;
ALTER TABLE review_log ADD COLUMN IF NOT EXISTS result_status text;
ALTER TABLE review_log ADD COLUMN IF NOT EXISTS issue_reason text;
CREATE INDEX IF NOT EXISTS preparation_date_idx ON preparation(work_date);
CREATE INDEX IF NOT EXISTS preparation_batch_idx ON preparation(batch_id);
CREATE INDEX IF NOT EXISTS welding_need_date_idx ON welding_need(work_date);
CREATE INDEX IF NOT EXISTS documentation_date_idx ON documentation(work_date);
"""


def _hash_password(password: str, salt: str) -> str:
    return hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt), 260_000).hex()


def initialize() -> None:
    director_password = os.environ.get("DIRECTOR_PASSWORD", "director123")
    staff_password = os.environ.get("STAFF_PASSWORD", "staff123")
    accounts = [
        ("director", director_password, "director", None),
        ("anna", staff_password, "preparation", "Анна Соколова"),
        ("ivan", staff_password, "preparation", "Иван Морозов"),
        ("maria", staff_password, "documentation", "Мария Лебедева"),
        ("oleg", staff_password, "documentation", "Олег Волков"),
    ]
    with connect() as conn:
        conn.execute(SCHEMA)
        for username, password, role, employee_name in accounts:
            salt = secrets.token_hex(16)
            conn.execute(
                "INSERT INTO users (username,salt,password_hash,role,employee_name) VALUES (%s,%s,%s,%s,%s) ON CONFLICT (username) DO NOTHING",
                (username, salt, _hash_password(password, salt), role, employee_name),
            )


def authenticate(username: str, password: str) -> dict | None:
    with connect() as conn:
        row = conn.execute("SELECT * FROM users WHERE username=%s", (username,)).fetchone()
    if row and secrets.compare_digest(_hash_password(password, row["salt"]), row["password_hash"]):
        return {"username": row["username"], "role": row["role"], "employee_name": row["employee_name"]}
    return None
