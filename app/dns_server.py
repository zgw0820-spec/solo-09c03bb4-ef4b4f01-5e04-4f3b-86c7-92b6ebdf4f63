"""Authoritative UDP DNS service for the fixed intranet zone.

Every answer is built from one immutable version snapshot fetched in a
single storage read, so a reply can never mix records (or delegations) of
two versions. While a transfer is in progress the old version stays
active and keeps being answered.

The zone may carry one delegation for a child zone. Queries at or below
the child are answered with a non-authoritative referral built from that
same snapshot: NS records in the authority section and the in-bailiwick
nameserver's IPv4 glue in the additional section. No old parent-side A
data and no NXDOMAIN is used for those names.
"""
from __future__ import annotations

import logging
from typing import Dict, List, Optional

from dnslib import A, DNSRecord, NS, RR, QTYPE, RCODE
from dnslib.server import BaseResolver, DNSServer

from .config import settings
from .storage import storage

log = logging.getLogger("dns")

_REFERRAL_TTL = 30


def _normalise(qname: str) -> str:
    return qname.rstrip(".").lower()


def _in_child(qname: str, child: str) -> bool:
    return qname == child or qname.endswith("." + child)


class ZoneResolver(BaseResolver):
    def resolve(self, request: DNSRecord, handler) -> DNSRecord:
        reply = request.reply()
        q = request.q
        qname = _normalise(str(q.qname))
        zone = settings.dns_zone.rstrip(".").lower()

        in_zone = qname == zone or qname.endswith("." + zone)
        if not in_zone:
            # This service is authoritative only for the fixed zone.
            reply.header.rcode = RCODE.REFUSED
            return reply

        # One consistent snapshot -> one records+delegation pair per answer.
        serial, records, delegation = storage.get_active_zone()

        # A delegated child is not answered authoritatively at all: return
        # the referral even for A/NS queries at the child apex or below.
        if delegation is not None and _in_child(qname, delegation["name"]):
            return self._referral(reply, delegation, serial)

        if QTYPE[q.qtype] != "A":
            # In-zone but not an A query: authoritative NOERROR/NODATA.
            reply.header.rcode = RCODE.NOERROR
            return reply

        name_map: Dict[str, List[str]] = {}
        for rec in records:
            name_map.setdefault(rec["name"], []).append(rec["ip"])

        ips: List[str] = name_map.get(qname, [])
        if not ips:
            reply.header.rcode = RCODE.NXDOMAIN
            log.debug("NXDOMAIN %s (serial=%s)", qname, serial)
            return reply

        for ip in ips:
            reply.add_answer(
                RR(
                    q.qname,
                    QTYPE.A,
                    rdata=A(ip),
                    ttl=_REFERRAL_TTL,
                )
            )
        log.debug("ANSWER %s -> %s (serial=%s)", qname, ips, serial)
        return reply

    @staticmethod
    def _referral(reply: DNSRecord, delegation: Dict[str, str],
                  serial: int) -> DNSRecord:
        """Non-authoritative referral: NS in authority, glue in additional."""
        child = delegation["name"]
        ns = delegation["ns"]
        glue = delegation["glue"]
        reply.header.rcode = RCODE.NOERROR
        reply.header.aa = 0  # referral, not an authoritative answer
        reply.add_auth(
            RR(child, QTYPE.NS, rdata=NS(ns), ttl=_REFERRAL_TTL)
        )
        reply.add_ar(
            RR(ns, QTYPE.A, rdata=A(glue), ttl=_REFERRAL_TTL)
        )
        log.debug(
            "REFERRAL %s -> %s glue=%s (serial=%s)",
            child, ns, glue, serial,
        )
        return reply


class DnsService:
    def __init__(self) -> None:
        self._server: Optional[DNSServer] = None

    def start(self) -> None:
        resolver = ZoneResolver()
        # DNSServer is itself a daemon thread; start_thread() runs the
        # UDP serve loop without blocking the FastAPI startup.
        self._server = DNSServer(
            resolver,
            port=settings.dns_port,
            address=settings.dns_host,
            tcp=False,
        )
        self._server.start_thread()
        log.info(
            "UDP DNS listener on %s:%s (zone=%s, role=%s)",
            settings.dns_host,
            settings.dns_port,
            settings.dns_zone,
            settings.role,
        )

    def stop(self) -> None:
        if self._server is not None:
            try:
                self._server.stop()
            except Exception:  # pragma: no cover
                pass


dns_service = DnsService()
