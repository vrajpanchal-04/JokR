#!/bin/sh
# Runs once, when the db volume is first created. Creates the login role the
# API uses. It owns nothing; migration 0001 grants it SELECT and INSERT only.
set -eu
psql -v ON_ERROR_STOP=1 -v app_password="$JOKR_APP_PASSWORD" \
    --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" <<'SQL'
CREATE ROLE jokr_app LOGIN PASSWORD :'app_password';
SQL
