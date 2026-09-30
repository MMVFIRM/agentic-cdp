"""Deterministic normalizers. No network, no randomness, no model.

Every function is pure: same input -> same output, which is what makes the
canonical layer and replay reproducible.
"""
from __future__ import annotations

import datetime as dt
import re
import unicodedata

EMAIL_RE = re.compile(r"^[A-Za-z0-9._%+'\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}$")
GMAIL_DOMAINS = {"gmail.com", "googlemail.com"}

# Small, auditable nickname table (extend via config in production).
NICKNAMES = {
    "bob": "robert", "bobby": "robert", "rob": "robert", "robbie": "robert",
    "bill": "william", "billy": "william", "will": "william", "willy": "william", "liam": "william",
    "jim": "james", "jimmy": "james", "jamie": "james",
    "mike": "michael", "mikey": "michael", "mick": "michael",
    "dave": "david", "davey": "david",
    "dick": "richard", "rick": "richard", "ricky": "richard", "rich": "richard", "richie": "richard",
    "tom": "thomas", "tommy": "thomas",
    "joe": "joseph", "joey": "joseph",
    "chris": "christopher", "kit": "christopher",
    "dan": "daniel", "danny": "daniel",
    "matt": "matthew",
    "tony": "anthony",
    "steve": "steven", "stephen": "steven",
    "jon": "jonathan", "johnny": "john", "jack": "john",
    "ed": "edward", "eddie": "edward", "ted": "edward", "ned": "edward",
    "andy": "andrew", "drew": "andrew",
    "alex": "alexander", "sasha": "alexander",
    "ben": "benjamin", "benny": "benjamin",
    "sam": "samuel", "sammy": "samuel",
    "nick": "nicholas", "nicky": "nicholas",
    "greg": "gregory", "pat": "patrick", "patty": "patricia", "trish": "patricia",
    "liz": "elizabeth", "beth": "elizabeth", "betty": "elizabeth", "lizzie": "elizabeth", "eliza": "elizabeth",
    "kate": "katherine", "katie": "katherine", "kathy": "katherine", "cathy": "katherine", "catherine": "katherine",
    "jen": "jennifer", "jenny": "jennifer",
    "sue": "susan", "susie": "susan",
    "meg": "margaret", "maggie": "margaret", "peggy": "margaret",
    "debbie": "deborah", "deb": "deborah",
    "becky": "rebecca", "becca": "rebecca",
    "vicky": "victoria", "tori": "victoria",
    "abby": "abigail", "mandy": "amanda", "manny": "manuel",
    "jess": "jessica", "jessie": "jessica",
    "chuck": "charles", "charlie": "charles",
    "larry": "lawrence", "hank": "henry", "harry": "henry",
}

SUFFIXES = {"jr": "jr", "jr.": "jr", "junior": "jr", "sr": "sr", "sr.": "sr", "senior": "sr",
            "ii": "ii", "iii": "iii", "iv": "iv", "2nd": "ii", "3rd": "iii"}


def strip_accents(s: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFKD", s) if not unicodedata.combining(c))


def clean_str(v) -> str | None:
    if v is None:
        return None
    s = str(v).strip()
    if not s or s.lower() in {"null", "none", "n/a", "na", "-", "unknown"}:
        return None
    return re.sub(r"\s+", " ", s)


def email(v) -> str | None:
    s = clean_str(v)
    if not s:
        return None
    s = s.lower().strip("<>").replace("mailto:", "")
    return s if EMAIL_RE.match(s) else None


def email_key(e: str) -> str:
    """Match key: collapse gmail dots and +tags. Display value is untouched."""
    local, _, domain = e.partition("@")
    local = local.split("+", 1)[0]
    if domain in GMAIL_DOMAINS:
        local = local.replace(".", "")
        domain = "gmail.com"
    return f"{local}@{domain}"


def phone(v, default_cc: str = "1") -> str | None:
    """Best-effort E.164. NANP-aware; other countries require a leading '+'.

    Limitation (documented): without libphonenumber we do not validate
    national numbering plans outside NANP.
    """
    s = clean_str(v)
    if not s:
        return None
    s = re.split(r"(?i)\s*(?:x|ext\.?|extension)\s*\d+$", s)[0]
    plus = s.startswith("+") or s.startswith("00")
    digits = re.sub(r"\D", "", s)
    if s.startswith("00"):
        digits = digits[2:]
    if plus:
        return f"+{digits}" if 8 <= len(digits) <= 15 else None
    if default_cc == "1":
        if len(digits) == 10 and digits[0] not in "01":
            return f"+1{digits}"
        if len(digits) == 11 and digits[0] == "1":
            return f"+{digits}"
        return None
    return f"+{default_cc}{digits.lstrip('0')}" if 6 <= len(digits) <= 13 else None


def name_part(v) -> str | None:
    s = clean_str(v)
    if not s:
        return None
    s = re.sub(r"[^\w\s'\-.]", "", s)
    return " ".join(w[:1].upper() + w[1:].lower() if w.isupper() or w.islower() else w for w in s.split())


def name_key(v: str | None) -> str | None:
    if not v:
        return None
    s = strip_accents(v).lower()
    s = re.sub(r"[^a-z]", "", s)
    return s or None


def first_name_key(v: str | None) -> str | None:
    k = name_key(v.split()[0] if v else None)
    return NICKNAMES.get(k, k) if k else None


def split_full_name(v) -> tuple[str | None, str | None, str | None]:
    """'Dr. Robert J. Smith Jr.' -> ('Robert', 'Smith', 'jr'); 'Smith, Robert' handled."""
    s = clean_str(v)
    if not s:
        return None, None, None
    if "," in s:
        last, _, rest = s.partition(",")
        rest_parts = rest.split()
        suffix = None
        if rest_parts and rest_parts[-1].lower() in SUFFIXES:
            suffix = SUFFIXES[rest_parts.pop().lower()]
        return name_part(rest_parts[0]) if rest_parts else None, name_part(last), suffix
    parts = [p for p in s.split() if p.lower().rstrip(".") not in {"mr", "mrs", "ms", "dr", "prof", "miss"}]
    suffix = None
    if parts and parts[-1].lower() in SUFFIXES:
        suffix = SUFFIXES[parts.pop().lower()]
    if not parts:
        return None, None, suffix
    if len(parts) == 1:
        return name_part(parts[0]), None, suffix
    return name_part(parts[0]), name_part(parts[-1]), suffix


def suffix(v) -> str | None:
    s = clean_str(v)
    return SUFFIXES.get(s.lower()) if s else None


_DATE_FORMATS = ["%Y-%m-%d", "%m/%d/%Y", "%m-%d-%Y", "%Y/%m/%d", "%d.%m.%Y", "%b %d %Y", "%B %d, %Y", "%Y%m%d"]


def date(v) -> str | None:
    s = clean_str(v)
    if not s:
        return None
    s = s.split("T")[0]
    for fmt in _DATE_FORMATS:
        try:
            d = dt.datetime.strptime(s, fmt).date()
        except ValueError:
            continue
        if 1900 <= d.year <= dt.date.today().year:
            return d.isoformat()
    return None


def timestamp(v, allow_unix: bool = True) -> str | None:
    """ISO-8601 or plausible Unix seconds -> 'YYYY-MM-DDTHH:MM:SSZ'.

    Callers examining arbitrary fields should disable Unix parsing unless the
    field name is timestamp-like. Keep epoch conversion within a practical
    range and avoid the platform C runtime's narrower ``fromtimestamp`` range.
    """
    s = clean_str(v)
    if not s:
        return None
    if allow_unix and re.fullmatch(r"\d{9,11}", s):
        seconds = int(s)
        if 0 <= seconds <= 4_102_444_800:  # 2100-01-01 UTC
            epoch = dt.datetime(1970, 1, 1, tzinfo=dt.timezone.utc)
            try:
                return (epoch + dt.timedelta(seconds=seconds)).strftime("%Y-%m-%dT%H:%M:%SZ")
            except (OverflowError, ValueError):
                return None
        return None
    try:
        d = dt.datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        dd = date(s)
        return f"{dd}T00:00:00Z" if dd else None
    if d.tzinfo is None:
        d = d.replace(tzinfo=dt.timezone.utc)
    return d.astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def postal(v, country: str | None = None) -> str | None:
    s = clean_str(v)
    if not s:
        return None
    s = s.upper()
    if (country or "US") in {"US", "USA"}:
        m = re.match(r"^(\d{5})(?:-?\d{4})?$", s)
        if m:
            return m.group(1)
        if re.fullmatch(r"\d{4}", s):  # leading zero dropped by spreadsheets
            return "0" + s
        return None
    return re.sub(r"\s+", " ", s)


_STREET_ABBR = {"street": "st", "avenue": "ave", "road": "rd", "drive": "dr", "boulevard": "blvd",
                "lane": "ln", "court": "ct", "place": "pl", "apartment": "apt", "suite": "ste",
                "north": "n", "south": "s", "east": "e", "west": "w"}


def address_key(line1: str | None) -> str | None:
    if not line1:
        return None
    s = strip_accents(line1).lower()
    s = re.sub(r"[^a-z0-9 ]", " ", s)
    toks = [_STREET_ABBR.get(t, t) for t in s.split()]
    return " ".join(toks) or None


def country(v) -> str | None:
    s = clean_str(v)
    if not s:
        return None
    u = s.upper()
    return {"USA": "US", "UNITED STATES": "US", "UNITED STATES OF AMERICA": "US", "U.S.": "US",
            "UK": "GB", "UNITED KINGDOM": "GB", "CANADA": "CA"}.get(u, u if len(u) == 2 else u)


def boolean(v) -> bool | None:
    if isinstance(v, bool):
        return v
    s = clean_str(v)
    if s is None:
        return None
    s = s.lower()
    if s in {"true", "t", "yes", "y", "1", "subscribed", "opted_in", "opt-in", "granted"}:
        return True
    if s in {"false", "f", "no", "n", "0", "unsubscribed", "opted_out", "opt-out", "denied", "not_subscribed"}:
        return False
    return None


# ---- string similarity (pure python, deterministic) ----

def jaro_winkler(a: str | None, b: str | None) -> float:
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    la, lb = len(a), len(b)
    window = max(la, lb) // 2 - 1
    am = [False] * la
    bm = [False] * lb
    matches = 0
    for i in range(la):
        lo, hi = max(0, i - window), min(i + window + 1, lb)
        for j in range(lo, hi):
            if not bm[j] and a[i] == b[j]:
                am[i] = bm[j] = True
                matches += 1
                break
    if not matches:
        return 0.0
    t = 0
    k = 0
    for i in range(la):
        if am[i]:
            while not bm[k]:
                k += 1
            if a[i] != b[k]:
                t += 1
            k += 1
    t /= 2
    jaro = (matches / la + matches / lb + (matches - t) / matches) / 3
    prefix = 0
    for x, y in zip(a[:4], b[:4]):
        if x != y:
            break
        prefix += 1
    return jaro + prefix * 0.1 * (1 - jaro)
