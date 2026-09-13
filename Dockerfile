# ---- Frontend CSS build: Tailwind + daisyUI -> one static stylesheet ----
# Node is used ONLY here, at build time. The runtime image below is pure
# Python; it just copies the compiled webatem.css out of this stage.
# --platform=$BUILDPLATFORM: the CSS is architecture-neutral text, so this
# stage runs on the builder's own arch even when the image is built for
# arm64 (a Raspberry Pi) under emulation — npm under QEMU is painfully slow.
FROM --platform=$BUILDPLATFORM node:20-alpine AS css
WORKDIR /build
COPY package.json tailwind.config.js ./
RUN npm install --no-audit --no-fund
COPY styles ./styles
COPY webatem/templates ./webatem/templates
COPY atem_control/templates ./atem_control/templates
COPY atem_control/static/js ./atem_control/static/js
RUN npx tailwindcss -c tailwind.config.js -i styles/app.css -o webatem.css --minify

# ---- Runtime image ----
FROM python:3.14-slim-trixie

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

COPY . /app
# The compiled stylesheet, into the source tree BEFORE the package is
# installed so the installed copy carries it.
COPY --from=css /build/webatem.css /app/atem_control/static/vendor/webatem.css

# Install the app as the `webatem` package (pyproject.toml is the one list
# of dependencies; the test extras are for the CI run inside this image).
# gcc is here for ONE reason: atemwire builds its mediaconvert C extension
# from the sdist where no wheel matches. Purged after.
RUN apt-get update \
    && apt-get install -y --no-install-recommends gcc libc6-dev \
    && pip install --no-cache-dir --upgrade pip \
    && pip install --no-cache-dir -c requirements.txt ".[test]" \
    && python3 -c 'import atemwire.mediaconvert, hyperdeckwire, webatem, atem_control; print("webatem + libraries OK")' \
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

EXPOSE 8880
ENTRYPOINT ["/app/docker-entrypoint.sh"]
