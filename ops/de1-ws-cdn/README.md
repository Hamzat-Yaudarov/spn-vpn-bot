# DE1 WS CDN origin — preparation only

Approved origin: `64.188.83.197`, `de1.chislo.site`. No RU cascade, panel node, hosts,
subscriber credentials or CDN-provider configuration is created here.

Run `python3 ops/de1-ws-cdn/prepare.py prepare` locally. It exclusively creates
`/private/tmp/wayspn-de1-ws-cdn` (0700, files 0600), generates a private random WS
path, renders `nginx.conf` / `configprofile.json` and copies the existing CHISLO
gallery. Do not commit or publicly expose this bundle. Copy it privately to the
new VPS, preserving modes, then run its scripts through `bash` as root.

`install.sh` refuses anything other than clean Ubuntu 24.04, existing Docker,
Nginx/TLS deployment paths or occupied service ports. Existing active UFW rules
(including the preverified OpenSSH/22 rule) are preserved; there is no reset.
It installs packages only from Ubuntu APT origins (no OS upgrade), serves HTTP
ACME/static content and adds only TCP 80/443 to the existing firewall. On a host
without active UFW it initializes the policy with TCP 22/80/443 only.

`finish.sh` checks A/AAAA records through two public resolvers before first
certificate issuance, enables TLS 1.2/1.3 and tests Nginx before reloading. Run
Certbot renewal dry-run separately during the reviewed server validation.

The WS route only forwards to `127.0.0.1:10088`. `configprofile.json` is an empty-
clients standalone VLESS/WS probe configuration, not a deployed service. No
container is started; a reviewed temporary runtime (pinned `remnawave/node:3.4.1`,
binary `/usr/local/bin/xray`) may be used separately to validate the upgrade. Until then the WS
route has no listening backend. There is no public port 10088 or 2222 rule. A
later authorized node phase must restrict TCP 2222 to panel `144.31.170.147`.

Local checks: `python3 -m unittest discover -s ops/de1-ws-cdn -p 'test_*.py'` and
`bash -n ops/de1-ws-cdn/{install,finish,renew-nginx}.sh`.
