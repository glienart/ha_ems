#!/usr/bin/with-contenv bashio

# Pass ingress path to the app so FastAPI sets the correct root_path
INGRESS_PATH=$(bashio::addon.ingress_path)
export INGRESS_PATH="${INGRESS_PATH}"

bashio::log.info "Starting HA EMS on port 8099 (ingress: ${INGRESS_PATH})"

exec python3 -m uvicorn app.main:app \
    --host 0.0.0.0 \
    --port 8099 \
    --root-path "${INGRESS_PATH}"
