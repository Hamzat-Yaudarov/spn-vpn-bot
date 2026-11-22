# Finland: one additive Yandex XHTTP transport

Fixed origin `92.42.102.103` / `fi103.wayspn.com`; fixed CDN name
`cdn-fi103.wayspn.com`. These scripts do not create a CDN resource, edit DNS,
obtain a certificate, upload files, or change any existing direct key.

`manage.py` defaults to a pure plan. Finish and audit `ops/direct-fi` first.
Then capture a fresh infrastructure-only snapshot into a local private directory
(0700 directory, 0600 regular file). No users or subscriptions are needed.

Prepare locally, with no network calls:

```text
python ops/yandex-fi/manage.py prepare --snapshot /private/tmp/PRIVATE/snapshot.json \
  --direct-state /private/tmp/wayspn-direct-fi-20261030 \
  --state /private/tmp/PRIVATE/operation
```

Preparation validates the direct creation records, exact fixed IP/domain,
newly returned UUIDs and the fresh snapshot. It freezes these private records
and candidate hashes. It copies only the known DE11 XHTTP transport and appends
one inbound, preserving the direct profile and every other profile.

Each live panel stage is separate and requires `--confirm`:
`apply-profile`, `bind-node`, `create-host`, `bind-squads`, then
`enable --tls-verified`. `audit` performs only GETs. Every write saves its exact
intent before sending; a timeout must be audited, never blindly retried.
`create-host` creates a disabled host. The last stage additionally requires a
connected node, source-mirrored squad access and explicit verified TLS.

`origin_patch.py` defaults to a pure plan. Its explicit `--apply
--confirm-target 92.42.102.103` mode is for the approved VPS only. It accepts
only the exact reviewed direct-fi Nginx template (SHA256 pinned in source), adds
the XHTTP location and 8192-byte header field buffers, and retains an exclusive
root-private backup. It tests/reloads Nginx and restores the exact original on
failure. WS, gRPC, certificates, firewall and all other vhosts are untouched.

Synthetic local tests (no API, VPS, CDN or credentials):

```text
python -B -m unittest discover -s ops/yandex-fi -p 'test_*.py'
```
