#!/usr/bin/env bash
set -e
cd "$(dirname "$0")"
# Optional local settings (e.g. CLIPFORGE_PROXY=http://127.0.0.1:18081 on a
# server whose IP YouTube refuses). Never committed — see .gitignore.
if [ -f .env ]; then
  set -a
  . ./.env
  set +a
fi
if [ ! -x .venv/bin/python ]; then
  python3 -m venv --system-site-packages .venv
  .venv/bin/pip install -r requirements.txt
fi
exec .venv/bin/python -m app
