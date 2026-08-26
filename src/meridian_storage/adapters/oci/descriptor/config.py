# SPDX-License-Identifier: Apache-2.0
"""Private binding, credential, and limit configuration."""

from __future__ import annotations

import base64
import hashlib
import re
import secrets
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Protocol, cast, runtime_checkable
from urllib.parse import urlsplit, urlunsplit

from meridian_storage import ResourceRef

_REPOSITORY_RE = re.compile(
    r"[a-z0-9]+(?:(?:\.|_|__|-+)[a-z0-9]+)*"
    r"(?:/[a-z0-9]+(?:(?:\.|_|__|-+)[a-z0-9]+)*)*\Z"
)
_CONDITIONAL_MODES = {"single-writer", "registry-enforced"}


@runtime_checkable
class CredentialProvider(Protocol):
    """Resolve one current Authorization value without exposing the secret."""

    def authorization_header(self) -> str | None: ...


@dataclass(frozen=True, slots=True)
class AnonymousCredentials:
    def authorization_header(self) -> None:
        return None


@dataclass(frozen=True, slots=True)
class BasicCredentials:
    username: str
    password: str = field(repr=False)

    def __post_init__(self) -> None:
        if not self.username or any(character in self.username for character in "\r\n"):
            raise ValueError("basic-auth username must be non-empty and single-line")
        if not self.password or any(character in self.password for character in "\r\n"):
            raise ValueError("basic-auth password must be non-empty and single-line")

    def authorization_header(self) -> str:
        token = base64.b64encode(f"{self.username}:{self.password}".encode()).decode("ascii")
        return f"Basic {token}"


@dataclass(frozen=True, slots=True)
class BearerCredentials:
    token: str = field(repr=False)

    def __post_init__(self) -> None:
        if not self.token or any(character in self.token for character in "\r\n"):
            raise ValueError("bearer token must be non-empty and single-line")

    def authorization_header(self) -> str:
        return f"Bearer {self.token}"


@dataclass(frozen=True, slots=True)
class CallbackCredentials:
    resolver: Callable[[], str | None] = field(repr=False)

    def authorization_header(self) -> str | None:
        value = self.resolver()
        if value is not None and (
            not isinstance(value, str)
            or not value
            or any(character in value for character in "\r\n")
        ):
            raise ValueError("resolved Authorization header must be non-empty and single-line")
        return value


@dataclass(frozen=True, slots=True)
class OciDistributionBinding:
    """One IaC-owned association from a logical Object Resource to an OCI repository."""

    resource: ResourceRef | str | Mapping[str, object]
    endpoint: str
    repository: str
    credentials: CredentialProvider = field(default_factory=AnonymousCredentials, repr=False)
    verify_tls: bool | str = True
    allow_insecure_http: bool = False
    timeout_seconds: float = 30.0
    chunk_size: int = 4 * 1024 * 1024
    max_object_bytes: int = 5 * 1024**4
    max_range_bytes: int = 256 * 1024 * 1024
    max_manifest_bytes: int = 4 * 1024 * 1024
    max_metadata_bytes: int = 512 * 1024
    max_list_page_size: int = 1000
    max_scan_tags: int = 2000
    max_multipart_parts: int = 10_000
    conditional_create_mode: str = "single-writer"
    deletion_enabled: bool = False
    retention_enforced: bool = False
    referrers_required: bool = False
    cursor_signing_key: bytes | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        resource = ResourceRef.parse(self.resource, catalog="object")
        endpoint = _endpoint(self.endpoint, allow_http=self.allow_insecure_http)
        if _REPOSITORY_RE.fullmatch(self.repository) is None:
            raise ValueError("repository does not match the OCI Distribution name grammar")
        if not isinstance(self.credentials, CredentialProvider):
            raise TypeError("credentials must implement CredentialProvider")
        if not isinstance(self.verify_tls, bool) and not (
            isinstance(self.verify_tls, str) and self.verify_tls
        ):
            raise TypeError("verify_tls must be a boolean or non-empty CA-bundle path")
        _positive_number(self.timeout_seconds, "timeout_seconds")
        _bounded_int(self.chunk_size, "chunk_size", minimum=64 * 1024, maximum=16 * 1024 * 1024)
        _bounded_int(self.max_object_bytes, "max_object_bytes", minimum=1)
        _bounded_int(self.max_range_bytes, "max_range_bytes", minimum=1)
        _bounded_int(self.max_manifest_bytes, "max_manifest_bytes", minimum=1024)
        _bounded_int(self.max_metadata_bytes, "max_metadata_bytes", minimum=1024)
        _bounded_int(
            self.max_list_page_size,
            "max_list_page_size",
            minimum=1,
            maximum=1000,
        )
        _bounded_int(self.max_scan_tags, "max_scan_tags", minimum=self.max_list_page_size)
        _bounded_int(
            self.max_multipart_parts,
            "max_multipart_parts",
            minimum=1,
            maximum=100_000,
        )
        if self.conditional_create_mode not in _CONDITIONAL_MODES:
            raise ValueError("conditional_create_mode must be single-writer or registry-enforced")
        for name in (
            "allow_insecure_http",
            "deletion_enabled",
            "retention_enforced",
            "referrers_required",
        ):
            if not isinstance(getattr(self, name), bool):
                raise TypeError(f"{name} must be boolean")
        cursor_key = self.cursor_signing_key
        if cursor_key is None:
            cursor_key = secrets.token_bytes(32)
        if not isinstance(cursor_key, bytes) or len(cursor_key) < 16:
            raise ValueError("cursor_signing_key must contain at least 16 bytes")
        object.__setattr__(self, "resource", resource)
        object.__setattr__(self, "endpoint", endpoint)
        object.__setattr__(self, "cursor_signing_key", cursor_key)

    @property
    def origin(self) -> tuple[str, str, int | None]:
        value = urlsplit(self.endpoint)
        return value.scheme, value.hostname or "", value.port

    @property
    def resource_ref(self) -> ResourceRef:
        """Return the normalized resource established during initialization."""

        return cast(ResourceRef, self.resource)

    @property
    def public_fingerprint(self) -> str:
        """A non-secret identity for diagnostics; it excludes physical binding fields."""

        value = (
            f"{self.resource_ref.canonical}|{self.conditional_create_mode}|{self.deletion_enabled}"
        )
        return f"sha256:{hashlib.sha256(value.encode()).hexdigest()}"


def _endpoint(value: str, *, allow_http: bool) -> str:
    if not isinstance(value, str):
        raise TypeError("endpoint must be a string")
    parsed = urlsplit(value)
    try:
        _ = parsed.port
    except ValueError as exc:
        raise ValueError("endpoint contains an invalid port") from exc
    if parsed.scheme not in ({"https", "http"} if allow_http else {"https"}):
        raise ValueError("endpoint must use HTTPS unless insecure HTTP is explicitly allowed")
    if not parsed.hostname or parsed.username is not None or parsed.password is not None:
        raise ValueError("endpoint must contain a host and no userinfo")
    if parsed.query or parsed.fragment or parsed.path not in {"", "/"}:
        raise ValueError("endpoint cannot contain a path, query, or fragment")
    return urlunsplit((parsed.scheme, parsed.netloc, "", "", "")).rstrip("/")


def _positive_number(value: float, name: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int | float) or value <= 0:
        raise ValueError(f"{name} must be positive")


def _bounded_int(value: int, name: str, *, minimum: int, maximum: int | None = None) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise ValueError(f"{name} must be an integer of at least {minimum}")
    if maximum is not None and value > maximum:
        raise ValueError(f"{name} must not exceed {maximum}")


__all__ = [
    "AnonymousCredentials",
    "BasicCredentials",
    "BearerCredentials",
    "CallbackCredentials",
    "CredentialProvider",
    "OciDistributionBinding",
]
