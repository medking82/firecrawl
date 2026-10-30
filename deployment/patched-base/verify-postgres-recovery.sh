#!/usr/bin/env bash
# Read-only logical dumps from named production databases; restore only to an
# isolated, disposable in-memory database. Never attach a production volume.
set -Eeuo pipefail
image=${1:?provide immutable recovery image ID}
[[ "$image" =~ ^sha256:[0-9a-f]{64}$ ]] || exit 2
id=''
cleanup() { if [[ "$id" =~ ^[0-9a-f]{64}$ ]]; then docker rm -f "$id" >/dev/null; fi; }
trap cleanup EXIT
for pair in firecrawl-nuq-postgres-1:postgres firecrawl-monitor-app-db-1:postgres firecrawl-monitor-app-db-1:monitor firecrawl-browser-app-db-1:postgres firecrawl-browser-app-db-1:browser; do
  source=${pair%%:*}; db=${pair#*:}
  id=$(docker run -d --network none --tmpfs /var/lib/postgresql/data:rw,size=512m -e POSTGRES_HOST_AUTH_METHOD=trust "$image")
  [[ "$id" =~ ^[0-9a-f]{64}$ ]] || exit 3
  for n in {1..60}; do docker exec "$id" pg_isready -U postgres -d postgres >/dev/null 2>&1 && break; sleep 1; done
  docker exec "$id" pg_isready -U postgres -d postgres >/dev/null
  if [[ "$db" != postgres ]]; then docker exec "$id" createdb -U postgres "$db"; fi
  docker exec "$source" pg_dump -U postgres -Fc "$db" | docker exec -i "$id" pg_restore -U postgres -d "$db" --clean --if-exists --no-owner --no-privileges --exit-on-error
  docker exec "$id" psql -X -U postgres -d "$db" -v ON_ERROR_STOP=1 -At -c 'SELECT count(*) FROM pg_class' >/dev/null
  printf 'restore passed: %s / %s\n' "$source" "$db"
  cleanup; id=''
done
