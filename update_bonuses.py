"""
HRT Transfer Bonus Auto-Updater v2.4
Runs daily via GitHub Actions. Uses Claude + web search to find current
transfer bonuses from Chase, Amex, Capital One, Bilt, and Citi.

v2.4 adds:
- Stronger Citi-specific prompting (calls out Avianca, Virgin, Turkish, JetBlue)
- Reads manual-additions.json for bonuses the AI misses
- Dedupes manual entries against AI-found ones (manual always wins)
"""

import anthropic
import json
import os
import time
from datetime import datetime, date, timezone

client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])

MODEL = "claude-opus-4-7"
MANUAL_FILE = "manual-additions.json"

SYSTEM_PROMPT = """You are a credit card points expert for Hawaii Reward Travel (HRT).
Research current transfer bonuses and return ONLY a valid JSON object.

CRITICAL FORMATTING RULE:
Your FINAL response must be ONLY raw JSON — no preamble, no markdown fences, no
explanation sentences. Just the JSON object starting with { and ending with }.

Required structure:
{
  "lastUpdated": "ISO timestamp",
  "bonuses": [
    {
      "id": 1,
      "bank": "amex",
      "bankName": "American Express",
      "partner": "Air Canada Aeroplan",
      "partnerType": "airline",
      "partnerIcon": "\u2708\ufe0f",
      "bonusPct": 30,
      "transferRatio": "1:1",
      "bonusRatio": "1:1.3",
      "transferTime": "Instant",
      "expiresDate": "YYYY-MM-DD",
      "sourceUrl": "https://source.com"
    }
  ]
}

Rules:
- bank must be one of: chase, amex, capital-one, bilt, citi
- bankName should be the full program name:
  * chase -> "Chase Ultimate Rewards"
  * amex -> "American Express Membership Rewards"
  * capital-one -> "Capital One Miles"
  * bilt -> "Bilt Rewards"
  * citi -> "Citi ThankYou Points"
- partnerType must be: airline or hotel
- partnerIcon: airplane emoji for airlines, hotel emoji for hotels
- bonusPct is a number (30 = 30% bonus)
- expiresDate must be a future YYYY-MM-DD date
- If no clear expiry, use 30 days from today
- Only include ACTIVE bonuses that are live right now
- Return empty bonuses array if none found - never fabricate
- Your final message must be ONLY the raw JSON object"""

USER_PROMPT = f"""Today is {date.today().isoformat()}.

Search the web for ALL currently active credit card transfer bonuses from these 5 programs:

1. Chase Ultimate Rewards
2. American Express Membership Rewards
3. Capital One Miles
4. Bilt Rewards
5. Citi ThankYou Points

IMPORTANT — be thorough on Citi. Citi ThankYou Points partners with:
- Avianca LifeMiles, Virgin Atlantic Flying Club, Turkish Airlines Miles&Smiles,
  JetBlue TrueBlue, Air France/KLM Flying Blue, Emirates Skywards, Singapore KrisFlyer,
  Qatar Privilege Club, Etihad Guest, EVA Air, Thai Royal Orchid, Cathay Pacific,
  Choice Privileges, Wyndham Rewards, Shop Your Way
Check each of these for active bonuses — Citi runs short-burst promos that are easy to miss.

Search sources:
- thepointsguy.com/guide/transfer-bonuses (their master tracker)
- frequentmiler.com (search for "current transfer bonuses")
- monkeymiles.com (strong Citi coverage)
- onemileatatime.com
- 10xtravel.com
- The official issuer pages: citi.com/thankyou, americanexpress.com/membership-rewards,
  bilt.com, capitalone.com/rewards, chase.com/ultimate-rewards

Also pay special attention to Bilt Rent Day bonuses on the 1st of each month.

Find every active bonus with exact percentage, partner, expiration date, and transfer ratio.
If a bonus ends within the next 7 days, include it — those are the most valuable to surface.

When you're done researching, respond with ONLY the raw JSON object."""


def call_with_retry(func, max_retries=5):
    """Retry wrapper with exponential backoff for transient errors."""
    for attempt in range(1, max_retries + 1):
        try:
            return func()
        except anthropic.RateLimitError:
            if attempt == max_retries:
                raise
            wait = 60 * attempt
            print(f"  Rate limited (attempt {attempt}/{max_retries}). Waiting {wait}s...")
            time.sleep(wait)
        except anthropic.APIStatusError as e:
            if e.status_code in (529,) or e.status_code >= 500:
                if attempt == max_retries:
                    raise
                wait = 30 * attempt
                print(f"  Server error {e.status_code} (attempt {attempt}/{max_retries}). Waiting {wait}s...")
                time.sleep(wait)
            else:
                raise


def fetch_bonuses():
    """Call Claude with web_search enabled and return the final text response."""
    print("Searching for current transfer bonuses across 5 banks...")

    def make_request():
        return client.messages.create(
            model=MODEL,
            max_tokens=8192,
            system=SYSTEM_PROMPT,
            tools=[{
                "type": "web_search_20250305",
                "name": "web_search",
                "max_uses": 15,  # bumped to 15 for thorough Citi partner coverage
            }],
            messages=[{"role": "user", "content": USER_PROMPT}],
        )

    response = call_with_retry(make_request)

    text_blocks = []
    for block in response.content:
        if getattr(block, "type", None) == "text" and block.text.strip():
            text_blocks.append(block.text.strip())

    if not text_blocks:
        print(f"  stop_reason: {response.stop_reason}")
        print(f"  block types: {[getattr(b, 'type', '?') for b in response.content]}")
        raise ValueError("Claude returned no text blocks")

    full_text = "\n\n".join(text_blocks)

    search_count = sum(
        1 for b in response.content if getattr(b, "type", None) == "server_tool_use"
    )
    print(f"  Claude performed {search_count} web searches")
    print(f"  Collected {len(text_blocks)} text block(s), total {len(full_text)} chars")

    return full_text


def parse_response(raw):
    """Clean and parse Claude's JSON response."""
    if not raw or not raw.strip():
        raise ValueError("Empty response from Claude")

    text = raw.strip()

    if "```" in text:
        import re
        fenced = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
        if fenced:
            text = fenced.group(1)

    start = text.find("{")
    end = text.rfind("}")

    if start == -1 or end == -1 or end <= start:
        preview = text[:500] if len(text) > 500 else text
        raise ValueError(
            f"No JSON object found in response. "
            f"Got {len(text)} chars starting with: {preview!r}"
        )

    json_text = text[start:end + 1]

    try:
        return json.loads(json_text)
    except json.JSONDecodeError as e:
        preview = json_text[:500] if len(json_text) > 500 else json_text
        raise ValueError(f"JSON parse failed: {e}. Extracted text: {preview!r}") from e


def load_manual_additions():
    """Load manual bonus additions from manual-additions.json."""
    try:
        with open(MANUAL_FILE, "r") as f:
            data = json.load(f)
            bonuses = data.get("bonuses", [])
            print(f"Loaded {len(bonuses)} manual bonus(es) from {MANUAL_FILE}")
            return bonuses
    except FileNotFoundError:
        print(f"No {MANUAL_FILE} found - skipping manual additions")
        return []
    except json.JSONDecodeError as e:
        print(f"WARNING: {MANUAL_FILE} has invalid JSON ({e}). Skipping manual additions.")
        return []


def dedupe_key(b):
    """Create a dedup key for a bonus: bank + partner (case-insensitive)."""
    bank = (b.get("bank") or "").lower().strip()
    partner = (b.get("partner") or "").lower().strip()
    return f"{bank}|{partner}"


def merge_sources(ai_bonuses, manual_bonuses):
    """
    Merge AI-scraped and manual bonuses. Manual entries always win on collision
    (since Scottie verified them directly).
    """
    today = date.today()

    # Start with manual entries (they take priority)
    seen_keys = set()
    merged = []

    for b in manual_bonuses:
        # Skip expired manuals so stale entries auto-clean
        try:
            exp = date.fromisoformat(b["expiresDate"])
            if exp < today:
                print(f"  Skipping expired manual entry: {b.get('partner')} "
                      f"(expired {b['expiresDate']})")
                continue
        except (KeyError, ValueError):
            pass
        merged.append(dict(b))  # copy to avoid mutating source
        seen_keys.add(dedupe_key(b))

    for b in ai_bonuses:
        key = dedupe_key(b)
        if key in seen_keys:
            print(f"  AI found duplicate of manual entry ({key}) - keeping manual version")
            continue
        # Filter expired from AI results
        try:
            exp = date.fromisoformat(b["expiresDate"])
            if exp < today:
                continue
        except (KeyError, ValueError):
            pass
        merged.append(dict(b))
        seen_keys.add(key)

    # Renumber IDs
    for i, b in enumerate(merged, 1):
        b["id"] = i

    return merged


def build_output(merged_bonuses):
    """Build the final output structure for bonuses.json."""
    return {
        "lastUpdated": datetime.now(timezone.utc).isoformat(),
        "bonuses": merged_bonuses,
        "meta": {
            "source": "HRT Auto-Updater v2.4",
            "bonusCount": len(merged_bonuses),
            "banks": sorted(list(set(b["bank"] for b in merged_bonuses if b.get("bank")))),
        },
    }


def save(data):
    with open("bonuses.json", "w") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    print(f"Saved {len(data['bonuses'])} active bonuses to bonuses.json")


def main():
    try:
        # 1. Pull live bonuses from Claude
        raw = fetch_bonuses()
        print(f"Raw response length: {len(raw)} chars")
        print(f"Raw response preview: {raw[:200]!r}")

        new_data = parse_response(raw)
        ai_bonuses = new_data.get("bonuses", [])
        print(f"Parsed {len(ai_bonuses)} bonuses from AI response")

        # 2. Load manual additions
        manual_bonuses = load_manual_additions()

        # 3. Merge (manual wins on collision)
        merged = merge_sources(ai_bonuses, manual_bonuses)
        print(f"Final merged count: {len(merged)} active bonuses")

        # 4. Build output and save
        output = build_output(merged)
        save(output)

        print("\nActive bonuses:")
        for b in output["bonuses"]:
            source_tag = " [manual]" if b in manual_bonuses else ""
            print(
                f"  {b['bankName']} -> {b['partner']} (+{b['bonusPct']}%) "
                f"expires {b['expiresDate']}{source_tag}"
            )

    except Exception as e:
        print(f"Error: {e}")
        import traceback
        traceback.print_exc()
        raise


if __name__ == "__main__":
    main()
