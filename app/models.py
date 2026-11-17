"""Pydantic models and zone validation rules shared by both roles."""
from __future__ import annotations

import ipaddress
import re
from typing import List, Optional

from pydantic import BaseModel, Field, field_validator, model_validator

from .config import settings

_LABEL = re.compile(r"^[a-z0-9]([a-z0-9-]*[a-z0-9])?$")


class RecordInput(BaseModel):
    name: str = Field(..., examples=["db.internal.local"])
    ip: str = Field(..., examples=["10.0.0.11"])

    @field_validator("name")
    @classmethod
    def _strip_name(cls, v: str) -> str:
        if not isinstance(v, str):
            raise ValueError("name must be a string")
        return v.strip().rstrip(".").lower()

    @field_validator("ip")
    @classmethod
    def _valid_ip(cls, v: str) -> str:
        try:
            ip = ipaddress.ip_address(v.strip())
        except ValueError as exc:
            raise ValueError(f"invalid IP address: {v}") from exc
        if ip.version != 4:
            raise ValueError("only IPv4 A records are supported")
        return str(ip)


class ZonePayload(BaseModel):
    """A complete replacement of the fixed zone (max 8 A records)."""

    records: List[RecordInput]

    @model_validator(mode="after")
    def _validate_zone(self) -> "ZonePayload":
        if not self.records:
            raise ValueError("zone must contain at least one A record")
        if len(self.records) > settings.max_records:
            raise ValueError(
                f"a zone may contain at most {settings.max_records} A records"
            )
        zone = settings.dns_zone.rstrip(".").lower()
        suffix = "." + zone
        seen: set[str] = set()
        for rec in self.records:
            name = rec.name
            if name != zone and not name.endswith(suffix):
                raise ValueError(
                    f"record {name!r} is outside the fixed zone {zone!r}"
                )
            owner = name[: -len(suffix)] if name.endswith(suffix) else ""
            labels = owner.split(".") if owner else []
            if any(not _LABEL.match(label) for label in labels):
                raise ValueError(f"invalid owner name: {name!r}")
            if name in seen:
                raise ValueError(f"duplicate A record name: {name!r}")
            seen.add(name)
        return self

    def sorted_records(self) -> List[RecordInput]:
        return sorted(self.records, key=lambda r: r.name)


class RecordOut(BaseModel):
    name: str
    ip: str


class ZoneOut(BaseModel):
    zone: str
    serial: int
    records: List[RecordOut]
    published_at: Optional[str] = None


class VersionOut(BaseModel):
    role: str
    zone: str
    serial: int
    record_count: int
    published_at: Optional[str] = None
    last_sync_at: Optional[str] = None
    last_sync_status: str = "never"
    last_sync_error: Optional[str] = None
    upstream_serial: Optional[int] = None


class PublishResult(BaseModel):
    ok: bool
    serial: int
    record_count: int
    published_at: str
