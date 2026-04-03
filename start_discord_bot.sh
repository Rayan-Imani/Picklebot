#!/usr/bin/env sh
set -eu

export PLAYWRIGHT_BROWSERS_PATH="${PLAYWRIGHT_BROWSERS_PATH:-/ms-playwright}"

if [ -n "${DISPLAY:-}" ]; then
  exec python discord_bot.py
fi

exec xvfb-run -a python discord_bot.py