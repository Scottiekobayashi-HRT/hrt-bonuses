"""
HRT Transfer Bonus Auto-Updater v2.1
Runs daily via GitHub Actions. Uses Claude + web search to find current
transfer bonuses from Chase, Amex, Capital One, and Bilt.
"""

import anthropic
import json
import os
import time
from datetime import datetime, date

client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])

SYSTEM_PROMPT = """You are a credit card points expert for Hawaii Reward Travel (HRT).
Research current transfer bonuses and return ONLY a valid JSON object.
No preamble, no markdown fences, no explanation — just the raw JSON.

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
      "partnerIcon": "✈️",
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
- bank must be: chase, amex, capital-one, bilt, citi, or rove
- partnerType must be: airline or hotel
- partnerIcon: use the airplane emoji for airlines, hotel emoji for hotels
- bonusPct is a number (30 = 30% bonus)
- expiresDate must be a future YYYY-MM-DD date
- If no clear expiry, use 30 days from today
- Only include ACTIVE bonuses that are live right now
- Return empty bonuses array if none found — never fabricate
- Return ONLY raw JSON, nothing else"""

USER_PROMPT = f"""Today is {date.today().isoformat()}.

You need to find ALL currently active credit card transfer bonuses. Do this in two steps:

STEP 1 — Fetch these specific pages directly (they track transfer bonuses in real time):
- https://www.maxmilespoints.com/transfer-bonuses
- https://frequentmiler.com/transfer-bonuses/
- https://thepointsguy.com/news/transfer-bonus/
- https://10xtravel.com/transfer-bonuses/

STEP 2 — Also run web searches for any bonuses you may have missed:
- "active transfer bonus 2026 chase amex capital one bilt citi rove"
- "new transfer bonus June 2026"

Look for bonuses from these banks specifically:
1. Chase Ultimate Rewards
2. American Express Membership Rewards
3. Capital One Miles
4. Bilt Rewards
5. Citi ThankYou Points
6. Rove (bank value: "rove")

For each bonus found, record: bank, partner, bonus %, expiration date, transfer ratio, and transfer time.
Cross-reference across sources to make sure you have every active bonus.

Return ONLY the raw JSON object described in your instructions."""


def call_with_retry(func, max_retries=5):
    """
    Retry wrapper with exponential backoff.
    Handles 529 Overloaded and 529-like transient errors.
    """
    for attempt in range(1, max_retries + 1):
        try:
            return func()
        except anthropic.OverloadedError as e:
            if attempt == max_retries:
                raise
            wait = 30 * attempt  # 30s, 60s, 90s, 120s
            print(f"  API overloaded (attempt {attempt}/{max_retries}). Waiting {wait}s before retry...")
            time.sleep(wait)
        except anthropic.RateLimitError as e:
            if attempt == max_retries:
                raise
            wait = 60 * attempt
            print(f"  Rate limited (attempt {attempt}/{max_retries}). Waiting {wait}s before retry...")
            time.sleep(wait)
        except anthropic.APIStatusError as e:
            # Retry on any 5xx server error
            if e.status_code >= 500 and attempt < max_retries:
                wait = 30 * attempt
                print(f"  Server error {e.status_code} (attempt {attempt}/{max_retries}). Waiting {wait}s...")
                time.sleep(wait)
            else:
                raise


def fetch_bonuses():
    print("Searching for current transfer bonuses...")

    messages = [{"role": "user", "content": USER_PROMPT}]

    # Agentic loop — keeps going until Claude stops using tools
    while True:
        def make_request():
            return client.messages.create(
                model="claude-opus-4-5",
                max_tokens=8192,
                system=SYSTEM_PROMPT,
                tools=[{"type": "web_search_20250305", "name": "web_search"}],
                messages=messages,
            )

        response = call_with_retry(make_request)

        # Add Claude's response to the conversation history
        messages.append({"role": "assistant", "content": response.content})

        # Done — extract the final text response
        if response.stop_reason == "end_turn":
            for block in response.content:
                if block.type == "text" and block.text.strip():
                    return block.text.strip()
            raise ValueError("Claude returned no text in final response")

        # Tool use — collect results and continue the loop
        if response.stop_reason == "tool_use":
            tool_results = []
            for block in response.content:
                if block.type == "tool_use":
                    if block.name == "web_search":
                        print(f"  Searched: {block.input.get('query', '')}")
                    elif block.name == "web_fetch":
                        print(f"  Fetched:  {block.input.get('url', '')}")
                    else:
                        print(f"  Tool: {block.name} — {str(block.input)[:80]}")
                    tool_results.append({
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": "Search completed successfully."
                    })
            if tool_results:
                messages.append({"role": "user", "content": tool_results})
            continue

        # Fallback — try to get any text from the response
        for block in response.content:
            if hasattr(block, "text") and block.text.strip():
                return block.text.strip()
        raise ValueError(f"Unexpected stop reason: {response.stop_reason}")


def parse_response(raw):
    """Clean and parse Claude's JSON response. Returns empty bonuses if no valid JSON found."""
    text = raw.strip()

    if not text:
        print("  Warning: Empty response from Claude — returning empty bonuses")
        return {"lastUpdated": datetime.utcnow().isoformat() + "Z", "bonuses": []}

    # Strip markdown fences if present
    if "```" in text:
        parts = text.split("```")
        for part in parts:
            part = part.strip()
            if part.startswith("json"):
                part = part[4:].strip()
            if part.startswith("{"):
                text = part
                break

    # Find the JSON object boundaries
    start = text.find("{")
    end = text.rfind("}") + 1

    if start == -1 or end <= start:
        # Claude returned plain text (e.g. "No active bonuses found") — not a crash
        print(f"  Note: Claude returned non-JSON response: {text[:200]}")
        print("  Treating as no active bonuses found.")
        return {"lastUpdated": datetime.utcnow().isoformat() + "Z", "bonuses": []}

    text = text[start:end]

    try:
        return json.loads(text)
    except json.JSONDecodeError as e:
        print(f"  Warning: JSON parse error ({e}) — returning empty bonuses")
        print(f"  Raw text was: {text[:300]}")
        return {"lastUpdated": datetime.utcnow().isoformat() + "Z", "bonuses": []}


def load_existing():
    """Load existing bonuses.json to preserve manually added entries."""
    try:
        with open("bonuses.json", "r") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {"lastUpdated": None, "bonuses": []}


def merge_bonuses(existing, new_data):
    """Keep manual entries, add new AI-found ones, move expired to recentlyExpired log."""
    today = date.today()
    cutoff = date.fromisoformat((datetime.utcnow().replace(day=1) - __import__("datetime").timedelta(days=30)).strftime("%Y-%m-%d"))

    # Preserve manual entries
    manual = [b for b in existing.get("bonuses", []) if b.get("bank") == "manual"]

    # Build lookup of existing bonuses to preserve startDate
    existing_lookup = {}
    for b in existing.get("bonuses", []):
        key = (b.get("bank",""), b.get("partner",""))
        existing_lookup[key] = b

    # Split new bonuses into active and newly expired, stamp startDate
    active_new = []
    newly_expired = []
    for b in new_data.get("bonuses", []):
        try:
            exp = date.fromisoformat(b["expiresDate"])
            if exp >= today:
                key = (b.get("bank",""), b.get("partner",""))
                if key in existing_lookup and existing_lookup[key].get("startDate"):
                    # Preserve original discovery date
                    b["startDate"] = existing_lookup[key]["startDate"]
                else:
                    # Brand new — stamp today
                    b["startDate"] = today.isoformat()
                active_new.append(b)
            else:
                newly_expired.append(b)
        except (KeyError, ValueError):
            b.setdefault("startDate", today.isoformat())
            active_new.append(b)

    # Also check previously active bonuses that may have expired since last run
    for b in existing.get("bonuses", []):
        if b.get("bank") == "manual":
            continue
        try:
            exp = date.fromisoformat(b["expiresDate"])
            if exp < today:
                # Check it's not already in newly_expired
                key = (b.get("bank"), b.get("partner"))
                if not any((x.get("bank"), x.get("partner")) == key for x in newly_expired):
                    newly_expired.append(b)
        except (KeyError, ValueError):
            pass

    # Merge with existing recentlyExpired log (keep last 30 days, dedupe)
    existing_expired = existing.get("recentlyExpired", [])
    seen = set()
    merged_expired = []
    for b in (newly_expired + existing_expired):
        key = (b.get("bank"), b.get("partner"), b.get("expiresDate"))
        if key not in seen:
            try:
                exp = date.fromisoformat(b["expiresDate"])
                # Keep if expired within last 30 days
                days_ago = (today - exp).days
                if days_ago <= 30:
                    seen.add(key)
                    merged_expired.append(b)
            except (KeyError, ValueError):
                pass

    # Sort expired: most recently expired first
    merged_expired.sort(key=lambda b: b.get("expiresDate", ""), reverse=True)

    all_bonuses = active_new + manual
    for i, b in enumerate(all_bonuses, 1):
        b["id"] = i

    return {
        "lastUpdated": datetime.utcnow().isoformat() + "Z",
        "bonuses": all_bonuses,
        "recentlyExpired": merged_expired,
        "meta": {
            "source": "HRT Auto-Updater v2.1",
            "bonusCount": len(all_bonuses),
            "expiredCount": len(merged_expired),
            "banks": list(set(b["bank"] for b in all_bonuses))
        }
    }


def save(data):
    with open("bonuses.json", "w") as f:
        json.dump(data, f, indent=2)
    print(f"Saved {len(data['bonuses'])} active bonuses to bonuses.json")


def main():
    try:
        raw = fetch_bonuses()
        print(f"Raw response length: {len(raw)} chars")
        print(f"Response preview: {raw[:500]}")

        new_data = parse_response(raw)
        found = len(new_data.get("bonuses", []))
        print(f"Found {found} bonuses")

        # Safety check: never overwrite good existing data with empty results
        if found == 0:
            existing = load_existing()
            existing_active = [b for b in existing.get("bonuses", [])
                               if b.get("expiresDate") and
                               date.fromisoformat(b["expiresDate"]) >= date.today()]
            if existing_active:
                print(f"WARNING: Script found 0 bonuses but {len(existing_active)} valid ones exist.")
                print("Keeping existing data. Will retry at next scheduled run.")
                return

        existing = load_existing()
        merged = merge_bonuses(existing, new_data)
        save(merged)

        print("\nActive bonuses:")
        for b in merged["bonuses"]:
            print(f"  {b['bankName']} -> {b['partner']} (+{b['bonusPct']}%) expires {b['expiresDate']}")

    except Exception as e:
        print(f"Error: {e}")
        import traceback
        traceback.print_exc()
        raise


if __name__ == "__main__":
    main()
