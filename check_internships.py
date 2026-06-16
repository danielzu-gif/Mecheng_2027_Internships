"""
Summer 2027 Mechanical Engineering Internship Watcher
------------------------------------------------------
Checks a few community-maintained internship tracker repos (the same ones
thousands of students already use) for postings that match BOTH:
  1) a "big tech" / hardware-heavy company name, and
  2) a mechanical-engineering-relevant role title

When a NEW match shows up (one we haven't already alerted on), it pushes an
instant notification to your phone via ntfy.sh.

This is designed to be run on a schedule by GitHub Actions (see
.github/workflows/check.yml) so it works even when your laptop is off.

EDIT THESE TWO LISTS to broaden/narrow what counts as a match for you.
"""

import json
import os
import re
import sys
import requests

# ---------------------------------------------------------------------------
# CONFIG — feel free to edit
# ---------------------------------------------------------------------------

# Raw README URLs of community-maintained internship trackers.
# These repos are updated multiple times a day by their maintainers/community.
# NOTE: repo names sometimes get renamed each year (e.g. ...-Engineer-Internship
# repos roll from "2026" to "2027" naming). If a source starts silently
# returning nothing, check whether the repo got renamed and update the URL.
SOURCES = [
    "https://raw.githubusercontent.com/vanshb03/Summer2027-Internships/dev/README.md",
    "https://raw.githubusercontent.com/jobright-ai/2026-Engineer-Internship/master/README.md",
    "https://raw.githubusercontent.com/sndsh404/summer-2027-internships/main/README.md",
]

# Substring match against the company name (case-insensitive). Add/remove freely.
BIG_TECH_KEYWORDS = BIG_TECH_KEYWORDS = [
    # Big Tech / Consumer Hardware
    "apple", "google", "alphabet", "amazon", "microsoft", "meta", "facebook",
    "nvidia", "intel", "amd", "qualcomm", "samsung", "sony", "dell", "hp inc",
    "hewlett packard", "hpe", "ibm", "oracle", "cisco", "juniper", "supermicro",
    "lenovo", "logitech", "corsair", "razer", "dropbox", "cloudflare",

    # Aerospace / Space / Launch / Satellites
    "spacex", "blue origin", "boeing", "boeing defense", "lockheed", 
    "lockheed martin", "northrop", "northrop grumman", "raytheon", "rtx",
    "collins aerospace", "pratt & whitney", "ge aerospace", "general electric",
    "honeywell", "textron", "bell flight", "sikorsky", "gulfstream",
    "bombardier", "embraer", "airbus", "rolls-royce", "rocket lab",
    "relativity space", "firefly aerospace", "sierra space", "axiom space",
    "astranis", "planet labs", "maxar", "ast spacemobile", "intuitive machines",
    "vast", "varda", "impulse space", "anduril", "shield ai", "skydio",
    "aerojet rocketdyne", "l3harris", "general atomics", "kratos",
    "leidos", "saic", "booz allen", "mitre", "aurora flight sciences",

    # eVTOL / Aviation Electrification / Clean Aviation
    "joby", "joby aviation", "archer aviation", "wisk", "beta technologies",
    "electra.aero", "electra", "eve air mobility", "supernal", "magniX",
    "ampaire", "heart aerospace", "regent craft", "urban sky",

    # Automotive / EV / Mobility / Autonomous Vehicles
    "tesla", "rivian", "lucid", "ford", "ford motor", "general motors", "gm",
    "stellantis", "chrysler", "toyota", "honda", "hyundai", "kia", "nissan",
    "bmw", "mercedes-benz", "volkswagen", "audi", "porsche", "volvo",
    "polestar", "ferrari", "mazda", "subaru", "canoo", "fisker",
    "waymo", "zoox", "cruise", "aurora innovation", "nuro", "motive",
    "aptiv", "magna", "bosch", "continental", "zf", "denso", "valeo",
    "lear", "forvia", "faurecia", "autoliv", "adient", "te connectivity",

    # Robotics / Automation / Industrial Hardware
    "boston dynamics", "figure ai", "agility robotics", "apptronik",
    "tesla robotics", "symbotic", "teradyne", "universal robots",
    "fanuc", "abb", "kuka", "yaskawa", "rockwell automation", "siemens",
    "schneider electric", "emerson", "parker hannifin", "eaton",
    "nidec", "omron", "keyence", "cognex", "intuitive surgical",
    "gray matter robotics", "zipline", "serve robotics",

    # Semiconductor / Chip Manufacturing / Equipment
    "tsmc", "globalfoundries", "samsung semiconductor", "micron",
    "texas instruments", "analog devices", "broadcom", "marvell",
    "onsemi", "nxp", "infineon", "stmicroelectronics", "renesas",
    "applied materials", "lam research", "asml", "kla", "tokyo electron",
    "asm", "synopsys", "cadence", "teradyne", "formfactor", "atomic semi",

    # Energy / Battery / Nuclear / Renewables
    "redwood materials", "panasonic energy", "lg energy solution", "sk on",
    "northvolt", "quantumscape", "solid power", "factorial energy",
    "form energy", "our next energy", "one", "sila nanotechnologies",
    "enovix", "fluence", "tesla energy", "exxonmobil", "chevron",
    "shell", "bp", "conocophillips", "slb", "schlumberger", "halliburton",
    "baker hughes", "duke energy", "nextera energy", "constellation energy",
    "ge vernova", "siemens energy", "vestas", "first solar", "enphase",
    "solaredge", "nrel", "nuScale", "terrapower", "kairos power",
    "x-energy", "hadron energy", "westinghouse", "holtec",

    # Manufacturing / Industrial / Materials
    "3m", "dupont", "dow", "corning", "carrier", "trane", "johnson controls",
    "ingersoll rand", "caterpillar", "john deere", "deere", "cummins",
    "paccar", "kenworth", "peterbilt", "oshkosh", "navistar",
    "generac", "stanley black & decker", "black & decker", "milwaukee tool",
    "tti", "snap-on", "grainger", "fastenal", "illinois tool works", "itw",
    "ingersoll rand", "regal rexnord", "pentair", "xylem", "graco",
    "flowserve", "itt", "ametek", "teledyne", "keysight", "fluke",
    "ni", "national instruments", "thermo fisher", "agilent",

    # Medical Device / Biomechanical / Product Design
    "medtronic", "stryker", "boston scientific", "abbott", "johnson & johnson",
    "j&j", "ethicon", "zimmer biomet", "smith & nephew", "bd",
    "becton dickinson", "baxter", "edwards lifesciences", "intuitive surgical",
    "resmed", "dexcom", "insulet", "align technology", "philips healthcare",
    "ge healthcare", "Siemens Healthineers",

    # Civil / Infrastructure / MEP / Engineering Consulting
    "jacobs", "aecom", "wsp", "hdr", "burns & mcdonnell", "black & veatch",
    "arcadis", "stantec", "kimley-horn", "tetratech", "tishman",
    "turner construction", "bechtel", "fluor", "kiewit", "skanska",
    "gannett fleming", "parsons", "syska hennessy", "mott macdonald",

    # Defense / Shipbuilding / Marine
    "general dynamics", "gdms", "electric boat", "huntington ingalls",
    "hii", "newport news shipbuilding", "bath iron works", "naval nuclear laboratory",
    "curtiss-wright", "woodward", "moog", "bwxt", "bae systems",
    "rheinmetall", "oshkosh defense", "am general",

    # Smaller / Startup / Hardware-Focused Companies
    "rebuild manufacturing", "slate auto", "slate", "roush", "karman",
    "kaman", "hadrian", "machina labs", "orbital composites", "seurat",
    "fictiv", "xometry", "protolabs", "carbon", "formlabs", "markforged",
    "desktop metal", "stratasys", "mantle", "vulcanforms", "spade",
]

# Substring match against the role title (case-insensitive). Add/remove freely.
ME_KEYWORDS = [
    # Core Mechanical Engineering
    "mechanical engineer",
    "mechanical engineering",
    "mechanical design",
    "mechanical design engineer",
    "mechanical systems",
    "mechanical systems engineer",
    "mechanical intern",
    "mechanical engineering intern",
    "mechanical engineering internship",
    "mechanical engineer intern",
    "mechanical engineering co-op",
    "mechanical co-op",
    "mechanical product design",
    "product design engineer",
    "product development engineer",
    "design engineering intern",
    "design engineer intern",
    "r&d engineering intern",
    "r&d engineer intern",

    # Hardware / Electromechanical / Mechatronics
    "hardware engineer",
    "hardware engineering intern",
    "mechanical hardware",
    "hardware design engineer",
    "electromechanical",
    "electromechanical engineer",
    "mechatronics",
    "mechatronics engineer",
    "robotics engineer",
    "robotics engineering intern",
    "automation engineer",
    "controls engineer",
    "test hardware engineer",
    "prototype engineer",
    "prototype engineering intern",
    "rapid prototyping",
    "new product introduction",
    "npi engineer",
    "npi intern",

    # Thermal / Fluids / Energy
    "thermal engineer",
    "thermal engineering intern",
    "thermal systems engineer",
    "thermal design engineer",
    "thermal analyst",
    "heat transfer",
    "fluid systems",
    "fluids engineer",
    "cooling systems",
    "battery thermal",
    "battery thermal engineer",
    "thermal management",
    "hvac engineer",
    "energy systems engineer",
    "propulsion engineer",
    "electric propulsion",
    "powertrain engineer",
    "powertrain engineering intern",

    # Manufacturing / Tooling / Industrial
    "manufacturing engineer",
    "manufacturing engineering intern",
    "manufacturing engineering internship",
    "manufacturing co-op",
    "process engineer",
    "process engineering intern",
    "tooling engineer",
    "tooling engineering intern",
    "production engineer",
    "industrial engineer",
    "industrial engineering intern",
    "quality engineer",
    "quality engineering intern",
    "supplier quality engineer",
    "operations engineering intern",
    "factory engineering intern",
    "assembly engineer",
    "automation manufacturing",
    "dfm",
    "design for manufacturing",
    "additive manufacturing",
    "3d printing",
    "advanced manufacturing",

    # Aerospace / Structures / Defense
    "aerospace engineer",
    "aerospace engineering intern",
    "aeronautical engineer",
    "flight hardware",
    "spacecraft mechanical",
    "space systems",
    "space systems engineer",
    "payload engineer",
    "launch vehicle",
    "propulsion intern",
    "structures engineer",
    "structural engineer",
    "structural analysis",
    "stress engineer",
    "stress analysis",
    "mechanisms engineer",
    "mechanical systems design",
    "airframe",
    "avionics mechanical",
    "ground support equipment",
    "gse engineer",
    "mission systems",

    # Automotive / EV / Battery
    "vehicle engineering intern",
    "vehicle engineer",
    "chassis engineer",
    "body engineer",
    "battery engineer",
    "battery engineering intern",
    "cell engineering intern",
    "battery pack",
    "battery pack engineer",
    "energy storage engineer",
    "ev engineer",
    "electric vehicle",
    "drivetrain engineer",
    "powertrain calibration",
    "prototype vehicle",
    "test engineer intern",
    "vehicle test engineer",
    "validation engineer",
    "reliability engineer",

    # Simulation / Analysis / CAD
    "cad engineer",
    "cad design intern",
    "solidworks",
    "catia",
    "creo",
    "nx",
    "fusion 360",
    "autocad",
    "ansys",
    "fea",
    "finite element",
    "cfd",
    "computational fluid dynamics",
    "simulation engineer",
    "simulation engineering intern",
    "modeling and simulation",
    "mechanical analysis",
    "design analysis",
]

STATE_FILE = "seen.json"
NTFY_TOPIC = os.environ.get("NTFY_TOPIC")

# ---------------------------------------------------------------------------
# Parsing helpers
# ---------------------------------------------------------------------------

def strip_markdown(text: str) -> str:
    """Turn a raw markdown/HTML table cell into plain readable text."""
    text = re.sub(r"<[^>]+>", " ", text)              # strip HTML tags
    text = text.replace("**", "")                      # strip bold markers
    text = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", text)  # [text](url) -> text
    text = re.sub(r"\s+", " ", text).strip()
    return text


def extract_url(raw_cell: str):
    """Pull the first link out of a raw table cell, HTML or markdown style."""
    m = re.search(r'href="([^"]+)"', raw_cell)
    if m:
        return m.group(1)
    m = re.search(r"\]\((https?://[^)]+)\)", raw_cell)
    if m:
        return m.group(1)
    return None


def extract_table_rows(md_text: str):
    """Find the first markdown table whose header starts with '| Company'
    and return each data row as a list of 5 cleaned cell strings."""
    rows = []
    in_table = False
    for line in md_text.splitlines():
        stripped = line.strip()
        if stripped.lower().startswith("| company"):
            in_table = True
            continue
        if not in_table:
            continue
        if not stripped.startswith("|"):
            if rows:
                break  # table ended
            continue
        if re.match(r"^\|[\s\-]+\|", stripped):
            continue  # the |---|---|---| divider row
        cells = [c.strip() for c in stripped.split("|")]
        cells = cells[1:-1] if len(cells) > 2 else cells
        if len(cells) >= 5:
            rows.append(cells[:5])
    return rows


def parse_postings(md_text: str, source_name: str):
    postings = []
    last_company = None
    for cells in extract_table_rows(md_text):
        company_raw, role_raw, location_raw, link_raw, date_raw = cells
        company = strip_markdown(company_raw)
        if company in ("↳", "") :
            company = last_company
        else:
            last_company = company
        if not company:
            continue
        url = extract_url(link_raw) or extract_url(role_raw) or extract_url(company_raw)
        postings.append({
            "source": source_name,
            "company": company,
            "role": strip_markdown(role_raw),
            "location": strip_markdown(location_raw),
            "date": strip_markdown(date_raw),
            "url": url,
        })
    return postings


def matches_filters(p: dict) -> bool:
    company = p["company"].lower()
    role = p["role"].lower()
    company_hit = any(k in company for k in BIG_TECH_KEYWORDS)
    role_hit = any(k in role for k in ME_KEYWORDS)
    return company_hit and role_hit


def posting_id(p: dict) -> str:
    return f"{p['company']}|{p['role']}|{p['url']}"


# ---------------------------------------------------------------------------
# State + notification
# ---------------------------------------------------------------------------

def load_seen() -> set:
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE) as f:
                return set(json.load(f))
        except (json.JSONDecodeError, OSError):
            return set()
    return set()


def save_seen(seen: set):
    with open(STATE_FILE, "w") as f:
        json.dump(sorted(seen), f, indent=2)


def notify(p: dict):
    if not NTFY_TOPIC:
        print("NTFY_TOPIC not set — skipping push, but here's the match:")
        print(json.dumps(p, indent=2))
        return
    title = f"{p['company']}: {p['role']}"
    message = f"{p['location']}"
    headers = {
        "Title": title,
        "Priority": "high",
        "Tags": "tools,rotating_light",
    }
    if p.get("url"):
        headers["Click"] = p["url"]
        message += f"\n{p['url']}"
    try:
        requests.post(
            f"https://ntfy.sh/{NTFY_TOPIC}",
            data=message.encode("utf-8"),
            headers=headers,
            timeout=10,
        )
        print(f"Notified: {p['company']} - {p['role']}")
    except requests.RequestException as e:
        print(f"Failed to send notification for {p['company']}: {e}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    seen = load_seen()
    new_count = 0

    for url in SOURCES:
        try:
            resp = requests.get(url, timeout=20)
            resp.raise_for_status()
        except requests.RequestException as e:
            print(f"Could not fetch {url}: {e}")
            continue

        for p in parse_postings(resp.text, url):
            if not matches_filters(p):
                continue
            pid = posting_id(p)
            if pid in seen:
                continue
            notify(p)
            seen.add(pid)
            new_count += 1

    save_seen(seen)
    print(f"Done. {new_count} new matching posting(s) this run.")


if __name__ == "__main__":
    sys.exit(main() or 0)
