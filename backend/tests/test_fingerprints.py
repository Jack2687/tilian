"""Fingerprint generation guards.

These cover the `generate()` contract directly.  The worker test in
`test_engine_live_protocol.py` monkeypatches `fingerprints.generate`, so it
verifies the *intent* passed to the generator and cannot catch a generator that
silently discards its arguments.  That is exactly the bug these tests pin:
`generate()` used to overwrite its own `impersonate` parameter, so the UPI
worker's profile rotation never reached the engine and ~3/5 of tasks died with
"impersonate profile ... does not belong to kernel=firefox" before any request
was sent.
"""

from __future__ import annotations

import pytest

from app import fingerprints


def test_generate_honors_the_requested_impersonate_target() -> None:
    """The requested TLS target must round-trip, not degrade to a random pick."""
    for target in ("firefox133", "firefox147", "chrome146", "chrome150", "chrome136"):
        kernel = "firefox" if target.startswith("firefox") else "chrome"
        for _ in range(5):
            profile = fingerprints.generate("IN", channel="upi", kernel=kernel, impersonate=target)
            assert profile["impersonate"] == target
            assert profile["kernel"] == kernel


def test_generate_pairs_the_ua_with_the_requested_target() -> None:
    """A firefox TLS target must not carry a chrome UA (or a different major)."""
    profile = fingerprints.generate("IN", kernel="firefox", impersonate="firefox133")
    assert "Firefox/133.0" in profile["ua"]
    assert profile["sec_ch_ua"] == ""

    chrome = fingerprints.generate("IN", kernel="chrome", impersonate="chrome146")
    assert "Chrome/146.0.0.0" in chrome["ua"]
    assert '"146"' in chrome["sec_ch_ua"]


def test_generate_supports_the_git5_macos_profiles() -> None:
    """Every profile in the UPI rail's macOS set must resolve to a macOS identity."""
    for target in fingerprints.UPI_GIT5_MACOS_PROFILES:
        kernel = "firefox" if target.startswith("firefox") else "chrome"
        profile = fingerprints.generate(
            "IN", channel="upi", kernel=kernel, impersonate=target, platform="macOS"
        )
        # The -macos suffix describes the platform; it must be stripped for the
        # value handed to curl_cffi.
        assert profile["impersonate"] == target.removesuffix("-macos")
        assert profile["platform_os"] == "macOS"
        assert profile["platform"] == "MacIntel"
        assert "Macintosh" in profile["ua"]
        if kernel == "chrome":
            assert profile["sec_ch_ua_platform"] == '"macOS"'
        else:
            assert profile["sec_ch_ua"] == ""


def test_generate_keeps_windows_platform_without_a_macos_request() -> None:
    """Non-UPI channels must not silently start looking like macOS."""
    profile = fingerprints.generate("IN", kernel="firefox", impersonate="firefox133")
    assert profile["platform_os"] == "Windows"
    assert profile["platform"] == "Win32"
    assert "Windows NT" in profile["ua"]


def test_generate_rejects_a_target_from_another_kernel_family() -> None:
    """A chrome target requested under the firefox kernel falls back in-family.

    Returning `chrome146` with `kernel="firefox"` would pair a Chrome UA with a
    Firefox TLS profile; the engine rejects that combination outright.
    """
    profile = fingerprints.generate("IN", kernel="firefox", impersonate="chrome146")
    assert profile["kernel"] == "firefox"
    assert profile["impersonate"] in fingerprints.FIREFOX_VARIANTS


def test_unrequested_kernel_still_draws_from_its_own_family() -> None:
    """With no explicit target the pool stays in-family."""
    for _ in range(10):
        chrome = fingerprints.generate("IN", kernel="chrome")
        assert chrome["impersonate"] in fingerprints.CHROME_TARGETS
        firefox = fingerprints.generate("IN", kernel="firefox")
        assert firefox["impersonate"] in fingerprints.FIREFOX_VARIANTS
