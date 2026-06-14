#!/usr/bin/with-contenv bashio

bashio::log.info "Starting HA EMS on port 8099"

exec python3 -m uvicorn app.main:app \
    --host 0.0.0.0 \
    --port 8099
