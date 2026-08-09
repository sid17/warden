#!/bin/bash
# Clear the sessions database (SQLite + WAL files)
DB_DIR="$(dirname "$0")/../server/data"
rm -f "$DB_DIR/sessions.db" "$DB_DIR/sessions.db-wal" "$DB_DIR/sessions.db-shm"
echo "Sessions DB cleared. Restart the server or wait for auto-reload."
