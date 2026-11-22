"""Route explicitly configured A records to their DNS provider."""

from .dns import DNSRecord, DNSUpdateError
from .regru_dns import RegRuRecord


class DNSRouter:
    def __init__(self, *, cloudflare=None, regru=None):
        self.cloudflare, self.regru = cloudflare, regru

    def backend(self, record):
        if isinstance(record, RegRuRecord):
            backend = self.regru
        elif isinstance(record, DNSRecord):
            backend = self.cloudflare
        else:
            raise DNSUpdateError("Unknown configured DNS record provider")
        if backend is None:
            raise DNSUpdateError("The configured DNS provider is not connected")
        return backend

    def read_ipv4(self, record):
        return self.backend(record).read_ipv4(record)

    def replace_ipv4(self, record, expected, replacement):
        return self.backend(record).replace_ipv4(record, expected, replacement)

    def resume_ipv4(self, record, allowed_ips):
        resume = getattr(self.backend(record), "resume_ipv4", None)
        return resume(record, allowed_ips) if callable(resume) else False
