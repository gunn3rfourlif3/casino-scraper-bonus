# Casino bonus scraper

Pulls welcome-bonus data for three online casinos from one public source
([AskGamblers](https://www.askgamblers.com)), uses a language model to turn the free-text
bonus terms into structured fields, writes `data/bonuses.json`, and generates a mobile-readable
`index.html` table from that JSON.

**Live page:** <https://gunn3rfourlif3.github.io/casino-scraper-bonus/>

**Repo:** <https://github.com/gunn3rfourlif3/casino-scraper-bonus>

## Fields

| Field | Example |
| --- | --- |
| Welcome bonus | `100% up to 500 EUR, + 100 free spins` |
| Wagering requirement (multiplier) | `40` |
| Minimum deposit | `15 EUR` |
| Source URL | link to the AskGamblers page it came from |

Two extras the source gives for free and that make the numbers comparable: `wagering_basis`
(`40xb` means 40× the bonus; `25x(d+b)` means 25× deposit **and** bonus — a very different
amount of money) and `max_bonus`.

## Install and run

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
```

The extraction step can run against either a local model or the Claude API.

**Local, via [Ollama](https://ollama.com) — no API key, no cost:**

```bash
ollama pull llama3.1:8b
python scrape.py --provider ollama
```

**Claude API:**

```bash
export ANTHROPIC_API_KEY=sk-ant-...
python scrape.py
```

That one command fetches the three pages, extracts the fields, writes `data/bonuses.json`
and regenerates `index.html`. Open `index.html` in a browser, or push the repo and point
GitHub Pages at the root.

Options:

```bash
python scrape.py --check-robots  # report robots.txt permission only, no model calls
python scrape.py --no-build      # write JSON only
python build_page.py             # rebuild the page from existing JSON
python tests/test_parsing.py     # parsing + validation tests, no network or model needed

# the model is configurable on either provider
OLLAMA_MODEL=qwen2.5:7b python scrape.py --provider ollama
MODEL=claude-opus-5 python scrape.py
OLLAMA_HOST=http://192.168.1.50:11434 python scrape.py --provider ollama
```

## How it works

1. **robots.txt first.** `urllib.robotparser` checks each URL against the declared
   user agent before any fetch. 3-second delay between requests, honest UA string with a
   contact address. HTTP 401/403/429/5xx is treated as a block and the run stops rather than
   retrying around it.
2. **Reduce the page to text.** `extract_bonus_text()` strips `script`, `style`, `nav`,
   `header`, `footer`, takes the visible text and starts from the `<h1>`. Deliberately
   selector-light — class names on a site like this churn constantly, the shape of the content
   doesn't.
3. **The LLM step.** ~3.5 KB of that text goes to Claude with a prompt specifying an exact
   JSON shape. This is where `40xb`, `25x(d+b)`, `€15`, `10000 ARS` and prose like *"you must
   wager the deposit and bonus 25 times"* all become the same four fields. A regex can pull the
   digits out of `40xb`; it can't normalise the basis or read the sentence.
4. **Validate.** Range checks on the multiplier and deposit, an allowlist on the basis, and a
   cross-check that the bonus percentage the model returned actually appears in the source URL
   slug. Warnings go to stderr and into the JSON next to the record rather than being swallowed.
5. **Build the page.** `build_page.py` renders a static table. On phones a media query collapses
   each row into a labelled card, so there's no horizontal scrolling.

## Source choice

Two sources were tried and rejected first, both worth recording:

- **casino.guru** geo-gates its bonus data. From a US IP the review pages render
  *"No bonuses available for players from Ohio"* and the wagering and minimum-deposit fields
  are simply absent from the HTML.
- **lcb.org** `robots.txt` contains `User-agent: ClaudeBot / Disallow: /`. Out, regardless of
  what the pages contain.

AskGamblers allows the `/casino-bonuses/latest/...` paths (its `robots.txt` blocks query
strings and a long list of filter parameters, none of which these URLs use), serves all four
fields in the HTML without JavaScript, and states the terms in prose as well as in a label
block — which is what makes the LLM step a genuine step rather than decoration.

## Notes and limitations

- The three bonus URLs are pinned in `TARGETS` in `scrape.py`. For a production run you'd
  discover them from a listing page instead; see `WRITEUP.md`.
- Bonus offers rotate. If a pinned URL 404s, replace it with a current one from
  <https://www.askgamblers.com/casino-bonuses/welcome>.
- All three casinos happen to quote EUR. The schema carries a currency code per record, so
  mixed currencies work, but the page doesn't convert between them.
