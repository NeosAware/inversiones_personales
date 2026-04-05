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
If `POSTGRES_PASSWORD` is not configured, it now falls back to the local SQLite database instead of forcing PostgreSQL and leaving the site returning `500`.

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

If `POSTGRES_PASSWORD` is not configured, the tunnel script also falls back to SQLite automatically.

Share the HTTPS URL printed by `cloudflared` with Monica.

### Daily maintenance

This script captures the daily household snapshot and creates a backup:

```powershell
cd C:\Users\Gerencia\Documents\inversiones_personales
.\scripts\daily_household_maintenance.ps1
```

You can schedule it daily with Windows Task Scheduler.

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
- historical evolution of current portfolio value
- spending alerts when monthly expenses exceed the cap or spike versus recent months

You can also capture a snapshot manually from the dashboard or with:

```powershell
python manage.py capture_portfolio_snapshot
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
python manage.py migrate
python manage.py collectstatic --noinput
python manage.py createsuperuser
gunicorn config.wsgi:application --bind 127.0.0.1:8000
```

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
