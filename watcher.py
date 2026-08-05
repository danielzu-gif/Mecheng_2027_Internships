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

Everything is scored, deduped across layers, then assigned a RANK (S/A/B/C).
Rank, not raw score, decides what happens next:

  S  wakes the phone, one notification per posting, priority 5, never batched,
     never capped away. This is the "you cannot miss this" lane.
  A  wakes the phone at normal priority, capped per run, overflow rolled up.
  B  never fires alone — collapses into a single roll-up notification.
  C  board and brief only, silent.

Two daily briefs summarise everything by rank, and a living application board
(a GitHub issue with tappable checkboxes, plus state/BOARD.md as a fallback)
holds every open posting sorted by rank until you tick it off.

  python watcher.py                  normal run
  python watcher.py --dry-run        print what it would send, send nothing
  python watcher.py --seed           record current state, send nothing
  python watcher.py --verify-boards  check every ATS token in targets.yml
  python watcher.py --brief          force the daily brief to send now
  python watcher.py --board-only     rebuild the board, send nothing
"""

from __future__ import annotations

import argparse
import base64
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
BOARD_PATH = ROOT / "state" / "BOARD.md"

NTFY_TOPIC = os.environ.get("NTFY_TOPIC", "").strip()
# Optional second topic carrying ONLY rank S. Subscribe to it separately in the
# ntfy app and give it its own sound, so a tier 1 drop never sounds like the
# other forty notifications on your phone.
NTFY_TOPIC_TOP = os.environ.get("NTFY_TOPIC_TOP", "").strip()
NTFY_HOST = os.environ.get("NTFY_HOST", "https://ntfy.sh").rstrip("/")
UA = "internship-watcher/3.0 (personal job alert bot)"

GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN", "").strip()
GITHUB_REPO = os.environ.get("GITHUB_REPOSITORY", "").strip()
GITHUB_API = "https://api.github.com"

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

    rank: str = "C"                   # S | A | B | C
    rank_reason: str = ""

    @property
    def key(self) -> str:
        role_core = re.sub(
            r"\b(summer|fall|autumn|spring|winter)\b|\b20\d{2}\b|\b'?\d{2}\b",
            "", self.role.lower())
        return f"{slug(self.company)}|{slug(role_core)}"

    @property
    def pid(self) -> str:
        return sha(self.key)

    @property
    def tier_label(self) -> str:
        return {1: "Tier 1", 2: "Tier 2", 3: "Tier 3"}.get(self.tier, "unlisted")

    @property
    def icon(self) -> str:
        return RANK_META[self.rank]["icon"]


# --------------------------------------------------------------------------
# ranking — one letter that decides how loud a posting is allowed to be
# --------------------------------------------------------------------------
#
# Score alone was the problem: a score 7 posting at a company you'd never work
# for looked identical on the lock screen to a score 7 posting at SpaceX, and
# in a flood the second one scrolled away. Rank folds tier, role fit and season
# confidence into a single sort key that survives truncation to two characters.

RANK_META = {
    "S": {"icon": "⭐", "label": "APPLY NOW",    "tag": "star",
          "priority": 5, "board": "⭐ Apply now"},
    "A": {"icon": "🔷", "label": "Strong",       "tag": "large_blue_diamond",
          "priority": 4, "board": "🔷 Strong"},
    "B": {"icon": "▫️", "label": "Worth a look", "tag": "white_small_square",
          "priority": 3, "board": "▫️ Worth a look"},
    "C": {"icon": "·",  "label": "Wide net",     "tag": "grey_question",
          "priority": 2, "board": "· Wide net"},
}
RANK_ORDER = ["S", "A", "B", "C"]

DEFAULT_RANKS = {
    "S": {"max_tier": 1, "min_score": 8, "terms": ["target", "assumed"]},
    "A": {"max_tier": 2, "min_score": 6, "terms": ["target", "assumed"]},
    "B": {"max_tier": 3, "min_score": 5},
}


def rank_for(tier: int, score: int, term: str, focus: bool, cfg: dict) -> str:
    """First matching rule wins, S first. Everything unmatched is C."""
    rules = cfg.get("ranks") or DEFAULT_RANKS
    for letter in ("S", "A", "B"):
        rule = rules.get(letter)
        if not rule:
            continue
        if int(tier) > int(rule.get("max_tier", 9)):
            continue
        if int(score) < int(rule.get("min_score", 999)):
            continue
        if rule.get("terms") and term not in rule["terms"]:
            continue
        if rule.get("require_focus") and not focus:
            continue
        return letter
    return "C"


def assign_rank(post: Posting, cfg: dict) -> None:
    post.rank = rank_for(post.tier, post.score, post.term, post.focus_hit, cfg)
    post.rank_reason = (f"{post.tier_label.lower()}, score {post.score}, "
                        f"season {post.term}")


def backfill_ranks(state: dict, cfg: dict, index: dict) -> None:
    """Rank every record written before ranks existed, so the first board is
    correct instead of dumping a season of tier 1 postings into the wide-net
    drawer. Their stored scores came from older keyword lists, so re-score from
    the title under today's rules rather than trusting the number on disk."""
    n, dropped = 0, 0
    for rec in state["postings"].values():
        if rec.get("rank"):
            continue
        probe = Posting(company=rec.get("company", ""), role=rec.get("role", ""),
                        location=rec.get("location", ""))
        score_posting(probe, cfg, index)
        if probe.score < int(cfg["routing"]["digest_min_score"]):
            rec["rank"], rec["filtered"] = "C", True   # no longer matches, hide it
            dropped += 1
        else:
            rec["score"], rec["tier"] = probe.score, probe.tier
            rec["rank"] = rank_for(probe.tier, probe.score,
                                   rec.get("term", "unknown"), probe.focus_hit, cfg)
        n += 1
    if n:
        log(f"back-filled ranks on {n} stored postings ({dropped} no longer match)")


def rank_key(rank: str) -> int:
    return RANK_ORDER.index(rank) if rank in RANK_ORDER else len(RANK_ORDER)


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

    # Trackers only list internships; ATS boards list every open req at the
    # company, so this has to be a hard drop rather than a penalty.
    truncated = bool(TRUNCATED.search(post.role))
    if not INTERNISH.search(title) and not truncated:
        post.score = -99
        post.reasons = ["not an internship req"]
        return

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
        return {"version": 3, "postings": {}, "desc_cache": {}, "source_health": {},
                "applied": {}, "digest_queue": [], "last_digest": None,
                "board_issue": None}
    state = json.loads(STATE_PATH.read_text())
    for key in ("postings", "desc_cache", "source_health", "applied"):
        state.setdefault(key, {})
    state.setdefault("digest_queue", [])
    state.setdefault("last_digest", None)
    state.setdefault("board_issue", None)
    state["version"] = 3
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

def encode_header(value: str) -> str:
    """HTTP headers are latin-1. Emoji in a Title header raises
    UnicodeEncodeError, so fall back to the RFC 2047 encoded-word form that
    ntfy decodes on its side."""
    try:
        value.encode("latin-1")
        return value
    except UnicodeEncodeError:
        return "=?UTF-8?B?" + base64.b64encode(value.encode("utf-8")).decode("ascii") + "?="


def push(title: str, message: str, priority: int = 3, tags: str = "rocket",
         click: str | None = None, dry: bool = False,
         actions: list[tuple[str, str]] | None = None,
         top_lane: bool = False) -> None:
    """top_lane also mirrors to NTFY_TOPIC_TOP if you've set one up, so rank S
    can have its own sound and its own notification channel on the phone."""
    topics = [NTFY_TOPIC] if NTFY_TOPIC else []
    if top_lane and NTFY_TOPIC_TOP and NTFY_TOPIC_TOP != NTFY_TOPIC:
        topics.append(NTFY_TOPIC_TOP)
    if dry or not topics:
        log(f"\n[{'DRY' if dry else 'NO TOPIC'}] p{priority} {title}\n{message}\n")
        return
    headers = {"Title": encode_header(title), "Priority": str(priority),
               "Tags": encode_header(tags)}
    if click:
        headers["Click"] = click
    if actions:
        headers["Actions"] = encode_header(
            "; ".join(f"view, {label}, {url}, clear=true" for label, url in actions[:3]))
    for topic in topics:
        try:
            SESSION.post(f"{NTFY_HOST}/{topic}", data=message.encode("utf-8"),
                         headers=headers, timeout=15)
        except requests.RequestException as e:
            log(f"push failed ({topic}): {e}")


def board_url(state: dict) -> str | None:
    num = state.get("board_issue")
    if num and GITHUB_REPO:
        return f"https://github.com/{GITHUB_REPO}/issues/{num}"
    if GITHUB_REPO:
        return f"https://github.com/{GITHUB_REPO}/blob/main/state/BOARD.md"
    return None


def instant_push(p: Posting, state: dict, dry: bool) -> None:
    """One posting, one notification. The rank icon leads the title so the
    lock screen is scannable even when the text is truncated to nothing."""
    meta = RANK_META[p.rank]
    focus = "🚀 " if p.focus_hit else ""
    title = f"{meta['icon']} {focus}{p.company} · {p.role}"[:120]
    season = ("Summer 2027 confirmed" if p.term == "target"
              else "season unconfirmed" if p.term == "assumed" else p.term)
    body = [
        f"{meta['label']} · {p.tier_label} · score {p.score}",
        f"{p.location or 'location n/a'} · {human_age(p.posted_at)}",
        f"{season} · {'direct from ' + p.source.split(':')[0] if p.source_kind == 'ats' else p.source}",
    ]
    if p.also_seen:
        body.append("also listed on: " + ", ".join(p.also_seen))
    if p.url:
        body.append(p.url)
    actions = []
    if p.url:
        actions.append(("Apply", p.url))
    burl = board_url(state)
    if burl:
        actions.append(("Board", burl))
    push(title, "\n".join(body), meta["priority"], meta["tag"], p.url, dry,
         actions=actions, top_lane=(p.rank == "S"))


def company_push(company: str, group: list[Posting], state: dict, dry: bool) -> None:
    """One company dropping five reqs at once is one event, not five
    notifications. Collapsing it keeps the flood from pushing everything else
    off the screen, and the company name still leads the title."""
    top = max(group, key=lambda x: (-rank_key(x.rank), x.score))
    meta = RANK_META[top.rank]
    title = f"{meta['icon']} {company} · {len(group)} new roles"[:120]
    lines = [f"{meta['label']} · {top.tier_label}", ""]
    for p in sorted(group, key=lambda x: (rank_key(x.rank), -x.score))[:10]:
        lines.append(f"{p.rank} · {p.role[:60]}")
        lines.append(f"    {p.location[:34] or 'location n/a'} · {human_age(p.posted_at)}")
    burl = board_url(state)
    if burl:
        lines += ["", burl]
    actions = []
    if top.url:
        actions.append(("Open one", top.url))
    if burl:
        actions.append(("Board", burl))
    push(title, "\n".join(lines)[:3800], meta["priority"], meta["tag"],
         top.url, dry, actions=actions, top_lane=(top.rank == "S"))


def rollup_push(posts: list[Posting], state: dict, dry: bool) -> None:
    """Everything that isn't worth its own notification arrives as exactly one,
    at a priority that doesn't interrupt anything."""
    if not posts:
        return
    counts = {}
    for p in posts:
        counts[p.rank] = counts.get(p.rank, 0) + 1
    summary = " · ".join(f"{n} {r}" for r, n in sorted(counts.items(), key=lambda x: rank_key(x[0])))
    lines = []
    for p in sorted(posts, key=lambda x: (rank_key(x.rank), -x.score))[:25]:
        lines.append(f"{p.rank} · {p.company[:22]} · {p.role[:44]}")
    if len(posts) > 25:
        lines.append(f"…and {len(posts) - 25} more on the board")
    burl = board_url(state)
    if burl:
        lines += ["", burl]
    push(f"▫️ {len(posts)} more ({summary})", "\n".join(lines)[:3800],
         2, "white_small_square", burl, dry,
         actions=[("Board", burl)] if burl else None)


def nudge_push(items: list[dict], state: dict, dry: bool) -> None:
    """A top-rank posting you never ticked off, still live days later. This is
    the one that actually costs you a summer if it slips."""
    if not items:
        return
    lines = []
    for it in items[:10]:
        age = human_age(parse_iso(it.get("first_seen"))).replace("posted ", "first seen ")
        lines.append(f"{it['company']} · {it['role'][:52]}")
        lines.append(f"    {age}, still open, not ticked off")
    burl = board_url(state)
    if burl:
        lines += ["", burl]
    push(f"⏰ {len(items)} top posting(s) still unapplied",
         "\n".join(lines)[:3800], 4, "alarm_clock", burl, dry,
         actions=[("Board", burl)] if burl else None, top_lane=True)


def send_brief(state: dict, cfg: dict, dry: bool, force: bool = False) -> None:
    """The daily read: every rank band in order, best companies first, counts
    where the detail stops earning its space."""
    routing = cfg["routing"]
    queue = state.get("digest_queue", [])
    last = parse_iso(state.get("last_digest"))
    min_gap = int(routing.get("brief_min_gap_hours", 8))
    due = force or not last or (NOW - last) > timedelta(hours=min_gap)
    if not due:
        return
    if not queue and not routing.get("heartbeat", True):
        return

    bands = {r: [] for r in RANK_ORDER}
    for item in queue:
        bands.get(item.get("rank", "C"), bands["C"]).append(item)

    caps = routing.get("brief_detail_caps") or {"S": 12, "A": 10, "B": 6, "C": 0}
    lines = []
    for r in RANK_ORDER:
        items = sorted(bands[r], key=lambda x: (-x["score"], x["company"]))
        if not items:
            continue
        meta = RANK_META[r]
        cap = int(caps.get(r, 0))
        if cap == 0:
            firms = len({i["company"] for i in items})
            lines.append(f"{meta['icon']} {meta['label'].upper()} — {len(items)} "
                         f"from {firms} companies, board only")
            lines.append("")
            continue
        lines.append(f"{meta['icon']} {meta['label'].upper()} ({len(items)})")
        for it in items[:cap]:
            lines.append(f"{it['company'][:24]} · {it['role'][:46]}")
            detail = f"    {it['location'][:30] or 'location n/a'}"
            if it.get("term") == "target":
                detail += " · S27 confirmed"
            lines.append(detail)
        if len(items) > cap:
            lines.append(f"    +{len(items) - cap} more on the board")
        lines.append("")

    burl = board_url(state)
    if burl:
        lines.append(burl)

    total = len(queue)
    head = " · ".join(f"{len(bands[r])}⭐" if r == "S" else f"{len(bands[r])}{r}"
                      for r in RANK_ORDER if bands[r]) or "nothing new"
    label = "Morning brief" if NOW.hour < 18 else "Evening brief"
    if total:
        title = f"📋 {label} · {head}"
        body = "\n".join(lines)[:3800]
        priority = 3 if bands["S"] or bands["A"] else 2
    else:
        # A silent daily heartbeat, so silence always means "nothing matched"
        # and never "the workflow has been broken for nine days".
        ok = sum(1 for h in state.get("source_health", {}).values() if h.get("last_ok"))
        title = f"📋 {label} · all quiet"
        body = (f"No new matches since the last brief.\n"
                f"{ok} tracker sources answered, watcher is running.")
        priority = 1

    push(title, body, priority, "clipboard", burl, dry,
         actions=[("Board", burl)] if burl else None)

    DIGEST_PATH.parent.mkdir(parents=True, exist_ok=True)
    DIGEST_PATH.write_text(
        f"# Brief {NOW:%Y-%m-%d %H:%M} UTC\n\n" +
        (f"{total} new since the last brief.\n\n" if total else "Nothing new.\n") +
        "\n".join(
            f"- `{i.get('rank','C')}` **{i['company']}** — {i['role']} · "
            f"{i['location']} · score {i['score']}"
            + (f" · [apply]({i['url']})" if i.get("url") else "")
            for i in sorted(queue, key=lambda x: (rank_key(x.get("rank", "C")), -x["score"]))))
    if not dry:
        state["digest_queue"] = []
        state["last_digest"] = iso(NOW)


# --------------------------------------------------------------------------
# the board — one living, ranked list you tick off as you apply
# --------------------------------------------------------------------------
#
# A notification is a moment; the board is the state. Every open posting sits
# here sorted by rank until you check its box, and a checked box is permanent:
# the posting stops appearing in briefs, roll-ups and nudges forever.

CHECKED = re.compile(r"^\s*[-*]\s*\[([ xX])\].*?<!--([0-9a-f]{6,32})-->", re.M)


def gh(method: str, path: str, payload: dict | None = None):
    if not (GITHUB_TOKEN and GITHUB_REPO):
        return None
    url = f"{GITHUB_API}/repos/{GITHUB_REPO}{path}"
    try:
        r = SESSION.request(method, url, json=payload, timeout=20, headers={
            "Authorization": f"Bearer {GITHUB_TOKEN}",
            "Accept": "application/vnd.github+json",
        })
        if r.status_code >= 400:
            log(f"  github {method} {path} -> {r.status_code}")
            return None
        return r.json()
    except requests.RequestException as e:
        log(f"  github {method} {path} failed: {type(e).__name__}")
        return None


def harvest_checked(state: dict) -> int:
    """Read ticks from wherever you made them — the issue or the file — before
    the board is rebuilt, so applying to something removes it the same day."""
    bodies = []
    num = state.get("board_issue")
    if num:
        issue = gh("GET", f"/issues/{num}")
        if issue and issue.get("body"):
            bodies.append(issue["body"])
    if BOARD_PATH.exists():
        bodies.append(BOARD_PATH.read_text())
    found = 0
    for body in bodies:
        for mark, pid in CHECKED.findall(body):
            if mark.lower() == "x" and pid not in state["applied"]:
                state["applied"][pid] = iso(NOW)
                found += 1
    if found:
        log(f"{found} newly ticked off as applied")
    return found


def board_line(pid: str, rec: dict, checked: bool = False,
               company_prefix: bool = True) -> str:
    box = "x" if checked else " "
    bits = [f"**{rec.get('company', '?')}** — {rec.get('role', '?')}"
            if company_prefix else rec.get("role", "?")]
    if rec.get("location"):
        bits.append(rec["location"])
    if rec.get("term") == "target":
        bits.append("S27 confirmed")
    posted = parse_iso(rec.get("posted_at")) or parse_iso(rec.get("first_seen"))
    if posted:
        bits.append(human_age(posted))
    if rec.get("url"):
        bits.append(f"[apply]({rec['url']})")
    if checked and state_applied_date(rec):
        bits.append(f"applied {state_applied_date(rec)}")
    return f"- [{box}] " + " · ".join(bits) + f" <!--{pid}-->"


def state_applied_date(rec: dict) -> str:
    d = parse_iso(rec.get("applied_at"))
    return f"{d:%b %d}" if d else ""


def band_rows(items: list[tuple[str, dict]]) -> list[str]:
    """Rocket Lab opening seventeen reqs in one morning is the same burial
    problem one level down, so a band is grouped by company and the companies
    are ordered by their strongest single role, not by how many they posted."""
    groups: dict[str, list[tuple[str, dict]]] = {}
    for pid, rec in items:
        groups.setdefault(rec.get("company", "?"), []).append((pid, rec))
    ordered = sorted(groups.items(),
                     key=lambda kv: (-max(int(r.get("score", 0)) for _, r in kv[1]), kv[0]))
    rows = []
    for company, group in ordered:
        group.sort(key=lambda kv: -int(kv[1].get("score", 0)))
        if len(group) == 1:
            rows.append(board_line(group[0][0], group[0][1], company_prefix=True))
            continue
        rows.append(f"**{company}** ({len(group)})")
        rows += [board_line(pid, rec, company_prefix=False) for pid, rec in group]
        rows.append("")
    return rows


def compose_board(state: dict, cfg: dict) -> str:
    active_days = int(cfg["routing"].get("board_active_days", 21))
    applied = state.get("applied", {})
    bands = {r: [] for r in RANK_ORDER}
    done = []
    for pid, rec in state["postings"].items():
        if pid in applied:
            rec = dict(rec, applied_at=applied[pid])
            done.append((pid, rec))
            continue
        if rec.get("filtered"):
            continue
        seen = parse_iso(rec.get("last_seen")) or parse_iso(rec.get("first_seen"))
        if seen and (NOW - seen) > timedelta(days=active_days):
            continue
        bands.get(rec.get("rank", "C"), bands["C"]).append((pid, rec))

    for band in bands.values():
        band.sort(key=lambda kv: (-int(kv[1].get("score", 0)), kv[1].get("company", "")))
    done.sort(key=lambda kv: kv[1].get("applied_at", ""), reverse=True)

    out = [f"# Summer 2027 board",
           f"_rebuilt {NOW:%Y-%m-%d %H:%M} UTC · tick a box the moment you apply "
           f"and it disappears from every alert_", ""]
    live = sum(len(b) for b in bands.values())
    out.append(f"**{live} open · {len(bands['S'])} apply-now · {len(done)} applied**")
    out.append("")

    for r in RANK_ORDER:
        items = bands[r]
        meta = RANK_META[r]
        if not items:
            continue
        rows = band_rows(items)
        # S and A stay open on the page: those are the ones you actually tick.
        # B and C fold away so the top of the board is never buried.
        if r in ("S", "A"):
            out += [f"## {meta['board']} ({len(items)})", ""] + rows + [""]
        else:
            out += [f"<details><summary><b>{meta['board']}</b> ({len(items)})</summary>", ""]
            out += rows + ["", "</details>", ""]

    if done:
        out += [f"<details><summary>✅ Applied ({len(done)})</summary>", ""]
        out += [board_line(pid, rec, checked=True) for pid, rec in done[:120]]
        out += ["", "</details>", ""]
    return "\n".join(out)


def publish_board(state: dict, cfg: dict, dry: bool) -> None:
    body = compose_board(state, cfg)
    BOARD_PATH.parent.mkdir(parents=True, exist_ok=True)
    if not dry:
        BOARD_PATH.write_text(body)
    if dry or not (GITHUB_TOKEN and GITHUB_REPO):
        log(f"board rebuilt locally ({len(body)} chars)")
        return
    title = cfg["routing"].get("board_title", "📋 Summer 2027 internship board")
    num = state.get("board_issue")
    # GitHub caps an issue body at 65536 characters
    body = body[:65000]
    if num:
        if gh("PATCH", f"/issues/{num}", {"body": body, "title": title}):
            log(f"board issue #{num} updated")
            return
        log("board issue update failed, recreating")
    issue = gh("POST", "/issues", {"title": title, "body": body})
    if issue:
        state["board_issue"] = issue["number"]
        log(f"board issue #{issue['number']} created")


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
    return merge_truncated(list(best.values()))


def merge_truncated(posts: list[Posting]) -> list[Posting]:
    """One tracker publishes "Mechanical Engineering & System Packa..." and
    another publishes the full title, so the dedupe key misses and the same job
    shows up twice. Only a title the source actually truncated gets folded in,
    which keeps "Ground Systems Intern" and "Ground Systems Intern - Electron"
    as the two separate reqs they are."""
    by_company: dict[str, list[Posting]] = {}
    for p in posts:
        by_company.setdefault(slug(p.company), []).append(p)
    out = []
    for group in by_company.values():
        group.sort(key=lambda x: (bool(TRUNCATED.search(x.role)), -len(x.role)))
        keep: list[Posting] = []
        for p in group:
            stem = slug(TRUNCATED.sub("", p.role))
            parent = None
            if TRUNCATED.search(p.role) and len(stem) >= 10:
                parent = next((k for k in keep if slug(k.role).startswith(stem[:-2])), None)
            if parent is None:
                keep.append(p)
                continue
            for s in [p.source] + p.also_seen:
                if s != parent.source and s not in parent.also_seen:
                    parent.also_seen.append(s)
            if not parent.url and p.url:
                parent.url = p.url
            if not parent.location and p.location:
                parent.location = p.location
            if p.posted_at and (parent.posted_at is None or p.posted_at < parent.posted_at):
                parent.posted_at = p.posted_at
        out.extend(keep)
    return out


# --------------------------------------------------------------------------
# main
# --------------------------------------------------------------------------

def run(args) -> int:
    cfg = yaml.safe_load(CONFIG_PATH.read_text())
    state = load_state()
    index = build_company_index(cfg)
    routing = cfg["routing"]

    # Ticks first: anything you applied to since the last run drops out of the
    # alerts below instead of being re-pushed at you.
    harvest_checked(state)
    backfill_ranks(state, cfg, index)

    if args.board_only:
        publish_board(state, cfg, args.dry_run)
        if not args.dry_run:
            save_state(state)
        return 0

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

    for p in kept:
        assign_rank(p, cfg)
    log("ranks: " + ", ".join(f"{r}={sum(1 for p in kept if p.rank == r)}" for r in RANK_ORDER))

    first_run = not state["postings"]
    if first_run and not args.seed:
        log("state is empty — seeding instead of pushing 200 notifications")
        args.seed = True

    applied = state["applied"]
    fresh_hits, queued, new_count = [], [], 0
    for p in sorted(kept, key=lambda x: (rank_key(x.rank), -x.score, x.company)):
        pid = p.pid
        record = state["postings"].get(pid)
        if record:
            # still live — this is what lets the board expire dead postings and
            # what makes the unapplied nudge honest
            record["last_seen"] = iso(NOW)
            if p.rank != record.get("rank") and rank_key(p.rank) < rank_key(record.get("rank", "C")):
                record["rank"] = p.rank      # promoted, e.g. season got confirmed
            for s in [p.source] + p.also_seen:
                if s not in record.get("sources", []):
                    record.setdefault("sources", []).append(s)
            continue
        if pid in applied:
            continue                          # already ticked off, never alert again
        new_count += 1
        state["postings"][pid] = {
            "first_seen": iso(NOW), "last_seen": iso(NOW), "posted_at": iso(p.posted_at),
            "company": p.company, "role": p.role, "location": p.location,
            "term": p.term, "score": p.score, "tier": p.tier, "rank": p.rank,
            "sources": [p.source] + p.also_seen, "url": p.url,
        }
        fresh_hits.append(p)

    # ------------------------------------------------------------------
    # routing: rank decides the lane, and the lanes can't crowd each other
    # ------------------------------------------------------------------
    quiet = in_quiet_hours(cfg)
    max_age = float(routing.get("instant_max_age_hours", 72))
    loud, rollup = [], []
    for p in fresh_hits:
        age_h = (NOW - p.posted_at).total_seconds() / 3600 if p.posted_at else 0
        if p.rank == "S":
            loud.append(p)                    # always, regardless of age or hour
        elif p.rank == "A" and not quiet and age_h <= max_age:
            loud.append(p)
        elif p.rank in ("A", "B"):
            rollup.append(p)
        else:
            queued.append(p)

    # a single company dropping several reqs is one event
    by_company: dict[str, list[Posting]] = {}
    for p in loud:
        by_company.setdefault(p.company, []).append(p)
    group_min = int(routing.get("group_company_at", 3))
    singles, groups = [], []
    for company, group in by_company.items():
        (groups if len(group) >= group_min else singles).append((company, group))
    singles = [p for _, g in singles for p in g]

    # caps are per rank, so a flood of A can never push an S off the phone
    cap_s = int(routing.get("max_instant_top", 8))
    cap_a = int(routing.get("max_instant_strong", 5))
    s_items = [p for p in singles if p.rank == "S"]
    a_items = [p for p in singles if p.rank != "S"]
    over = s_items[cap_s:] + a_items[cap_a:]
    singles = s_items[:cap_s] + a_items[:cap_a]
    rollup.extend(over)

    log(f"{new_count} new · {len(singles)} individual · {len(groups)} grouped · "
        f"{len(rollup)} rolled up · {len(queued)} brief only"
        + (" · quiet hours" if quiet else ""))

    if args.explain:
        log("\n" + "=" * 100)
        for p in sorted(kept, key=lambda x: (rank_key(x.rank), -x.score)):
            route = ("PUSH " if p in singles else "GROUP" if any(p in g for _, g in groups)
                     else "ROLL " if p in rollup else "brief" if p in queued else "seen ")
            log(f"{p.rank} {route} {p.score:>3} {p.term:<9} {p.company[:20]:<20} "
                f"{p.role[:42]:<42} {p.location[:18]}")
            log(f"        {'; '.join(p.reasons)} | season: {p.term_evidence}")
        log("=" * 100 + "\n")

    if args.seed:
        log("seed mode — nothing sent")
        singles, groups, rollup, queued = [], [], [], []
        state["last_digest"] = iso(NOW)
        push("🌱 Watcher seeded",
             f"Baseline recorded: {len(state['postings'])} postings.\n"
             f"Alerts start on the next run.", 3, "seedling", None, args.dry_run)
    elif not args.board_only:
        for p in singles:
            instant_push(p, state, args.dry_run)
        for company, group in groups:
            company_push(company, group, state, args.dry_run)
        rollup_push(rollup, state, args.dry_run)

    for p in rollup + queued:
        state["digest_queue"].append({
            "pid": p.pid, "rank": p.rank, "company": p.company, "role": p.role,
            "location": p.location, "term": p.term, "score": p.score, "url": p.url or "",
        })
    for p in singles + [x for _, g in groups for x in g]:
        state["digest_queue"].append({
            "pid": p.pid, "rank": p.rank, "company": p.company, "role": p.role,
            "location": p.location, "term": p.term, "score": p.score, "url": p.url or "",
        })

    if not args.seed and not args.board_only:
        nudge_push(collect_nudges(state, cfg), state, args.dry_run)

    brief_hours = routing.get("brief_hours_utc") or [int(routing.get("digest_hour_utc", 23))]
    hour_due = NOW.hour in [int(h) for h in brief_hours]
    if not args.seed and not args.board_only:
        send_brief(state, cfg, args.dry_run, force=args.brief or hour_due)

    publish_board(state, cfg, args.dry_run)

    if not args.dry_run:
        if kept:
            archive([p for p in kept if p.pid in state["postings"]])
        prune(state, cfg)
        save_state(state)
    return 0


def in_quiet_hours(cfg: dict) -> bool:
    window = cfg["routing"].get("quiet_hours_utc")
    if not window:
        return False
    start, end = int(window[0]), int(window[1])
    return start <= NOW.hour < end if start <= end else (NOW.hour >= start or NOW.hour < end)


def collect_nudges(state: dict, cfg: dict) -> list[dict]:
    """Rank S, still showing up in the feed, days old, box never ticked. One
    reminder each, then it stays quiet."""
    days = int(cfg["routing"].get("nudge_after_days", 3))
    if days <= 0:
        return []
    out = []
    for pid, rec in state["postings"].items():
        if rec.get("rank") != "S" or pid in state["applied"] or rec.get("nudged"):
            continue
        first = parse_iso(rec.get("first_seen"))
        last = parse_iso(rec.get("last_seen"))
        if not first or (NOW - first) < timedelta(days=days):
            continue
        if not last or (NOW - last) > timedelta(hours=36):
            continue                          # gone from the feed, probably closed
        rec["nudged"] = iso(NOW)
        out.append(dict(rec, pid=pid))
    return sorted(out, key=lambda r: -int(r.get("score", 0)))[:10]


def prune(state: dict, cfg: dict) -> None:
    """seen.json is already 80KB. Drop description cache entries and dead
    postings so the file a GitHub Action rewrites every 20 minutes stays sane."""
    keep_days = int(cfg["routing"].get("state_keep_days", 400))
    cutoff = NOW - timedelta(days=keep_days)
    for pid in [k for k, v in state["desc_cache"].items()
                if (parse_iso(v.get("checked")) or NOW) < NOW - timedelta(days=45)]:
        del state["desc_cache"][pid]
    for pid in [k for k, v in state["postings"].items()
                if (parse_iso(v.get("first_seen")) or NOW) < cutoff and k not in state["applied"]]:
        del state["postings"][pid]


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
    ap.add_argument("--brief", "--digest", dest="brief", action="store_true",
                    help="force the daily brief now")
    ap.add_argument("--board-only", action="store_true", help="rebuild the board, send nothing")
    ap.add_argument("--explain", action="store_true", help="show every match with rank and score")
    ap.add_argument("--verify-boards", action="store_true", help="check every ATS token")
    args = ap.parse_args()
    if args.verify_boards:
        return verify_boards(args)
    return run(args)


if __name__ == "__main__":
    sys.exit(main())
