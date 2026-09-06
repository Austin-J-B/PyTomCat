"""Regression tests for the public UI trust boundaries."""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tomcat.web_security import (  # noqa: E402
    oauth_redirect_is_allowed,
    origin_is_allowed,
    redact_oauth_message,
    trusted_client_ip,
)


def main() -> int:
    allowed = {"https://ui.catsofuta.org", "http://localhost:8080"}
    assert origin_is_allowed(None, allowed)
    assert origin_is_allowed("https://ui.catsofuta.org", allowed)
    assert not origin_is_allowed("https://evil.example", allowed)
    assert not origin_is_allowed("https://evil.example", {"*"})

    assert oauth_redirect_is_allowed("", allowed, set())
    assert oauth_redirect_is_allowed("https://ui.catsofuta.org/", allowed, set())
    assert oauth_redirect_is_allowed("http://localhost:8080/", allowed, set())
    assert not oauth_redirect_is_allowed("https://evil.example/", allowed, set())
    assert not oauth_redirect_is_allowed("https://ui.catsofuta.org/?next=evil", allowed, set())
    assert not oauth_redirect_is_allowed("https://user@ui.catsofuta.org/", allowed, set())
    assert oauth_redirect_is_allowed(
        "https://ui.catsofuta.org/callback",
        allowed,
        {"https://ui.catsofuta.org/callback"},
    )
    assert not oauth_redirect_is_allowed(
        "https://ui.catsofuta.org/other",
        allowed,
        {"https://ui.catsofuta.org/callback"},
    )

    assert trusted_client_ip("127.0.0.1", "203.0.113.8") == "203.0.113.8"
    assert trusted_client_ip("::1", "2001:db8::8") == "2001:db8::8"
    assert trusted_client_ip("198.51.100.4", "203.0.113.8") == "198.51.100.4"
    assert trusted_client_ip("127.0.0.1", "not-an-ip") == "127.0.0.1"

    assert redact_oauth_message("TomCat, auth url http://localhost/?code=secret") == "[REDACTED: OAuth callback]"
    assert redact_oauth_message("ordinary club message") == "ordinary club message"
    print("web security helpers: all checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
