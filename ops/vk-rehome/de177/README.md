# Direct German TLS front on the existing VK backend

Fixed scope: `48.102.177.3`, `de177.wayspn.com`, TCP 80/443. This does not
recreate the running host-network Remnawave Node or edit VK TCP 2443, panel
TCP 2222, SSH, DNS, Xray, panel hosts, or the rented relay.

1. Confirm `de177.wayspn.com` is the intended domain and the chosen DNS A
   record points directly to `48.102.177.3`; no unrelated/stale AAAA. Confirm
   Nginx, Certbot, OpenSSL and curl are present (official Ubuntu packages).
   If installing Nginx, check existing ports before starting the package.
2. Stage the scripts and configs as root under `/root/vk-rehome-de177` (0700).
   Run `bash /root/vk-rehome-de177/bootstrap.sh --apply`, then
   `bash /root/vk-rehome-de177/firewall.sh --apply` if direct ports aren't
   already permitted. Only the distribution default Nginx site may be
   replaced; only TCP 80/443 public rules are added to an already active UFW.
3. Only after the operator approves certificate issuance (and any new
   account terms), use Certbot HTTP-01 with webroot
   `/var/www/vk-rehome-de177-acme`, certificate name/domain
   `de177.wayspn.com`, and retain the default trusted Let's Encrypt chain.
   No script here requests a certificate or accepts terms.
4. Run `bash /root/vk-rehome-de177/finish.sh --apply`. It checks the local
   certificate and UFW, tests Nginx, performs a graceful Nginx reload, and
   checks strict HTTPS health. Only then run end-to-end tests for both direct
   transports before publishing them.

Client ingress: TLS 443, SNI/Host `de177.wayspn.com`. gRPC is ALPN h2,
service `spngrpc`, and Nginx forwards `/spngrpc/Tun` to
`127.0.0.1:10087`. WS is ALPN http/1.1, path `/direct/ws/`, and Nginx
forwards it to `127.0.0.1:10088`. The two Xray inbounds are configured
separately on loopback without inner TLS.
