#!/usr/bin/env bash
set -euo pipefail
cd /home/nicholas/data/bybit-backtest/web
export BYBIT_WEB_DB=/home/nicholas/data/bybit-backtest/bybit_spot_1m.sqlite3
export BYBIT_WEB_JOBS=/home/nicholas/data/bybit-backtest/web/jobs
export BYBIT_WEB_OWNER=nicholaswang180@gmail.com
exec /home/nicholas/data/bybit-backtest/web/venv312/bin/gunicorn \
  --workers 1 --threads 4 --bind 127.0.0.1:18080 \
  --timeout 75 --graceful-timeout 15 \
  --access-logfile /dev/null --error-logfile - \
  app:app
