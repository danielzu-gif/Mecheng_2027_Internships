#!/usr/bin/env python3
"""
Summer 2027 Mechanical Engineering internship watcher.

Two layers:
  1. ATS polling  — hits the public job-board APIs of the companies in
     targets.yml directly. This is the day-of-drop layer: Greenhouse hands
     back `first_published`, Lever hands back `createdAt`, so a posting is
     detectable minutes after it goes live and long before a tracker repo
     picks it up. It also returns the full job description, which is the only
     reliable way to tell what season a posting is for.
  2. Tracker scraping — the community README lists, as a wide net for
     companies that aren't in targets.yml yet.

Everything is scored, deduped across layers, and routed either to an instant
phone push or to a once-a-day digest.

  python watcher.py                  normal run
  python watcher.py --dry-run        print what it would send, send nothing
  python watcher.py --seed           record current state, send nothing
  python watcher.py --verify-boards  check every ATS token in targets.yml
  python watcher.py --digest         force the digest to send now
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import html as htmllib
import json
import os
import re
import sys
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests
import yaml

ROOT = Path(__file__).resolve().parent
CONFIG_PATH = ROOT / "targets.yml"
STATE_PATH = ROOT / "state" / "seen.json"
ARCHIVE_PATH = ROOT / "state" / "postings.csv"
DIGEST_PATH = ROOT / "state" / "digest.md"

NTFY_TOPIC = os.environ.get("NTFY_TOPIC", "").strip()
NTFY_HOST = os.environ.get("NTFY_HOST", "https://ntfy.sh").rstrip("/")
UA = "internship-watcher/2.0 (personal job alert bot)"

SESSION = requests.Session()
SESSION.headers.update({"User-Agent": UA})

NOW = datetime.now(timezone.utc)


# --------------------------------------------------------------------------
# small helpers
# --------------------------------------------------------------------------

def log(msg: str) -> None:
    print(msg, flush=True)


def clean(text: str) -> str:
    """Markdown/HTML cell -> plain text."""
    text = re.sub(r"<[^>]+>", " ", text or "")
    text = htmllib.unescape(text)
    text = text.replace("**", "").replace("*", "")
    text = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", text)
    text = re.sub(r"[\U0001F000-\U0001FAFF\u2190-\u21FF\u2600-\u27BF]", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", (text or "").lower())


def first_url(cell: str) -> str | None:
    for pat in (r'href="([^"]+)"', r"\]\((https?://[^)\s]+)\)", r"(https?://[^\s\"'<>\)]+)"):
        m = re.search(pat, cell or "")
        if m:
            url = htmllib.unescape(m.group(1))
            if not any(x in url for x in (".png", ".jpg", ".gif", "imgur.com")):
                return url
    return None


def sha(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8")).hexdigest()[:16]


def iso(dt: datetime | None) -> str | None:
    return dt.astimezone(timezone.utc).isoformat() if dt else None


def parse_iso(value) -> datetime | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def human_age(dt: datetime | None) -> str:
    if not dt:
        return "posted date unknown"
    delta = NOW - dt
    hours = delta.total_seconds() / 3600
    if hours < 1:
        return f"posted {int(delta.total_seconds() / 60)}m ago"
    if hours < 48:
        return f"posted {int(hours)}h ago"
    return f"posted {int(hours / 24)}d ago"


# --------------------------------------------------------------------------
# posting model
# --------------------------------------------------------------------------

@dataclass
class Posting:
    company: str
    role: str
    location: str = ""
    url: str | None = None
    source: str = ""
    source_kind: str = "tracker"      # "ats" or "tracker"
    posted_at: datetime | None = None
    description: str = ""
    season_hint: str | None = None

    tier: int = 9
    score: int = 0
    term: str = "unknown"             # target | offseason | assumed | unknown
    term_evidence: str = ""
    focus_hit: bool = False
    reasons: list = field(default_factory=list)
    also_seen: list = field(default_factory=list)

    @property
    def key(self) -> str:
        role_core = re.sub(
            r"\b(summer|fall|autumn|spring|winter)\b|\b20\d{2}\b|\b'?\d{2}\b",
            "", self.role.lower())
        return f"{slug(self.company)}|{slug(role_core)}"

    @property
    def tier_label(self) -> str:
        return {1: "Tier 1", 2: "Tier 2", 3: "Tier 3"}.get(self.tier, "unlisted")


# --------------------------------------------------------------------------
# table parsing — markdown pipe tables AND html tables, every table in the doc
# --------------------------------------------------------------------------

FIELD_ALIASES = {
    "company": ("company", "org", "employer"),
    "role": ("role", "job title", "title", "position", "opportunity"),
    "location": ("location", "locations", "city"),
    "url": ("application", "apply", "link", "application/link"),
    "date": ("date posted", "posted", "added", "age", "date"),
}


def _map_columns(header: list[str]) -> dict:
    low = [h.lower().strip() for h in header]
    mapping = {}
    for field_name, aliases in FIELD_ALIASES.items():
        for alias in aliases:
            for i, h in enumerate(low):
                if alias == h or alias in h:
                    mapping.setdefault(field_name, i)
                    break
            if field_name in mapping:
                break
    return mapping


def _markdown_tables(text: str):
    lines = text.splitlines()
    i = 0
    while i < len(lines):
        line = lines[i].strip()
        nxt = lines[i + 1].strip() if i + 1 < len(lines) else ""
        if line.startswith("|") and re.match(r"^\|[\s\-:|]+\|?\s*$", nxt):
            header = [c.strip() for c in line.strip("|").split("|")]
            rows, j = [], i + 2
            while j < len(lines) and lines[j].strip().startswith("|"):
                rows.append([c.strip() for c in lines[j].strip().strip("|").split("|")])
                j += 1
            yield header, rows
            i = j
        else:
            i += 1


def _html_tables(text: str):
    for table in re.findall(r"<table[^>]*>.*?</table>", text, re.S | re.I):
        header = [clean(h) for h in re.findall(r"<th[^>]*>(.*?)</th>", table, re.S | re.I)]
        if not header:
            continue
        rows = []
        for tr in re.findall(r"<tr[^>]*>(.*?)</tr>", table, re.S | re.I):
            cells = re.findall(r"<td[^>]*>(.*?)</td>", tr, re.S | re.I)
            if cells:
                rows.append(cells)
        yield header, rows


REL_AGE = re.compile(r"^(\d+)\s*(h|hr|hrs|hour|d|day|days|w|wk|mo|month)s?\b", re.I)
MONTH_DAY = re.compile(r"^([A-Z][a-z]{2})\.?\s+(\d{1,2})$")


def parse_posted(value: str) -> datetime | None:
    value = (value or "").strip()
    if not value or value.lower() in {"undated", "date unknown", "n/a", "-"}:
        return None
    m = REL_AGE.match(value)
    if m:
        n, unit = int(m.group(1)), m.group(2).lower()
        hours = {"h": 1, "hr": 1, "hrs": 1, "hour": 1,
                 "d": 24, "day": 24, "days": 24,
                 "w": 168, "wk": 168, "mo": 720, "month": 720}.get(unit, 24)
        return NOW - timedelta(hours=n * hours)
    dt = parse_iso(value)
    if dt:
        return dt
    m = MONTH_DAY.match(value)
    if m:
        try:
            guess = datetime.strptime(f"{m.group(1)} {m.group(2)} {NOW.year}", "%b %d %Y").replace(tzinfo=timezone.utc)
            # a date more than a week in the future means it's from last year
            return guess - timedelta(days=365) if guess > NOW + timedelta(days=7) else guess
        except ValueError:
            return None
    return None


def parse_tracker(text: str, source: str, season_hint: str | None) -> list[Posting]:
    postings, last_company = [], None
    for header, rows in list(_markdown_tables(text)) + list(_html_tables(text)):
        cols = _map_columns(header)
        if "role" not in cols or "company" not in cols:
            continue
        for row in rows:
            def cell(name):
                i = cols.get(name)
                return row[i] if i is not None and i < len(row) else ""

            company = clean(cell("company"))
            if company in {"↳", "->", ""} and last_company:
                company = last_company
            elif company:
                last_company = company
            role = clean(cell("role"))
            if not company or not role:
                continue
            url = first_url(cell("url")) or first_url(cell("role")) or first_url(cell("company"))
            postings.append(Posting(
                company=company,
                role=role,
                location=clean(cell("location")),
                url=url,
                source=source,
                source_kind="tracker",
                posted_at=parse_posted(clean(cell("date"))),
                season_hint=season_hint,
            ))
    return postings


# --------------------------------------------------------------------------
# ATS clients — the day-of-drop layer
# --------------------------------------------------------------------------

def _get_json(url: str, timeout: int = 20):
    r = SESSION.get(url, timeout=timeout)
    r.raise_for_status()
    return r.json()


def fetch_greenhouse(company: str, board: str) -> list[Posting]:
    data = _get_json(f"https://boards-api.greenhouse.io/v1/boards/{board}/jobs?content=true")
    out = []
    for job in data.get("jobs", []):
        out.append(Posting(
            company=company,
            role=job.get("title", ""),
            location=(job.get("location") or {}).get("name", ""),
            url=job.get("absolute_url"),
            source=f"greenhouse:{board}",
            source_kind="ats",
            posted_at=parse_iso(job.get("first_published") or job.get("updated_at")),
            description=clean(job.get("content", ""))[:6000],
        ))
    return out


def fetch_lever(company: str, board: str) -> list[Posting]:
    data = _get_json(f"https://api.lever.co/v0/postings/{board}?mode=json")
    out = []
    for job in data:
        created = job.get("createdAt")
        posted = datetime.fromtimestamp(created / 1000, timezone.utc) if created else None
        out.append(Posting(
            company=company,
            role=job.get("text", ""),
            location=(job.get("categories") or {}).get("location", ""),
            url=job.get("hostedUrl"),
            source=f"lever:{board}",
            source_kind="ats",
            posted_at=posted,
            description=clean(job.get("descriptionPlain") or job.get("description", ""))[:6000],
        ))
    return out


def fetch_ashby(company: str, board: str) -> list[Posting]:
    data = _get_json(f"https://api.ashbyhq.com/posting-api/job-board/{board}?includeCompensation=true")
    out = []
    for job in data.get("jobs", []):
        out.append(Posting(
            company=company,
            role=job.get("title", ""),
            location=job.get("location", ""),
            url=job.get("jobUrl") or job.get("applyUrl"),
            source=f"ashby:{board}",
            source_kind="ats",
            posted_at=parse_iso(job.get("publishedAt") or job.get("updatedAt")),
            description=clean(job.get("descriptionPlain") or job.get("descriptionHtml", ""))[:6000],
        ))
    return out


def fetch_smartrecruiters(company: str, board: str) -> list[Posting]:
    data = _get_json(f"https://api.smartrecruiters.com/v1/companies/{board}/postings?limit=100")
    out = []
    for job in data.get("content", []):
        loc = job.get("location") or {}
        out.append(Posting(
            company=company,
            role=job.get("name", ""),
            location=", ".join(x for x in (loc.get("city"), loc.get("region")) if x),
            url=f"https://jobs.smartrecruiters.com/{board}/{job.get('id')}",
            source=f"smartrecruiters:{board}",
            source_kind="ats",
            posted_at=parse_iso(job.get("releasedDate")),
        ))
    return out


ATS_CLIENTS = {
    "greenhouse": fetch_greenhouse,
    "lever": fetch_lever,
    "ashby": fetch_ashby,
    "smartrecruiters": fetch_smartrecruiters,
}


# --------------------------------------------------------------------------
# season classification — the actual fix for the noise problem
# --------------------------------------------------------------------------

SEASON_WORD = r"(summer|spring|fall|autumn|winter)"
SEASON_PATTERNS = [
    re.compile(rf"{SEASON_WORD}[\s\-_/,]*'?(\d{{2,4}})", re.I),
    re.compile(rf"\b(\d{{4}})[\s\-_/,]*{SEASON_WORD}", re.I),
]
BARE_SEASON = re.compile(rf"{SEASON_WORD}\s+(semester|term|internship|intern|co-?op|program|start)", re.I)
# In a *title*, a lone season word is decisive even with no year attached —
# "Propulsion Manufacturing Intern (Fall..." is a fall role, truncation or not.
LONE_SEASON = re.compile(rf"\b{SEASON_WORD}\b", re.I)
MONTH_YEAR = re.compile(r"\b(may|june|jun|july)\s+(\d{4})\b", re.I)
TRUNCATED = re.compile(r"(\.\.\.|…)\s*$")


def _norm_year(raw: str) -> int | None:
    try:
        n = int(raw)
    except (TypeError, ValueError):
        return None
    if n < 100:
        n += 2000
    return n if 2020 <= n <= 2035 else None


def find_seasons(text: str) -> list[tuple[str, int | None]]:
    """Return [(season_word, year|None), ...] found in text."""
    hits = []
    for pat in SEASON_PATTERNS:
        for m in pat.finditer(text or ""):
            groups = m.groups()
            if groups[0].isdigit():
                hits.append((groups[1].lower(), _norm_year(groups[0])))
            else:
                hits.append((groups[0].lower(), _norm_year(groups[1])))
    for m in BARE_SEASON.finditer(text or ""):
        hits.append((m.group(1).lower(), None))
    return hits


def classify_term(post: Posting, cfg: dict) -> tuple[str, str]:
    term_word = cfg["season"]["term"].lower()
    year = int(cfg["season"]["year"])
    blob_title = post.role
    blob_desc = post.description or ""

    for blob, where in ((blob_title, "title"), (blob_desc, "description")):
        if not blob:
            continue
        hits = find_seasons(blob)
        if where == "title":
            # a bare year in the title, cycle-appropriate, is a real signal:
            # "2027 Mechanical Engineer Intern" is a Summer 2027 req
            if not hits and re.search(rf"\b{year}\b", blob):
                return "target", f"target year in {where}"
            if not hits:
                hits = [(m.group(1).lower(), None) for m in LONE_SEASON.finditer(blob)]
        # an exact target hit anywhere wins outright
        for season, yr in hits:
            if season == term_word and yr == year:
                return "target", f"{season} {yr} in {where}"
        for m in MONTH_YEAR.finditer(blob):
            if _norm_year(m.group(2)) == year:
                return "target", f"{m.group(0)} in {where}"
        # a season word with no year, in the right term, during the right window
        for season, yr in hits:
            if season == term_word and yr is None:
                return "target", f"unqualified '{season}' in {where}"
        # explicit other season/year
        for season, yr in hits:
            if yr is not None and (season != term_word or yr != year):
                return "offseason", f"{season} {yr} in {where}"
        for season, yr in hits:
            if season != term_word:
                return "offseason", f"'{season}' in {where} with no year"

    if post.season_hint and term_word in post.season_hint and str(year) in post.season_hint:
        return "target", "source is a target-season-only list"

    if TRUNCATED.search(post.role) and not post.description:
        return "unknown", "title truncated by the source, season unreadable"

    start, end = (parse_iso(d + "T00:00:00+00:00") for d in cfg["season"]["assume_window"])
    stamp = post.posted_at or NOW
    if start and end and start <= stamp <= end:
        return "assumed", "no season stated, inside the target hiring window"
    return "unknown", "no season stated"


# --------------------------------------------------------------------------
# scoring
# --------------------------------------------------------------------------

INTERNISH = re.compile(r"\b(intern|internship|co-?op|apprentice|student)\b", re.I)
# unambiguous non-US suffixes only — "CA" and "IN" are California and Indiana
FOREIGN_CODE = re.compile(r",\s*(DE|UK|GB|FR|ES|MX|JP|SG|AU|IL|NL|PL|BR|KR|CN|IT|SE|CH|IE|ON|QC|BC|AB|MB|SK|NS)\s*$", re.I)


def build_company_index(cfg: dict) -> dict:
    index = {}
    for tier, entries in ((1, cfg["companies"]["tier1"]),
                          (2, cfg["companies"]["tier2"]),
                          (3, cfg["companies"]["tier3"])):
        for entry in entries or []:
            name = entry["name"] if isinstance(entry, dict) else entry
            index[slug(name)] = {"tier": tier, "name": name,
                                 "entry": entry if isinstance(entry, dict) else {"name": name}}
    return index


def match_company(name: str, index: dict) -> dict | None:
    s = slug(name)
    if not s:
        return None
    if s in index:
        return index[s]
    for key, meta in index.items():
        if len(key) >= 5 and (key in s or s in key):
            return meta
    return None


def score_posting(post: Posting, cfg: dict, index: dict) -> None:
    roles = cfg["roles"]
    title = post.role.lower()
    loc = post.location.lower()

    meta = match_company(post.company, index)
    post.tier = meta["tier"] if meta else 9

    reasons, score = [], 0

    for bad in roles["negative"]:
        if bad in title:
            post.score = -99
            post.reasons = [f"negative keyword: {bad}"]
            return

    for blocked in cfg.get("location_blocklist", []):
        if blocked in loc:
            post.score = -99
            post.reasons = [f"location blocked: {blocked}"]
            return
    if FOREIGN_CODE.search(post.location):
        post.score = -99
        post.reasons = ["location outside the US"]
        return

    # "software" in the title with nothing mechanical alongside it is a SWE req
    if "software" in title and not any(k in title for k in roles["core"] + roles["focus"]):
        post.score = -99
        post.reasons = ["software role"]
        return

    truncated = bool(TRUNCATED.search(post.role))
    if not INTERNISH.search(title) and not truncated:
        score -= 6
        reasons.append("title doesn't read like an internship (-6)")

    if any(k in title for k in roles["core"]):
        score += 5
        reasons.append("core ME title (+5)")
    focus = [k for k in roles["focus"] if k in title or (post.description and k in post.description.lower()[:1500])]
    if focus:
        score += 5
        post.focus_hit = True
        reasons.append(f"focus area: {focus[0]} (+5)")
    if any(k in title for k in roles["adjacent"]):
        score += 3
        reasons.append("adjacent hardware role (+3)")
    if post.tier <= 2 and any(k in title for k in roles["generic"]):
        score += 2
        reasons.append("generic engineering intern at a target company (+2)")

    tier_bonus = {1: 4, 2: 2, 3: 0}.get(post.tier, -1)
    score += tier_bonus
    reasons.append(f"{post.tier_label} company ({tier_bonus:+d})")

    if any(k in title for k in roles["eligibility_penalty"]):
        score -= 4
        reasons.append("grad-level or returning-student wording (-4)")

    if not any(r.endswith(("(+5)", "(+3)", "(+2)")) for r in reasons):
        post.score = -99
        post.reasons = ["no relevant role signal in the title"]
        return

    post.score = score
    post.reasons = reasons


# --------------------------------------------------------------------------
# state
# --------------------------------------------------------------------------

def load_state() -> dict:
    if not STATE_PATH.exists():
        legacy = ROOT / "seen.json"
        if legacy.exists():
            # The old ids were "company|role|url"; the new key drops the url and
            # strips season words, so old ids can't be matched against new ones.
            # Nothing to carry over — the empty state triggers seed mode, which
            # is what actually stops a first-run flood.
            log("found the old seen.json — ignoring it and seeding fresh")
        return {"version": 2, "postings": {}, "desc_cache": {}, "source_health": {},
                "digest_queue": [], "last_digest": None}
    state = json.loads(STATE_PATH.read_text())
    for key in ("postings", "desc_cache", "source_health"):
        state.setdefault(key, {})
    state.setdefault("digest_queue", [])
    state.setdefault("last_digest", None)
    return state


def save_state(state: dict) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps(state, indent=1, sort_keys=True))


def archive(postings: list[Posting]) -> None:
    """Append-only log. Two seasons of this and you know exactly when every
    company drops, which is worth more next cycle than any of the alerts."""
    ARCHIVE_PATH.parent.mkdir(parents=True, exist_ok=True)
    new = not ARCHIVE_PATH.exists()
    with ARCHIVE_PATH.open("a", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        if new:
            w.writerow(["first_seen", "posted_at", "company", "tier", "role",
                        "location", "term", "score", "source", "url"])
        for p in postings:
            w.writerow([iso(NOW), iso(p.posted_at) or "", p.company, p.tier, p.role,
                        p.location, p.term, p.score, p.source, p.url or ""])


# --------------------------------------------------------------------------
# notification
# --------------------------------------------------------------------------

def push(title: str, message: str, priority: int = 3, tags: str = "rocket",
         click: str | None = None, dry: bool = False) -> None:
    if dry or not NTFY_TOPIC:
        log(f"\n[{'DRY' if dry else 'NO TOPIC'}] {title}\n{message}\n")
        return
    headers = {"Title": title, "Priority": str(priority), "Tags": tags}
    if click:
        headers["Click"] = click
    try:
        SESSION.post(f"{NTFY_HOST}/{NTFY_TOPIC}", data=message.encode("utf-8"),
                     headers=headers, timeout=15)
    except requests.RequestException as e:
        log(f"push failed: {e}")


def instant_push(p: Posting, dry: bool) -> None:
    icon = "🚀" if p.focus_hit else "🔧"
    season = {"target": f"Summer {2027}", "assumed": "season unconfirmed"}.get(p.term, p.term)
    title = f"{icon} {p.company} — {p.role}"[:120]
    body = [
        f"{p.location or 'location n/a'} · {p.tier_label} · score {p.score}",
        f"{human_age(p.posted_at)} · {'direct from ' + p.source.split(':')[0] if p.source_kind == 'ats' else p.source}",
        season if p.term != "target" else f"Summer 2027 confirmed ({p.term_evidence})",
    ]
    if p.also_seen:
        body.append("also listed on: " + ", ".join(p.also_seen))
    if p.url:
        body.append(p.url)
    age_h = (NOW - p.posted_at).total_seconds() / 3600 if p.posted_at else 999
    priority = 5 if (p.tier == 1 and age_h < 12) else 4 if p.tier <= 2 else 3
    tags = "rocket" if p.focus_hit else "wrench"
    push(title, "\n".join(body), priority, tags, p.url, dry)


def send_digest(state: dict, cfg: dict, dry: bool, force: bool = False) -> None:
    queue = state.get("digest_queue", [])
    if not queue:
        return
    last = parse_iso(state.get("last_digest"))
    due = force or not last or (NOW - last) > timedelta(hours=20)
    if not due:
        return
    by_term = {}
    for item in queue:
        by_term.setdefault(item["term"], []).append(item)
    lines = []
    for term in ("target", "assumed", "unknown"):
        items = by_term.get(term, [])
        if not items:
            continue
        label = {"target": "Summer 2027 confirmed", "assumed": "probably Summer 2027",
                 "unknown": "season unclear"}[term]
        lines.append(f"— {label} ({len(items)}) —")
        for item in sorted(items, key=lambda x: -x["score"])[:15]:
            lines.append(f"{item['company']}: {item['role'][:58]} · {item['location'][:26]}")
    push(f"📋 Digest — {len(queue)} new roles", "\n".join(lines)[:3800], 3, "clipboard", None, dry)
    DIGEST_PATH.parent.mkdir(parents=True, exist_ok=True)
    DIGEST_PATH.write_text(f"# Digest {NOW:%Y-%m-%d %H:%M} UTC\n\n" + "\n".join(
        f"- **{i['company']}** — {i['role']} · {i['location']} · score {i['score']} · [link]({i['url']})"
        for i in sorted(queue, key=lambda x: -x["score"])))
    if not dry:
        state["digest_queue"] = []
        state["last_digest"] = iso(NOW)


# --------------------------------------------------------------------------
# collection
# --------------------------------------------------------------------------

def collect_ats(cfg: dict, state: dict) -> list[Posting]:
    jobs = []
    for tier in ("tier1", "tier2"):
        for entry in cfg["companies"][tier] or []:
            if isinstance(entry, dict) and entry.get("ats") and entry.get("board"):
                jobs.append(entry)

    out: list[Posting] = []

    def one(entry):
        fn = ATS_CLIENTS.get(entry["ats"])
        if not fn:
            return []
        try:
            return fn(entry["name"], entry["board"])
        except Exception as e:
            log(f"  ats {entry['name']} ({entry['ats']}:{entry['board']}) failed: {type(e).__name__}")
            return []

    with ThreadPoolExecutor(max_workers=8) as pool:
        for result in pool.map(one, jobs):
            out.extend(result)
    log(f"ATS layer: {len(out)} postings from {len(jobs)} boards")
    return out


def collect_trackers(cfg: dict, state: dict, dry: bool) -> list[Posting]:
    out = []
    for src in cfg["trackers"]:
        try:
            r = SESSION.get(src["url"], timeout=25)
            r.raise_for_status()
            rows = parse_tracker(r.text, src["name"], src.get("season_hint"))
        except requests.RequestException as e:
            log(f"  tracker {src['name']} unreachable: {e}")
            rows = []
        health = state["source_health"].setdefault(src["name"], {})
        if rows:
            health["last_ok"] = iso(NOW)
            health["rows"] = len(rows)
        else:
            last_warn = parse_iso(health.get("last_warned"))
            if not last_warn or (NOW - last_warn) > timedelta(hours=24):
                health["last_warned"] = iso(NOW)
                push("⚠️ Tracker source returned nothing",
                     f"{src['name']} parsed 0 rows.\nThe repo was probably renamed or "
                     f"reformatted — check {src['url']}", 4, "warning", src["url"], dry)
        log(f"  {src['name']}: {len(rows)} rows")
        out.extend(rows)
    return out


def enrich_descriptions(posts: list[Posting], state: dict, cfg: dict) -> None:
    """For tracker rows where the title says nothing about season, open the
    posting and read it. Capped per run, cached forever by URL."""
    budget = int(cfg["routing"]["max_description_fetches"])
    cache = state["desc_cache"]
    for p in posts:
        if budget <= 0:
            break
        if p.source_kind == "ats" or not p.url:
            continue
        if p.tier > 2 and not p.focus_hit:
            continue
        if find_seasons(p.role):
            continue
        key = sha(p.url)
        if key in cache:
            p.description = cache[key].get("snippet", "")
            continue
        try:
            r = SESSION.get(p.url, timeout=12, allow_redirects=True)
            # an error page is not a job description — treating one as text is
            # how you end up "classifying" the season off a 403 body
            ct = r.headers.get("Content-Type", "")
            body = clean(r.text)[:6000] if (r.ok and ("html" in ct or "text" in ct)) else ""
        except requests.RequestException:
            body = ""
        budget -= 1
        cache[key] = {"snippet": body[:2000], "checked": iso(NOW)}
        p.description = body


def dedupe(posts: list[Posting]) -> list[Posting]:
    """One posting can show up on four trackers and its own ATS. Keep the most
    authoritative copy, remember where else it appeared, keep the earliest
    credible publish date."""
    rank = {"ats": 0, "tracker": 1}
    best: dict[str, Posting] = {}
    for p in sorted(posts, key=lambda x: (rank[x.source_kind], -len(x.role))):
        cur = best.get(p.key)
        if cur is None:
            best[p.key] = p
            continue
        if p.source not in cur.also_seen and p.source != cur.source:
            cur.also_seen.append(p.source)
        if p.posted_at and (cur.posted_at is None or p.posted_at < cur.posted_at):
            if cur.source_kind != "ats":
                cur.posted_at = p.posted_at
        if not cur.url and p.url:
            cur.url = p.url
        if not cur.location and p.location:
            cur.location = p.location
    return list(best.values())


# --------------------------------------------------------------------------
# main
# --------------------------------------------------------------------------

def run(args) -> int:
    cfg = yaml.safe_load(CONFIG_PATH.read_text())
    state = load_state()
    index = build_company_index(cfg)
    routing = cfg["routing"]

    posts = collect_ats(cfg, state) + collect_trackers(cfg, state, args.dry_run)
    posts = dedupe(posts)
    log(f"{len(posts)} unique postings after dedupe")

    for p in posts:
        score_posting(p, cfg, index)
    posts = [p for p in posts if p.score >= routing["digest_min_score"]]
    log(f"{len(posts)} clear the score floor")

    enrich_descriptions(posts, state, cfg)
    for p in posts:
        p.term, p.term_evidence = classify_term(p, cfg)
        if p.term == "target":
            p.score += 2
            p.reasons.append("target season confirmed (+2)")

    allow_off = bool(cfg["season"]["allow_offseason"])
    kept = [p for p in posts if allow_off or p.term != "offseason"]
    dropped = len(posts) - len(kept)
    log(f"{dropped} dropped as wrong season, {len(kept)} remain")

    first_run = not state["postings"]
    if first_run and not args.seed:
        log("state is empty — seeding instead of pushing 200 notifications")
        args.seed = True

    instant, digest, new_count = [], [], 0
    for p in sorted(kept, key=lambda x: (-x.score, x.company)):
        pid = sha(p.key)
        record = state["postings"].get(pid)
        if record:
            for s in [p.source] + p.also_seen:
                if s not in record.get("sources", []):
                    record.setdefault("sources", []).append(s)
            continue
        new_count += 1
        state["postings"][pid] = {
            "first_seen": iso(NOW), "posted_at": iso(p.posted_at), "company": p.company,
            "role": p.role, "term": p.term, "score": p.score, "tier": p.tier,
            "sources": [p.source] + p.also_seen, "url": p.url,
        }
        age_h = (NOW - p.posted_at).total_seconds() / 3600 if p.posted_at else 0
        fresh = age_h <= routing["instant_max_age_hours"]
        # a confirmed target-season role at a tier 1/2 company is worth waking
        # the phone for even if a tracker sat on it for a week
        hot = (p.score >= routing["instant_min_score"]
               and p.term in ("target", "assumed")
               and (fresh or (p.term == "target" and p.tier <= 2)))
        (instant if hot else digest).append(p)

    log(f"{new_count} new · {len(instant)} instant · {len(digest)} to digest")

    if args.explain:
        log("\n" + "=" * 96)
        for p in sorted(kept, key=lambda x: -x.score):
            route = "INSTANT" if p in instant else "digest " if p in digest else "seen   "
            log(f"{route} {p.score:>3}  {p.term:<9} {p.company[:20]:<20} {p.role[:44]:<44} {p.location[:18]}")
            log(f"            {'; '.join(p.reasons)}  | season: {p.term_evidence}")
        log("=" * 96 + "\n")

    if args.seed:
        log("seed mode — nothing sent")
        digest = []                      # don't dump the whole baseline into the first digest
        state["last_digest"] = iso(NOW)
        push("🌱 Watcher seeded",
             f"Baseline recorded: {len(state['postings'])} postings.\n"
             f"Alerts start on the next run.", 3, "seedling", None, args.dry_run)
    else:
        for p in instant[: routing["max_instant_pushes"]]:
            instant_push(p, args.dry_run)
        overflow = instant[routing["max_instant_pushes"]:]
        digest.extend(overflow)
        if overflow:
            log(f"{len(overflow)} instant pushes over the cap, moved to digest")

    for p in digest:
        state["digest_queue"].append({
            "company": p.company, "role": p.role, "location": p.location,
            "term": p.term, "score": p.score, "url": p.url or "",
        })

    hour_due = NOW.hour == int(routing.get("digest_hour_utc", 23))
    if not args.seed:
        send_digest(state, cfg, args.dry_run, force=args.digest or hour_due)

    if not args.dry_run:
        if kept:
            archive([p for p in kept if sha(p.key) in state["postings"]])
        save_state(state)
    return 0


def verify_boards(args) -> int:
    """Exit non-zero if any configured board is dead, so the weekly workflow
    run fails and GitHub emails you instead of the bot going quiet."""
    cfg = yaml.safe_load(CONFIG_PATH.read_text())
    dead = 0
    rows = []
    for tier in ("tier1", "tier2"):
        for e in cfg["companies"][tier] or []:
            if isinstance(e, dict) and e.get("ats") and e.get("board"):
                rows.append((tier, e))
    log(f"checking {len(rows)} boards\n")
    for tier, e in rows:
        fn = ATS_CLIENTS[e["ats"]]
        try:
            jobs = fn(e["name"], e["board"])
            interns = [j for j in jobs if INTERNISH.search(j.role)]
            mark = "ok  " if jobs else "EMPTY"
            log(f"  {mark} {e['name']:<22} {e['ats']}:{e['board']:<22} "
                f"{len(jobs):>4} jobs, {len(interns):>3} intern-ish")
        except Exception as ex:
            code = getattr(getattr(ex, "response", None), "status_code", "")
            log(f"  DEAD {e['name']:<22} {e['ats']}:{e['board']:<22} {type(ex).__name__} {code}"
                f"   <- fix the token or set ats: null")
            dead += 1
    if dead:
        log(f"\n{dead} board(s) unreachable — fix the tokens in targets.yml")
    return 1 if dead else 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="print, don't push or save")
    ap.add_argument("--seed", action="store_true", help="record a baseline silently")
    ap.add_argument("--digest", action="store_true", help="force the digest now")
    ap.add_argument("--explain", action="store_true", help="show every match with its score breakdown")
    ap.add_argument("--verify-boards", action="store_true", help="check every ATS token")
    args = ap.parse_args()
    if args.verify_boards:
        return verify_boards(args)
    return run(args)


if __name__ == "__main__":
    sys.exit(main())
