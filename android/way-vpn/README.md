# Way VPN for Android

Way VPN is a restricted Russian-language Android client for Way SPN. It is a GPL-3.0 derivative of v2rayNG 2.2.6 and accepts only VLESS, Trojan and Shadowsocks profiles received from the authenticated Way VPN API.

## Security boundaries

- Android 8+ (`minSdk 26`), package `ru.wayspn.vpn`, target/compile SDK 37.
- Full VPN mode with IPv4, IPv6 and DNS routed through TUN; no app split tunnelling or local proxy sharing.
- Refresh token, installation HWID and offline profile are encrypted with AES-GCM keys from Android Keystore. MMKV runtime profiles use a separately wrapped random key.
- Random 32-character installation HWID; Android ID, IMEI and advertising identifiers are not used.
- No analytics, ads, cleartext HTTP, backup, arbitrary subscription URL, configuration export or reachable QR scanner.
- Updates require HTTPS, the manifest SHA-256 and the currently installed signing certificate.

Exact upstream source and binary revisions are in `UPSTREAM_LOCK.json`. Build and release instructions are in `REPRODUCIBLE-BUILD.md`.
