# DE1 Timeweb XHTTP pilot — 2026-10-17

This is a separate pilot, not a migration of an existing customer host.

- VPS: `64.188.83.197`, Ubuntu 24.04; origin: `de1.chislo.site`.
- Timeweb resource `43833`, `de1-chislo-xhttp`, was created by the user.
- User-confirmed delivery hostname: `1sd2ljvb74.cdn.twcstorage.ru`.
  No custom delivery CNAME or additional paid CDN was created.
- Profile `DE1_XHTTP_TIMEWEB`: `6290e311-62cf-4a43-8d7d-e55561790a96`.
- Inbound `vless-xhttp-de1-timeweb`: `7bfa1220-bee5-4228-82e9-e05b82de518c`.
- Node `node-de1-xhttp-timeweb`: `f87b14cf-438a-4c61-acc1-7522491c05ae`.
- Host `DE1 • Timeweb XHTTP • TEST`: `9a0fba69-39fc-4849-8f67-9b273a94af32`.

## Verified VPS/panel state

Origin HTTPS returns 200 with normal certificate validation. The origin
certificate expires 2027-01-15; Certbot renewal dry-run passed and the deploy
hook reloads Nginx. Static assets use relative URLs.

Nginx terminates TLS and forwards only the new private transport prefix to
`127.0.0.1:10085`, with request/response buffering and caching disabled. The
empty-clients profile passed Xray's configuration test. A deliberately incomplete
request to the origin transport returns 400 with `Cache-Control: no-store`.
This does not verify authenticated VPN connectivity.

The node is running the reviewed `remnawave/node:3.4.1` image, pinned to digest
`sha256:0cdf386dd49f360fc885bb34bde21132e478e40f0deac62d616086ec0fa9257e`.
Panel reports Connected, Node 3.4.1 / Xray 26.7.28, no last-status error. Container
restart count was zero. TCP 2222 is allowed only from panel IP `144.31.170.147`;
the transport listener is loopback-only. Public ports remain SSH/HTTP/HTTPS.

The new host is **disabled** and has ALLOW_ONLY access for the existing DE test
squad. Its inbound has **not** been appended to DE yet. No existing profile,
host, node binding or squad was changed; fresh API audit passed. Existing
auto-selection templates use explicit UUID selectors, not a wildcard that would
automatically adopt this new host.

## CDN validation still pending

The technical CNAME began publishing during provisioning. Recursive DNS answers
were temporarily inconsistent. CDN TLS validation succeeded when probing a
published edge IP with the correct SNI, but `/health` returned **403** with
`x-cdn-edge-cache: HIT`; one other attempt timed out during TLS. User reported
disabling CDN/browser caching, Always Online, gzip and large-file optimization.
Application of those settings and the actual origin request still need checking.

Later check: the CDN still returned 403, now explicitly with `X-Reason-Code: 7`.
The origin had no additional `/health` hits and zero TLS rejection/handshake or
upstream errors in its dedicated Nginx error log. The node remained running
with zero restarts. User screenshots showed Secure Token, AWS authorization,
custom request headers, response-header hiding, CORS, HTTP/3, gzip and file
segmentation disabled. Those visible controls do not explain the rejection.
The screenshot still showed settings applying; current resource activation
needs confirmation. No public Timeweb/CDNvideo documentation was found that
defines reason 7. A similar Selectel FAQ is not proof of Timeweb's root cause.

Subsequent read-only panel check detected changes outside this helper's saved
activation flow: the new host was enabled and its inbound was present in DE.
The helper's normal audit correctly stopped on the unexpected `isDisabled`
change; no subsequent panel mutation or manifest re-baselining was performed.
The node still reported Connected/no error; all baseline profiles, node bindings
and hosts remained unchanged. Do not overwrite this newer panel state from the
original disabled-host plan.

User subsequently confirmed that Timeweb already reports Active. A controlled
comparison on the same published edge IP (`185.31.114.248`, edge ID 2020), with
normal TLS validation and proxies disabled, produced:

- `/assets/site.css`: 200, MISS, `Cache-Control: max-age=604800`.
- `/health`: 403, HIT, `X-Reason-Code: 7`.
- The deliberately incomplete private XHTTP request: 403, HIT, reason 7.

The same CSS and health responses are 200 directly at the origin; direct
incomplete XHTTP is 400/no-store. This demonstrates working CDN-to-origin static
delivery and a reproducible difference for these non-cacheable/dynamic responses,
not a general TLS/DNS/VPS failure. The internal reason-code meaning remains
unconfirmed by Timeweb. No attempt was made to mark tunnel data cacheable or
override its no-store response as a workaround. Provider clarification of the
supported dynamic, non-caching/streaming scenario is the next prerequisite.

## Later custom-domain and certificate state

The user changed the new host's address/SNI/HTTP Host to `cdnde1.chislo.site` and
created its correct CNAME to the technical Timeweb domain. Initial TLS on that
custom name failed hostname verification. After the user's approval, a separate
manual DNS-01 certificate was issued on the VPS with cert-name
`cdnde1.chislo.site`; it did not replace the `de1.chislo.site` origin certificate.
Only its fullchain/key were copied to an operator-only Mac temporary folder;
their match and 0700/0600 permissions were verified. No key was printed or
uploaded by the assistant. This manual certificate has no automatic renewal.

Timeweb subsequently issued its own different certificate. A live check verified
TLS 1.3, SAN `cdnde1.chislo.site`, expiry `2027-01-15T17:46:35Z`; its fingerprint
does not match the manually issued backup, which is therefore not needed for
the active CDN TLS configuration. Do not overwrite the working provider-managed
certificate with the manual backup merely because the temporary folder is hidden.

At 19:56 UTC, with normal certificate validation:

- Custom-domain `/assets/site.css`: 200/MISS on edge 2024.
- Custom-domain `/health`: 403/HIT, reason 7 on edge 2024.
- Incomplete XHTTP: origin 400, custom CDN 403/HIT, reason 7.

Scoped GET-only panel recheck found the profile exactly matching the prepared
configuration, all server/client XHTTP settings and path consistent, correct
TLS/443/h2/domain and node bindings, Connected/no-error node, and access only in
DE. Original DE inbounds were preserved; no other squad contains this inbound.
The certificate problem is resolved, but authenticated VPN operation through this
CDN is not verified and the observed dynamic-response rejection remains.

Do not enable DE/public access based on the VPS checks alone. Recheck CDN DNS,
certificate, `/health`, and the keyless transport response. A fresh guarded
`enable-DE --confirm --cdn-verified` is only appropriate after those checks pass;
it preserves every old DE inbound and appends only the new one. Actual VPN and
whitelist behavior must then be tested by the user in Happ/INCY.

Private manifests, the random transport path and node credentials are outside
Git in operator-only staging directories. No active subscriber key/config was
read or copied for testing. No claim of provider-ban immunity is made.
