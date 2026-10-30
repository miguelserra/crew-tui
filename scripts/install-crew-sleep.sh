#!/bin/bash
# One-time install: sudo bash scripts/install-crew-sleep.sh
set -euo pipefail

if [ "$(id -u)" -ne 0 ]; then
    echo "run me as root: sudo bash $0" >&2
    exit 1
fi

here="$(cd "$(dirname "$0")" && pwd)"
user="${SUDO_USER:-miguel}"

install -o root -g root -m 0755 "$here/crew-sleep" /usr/local/sbin/crew-sleep

tmp="$(mktemp)"
printf '%s ALL=(root) NOPASSWD: /usr/local/sbin/crew-sleep\n' "$user" > "$tmp"
chmod 0440 "$tmp"
visudo -cf "$tmp" >/dev/null
install -o root -g root -m 0440 "$tmp" /etc/sudoers.d/crew-sleep
rm -f "$tmp"

echo "instalado: /usr/local/sbin/crew-sleep"
echo "sudoers:   /etc/sudoers.d/crew-sleep (NOPASSWD para $user)"
echo "teste:     sudo -u $user sudo -n /usr/local/sbin/crew-sleep --dry-run --arm $(( $(date -u +%s) + 3600 ))"
