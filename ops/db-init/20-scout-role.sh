#!/bin/sh
# Runs once, when the db volume is first created. Creates the login role Scout
# uses. It owns nothing; migration 0002 grants it read on sources, insert on
# signals and runs, and update on a few run columns only.
#
# An existing volume skips this script. There, migration 0002 creates the role
# NOLOGIN; give it a login once with:
#   ALTER ROLE jokr_scout LOGIN PASSWORD '<JOKR_SCOUT_PASSWORD>';
set -eu
psql -v ON_ERROR_STOP=1 -v scout_password="$JOKR_SCOUT_PASSWORD" \
    --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" <<'SQL'
CREATE ROLE jokr_scout LOGIN PASSWORD :'scout_password';
SQL
