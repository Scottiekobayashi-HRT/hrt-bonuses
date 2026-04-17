"""
HRT Transfer Bonus Auto-Updater v2.3
Runs daily via GitHub Actions. Uses Claude + web search to find current
transfer bonuses from Chase, Amex, Capital One, Bilt, and Citi.
"""

import anthropic
import json
import os
import time
from datetime import datetime, date, timezone

client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])

MODEL = "claude-opus-4-7"

SYSTEM_PROMPT = """You are a credit card points expert for Hawaii Reward Travel (HRT).
Research current transfer bonuses and return ONLY a valid JSON object.

CRITICAL FORMATTING RULE:
Your FINAL response must be ONLY raw JSON — no preamble, no markdown fences, no
explanation sentences like "Based on my research..." — just the JSON object
starting with { and ending with }. Any text before or after will break the pipeline.

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
      "expiresDate": "YYYY-MM-DD",
      "notes": "Hawaii-relevant tip, max 100 chars",
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
- Return empty bonuses array if none found — never fabricate
- Your final message must be ONLY the raw JSON object, nothing else"""

USER_PROMPT = f"""Today is {date.today().isoformat()}.

Search the web for ALL currently active credit card transfer bonuses from:
1. Chase Ultimate Rewards
2. American Express Membership Rewards
3. Capital One Miles
4. Bilt Rewards
5. Citi ThankYou Points

Search sources like thepointsguy.com, frequentmiler.com, onemileatatime.com, 10xtravel.com, and monkeymiles.com.

Find every active bonus with its exact percentage, partner, expiration date, and transfer ratio.

Pay special attention to Bilt Rent Day bonuses (1st of each month) and Citi ThankYou bonuses — these are often overlooked.

When you're done researching, respond with ONLY the raw JSON object — no preamble, no markdown fences, no explanation."""


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
                "max_uses": 12,  # bumped from 10 — 5 banks needs more searches
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
    """Clean and parse Claude's JSON response. Robust to preamble, fences, postamble."""
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


def load_existing():
    try:
        with open("bonuses.json", "r") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {"lastUpdated": None, "bonuses": []}


def merge_bonuses(existing, new_data):
    today = date.today()
    manual = [b for b in existing.get("bonuses", []) if b.get("bank") == "manual"]

    active_new = []
    for b in new_data.get("bonuses", []):
        try:
            exp = date.fromisoformat(b["expiresDate"])
            if exp >= today:
                active_new.append(b)
        except (KeyError, ValueError):
            active_new.append(b)

    all_bonuses = active_new + manual
    for i, b in enumerate(all_bonuses, 1):
        b["id"] = i

    return {
        "lastUpdated": datetime.now(timezone.utc).isoformat(),
        "bonuses": all_bonuses,
        "meta": {
            "source": "HRT Auto-Updater v2.3",
            "bonusCount": len(all_bonuses),
            "banks": list(set(b["bank"] for b in all_bonuses)),
        },
    }


def save(data):
    with open("bonuses.json", "w") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    print(f"Saved {len(data['bonuses'])} active bonuses to bonuses.json")


def main():
    try:
        raw = fetch_bonuses()
        print(f"Raw response length: {len(raw)} chars")
        print(f"Raw response preview: {raw[:200]!r}")

        new_data = parse_response(raw)
        print(f"Parsed {len(new_data.get('bonuses', []))} bonuses from response")

        existing = load_existing()
        merged = merge_bonuses(existing, new_data)
        save(merged)

        print("\nActive bonuses:")
        for b in merged["bonuses"]:
            print(
                f"  {b['bankName']} -> {b['partner']} (+{b['bonusPct']}%) "
                f"expires {b['expiresDate']}"
            )

    except Exception as e:
        print(f"Error: {e}")
        import traceback
        traceback.print_exc()
        raise


if __name__ == "__main__":
    main()
