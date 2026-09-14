#!/bin/sh
set -e

python manage.py migrate --noinput

# SINGLE worker only — see config/asgi.py.
exec uvicorn webatem.asgi:application --host 0.0.0.0 --port "${PORT:-8880}"
