"""
HRT Transfer Bonus Auto-Updater v2.3
Runs daily via GitHub Actions. Uses Claude + web search to find current
transfer bonuses from Chase, Amex, Capital One, Bilt, Citi, and Rove.

v2.3 — FIX: the previous version manually intercepted the web_search tool and
fed back fake tool results. web_search is a SERVER-side tool: the Anthropic API
runs the searches itself and returns the results inside the same turn. The old
loop confused the model, which replied "I'll search..." and stopped with zero
results, so bonuses.json silently froze. This version lets the server tool run,
handles "pause_turn" continuations, and reads the model's final text. The prompt
was also cleaned up (it no longer tells the model to "fetch pages directly" when
only a search tool is available).

v2.2 — switched model from Opus to Haiku 4.5 (structured extraction task).
"""

import anthropic
import json
import os
import time
from datetime import datetime, date, timedelta

client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])

MODEL = "claude-haiku-4-5"

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

Use the web_search tool to find ALL currently active credit card points transfer
bonuses. Search thoroughly — run several searches, for example:
- "current transfer bonuses {date.today().strftime('%B %Y')} chase amex capital one bilt citi rove"
- "frequent miler current point transfer bonuses"
- "the points guy current transfer bonuses"
- "new transfer bonus {date.today().strftime('%B %Y')}"

Reputable sources to look for in the results: frequentmiler.com, thepointsguy.com,
awardwallet.com, upgradedpoints.com, nerdwallet.com, roame.travel.

Cover these programs specifically:
1. Chase Ultimate Rewards
2. American Express Membership Rewards
3. Capital One Miles
4. Bilt Rewards
5. Citi ThankYou Points
6. Rove (bank value: "rove")

For each ACTIVE bonus, record: bank, partner, bonus %, expiration date
(YYYY-MM-DD), transfer ratio, and a short Hawaii-relevant note. Only include
bonuses you can confirm from a source; never fabricate one.

After you have searched, return ONLY the raw JSON object described in your
instructions — no preamble, no markdown fences."""


def call_with_retry(func, max_retries=5):
    """Retry wrapper with exponential backoff for transient API errors."""
    for attempt in range(1, max_retries + 1):
        try:
            return func()
        except anthropic.OverloadedError:
            if attempt == max_retries:
                raise
            wait = 30 * attempt
            print(f"  API overloaded (attempt {attempt}/{max_retries}). Waiting {wait}s...")
            time.sleep(wait)
        except anthropic.RateLimitError:
            if attempt == max_retries:
                raise
            wait = 60 * attempt
            print(f"  Rate limited (attempt {attempt}/{max_retries}). Waiting {wait}s...")
            time.sleep(wait)
        except anthropic.APIStatusError as e:
            if e.status_code >= 500 and attempt < max_retries:
                wait = 30 * attempt
                print(f"  Server error {e.status_code} (attempt {attempt}/{max_retries}). Waiting {wait}s...")
                time.sleep(wait)
            else:
                raise


def fetch_bonuses():
    """Run the research turn and return the model's final text answer.

    web_search is a server-side tool: the API executes the searches and returns
    the results within the turn. We must NOT fabricate tool results. A long turn
    can come back as stop_reason "pause_turn"; if so we simply resend the
    accumulated messages so the API can continue, then read the final text.
    """
    print("Searching for current transfer bonuses...")
    messages = [{"role": "user", "content": USER_PROMPT}]

    for _ in range(12):  # generous cap on pause_turn continuations
        def make_request():
            return client.messages.create(
                model=MODEL,
                max_tokens=8192,
                system=SYSTEM_PROMPT,
                tools=[{
                    "type": "web_search_20250305",
                    "name": "web_search",
                    "max_uses": 8,
                }],
                messages=messages,
            )

        response = call_with_retry(make_request)
        messages.append({"role": "assistant", "content": response.content})

        # Log any searches the server tool performed, for the run log.
        for block in response.content:
            if getattr(block, "type", None) == "server_tool_use" and getattr(block, "name", "") == "web_search":
                query = ""
                if isinstance(getattr(block, "input", None), dict):
                    query = block.input.get("query", "")
                print(f"  Searched: {query}")

        if response.stop_reason == "pause_turn":
            # Server tool paused mid-turn — let it continue.
            continue

        # Turn complete (end_turn / max_tokens). Concatenate all text output.
        text = "".join(
            b.text for b in response.content
            if getattr(b, "type", None) == "text"
        ).strip()

        if text:
            return text
        raise ValueError(f"Model returned no text (stop_reason={response.stop_reason})")

    raise ValueError("Exceeded continuation limit without a final answer")


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

    # Preserve manual entries
    manual = [b for b in existing.get("bonuses", []) if b.get("bank") == "manual"]

    # Build lookup of existing bonuses to preserve startDate
    existing_lookup = {}
    for b in existing.get("bonuses", []):
        key = (b.get("bank", ""), b.get("partner", ""))
        existing_lookup[key] = b

    # Split new bonuses into active and newly expired, stamp startDate
    active_new = []
    newly_expired = []
    for b in new_data.get("bonuses", []):
        try:
            exp = date.fromisoformat(b["expiresDate"])
            if exp >= today:
                key = (b.get("bank", ""), b.get("partner", ""))
                if key in existing_lookup and existing_lookup[key].get("startDate"):
                    b["startDate"] = existing_lookup[key]["startDate"]
                else:
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
                days_ago = (today - exp).days
                if 0 <= days_ago <= 30:
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
            "source": "HRT Auto-Updater v2.3",
            "bonusCount": len(all_bonuses),
            "expiredCount": len(merged_expired),
            "banks": sorted(set(b["bank"] for b in all_bonuses)),
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
        print(f"Response preview: {raw[:500]}")

        new_data = parse_response(raw)
        found = len(new_data.get("bonuses", []))
        print(f"Found {found} bonuses")

        # Safety check: never overwrite good existing data with empty results
        if found == 0:
            existing = load_existing()
            existing_active = [
                b for b in existing.get("bonuses", [])
                if b.get("expiresDate") and date.fromisoformat(b["expiresDate"]) >= date.today()
            ]
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
