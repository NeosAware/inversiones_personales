$env:DB_ENGINE = "postgresql"
$env:POSTGRES_DB = "inversiones_personales"
$env:POSTGRES_USER = "postgres"
$env:POSTGRES_HOST = "127.0.0.1"
$env:POSTGRES_PORT = "5432"
python manage.py run_equity_nightly_analysis
python manage.py capture_portfolio_snapshot
python manage.py backup_database --include-media
