# Personal Investments Hub

Starter Django project to manage multiple personal investment buckets in one place:

- bank balances
- listed equities
- Neos Additives
- Neos Ceramica
- Neos Materials
- real estate

## Main ideas

- each app stores its own positions
- the `portfolio` app aggregates all positions into one dashboard
- the admin is enabled so you can start loading data immediately

## Run locally

```bash
python manage.py makemigrations
python manage.py migrate
python manage.py createsuperuser
python manage.py runserver
```

Open:

- `http://127.0.0.1:8000/` for the dashboard
- `http://127.0.0.1:8000/admin/` for data entry

## Household mode

The app now supports a shared local workflow for two people in the same home:

- login required for all portfolio modules
- global login gate enforced before opening any private page
- local-network launch script
- daily portfolio snapshots
- daily database backups
- spending alerts on the main dashboard

### Start on your home Wi-Fi

From PowerShell:

```powershell
cd C:\Users\Gerencia\Documents\inversiones_personales
.\scripts\start_home_server.ps1
```

That script exposes the app on `0.0.0.0:8000` and prepares local-network hosts automatically.
If `POSTGRES_PASSWORD` is not configured, the script now stops instead of switching silently to SQLite.

### Share without touching the router

For a temporary public link, use a Cloudflare Quick Tunnel instead of relying on local Wi-Fi visibility.

Install `cloudflared` on Windows:

```powershell
winget install --id Cloudflare.cloudflared
```

Then start the app and the tunnel together:

```powershell
cd C:\Users\Gerencia\Documents\inversiones_personales
.\scripts\start_cloudflare_tunnel.ps1
```

The script:

1. starts Django on `127.0.0.1:8000`
2. allows the temporary `trycloudflare.com` hostname in Django
3. opens a Cloudflare Quick Tunnel to your local server

If `POSTGRES_PASSWORD` is not configured, the tunnel script also stops instead of changing silently to SQLite.

Share the HTTPS URL printed by `cloudflared` with Monica.

### Daily maintenance

This script now:

- runs the full nightly equities analysis if the clock is already past `00:00`
- captures the daily household snapshot
- creates a backup

Command:

```powershell
cd C:\Users\Gerencia\Documents\inversiones_personales
.\scripts\daily_household_maintenance.ps1
```

You can schedule it daily with Windows Task Scheduler just after midnight, for example at `00:05`.

### PostgreSQL mode

The project is now configured to run on PostgreSQL through environment variables:

```powershell
$env:DB_ENGINE='postgresql'
$env:POSTGRES_DB='inversiones_personales'
$env:POSTGRES_USER='postgres'
$env:POSTGRES_PASSWORD='YOUR_PASSWORD'
$env:POSTGRES_HOST='127.0.0.1'
$env:POSTGRES_PORT='5432'
python manage.py migrate
```

Production no longer falls back to SQLite when `DB_ENGINE` or the PostgreSQL variables are missing. If you want a one-off local SQLite session, set `DB_ENGINE=sqlite` explicitly and only in development.

### Backups

Manual backup:

```powershell
python manage.py backup_database --include-media
```

If `pg_dump` is not available in PATH, the command falls back to a JSON data backup plus the media ZIP.

### Rebuild PostgreSQL from SQLite

If you ever want to wipe PostgreSQL and repopulate it from the current SQLite file, use:

```powershell
cd C:\Users\Gerencia\Documents\inversiones_personales
.\scripts\rebuild_postgres_from_sqlite.ps1 -Force
```

Optional:

- `-IncludeMedia` also zips the `media` folder in the pre-reset backup
- `-SkipBackup` skips the PostgreSQL backup step

The script:

1. exports the current SQLite data
2. backs up PostgreSQL
3. drops and recreates the PostgreSQL database
4. runs migrations
5. reloads all exported data
6. captures a fresh portfolio snapshot

### Snapshots and alerts

The dashboard now stores and shows:

- daily portfolio value snapshots
- nightly full-stock analysis snapshots for tracked positions and the whole IBEX
- historical evolution of current portfolio value
- spending alerts when monthly expenses exceed the cap or spike versus recent months

You can also capture a snapshot manually from the dashboard or with:

```powershell
python manage.py capture_portfolio_snapshot
```

Nightly equities analysis can also be launched manually:

```powershell
python manage.py run_equity_nightly_analysis --force
```

By default the command only runs from `00:00` onwards and stores:

- the full analysis card for each tracked stock
- the full analysis card for every IBEX company, including `12M`, `5A`, backtest and ratios
- an optional IA synthesis per company using your `Claude` or `ChatGPT` API account
- a reusable nightly cache so daytime dashboard views and optimizations do not need to rebuild the heavy analysis

LLM provider selection is controlled with environment variables:

```powershell
AI_LLM_PROVIDER=anthropic
ANTHROPIC_API_KEY=...
CLAUDE_DEFAULT_MODEL=claude-sonnet-4-20250514
CLAUDE_MAX_TOKENS=1024
CLAUDE_MONTHLY_BUDGET_USD=50.00

# or
AI_LLM_PROVIDER=openai
OPENAI_API_KEY=...
OPENAI_DEFAULT_MODEL=gpt-4o-mini
OPENAI_MAX_TOKENS=2048
```

## Production deployment

Server-ready items included in this project:

- global login enforcement via Django middleware
- public healthcheck at `/health/`
- production dependency file: `requirements-prod.txt`
- example environment file: `.env.example`
- static collection target through `STATIC_ROOT`
- deployment guide for IONOS: `deploy/ionos/DEPLOY.md`

Typical server flow:

```bash
python3 manage.py migrate
python3 manage.py collectstatic --noinput
python3 manage.py createsuperuser
gunicorn config.wsgi:application --bind 127.0.0.1:8082
```

If you deployed and cannot log in, create or reset an access user from the server:

```bash
set -a
. ../.env
set +a
python3 manage.py ensure_household_user --username household --password-env APP_BOOTSTRAP_PASSWORD --superuser
```

The IONOS deploy script also does this automatically when `APP_BOOTSTRAP_USERNAME` and `APP_BOOTSTRAP_PASSWORD` are set in the production `.env`.

## Banking extract import

The `banking` module can import monthly account extracts and summarise them into:

- income
- expenses by concept
- pension / savings plan contributions
- stock dividends

Supported files:

- `.xlsx` directly
- `.xls` directly through Python (`xlrd` for legacy Excel files, plus HTML-table fallback for bank exports)

## Encrypted uploads

Uploaded documents can be encrypted at rest with a Fernet key stored outside the codebase.

Set `APP_MEDIA_ENCRYPTION_KEY` in your environment and restart the app:

```powershell
@'
from cryptography.fernet import Fernet
print(Fernet.generate_key().decode())
'@ | python -
```

Then put the generated value into `.env` as:

```text
APP_MEDIA_ENCRYPTION_KEY=YOUR_GENERATED_FERNET_KEY
```

When this setting is active:

- uploaded files are stored encrypted inside `MEDIA_ROOT`
- document links are served through an authenticated Django route instead of exposing raw files directly
- existing unencrypted files remain readable, but they are not retroactively encrypted until you upload them again

## Local bank robot for personal use

The banking module now uses a local robot on your own Windows PC. It opens each bank website, downloads the XLS/XLSX or PDF statement, and uploads it to this Django app.

Server-side requirement:

```text
BANK_ROBOT_IMPORT_TOKEN=your-long-random-token
```

The robot uploads to:

```text
POST /banking/robot/upload/
```

using the header:

```text
X-Bank-Robot-Token: your-long-random-token
```

### Install the local robot on Windows

```powershell
cd C:\Users\Gerencia\Documents\inversiones_personales
python -m pip install -r requirements-robot.txt
python -m playwright install chromium
```

### Save bank credentials in Windows Credential Manager

```powershell
python .\scripts\set_bank_robot_secret.py --job sabadell-cuenta-ximo --name username
python .\scripts\set_bank_robot_secret.py --job sabadell-cuenta-ximo --name password
```

The script stores secrets under the service name:

```text
inversiones_personales.bank_robot.<job-id>
```

### Configure one job per bank/account/card

Copy the example file and edit selectors/URLs for each bank:

```powershell
Copy-Item .\scripts\bank_robot.example.json .\scripts\bank_robot.config.json
```

Each job can represent:

- one current account
- one savings account
- one card

Recommended pattern:

- one job per bank login profile
- one job per account/card export page
- reuse the same `storage_state_path` inside one bank if the session is shared

### Run the robot manually

```powershell
setx BANK_ROBOT_IMPORT_TOKEN "your-long-random-token"
.\scripts\run_bank_robot.ps1 -ConfigPath .\scripts\bank_robot.config.json -Job sabadell-cuenta-ximo -Headed
```

Useful flags:

- `-Headed` shows the browser
- `-Headless` runs hidden
- `-DryRun` downloads but does not upload
- `-SkipUpload` keeps files local only

### Schedule it in Windows Task Scheduler

Create the task automatically:

```powershell
.\scripts\register_bank_robot_task.ps1 -TaskName "Robot bancos" -Time "08:00" -ConfigPath .\scripts\bank_robot.config.json
```

Or run only selected jobs:

```powershell
.\scripts\register_bank_robot_task.ps1 -TaskName "Robot Sabadell" -Time "08:00" -ConfigPath .\scripts\bank_robot.config.json -Job sabadell-cuenta-ximo -Job sabadell-tarjeta-ximo
```

### Practical notes

- the robot runs on your PC, not on the server
- your bank password stays in Windows Credential Manager, not in the server
- cards and accounts are separated using `statement_kind = card/account`
- if a bank changes its website, update only that job's steps in `bank_robot.config.json`
