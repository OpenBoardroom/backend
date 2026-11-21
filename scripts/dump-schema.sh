#!/bin/sh
# Regenerate db/schema.sql: apply every migration to a throwaway Postgres and
# run `dbmate dump`. pg_dump must match the server's major version, so it runs
# inside the same pinned image CI uses (.github/workflows/ci.yml); the dump's
# header records the exact version, so keep the two tags in step.
set -eu

IMAGE=postgres:17.12
PORT=55432
ROOT=$(cd "$(dirname "$0")/.." && pwd)
SHIM=$(mktemp -d)

# The server listens on the same port inside and outside the container, so
# the URL dbmate passes to pg_dump also works from `docker exec`.
CONTAINER=$(docker run -d --rm -e POSTGRES_PASSWORD=postgres \
    -p "127.0.0.1:$PORT:$PORT" "$IMAGE" -p "$PORT")
trap 'docker stop "$CONTAINER" >/dev/null; rm -rf "$SHIM"' EXIT

until docker exec "$CONTAINER" pg_isready -q -h localhost -p "$PORT"; do sleep 1; done
# The migrations grant to these roles; `infrastructure` creates them in production.
docker exec "$CONTAINER" psql -q -h localhost -p "$PORT" -U postgres \
    -c "create role pipeline_rw; create role web_ro;"

printf '#!/bin/sh\nexec docker exec -i %s pg_dump "$@"\n' "$CONTAINER" > "$SHIM/pg_dump"
chmod +x "$SHIM/pg_dump"

PATH="$SHIM:$PATH" dbmate \
    --url "postgres://postgres:postgres@127.0.0.1:$PORT/postgres?sslmode=disable" \
    --migrations-dir "$ROOT/db/migrations" \
    --schema-file "$ROOT/db/schema.sql" \
    up
