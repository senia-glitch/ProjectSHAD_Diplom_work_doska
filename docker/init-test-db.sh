#!/bin/sh
set -eu
if [ "$POSTGRES_TEST_DB" = "$POSTGRES_DB" ]; then
    echo 'Test database must differ from application database' >&2
    exit 1
fi
# psql variables quote arbitrary identifiers correctly; passwords are never printed.
psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" --set=test_db="$POSTGRES_TEST_DB" <<'SQL'
CREATE DATABASE :"test_db";
SQL
