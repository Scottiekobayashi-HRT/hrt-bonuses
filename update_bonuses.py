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
- bank must be: chase, amex, capital-one, or bilt
- partnerType must be: airline or hotel
- partnerIcon: use the airplane emoji for airlines, hotel emoji for hotels
- bonusPct is a number (30 = 30% bonus)
- expiresDate must be a future YYYY-MM-DD date
- If no clear expiry, use 30 days from today
- Only include ACTIVE bonuses that are live right now
- Return empty bonuses array if none found — never fabricate
- Return ONLY raw JSON, nothing else"""

USER_PROMPT = f"""Today is {date.today().isoformat()}.

Search the web for ALL currently active credit card transfer bonuses from:
1. Chase Ultimate Rewards
2. American Express Membership Rewards
3. Capital One Miles
4. Bilt Rewards

Search sources like thepointsguy.com, frequentmiler.com, onemileatatime.com, and 10xtravel.com.

Find every active bonus with its exact percentage, partner, expiration date, and transfer ratio.

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
                max_tokens=4096,
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
                    print(f"  Searched: {block.input.get('query', 'unknown query')}")
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
    """Clean and parse Claude's JSON response."""
    text = raw.strip()

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
    if start != -1 and end > start:
        text = text[start:end]

    return json.loads(text)


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

    return {
        "lastUpdated": datetime.utcnow().isoformat() + "Z",
        "bonuses": all_bonuses,
        "meta": {
            "source": "HRT Auto-Updater v2.1",
            "bonusCount": len(all_bonuses),
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

        new_data = parse_response(raw)
        print(f"Found {len(new_data.get('bonuses', []))} bonuses")

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
