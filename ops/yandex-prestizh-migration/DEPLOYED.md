# PRESTIZH CDN migration — 2026-10-15

Owner authorized migrating only host `645c8877-c1ef-4224-993d-70a31b5cd4e5`
(«Эстония ОБХОД 1») and creating one Yandex CDN resource. The displayed charge
of 150 RUB/month plus tariff-based traffic was separately confirmed.

## Applied

- Yandex folder: `b1gd0s9bmbop5deptg62`.
- CDN resource: `bc8rc4ixgrakhqglkkws`, `cdn.prestizh.site`.
- Origin: `prestizh.site`, HTTPS, explicit origin SNI/Host `prestizh.site`.
- Managed certificate: `cdn-prestizh`, `fpq56rn96j8aihtvvlhi`, issued.
- Reg.ru CNAME `cdn` → `e2a9e51d3ba2edff.topology.gslb.yccdn.ru.`
- Reg.ru CNAME `_acme-challenge.cdn` →
  `fpq56rn96j8aihtvvlhi.cm.yandexcloud.net.` (retain for renewal).
- Existing A records for `@`, `www`, `node29` were preserved.
- CDN/browser caching, gzip, segmentation, cookie ignoring, extra request/response
  headers, token checks, shielding and log exports were not enabled.
- Allowed methods: GET, HEAD, OPTIONS. Client uplink uses headers, not GET bodies.
- Only the selected profile inbound's XHTTP settings and selected host's extras
  changed to the current DE11 GET/header settings. Existing path, host address,
  SNI, ALPN, node links, tags, name and user credentials were preserved.

The node address `217.60.177.12` and origin DNS address `64.188.78.32` are both
assigned to the same VPS interface; this was verified over SSH before migration.
No origin Nginx, firewall, direct gRPC/WS/Hysteria2 configuration, node/squad/user
objects, or other CDN resource was changed.

## Checks completed

- 117 isolated guard/migration tests passed; `git diff --check` passed.
- Target Node 3.4.1 / Xray 26.7.28 accepted the candidate with `Configuration OK`.
  All client lists in this validation-only copy were empty. Temporary validation
  files on the VPS and in the container were removed after the check.
- New edge certificate verifies; HTTP/2 HEAD `/` returns 200 from Yandex CDN.
- An intentionally incomplete XHTTP request to the existing transport path returns
  HTTP/2 400 from the origin transport through Yandex (not 413/403/502).
  This is a keyless reachability probe, not authenticated VPN verification.
- Remnawave reports node connected, no lastStatusMessage, expected versions.
- Fresh GET audit: migration complete; every other profile/config, host object,
  direct inbound and node binding matches the before snapshot.

Private rollback snapshots remain in the root operator's `/private/tmp` staging
directory, outside Git, mode 0700/0600. Use the guarded helper and current live
audit; never overwrite subsequent user changes with a stale snapshot.

## Client confirmation still required

Refresh the existing subscription in Happ/INCY, reconnect to «Эстония ОБХОД 1»
and open a website. The link/keys do not change, but old GET/body client parameters
must be refreshed. Existing DNS caches can temporarily retain VK's old CNAME.
Whitelist/carrier behavior requires a separate real-network test.

No claim of provider-ban immunity is made. Other blocked VK CDN hosts were not
migrated by this operation. No subscriber credentials were fetched or copied.
