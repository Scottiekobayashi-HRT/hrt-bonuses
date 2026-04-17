"""
HRT Transfer Bonus Auto-Updater v2.2
Runs daily via GitHub Actions. Uses Claude + web search to find current
transfer bonuses from Chase, Amex, Capital One, and Bilt.
"""

import anthropic
import json
import os
import time
from datetime import datetime, date, timezone

client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])

# FIX #2: Use a current model. claude-opus-4-5 doesn't exist.
# Options: claude-opus-4-7 (newest, best), claude-sonnet-4-6 (cheaper, fine for this)
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
- bank must be: chase, amex, capital-one, or bilt
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

Search sources like thepointsguy.com, frequentmiler.com, onemileatatime.com, and 10xtravel.com.

Find every active bonus with its exact percentage, partner, expiration date, and transfer ratio.

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
            # 529 = overloaded, 5xx = server errors. Retry these.
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
    print("Searching for current transfer bonuses...")

    def make_request():
        return client.messages.create(
            model=MODEL,
            max_tokens=8192,  # JSON can get long with many bonuses
            system=SYSTEM_PROMPT,
            tools=[{
                "type": "web_search_20250305",
                "name": "web_search",
                "max_uses": 10,  # enough for researching 4 banks thoroughly
            }],
            messages=[{"role": "user", "content": USER_PROMPT}],
        )

    response = call_with_retry(make_request)

    # FIX #3: web_search is a server-side tool. No client-side tool_use loop needed.
    # Anthropic runs the searches and returns everything in one response.

    # FIX #1: Collect ALL text blocks, not just the first one.
    # Claude typically outputs: preamble text → search calls → JSON text.
    # We want the LAST text block (the final answer), but we'll concatenate all
    # text blocks so parse_response can robustly extract the JSON from anywhere.
    text_blocks = []
    for block in response.content:
        # Only grab client-facing text blocks, skip server_tool_use and tool_result blocks
        if getattr(block, "type", None) == "text" and block.text.strip():
            text_blocks.append(block.text.strip())

    if not text_blocks:
        # Dump the whole response for debugging if we somehow got nothing
        print(f"  stop_reason: {response.stop_reason}")
        print(f"  block types: {[getattr(b, 'type', '?') for b in response.content]}")
        raise ValueError("Claude returned no text blocks")

    # Join all text blocks. The JSON should be at the end (Claude's final answer).
    full_text = "\n\n".join(text_blocks)

    # Log how many searches Claude actually did (good for debugging)
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

    # Strip markdown fences if present
    if "```" in text:
        # Try to find a fenced json block first
        import re
        fenced = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
        if fenced:
            text = fenced.group(1)

    # Find the outermost JSON object boundaries
    start = text.find("{")
    end = text.rfind("}")

    if start == -1 or end == -1 or end <= start:
        # Give a useful error — show what we actually got so we can debug
        preview = text[:500] if len(text) > 500 else text
        raise ValueError(
            f"No JSON object found in response. "
            f"Got {len(text)} chars starting with: {preview!r}"
        )

    json_text = text[start:end + 1]

    try:
        return json.loads(json_text)
    except json.JSONDecodeError as e:
        # On parse failure, include the problematic JSON for debugging
        preview = json_text[:500] if len(json_text) > 500 else json_text
        raise ValueError(f"JSON parse failed: {e}. Extracted text: {preview!r}") from e


def load_existing():
    """Load existing bonuses.json to preserve manually added entries."""
    try:
        with open("bonuses.json", "r") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {"lastUpdated": None, "bonuses": []}


def merge_bonuses(existing, new_data):
    """Keep manual entries, add new AI-found ones, remove expired."""
    today = date.today()

    # Preserve manual entries
    manual = [b for b in existing.get("bonuses", []) if b.get("bank") == "manual"]

    # Filter out expired from new data
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

    # FIX: Use timezone-aware UTC instead of deprecated utcnow()
    return {
        "lastUpdated": datetime.now(timezone.utc).isoformat(),
        "bonuses": all_bonuses,
        "meta": {
            "source": "HRT Auto-Updater v2.2",
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
        # Print a preview so future failures are easier to debug
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
