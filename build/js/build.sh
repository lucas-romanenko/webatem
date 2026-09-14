#!/bin/sh
# Bundle atem_control/js/ (ES module sources) into the served
# atem_control/static/js/atem_control.js.
#
# The Docker image build runs this same step so deployments get the bundle
# baked in. During development, rerun after editing any module under
# atem_control/js/ — an edit is invisible until bundled:
#
#   docker run --rm -v "$(pwd)":/work -w /work/build/js node:20-alpine sh build.sh
#
# The bundle is committed (same convention as the compiled stylesheet):
# readable IIFE output, no minify, so a bundle diff stays reviewable.
# Never edit the bundle directly — the banner says so too.
set -e
npm install --no-fund --no-audit
npx esbuild ../../atem_control/js/main.js \
    --bundle --format=iife --target=es2020 --charset=utf8 \
    --banner:js='/* GENERATED FILE — do not edit. Source: atem_control/js/ ; rebuild: build/js/build.sh */' \
    --outfile=../../atem_control/static/js/atem_control.js
wc -c ../../atem_control/static/js/atem_control.js
