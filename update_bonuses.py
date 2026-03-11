"""
HRT Transfer Bonus Auto-Updater
Runs daily via GitHub Actions. Uses Claude + web search to find current
transfer bonuses from Chase, Amex, Capital One, and Bilt.
"""

import anthropic
import json
import os
from datetime import datetime, date

client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])

SYSTEM_PROMPT = """You are a credit card points expert assistant for Hawaii Reward Travel (HRT).
Your job is to research current transfer bonuses and return ONLY a valid JSON object — no preamble, no markdown, no explanation.

The JSON must follow this exact structure:
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
      "notes": "Brief Hawaii-relevant tip from Scottie's perspective",
      "sourceUrl": "https://source-url.com"
    }
  ]
}

Rules:
- bank field must be one of: "chase", "amex", "capital-one", "bilt"
- partnerType must be "airline" or "hotel"
- partnerIcon: use ✈️ for airlines, 🏨 for hotels
- bonusPct is a number (e.g. 30 for 30% bonus)
- transferRatio is the standard ratio (e.g. "1:1")
- bonusRatio is the ratio with the bonus applied (e.g. "1:1.3")
- expiresDate must be a future date in YYYY-MM-DD format
- notes should be Hawaii-relevant, max 100 characters, written as Scottie's insider tip
- If a bonus has no clear expiry, use 30 days from today as a conservative estimate
- Only include ACTIVE bonuses that are currently live
- Return empty bonuses array if none are found — never fabricate data

Return ONLY the JSON. No other text."""

USER_PROMPT = f"""Today is {date.today().isoformat()}.

Please search the web for ALL currently active credit card transfer bonuses from:
1. Chase Ultimate Rewards
2. American Express Membership Rewards  
3. Capital One Miles
4. Bilt Rewards

Search these sources:
- thepointsguy.com/news
- thepointsguy.com transfer-bonus
- frequentmiler.com transfer bonus
- onemileatatime.com transfer bonus
- 10xtravel.com transfer bonus
- Bilt Rewards official announcements
- Chase, Amex, Capital One official promotion pages

Find every active transfer bonus with its exact bonus percentage, transfer partner, expiration date, and standard transfer ratio.

Return the complete JSON of all currently active bonuses."""

def fetch_bonuses():
    print("🔍 Searching for current transfer bonuses...")
    
    response = client.messages.create(
        model="claude-opus-4-5",
        max_tokens=4096,
        system=SYSTEM_PROMPT,
        tools=[
            {
                "type": "web_search_20250305",
                "name": "web_search"
            }
        ],
        messages=[
            {"role": "user", "content": USER_PROMPT}
        ]
    )

    # Extract the final text response (after tool use)
    result_text = ""
    for block in response.content:
        if block.type == "text":
            result_text += block.text

    if not result_text.strip():
        raise ValueError("No text response from Claude")

    # Clean up any accidental markdown fences
    clean = result_text.strip()
    if clean.startswith("```"):
        clean = clean.split("```")[1]
        if clean.startswith("json"):
            clean = clean[4:]
    clean = clean.strip().rstrip("```").strip()

    data = json.loads(clean)
    return data


def load_existing():
    """Load existing bonuses.json to preserve any manually added entries."""
    try:
        with open("bonuses.json", "r") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {"lastUpdated": None, "bonuses": []}


def merge_bonuses(existing, new_data):
    """
    Merge strategy:
    - New data from AI takes precedence for bank/partner matches
    - Manually added entries (bank = 'manual') are preserved
    - Expired bonuses are removed
    """
    today = date.today()
    
    # Keep manual entries
    manual = [b for b in existing.get("bonuses", []) if b.get("bank") == "manual"]
    
    # Filter out expired from new data
    active_new = []
    for b in new_data.get("bonuses", []):
        try:
            exp = date.fromisoformat(b["expiresDate"])
            if exp >= today:
                active_new.append(b)
        except (KeyError, ValueError):
            active_new.append(b)  # Include if we can't parse date
    
    # Re-assign sequential IDs
    all_bonuses = active_new + manual
    for i, b in enumerate(all_bonuses, 1):
        b["id"] = i

    return {
        "lastUpdated": datetime.utcnow().isoformat() + "Z",
        "bonuses": all_bonuses,
        "meta": {
            "source": "HRT Auto-Updater v1.0",
            "bonusCount": len(all_bonuses),
            "banks": list(set(b["bank"] for b in all_bonuses))
        }
    }


def save(data):
    with open("bonuses.json", "w") as f:
        json.dump(data, f, indent=2)
    print(f"✅ Saved {len(data['bonuses'])} active bonuses to bonuses.json")


def main():
    try:
        new_data = fetch_bonuses()
        existing = load_existing()
        merged = merge_bonuses(existing, new_data)
        save(merged)
        
        print("\n📋 Active bonuses found:")
        for b in merged["bonuses"]:
            print(f"  • {b['bankName']} → {b['partner']} (+{b['bonusPct']}%) expires {b['expiresDate']}")
        
    except Exception as e:
        print(f"❌ Error updating bonuses: {e}")
        # On failure, keep existing file unchanged — don't wipe good data
        raise


if __name__ == "__main__":
    main()
