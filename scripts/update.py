#!/usr/bin/env python3
"""Rebuild the Telegram Demand Index from the public tgden API.

Fetches https://tgden.com/api/v1/intent/week, validates it, and writes:

  data/latest.json            raw payload as served
  data/latest.csv             one row per country x niche, with shares
  data/history/YYYY-MM-DD.json  one snapshot per UTC day (the time series)
  README.md                   the generated block between the index markers

A broken or thin payload stops the run before anything is written: an empty
table that replaces yesterday's index is worse than a skipped day. The same
goes for a payload whose day is older than the newest snapshot, and for a
computedAt that is not a real calendar date (it names the history file).

Standard library only. Usage:

    python3 scripts/update.py                 # fetch from the API
    python3 scripts/update.py --dry-run       # print the block, write nothing
    python3 scripts/update.py --input week.json --pages geo.json  # offline rebuild
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import io
import json
import os
import re
import sys
import tempfile
import urllib.error
import urllib.request
from pathlib import Path

SOURCE = "https://tgden.com/api/v1/intent/week"
PAGES = "https://tgden.com/api/v1/intent/geo"  # markets that have a page on tgden.com
USER_AGENT = "telegram-demand-index/1.0 (+https://github.com/JustJuice55/telegram-demand-index)"
SITE = "https://tgden.com/en"
UTM = "utm_source=github&utm_medium=referral&utm_campaign=demand-index"
MIN_ITEMS = 50
GAIN_MIN_NOW, GAIN_MIN_PREV = 300, 100  # below this a share swings on a handful of chats
START, END = "<!-- index:start -->", "<!-- index:end -->"
ROOT = Path(__file__).resolve().parent.parent

NICHES = {
    "services": "Services & help",
    "goods": "Goods",
    "real-estate": "Real estate",
    "jobs": "Jobs",
    "freight": "Freight & logistics",
}

COUNTRIES = {
    "ab": "Abkhazia", "ae": "UAE", "al": "Albania", "am": "Armenia", "ar": "Argentina",
    "at": "Austria", "az": "Azerbaijan", "bd": "Bangladesh", "bg": "Bulgaria",
    "br": "Brazil", "by": "Belarus", "ca": "Canada", "ch": "Switzerland", "cn": "China",
    "cy": "Cyprus", "cz": "Czechia", "de": "Germany", "dk": "Denmark", "ee": "Estonia",
    "eg": "Egypt", "es": "Spain", "fi": "Finland", "fr": "France", "gb": "United Kingdom",
    "ge": "Georgia", "gr": "Greece", "hu": "Hungary", "id": "Indonesia", "ie": "Ireland",
    "il": "Israel", "in": "India", "it": "Italy", "jp": "Japan", "kg": "Kyrgyzstan",
    "kh": "Cambodia",
    "kr": "South Korea", "kz": "Kazakhstan", "lk": "Sri Lanka", "lt": "Lithuania",
    "lv": "Latvia", "ma": "Morocco", "md": "Moldova", "me": "Montenegro", "mv": "Maldives",
    "mx": "Mexico", "my": "Malaysia", "ng": "Nigeria", "nl": "Netherlands", "no": "Norway",
    "ph": "Philippines",
    "pl": "Poland", "pt": "Portugal", "qa": "Qatar", "ro": "Romania", "rs": "Serbia",
    "ru": "Russia", "sa": "Saudi Arabia", "sc": "Seychelles", "se": "Sweden",
    "sg": "Singapore", "sk": "Slovakia", "th": "Thailand", "tj": "Tajikistan",
    "tr": "Turkey", "tz": "Tanzania", "ua": "Ukraine", "us": "United States",
    "uz": "Uzbekistan", "vn": "Vietnam", "za": "South Africa",
}
NOT_ISO = {"ab"}  # no regional-indicator flag exists for these


class PayloadError(ValueError):
    """The API answered, but not with an index we are willing to publish."""


# --------------------------------------------------------------------------- fetch

def fetch(url: str, timeout: int = 60) -> object:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT,
                                               "Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read()
            if resp.status != 200:
                raise PayloadError(f"{url} answered HTTP {resp.status}")
    except urllib.error.HTTPError as e:
        raise PayloadError(f"{url} answered HTTP {e.code}") from None
    except (urllib.error.URLError, OSError) as e:
        raise PayloadError(f"{url} is unreachable: {getattr(e, 'reason', e)}") from None
    try:
        return json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as e:
        raise PayloadError(f"{url} did not answer JSON: {e}") from None


def page_set(geo: object) -> set[tuple[str, str]]:
    """(niche, country) pairs that tgden.com renders a page for.

    The site builds a market page only from this map and never for Georgia, so
    a market outside it is shown without a link instead of linking to a 404.
    """
    if not isinstance(geo, list) or not geo:
        raise PayloadError("page map is empty")
    return {(str(c.get("niche")), str(c.get("country", "")).lower()) for c in geo
            if isinstance(c, dict) and str(c.get("country", "")).lower() != "ge"}


DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}")


def snapshot_day(payload: dict) -> str:
    """The day this payload describes, checked before it becomes a filename.

    computedAt names data/history/<day>.json, so anything that is not a real
    calendar date is refused here instead of creating a file called "9999-99-99"
    or a directory named after whatever the API sent.
    """
    raw = payload.get("computedAt")
    if not isinstance(raw, str) or len(raw) < 10 or not DATE_RE.fullmatch(raw[:10]):
        raise PayloadError(f"computedAt is not an ISO date: {str(raw)[:32]!r}")
    try:
        return dt.date.fromisoformat(raw[:10]).isoformat()
    except ValueError:
        raise PayloadError(f"computedAt is not a calendar date: {raw[:32]!r}") from None


def count(item: dict, key: str) -> int:
    """A non-negative int field, with bools refused (True is an int in Python)."""
    value = item.get(key)
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise PayloadError(f"{key} is not a non-negative int: {item!r:.120}")
    return value


def as_payload(value: object) -> dict:
    if not isinstance(value, dict):
        raise PayloadError("payload is not an object")
    return value


def validate(payload: object) -> list[dict]:
    payload = as_payload(payload)
    items = payload.get("items")
    if not isinstance(items, list) or len(items) < MIN_ITEMS:
        n = len(items) if isinstance(items, list) else "no"
        raise PayloadError(f"{n} items, need at least {MIN_ITEMS}")
    snapshot_day(payload)
    seen: set[tuple[str, str]] = set()
    for i in items:
        if not isinstance(i, dict):
            raise PayloadError(f"item is not an object: {i!r:.120}")
        for key in ("country", "niche"):
            if not isinstance(i.get(key), str) or not i[key]:
                raise PayloadError(f"item without {key}: {i!r:.120}")
        market_key = (i["country"].lower(), i["niche"])
        if market_key in seen:
            # Two rows for one market double its country's denominator and print twice.
            raise PayloadError(f"duplicate market: {i['country']}/{i['niche']}")
        seen.add(market_key)
        for key in ("requests7d", "requestsPrev7d", "requests30d", "chats"):
            count(i, key)
        chats = i.get("topChats", [])
        if not isinstance(chats, list):
            raise PayloadError("topChats is not a list")
        for c in chats:
            if not isinstance(c, dict):
                raise PayloadError(f"topChats entry is not an object: {c!r:.80}")
            if c.get("username") is not None and not isinstance(c["username"], str):
                raise PayloadError(f"topChats username is not a string: {c!r:.80}")
    return items


# --------------------------------------------------------------------------- numbers

def country_name(code: str) -> str:
    return COUNTRIES.get(code, code.upper())


def flag(code: str) -> str:
    if code in NOT_ISO or len(code) != 2 or not code.isalpha():
        return ""
    return "".join(chr(0x1F1E6 + ord(ch) - ord("a")) for ch in code.lower()) + " "


def niche_name(code: str) -> str:
    return NICHES.get(code, code.replace("-", " ").capitalize())


def intent_url(item: dict) -> str:
    return f"{SITE}/intent/{item['niche']}/{item['country']}?{UTM}"


def market(item: dict, pages: set[tuple[str, str]], with_country: bool = True) -> str:
    label = (f"{flag(item['country'])}{country_name(item['country'])} · {niche_name(item['niche'])}"
             if with_country else niche_name(item["niche"]))
    if (item["niche"], item["country"]) in pages:
        return f"[{label}]({intent_url(item)})"
    return label


def chat_url(username: str) -> str:
    return f"{SITE}/chat/{username}?{UTM}"


def rows(items: list[dict]) -> list[dict]:
    """Items with each niche's share of its country, this week and last week.

    Absolute counts move with how many chats the reader covered that week, so
    the share inside a country is the number to compare across weeks.
    """
    now: dict[str, int] = {}
    prev: dict[str, int] = {}
    for i in items:
        now[i["country"]] = now.get(i["country"], 0) + i["requests7d"]
        prev[i["country"]] = prev.get(i["country"], 0) + i["requestsPrev7d"]
    out = []
    for i in items:
        c = i["country"]
        share = i["requests7d"] / now[c] if now[c] else 0.0
        share_prev = i["requestsPrev7d"] / prev[c] if prev[c] else 0.0
        out.append({**i, "share7d": share, "sharePrev7d": share_prev,
                    "shareDeltaPp": (share - share_prev) * 100})
    return out


def fmt(n: int) -> str:
    return f"{n:,}"


def pct(x: float) -> str:
    return f"{x * 100:.0f}%"


def pp(x: float) -> str:
    if abs(x) < 0.5:
        return "±0"
    return f"{'+' if x > 0 else '−'}{abs(x):.0f} pp"


def chats_cell(item: dict, limit: int = 3) -> str:
    links = [f"[@{c['username']}]({chat_url(c['username'])})"
             for c in item.get("topChats", [])[:limit] if c.get("username")]
    return ", ".join(links) or "—"


# --------------------------------------------------------------------------- render

def coverage(payload: dict) -> str | None:
    """What the reader covered, when the payload reports it.

    Counts grow whenever more chats are read, which is why the README compares
    shares. Saying the coverage out loud lets a reader see how much of a week's
    movement is the reader's own reach rather than demand.
    """
    obs = payload.get("observation")
    if not isinstance(obs, dict):
        return None
    cur, prev = obs.get("current7d"), obs.get("prev7d")
    if not isinstance(cur, dict):
        return None
    observed, labeled = cur.get("messagesObserved"), cur.get("messagesLabeled")
    if not isinstance(observed, int) or not isinstance(labeled, int) or observed <= 0:
        return None
    line = (f"**What the reader saw** — {fmt(observed)} messages scanned this week, "
            f"{fmt(labeled)} of them requests ({rate(cur, observed, labeled)})")
    if isinstance(prev, dict):
        p_obs, p_lab = prev.get("messagesObserved"), prev.get("messagesLabeled")
        if isinstance(p_obs, int) and isinstance(p_lab, int) and p_obs > 0:
            line += (f", a week earlier {fmt(p_lab)} of {fmt(p_obs)} "
                     f"({rate(prev, p_obs, p_lab)})")
    return line + ". Counts follow coverage, so compare shares, not totals."


def rate(window: dict, observed: int, labeled: int) -> str:
    pct = window.get("coveragePct")
    if isinstance(pct, (int, float)) and not isinstance(pct, bool):
        return f"{pct:.1f}%"
    return f"{labeled / observed * 100:.1f}%"


def render(payload: dict, items: list[dict], pages: set[tuple[str, str]]) -> str:
    rs = rows(items)
    day = snapshot_day(payload)
    total = sum(r["requests7d"] for r in rs)
    by_country: dict[str, list[dict]] = {}
    for r in rs:
        by_country.setdefault(r["country"], []).append(r)
    order = sorted(by_country, key=lambda c: -sum(r["requests7d"] for r in by_country[c]))

    out: list[str] = []
    w = out.append
    w(f"**Updated {day}** · **{fmt(total)} requests** in the last 7 days · "
      f"**{len(by_country)} countries** · **{len(rs)} markets** (country × niche)")
    w("")
    seen = coverage(payload)
    if seen:
        w(seen)
        w("")
    w("### Biggest markets this week")
    w("")
    w("| # | Market | Requests, 7 days | Share of the country | Chats with requests | Where people ask |")
    w("|---:|---|---:|---:|---:|---|")
    for n, r in enumerate(sorted(rs, key=lambda r: -r["requests7d"])[:15], 1):
        w(f"| {n} | {market(r, pages)} | {fmt(r['requests7d'])} | {pct(r['share7d'])} | "
          f"{fmt(r['chats'])} | {chats_cell(r)} |")
    w("")

    gaining = [r for r in rs if r["requests7d"] >= GAIN_MIN_NOW
               and r["requestsPrev7d"] >= GAIN_MIN_PREV and r["shareDeltaPp"] >= 1]
    gaining.sort(key=lambda r: -r["shareDeltaPp"])
    if gaining:
        w("### Gaining share this week")
        w("")
        w(f"Share of all requests in that country, this week against last week, for markets with "
          f"{GAIN_MIN_NOW}+ requests. Raw counts grow whenever more chats are read; a share moves only "
          f"when one kind of request grows faster than the rest, so it is the closer sign of a real shift.")
        w("")
        w("| Market | Share now | Share a week ago | Change | Requests, 7 days |")
        w("|---|---:|---:|---:|---:|")
        for r in gaining[:10]:
            w(f"| {market(r, pages)} | {pct(r['share7d'])} | {pct(r['sharePrev7d'])} | "
              f"{pp(r['shareDeltaPp'])} | {fmt(r['requests7d'])} |")
        w("")

    w("### By country")
    w("")
    for c in order:
        group = sorted(by_country[c], key=lambda r: -r["requests7d"])
        ctotal = sum(r["requests7d"] for r in group)
        if ctotal < 100:
            continue
        w(f"<details><summary><b>{flag(c)}{country_name(c)}</b> — {fmt(ctotal)} requests</summary>")
        w("")
        w("| Niche | Requests, 7 days | Last 30 days | Share | Chats | Where people ask |")
        w("|---|---:|---:|---:|---:|---|")
        for r in group:
            w(f"| {market(r, pages, with_country=False)} | {fmt(r['requests7d'])} | "
              f"{fmt(r['requests30d'])} | {pct(r['share7d'])} | {fmt(r['chats'])} | {chats_cell(r)} |")
        w("")
        w("</details>")
        w("")
    small = [c for c in order if sum(r["requests7d"] for r in by_country[c]) < 100]
    if small:
        w("Fewer than 100 requests this week: " + ", ".join(
            f"{flag(c)}{country_name(c)}" for c in small) + ". Full numbers in "
          "[`data/latest.csv`](data/latest.csv).")
        w("")
    return "\n".join(out).rstrip() + "\n"


def splice(readme: str, block: str) -> str:
    a, b = readme.find(START), readme.find(END)
    if a < 0 or b < a:
        raise PayloadError("README has no index markers")
    return readme[: a + len(START)] + "\n" + block + readme[b:]


def to_csv(items: list[dict], pages: set[tuple[str, str]]) -> str:
    buf = io.StringIO()
    fields = ["country", "country_name", "niche", "requests7d", "requestsPrev7d", "requests30d",
              "chats", "share7d", "sharePrev7d", "top_chats", "url"]
    wr = csv.DictWriter(buf, fieldnames=fields, lineterminator="\n")
    wr.writeheader()
    for r in sorted(rows(items), key=lambda r: (r["country"], -r["requests7d"])):
        wr.writerow({
            "country": r["country"], "country_name": country_name(r["country"]),
            "niche": r["niche"], "requests7d": r["requests7d"],
            "requestsPrev7d": r["requestsPrev7d"], "requests30d": r["requests30d"],
            "chats": r["chats"], "share7d": f"{r['share7d']:.4f}",
            "sharePrev7d": f"{r['sharePrev7d']:.4f}",
            "top_chats": " ".join(c["username"] for c in r.get("topChats", []) if c.get("username")),
            "url": (f"{SITE}/intent/{r['niche']}/{r['country']}"
                    if (r["niche"], r["country"]) in pages else ""),
        })
    return buf.getvalue()


# --------------------------------------------------------------------------- write

def write_atomic(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
            f.write(text)
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


def newest_snapshot(root: Path | None = None) -> str | None:
    """The latest day already published, if any."""
    root = ROOT if root is None else Path(root)
    history = root / "data" / "history"
    if not history.is_dir():
        return None
    days = [p.stem for p in history.glob("*.json") if DATE_RE.fullmatch(p.stem)]
    return max(days) if days else None


def build(payload: dict, geo: object, root: Path | None = None) -> str:
    """Validate first, render everything in memory, then write. Returns the day."""
    root = ROOT if root is None else Path(root)
    items = validate(payload)
    pages = page_set(geo)
    day = snapshot_day(payload)
    newest = newest_snapshot(root)
    if newest and day < newest:
        # A cached or backdated payload must not rewrite a newer day's numbers.
        raise PayloadError(f"payload is for {day}, older than the newest snapshot {newest}")
    readme_path = root / "README.md"
    readme = splice(readme_path.read_text(encoding="utf-8"), render(payload, items, pages))
    raw = json.dumps(payload, ensure_ascii=False, indent=1) + "\n"
    write_atomic(root / "data" / "history" / f"{day}.json", raw)
    write_atomic(root / "data" / "latest.json", raw)
    write_atomic(root / "data" / "latest.csv", to_csv(items, pages))
    write_atomic(readme_path, readme)
    return day


def load(path: Path, what: str) -> object:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except OSError as e:
        raise PayloadError(f"cannot read the {what} {path}: {e}") from None
    except json.JSONDecodeError as e:
        raise PayloadError(f"the {what} {path} is not JSON: {e}") from None


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--input", type=Path, help="build from a saved payload instead of the API")
    ap.add_argument("--pages", type=Path, help="saved page map to go with --input")
    ap.add_argument("--dry-run", action="store_true",
                    help="validate and print the block, write nothing")
    args = ap.parse_args(argv)
    if bool(args.input) != bool(args.pages):
        ap.error("--input and --pages go together: a payload without its page map "
                 "would publish every market unlinked")
    try:
        if args.input:
            payload = as_payload(load(args.input, "payload"))
            geo = load(args.pages, "page map")
        else:
            payload = as_payload(fetch(SOURCE))
            geo = fetch(PAGES)
        if args.dry_run:
            items, pages = validate(payload), page_set(geo)
            day = snapshot_day(payload)
            print(render(payload, items, pages))
            newest = newest_snapshot()
            note = f" (older than {newest}, a run would refuse it)" if newest and day < newest else ""
            print(f"dry run for {day}: {len(items)} markets, nothing written{note}", file=sys.stderr)
            return 0
        day = build(payload, geo)
    except PayloadError as e:
        print(f"refused to publish: {e}", file=sys.stderr)
        return 1
    print(f"index rebuilt for {day}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
