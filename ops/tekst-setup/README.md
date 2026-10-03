# TEKST node deployment

Configured 2026-10-03 for the owner's request to replicate the working Estonia transports.

- VPS: `87.251.16.170`; domain: `tekst.site`, `www.tekst.site`.
- CDN: `cdn.tekst.site`, created separately by the owner in VK Cloud.
- Node: `node-tekst-16.170`, UUID `dae63e17-3258-488d-ba4c-62858fe81df4`.
- Profile: `VK_CDN_TEKST`, UUID `fa9617b8-8025-4e1c-a28e-c51f639c57fc`.
- Source profile: `VK_CDN_PRESTIZH`; fresh REALITY keys and CDN path were generated.
- Remnawave Node pinned to `3.4.1`, Xray `26.7.28`, matching the inspected source node.

## Services

| Transport | Public endpoint | Internal listener |
| --- | --- | --- |
| CDN XHTTP | cdn.tekst.site:443 | 127.0.0.1:10085 |
| gRPC REALITY | 87.251.16.170:2083 | :2083 TCP |
| WS TLS | tekst.site:443/direct/ws/ | 127.0.0.1:10088 |
| Hysteria2 TLS | 87.251.16.170:8443 | :8443 UDP |

The three direct hosts are enabled in the same internal squads as the Estonia source.
At this checkpoint the CDN host is disabled and not yet assigned to squads: VK's HTTPS
edge returns a TLS internal-error alert before presenting its certificate.
The owner was asked for the certificate status in VK Cloud. No VK settings were changed.

The origin supports both HTTP and HTTPS, with `Host: tekst.site`, so the configured
VK origin protocol can use either. CDN client SNI and Host are `cdn.tekst.site`.

## Verified

- Profile accepted by Xray's configuration validation.
- Remnawave node connected, four listeners started, no panel status error.
- Main and www site return HTTPS 200 with a valid Let's Encrypt certificate.
- External WS upgrade returns 101 with the correct WebSocket acceptance value.
- gRPC REALITY fallback presents the valid origin certificate over TLS 1.3 with ALPN h2.
- Hysteria2 UDP listener is bound to 8443.
- HTTP CDN returns the exact site/SVG files and forwards the XHTTP path to Xray
  (400 for an intentionally incomplete protocol request, with Cache-Control: no-store).
- `certbot renew --dry-run --cert-name tekst.site` succeeded.

These are infrastructure checks without using a subscriber's credential. Real app
connectivity still needs the owner's test after refreshing the subscription.

## Operations

Remote files:

- `/opt/remnanode/docker-compose.yml` and private `/opt/remnanode/node.env`.
- `/etc/nginx/sites-available/tekst.site`.
- `/var/www/tekst/` static site.
- `/etc/letsencrypt/renewal-hooks/deploy/tekst-services` reloads nginx and restarts
  this node after certificate renewal. `certbot.timer` is enabled.
- `/root/tekst-setup/` contains the uploaded configuration and original nginx default link.

UFW allows SSH 22, HTTP 80, HTTPS 443, gRPC 2083 TCP and Hysteria2 8443 UDP.
Node API port 2222 is allowed only from panel address `31.77.202.66`.

Private local snapshots and generated secrets are in `/private/tmp/wayspn-tekst-setup/`
(directory mode 0700, secret files 0600), outside the repository. The script reads
the existing Remnawave API credentials from the project's environment without printing them.

After VK HTTPS works, use `python3 ops/tekst-setup/manage.py activate` to assign the
CDN inbound to the corresponding Estonia squads and enable its already-created host.
`status` reads the current connection and host state. Creation is idempotent and
refuses to overwrite a differing existing target profile.
