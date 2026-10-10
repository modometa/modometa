#!/bin/sh
set -e

DATABASE_PATH="${DATABASE_PATH:-/app/modometa.db}"
KNN_INDEX_PATH="${KNN_INDEX_PATH:-/app/data/knn_index.npz}"
SEARCH_INDEX_PATH="${SEARCH_INDEX_PATH:-/app/data/search_index.json}"
DOWNLOAD_URL="${R2_PUBLIC_URL:-${DATA_DOWNLOAD_URL:-https://data.modometa.com}}"

# If arguments were passed to docker run (e.g. bash, custom command), execute them directly
if [ "$#" -gt 0 ]; then
    exec "$@"
fi

# Ensure directories exist
mkdir -p "$(dirname "$DATABASE_PATH")"
mkdir -p "$(dirname "$KNN_INDEX_PATH")"
mkdir -p "$(dirname "$SEARCH_INDEX_PATH")"

if [ -n "$DOWNLOAD_URL" ]; then
    # Strip trailing slash if present
    DOWNLOAD_URL="${DOWNLOAD_URL%/}"

    # Fetch SQLite database if not present locally
    if [ ! -f "$DATABASE_PATH" ]; then
        echo "[entrypoint] Database not found at $DATABASE_PATH. Attempting download from $DOWNLOAD_URL..."

        # Prefer uncompressed database to avoid CPU spikes and memory writeback stalls on boot
        if curl -fsSL -I "${DOWNLOAD_URL}/modometa.db" >/dev/null 2>&1; then
            echo "[entrypoint] Downloading uncompressed modometa.db..."
            if curl -fSL --progress-bar -o "$DATABASE_PATH" "${DOWNLOAD_URL}/modometa.db"; then
                echo "[entrypoint] Database downloaded successfully."
            else
                echo "[entrypoint] Warning: Failed to download uncompressed modometa.db"
                rm -f "$DATABASE_PATH"
            fi
        elif curl -fsSL -I "${DOWNLOAD_URL}/modometa.db.zst" >/dev/null 2>&1; then
            echo "[entrypoint] Downloading and decompressing modometa.db.zst..."
            if curl -fSL --progress-bar -o "${DATABASE_PATH}.zst" "${DOWNLOAD_URL}/modometa.db.zst"; then
                zstd -d --rm -f "${DATABASE_PATH}.zst" -o "$DATABASE_PATH"
                echo "[entrypoint] Database decompressed successfully."
            else
                echo "[entrypoint] Warning: Failed to download modometa.db.zst"
                rm -f "${DATABASE_PATH}.zst"
            fi
        else
            echo "[entrypoint] Database file not found at $DOWNLOAD_URL"
        fi
    else
        echo "[entrypoint] Database found at $DATABASE_PATH."
    fi

    # Fetch kNN index if not present locally
    if [ ! -f "$KNN_INDEX_PATH" ]; then
        echo "[entrypoint] kNN index not found at $KNN_INDEX_PATH. Checking remote..."
        if curl -fsSL -I "${DOWNLOAD_URL}/knn_index.npz" >/dev/null 2>&1; then
            echo "[entrypoint] Downloading knn_index.npz..."
            curl -fSL --progress-bar -o "$KNN_INDEX_PATH" "${DOWNLOAD_URL}/knn_index.npz" || rm -f "$KNN_INDEX_PATH"
        elif curl -fsSL -I "${DOWNLOAD_URL}/knn_index.npz.zst" >/dev/null 2>&1; then
            echo "[entrypoint] Downloading and decompressing knn_index.npz.zst..."
            if curl -fSL --progress-bar -o "${KNN_INDEX_PATH}.zst" "${DOWNLOAD_URL}/knn_index.npz.zst"; then
                zstd -d --rm -f "${KNN_INDEX_PATH}.zst" -o "$KNN_INDEX_PATH"
            else
                rm -f "${KNN_INDEX_PATH}.zst"
            fi
        else
            echo "[entrypoint] Notice: No remote kNN index found. Deck similarity queries will fall back to rule-based matches."
        fi
    else
        echo "[entrypoint] kNN index found at $KNN_INDEX_PATH."
    fi
    # Fetch search index if not present locally
    if [ ! -f "$SEARCH_INDEX_PATH" ]; then
        echo "[entrypoint] Search index not found at $SEARCH_INDEX_PATH. Checking remote..."
        if curl -fsSL -I "${DOWNLOAD_URL}/search_index.json" >/dev/null 2>&1; then
            echo "[entrypoint] Downloading search_index.json..."
            curl -fSL --progress-bar -o "$SEARCH_INDEX_PATH" "${DOWNLOAD_URL}/search_index.json" || rm -f "$SEARCH_INDEX_PATH"
        elif curl -fsSL -I "${DOWNLOAD_URL}/search_index.json.zst" >/dev/null 2>&1; then
            echo "[entrypoint] Downloading and decompressing search_index.json.zst..."
            if curl -fSL --progress-bar -o "${SEARCH_INDEX_PATH}.zst" "${DOWNLOAD_URL}/search_index.json.zst"; then
                zstd -d --rm -f "${SEARCH_INDEX_PATH}.zst" -o "$SEARCH_INDEX_PATH"
            else
                rm -f "${SEARCH_INDEX_PATH}.zst"
            fi
        else
            echo "[entrypoint] Notice: No remote search index found. Will build or compute on-demand."
        fi
    else
        echo "[entrypoint] Search index found at $SEARCH_INDEX_PATH."
    fi
fi

# Fallback: if database still does not exist, we'll create an empty one
if [ ! -f "$DATABASE_PATH" ]; then
    echo "[entrypoint] Warning: No database found at $DATABASE_PATH. Running migrations to initialize an empty database..."
fi

# Run migrations always
# If the DB doesn't exist, this will create it
# If the DB *does* exist, this will run any migrations that haven't been run yet
modometa migrate --no-input

# Precompute static search index if missing and database exists
if [ ! -f "$SEARCH_INDEX_PATH" ] && [ -f "$DATABASE_PATH" ]; then
    echo "[entrypoint] Building precomputed search index..."
    modometa build_search_index || echo "[entrypoint] Warning: Failed to precompute search index; will compute on-demand."
fi

PORT="${PORT:-8000}"
GUNICORN_WORKERS="${GUNICORN_WORKERS:-1}"
GUNICORN_THREADS="${GUNICORN_THREADS:-4}"
GUNICORN_TIMEOUT="${GUNICORN_TIMEOUT:-30}"
GUNICORN_BACKLOG="${GUNICORN_BACKLOG:-128}"
GUNICORN_MAX_REQUESTS="${GUNICORN_MAX_REQUESTS:-1000}"
GUNICORN_MAX_REQUESTS_JITTER="${GUNICORN_MAX_REQUESTS_JITTER:-100}"

# Enable immutable read-only SQLite mode for web serving
export DATABASE_READ_ONLY="${DATABASE_READ_ONLY:-true}"

echo "[entrypoint] Starting Gunicorn on port $PORT (workers=$GUNICORN_WORKERS, threads=$GUNICORN_THREADS, timeout=$GUNICORN_TIMEOUT, backlog=$GUNICORN_BACKLOG, read_only=$DATABASE_READ_ONLY)..."

exec gunicorn config.wsgi:application \
    --bind "0.0.0.0:${PORT}" \
    --workers "${GUNICORN_WORKERS}" \
    --threads "${GUNICORN_THREADS}" \
    --timeout "${GUNICORN_TIMEOUT}" \
    --backlog "${GUNICORN_BACKLOG}" \
    --max-requests "${GUNICORN_MAX_REQUESTS}" \
    --max-requests-jitter "${GUNICORN_MAX_REQUESTS_JITTER}" \
    --access-logfile - \
    --error-logfile - \
    --log-level info
