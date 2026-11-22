"""Convert supported Remnawave resolved VLESS rows into isolated Xray exits."""

from copy import deepcopy
from ipaddress import ip_address
import re


EMPTY = (None, "", [], {})
INDIRECTION = {"dialerProxy", "proxyTag", "proxySettings", "detour", "outboundTag", "balancerTag",
               "downloadSettings", "sendThrough", "interface", "tproxy"}


def _require(condition, reason):
    if not condition:
        raise ValueError(reason)


def _object(value, reason):
    if value is None:
        return {}
    _require(isinstance(value, dict), reason)
    return value


def _no_indirection(value):
    if isinstance(value, dict):
        _require(not any(key in INDIRECTION and item not in EMPTY for key, item in value.items()),
                 "dependent outbound is unsupported")
        for item in value.values():
            _no_indirection(item)
    elif isinstance(value, list):
        for item in value:
            _no_indirection(item)


def _supported(options, names, reason):
    _require(not any(key not in names and value not in EMPTY for key, value in options.items()), reason)
    return {key: deepcopy(value) for key, value in options.items() if key in names and value not in EMPTY}


def _address(value):
    _require(isinstance(value, str) and 0 < len(value) <= 253 and not any(c.isspace() for c in value),
             "invalid resolved address")
    try:
        address = ip_address(value)
    except ValueError:
        _require(re.fullmatch(r"[a-zA-Z0-9](?:[a-zA-Z0-9.-]*[a-zA-Z0-9])?", value) is not None
                 and "." in value and not value.lower().endswith((".local", ".internal", ".localhost")),
                 "invalid resolved hostname")
    else:
        _require(address.is_global and not address.is_multicast, "non-public resolved address")
    return value


def xray_outbound(row):
    """Return one VLESS exit. Unsupported customization fails before credentials escape."""
    _require(isinstance(row, dict) and row.get("protocol") == "vless", "unsupported resolved protocol")
    overrides = _object(row.get("clientOverrides"), "invalid client overrides")
    _require(overrides.get("xrayJsonTemplate") in EMPTY and overrides.get("mapper") in EMPTY,
             "custom client template is unsupported")
    stream_overrides = _object(row.get("streamOverrides"), "invalid stream overrides")
    _require(not any(value not in EMPTY for value in stream_overrides.values()),
             "custom stream overrides are unsupported")
    mux = _object(row.get("mux"), "invalid mux settings")
    _require(not mux or mux.get("enabled") is False, "enabled mux is unsupported")

    address = _address(row.get("address"))
    port = row.get("port")
    _require(type(port) is int and 1 <= port <= 65535, "invalid resolved port")
    user = _supported(_object(row.get("protocolOptions"), "invalid protocol options"),
                      {"id", "encryption", "flow"}, "unsupported VLESS protocol options")
    _require(isinstance(user.get("id"), str) and bool(user["id"]), "missing VLESS identity")
    _require(isinstance(user.get("encryption"), str) and bool(user["encryption"]), "missing VLESS encryption")
    if "flow" in user:
        _require(isinstance(user["flow"], str), "invalid VLESS flow")

    security = row.get("security")
    _require(security in {"tls", "reality"}, "unsupported resolved security")
    options = _object(row.get("securityOptions"), "invalid security options")
    if security == "tls":
        settings = _supported(options, {"serverName", "fingerprint", "alpn", "enableSessionResumption",
                                       "cipherSuites"}, "unsupported TLS options")
        alpn = settings.get("alpn")
        if isinstance(alpn, str):
            settings["alpn"] = [item.strip() for item in alpn.split(",") if item.strip()]
        elif alpn is not None:
            _require(isinstance(alpn, list) and all(isinstance(item, str) and item for item in alpn),
                     "invalid ALPN options")
    else:
        settings = _supported(options, {"serverName", "fingerprint", "publicKey", "shortId", "spiderX",
                                       "mldsa65Verify"}, "unsupported REALITY options")
        _require(isinstance(settings.get("publicKey"), str) and bool(settings["publicKey"]),
                 "missing REALITY public key")
    _require(isinstance(settings.get("serverName"), str) and bool(settings["serverName"]),
             "missing stable TLS identity")

    network = row.get("transport")
    _require(network in {"tcp", "grpc", "ws", "xhttp"}, "unsupported resolved transport")
    transport = _object(row.get("transportOptions"), "invalid transport options")
    stream = {"network": network, "security": security, security + "Settings": settings}
    if network == "tcp":
        header = transport.get("header")
        _require(header in EMPTY or header == {"type": "none"}, "TCP disguise is unsupported")
        _supported(transport, {"header"}, "unsupported TCP options")
    elif network == "grpc":
        stream["grpcSettings"] = _supported(transport, {"authority", "serviceName", "multiMode"},
                                            "unsupported gRPC options")
    elif network == "ws":
        ws = _supported(transport, {"host", "path", "headers", "heartbeatPeriod"}, "unsupported WS options")
        host = ws.pop("host", None)
        if host:
            headers = _object(ws.get("headers"), "invalid WS headers")
            _require(not any(key.lower() == "host" and value != host for key, value in headers.items()),
                     "conflicting WS host")
            ws["headers"] = {**headers, "Host": host}
        stream["wsSettings"] = ws
    else:
        stream["xhttpSettings"] = _supported(transport, {"path", "host", "mode", "extra"},
                                             "unsupported XHTTP options")
    _no_indirection(stream)
    outbound = {"tag": "probe-selected", "protocol": "vless",
                "settings": {"vnext": [{"address": address, "port": port, "users": [user]}]},
                "streamSettings": stream}
    if mux:
        outbound["mux"] = deepcopy(mux)
    return outbound
