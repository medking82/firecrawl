#!/bin/sh
# Prepared for post-review use only. Does not enable or start the timer.
set -eu
umask 077
src=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
lib="$HOME/.local/lib/firecrawl-advisory"
units="$HOME/.config/systemd/user"
mkdir -p "$lib" "$units"
for file in advisory_owner.py advisory_trivy.py; do
  if [ -e "$lib/$file" ] || [ -L "$lib/$file" ]; then echo "Refusing to overwrite $lib/$file" >&2; exit 1; fi
done
for file in firecrawl-advisory.service firecrawl-advisory.timer; do
  if [ -e "$units/$file" ] || [ -L "$units/$file" ]; then echo "Refusing to overwrite $units/$file" >&2; exit 1; fi
done
for file in advisory_owner.py advisory_trivy.py; do install -m 600 "$src/$file" "$lib/$file"; done
for file in firecrawl-advisory.service firecrawl-advisory.timer; do install -m 600 "$src/$file" "$units/$file"; done
echo 'Files installed; review them before daemon-reload or enabling the timer.'
