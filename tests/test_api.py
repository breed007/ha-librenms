"""Tests for the LibreNMS API client helpers."""

from __future__ import annotations

import pytest

from custom_components.librenms.api import normalize_url


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("https://librenms.example.com", "https://librenms.example.com"),
        ("https://librenms.example.com/", "https://librenms.example.com"),
        ("https://librenms.example.com///", "https://librenms.example.com"),
        ("  https://librenms.example.com  ", "https://librenms.example.com"),
        ("librenms.example.com", "https://librenms.example.com"),
        ("http://10.0.0.5:8000", "http://10.0.0.5:8000"),
        ("https://EXAMPLE.com/librenms", "https://example.com/librenms"),
        ("https://example.com/librenms/", "https://example.com/librenms"),
        # Users routinely paste the API path they read in the docs.
        ("https://example.com/api/v0", "https://example.com"),
        ("https://example.com/librenms/api/v0/", "https://example.com/librenms"),
    ],
)
def test_normalize_url(raw: str, expected: str) -> None:
    """A pasted URL is reduced to a canonical base."""
    assert normalize_url(raw) == expected


@pytest.mark.parametrize("raw", ["", "   ", "ftp://example.com", "https://"])
def test_normalize_url_rejects_bad_input(raw: str) -> None:
    """Unusable URLs raise rather than producing a broken client."""
    with pytest.raises(ValueError):
        normalize_url(raw)


def test_device_url() -> None:
    """The device deep link points at the LibreNMS web UI."""
    from custom_components.librenms.api import LibreNMSClient

    client = LibreNMSClient(None, "https://example.com/librenms/", "token")
    assert client.base_url == "https://example.com/librenms"
    assert client.device_url(7) == "https://example.com/librenms/device/device=7/"
