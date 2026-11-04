#!/usr/bin/env sh
set -e
cd "$(dirname "$0")"
[ -d node_modules ] || npm install --legacy-peer-deps
exec node server/index.mjs
