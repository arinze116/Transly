"""Validation for untrusted media URLs."""

import asyncio
import ipaddress
import socket
from urllib.parse import urlparse


class UnsafeURL(ValueError):
    pass


def validate_url(value: str) -> str:
    parsed = urlparse(value)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise UnsafeURL("Only http and https URLs are supported")
    host = parsed.hostname.lower().rstrip(".")
    if host in {"localhost", "metadata.google.internal"}:
        raise UnsafeURL("Private hosts are not allowed")
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        address = None
    if address is not None and (not address.is_global):
        raise UnsafeURL("Private addresses are not allowed")
    return value


async def validate_resolved_host(value: str) -> str:
    value = validate_url(value)
    host = urlparse(value).hostname
    try:
        addresses = await asyncio.to_thread(socket.getaddrinfo, host, None, type=socket.SOCK_STREAM)
    except OSError as exc:
        raise UnsafeURL("Host could not be resolved") from exc
    for item in addresses:
        address = ipaddress.ip_address(item[4][0])
        if not address.is_global:
            raise UnsafeURL("URL resolves to a private address")
    return value
