# DE1 XHTTP CDN origin — local preparation

Approved origin: `64.188.83.197`, `de1.chislo.site`. This is a separate XHTTP
variant; the earlier `ops/de1-ws-cdn` bundle is untouched. No RU cascade,
subscriber credentials, panel node/hosts or Timeweb CDN resource is created.

`python3 ops/de1-xhttp-cdn/prepare.py prepare` exclusively creates
`/private/tmp/wayspn-de1-xhttp-cdn` (0700; files 0600). It generates a private
random `/edge/…/` path, renders the Nginx and empty-clients VLESS/XHTTP profile,
exports matching client extras and copies the existing CHISLO static gallery.
Do not commit or publicly expose the resulting bundle.

Copy the bundle privately to the approved new VPS preserving modes, then run its
scripts with `bash` as root only after review. `install.sh` requires Ubuntu 24.04,
the approved local IPv4, no existing Docker/Nginx/TLS deployment and no occupied
service ports. It installs from Ubuntu APT origins without an OS upgrade. Existing
active UFW/OpenSSH rules are preserved; only TCP 80/443 is added. If UFW is not
active, the initial allowlist is TCP 22/80/443. TCP 2222 is not opened.

`finish.sh` checks A/AAAA records using two public DNS resolvers, obtains the
origin certificate and enables TLS 1.2/1.3, running `nginx -t` before reload.
Certbot renewal dry-run is a separate reviewed validation step.

Nginx forwards only the private edge prefix to `127.0.0.1:10085`, with request/
response buffering and cache disabled and 3600-second proxy timeouts. The profile
uses packet-up GET/header transport, `X-Transport-Data` and a 2048-byte uplink
limit matching the approved DE11 baseline. No Node/container is started. Until a
separately reviewed temporary Xray probe or node is running, this route has no
listening backend. The verified temporary image is `remnawave/node:3.4.1`, Xray
binary `/usr/local/bin/xray`; no subscriber credentials are needed for HTTP-route
checks. CDN provider availability/settings still require independent validation.

Local checks: `python3 -m unittest discover -s ops/de1-xhttp-cdn -p 'test_*.py'`
and `bash -n ops/de1-xhttp-cdn/{install,finish,renew-nginx}.sh`.
