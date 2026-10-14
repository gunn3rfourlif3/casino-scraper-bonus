#!/usr/bin/env python3
"""
Casino bonus scraper.

Pulls three welcome bonuses from AskGamblers (one public source), uses Claude to
turn the free-text bonus terms into structured fields, and writes data/bonuses.json.
Then regenerates index.html from that JSON.

Usage:
    python scrape.py --provider ollama        # local model, no API key
    ANTHROPIC_API_KEY=sk-ant-... python scrape.py

Options:
    --provider P   anthropic (default) or ollama
    --check-robots report robots.txt permission for each target, then exit
    --no-build     write the JSON but skip regenerating index.html
    --html-dir DIR parse saved .html fixtures instead of hitting the network
                   (used by tests; filenames must match the slugs in TARGETS)
"""

import argparse
import json
import os
import re
import sys
import time
import urllib.robotparser
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

import requests
from bs4 import BeautifulSoup

# --- configuration -----------------------------------------------------------

SOURCE = "https://www.askgamblers.com"

# Three casinos, one source. These are AskGamblers' own bonus-detail pages.
TARGETS = [
    "/casino-bonuses/latest/rokubet-casino-bonus-100percent-up-to-500-eur-100-free-spins-2026-10-05",
    "/casino-bonuses/latest/betbeast-casino-bonus-250percent-up-to-1000-eur-250-free-spins-2026-10-13",
    "/casino-bonuses/latest/spinlander-casino-bonus-120percent-up-to-888-eur-2026-10-13",
]

# Identify the bot honestly and leave a contact. Swap the email before real use.
USER_AGENT = "locare-screening-scraper/1.0 (+contact: you@example.com)"

REQUEST_DELAY_SECONDS = 3.0
REQUEST_TIMEOUT_SECONDS = 30
# Which model does the extraction step.
#   anthropic - the Claude API, needs ANTHROPIC_API_KEY
#   ollama    - a model running locally, needs nothing but Ollama on this machine
PROVIDER = os.environ.get("PROVIDER", "anthropic")
ANTHROPIC_MODEL = os.environ.get("MODEL", "claude-haiku-5-5")
OLLAMA_MODEL = os.environ.get("OLLAMA_MODEL", "llama3.1:8b")
OLLAMA_HOST = os.environ.get("OLLAMA_HOST", "http://localhost:11434")


def ollama_base_url() -> str:
    """
    Turn whatever is in OLLAMA_HOST into a URL we can actually call.

    Ollama uses that same variable for the address its server BINDS to, so on a
    machine where Ollama is configured it often holds "0.0.0.0" or ":11434" —
    not a URL. 0.0.0.0 means "every interface" to a listener and is not a valid
    destination for a client, so it becomes 127.0.0.1 here.
    """
    raw = (OLLAMA_HOST or "").strip().rstrip("/") or "http://localhost:11434"

    if "://" not in raw:
        raw = "http://" + raw.lstrip(":") if not raw.startswith(":") else "http://localhost" + raw

    parts = urlparse(raw)
    host = parts.hostname or "localhost"
    if host in ("0.0.0.0", "::", "[::]"):
        host = "127.0.0.1"
    port = parts.port or 11434
    return f"{parts.scheme}://{host}:{port}"


def active_model() -> str:
    return OLLAMA_MODEL if PROVIDER == "ollama" else ANTHROPIC_MODEL

OUT_JSON = Path("data/bonuses.json")

# How much page text to hand the model. Enough to cover the detail block and the
# "General Information" bullets, short enough to stay cheap.
TEXT_BUDGET = 3500


# --- fetching ----------------------------------------------------------------


_ROBOTS_CACHE: dict[str, urllib.robotparser.RobotFileParser | None] = {}


def load_robots(root: str) -> urllib.robotparser.RobotFileParser | None:
    """
    Fetch and parse robots.txt for a site root. None means "could not read it".

    Deliberately not RobotFileParser.read(): that fetches with urllib's own user
    agent, which sites behind a CDN routinely 403. Worse, read() swallows the
    HTTPError and quietly sets disallow_all, so every can_fetch() then returns
    False and the run looks like a polite refusal rather than a broken fetch.
    We fetch with our own UA and let a failure be visibly a failure.
    """
    if root in _ROBOTS_CACHE:
        return _ROBOTS_CACHE[root]

    robots_url = f"{root}/robots.txt"
    parser = urllib.robotparser.RobotFileParser()

    try:
        response = requests.get(
            robots_url,
            headers={"User-Agent": USER_AGENT, "Accept": "text/plain"},
            timeout=REQUEST_TIMEOUT_SECONDS,
        )
    except requests.RequestException as exc:
        print(f"  ! could not fetch {robots_url}: {exc}", file=sys.stderr)
        _ROBOTS_CACHE[root] = None
        return None

    if response.status_code == 404:
        # No robots.txt at all means nothing is disallowed.
        parser.parse([])
    elif response.status_code == 200:
        parser.parse(response.text.splitlines())
    else:
        print(
            f"  ! {robots_url} returned HTTP {response.status_code} - "
            "cannot confirm permission, so not crawling",
            file=sys.stderr,
        )
        _ROBOTS_CACHE[root] = None
        return None

    _ROBOTS_CACHE[root] = parser
    return parser


def robots_allows(url: str) -> bool:
    """Check robots.txt for our user agent before fetching anything."""
    parts = urlparse(url)
    robots = load_robots(f"{parts.scheme}://{parts.netloc}")
    if robots is None:
        return False
    return robots.can_fetch(USER_AGENT, url)


def fetch(url: str) -> str:
    response = requests.get(
        url,
        headers={"User-Agent": USER_AGENT, "Accept": "text/html"},
        timeout=REQUEST_TIMEOUT_SECONDS,
    )
    if response.status_code in (401, 403, 429) or response.status_code >= 500:
        # A block, a rate limit or an outage. Do not retry harder, do not work
        # around it: stop and report so a human can pick a different source.
        raise RuntimeError(
            f"{url} returned HTTP {response.status_code} - treating as a block, not retrying"
        )
    response.raise_for_status()
    return response.text


# --- parsing -----------------------------------------------------------------


def extract_bonus_text(html: str) -> tuple[str, str]:
    """
    Reduce a bonus page to (headline, free-text block).

    Deliberately selector-light: strip the chrome, take the visible text, and
    start from the H1. Class names on a site like this churn; the shape of the
    content does not. The model does the interpreting.
    """
    soup = BeautifulSoup(html, "html.parser")

    for tag in soup(["script", "style", "nav", "header", "footer", "noscript", "svg"]):
        tag.decompose()

    h1 = soup.find("h1")
    headline = h1.get_text(" ", strip=True) if h1 else ""

    text = soup.get_text("\n", strip=True)
    text = re.sub(r"\n{2,}", "\n", text)

    if headline:
        # Start at the second occurrence if the H1 also appears in a breadcrumb.
        start = text.find(headline)
        if start > -1:
            text = text[start:]

    return headline, text[:TEXT_BUDGET]


# --- the LLM step ------------------------------------------------------------

EXTRACTION_PROMPT = """\
Below is the visible text of one bonus page from a casino comparison site.

Return ONLY a JSON object, no prose and no code fence, with exactly these keys:

  "casino_name"          string  - the casino, e.g. "Rokubet Casino"
  "welcome_bonus"        string  - the headline offer in the form
                                   "<percent>% up to <amount> <CURRENCY>" plus
                                   ", + N free spins" when free spins are included.
                                   Use the ISO currency code (EUR, USD, CAD), not the symbol.
  "wagering_multiplier"  integer - just the number from the wagering requirement.
                                   "40xb" -> 40. "25x(d+b)" -> 25. Null if none is stated.
  "wagering_basis"       string  - what the multiplier applies to, one of
                                   "bonus", "deposit+bonus", or "unknown".
                                   "xb" means bonus. "x(d+b)" means deposit+bonus.
  "min_deposit_amount"   number  - the minimum qualifying deposit, number only.
  "min_deposit_currency" string  - ISO code for that deposit, e.g. "EUR".
  "max_bonus_amount"     number  - the cap on the bonus, number only. Null if not stated.
  "free_spins"           integer - free spins included, 0 if none.

Rules:
- Use only what the page states. If a field is genuinely absent, use null.
- Do not infer a wagering requirement from a different bonus on the page. The page
  lists other offers from the same casino and similar offers from other casinos;
  ignore all of them and read only the bonus named in the headline.

Headline: {headline}

Page text:
{text}
"""


def _call_anthropic(prompt: str) -> str:
    try:
        from anthropic import Anthropic
    except ImportError:
        sys.exit("anthropic package missing - run: pip install -r requirements.txt")

    if not os.environ.get("ANTHROPIC_API_KEY"):
        sys.exit(
            "ANTHROPIC_API_KEY is not set - the extraction step needs it, "
            "or run with --provider ollama to use a local model"
        )

    client = Anthropic()
    message = client.messages.create(
        model=ANTHROPIC_MODEL,
        max_tokens=600,
        messages=[{"role": "user", "content": prompt}],
    )
    return message.content[0].text


def _call_ollama(prompt: str) -> str:
    """
    A local model through Ollama's HTTP API.

    format=json constrains the output to a JSON object. Small models otherwise
    tend to wrap the answer in prose and a code fence, and temperature 0 keeps
    repeated runs comparable - this is extraction, not writing.
    """
    def post(payload):
        try:
            return requests.post(
                f"{ollama_base_url()}/api/chat", json=payload, timeout=300
            )
        except requests.RequestException as exc:
            sys.exit(
                f"could not reach Ollama at {ollama_base_url()}: {exc}\n"
                "Is it running? Start it with: ollama serve"
            )

    payload = {
        "model": OLLAMA_MODEL,
        "messages": [{"role": "user", "content": prompt}],
        "format": "json",
        "stream": False,
        # Reasoning models (qwen3, deepseek-r1) emit a thinking block that would
        # land in front of the JSON. Turning it off keeps the response parseable;
        # there is nothing here to reason about, only fields to read off a page.
        "think": False,
        "options": {"temperature": 0},
    }

    response = post(payload)
    if response.status_code >= 400 and "think" in response.text.lower():
        # Model has no thinking mode at all, so Ollama rejects the parameter.
        payload.pop("think")
        response = post(payload)

    if response.status_code == 404:
        sys.exit(
            f"Ollama has no model called {OLLAMA_MODEL!r} - pull it first:\n"
            f"  ollama pull {OLLAMA_MODEL}"
        )
    response.raise_for_status()
    return response.json()["message"]["content"]


def _as_number(value):
    """
    Small local models often return "40xb", "€15" or "15 EUR" where the prompt
    asked for a number. Pull the first number out rather than failing the run.
    """
    if value is None or isinstance(value, (int, float)):
        return value
    match = re.search(r"\d+(?:\.\d+)?", str(value).replace(",", ""))
    if not match:
        return None
    number = float(match.group())
    return int(number) if number.is_integer() else number


CURRENCY_SYMBOLS = {"€": "EUR", "$": "USD", "£": "GBP", "¥": "JPY"}


def _normalise_bonus(text: str | None) -> str | None:
    """
    Put the headline offer into one consistent shape: "100% up to 500 EUR".

    Models render currency however the page did - "€500", "EUR1000", "500 EUR"
    all come back from the same prompt. Formatting is a rule, not a judgement,
    so it is enforced here rather than asked for and hoped for.
    """
    if not text:
        return text

    for symbol, code in CURRENCY_SYMBOLS.items():
        # "€500" and "€ 500" -> "500 EUR"
        text = re.sub(rf"{re.escape(symbol)}\s*([\d][\d,.]*)", rf"\1 {code}", text)

    # "EUR1000" / "EUR 1000" -> "1000 EUR"
    text = re.sub(
        r"\b(EUR|USD|GBP|CAD|AUD|NZD|JPY|CHF|SEK|NOK|PLN|BRL|MXN|ZAR)\s*([\d][\d,.]*)",
        r"\2 \1",
        text,
    )

    text = re.sub(r"\bFree Spins\b", "free spins", text)
    return re.sub(r"\s{2,}", " ", text).strip()


def extract_fields(headline: str, text: str) -> dict:
    """Hand the free text to a model and get structured fields back."""
    prompt = EXTRACTION_PROMPT.format(headline=headline, text=text)

    if PROVIDER == "ollama":
        raw = _call_ollama(prompt)
    elif PROVIDER == "anthropic":
        raw = _call_anthropic(prompt)
    else:
        sys.exit(f"unknown provider {PROVIDER!r} - use 'anthropic' or 'ollama'")

    raw = re.sub(r"^```(?:json)?|```$", "", raw.strip(), flags=re.MULTILINE).strip()
    try:
        fields = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"model did not return valid JSON: {exc}\n{raw[:400]}") from exc

    for key in ("wagering_multiplier", "min_deposit_amount", "max_bonus_amount", "free_spins"):
        fields[key] = _as_number(fields.get(key))
    for key in ("wagering_multiplier", "free_spins"):
        if fields[key] is not None:
            fields[key] = int(fields[key])

    fields["welcome_bonus"] = _normalise_bonus(fields.get("welcome_bonus"))

    return fields


# --- validation --------------------------------------------------------------


def validate(record: dict, url: str) -> list[str]:
    """
    Sanity-check what came back. An LLM that misreads a page usually fails loudly
    here rather than quietly writing a wrong number into the JSON.
    """
    problems = []

    if not record.get("casino_name"):
        problems.append("casino_name is empty")

    multiplier = record.get("wagering_multiplier")
    if multiplier is not None and not (0 <= multiplier <= 150):
        problems.append(f"wagering_multiplier {multiplier} outside plausible range 0-150")

    deposit = record.get("min_deposit_amount")
    if deposit is not None and not (0 < deposit <= 100_000):
        problems.append(f"min_deposit_amount {deposit} outside plausible range")

    if record.get("wagering_basis") not in ("bonus", "deposit+bonus", "unknown", None):
        problems.append(f"unexpected wagering_basis {record.get('wagering_basis')!r}")

    # The headline percentage should appear in the source URL slug. Cheap check
    # that the model read the right bonus and not a neighbouring card.
    bonus = record.get("welcome_bonus") or ""
    match = re.match(r"(\d+)%", bonus)
    if match and f"{match.group(1)}percent" not in url:
        problems.append(
            f"welcome_bonus starts {match.group(1)}% which is not in the source URL - "
            "possible cross-contamination from another offer on the page"
        )

    return problems


# --- main --------------------------------------------------------------------


def scrape_one(path: str, html_dir: Path | None) -> dict:
    url = SOURCE + path
    print(f"- {url}")

    if html_dir:
        slug = path.rsplit("/", 1)[-1]
        html = (html_dir / f"{slug}.html").read_text(encoding="utf-8")
    else:
        if not robots_allows(url):
            raise RuntimeError(f"robots.txt disallows {url} for {USER_AGENT}")
        html = fetch(url)
        time.sleep(REQUEST_DELAY_SECONDS)

    headline, text = extract_bonus_text(html)
    if not headline:
        raise RuntimeError(f"no H1 found on {url} - page layout may have changed")

    fields = extract_fields(headline, text)

    problems = validate(fields, url)
    if problems:
        for problem in problems:
            print(f"  ! {problem}", file=sys.stderr)

    return {
        "casino": fields.get("casino_name"),
        "welcome_bonus": fields.get("welcome_bonus"),
        "wagering_multiplier": fields.get("wagering_multiplier"),
        "wagering_basis": fields.get("wagering_basis"),
        "min_deposit": fields.get("min_deposit_amount"),
        "min_deposit_currency": fields.get("min_deposit_currency"),
        "max_bonus": fields.get("max_bonus_amount"),
        "free_spins": fields.get("free_spins"),
        "source_url": url,
        "validation_warnings": problems,
    }


def check_robots() -> int:
    """Report what robots.txt actually says, without spending any API calls."""
    print(f"User agent: {USER_AGENT}")
    robots = load_robots(SOURCE)
    if robots is None:
        print("could not read robots.txt - see the error above", file=sys.stderr)
        return 1

    all_allowed = True
    for path in TARGETS:
        url = SOURCE + path
        allowed = robots.can_fetch(USER_AGENT, url)
        print(f"  {'allowed   ' if allowed else 'DISALLOWED'} {url}")
        all_allowed = all_allowed and allowed
    return 0 if all_allowed else 1


def main() -> int:
    global PROVIDER

    parser = argparse.ArgumentParser()
    parser.add_argument("--no-build", action="store_true")
    parser.add_argument("--html-dir", type=Path)
    parser.add_argument(
        "--check-robots",
        action="store_true",
        help="report robots.txt status and per-URL permission, then exit",
    )
    parser.add_argument(
        "--provider",
        choices=["anthropic", "ollama"],
        help=f"which model runs the extraction step (default: {PROVIDER})",
    )
    args = parser.parse_args()

    if args.provider:
        PROVIDER = args.provider

    if args.check_robots:
        return check_robots()

    print(f"Scraping {len(TARGETS)} bonuses from {SOURCE}")
    print(f"Extraction model: {PROVIDER}:{active_model()}")

    records, failures = [], []
    for path in TARGETS:
        try:
            records.append(scrape_one(path, args.html_dir))
        except Exception as exc:
            print(f"  ! FAILED: {exc}", file=sys.stderr)
            failures.append({"path": path, "error": str(exc)})

    if not records:
        print("nothing scraped, leaving existing JSON alone", file=sys.stderr)
        return 1

    payload = {
        "source": SOURCE,
        "scraped_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "model": f"{PROVIDER}:{active_model()}",
        "casinos": records,
        "failures": failures,
    }

    OUT_JSON.parent.mkdir(parents=True, exist_ok=True)
    OUT_JSON.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"wrote {OUT_JSON} ({len(records)} casinos, {len(failures)} failed)")

    if not args.no_build:
        import build_page

        build_page.build()

    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
