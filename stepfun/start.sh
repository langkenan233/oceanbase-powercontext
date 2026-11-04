#!/usr/bin/env sh
set -e
cd "$(dirname "$0")"
[ -d node_modules ] || npm install
exec node server/index.mjs
