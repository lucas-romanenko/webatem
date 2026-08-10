# ---- Frontend CSS build: Tailwind + daisyUI -> one static stylesheet ----
# Node is used ONLY here, at build time. The runtime image below is pure
# Python; it just copies the compiled webatem.css out of this stage.
FROM node:20-alpine AS css
WORKDIR /build
COPY package.json tailwind.config.js ./
RUN npm install --no-audit --no-fund
COPY styles ./styles
COPY templates ./templates
COPY atem_control/templates ./atem_control/templates
COPY atem_control/static/js ./atem_control/static/js
RUN npx tailwindcss -c tailwind.config.js -i styles/app.css -o webatem.css --minify

# ---- Runtime image ----
FROM python:3.14-slim-trixie

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

COPY requirements.txt /app/
RUN pip install --no-cache-dir --upgrade pip \
    && pip install --no-cache-dir -r requirements.txt

COPY . /app

# Drop in the compiled stylesheet from the css stage (before collectstatic).
COPY --from=css /build/webatem.css /app/atem_control/static/vendor/webatem.css

# Compile the pyatem mediaconvert C extension (BT.709 YCbCr<->RGB + RLE)
# in place; CPython imports the ABI-suffixed .so alongside the .py files.
RUN apt-get update \
    && apt-get install -y --no-install-recommends gcc libc6-dev \
    && INCLUDE_DIR="$(python3 -c 'import sysconfig; print(sysconfig.get_paths()["include"])')" \
    && EXT_SUFFIX="$(python3 -c 'import sysconfig; print(sysconfig.get_config_var("EXT_SUFFIX"))')" \
    && gcc -shared -fPIC -O2 -I"$INCLUDE_DIR" \
        pyatem/mediaconvertmodule.c \
        -o "pyatem/mediaconvert$EXT_SUFFIX" \
    && python3 -c 'import pyatem.mediaconvert; print("pyatem.mediaconvert OK")' \
    && apt-get remove -y --purge gcc libc6-dev \
    && apt-get autoremove -y \
    && rm -rf /var/lib/apt/lists/*

# Static assets are baked at build time (WhiteNoise serves them with
# DEBUG=False). SECRET_KEY here is build-scoped and never persisted.
RUN SECRET_KEY=build-only DEBUG=False python manage.py collectstatic --noinput

RUN adduser --uid 1000 --disabled-password --gecos "" appuser \
    && mkdir -p /app/data \
    && chown -R appuser:appuser /app
USER appuser

EXPOSE 8000
ENTRYPOINT ["/app/docker-entrypoint.sh"]
