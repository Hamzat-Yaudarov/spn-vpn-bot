# TEKST direct transport update — 2026-11-06

Current node `dae63e17-3258-488d-ba4c-62858fe81df4` uses IP `64.188.80.39`
and profile `fa9617b8-8025-4e1c-a28e-c51f639c57fc`. The older README's IP
and original deployment script are historical, not safe current deployment commands.

- Added RAW/TCP + REALITY on TCP2443 with independent key/short ID.
- Corrected existing host `ac8404f0-0cf6-4709-9fad-be2ceb40dddd` (Финляндия 5):
  its IP pointed here but its old inbound, node and SNI belonged to CHISLO.
- Added TCP host `cdf0f08b-e524-45bd-8657-cf3f20b6bfb6` and WS host
  `8c1bd26f-f9b5-4a0d-ac80-224e11ff4e0d`, both on this IP and node.
- WS uses TLS/SNI/Host `tekst.site`, port443 and `/direct/ws/`; certificate
  validation remains enabled. No public DNS lookup is needed to dial its IP.
- gRPC and new TCP use local REALITY target `127.0.0.1:443`, SNI
  `www.tekst.site`. Local TLS1.3/h2 and hostname validation passed. Public DNS
  queried from the Mac is correct, but resolution from the VPS was failing.
- Existing four inbound configurations retained; only gRPC target changed.
  Hysteria2, XHTTP, routing, other nodes and the CHISLO WS host were not changed.
- Source gRPC audience preserved; corresponding TEKST inbound IDs appended to
  its existing squads. New hosts were created disabled and enabled after checks.
- Firewall: only additional rule is TCP2443 (IPv4/IPv6).

Validation: Xray26.7.28 configuration test passed; node connected without error;
2083/2443 listen and pass TLS1.3/h2; external TCP HTTPS health200 with trusted
certificate; external WS upgrade101. These are infrastructure checks, not an
authenticated VPN or speed benchmark. No subscriber was created or modified.

Private rollback snapshots and immutable intents are in
`/private/tmp/wayspn-tcp-tekst-20261106` (0700; files0600). Keep these private.
`tcp_pilot.py` is a fixed-target operation helper, not a multi-node rollout tool.
Do not regenerate keys or blindly restore entire old snapshots over concurrent edits.

Certificate for `tekst.site`/`www.tekst.site` expires on 2027-01-01. Check normal
certificate renewal before then; no new certificate was issued by this operation.
REALITY minimum client version was not lowered.

## Follow-up: all three transports failed for the owner

The initial checks only established incoming TLS/WS, not successful VPN egress.
The VPS had both old `87.251.16.170/32` and new `64.188.80.39/32` on ens3.
Its default route selected the old source IP. HTTPS to1.1.1.1 failed with the
default source but succeeded immediately when bound to64.188.80.39.

Corrected the live default route to prefer source64.188.80.39, preserving the
gateway, interface, both assigned addresses and firewall. Refreshed
systemd-resolved after the route change. DNS and HTTPS requests to both
www.gstatic.com/generate_204 and www.google.com/generate_204 then returned204
from source64.188.80.39. No subscriber account or quota was changed.

The actual interface is managed by ifupdown, not the stale netplan enp1s0 entry.
Added exactly one `post-up` route command under existing ens3:0 in
`/etc/network/interfaces` to preserve the preferred source after network startup.
No ifdown, networking restart or reboot was performed. `ifquery` parsed the
candidate and installed file successfully. This operation did not run an
authenticated client test; owner app reconnection remains the end-to-end check.

Private network backups: local `interfaces.before` in the private operation
directory, remote `/root/spn-tekst-interfaces-20261106.before`. Original SHA256:
`f08e8e29c49de4adabeb7f7bf692145a9de1e19e654bf5267bac8c696b68b2b8`.
Installed candidate SHA256:
`f0151ee8c73cf43dd693c512c539032a47bb48d37ce4501ad05759c6a20245e3`.

## Authenticated owner checks and TCP target comparison

Owner subsequently confirmed WS works, while TCP/gRPC still fail on the phone;
the separate German VK RAW/REALITY connection works in both Happ and INCY.
The owner supplied the actual subscription in an ignored0600 local file.
No new subscription or quota changes were made. Its published public keys,
short IDs and SNI matched the corresponding server inbound; DNS/routing and
inbounds were identical across the three individual generated profiles.

An official macOS ARM64 Xray26.7.28 archive was verified against GitHub release
SHA256 `9b99a351febe31b7e0c7f22deeb1577a1da0b98aaa51aec7fd17832e68cf63d6`;
engine SHA256 `bd4154efa640c5b8e21f10b68afcc9177c4f1f543be3ec0485b10c499b2a4b27`.
Isolated localhost SOCKS tests, with no direct fallback, passed HTTPS204 through
all three hosts. The first tests followed an existing Mac VPN and are not
evidence of the direct mobile path.

Repeated the tests with only the diagnostic outbound bound to physical en0
(supported by the pinned Xray Darwin code), without modifying any system route
or the Mac VPN. Owner confirmed en0 was tethered to the problem phone.
All four endpoints passed: gRPC2083 (714ms), TCP2443 (441ms), WS443 (931ms),
and German VK control10016 (589ms). lsof observed the physical source socket
for each test. These are small HTTPS response timings, not throughput results,
and do not establish why the native phone clients still fail.

Prepared and briefly applied a single reversible comparison: only the new TCP inbound's
REALITY target/serverNames and its host SNI used `login.vk.com`, matching the
working VK connection's TLS target. Keys, short ID, port, user account, audience,
host name, and WS/gRPC configurations remain unchanged. Xray validation passed;
fresh subscription fetch confirmed the trial TCP SNI. The physical test then
timed out for TCP while gRPC, WS and VK control passed. Rolled back ONLY the
trial TCP target/serverNames and host SNI to the original localhost/www.tekst.site
values, preserving keys and any unrelated concurrent fields. Repeated physical
tests after rollback passed for all four endpoints. Client minimum version and
certificate validation were not weakened. The VK target is NOT the current
TEKST configuration.

Private before/candidate/intent files: `tcp-vk-target.*` in the private pilot
directory. Target comparison helper is `tcp_target_compare.py`; it requires the
localhost HTTPS forward18748, with original panel SNI and certificate checks.
Authenticated results and short-lived client logs remain private under
`/private/tmp/wayspn-tekst-auth-20261106`. Probe client processes are stopped and
their temporary executable configs deleted. Phone-side connection error logs
are needed before calling the remaining compatibility/path problem resolved or
changing gRPC similarly. Do not infer that the phone is at fault merely because
Mac probes passed.

## Phone log and exact client-core comparison

The subsequently provided phone log contains Xray26.9.9, 103 core-start records
and49 errors during a short mass-probe window. Its errors concern other WS
hosts; it contains no reference to64.188.80.39,2083 or2443, and no explicit
REALITY verification/flow failure. Deprecation warnings are not evidence that
these connections failed. A selected-phone-session error is still missing.

Downloaded a separate official macOS ARM64 Xray26.9.9 without updating any
installed app or server. Release archive SHA256:
`b7cf765d60ccc703853d4218c49a1eacc5bca764543b9540bdeaf45c951afc7d`;
engine SHA256 `7616e4d8d5b8bedaee14ebe167954e24f35ccea58cdb941600e2ff76509fc31c`.
The same owner-authorized physical-en0 test passed HTTPS204 through TEKST
gRPC,TCP,WS and VK control, with the physical source observed for all four.
This does not reproduce the phone failure and does not prove that the version
or the phone itself is at fault. Server settings were not changed in this stage.

Follow-up without requesting phone logs: authenticated physical-en0 probes on
26.9.9 also passed a262144-byte HTTPS body through each of the four endpoints
(200 and exact byte count,512KiB response cap). Separate SOCKS5 UDP-associate
tests each sent one A question forwww.gstatic.com to1.1.1.1:53 through the same
four outbounds; all returned valid matching DNS replies. These probes do not
reproduce the phone TUN's behaviour and are not a throughput benchmark.
All generated probe client configurations/processes were cleaned up; results
remain in protected directories. No new subscribers, payments, global routes,
MTU values or production settings were changed for these checks.

## Bounded owner observation and isolated phone profile

Owner reported no usable selected-session app logs. A180-second read-only
observer used only existing account id/username from the owner-supplied link;
no peer addresses, destinations, request payloads or account identities were
printed/persisted. It sampled TCP2443 kernel counters and only new matching
access-log records. The window had matching-owner gRPC activity but no confirmed
owner TCP authentication. The same account was also active on the Mac, so gRPC
activity must NOT be attributed to the phone without further evidence. The
owner reported the manual TCP attempt still failed; no definitive failure
location was established. The observer stopped automatically; SSH was closed.

Prepared one private standalone VLESS TCP client link using exactly the existing
authorized user's credentials and the restored TCP settings. No new subscriber,
bot payment or subscription was created, and production settings did not change.
The profile is named SPN-TCP-CLEAN-TEST, separate from the normal JSON subscription.
It is intended to isolate cached/generated client-profile behaviour, not assert
that caching is the known cause. A native local CoreImage QR was generated,
decoded back with Vision and checked against the original payload, saved0600
in the private state directory and opened only in local Preview (not the chat).
The link is not transmitted to any QR service or logged. Helpers:
single_tcp_qr.py/private_qr.swift. Phone import/retest is the next required check.

## Separate TCP/TLS check — 2026-11-07

The user reported the standalone REALITY profile also failed on the phone.
An unpublished panel-managed RAW/TLS1.3/Vision inbound on 2444 reused the
mounted tekst.site certificate. It was assigned to one existing owner squad;
no hosts, user records, subscription limits, WS or gRPC settings changed.
The installed Xray validated the candidate and TLS hostname/chain verification
succeeded. Authenticated HTTPS204 passed over the Mac's default tunnel route.

Through physical en0 the new port did not establish TCP. The original TEKST
VPN checks on 2083, 2443, and 443 timed out as well, while the independent VK
control passed. Ordinary HTTPS on 443 confirmed the same distinction: no TCP
connect through en0, HTTP200 via the default route (utun11). This is evidence
of a path-dependent issue in that observation, not proof of a particular block,
and not an authenticated connection from the phone itself.

Removed the unpublished inbound and only its 2444 firewall rule. Confirmed
443/2083/2443 still listening and 2444 absent. The TLS QR was never published or
shown for phone testing. The helper retains private before/intent snapshots.
