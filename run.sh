#!/usr/bin/env bash
set -e
cd "$(dirname "$0")"
if [ ! -x .venv/bin/python ]; then
  python3 -m venv --system-site-packages .venv
  .venv/bin/pip install -r requirements.txt
fi
exec .venv/bin/python -m app
