#!/bin/sh
set -e

python manage.py migrate --noinput

# SINGLE worker only — see config/asgi.py.
exec uvicorn config.asgi:application --host 0.0.0.0 --port 8000
