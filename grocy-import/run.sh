#!/usr/bin/with-contenv bashio
set -e

export GROCY_URL=$(bashio::config 'grocy_url')
export GROCY_API_KEY=$(bashio::config 'grocy_api_key')

bashio::log.info "Starting Grocy Import on port 8099..."

exec python3 /app/app.py
