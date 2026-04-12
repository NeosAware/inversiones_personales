# Despliegue en IONOS: `personal.neosaware.ai`

Este proyecto queda preparado para subirlo a un servidor Linux con Django + Gunicorn + PostgreSQL.

## 1. Estructura recomendada en el servidor

```text
/var/www/personal.neosaware.ai/
  app/           <- codigo Django
  venv/          <- entorno virtual
  media/         <- ficheros subidos desde la web
  staticfiles/   <- salida de collectstatic
  .env           <- variables de entorno de produccion
```

## 2. Base de datos PostgreSQL

Crear la base de datos y el usuario:

```sql
CREATE DATABASE inversiones_personales;
CREATE USER inversiones_personales WITH ENCRYPTED PASSWORD 'CAMBIAR_ESTA_PASSWORD';
GRANT ALL PRIVILEGES ON DATABASE inversiones_personales TO inversiones_personales;
```

## 3. Subir la aplicacion

```bash
mkdir -p /var/www/personal.neosaware.ai
cd /var/www/personal.neosaware.ai
python3 -m venv venv
source venv/bin/activate
mkdir -p app media staticfiles
```

Sube el contenido del proyecto dentro de `/var/www/personal.neosaware.ai/app`.

## 4. Instalar dependencias

```bash
cd /var/www/personal.neosaware.ai/app
source ../venv/bin/activate
pip install --upgrade pip
pip install -r requirements-prod.txt
```

## 5. Variables de entorno

Crear `/var/www/personal.neosaware.ai/.env` a partir de `.env.example` y ajustar:

```dotenv
DJANGO_SECRET_KEY=poner-una-clave-larga-y-aleatoria
DJANGO_DEBUG=0
APP_ALLOWED_HOSTS=personal.neosaware.ai
APP_CSRF_TRUSTED_ORIGINS=https://personal.neosaware.ai
DB_ENGINE=postgresql
POSTGRES_DB=inversiones_personales
POSTGRES_USER=inversiones_personales
POSTGRES_PASSWORD=CAMBIAR_ESTA_PASSWORD
POSTGRES_HOST=127.0.0.1
POSTGRES_PORT=5432
APP_STATIC_ROOT=/var/www/personal.neosaware.ai/staticfiles
APP_MEDIA_ROOT=/var/www/personal.neosaware.ai/media
APP_USE_X_FORWARDED_HOST=1
APP_SECURE_PROXY_SSL_HEADER=HTTP_X_FORWARDED_PROTO,https
APP_SECURE_SSL_REDIRECT=1
APP_SESSION_COOKIE_SECURE=1
APP_CSRF_COOKIE_SECURE=1
APP_SECURE_HSTS_SECONDS=31536000
APP_SECURE_HSTS_INCLUDE_SUBDOMAINS=1
APP_SECURE_HSTS_PRELOAD=1
```

Importante: la aplicacion ya no cae a SQLite en produccion. Si este `.env` no se carga en el servicio, Django fallara al arrancar para evitar usar una base equivocada.

## 6. Migraciones, estaticos y usuario inicial

```bash
cd /var/www/personal.neosaware.ai/app
source ../venv/bin/activate
set -a
. ../.env
set +a
python manage.py migrate
python manage.py collectstatic --noinput
python manage.py createsuperuser
```

Si quieres crear un usuario adicional para acceso normal:

```bash
python manage.py ensure_household_user --username household --password CAMBIAR_PASSWORD
```

## 7. Gunicorn

Prueba manual:

```bash
cd /var/www/personal.neosaware.ai/app
source ../venv/bin/activate
set -a
. ../.env
set +a
gunicorn config.wsgi:application --bind 127.0.0.1:8000
```

Si lo arrancas con `systemd`, el servicio debe cargar el `.env`. Ejemplo minimo:

```ini
[Service]
WorkingDirectory=/var/www/personal.neosaware.ai/app
EnvironmentFile=/var/www/personal.neosaware.ai/.env
ExecStart=/var/www/personal.neosaware.ai/venv/bin/gunicorn config.wsgi:application --bind 127.0.0.1:8000
```

Healthcheck publico:

```text
https://personal.neosaware.ai/health/
```

Debe devolver `ok`.

## 8. Proxy inverso

Ejemplo de bloque `server` con Nginx:

```nginx
server {
    listen 80;
    server_name personal.neosaware.ai;
    return 301 https://$host$request_uri;
}

server {
    listen 443 ssl http2;
    server_name personal.neosaware.ai;

    client_max_body_size 25M;

    location /static/ {
        alias /var/www/personal.neosaware.ai/staticfiles/;
    }

    location /media/ {
        alias /var/www/personal.neosaware.ai/media/;
    }

    location / {
        proxy_pass http://127.0.0.1:8000;
        proxy_set_header Host $host;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto https;
        proxy_redirect off;
    }
}
```

## 9. DNS en IONOS

En IONOS tendreis que crear el host/subdominio `personal.neosaware.ai` y apuntarlo a la IP publica del servidor donde corra esta app.

## 10. Checklist final

- Base de datos PostgreSQL creada
- Usuario administrador creado
- Variables de entorno cargadas
- `migrate` ejecutado
- `collectstatic` ejecutado
- `personal.neosaware.ai` apuntando a la IP correcta
- HTTPS activo
- `/health/` devolviendo `ok`
- Login obligatorio al entrar en la web
