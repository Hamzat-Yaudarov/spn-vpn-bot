# Additional VK relay — 2026-11-11

- Existing Finland node `5c608352-00f2-44b8-83a9-97c2bcfa255f`, now at
  `83.219.97.120`, retained. No reinstall, account or subscription creation.
- Existing profile `8811b5b8-9608-4734-89b0-725372c96037` received one additional
  RAW/REALITY inbound `vk-fi120-relay-reality` on TCP2443, with a fresh key and
  short ID. The three existing gRPC/WS/XHTTP inbounds are unchanged.
- New host `9c35bdc5-9eba-4186-ab01-844c9976ffef`:
  **🇫🇮 Финляндия • ОБХОД VK**, public `185.86.147.77:10038`, SNI
  `login.vk.com`, backend `83.219.97.120:2443`.
- Only the source bypass inbound's existing three squads received the new
  inbound. Existing memberships, balances, purchases and subscribers unchanged.
- Sweden's profile and host `3c0cfecc-5888-4dd7-a38a-023d2edfc223` are preserved;
  its front was already `185.86.147.77:10040` when this operation started.
  The two public ports/rentals are independent.
- UFW2443 allows only relay source `185.86.147.77`. Existing firewall rules
  retained; backup `/root/spn-vk-fi120/ufw.before.txt` on Finland.
- Exact combined configuration accepted by installed Xray26.7.28. Actual
  refreshed owner subscription passed HTTPS204 through public relay10038 on
  pinned Xray26.9.9, bound to physical Mac en0: 536ms. No restricted-carrier
  allowlist test was performed.

Closed local state: `/private/tmp/wayspn-vk-fi120-20261111` (0700/0600).
Do not publish that directory or the owner's private test link.
