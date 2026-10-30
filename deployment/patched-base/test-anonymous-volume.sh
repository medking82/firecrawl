#!/usr/bin/env bash
# Test Compose anonymous-volume preservation with a disposable project only.
set -Eeuo pipefail
image=${1:?exact recovery image ID required}
[[ "$image" =~ ^sha256:[0-9a-f]{64}$ ]]
project="fc-volume-test-$(od -An -N8 -tx1 /dev/urandom | tr -d ' \n')"
dir=$(mktemp -d /home/marck/services/firecrawl-security/volume-test.XXXXXXXX)
export image
cat >"$dir/compose.yaml" <<'EOF'
services:
  db:
    image: ${image:?}
    pull_policy: never
    network_mode: none
    environment:
      POSTGRES_HOST_AUTH_METHOD: trust
EOF
compose=(docker compose --project-name "$project" -f "$dir/compose.yaml")
cleanup() { "${compose[@]}" down --volumes --remove-orphans >/dev/null 2>&1 || true; rm -f -- "$dir/compose.yaml"; rmdir -- "$dir"; }
trap cleanup EXIT
"${compose[@]}" up -d --no-build --pull never >/dev/null
id=$("${compose[@]}" ps -q db)
for n in {1..60}; do docker exec "$id" pg_isready -U postgres >/dev/null 2>&1 && break; sleep 1; done
docker exec "$id" psql -X -U postgres -v ON_ERROR_STOP=1 -c "CREATE TABLE recovery_probe(x integer); INSERT INTO recovery_probe VALUES(739);" >/dev/null
before=$(docker inspect "$id" --format '{{range .Mounts}}{{if eq .Destination "/var/lib/postgresql/data"}}{{.Name}}{{end}}{{end}}')
"${compose[@]}" up -d --no-deps --no-build --pull never --force-recreate db >/dev/null
id=$("${compose[@]}" ps -q db)
for n in {1..60}; do docker exec "$id" pg_isready -U postgres >/dev/null 2>&1 && break; sleep 1; done
after=$(docker inspect "$id" --format '{{range .Mounts}}{{if eq .Destination "/var/lib/postgresql/data"}}{{.Name}}{{end}}{{end}}')
value=$(docker exec "$id" psql -X -U postgres -v ON_ERROR_STOP=1 -At -c 'SELECT x FROM recovery_probe')
[[ -n "$before" && "$before" == "$after" && "$value" == 739 ]]
printf 'anonymous volume and database row preserved across recreation\n'
