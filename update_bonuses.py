"""
HRT Transfer Bonus Auto-Updater v3.0
Runs daily via GitHub Actions (.github/workflows/update-bonuses.yml, unchanged).

WHY v3.0
  v2.3 asked the model to cover all six banks in ONE research turn with 8 searches.
  Roundup articles lead with Chase and Amex, so smaller programs (Citi most often)
  got dropped on many days. Worse, anything not found on a given day vanished from
  bonuses.json even though it was still live. Example: the Citi to Japan Airlines
  30% launch bonus (Sept 20 to Oct 24, 2026) never made it into the feed.

WHAT CHANGED
  1. One research turn PER BANK, with bank specific searches (Citi gets its own).
  2. Carry forward: a bonus with a published end date stays live until that date,
     even if one day's search misses it. If a bank's search fails, that bank's
     existing bonuses are kept untouched.
  3. manual-additions.json: bonuses you add by hand always show, override the AI
     for the same partner, and drop off after their end date. Marked "manual".
  4. No invented end dates. If no end date is published, expiresDate is null and the
     tracker shows "End date not announced". Those drop after 7 days unconfirmed.
  5. Partner names are normalized, so "Flying Blue" / "Air France/KLM Flying Blue"
     no longer show up as four different bonuses.
  6. meta.bankStatus records when each bank was checked and what was found, so the
     tracker can say "Citi: checked today, no bonus live" instead of looking broken.
"""

import anthropic
import json
import os
import re
import time
from datetime import datetime, date, timedelta, timezone

MODEL = os.environ.get("HRT_MODEL", "claude-haiku-4-5")
SEARCHES_PER_BANK = 5
NO_DATE_GRACE_DAYS = 7          # undated bonuses drop after this many days without a re-confirm
EXPIRED_LOG_DAYS = 30

TODAY = date.today()
NOW_ISO = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")

BANKS = {
    "amex": {
        "name": "American Express", "program": "American Express Membership Rewards",
        "queries": ["Amex Membership Rewards transfer bonus {month}", "Amex points transfer bonus new {month}"],
    },
    "bilt": {
        "name": "Bilt", "program": "Bilt Rewards",
        "queries": ["Bilt Rewards transfer bonus {month}", "Bilt Rent Day transfer bonus {month}"],
    },
    "capital-one": {
        "name": "Capital One", "program": "Capital One Miles",
        "queries": ["Capital One miles transfer bonus {month}", "Capital One transfer partner bonus {month}"],
    },
    "chase": {
        "name": "Chase", "program": "Chase Ultimate Rewards",
        "queries": ["Chase Ultimate Rewards transfer bonus {month}", "Chase points transfer bonus {month}"],
    },
    "citi": {
        "name": "Citi", "program": "Citi ThankYou Points",
        "queries": ["Citi ThankYou transfer bonus {month}", "Citi ThankYou new transfer partner bonus",
                    "Citi ThankYou points transfer bonus ends"],
    },
    "rove": {
        "name": "Rove", "program": "Rove Miles",
        "queries": ["Rove Miles transfer bonus {month}"],
    },
}

# Canonical partner names. First match wins, so specific patterns come first.
PARTNERS = [
    (r"aer\s*lingus", "Aer Lingus AerClub", "airline"),
    (r"iberia", "Iberia Plus Avios", "airline"),
    (r"qatar", "Qatar Airways Privilege Club", "airline"),
    (r"british|\bavios\b", "British Airways Avios", "airline"),
    (r"flying\s*blue|air\s*france|klm", "Air France-KLM Flying Blue", "airline"),
    (r"aeroplan|air\s*canada", "Air Canada Aeroplan", "airline"),
    (r"lifemiles|avianca", "Avianca LifeMiles", "airline"),
    (r"asia\s*miles|cathay", "Cathay Pacific Asia Miles", "airline"),
    (r"krisflyer|singapore", "Singapore Airlines KrisFlyer", "airline"),
    (r"virgin", "Virgin Atlantic Flying Club", "airline"),
    (r"turkish|miles\s*&?\s*smiles", "Turkish Airlines Miles&Smiles", "airline"),
    (r"emirates", "Emirates Skywards", "airline"),
    (r"etihad", "Etihad Guest", "airline"),
    (r"jetblue|trueblue", "JetBlue TrueBlue", "airline"),
    (r"united|mileageplus", "United MileagePlus", "airline"),
    (r"delta|skymiles", "Delta SkyMiles", "airline"),
    (r"\bana\b|all\s*nippon", "ANA Mileage Club", "airline"),
    (r"japan\s*airlines|\bjal\b|mileage\s*bank", "Japan Airlines Mileage Bank", "airline"),
    (r"atmos|alaska|hawaiian", "Atmos Rewards", "airline"),
    (r"aadvantage|american\s*airlines", "American Airlines AAdvantage", "airline"),
    (r"aeromexico|aerom[eé]xico", "Aeromexico Rewards", "airline"),
    (r"\beva\b|infinity\s*mileage", "EVA Air Infinity MileageLands", "airline"),
    (r"qantas", "Qantas Frequent Flyer", "airline"),
    (r"thai|royal\s*orchid", "Thai Royal Orchid Plus", "airline"),
    (r"\btap\b|miles\s*&?\s*go", "TAP Miles&Go", "airline"),
    (r"finnair", "Finnair Plus", "airline"),
    (r"copa|connectmiles", "Copa ConnectMiles", "airline"),
    (r"southwest|rapid\s*rewards", "Southwest Rapid Rewards", "airline"),
    (r"frontier", "Frontier Miles", "airline"),
    (r"spirit", "Spirit Free Spirit", "airline"),
    (r"hyatt", "World of Hyatt", "hotel"),
    (r"marriott|bonvoy", "Marriott Bonvoy", "hotel"),
    (r"hilton", "Hilton Honors", "hotel"),
    (r"\bihg\b|intercontinental", "IHG One Rewards", "hotel"),
    (r"wyndham", "Wyndham Rewards", "hotel"),
    (r"choice", "Choice Privileges", "hotel"),
    (r"accor|\(all\)|live\s*limitless", "Accor Live Limitless", "hotel"),
    (r"leading\s*hotels|leaders\s*club", "Leading Hotels of the World Leaders Club", "hotel"),
    (r"preferred\s*hotels|i\s*prefer", "Preferred Hotels I Prefer", "hotel"),
]

SYSTEM_PROMPT = """You research credit card points transfer bonuses for Hawaii Reward Travel.
Return ONLY a raw JSON object, no preamble and no markdown fences:
{"bonuses":[{"partner":"...","partnerType":"airline|hotel","bonusPct":30,
"transferRatio":"1:1","expiresDate":"YYYY-MM-DD or null","startDate":"YYYY-MM-DD or null",
"sourceUrl":"https://...","notes":"short factual note, max 100 chars"}]}
Rules:
- Only bonuses that are live today for the ONE program you are asked about.
- bonusPct is the bonus percent as a number (30 means +30%).
- transferRatio is the normal ratio without the bonus, for premium cards if it varies.
- expiresDate: the published end date. If the source gives no end date, use null. Never guess.
- sourceUrl must be the page that states the bonus. Skip anything you cannot source.
- Notes state facts only (eligibility, caps, registration). No invented tips.
- If nothing is live, return {"bonuses":[]}. Never fabricate."""


def user_prompt(bank):
    b = BANKS[bank]
    month = TODAY.strftime("%B %Y")
    queries = "\n".join("- " + q.format(month=month) for q in b["queries"])
    return (
        f"Today is {TODAY.isoformat()}.\n\n"
        f"Find every transfer bonus that is live today FROM {b['program']} to any airline or hotel partner.\n"
        f"Also check for brand new transfer partners that launched with a bonus.\n"
        f"Run searches like:\n{queries}\n\n"
        "Good sources: frequentmiler.com, onemileatatime.com, awardwallet.com, upgradedpoints.com, "
        "thepointsguy.com, doctorofcredit.com, milestomemories.com, loyaltylobby.com.\n\n"
        f"Return ONLY the JSON object, with bonuses from {b['program']} only."
    )


def call_with_retry(func, max_retries=5):
    for attempt in range(1, max_retries + 1):
        try:
            return func()
        except anthropic.OverloadedError:
            if attempt == max_retries:
                raise
            time.sleep(30 * attempt)
        except anthropic.RateLimitError:
            if attempt == max_retries:
                raise
            time.sleep(60 * attempt)
        except anthropic.APIStatusError as e:
            if e.status_code >= 500 and attempt < max_retries:
                time.sleep(30 * attempt)
            else:
                raise


def research(client, bank):
    """One research turn for one bank. Returns the model's final text."""
    messages = [{"role": "user", "content": user_prompt(bank)}]
    for _ in range(10):
        response = call_with_retry(lambda: client.messages.create(
            model=MODEL, max_tokens=4096, system=SYSTEM_PROMPT,
            tools=[{"type": "web_search_20250305", "name": "web_search", "max_uses": SEARCHES_PER_BANK}],
            messages=messages,
        ))
        messages.append({"role": "assistant", "content": response.content})
        for block in response.content:
            if getattr(block, "type", None) == "server_tool_use":
                q = block.input.get("query", "") if isinstance(getattr(block, "input", None), dict) else ""
                print(f"    searched: {q}")
        if response.stop_reason == "pause_turn":
            continue
        return "".join(b.text for b in response.content if getattr(b, "type", None) == "text").strip()
    raise ValueError("continuation limit reached")


def parse_json(text):
    if not text:
        raise ValueError("empty response")
    if "```" in text:
        for part in text.split("```"):
            part = part.strip()
            if part.startswith("json"):
                part = part[4:].strip()
            if part.startswith("{"):
                text = part
                break
    start, end = text.find("{"), text.rfind("}") + 1
    if start == -1 or end <= start:
        raise ValueError("no JSON object in response")
    return json.loads(text[start:end])


def canonical(partner, fallback_type=None):
    p = (partner or "").strip()
    low = p.lower()
    for pattern, name, ptype in PARTNERS:
        if re.search(pattern, low):
            return name, ptype
    return p, (fallback_type if fallback_type in ("airline", "hotel") else "airline")


def valid_date(s):
    if not s or not isinstance(s, str):
        return None
    try:
        return date.fromisoformat(s[:10])
    except ValueError:
        return None


def bonus_ratio(ratio, pct):
    try:
        a, b = [float(x) for x in str(ratio).replace(" ", "").split(":")]
        out = b * (1 + float(pct) / 100)
        fmt = lambda n: ("%.4f" % n).rstrip("0").rstrip(".")
        return f"{fmt(a)}:{fmt(out)}"
    except Exception:
        return None


def clean(bank, raw, method, today_iso):
    """Validate and normalize one bonus. Returns None if it should be dropped."""
    try:
        pct = float(raw.get("bonusPct"))
    except (TypeError, ValueError):
        return None
    if not (0 < pct <= 200):
        return None
    pct = int(pct) if pct == int(pct) else pct
    partner, ptype = canonical(raw.get("partner"), raw.get("partnerType"))
    if not partner:
        return None
    exp = valid_date(raw.get("expiresDate"))
    if exp and exp < TODAY:
        return None
    src = raw.get("sourceUrl") or ""
    if method == "auto" and not src.startswith("http"):
        return None
    ratio = raw.get("transferRatio") or "1:1"
    rec = {
        "bank": bank,
        "bankName": BANKS[bank]["name"],
        "partner": partner,
        "partnerType": ptype,
        "partnerIcon": "\U0001F3E8" if ptype == "hotel" else "\u2708\ufe0f",
        "bonusPct": pct,
        "transferRatio": ratio,
        "bonusRatio": bonus_ratio(ratio, pct),
        "expiresDate": exp.isoformat() if exp else None,
        "startDate": (valid_date(raw.get("startDate")) or TODAY).isoformat(),
        "notes": (raw.get("notes") or "")[:140],
        "sourceUrl": src,
        "method": method,
        "lastConfirmed": today_iso,
    }
    if method == "manual":
        rec["verifiedBy"] = raw.get("verifiedBy", "HRT")
        rec["verifiedAt"] = raw.get("verifiedAt", today_iso)
    return rec


def load(path, default):
    try:
        with open(path, "r") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def key(b):
    return (b.get("bank"), canonical(b.get("partner"), b.get("partnerType"))[0])


def main():
    client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
    existing = load("bonuses.json", {"bonuses": [], "recentlyExpired": []})
    manual_file = load("manual-additions.json", {"bonuses": []})
    today_iso = TODAY.isoformat()

    # 1. manual additions (always win)
    manual = {}
    for raw in manual_file.get("bonuses", []):
        bank = raw.get("bank")
        if bank not in BANKS:
            continue
        rec = clean(bank, raw, "manual", today_iso)
        if rec:
            manual[key(rec)] = rec

    # 2. research each bank on its own
    found, status = {}, {}
    for bank in BANKS:
        print(f"Researching {BANKS[bank]['program']}...")
        try:
            data = parse_json(research(client, bank))
            n = 0
            for raw in data.get("bonuses", []):
                rec = clean(bank, raw, "auto", today_iso)
                if rec and key(rec) not in manual:
                    found[key(rec)] = rec
                    n += 1
            status[bank] = {"checked": NOW_ISO, "ok": True, "found": n}
            print(f"  {n} live")
        except Exception as e:
            status[bank] = {"checked": NOW_ISO, "ok": False, "found": 0, "error": str(e)[:120]}
            print(f"  failed: {e}")

    if not any(s["ok"] for s in status.values()):
        print("Every bank failed. Keeping the existing bonuses.json untouched.")
        return

    # 3. carry forward yesterday's bonuses that are still live but were missed today
    prior_expired = []
    for old in existing.get("bonuses", []):
        k = key(old)
        bank = old.get("bank")
        if bank not in BANKS or k in found or k in manual or old.get("method") == "manual":
            continue
        exp = valid_date(old.get("expiresDate"))
        if exp and exp < TODAY:
            prior_expired.append(old)
            continue
        last = valid_date(old.get("lastConfirmed")) or valid_date(old.get("startDate")) or TODAY
        bank_failed = not status.get(bank, {}).get("ok")
        if exp or bank_failed or (TODAY - last).days <= NO_DATE_GRACE_DAYS:
            canon, ptype = canonical(old.get("partner"), old.get("partnerType"))
            old.update({"partner": canon, "partnerType": ptype, "method": old.get("method", "auto"),
                        "lastConfirmed": old.get("lastConfirmed", old.get("startDate", today_iso))})
            if old.get("expiresDate") and not exp:
                old["expiresDate"] = None
            found[k] = old

    # 4. keep first-seen dates stable
    old_lookup = {key(b): b for b in existing.get("bonuses", [])}
    active = list(manual.values()) + list(found.values())
    for b in active:
        prev = old_lookup.get(key(b))
        if prev and prev.get("startDate"):
            b["startDate"] = min(prev["startDate"], b["startDate"])

    # 5. recently expired log, de-duplicated by bank + canonical partner + end date
    log, seen = [], set()
    for b in prior_expired + existing.get("recentlyExpired", []):
        exp = valid_date(b.get("expiresDate"))
        if not exp or not (0 <= (TODAY - exp).days <= EXPIRED_LOG_DAYS):
            continue
        canon = canonical(b.get("partner"), b.get("partnerType"))[0]
        k = (b.get("bank"), canon, exp.isoformat())
        if k in seen:
            continue
        seen.add(k)
        b["partner"] = canon
        log.append(b)
    log.sort(key=lambda b: b.get("expiresDate", ""), reverse=True)

    order = list(BANKS)
    active.sort(key=lambda b: (order.index(b["bank"]), b.get("expiresDate") or "9999", b["partner"]))
    for i, b in enumerate(active, 1):
        b["id"] = i
    for bank in BANKS:
        status[bank]["live"] = sum(1 for b in active if b["bank"] == bank)

    out = {
        "lastUpdated": NOW_ISO,
        "bonuses": active,
        "recentlyExpired": log,
        "meta": {
            "source": "HRT Auto-Updater v3.0",
            "bonusCount": len(active),
            "expiredCount": len(log),
            "banks": sorted(set(b["bank"] for b in active)),
            "banksTracked": list(BANKS),
            "bankStatus": status,
        },
    }
    with open("bonuses.json", "w") as f:
        json.dump(out, f, indent=2, ensure_ascii=False)
    print(f"Saved {len(active)} live bonuses.")
    for b in active:
        print(f"  [{b['method']}] {b['bankName']} -> {b['partner']} +{b['bonusPct']}% ends {b['expiresDate'] or 'not announced'}")


if __name__ == "__main__":
    main()
