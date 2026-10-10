# Transly outbound-network isolation

## Why this is required

The Python URL preflight is not sufficient by itself: DNS can change between
validation and connection, and yt-dlp performs its own HTTP requests and may
follow redirects. The per-UID nftables rules in this directory are the
network-layer control that prevents the Transly process from reaching private
and special-use address ranges even if application-level validation is bypassed.

## Install on the VPS

This deployment example assumes Ubuntu/Debian, systemd, nftables, a dedicated
`transly` account, and a checkout at `/opt/transly`. Adjust paths if the
checkout lives elsewhere. Do not run the bot as root or under the same Unix
account as unrelated services.

1. Create the account and install system dependencies:

   ```sh
   sudo apt-get update
   sudo apt-get install -y nftables python3-venv ffmpeg
   sudo useradd --system --home-dir /opt/transly --shell /usr/sbin/nologin transly
   sudo install -d -o transly -g transly /opt/transly /tmp/transly
   ```

2. Put the checkout at `/opt/transly`, make the application files owned by
   `transly:transly`, create its virtual environment, and install
   `requirements.txt`. Keep secrets in `/opt/transly/.env` with mode 0600
   and owner `transly:transly`. Do not commit the real `.env`.

3. Check the DNS resolver in `/etc/resolv.conf`. The script permits DNS to
   systemd-resolved at `127.0.0.53` and IPv6 loopback. If the VPS uses a
   different local resolver, add only that resolver's UDP/TCP port 53 rule
   before the loopback deny rule. Do not add a general loopback exception.

4. Install the firewall and service units:

   ```sh
   sudo install -m 0750 -o root -g root deploy/transly-egress-firewall.sh /usr/local/sbin/transly-egress-firewall.sh
   sudo install -m 0644 deploy/transly-egress-firewall.service /etc/systemd/system/
   sudo install -m 0644 deploy/transly.service /etc/systemd/system/
   sudo systemctl daemon-reload
   sudo systemctl enable --now transly-egress-firewall.service
   sudo systemctl enable --now transly.service
   ```

   If Transly is already managed by PM2, stop and disable that instance before
   enabling the systemd service; do not run two bot instances against the same
   Telegram token. The unit expects the virtual environment at
   `/opt/transly/.venv` and the code at `/opt/transly`.

5. Verify the firewall loaded and that the service is running:

   ```sh
   sudo nft list table inet transly_egress
   sudo systemctl status transly-egress-firewall.service transly.service
   sudo -u transly python3 -c 'import socket; print(socket.getaddrinfo("example.com", 443))'
   ```

   DNS resolution should work. Test that outbound connections to loopback,
   RFC1918/private, link-local, and metadata addresses fail from the Transly
   service account. Do not test against a production metadata endpoint if
   that could expose credentials. Review `journalctl -u transly -n 100`.

## Firewall scope and limitations

The rules are scoped to the Unix UID `transly`, so the bot must run under that
account. They reject IPv4 private, loopback, link-local/metadata, reserved,
documentation, benchmarking, and multicast ranges; IPv6 loopback, mapped IPv4,
local-use translation, discard, documentation, unique-local, link-local, and
multicast ranges are rejected as well. Public internet access remains available
for Telegram, media providers, and transcription APIs.

The script replaces only its own `inet transly_egress` table and is safe to
rerun. The firewall service must start before the bot after every reboot. If
the deployment uses a custom DNS resolver, local database, Redis, or other
loopback service, add only the minimum specific destination/port exceptions
required and review the security implications.

This control is defense in depth, not a substitute for URL validation or
keeping yt-dlp, FFmpeg, and Python dependencies patched. CI tests validate
application behavior; they do not prove that the VPS firewall is installed.
