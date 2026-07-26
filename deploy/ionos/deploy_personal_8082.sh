#!/usr/bin/env bash
set -euo pipefail

APP_ROOT="${APP_ROOT:-/root/personal}"
APP_DIR="$APP_ROOT/app"
VENV_DIR="$APP_ROOT/venv"
SERVICE_NAME="${SERVICE_NAME:-neos-personal.service}"

cd "$APP_DIR"
git pull origin main

if [ ! -x "$VENV_DIR/bin/python3" ]; then
  python3 -m venv "$VENV_DIR"
fi

# shellcheck disable=SC1091
source "$VENV_DIR/bin/activate"
python3 -m pip install --upgrade pip
python3 -m pip install -r requirements-prod.txt

set -a
if [ -f "$APP_ROOT/.env" ]; then
  # shellcheck disable=SC1091
  source "$APP_ROOT/.env"
fi
set +a

python3 manage.py migrate
python3 manage.py collectstatic --noinput

if [ -n "${APP_BOOTSTRAP_USERNAME:-}" ] || [ -n "${APP_BOOTSTRAP_PASSWORD:-}" ]; then
  if [ -z "${APP_BOOTSTRAP_USERNAME:-}" ] || [ -z "${APP_BOOTSTRAP_PASSWORD:-}" ]; then
    echo "APP_BOOTSTRAP_USERNAME y APP_BOOTSTRAP_PASSWORD deben configurarse juntos para crear el acceso inicial." >&2
    exit 1
  fi

  bootstrap_args=(manage.py ensure_household_user --username "$APP_BOOTSTRAP_USERNAME" --password-env APP_BOOTSTRAP_PASSWORD)
  if [ -n "${APP_BOOTSTRAP_EMAIL:-}" ]; then
    bootstrap_args+=(--email "$APP_BOOTSTRAP_EMAIL")
  fi

  case "${APP_BOOTSTRAP_ROLE:-superuser}" in
    superuser|admin)
      bootstrap_args+=(--superuser)
      ;;
    staff)
      bootstrap_args+=(--staff)
      ;;
    user)
      ;;
    *)
      echo "APP_BOOTSTRAP_ROLE debe ser superuser, staff o user." >&2
      exit 1
      ;;
  esac

  python3 "${bootstrap_args[@]}"
fi

sudo systemctl restart "$SERVICE_NAME"
sudo systemctl status "$SERVICE_NAME" --no-pager --lines=20
curl -fsS http://127.0.0.1:8082/health/
