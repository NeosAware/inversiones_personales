#!/usr/bin/env bash
set -euo pipefail

APP_ROOT="/var/www/personal.neosaware.ai"
APP_DIR="$APP_ROOT/app"
VENV_DIR="$APP_ROOT/venv"
SERVICE_NAME="personal-neosaware-ai"

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
# shellcheck disable=SC1091
source "$APP_ROOT/.env"
set +a

python3 manage.py migrate
python3 manage.py collectstatic --noinput

sudo systemctl restart "$SERVICE_NAME"
sudo systemctl status "$SERVICE_NAME" --no-pager --lines=20
curl -fsS http://127.0.0.1:8082/health/
