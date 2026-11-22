"""Single-use HTTPS enrollment binds a new VPS to its real SSH host key.

An authenticated callback is only a receipt, not ownership proof. ``verify``
additionally requires its actual socket source AND assigned address to equal
the authoritative Voxa service IPv4. No keyscan or trust-on-first-SSH is used.
Public CA / management public key may enter user-data; private keys never do.
"""

from __future__ import annotations

import base64
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import fcntl
import hashlib
import hmac
import ipaddress
import json
import math
import os
from pathlib import Path
import re
import secrets
import stat
import tempfile
import time
from urllib.parse import urlsplit, urlunsplit
import uuid

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec, ed25519
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

from .credentials import private_directory


NONCE_LIFETIME = 3600
CALLBACK_MAX_AGE = 300
MAX_CALLBACK_BYTES = 16_384
MAX_FILE_BYTES = 131_072


class EnrollmentError(ValueError):
    """Safe diagnostic with no nonce, user-data, private key or raw callback."""


def _require(condition, message="Enrollment data is invalid or incomplete"):
    if not condition:
        raise EnrollmentError(message)


def _operation(value):
    try:
        parsed = uuid.UUID(value)
        _require(parsed.int != 0 and str(parsed) == value)
        return str(parsed)
    except (ValueError, TypeError, AttributeError):
        raise EnrollmentError("Enrollment operation must be a canonical UUID") from None


def _public_ipv4(value, *, mapped=False):
    try:
        parsed = ipaddress.ip_address(value)
        if mapped and isinstance(parsed, ipaddress.IPv6Address) and parsed.ipv4_mapped:
            parsed = parsed.ipv4_mapped
        _require(isinstance(value, str) and isinstance(parsed, ipaddress.IPv4Address)
                 and parsed.is_global and not parsed.is_reserved and not parsed.is_multicast)
        return str(parsed)
    except (ValueError, TypeError):
        raise EnrollmentError("Enrollment requires a public IPv4 address") from None


def _controller(value):
    """Static public-URL validation; deliberately does not resolve DNS or probe ingress."""
    try:
        _require(isinstance(value, str) and len(value) <= 2048 and value.isascii()
                 and not any(character.isspace() for character in value))
        parsed = urlsplit(value)
        _require(parsed.scheme == "https" and parsed.hostname and parsed.username is None
                 and parsed.password is None and parsed.path in ("", "/") and not parsed.query
                 and not parsed.fragment and (parsed.port is None or 1 <= parsed.port <= 65535))
        host = parsed.hostname.casefold()
        try:
            address = ipaddress.ip_address(host)
        except ValueError:
            _require(len(host) <= 253 and "." in host and not host.endswith((".local", ".internal", ".localhost"))
                     and all(re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", label)
                             for label in host.split(".")))
            identity = x509.DNSName(host)
        else:
            _require(address.is_global and not address.is_reserved and not address.is_multicast)
            identity = x509.IPAddress(address)
        authority = "[" + host + "]" if ":" in host else host
        if parsed.port not in (None, 443):
            authority += ":" + str(parsed.port)
        return urlunsplit(("https", authority, "", "", "")), host, identity
    except (ValueError, TypeError, AttributeError):
        raise EnrollmentError("Enrollment needs a public HTTPS controller URL without credentials or a path") from None


def _unique(pairs):
    result = {}
    for key, value in pairs:
        _require(key not in result)
        result[key] = value
    return result


def _json_bytes(value):
    try:
        return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    except (TypeError, ValueError, RecursionError):
        raise EnrollmentError("Enrollment JSON is invalid") from None


def _read(path):
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError:
        return None
    except OSError:
        raise EnrollmentError("Enrollment file cannot be read safely") from None
    try:
        before = os.fstat(descriptor)
        _require(stat.S_ISREG(before.st_mode) and before.st_uid == os.geteuid()
                 and stat.S_IMODE(before.st_mode) == 0o600 and before.st_nlink == 1
                 and 0 < before.st_size <= MAX_FILE_BYTES, "Enrollment files require private 0600 permissions")
        with os.fdopen(descriptor, "rb", closefd=False) as stream:
            raw = stream.read(MAX_FILE_BYTES + 1)
        after, named = os.fstat(descriptor), path.stat()
        identity = lambda row: (row.st_dev, row.st_ino, row.st_size, row.st_mtime_ns, row.st_mode, row.st_nlink)
        _require(len(raw) == before.st_size and identity(before) == identity(after) == identity(named))
        return raw
    finally:
        os.close(descriptor)


def _write(path, raw):
    _require(isinstance(raw, bytes) and 0 < len(raw) <= MAX_FILE_BYTES)
    private_directory(path.parent)
    _read(path)  # Do not replace an unsafe existing inode.
    descriptor, temporary = tempfile.mkstemp(prefix=".enrollment-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            os.fchmod(stream.fileno(), 0o600)
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        Path(temporary).unlink(missing_ok=True)


def _load_json(path):
    raw = _read(path)
    if raw is None:
        return None
    try:
        def invalid(value):
            raise ValueError()
        return json.loads(raw, object_pairs_hook=_unique, parse_constant=invalid)
    except (ValueError, TypeError, UnicodeError, RecursionError):
        raise EnrollmentError("Enrollment journal is invalid; contents suppressed") from None


def _host_key(value):
    try:
        _require(isinstance(value, str) and 1 <= len(value) <= 1024 and value.isascii() and value.isprintable())
        loaded = serialization.load_ssh_public_key(value.encode("ascii"))
        _require(isinstance(loaded, ed25519.Ed25519PublicKey))
        canonical = loaded.public_bytes(serialization.Encoding.OpenSSH, serialization.PublicFormat.OpenSSH).decode()
        wire = base64.b64decode(canonical.split()[1], validate=True)
        fingerprint = "SHA256:" + base64.b64encode(hashlib.sha256(wire).digest()).decode().rstrip("=")
        return canonical, fingerprint
    except (ValueError, TypeError, UnicodeError, IndexError):
        raise EnrollmentError("Enrollment needs a valid Ed25519 SSH host public key") from None


def _payload(value):
    _require(isinstance(value, dict) and set(value) == {"nonce", "machine_id", "host_key", "assigned_ips", "observed_at"}
             and len(_json_bytes(value)) <= MAX_CALLBACK_BYTES)
    _require(isinstance(value["nonce"], str) and re.fullmatch(r"[A-Za-z0-9_-]{43}", value["nonce"]) is not None)
    _require(isinstance(value["machine_id"], str) and re.fullmatch(r"[0-9a-f]{32}", value["machine_id"]) is not None
             and int(value["machine_id"], 16) != 0, "Enrollment needs a valid machine identity")
    key, fingerprint = _host_key(value["host_key"])
    ips = value["assigned_ips"]
    _require(isinstance(ips, list) and 1 <= len(ips) <= 32 and all(isinstance(ip, str) for ip in ips)
             and len(set(ips)) == len(ips))
    ips = [_public_ipv4(ip) for ip in ips]
    stamp = value["observed_at"]
    _require(type(stamp) in (int, float) and math.isfinite(stamp), "Enrollment observation time is invalid")
    return {"machine_id": value["machine_id"], "host_key": key, "host_key_sha256": fingerprint,
            "assigned_ips": ips, "observed_at": stamp}


class Enrollment:
    """Private enrollment journal; callbacks remain untrusted until ``verify``.

    ``ready`` is static readiness ONLY, never proof the public TLS listener is up.
    SSH key paths are internal; prepare returns only public key + one-use script.
    """

    def __init__(self, data_dir, *, controller_url):
        self.directory = private_directory(Path(data_dir).expanduser().absolute() / "enrollment")
        self.operations = private_directory(self.directory / "operations")
        self.tls_directory = private_directory(self.directory / "tls")
        self.ssh_private_key = self.directory / "management_ed25519"
        self.ssh_public_key = self.directory / "management_ed25519.pub"
        self.tls_key = self.tls_directory / "controller.key"
        self.tls_cert = self.tls_directory / "controller.crt"
        self._clock = time.time
        self.controller_url = controller_url
        with self._locked():
            self._management_key()

    @contextmanager
    def _locked(self):
        try:
            descriptor = os.open(self.directory / ".lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        except OSError:
            raise EnrollmentError("Enrollment journal cannot be locked safely") from None
        try:
            info = os.fstat(descriptor)
            _require(stat.S_ISREG(info.st_mode) and info.st_uid == os.geteuid()
                     and stat.S_IMODE(info.st_mode) == 0o600 and info.st_nlink == 1)
            fcntl.flock(descriptor, fcntl.LOCK_EX)
            yield
        finally:
            os.close(descriptor)

    def _management_key(self):
        private, public = _read(self.ssh_private_key), _read(self.ssh_public_key)
        try:
            if private is None:
                _require(public is None, "Management private key is missing; refusing to rotate identity")
                key = ed25519.Ed25519PrivateKey.generate()
                private = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.OpenSSH,
                                            serialization.NoEncryption())
                _write(self.ssh_private_key, private)
            else:
                key = serialization.load_ssh_private_key(private, password=None)
                _require(isinstance(key, ed25519.Ed25519PrivateKey))
            expected = key.public_key().public_bytes(serialization.Encoding.OpenSSH, serialization.PublicFormat.OpenSSH)
            if public is None:
                _write(self.ssh_public_key, expected + b"\n")
            else:
                _require(hmac.compare_digest(public.strip(), expected), "Management public and private keys disagree")
            return expected.decode()
        except EnrollmentError:
            raise
        except Exception:
            raise EnrollmentError("Management SSH identity is invalid; details suppressed") from None

    def _tls(self, *, create=False):
        url, host, identity = _controller(self.controller_url)
        key_raw, cert_raw = _read(self.tls_key), _read(self.tls_cert)
        if not create:
            _require(key_raw is not None and cert_raw is not None, "Local enrollment TLS certificate is not configured")
        try:
            if key_raw is None:
                _require(cert_raw is None, "TLS private key is missing; refusing to replace identity")
                key = ec.generate_private_key(ec.SECP256R1())
                key_raw = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                            serialization.NoEncryption())
                _write(self.tls_key, key_raw)
            else:
                key = serialization.load_pem_private_key(key_raw, password=None)
                _require(isinstance(key, ec.EllipticCurvePrivateKey) and isinstance(key.curve, ec.SECP256R1))
            now = datetime.now(timezone.utc)
            if cert_raw is None:
                subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, host)])
                cert = (x509.CertificateBuilder().subject_name(subject).issuer_name(subject)
                    .public_key(key.public_key()).serial_number(x509.random_serial_number())
                    .not_valid_before(now - timedelta(minutes=5)).not_valid_after(now + timedelta(days=365))
                    .add_extension(x509.SubjectAlternativeName([identity]), critical=False)
                    .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
                    .add_extension(x509.KeyUsage(digital_signature=True, content_commitment=False,
                        key_encipherment=False, data_encipherment=False, key_agreement=False,
                        key_cert_sign=True, crl_sign=False, encipher_only=False, decipher_only=False), critical=True)
                    .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
                    .sign(key, hashes.SHA256()))
                cert_raw = cert.public_bytes(serialization.Encoding.PEM)
                _write(self.tls_cert, cert_raw)
            else:
                cert = x509.load_pem_x509_certificate(cert_raw)
            before = cert.not_valid_before_utc if hasattr(cert, "not_valid_before_utc") else cert.not_valid_before.replace(tzinfo=timezone.utc)
            after = cert.not_valid_after_utc if hasattr(cert, "not_valid_after_utc") else cert.not_valid_after.replace(tzinfo=timezone.utc)
            _require(cert.issuer == cert.subject and before <= now
                     and after > now + timedelta(seconds=NONCE_LIFETIME))
            sans = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
            _require(identity in sans)
            expected = key.public_key().public_bytes(serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo)
            actual = cert.public_key().public_bytes(serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo)
            _require(hmac.compare_digest(expected, actual))
            key.public_key().verify(cert.signature, cert.tbs_certificate_bytes, ec.ECDSA(cert.signature_hash_algorithm))
            return {"key_path": self.tls_key, "cert_path": self.tls_cert, "ca_pem": cert_raw.decode("ascii")}
        except EnrollmentError:
            raise
        except Exception:
            raise EnrollmentError("Enrollment TLS identity is invalid; details suppressed") from None

    def ensure_tls(self):
        """Create a local self-signed SAN certificate, not a live-ingress claim."""
        with self._locked():
            return self._tls(create=True)

    def ready(self):
        result = {"ready": False, "reasons": [], "static_only": True, "ingress_verified": False}
        try:
            with self._locked():
                _controller(self.controller_url)
                self._management_key()
                self._tls()
            result["ready"] = True
        except Exception as error:
            result["reasons"] = [str(error) if isinstance(error, EnrollmentError)
                                 else "Enrollment prerequisites are not configured"]
        return result

    def _state(self, operation_id):
        value = _load_json(self.operations / (operation_id + ".json"))
        if value is not None:
            _require(isinstance(value, dict) and value.get("format") == 1
                     and value.get("operation_id") == operation_id and isinstance(value.get("nonce"), str)
                     and re.fullmatch(r"[A-Za-z0-9_-]{43}", value["nonce"]) is not None
                     and type(value.get("issued_at")) in (int, float) and math.isfinite(value["issued_at"])
                     and type(value.get("expires_at")) in (int, float) and math.isfinite(value["expires_at"])
                     and value["expires_at"] - value["issued_at"] == NONCE_LIFETIME
                     and isinstance(value.get("prepared"), dict)
                     and set(value["prepared"]) == {"ssh_public_key", "user_data"})
        return value

    def prepare(self, operation_id):
        operation_id = _operation(operation_id)
        with self._locked():
            value = self._state(operation_id)
            if value is not None:
                # Never refresh a nonce, script, certificate pin or SSH key on retry.
                return dict(value["prepared"])
            tls = self._tls(create=True)
            public = self._management_key()
            issued = self._clock()
            _require(type(issued) in (int, float) and math.isfinite(issued))
            nonce = secrets.token_urlsafe(32)
            expires = issued + NONCE_LIFETIME
            prepared = {"ssh_public_key": public, "user_data": self._script(operation_id, nonce, expires, tls["ca_pem"])}
            _write(self.operations / (operation_id + ".json"), _json_bytes({"format": 1, "operation_id": operation_id,
                "nonce": nonce, "issued_at": issued, "expires_at": expires, "prepared": prepared,
                "receipt": None, "receipt_digest": None}))
            return dict(prepared)

    def _script(self, operation_id, nonce, expires, ca_pem):
        url, _, _ = _controller(self.controller_url)
        config = {"url": url + "/bootstrap/" + operation_id, "operation_id": operation_id,
                  "nonce": nonce, "expires_at": expires, "ca_pem": ca_pem}
        # JSON is embedded as a Python string, never interpolated into shell syntax.
        literal = repr(json.dumps(config, separators=(",", ":")))
        script = """#!/bin/bash
set -eu
umask 077
/usr/bin/python3 - <<'VOXA_ENROLLMENT_PY'
import ipaddress,json,os,pathlib,ssl,subprocess,time,urllib.request
CFG=json.loads(CONFIG_LITERAL)
class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self,*args,**kwargs):
        return None
def enroll():
    context=ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.minimum_version=ssl.TLSVersion.TLSv1_2
    context.load_verify_locations(cadata=CFG['ca_pem'])
    opener=urllib.request.build_opener(urllib.request.ProxyHandler({}),urllib.request.HTTPSHandler(context=context),NoRedirect())
    machine=pathlib.Path('/etc/machine-id').read_text().strip()
    host_key=pathlib.Path('/etc/ssh/ssh_host_ed25519_key.pub').read_text().strip()
    command=next((p for p in ['/usr/sbin/ip','/sbin/ip','/usr/bin/ip'] if os.path.isfile(p)),None)
    if command is None:
        return
    rows=subprocess.run([command,'-j','-4','addr','show','scope','global'],stdin=subprocess.DEVNULL,stdout=subprocess.PIPE,stderr=subprocess.DEVNULL,timeout=5,check=True).stdout
    if len(rows)>65536:
        return
    addresses=[]
    for row in json.loads(rows):
        for info in row.get('addr_info',[]):
            value=info.get('local')
            try:
                address=ipaddress.IPv4Address(value)
                if address.is_global and not address.is_multicast and not address.is_reserved:
                    addresses.append(str(address))
            except (ValueError,TypeError):
                pass
    addresses=sorted(set(addresses))
    if not addresses or len(addresses)>32:
        return
    payload=json.dumps({'nonce':CFG['nonce'],'machine_id':machine,'host_key':host_key,'assigned_ips':addresses,'observed_at':time.time()},separators=(',',':')).encode()
    if len(payload)>16384:
        return
    for attempt in range(24):
        if time.time()>=CFG['expires_at']:
            return
        try:
            request=urllib.request.Request(CFG['url'],data=payload,headers={'Content-Type':'application/json','Accept':'application/json'},method='POST')
            with opener.open(request,timeout=10) as response:
                raw=response.read(4097)
                if 200<=response.status<300 and len(raw)<=4096:
                    receipt=json.loads(raw)
                    if receipt.get('accepted') is True and receipt.get('operation_id')==CFG['operation_id']:
                        return
        except Exception:
            pass
        time.sleep(5)
try:
    enroll()
except Exception:
    pass
VOXA_ENROLLMENT_PY
""".replace("CONFIG_LITERAL", literal)
        _require(script.startswith("#!/bin/bash\n") and len(script.encode()) <= 65_536)
        return script

    def accept(self, operation_id, payload, source_ip):
        """Record one nonce-authenticated receipt; source must be the socket peer.

        Never pass forwarded headers as source_ip. Exact duplicate delivery is
        harmless, including after expiry; no new receipt can replace the first.
        """
        operation_id = _operation(operation_id)
        source = _public_ipv4(source_ip, mapped=True)
        normalized = _payload(payload)
        digest = hashlib.sha256(_json_bytes(payload)).hexdigest()
        with self._locked():
            state = self._state(operation_id)
            _require(state is not None, "Enrollment operation is not prepared")
            _require(hmac.compare_digest(state["nonce"].encode("ascii"), payload["nonce"].encode("ascii")),
                     "Enrollment nonce was rejected")
            if state.get("receipt") is not None:
                _require(hmac.compare_digest(state.get("receipt_digest", ""), digest)
                         and state["receipt"].get("source_ip") == source,
                         "Enrollment receipt is immutable; replacement was rejected")
                return dict(state["receipt"])
            now = self._clock()
            _require(state["issued_at"] <= now < state["expires_at"], "Enrollment nonce has expired or is not active")
            _require(abs(now - normalized["observed_at"]) <= CALLBACK_MAX_AGE
                     and state["issued_at"] - CALLBACK_MAX_AGE <= normalized["observed_at"] <= state["expires_at"],
                     "Enrollment callback is stale or has an invalid clock")
            receipt = {"accepted": True, "verified": False, "operation_id": operation_id,
                       "source_ip": source, "received_at": now, **normalized}
            state.update(receipt=receipt, receipt_digest=digest)
            _write(self.operations / (operation_id + ".json"), _json_bytes(state))
            return dict(receipt)

    def verify(self, operation_id, expected_ip):
        operation_id = _operation(operation_id)
        expected = _public_ipv4(expected_ip)
        with self._locked():
            state = self._state(operation_id)
            _require(state is not None and isinstance(state.get("receipt"), dict), "Enrollment callback has not arrived")
            receipt = state["receipt"]
            _require(receipt.get("source_ip") == expected and expected in receipt.get("assigned_ips", []),
                     "Enrollment source and assigned address do not match the Voxa service IP")
            _require(state["issued_at"] <= receipt.get("received_at", -1) < state["expires_at"],
                     "Enrollment receipt was not accepted during its valid lifetime")
            host_key, fingerprint = _host_key(receipt.get("host_key"))
            _require(hmac.compare_digest(fingerprint, receipt.get("host_key_sha256", "")))
            return {**receipt, "verified": True, "expected_ip": expected,
                    "host_key": host_key, "host_key_sha256": fingerprint}
