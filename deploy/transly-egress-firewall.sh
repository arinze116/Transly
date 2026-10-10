#!/usr/bin/env bash
set -euo pipefail

# Install/run as root. The downloader must run as the dedicated "transly"
# Unix account. This limits the rules to that process identity, not the whole VPS.
if [[ "${EUID}" -ne 0 ]]; then
  echo "Run as root: sudo $0" >&2
  exit 1
fi
if ! id transly >/dev/null 2>&1; then
  echo "Create a dedicated 'transly' system user and run the bot as that user first." >&2
  exit 1
fi
if ! command -v nft >/dev/null 2>&1; then
  echo "nftables is required (install the nftables package)." >&2
  exit 1
fi

uid="$(id -u transly)"

# DNS may use the local systemd-resolved stub. This narrow exception is added
# before loopback is blocked; all other local/private destinations are denied.
nft delete table inet transly_egress 2>/dev/null || true
nft add table inet transly_egress
nft 'add chain inet transly_egress output { type filter hook output priority filter; policy accept; }'

nft add rule inet transly_egress output meta skuid "$uid" ip daddr 127.0.0.53 udp dport 53 accept
nft add rule inet transly_egress output meta skuid "$uid" ip daddr 127.0.0.53 tcp dport 53 accept
nft add rule inet transly_egress output meta skuid "$uid" ip6 daddr ::1 udp dport 53 accept
nft add rule inet transly_egress output meta skuid "$uid" ip6 daddr ::1 tcp dport 53 accept

# IPv4 special-use, private, loopback, link-local (including cloud metadata),
# documentation, benchmarking, multicast, and reserved ranges.
nft add rule inet transly_egress output meta skuid "$uid" ip daddr {
  0.0.0.0/8, 10.0.0.0/8, 100.64.0.0/10, 127.0.0.0/8,
  169.254.0.0/16, 172.16.0.0/12, 192.0.0.0/24, 192.0.2.0/24,
  192.88.99.0/24, 192.168.0.0/16, 198.18.0.0/15, 198.51.100.0/24,
  203.0.113.0/24, 224.0.0.0/4, 240.0.0.0/4
} reject

# IPv6 local, mapped-IPv4, local-use translation, discard, documentation,
# unique-local, link-local, and multicast ranges.
nft add rule inet transly_egress output meta skuid "$uid" ip6 daddr {
  ::/128, ::1/128, ::ffff:0:0/96, 64:ff9b::/96, 64:ff9b:1::/48,
  100::/64, 2001:db8::/32, fc00::/7, fe80::/10, ff00::/8
} reject

echo "SSRF egress restrictions installed for UID $uid (transly)."
