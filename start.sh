#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")"

if [[ ! -f .mvp.env ]]; then
  umask 077
  printf 'MVP_DB_PASSWORD=%s\n' "$(python3 -c 'import secrets; print(secrets.token_hex(16))')" > .mvp.env
fi
if ! grep -q '^MVP_SESSION_SECRET=' .mvp.env; then
  umask 077
  printf 'MVP_SESSION_SECRET=%s\n' "$(python3 -c 'import secrets; print(secrets.token_hex(32))')" >> .mvp.env
fi
# The generated file contains only hexadecimal secrets and is ignored by Git.
source ./.mvp.env
export MVP_SESSION_SECRET

if [[ -z "${DATABASE_URL:-}" ]]; then
  container="${POSTGRES_CONTAINER:-postgres16}"
  if ! docker inspect "$container" >/dev/null 2>&1; then
    echo "Контейнер $container не найден. Укажите POSTGRES_CONTAINER или DATABASE_URL." >&2
    exit 1
  fi
  if [[ "$(docker inspect -f '{{.State.Running}}' "$container")" != "true" ]]; then
    docker start "$container" >/dev/null
  fi

  read -r admin_user admin_db < <(docker exec "$container" sh -c 'printf "%s %s\n" "${POSTGRES_USER:-postgres}" "${POSTGRES_DB:-postgres}"')

  docker exec -i "$container" psql -X -q -v ON_ERROR_STOP=1 -U "$admin_user" -d "$admin_db" -v project_password="$MVP_DB_PASSWORD" <<'SQL'
SELECT format('CREATE ROLE mvp_task LOGIN PASSWORD %L', :'project_password')
WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname='mvp_task') \gexec
SELECT format('ALTER ROLE mvp_task PASSWORD %L', :'project_password') \gexec
SELECT 'CREATE DATABASE mvp_task OWNER mvp_task'
WHERE NOT EXISTS (SELECT 1 FROM pg_database WHERE datname='mvp_task') \gexec
SQL

  export DATABASE_URL="postgresql://mvp_task:${MVP_DB_PASSWORD}@127.0.0.1:${POSTGRES_PORT:-5432}/mvp_task"
fi

export DIRECTOR_PASSWORD="${DIRECTOR_PASSWORD:-director123}"
export STAFF_PASSWORD="${STAFF_PASSWORD:-staff123}"

if ! command -v uv >/dev/null 2>&1; then
  echo "Нужен uv: https://docs.astral.sh/uv/getting-started/installation/" >&2
  exit 1
fi

uv sync
uv run python -m mvp_task.cli init
exec uv run uvicorn mvp_task.web:app --host 127.0.0.1 --port "${APP_PORT:-8501}"
