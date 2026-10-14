"""
Tests for the parts of scrape.py that don't need the network or the API:
the HTML reduction and the post-extraction validation.

    python -m pytest tests/ -q      (or: python tests/test_parsing.py)
"""

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import urllib.robotparser  # noqa: E402

from scrape import (  # noqa: E402
    TARGETS,
    USER_AGENT,
    _as_number,
    _normalise_bonus,
    extract_bonus_text,
    validate,
)

# The relevant part of AskGamblers' robots.txt as served on 2026-10-14. Pinned here
# so the permission check is testable without the network.
ASKGAMBLERS_ROBOTS = """
User-agent: *
Disallow: /api
Disallow: /page/review/download/
Disallow: /r-v-
Disallow: /r-d-
Disallow: /admin/
Disallow: /*visit/
Disallow: /download/
Disallow: /search/
Disallow: /quick-search/
Disallow: /*?q=
Disallow: /html/
Disallow: /your-country-is-restricted
Disallow: /screenshots
Disallow: /cdn-cgi/
Disallow: /tags/
Disallow: /*?*

Sitemap: https://www.askgamblers.com/sitemapindex.xml
"""

FIXTURE = """
<html><body>
<nav><a href="/casino-bonuses">Casino Bonuses</a><a href="/latest">Latest</a></nav>
<header>THIS WEEK'S HOTTEST CASINOS</header>
<script>var tracking = {"wagering":"999x"};</script>
<main>
  <h1>100% up to &euro;500 + 100 Free Spins at Rokubet Casino, 1st Deposit</h1>
  <div>Minimum Deposit &euro;15</div>
  <div>Wagering Requirements 40xb</div>
  <div>Bonus Percentage 100%</div>
  <div>Maximum Bonus Amount &euro;500</div>
  <h2>General Information</h2>
  <ul>
    <li>You can withdraw your winnings only after the bonus has been wagered 40 times.</li>
    <li>Deposit at least &euro;15 to qualify for the offer.</li>
  </ul>
</main>
<footer>&copy; 2026 AskGamblers.com</footer>
</body></html>
"""


def test_extracts_headline():
    headline, _ = extract_bonus_text(FIXTURE)
    assert "Rokubet Casino" in headline
    assert "100%" in headline


def test_drops_chrome_and_scripts():
    _, text = extract_bonus_text(FIXTURE)
    assert "999x" not in text, "script contents leaked into the model input"
    assert "HOTTEST CASINOS" not in text
    assert "AskGamblers.com" not in text


def test_keeps_the_fields_that_matter():
    _, text = extract_bonus_text(FIXTURE)
    for expected in ("Minimum Deposit", "40xb", "wagered 40 times"):
        assert expected in text


def test_validate_accepts_a_good_record():
    record = {
        "casino_name": "Rokubet Casino",
        "welcome_bonus": "100% up to 500 EUR, + 100 free spins",
        "wagering_multiplier": 40,
        "wagering_basis": "bonus",
        "min_deposit_amount": 15,
    }
    url = "https://www.askgamblers.com/casino-bonuses/latest/rokubet-casino-bonus-100percent-up-to-500-eur-100-free-spins-2026-10-05"
    assert validate(record, url) == []


def test_validate_catches_the_wrong_bonus_on_the_page():
    """The real failure mode: the model reads a neighbouring offer card."""
    record = {
        "casino_name": "Rokubet Casino",
        "welcome_bonus": "110% up to 750 EUR",  # this is the 2nd-deposit offer
        "wagering_multiplier": 40,
        "wagering_basis": "bonus",
        "min_deposit_amount": 15,
    }
    url = "https://www.askgamblers.com/casino-bonuses/latest/rokubet-casino-bonus-100percent-up-to-500-eur-100-free-spins-2026-10-05"
    problems = validate(record, url)
    assert any("cross-contamination" in p for p in problems)


def _robots():
    parser = urllib.robotparser.RobotFileParser()
    parser.parse(ASKGAMBLERS_ROBOTS.splitlines())
    return parser


def test_robots_allows_every_target():
    """
    Regression guard. A first version fetched robots.txt via RobotFileParser.read(),
    which uses urllib's own user agent; the CDN 403s it, read() swallows the error
    and sets disallow_all, and every URL came back 'disallowed' for no real reason.
    """
    parser = _robots()
    for path in TARGETS:
        url = "https://www.askgamblers.com" + path
        assert parser.can_fetch(USER_AGENT, url), f"should be allowed: {url}"


def test_robots_still_blocks_what_it_should():
    parser = _robots()
    for blocked in (
        "https://www.askgamblers.com/admin/",
        "https://www.askgamblers.com/search/",
        "https://www.askgamblers.com/api",
    ):
        assert not parser.can_fetch(USER_AGENT, blocked), f"should be blocked: {blocked}"


def test_as_number_handles_what_small_models_return():
    """Local models return '40xb' or '€15' where the prompt asked for a number."""
    assert _as_number(40) == 40
    assert _as_number("40xb") == 40
    assert _as_number("€15") == 15
    assert _as_number("15 EUR") == 15
    assert _as_number("1,000") == 1000
    assert _as_number("19.99") == 19.99
    assert _as_number(None) is None
    assert _as_number("N/A") is None


def test_ollama_host_normalisation():
    """
    Ollama uses OLLAMA_HOST for the address its server BINDS to, so on a
    configured machine it often holds "0.0.0.0" rather than a URL. 0.0.0.0 is
    not a valid destination for a client.
    """
    import importlib

    import scrape as scrape_module

    cases = {
        "0.0.0.0": "http://127.0.0.1:11434",
        "0.0.0.0:11434": "http://127.0.0.1:11434",
        ":11434": "http://localhost:11434",
        "localhost": "http://localhost:11434",
        "": "http://localhost:11434",
        "http://192.168.1.50:11434/": "http://192.168.1.50:11434",
    }
    original = os.environ.get("OLLAMA_HOST")
    try:
        for value, expected in cases.items():
            os.environ["OLLAMA_HOST"] = value
            importlib.reload(scrape_module)
            got = scrape_module.ollama_base_url()
            assert got == expected, f"{value!r} gave {got}, expected {expected}"
    finally:
        if original is None:
            os.environ.pop("OLLAMA_HOST", None)
        else:
            os.environ["OLLAMA_HOST"] = original
        importlib.reload(scrape_module)


def test_bonus_currency_is_normalised():
    """One prompt, three renderings - so the shape is enforced in code."""
    assert _normalise_bonus("100% up to €500 + 100 Free Spins") == (
        "100% up to 500 EUR + 100 free spins"
    )
    assert _normalise_bonus("250% up to EUR1000 + 250 Free Spins") == (
        "250% up to 1000 EUR + 250 free spins"
    )
    assert _normalise_bonus("150% up to $400 + 60 Free Spins") == (
        "150% up to 400 USD + 60 free spins"
    )
    # Already correct, and an unlisted currency: both left alone.
    assert _normalise_bonus("200% up to 1000000 COP + 100 free spins") == (
        "200% up to 1000000 COP + 100 free spins"
    )
    assert _normalise_bonus(None) is None


def test_validate_catches_implausible_numbers():
    record = {
        "casino_name": "X",
        "welcome_bonus": "",
        "wagering_multiplier": 4000,
        "wagering_basis": "sideways",
        "min_deposit_amount": 0,
    }
    problems = validate(record, "https://example.com/x")
    assert len(problems) == 3


if __name__ == "__main__":
    failures = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"  ok   {name}")
            except AssertionError as exc:
                failures += 1
                print(f"  FAIL {name}: {exc}")
    print("all passed" if not failures else f"{failures} failed")
    sys.exit(1 if failures else 0)
