# CHISLO deployment

Target: `31.76.50.87`, origin `chislo.site` / `www.chislo.site`, CDN `cdn.chislo.site`.
The owner created DNS and the VK resource. Initially the panel setup was manual;
the owner subsequently authorized creating the dedicated CHISLO panel objects.
`prepare.py` is GET-only; `manage.py` creates the profile/node/hosts and activates
only the owner's existing DE test squad. No public rollout is performed.

`prepare.py` makes a copy of the working `VK_CDN_TEKST` profile with exactly four
inbounds and fresh REALITY keys, Short ID and CDN path. It only uses GET requests.
Secrets are generated into `/private/tmp/wayspn-chislo-setup/` (0700, files 0600),
with private handoff copies in Git-ignored `private/`. Keep `profile.json` and
`node.env` private. No secret files are tracked in Git.

`install.sh` deploys an HTTP bootstrap site and firewall and downloads the pinned
Remnawave Node 3.4.1 image. `finish.sh` refuses to issue a certificate until both
origin names resolve to the VPS, then installs TLS and starts the node API.
Do not use these scripts on a different or existing production VPS.

Services: CDN XHTTP → nginx → localhost:10085; direct WS TLS → nginx → localhost:10088;
gRPC REALITY TCP:2083; Hysteria2 TLS UDP:8443. Node API TCP:2222 is restricted to
the verified panel IP `31.77.202.66`. Certificates are mounted read-only into
the node. Certbot's deploy hook reloads nginx and restarts the node on renewal.

The static site is an actual, self-contained geometric art gallery. It has no
external scripts, trackers, forms, fabricated companies or claims of ban immunity.
It does not change what the CDN provider can observe about proxied traffic.

## Verified 2026-10-06

- Ubuntu packages installed; pinned image reports Node 3.4.1 / Xray 26.7.28.
- Origin/www trusted HTTPS certificate issued, expires 2027-01-04.
- Certbot renewal dry-run succeeded; timer and deploy hook enabled.
- Xray validates the four-inbound profile (no subscriber credentials).
- Temporary empty-client probe confirmed WS 101 with exact acceptance, gRPC REALITY
  fallback TLS 1.3/h2, origin XHTTP incomplete-request 400/no-store and Hysteria2 UDP binding.
- Site passed desktop/mobile Chrome layout, images, www TLS and 404 checks.
- UFW INPUT default-deny; TCP:2222 allows only panel source 31.77.202.66.
- CDN edge HTTPS now verifies its own certificate and responds over HTTP/2.
  An intentionally incomplete XHTTP request returns 400, Cache-Control no-store,
  Cache MISS. Static gallery files are subject to the owner's VK cache settings.
- Temporary empty-client probe container removed before creating the real node.
- Dedicated VK_CDN_CHISLO profile/node/four hosts created after explicit authorization.
  The panel reports Connected, Node 3.4.1 / Xray 26.7.28, no lastStatusMessage.
  All four hosts enabled only through new inbounds added to the DE test squad.

Manual-panel handoff copies are in `private/` (Git-ignored, 0700/0600).
The owner confirmed that all four connections work in their app test.
Public rollout remains unperformed: only the DE test squad has the new inbounds.
`manage.py audit` compares existing profile configs, host objects and node bindings
with private pre-change snapshots. No subscriber credentials are copied or used.

Xray warns that gRPC/WS are deprecated and REALITY has a default minimum client
core version 26.3.27. Keep the existing working transport choices as requested;
use current Happ/INCY versions rather than silently downgrading transport security.
