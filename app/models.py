"""Pydantic models and zone validation rules shared by both roles."""
from __future__ import annotations

import ipaddress
import re
from typing import List, Optional

from pydantic import (
    AliasChoices,
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
    model_validator,
)

from .config import settings

_LABEL = re.compile(r"^[a-z0-9]([a-z0-9-]*[a-z0-9])?$")


def _normalise_name(value: str) -> str:
    if not isinstance(value, str):
        raise ValueError("name must be a string")
    return value.strip().rstrip(".").lower()


def _check_labels(owner: str, what: str) -> None:
    labels = owner.split(".") if owner else []
    if any(not _LABEL.match(label) for label in labels):
        raise ValueError(f"invalid {what}: {owner!r}")


class RecordInput(BaseModel):
    name: str = Field(..., examples=["db.internal.local"])
    ip: str = Field(..., examples=["10.0.0.11"])

    @field_validator("name")
    @classmethod
    def _strip_name(cls, v: str) -> str:
        return _normalise_name(v)

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


class DelegationInput(BaseModel):
    """One child-zone delegation carried inside the parent zone snapshot.

    Canonical field names are ``name`` (child apex), ``ns`` (nameserver
    owner, which must live inside the child) and ``glue`` (IPv4 address of
    that nameserver). A few common aliases are accepted as well.
    """

    model_config = ConfigDict(populate_by_name=True)

    name: str = Field(
        ...,
        examples=["dev.internal.local"],
        validation_alias=AliasChoices(
            "name", "subzone", "child_zone", "delegated_zone",
            "delegated_subzone", "zone",
        ),
    )
    ns: str = Field(
        ...,
        examples=["ns1.dev.internal.local"],
        validation_alias=AliasChoices(
            "ns", "ns_name", "nameserver", "nameserver_name", "target"
        ),
    )
    glue: str = Field(
        ...,
        examples=["10.0.0.53"],
        validation_alias=AliasChoices(
            "glue", "glue_ip", "ns_ip", "address"
        ),
    )

    @field_validator("name", "ns")
    @classmethod
    def _strip_dns_name(cls, v: str) -> str:
        return _normalise_name(v)

    @field_validator("glue")
    @classmethod
    def _valid_glue(cls, v: str) -> str:
        try:
            ip = ipaddress.ip_address(v.strip())
        except ValueError as exc:
            raise ValueError(f"invalid glue address: {v}") from exc
        if ip.version != 4:
            raise ValueError("only IPv4 glue is supported")
        return str(ip)


class ZonePayload(BaseModel):
    """A complete replacement of the fixed zone (max 8 A records).

    ``delegation`` is optional. When present it must describe exactly one
    child zone inside the fixed zone, and no parent A record may live at or
    below that child. Validation happens before storage is touched, so a
    conflicting submission can never change the serial or active zone.
    """

    model_config = ConfigDict(populate_by_name=True)

    records: List[RecordInput]
    delegation: Optional[DelegationInput] = Field(
        default=None,
        validation_alias=AliasChoices("delegation", "delegated_subzone"),
    )

    @model_validator(mode="before")
    @classmethod
    def _accept_flat_delegation(cls, data):
        """Normalise the accepted delegation shapes to one nested object.

        Accepted: ``"delegation": {name,ns,glue}``, a dict under
        ``subzone``/``child_zone``/``delegated_subzone``, or the same
        values written flat (``delegation_name``/``subzone``/``ns``/
        ``glue`` ...). Anything left over still goes through the full
        single-delegation validation below; there is no way to express two
        child delegations.
        """
        if not isinstance(data, dict):
            return data
        merged = dict(data)
        flat_keys = ("delegation_name", "delegated_subzone_name",
                     "subzone", "subzone_name", "child_zone", "ns",
                     "ns_name", "nameserver", "nameserver_name", "glue",
                     "glue_ip", "ns_glue", "ns_ip")
        has_flat = any(
            not isinstance(merged.get(key), dict) and merged.get(key)
            is not None
            for key in flat_keys
        )
        has_nested_alias = any(
            isinstance(merged.get(key), dict)
            for key in ("delegated_subzone", "subzone", "child_zone")
        )
        if merged.get("delegation") is not None:
            if has_flat or has_nested_alias:
                raise ValueError(
                    "ambiguous delegation: send exactly one delegation "
                    "object (nested 'delegation' or flat fields)"
                )
            return merged
        for key in ("delegated_subzone", "subzone", "child_zone"):
            value = merged.get(key)
            if isinstance(value, dict):
                merged["delegation"] = value
                merged.pop(key, None)
                break
        if merged.get("delegation") is not None:
            if has_flat:
                raise ValueError(
                    "ambiguous delegation: send either the nested "
                    "'delegation' object or the flat fields, not both"
                )
            return merged

        def pick(*keys):
            for key in keys:
                value = merged.get(key)
                if isinstance(value, str) and value:
                    return value
            return None

        name = pick("delegation_name", "delegated_subzone_name",
                    "subzone", "subzone_name", "child_zone")
        ns = pick("ns", "ns_name", "nameserver", "nameserver_name")
        glue = pick("glue", "glue_ip", "ns_glue", "ns_ip")
        if name is None and ns is None and glue is None:
            return merged
        merged["delegation"] = {"name": name, "ns": ns, "glue": glue}
        for key in ("delegation_name", "delegated_subzone_name",
                    "subzone", "subzone_name", "child_zone", "ns",
                    "ns_name", "nameserver", "nameserver_name", "glue",
                    "glue_ip", "ns_glue", "ns_ip"):
            merged.pop(key, None)
        return merged

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
            _check_labels(owner, "owner name")
            if name in seen:
                raise ValueError(f"duplicate A record name: {name!r}")
            seen.add(name)

        if self.delegation is not None:
            self._validate_delegation(zone, suffix)
        return self

    def _validate_delegation(self, zone: str, suffix: str) -> None:
        delegation = self.delegation
        assert delegation is not None
        child = delegation.name
        if child == zone or not child.endswith(suffix):
            raise ValueError(
                f"delegation {child!r} must be a sub-zone of {zone!r}"
            )
        child_owner = child[: -len(suffix)]
        _check_labels(child_owner, "delegation name")

        ns = delegation.ns
        if not (ns == child or ns.endswith("." + child)):
            raise ValueError(
                f"nameserver {ns!r} must be inside delegated sub-zone "
                f"{child!r}"
            )
        ns_owner = ns[: -(len(child) + 1)] if ns != child else ""
        _check_labels(ns_owner, "nameserver name")

        # Parent-side A data must not intrude into the delegated child.
        for rec in self.records:
            if rec.name == child or rec.name.endswith("." + child):
                raise ValueError(
                    f"A record {rec.name!r} conflicts with delegation "
                    f"{child!r}"
                )

    def sorted_records(self) -> List[RecordInput]:
        return sorted(self.records, key=lambda r: r.name)

    def delegation_dict(self) -> Optional[dict]:
        return (
            self.delegation.model_dump()
            if self.delegation is not None
            else None
        )


class RecordOut(BaseModel):
    name: str
    ip: str


class DelegationOut(BaseModel):
    name: str
    ns: str
    glue: str


class ZoneOut(BaseModel):
    zone: str
    serial: int
    records: List[RecordOut]
    delegation: Optional[DelegationOut] = None
    published_at: Optional[str] = None


class VersionOut(BaseModel):
    role: str
    zone: str
    serial: int
    record_count: int
    delegation_name: Optional[str] = None
    published_at: Optional[str] = None
    last_sync_at: Optional[str] = None
    last_sync_status: str = "never"
    last_sync_error: Optional[str] = None
    upstream_serial: Optional[int] = None


class PublishResult(BaseModel):
    ok: bool
    serial: int
    record_count: int
    delegation: Optional[DelegationOut] = None
    published_at: str
