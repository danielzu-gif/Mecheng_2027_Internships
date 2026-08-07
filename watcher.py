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
APPLIED_PATH = ROOT / "state" / "APPLIED.md"
WIDE_PATH = ROOT / "state" / "WIDE_NET.md"

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


LEGAL_SUFFIX = re.compile(
    r"\b(corporation|corp|incorporated|inc|llc|ltd|limited|plc|company|"
    r"holdings|group|laboratories|labs)\b\.?", re.I)


def tidy_role(role: str, company: str = "") -> str:
    """Trackers paste the whole page title into the role cell. "Process
    Engineering Intern Job Details / Lam Research Corporation" and "Process
    Engineering Intern" are one job and should look like one job. Only a
    trailing fragment that IS the company gets cut, so "Mechanical/Industrial
    Internship" keeps both disciplines."""
    role = re.sub(r"\s*\bjob details\b.*$", "", role or "", flags=re.I)
    if company:
        tail = role.rsplit("/", 1)
        if len(tail) == 2 and slug(tail[1]) and slug(tail[1]) in slug(company):
            role = tail[0]
    return re.sub(r"\s+", " ", role).strip(" -|·/")


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


def revalidate(state: dict, cfg: dict, index: dict) -> None:
    """Re-score every stored posting against the CURRENT rules, every run.
    Without this, tightening a filter only affects postings that drop
    afterwards, and the ones already on the board sit there forever. This is
    what retires the Auckland reqs and the boilerplate-inflated scores."""
    changed, dropped, restored, tidied = 0, 0, 0, 0
    for rec in state["postings"].values():
        if rec.get("dup"):
            continue          # a resolved duplicate stays resolved
        # role text written before the title cleanup still reads like a page
        # title; the stored id never depended on it, so it is safe to fix
        neat = tidy_role(rec.get("role", ""), rec.get("company", ""))
        if neat and neat != rec.get("role"):
            rec["role"] = neat
            tidied += 1
        probe = Posting(company=rec.get("company", ""), role=rec.get("role", ""),
                        location=rec.get("location", ""))
        # a posting whose description already proved it takes ME majors keeps
        # that verdict; we no longer have the description text on hand
        if rec.get("me_ok"):
            probe.description = "mechanical engineering"
        score_posting(probe, cfg, index)
        was_filtered = bool(rec.get("filtered"))
        if probe.score < int(cfg["routing"]["digest_min_score"]):
            if not was_filtered:
                dropped += 1
            # Pin the rejecting score onto the record. Without this the stored
            # score stays at its old value, every run "changes" it back, and
            # seen.json is rewritten and committed forever over nothing.
            rec["filtered"], rec["rank"], rec["score"] = True, "C", probe.score
            continue
        if was_filtered:
            rec.pop("filtered", None)
            restored += 1
        # A record scored while live had the full job description in hand; this
        # pass does not. Re-deriving its rank here would demote it, the next
        # live pass would promote it back, and the two would trade places
        # forever, rewriting state on every run. Hard filters above still bite.
        if rec.get("scored_live"):
            continue
        new_rank = rank_for(probe.tier, probe.score, rec.get("term", "unknown"),
                            probe.focus_hit, cfg)
        if rec.get("score") != probe.score or rec.get("rank") != new_rank:
            changed += 1
        rec["score"], rec["tier"], rec["rank"] = probe.score, probe.tier, new_rank
    collapsed = collapse_stored_duplicates(state)
    if changed or dropped or restored or tidied or collapsed:
        log(f"revalidated: {changed} re-ranked · {dropped} filtered out · "
            f"{restored} brought back · {tidied} titles tidied · "
            f"{collapsed} duplicates collapsed")


def collapse_stored_duplicates(state: dict) -> int:
    """The same job stored twice under two spellings of the company, or once
    truncated and once whole. Merging at collection time only helps postings
    arriving now, so the pairs already on disk get resolved here."""
    def norm(rec):
        role = TRUNCATED.sub("", (rec.get("role") or "").lower())
        role = re.sub(r"\(.*?\)", " ", role)
        role = re.sub(r"\b(summer|fall|autumn|spring|winter)\b|\b20\d{2}\b", " ", role)
        return slug(LEGAL_SUFFIX.sub("", rec.get("company") or "")), slug(role)

    groups: dict[tuple, list[str]] = {}
    for pid, rec in state["postings"].items():
        if rec.get("filtered"):
            continue
        groups.setdefault(norm(rec), []).append(pid)

    n = 0
    for pids in groups.values():
        if len(pids) < 2:
            continue
        # keep the copy that is applied to, then saved, then best ranked, then
        # the one with the fullest title and a real location
        def quality(pid):
            rec = state["postings"][pid]
            return (pid not in state.get("applied", {}),
                    pid not in state.get("saved", {}),
                    rank_key(rec.get("rank", "C")),
                    -len(rec.get("role", "")),
                    not rec.get("location"))
        pids.sort(key=quality)
        winner = state["postings"][pids[0]]
        for pid in pids[1:]:
            loser = state["postings"][pid]
            for key in ("location", "url", "posted_at"):
                if loser.get(key) and not winner.get(key):
                    winner[key] = loser[key]
            if loser.get("term") == "target":
                winner["term"] = "target"
            if not loser.get("dup"):
                n += 1
            loser["filtered"] = loser["dup"] = True
    return n


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
            role = tidy_role(clean(cell("role")), company)
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
# "Mechanical Engineering Intern (August-December)" is a fall co-op wearing a
# season-neutral title, and it used to sail straight through as "assumed".
OFF_RANGE = re.compile(
    r"\b(aug|august|sept?|september|jan|january)\w*\s*[-–—/]\s*"
    r"(dec|december|may|apr|april)\w*", re.I)
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

    # An explicit non-summer date range in the title settles it before anything
    # else gets a chance to call it "assumed".
    m = OFF_RANGE.search(blob_title)
    if m and not re.search(rf"{term_word}\s*'?{year}", blob_title, re.I):
        return "offseason", f"date range '{m.group(0)}' in title"

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

    # A description we actually read that never says which season it is, at a
    # company that states the season on its other reqs, is a weak signal, not a
    # good one. Kept separate from "we never looked" so ranks can treat them
    # differently: only 'target' is allowed into the top two bands.
    start, end = (parse_iso(d + "T00:00:00+00:00") for d in cfg["season"]["assume_window"])
    stamp = post.posted_at or NOW
    if start and end and start <= stamp <= end:
        if post.description:
            return "assumed", "description read, no season stated anywhere in it"
        return "assumed", "no season stated, inside the target hiring window"
    return "unknown", "no season stated"


# --------------------------------------------------------------------------
# scoring
# --------------------------------------------------------------------------

INTERNISH = re.compile(r"\b(intern|internship|co-?op|apprentice|student)\b", re.I)
# Unambiguous non-US suffixes only. DE/IL/IN are Delaware, Illinois and
# Indiana before they are Germany, Israel and India, and "Chicago, IL" being
# silently dropped as foreign is worse than letting a Tel Aviv req through:
# the country names in location_blocklist catch those anyway.
FOREIGN_CODE = re.compile(
    r",\s*(UK|GB|FR|ES|MX|JP|SG|AU|NZ|NL|PL|BR|KR|CN|IT|SE|CH|IE|NO|FI|DK|AT|BE|PT|CZ|"
    r"ON|QC|BC|AB|MB|SK|NS|NB)\s*$", re.I)
# A description that names mechanical engineering as an eligible major is what
# rescues a title owned by another discipline.
ME_ELIGIBLE = re.compile(
    r"mechanical engineer|mechanical engineering|\bmech\.?\s*e(ng)?\b|"
    r"mechanical,|,\s*mechanical|or mechanical|and mechanical", re.I)


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
    desc = (post.description or "").lower()

    meta = match_company(post.company, index)
    post.tier = meta["tier"] if meta else 9

    reasons, score = [], 0

    if slug(post.company) in {slug(c) for c in cfg.get("company_blocklist", [])}:
        post.score = -99
        post.reasons = ["company on the blocklist"]
        return

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

    core_hit = any(k in title for k in roles["core"])

    # A req titled for another discipline is theirs, not yours, unless the
    # description explicitly opens it to mechanical majors. This is the rule
    # that stops "2027 Electrical Engineer Intern" landing in the top band.
    if not core_hit:
        owned = next((k for k in roles.get("other_discipline", []) if k in title), None)
        if owned:
            if not desc:
                post.score = -99
                post.reasons = [f"titled for another discipline ({owned}), "
                                f"no description to check ME eligibility"]
                return
            if not ME_ELIGIBLE.search(desc):
                post.score = -99
                post.reasons = [f"titled for another discipline ({owned}), "
                                f"description never mentions mechanical"]
                return
            reasons.append(f"{owned} title, but the description takes ME")

    # ---- title signals. Only these can qualify a posting. ----
    title_signal = False
    if core_hit:
        score += 5
        title_signal = True
        reasons.append("core ME title (+5)")

    title_focus = [k for k in roles["focus"] if k in title]
    if title_focus:
        score += 5
        title_signal = True
        post.focus_hit = True
        reasons.append(f"focus area in title: {title_focus[0]} (+5)")

    if any(k in title for k in roles["adjacent"]):
        score += 3
        title_signal = True
        reasons.append("adjacent hardware role (+3)")

    if post.tier <= 2 and any(k in title for k in roles["generic"]):
        score += 2
        title_signal = True
        reasons.append("generic engineering intern at a target company (+2)")

    if not title_signal:
        # Every Rocket Lab description mentions launch vehicles and propulsion,
        # which is how "Talent Ops Intern" once scored a 9. The description is
        # supporting evidence, never the qualifying signal.
        post.score = -99
        post.reasons = ["no relevant role signal in the title"]
        return

    # ---- description signals. Supporting only, and capped. ----
    if not title_focus:
        desc_focus = [k for k in roles["focus"] if k in desc[:2500]]
        if desc_focus:
            score += 2
            post.focus_hit = True
            reasons.append(f"focus area in description: {desc_focus[0]} (+2)")

    tier_bonus = {1: 4, 2: 2, 3: 0}.get(post.tier, -1)
    score += tier_bonus
    reasons.append(f"{post.tier_label} company ({tier_bonus:+d})")

    if any(k in title for k in roles["eligibility_penalty"]):
        score -= 4
        reasons.append("grad-level or returning-student wording (-4)")

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
        return {"version": 4, "postings": {}, "desc_cache": {}, "source_health": {},
                "applied": {}, "saved": {}, "digest_queue": [], "last_digest": None,
                "board_issue": None, "widenet_issue": None}
    state = json.loads(STATE_PATH.read_text())
    for key in ("postings", "desc_cache", "source_health", "applied", "saved"):
        state.setdefault(key, {})
    state.setdefault("digest_queue", [])
    state.setdefault("last_digest", None)
    state.setdefault("board_issue", None)
    state.setdefault("widenet_issue", None)
    state["version"] = 4
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
        pid = item.get("pid")
        rec = state["postings"].get(pid or "", {})
        # a posting the current rules have since rejected, or one you already
        # ticked off, has no business showing up in tonight's brief
        if pid and (pid in state.get("applied", {}) or rec.get("filtered")):
            continue
        bands.get(item.get("rank", "C"), bands["C"]).append(item)
    queue = [i for b in bands.values() for i in b]

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
    # Named for when you read them, not for when they run. The midnight brief
    # covers the working day behind it; the noon brief covers the night.
    label, window = (("Morning brief", "12pm–12am") if NOW.hour < 10
                     else ("Evening brief", "12am–12pm"))
    if total:
        title = f"📋 {label} · {head}"
        body = f"covering {window}\n\n" + "\n".join(lines)
        body = body[:3800]
        priority = 3 if bands["S"] else 2
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
# the posting stops appearing in briefs, roll-ups and the board forever.

CHECKED = re.compile(r"^\s*[-*]\s*\[([ xX])\].*?<!--(?:([as]):)?([0-9a-f]{6,32})-->", re.M)


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


def harvest_checked(state: dict) -> None:
    """Read ticks from wherever you made them — the issue or the file — before
    the board is rebuilt. An apply tick archives the posting, a save tick pins
    it so the 3-day expiry never touches it. Unticking a save releases it."""
    bodies = []
    num = state.get("board_issue")
    if num:
        issue = gh("GET", f"/issues/{num}")
        if issue and issue.get("body"):
            bodies.append(issue["body"])
    wnum = state.get("widenet_issue")
    if wnum:
        issue = gh("GET", f"/issues/{wnum}")
        if issue and issue.get("body"):
            bodies.append(issue["body"])
    for path in (BOARD_PATH, WIDE_PATH):
        if path.exists():
            bodies.append(path.read_text())
    if not bodies:
        return
    applied_n = saved_n = released_n = 0
    for body in bodies:
        for mark, kind, pid in CHECKED.findall(body):
            ticked = mark.lower() == "x"
            if (kind or "a") == "a":
                if ticked and pid not in state["applied"]:
                    state["applied"][pid] = iso(NOW)
                    state["saved"].pop(pid, None)
                    applied_n += 1
            else:
                if ticked and pid not in state["saved"]:
                    state["saved"][pid] = iso(NOW)
                    saved_n += 1
                elif not ticked and pid in state["saved"]:
                    del state["saved"][pid]
                    released_n += 1
    if applied_n or saved_n or released_n:
        log(f"ticks: {applied_n} applied, {saved_n} saved, {released_n} unsaved")


def board_line(pid: str, rec: dict, checked: bool = False,
               company_prefix: bool = True, savable: bool = True) -> list[str]:
    """Two taps per posting and no typing: the top box archives it, the nested
    box pins it. Both carry a hidden id so the next run can read them back."""
    box = "x" if checked else " "
    bits = [f"**{rec.get('company', '?')}** — {rec.get('role', '?')}"
            if company_prefix else rec.get("role", "?")]
    if rec.get("location"):
        bits.append(rec["location"])
    if rec.get("term") == "target":
        bits.append("S27 confirmed")
    elif rec.get("term") == "assumed":
        bits.append("season unstated")
    posted = parse_iso(rec.get("posted_at")) or parse_iso(rec.get("first_seen"))
    if posted:
        bits.append(human_age(posted))
    if rec.get("url"):
        bits.append(f"[apply]({rec['url']})")
    if checked and applied_date(rec):
        bits.append(f"applied {applied_date(rec)}")
    rows = [f"- [{box}] " + " · ".join(bits) + f" <!--a:{pid}-->"]
    if savable:
        pinned = "x" if rec.get("_saved") else " "
        rows.append(f"  - [{pinned}] 📌 keep on the board <!--s:{pid}-->")
    return rows


def applied_date(rec: dict) -> str:
    d = parse_iso(rec.get("applied_at"))
    return f"{d:%b %d}" if d else ""


def band_rows(items: list[tuple[str, dict]], savable: bool = True) -> list[str]:
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
            rows += board_line(group[0][0], group[0][1], savable=savable)
            rows.append("")
            continue
        rows.append(f"**{company}** ({len(group)})")
        for pid, rec in group:
            rows += board_line(pid, rec, company_prefix=False, savable=savable)
        rows.append("")
    return rows


def too_old(rec: dict, cfg: dict) -> bool:
    """A req that has been sitting open for two weeks is either filled or
    ignoring its own pipeline. Top-rank gets a longer leash, nothing gets an
    unlimited one."""
    routing = cfg["routing"]
    limit = int(routing.get("max_posting_age_days_top", 21)) if rec.get("rank") == "S" \
        else int(routing.get("max_posting_age_days", 14))
    stamp = parse_iso(rec.get("posted_at")) or parse_iso(rec.get("first_seen"))
    return bool(stamp and (NOW - stamp) > timedelta(days=limit))


def compose_board(state: dict, cfg: dict) -> str:
    routing = cfg["routing"]
    fresh_days = float(routing.get("board_active_days", 3))
    applied_hours = float(routing.get("applied_visible_hours", 3))
    applied, saved = state.get("applied", {}), state.get("saved", {})

    bands = {r: [] for r in RANK_ORDER}
    pinned, recent_applied = [], []
    for pid, rec in state["postings"].items():
        if pid in applied:
            rec = dict(rec, applied_at=applied[pid])
            when = parse_iso(applied[pid])
            if when and (NOW - when) <= timedelta(hours=applied_hours):
                recent_applied.append((pid, rec))
            continue
        if rec.get("filtered"):
            continue
        if pid in saved:
            pinned.append((pid, dict(rec, _saved=True)))
            continue
        if too_old(rec, cfg):
            continue
        seen = parse_iso(rec.get("first_seen"))
        if seen and (NOW - seen) > timedelta(days=fresh_days):
            continue
        bands.get(rec.get("rank", "C"), bands["C"]).append((pid, rec))

    for band in bands.values():
        band.sort(key=lambda kv: (-int(kv[1].get("score", 0)), kv[1].get("company", "")))
    pinned.sort(key=lambda kv: (rank_key(kv[1].get("rank", "C")), -int(kv[1].get("score", 0))))
    recent_applied.sort(key=lambda kv: kv[1].get("applied_at", ""), reverse=True)

    live = sum(len(bands[r]) for r in ("S", "A", "B"))
    out = ["# Summer 2027 board",
           f"_rebuilt {NOW:%Y-%m-%d %H:%M} UTC · tick the top box when you apply · "
           f"tick 📌 to keep something past the {fresh_days:g}-day expiry_", "",
           f"**{live} open · {len(bands['S'])} apply-now · {len(pinned)} saved · "
           f"{len(applied)} applied all-time**", ""]
    if GITHUB_REPO:
        out += [f"Full applied log, by company: "
                f"[state/APPLIED.md](https://github.com/{GITHUB_REPO}/blob/main/state/APPLIED.md)", ""]

    if pinned:
        out += [f"## 📌 Saved ({len(pinned)})",
                "_untick 📌 to let these expire again_", ""]
        out += band_rows(pinned)

    for r in ("S", "A", "B"):
        items = bands[r]
        if not items:
            continue
        out += [f"## {RANK_META[r]['board']} ({len(items)})", ""]
        out += band_rows(items)

    # The wide net is real but it is not what this page is for. It lives on its
    # own page so the board stays a short list of things worth your afternoon.
    if bands["C"]:
        link = (f"[wide net]({wide_url(state)})" if wide_url(state) else "the wide net page")
        out += [f"### · Wide net ({len(bands['C'])})",
                f"Unlisted companies that still match. Parked on {link} so they "
                f"cannot bury the bands above. Ticking 📌 there moves one here.", ""]

    if recent_applied:
        out += [f"## ✅ Just applied ({len(recent_applied)})",
                f"_clears from this board {applied_hours:g}h after you tick it, "
                f"and moves to the applied log_", ""]
        for pid, rec in recent_applied:
            out += board_line(pid, rec, checked=True, savable=False)
        out.append("")
    if live == 0 and not pinned:
        out += ["_Nothing open right now. Everything that dropped has either been "
                "applied to, expired, or did not clear the bar._", ""]
    return "\n".join(out)


def compose_wide_net(state: dict, cfg: dict) -> str:
    """Everything that matched but is not at a company on your list. Same two
    taps: apply archives it, 📌 promotes it onto the real board."""
    routing = cfg["routing"]
    fresh_days = float(routing.get("board_active_days", 3))
    applied, saved = state.get("applied", {}), state.get("saved", {})
    items = []
    for pid, rec in state["postings"].items():
        if pid in applied or pid in saved or rec.get("filtered"):
            continue
        if rec.get("rank") != "C" or too_old(rec, cfg):
            continue
        seen = parse_iso(rec.get("first_seen"))
        if seen and (NOW - seen) > timedelta(days=fresh_days):
            continue
        items.append((pid, rec))
    items.sort(key=lambda kv: (-int(kv[1].get("score", 0)), kv[1].get("company", "")))
    out = ["# Wide net",
           f"_{len(items)} open · rebuilt {NOW:%Y-%m-%d %H:%M} UTC · "
           f"tick 📌 to move one onto the main board_", ""]
    if state.get("board_issue") and GITHUB_REPO:
        out += [f"Main board: https://github.com/{GITHUB_REPO}/issues/{state['board_issue']}", ""]
    if not items:
        out.append("_Nothing here right now._")
        return "\n".join(out)
    out += band_rows(items)
    return "\n".join(out)


def wide_url(state: dict) -> str | None:
    num = state.get("widenet_issue")
    return f"https://github.com/{GITHUB_REPO}/issues/{num}" if num and GITHUB_REPO else None


def compose_applied_log(state: dict) -> str:
    """The permanent record, grouped by company, so you can see at a glance
    where you have and have not put an application in."""
    applied = state.get("applied", {})
    by_company: dict[str, list[tuple[str, dict]]] = {}
    for pid, when in applied.items():
        rec = dict(state["postings"].get(pid, {}), applied_at=when)
        by_company.setdefault(rec.get("company", "unknown"), []).append((pid, rec))
    out = ["# Applied log", "",
           f"_{len(applied)} applications across {len(by_company)} companies · "
           f"updated {NOW:%Y-%m-%d %H:%M} UTC_", ""]
    for company in sorted(by_company, key=lambda c: (-len(by_company[c]), c.lower())):
        group = sorted(by_company[company], key=lambda kv: kv[1].get("applied_at", ""), reverse=True)
        out.append(f"## {company} ({len(group)})")
        for pid, rec in group:
            when = parse_iso(rec.get("applied_at"))
            bits = [rec.get("role", "?")]
            if rec.get("location"):
                bits.append(rec["location"])
            bits.append(f"applied {when:%b %d, %Y}" if when else "applied")
            if rec.get("url"):
                bits.append(f"[posting]({rec['url']})")
            out.append("- " + " · ".join(bits))
        out.append("")
    if not applied:
        out.append("_Nothing yet. Tick a box on the board and it lands here._")
    return "\n".join(out)


def sync_issue(state: dict, key: str, title: str, body: str) -> None:
    num = state.get(key)
    body = body[:65000]          # GitHub caps an issue body at 65536 characters
    if num:
        if gh("PATCH", f"/issues/{num}", {"body": body, "title": title}):
            log(f"  issue #{num} updated ({title})")
            return
        log(f"  issue #{num} update failed, recreating")
    issue = gh("POST", "/issues", {"title": title, "body": body})
    if issue:
        state[key] = issue["number"]
        log(f"  issue #{issue['number']} created ({title})")


def publish_board(state: dict, cfg: dict, dry: bool) -> None:
    routing = cfg["routing"]
    body = compose_board(state, cfg)
    wide = compose_wide_net(state, cfg)
    log_md = compose_applied_log(state)
    BOARD_PATH.parent.mkdir(parents=True, exist_ok=True)
    if not dry:
        BOARD_PATH.write_text(body)
        WIDE_PATH.write_text(wide)
        APPLIED_PATH.write_text(log_md)
    if dry or not (GITHUB_TOKEN and GITHUB_REPO):
        log(f"board {len(body)} chars · wide net {len(wide)} · applied log {len(log_md)}")
        return
    sync_issue(state, "board_issue",
               routing.get("board_title", "📋 Summer 2027 internship board"), body)
    sync_issue(state, "widenet_issue",
               routing.get("widenet_title", "🕸️ Wide net — everything else that matched"), wide)


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

    def one(src):
        try:
            r = SESSION.get(src["url"], timeout=20)
            r.raise_for_status()
            return src, parse_tracker(r.text, src["name"], src.get("season_hint"))
        except requests.RequestException as e:
            log(f"  tracker {src['name']} unreachable: {type(e).__name__}")
            return src, []

    with ThreadPoolExecutor(max_workers=5) as pool:
        results = list(pool.map(one, cfg["trackers"]))

    for src, rows in results:
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
    posting and read it. Capped per run, cached forever by URL, and fetched in
    parallel: done one at a time this could eat six minutes of a twelve-minute
    job on its own, which is how a run ends up cancelled instead of finished."""
    budget = int(cfg["routing"]["max_description_fetches"])
    timeout = float(cfg["routing"].get("description_timeout", 8))
    cache = state["desc_cache"]
    todo = []
    for p in posts:
        if p.source_kind == "ats" or not p.url:
            continue
        if p.tier > 3 and not p.focus_hit:
            continue
        if find_seasons(p.role):
            continue
        key = sha(p.url)
        if key in cache:
            p.description = cache[key].get("snippet", "")
            continue
        todo.append((key, p))
    # season confirmation is worth most where the company matters most
    todo.sort(key=lambda kp: (kp[1].tier, -kp[1].score))
    todo = todo[:budget]
    if not todo:
        return

    def one(item):
        key, p = item
        try:
            r = SESSION.get(p.url, timeout=timeout, allow_redirects=True)
            # an error page is not a job description — treating one as text is
            # how you end up "classifying" the season off a 403 body
            ct = r.headers.get("Content-Type", "")
            body = clean(r.text)[:6000] if (r.ok and ("html" in ct or "text" in ct)) else ""
        except requests.RequestException:
            body = ""
        return key, p, body

    with ThreadPoolExecutor(max_workers=6) as pool:
        for key, p, body in pool.map(one, todo):
            cache[key] = {"snippet": body[:2000], "checked": iso(NOW)}
            p.description = body
    log(f"read {len(todo)} descriptions to confirm season")

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
        # A Summer-2027-only tracker listing the same req is real season
        # evidence, and it used to be thrown away with the duplicate.
        if not cur.season_hint and p.season_hint:
            cur.season_hint = p.season_hint
        if not cur.description and p.description:
            cur.description = p.description
        if p.posted_at and (cur.posted_at is None or p.posted_at < cur.posted_at):
            if cur.source_kind != "ats":
                cur.posted_at = p.posted_at
        if not cur.url and p.url:
            cur.url = p.url
        if not cur.location and p.location:
            cur.location = p.location
    return merge_duplicates(list(best.values()))


def merge_duplicates(posts: list[Posting]) -> list[Posting]:
    """One tracker publishes "Curtiss-Wright Corporation", another publishes
    "Curtiss-Wright"; one truncates the title, another appends the company to
    it. The dedupe key misses all of that and the same job lands on the board
    three times. Merging runs on this run's postings only, never on stored ids,
    so tightening it can never make old postings look new again."""

    def norm_company(name: str) -> str:
        return slug(LEGAL_SUFFIX.sub("", name or ""))

    def norm_role(role: str) -> str:
        r = TRUNCATED.sub("", (role or "").lower())
        r = re.sub(r"\(.*?\)", " ", r)
        r = re.sub(r"\b(summer|fall|autumn|spring|winter)\b|\b20\d{2}\b", " ", r)
        return slug(r)

    def absorb(parent: Posting, child: Posting) -> None:
        for src in [child.source] + child.also_seen:
            if src != parent.source and src not in parent.also_seen:
                parent.also_seen.append(src)
        if not parent.url and child.url:
            parent.url = child.url
        if not parent.location and child.location:
            parent.location = child.location
        if not parent.description and child.description:
            parent.description = child.description
        if not parent.season_hint and child.season_hint:
            parent.season_hint = child.season_hint
        if child.posted_at and (parent.posted_at is None or child.posted_at < parent.posted_at):
            parent.posted_at = child.posted_at

    buckets: dict[str, list[Posting]] = {}
    for p in posts:
        buckets.setdefault(norm_company(p.company), []).append(p)

    out = []
    for group in buckets.values():
        # fullest, most authoritative copy wins; everything else folds into it
        group.sort(key=lambda x: (bool(TRUNCATED.search(x.role)),
                                  0 if x.source_kind == "ats" else 1,
                                  -len(x.role)))
        keep: list[Posting] = []
        for p in group:
            stem = norm_role(p.role)
            parent = next((k for k in keep if norm_role(k.role) == stem), None)
            if parent is None and TRUNCATED.search(p.role) and len(stem) >= 10:
                parent = next((k for k in keep if norm_role(k.role).startswith(stem[:-2])), None)
            if parent is None:
                keep.append(p)
            else:
                absorb(parent, p)
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
    revalidate(state, cfg, index)

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
    fresh_hits, queued, new_count, stale_count = [], [], 0, 0
    for p in sorted(kept, key=lambda x: (rank_key(x.rank), -x.score, x.company)):
        pid = p.pid
        record = state["postings"].get(pid)
        if record:
            # still live — this is what lets the board expire dead postings
            # and lets a pulled req drop off the board on its own
            record["last_seen"] = iso(NOW)
            # early records were written before some fields existed, and a
            # tracker copy often carries a location the ATS copy lacked
            for key, value in (("location", p.location), ("url", p.url),
                               ("posted_at", iso(p.posted_at))):
                if value and not record.get(key):
                    record[key] = value
            if p.term == "target" and record.get("term") != "target":
                record["term"] = "target"     # season finally confirmed somewhere
            # the live pass sees the description, so its verdict wins outright
            record["score"], record["tier"] = p.score, p.tier
            record["rank"], record["scored_live"] = p.rank, True
            for s in [p.source] + p.also_seen:
                if s not in record.get("sources", []):
                    record.setdefault("sources", []).append(s)
            continue
        if pid in applied:
            continue                          # already ticked off, never alert again
        # An old req surfacing for the first time is a tracker catching up, not
        # news. Never alert on it, and never put it on the board.
        age_days = (NOW - p.posted_at).days if p.posted_at else 0
        limit = int(routing.get("max_posting_age_days_top", 21)) if p.rank == "S" \
            else int(routing.get("max_posting_age_days", 14))
        if age_days > limit:
            stale_count += 1
            continue
        new_count += 1
        state["postings"][pid] = {
            "first_seen": iso(NOW), "last_seen": iso(NOW), "posted_at": iso(p.posted_at),
            "company": p.company, "role": p.role, "location": p.location,
            "term": p.term, "score": p.score, "tier": p.tier, "rank": p.rank,
            "sources": [p.source] + p.also_seen, "url": p.url,
        }
        if any("description takes ME" in r for r in p.reasons):
            state["postings"][pid]["me_ok"] = True
        fresh_hits.append(p)
    if stale_count:
        log(f"{stale_count} skipped as already open too long")

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


def doctor() -> int:
    """Answers "why did that run fail" without reading a log. Checks config
    keys, state sanity, the ntfy topic and the GitHub token in one pass."""
    problems = []
    try:
        cfg = yaml.safe_load(CONFIG_PATH.read_text())
    except Exception as e:
        log(f"targets.yml will not parse: {e}")
        return 1
    for key in ("season", "routing", "ranks", "roles", "companies", "trackers"):
        if key not in cfg:
            problems.append(f"targets.yml is missing the '{key}' block")
    for key in ("digest_min_score", "board_active_days", "brief_hours_utc"):
        if key not in cfg.get("routing", {}):
            problems.append(f"routing.{key} is missing")
    log(f"config     ok, {len(cfg.get('trackers', []))} trackers, "
        f"{sum(len(v or []) for v in cfg.get('companies', {}).values())} companies")

    state = load_state()
    log(f"state      {len(state['postings'])} postings, {len(state['applied'])} applied, "
        f"{len(state['saved'])} saved, queue {len(state['digest_queue'])}, "
        f"board issue {state.get('board_issue')}, wide net {state.get('widenet_issue')}")
    size = STATE_PATH.stat().st_size / 1024 if STATE_PATH.exists() else 0
    log(f"seen.json  {size:.0f} KB")
    if size > 4000:
        problems.append("seen.json is over 4 MB, lower routing.state_keep_days")

    log(f"ntfy       topic {'set' if NTFY_TOPIC else 'MISSING'}, "
        f"top-lane topic {'set' if NTFY_TOPIC_TOP else 'not set (optional)'}")
    if not NTFY_TOPIC:
        problems.append("NTFY_TOPIC is not set, so no notification can be sent")

    if GITHUB_TOKEN and GITHUB_REPO:
        who = gh("GET", "")
        if who is None:
            problems.append("the GitHub token cannot read this repo, so the board "
                            "cannot be written; check permissions: issues: write")
        else:
            log(f"github     ok, issues {'enabled' if who.get('has_issues') else 'DISABLED'}")
            if not who.get("has_issues"):
                problems.append("issues are disabled on this repo, so the board has "
                                "nowhere to live; turn them on in repo settings")
    else:
        log("github     no token in the environment (fine locally, needed in Actions)")

    stale = [n for n, h in state.get("source_health", {}).items()
             if not h.get("last_ok") or (NOW - parse_iso(h["last_ok"])) > timedelta(days=2)]
    if stale:
        problems.append(f"tracker sources quiet for over 2 days: {', '.join(stale)}")

    log("")
    for pr in problems:
        log(f"  PROBLEM  {pr}")
    if not problems:
        log("  no problems found")
    return 1 if problems else 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="print, don't push or save")
    ap.add_argument("--seed", action="store_true", help="record a baseline silently")
    ap.add_argument("--brief", "--digest", dest="brief", action="store_true",
                    help="force the daily brief now")
    ap.add_argument("--board-only", action="store_true", help="rebuild the board, send nothing")
    ap.add_argument("--explain", action="store_true", help="show every match with rank and score")
    ap.add_argument("--verify-boards", action="store_true", help="check every ATS token")
    ap.add_argument("--doctor", action="store_true", help="self-check config, state and tokens")
    args = ap.parse_args()
    if args.doctor:
        return doctor()
    if args.verify_boards:
        return verify_boards(args)
    return run(args)


if __name__ == "__main__":
    sys.exit(main())
