#!/usr/bin/env bash
set -euo pipefail
umask 077
source_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
install_dir="$HOME/.local/lib/firecrawl-security-refresh/deployment"
units="$HOME/.config/systemd/user"
state="$HOME/.local/state/firecrawl-security-refresh"
mkdir -p "$install_dir/self-hosted-auto" "$install_dir/firecrawl-updater" "$units" "$state" "$HOME/.local/bin"
chmod 700 "$state"
systemctl --user stop firecrawl-update.timer
if systemctl --user is-active --quiet firecrawl-update.service; then
  echo 'Updater is running; wait for its transaction to finish before installation.' >&2
  exit 1
fi
for name in security_refresh.py; do
  install -m 600 "$source_dir/$name" "$install_dir/self-hosted-auto/$name"
done
for name in advisory_owner.py advisory_trivy.py notify.py; do
  install -m 600 "$source_dir/../firecrawl-updater/$name" "$install_dir/firecrawl-updater/$name"
done
for name in firecrawl-update.service firecrawl-update.timer firecrawl-security-recover.service firecrawl-security-recover.timer firecrawl-security-notify.service firecrawl-security-notify.timer; do
  if [[ -f "$units/$name" ]]; then
    digest=$(sha256sum "$units/$name"); digest=${digest%% *}
    cp -p "$units/$name" "$state/$name.$digest.before-install"
  fi
  install -m 600 "$source_dir/$name" "$units/$name"
done
launcher="$HOME/.local/bin/firecrawl-update"
if [[ -f "$launcher" ]]; then
  digest=$(sha256sum "$launcher"); digest=${digest%% *}
  install -m 600 "$launcher" "$state/previous-updater-$digest.sh"
fi
temporary=$(mktemp "$HOME/.local/bin/.firecrawl-update.XXXXXXXX")
printf '%s\n' '#!/bin/sh' 'exec /usr/bin/python3 -B "$HOME/.local/lib/firecrawl-security-refresh/deployment/self-hosted-auto/security_refresh.py" "$@"' >"$temporary"
chmod 700 "$temporary"
mv "$temporary" "$launcher"
systemctl --user daemon-reload
systemctl --user enable --now firecrawl-security-recover.timer firecrawl-security-notify.timer
# Keep boot recovery enabled, but avoid a simultaneous initial timer activation
# competing for the owner lock during the mandatory first update.
systemctl --user stop firecrawl-security-recover.timer
systemctl --user start firecrawl-security-recover.service
# Start the new owner once and require success before restoring its recurring timer.
systemctl --user start firecrawl-update.service
systemctl --user start firecrawl-security-recover.timer
systemctl --user enable --now firecrawl-update.timer
