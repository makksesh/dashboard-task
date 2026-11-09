#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")"

if ! command -v docker >/dev/null 2>&1; then
  echo "Docker не найден. Установите Docker Desktop или Docker Engine: https://docs.docker.com/get-docker/" >&2
  exit 1
fi

as_root() {
  if [[ "$(id -u)" -eq 0 ]]; then
    "$@"
  elif command -v sudo >/dev/null 2>&1; then
    sudo "$@"
  else
    echo "Для установки Compose нужны права администратора (sudo)." >&2
    return 1
  fi
}

find_compose() {
  if docker compose version >/dev/null 2>&1; then
    compose_cmd=(docker compose)
  elif command -v docker-compose >/dev/null 2>&1 && \
       docker-compose version --short 2>/dev/null | grep -Eq '^v?2\.'; then
    compose_cmd=(docker-compose)
  else
    return 1
  fi
}

install_compose() {
  case "$(uname -s)" in
    Darwin)
      if ! command -v brew >/dev/null 2>&1; then
        echo "Homebrew не найден. Установите Docker Desktop с Compose: https://docs.docker.com/desktop/setup/install/mac-install/" >&2
        return 1
      fi
      brew install docker-compose
      ;;
    Linux)
      if ! command -v apt-get >/dev/null 2>&1; then
        echo "Автоматическая установка поддерживается на Debian/Ubuntu. Инструкция: https://docs.docker.com/compose/install/" >&2
        return 1
      fi
      as_root apt-get update
      if apt-cache show docker-compose-v2 >/dev/null 2>&1; then
        as_root apt-get install -y docker-compose-v2
      elif apt-cache show docker-compose-plugin >/dev/null 2>&1; then
        as_root apt-get install -y docker-compose-plugin
      else
        echo "Пакет Docker Compose не найден в подключённых репозиториях." >&2
        return 1
      fi
      ;;
    *)
      echo "Установите Docker Compose: https://docs.docker.com/compose/install/" >&2
      return 1
      ;;
  esac
}

compose_cmd=()
if ! find_compose; then
  answer="${COMPOSE_INSTALL:-}"
  if [[ -z "$answer" ]]; then
    if [[ ! -t 0 ]]; then
      echo "Docker Compose не найден. Запустите скрипт в терминале для выбора установки или задайте COMPOSE_INSTALL=yes." >&2
      exit 1
    fi
    read -r -p "Docker Compose не найден. Установить сейчас? [y/N] " answer
  fi
  case "$answer" in
    y|Y|yes|YES|да|Да|ДА) install_compose ;;
    *) echo "Установка отменена." >&2; exit 1 ;;
  esac
  find_compose || { echo "Docker Compose не появился после установки." >&2; exit 1; }
fi

if ! docker info >/dev/null 2>&1; then
  if [[ "$(uname -s)" == "Darwin" ]] && command -v open >/dev/null 2>&1; then
    open -a Docker || true
  elif [[ "$(uname -s)" == "Linux" ]] && command -v systemctl >/dev/null 2>&1; then
    as_root systemctl start docker || true
  fi
  for _ in {1..30}; do
    docker info >/dev/null 2>&1 && break
    sleep 2
  done
  docker info >/dev/null 2>&1 || { echo "Docker установлен, но движок не запущен или недоступен." >&2; exit 1; }
fi

env_file=.compose.env
if [[ ! -f "$env_file" ]]; then
  command -v openssl >/dev/null 2>&1 || { echo "Для генерации локальных паролей нужен openssl." >&2; exit 1; }
  bind_host="${APP_BIND_HOST:-127.0.0.1}"
  app_port="${APP_PORT:-8501}"
  case "$bind_host" in 127.0.0.1|0.0.0.0) ;; *) echo "APP_BIND_HOST должен быть 127.0.0.1 или 0.0.0.0" >&2; exit 1 ;; esac
  [[ "$app_port" =~ ^[0-9]+$ ]] && ((app_port >= 1 && app_port <= 65535)) || { echo "APP_PORT должен быть номером порта от 1 до 65535" >&2; exit 1; }
  db_password="$(openssl rand -hex 24)"
  session_secret="$(openssl rand -hex 32)"
  director_password="$(openssl rand -hex 12)"
  staff_password="$(openssl rand -hex 12)"
  umask 077
  temp_file="$(mktemp .compose.env.XXXXXX)"
  trap 'rm -f "$temp_file"' EXIT
  cat > "$temp_file" <<EOF
MVP_DB_PASSWORD=$db_password
MVP_SESSION_SECRET=$session_secret
DIRECTOR_PASSWORD=$director_password
STAFF_PASSWORD=$staff_password
APP_BIND_HOST=$bind_host
APP_PORT=$app_port
EOF
  mv "$temp_file" "$env_file"
  trap - EXIT
  echo "Создан $env_file с правами доступа только для владельца."
  echo "Демо-вход: director / $director_password; сотрудники / $staff_password"
fi

for key in MVP_DB_PASSWORD MVP_SESSION_SECRET DIRECTOR_PASSWORD STAFF_PASSWORD APP_BIND_HOST APP_PORT; do
  grep -Eq "^${key}=.+$" "$env_file" || { echo "В $env_file не задан $key" >&2; exit 1; }
done

app_port="${APP_PORT:-$(sed -n 's/^APP_PORT=//p' "$env_file" | tail -1)}"
[[ "$app_port" =~ ^[0-9]+$ ]] && ((app_port >= 1 && app_port <= 65535)) || { echo "Некорректный APP_PORT" >&2; exit 1; }

compose() { "${compose_cmd[@]}" --env-file "$env_file" "$@"; }
compose config --quiet
compose up --build --detach

if ! command -v curl >/dev/null 2>&1; then
  echo "Контейнеры запущены. Проверьте http://127.0.0.1:$app_port/health"
  exit 0
fi
for _ in {1..60}; do
  if curl --fail --silent --max-time 2 "http://127.0.0.1:$app_port/health" >/dev/null; then
    echo "Приложение запущено: http://127.0.0.1:$app_port"
    exit 0
  fi
  sleep 2
done

compose ps
compose logs --tail=80 app db
echo "Приложение не стало доступно за 120 секунд." >&2
exit 1
