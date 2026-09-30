"""Synthetic multi-source customer universe with ground truth.

Every record carries `_truth` (person id) which the engine never sees — it is
stripped before rows reach connectors. Hard negatives are built in deliberately:
households (shared phone/address/email), father/son Jr/Sr pairs, twins, and
same-name strangers. Positives are made hard with nicknames, typos, gmail
dot/plus variants, work vs personal email, moves, and last-name changes.
"""
from __future__ import annotations

import random
from dataclasses import dataclass, field
from typing import Any

FIRST = ["James", "Robert", "John", "Michael", "David", "William", "Richard", "Joseph", "Thomas", "Christopher",
         "Charles", "Daniel", "Matthew", "Anthony", "Steven", "Andrew", "Edward", "Benjamin", "Samuel", "Nicholas",
         "Patrick", "Gregory", "Alexander", "Henry", "Lawrence", "Mary", "Patricia", "Jennifer", "Elizabeth",
         "Susan", "Jessica", "Margaret", "Katherine", "Deborah", "Rebecca", "Victoria", "Abigail", "Amanda",
         "Linda", "Barbara", "Sarah", "Karen", "Nancy", "Lisa", "Betty", "Sandra", "Ashley", "Emily", "Donna",
         "Michelle", "Carol", "Melissa", "Laura", "Olivia", "Sofia", "Maya", "Priya", "Wei", "Luis", "Carlos",
         "Ahmed", "Fatima", "Diego", "Mateo", "Noah", "Liam", "Ethan", "Aiden", "Chloe", "Zoe", "Hannah", "Grace",
         "Ivan", "Yuki", "Kenji", "Omar", "Aisha", "Tariq", "Nina", "Elena"]
LAST = ["Smith", "Johnson", "Williams", "Brown", "Jones", "Garcia", "Miller", "Davis", "Rodriguez", "Martinez",
        "Hernandez", "Lopez", "Gonzalez", "Wilson", "Anderson", "Thomas", "Taylor", "Moore", "Jackson", "Martin",
        "Lee", "Perez", "Thompson", "White", "Harris", "Sanchez", "Clark", "Ramirez", "Lewis", "Robinson",
        "Walker", "Young", "Allen", "King", "Wright", "Scott", "Torres", "Nguyen", "Hill", "Flores", "Green",
        "Adams", "Nelson", "Baker", "Hall", "Rivera", "Campbell", "Mitchell", "Carter", "Roberts", "Patel",
        "Kim", "Chen", "Singh", "Okafor", "Kowalski", "Novak", "Haddad", "Rossi", "Muller"]
STREETS = ["Main", "Oak", "Pine", "Maple", "Cedar", "Elm", "Washington", "Lake", "Hill", "Park", "Sunset",
           "Bay", "Gulf", "Palm", "Harbor", "Ridge", "Meadow", "Forest", "River", "Spring"]
SUFFIX_T = ["Street", "Avenue", "Road", "Drive", "Lane", "Court", "Boulevard"]
CITIES = [("Clearwater", "FL", "337"), ("Tampa", "FL", "336"), ("Austin", "TX", "787"), ("Denver", "CO", "802"),
          ("Portland", "OR", "972"), ("Columbus", "OH", "432"), ("Raleigh", "NC", "276"), ("Phoenix", "AZ", "850"),
          ("Boston", "MA", "021"), ("Seattle", "WA", "981")]
DOMAINS = ["gmail.com", "yahoo.com", "outlook.com", "icloud.com", "proton.me", "aol.com"]
NICK = {"Robert": ["Bob", "Rob", "Bobby"], "William": ["Bill", "Will"], "James": ["Jim", "Jimmy"],
        "Michael": ["Mike"], "David": ["Dave"], "Richard": ["Rick", "Rich"], "Joseph": ["Joe"],
        "Thomas": ["Tom"], "Christopher": ["Chris"], "Daniel": ["Dan", "Danny"], "Matthew": ["Matt"],
        "Anthony": ["Tony"], "Steven": ["Steve"], "Andrew": ["Andy", "Drew"], "Edward": ["Ed", "Eddie"],
        "Benjamin": ["Ben"], "Samuel": ["Sam"], "Nicholas": ["Nick"], "Alexander": ["Alex"],
        "Elizabeth": ["Liz", "Beth"], "Katherine": ["Kate", "Katie"], "Jennifer": ["Jen", "Jenny"],
        "Susan": ["Sue"], "Margaret": ["Maggie", "Peggy"], "Rebecca": ["Becky"], "Jessica": ["Jess"],
        "Deborah": ["Debbie"], "Patricia": ["Patty", "Trish"], "Victoria": ["Vicky"], "Abigail": ["Abby"],
        "Charles": ["Charlie", "Chuck"], "Gregory": ["Greg"], "Lawrence": ["Larry"], "Henry": ["Hank"]}


@dataclass
class Person:
    pid: str
    first: str
    last: str
    dob: str
    emails: list[str]
    mobile: str
    home_phone: str | None
    address: dict[str, str]
    old_address: dict[str, str] | None = None
    maiden: str | None = None
    suffix: str | None = None
    work_email: str | None = None
    tags: list[str] = field(default_factory=list)


class Gen:
    def __init__(self, seed: int = 7):
        self.r = random.Random(seed)
        self.used_emails: set[str] = set()
        self.used_phones: set[str] = set()
        self.n = 0

    def phone(self, area: str | None = None) -> str:
        while True:
            p = f"{area or self.r.choice(['727', '813', '512', '303', '503', '614', '919', '602', '617', '206'])}" \
                f"{self.r.randint(200, 999)}{self.r.randint(1000, 9999)}"
            if p not in self.used_phones:
                self.used_phones.add(p)
                return p

    def email(self, first: str, last: str) -> str:
        for _ in range(50):
            pat = self.r.choice(["{f}.{l}", "{f}{l}", "{fi}{l}", "{f}.{l}{n}", "{f}_{l}{n}", "{l}.{f}", "{f}{n}"])
            local = pat.format(f=first.lower(), l=last.lower(), fi=first[0].lower(), n=self.r.randint(1, 99))
            e = f"{local}@{self.r.choice(DOMAINS)}"
            if e not in self.used_emails:
                self.used_emails.add(e)
                return e
        raise RuntimeError("email space exhausted")

    def address(self) -> dict[str, str]:
        city, st, z3 = self.r.choice(CITIES)
        return {"line1": f"{self.r.randint(10, 9999)} {self.r.choice(STREETS)} {self.r.choice(SUFFIX_T)}",
                "city": city, "state": st, "zip": f"{z3}{self.r.randint(10, 99)}", "country": "US"}

    def dob(self, lo: int = 1945, hi: int = 2004) -> str:
        return f"{self.r.randint(lo, hi)}-{self.r.randint(1, 12):02d}-{self.r.randint(1, 28):02d}"

    def person(self, first=None, last=None, **kw) -> Person:
        self.n += 1
        first = first or self.r.choice(FIRST)
        last = last or self.r.choice(LAST)
        p = Person(pid=f"P{self.n:05d}", first=first, last=last, dob=kw.pop("dob", None) or self.dob(),
                   emails=[self.email(first, last)], mobile=self.phone(), home_phone=kw.pop("home_phone", None),
                   address=kw.pop("address", None) or self.address(), **kw)
        if self.r.random() < 0.3:
            p.work_email = f"{first[0].lower()}{last.lower()}{self.r.randint(1, 999)}@{self.r.choice(['acme', 'globex', 'initech', 'umbrella', 'hooli'])}.com"
            self.used_emails.add(p.work_email)
        if self.r.random() < 0.12:
            p.old_address = self.address()
        return p


def build_universe(n_people: int = 1500, seed: int = 7) -> list[Person]:
    g = Gen(seed)
    people: list[Person] = []
    while len(people) < n_people:
        roll = g.r.random()
        if roll < 0.20:  # household
            last, addr = g.r.choice(LAST), g.address()
            home = g.phone()
            members = [g.person(last=last, address=addr, home_phone=home) for _ in range(g.r.randint(2, 4))]
            firsts = set()
            for m in members:  # distinct given names within a household
                while m.first in firsts:
                    m.first = g.r.choice(FIRST)
                firsts.add(m.first)
            if g.r.random() < 0.3:  # shared family email
                fam = g.email("the", last + "family")
                for m in members:
                    m.emails.append(fam)
            for m in members:
                m.tags.append("household")
            people += members
        elif roll < 0.24:  # father/son Jr/Sr
            first, last, addr = g.r.choice(FIRST[:25]), g.r.choice(LAST), g.address()
            home = g.phone()
            sr = g.person(first=first, last=last, address=addr, home_phone=home, dob=g.dob(1945, 1965), suffix="Sr")
            jr = g.person(first=first, last=last, address=addr, home_phone=home, dob=g.dob(1975, 1995), suffix="Jr")
            sr.tags.append("jrsr")
            jr.tags.append("jrsr")
            people += [sr, jr]
        elif roll < 0.25:  # twins
            last, addr, dob = g.r.choice(LAST), g.address(), g.dob(1980, 2004)
            a = g.person(last=last, address=addr, dob=dob)
            b = g.person(last=last, address=addr, dob=dob)
            while b.first == a.first:
                b.first = g.r.choice(FIRST)
            a.tags.append("twin")
            b.tags.append("twin")
            people += [a, b]
        else:
            p = g.person()
            if g.r.random() < 0.04:
                p.maiden = g.r.choice(LAST)
                p.tags.append("name_change")
            people.append(p)
    return people[:n_people]


# ------------------------------------------------------------------ record noise

class Noiser:
    def __init__(self, seed: int):
        self.r = random.Random(seed)

    def typo(self, s: str) -> str:
        if len(s) < 4:
            return s
        i = self.r.randint(1, len(s) - 2)
        op = self.r.choice(["swap", "drop", "dup"])
        if op == "swap":
            return s[:i] + s[i + 1] + s[i] + s[i + 2:]
        if op == "drop":
            return s[:i] + s[i + 1:]
        return s[:i] + s[i] + s[i:]

    def first(self, p: Person) -> str:
        f = p.first
        if f in NICK and self.r.random() < 0.18:
            f = self.r.choice(NICK[f])
        if self.r.random() < 0.04:
            f = self.typo(f)
        return f.upper() if self.r.random() < 0.05 else f

    def last(self, p: Person, old: bool) -> str:
        ln = p.maiden if (old and p.maiden) else p.last
        if self.r.random() < 0.04:
            ln = self.typo(ln)
        return ln.upper() if self.r.random() < 0.05 else ln

    def email(self, p: Person, allow_work: bool = True) -> str | None:
        opts = list(p.emails) + ([p.work_email] if allow_work and p.work_email else [])
        e = self.r.choice(opts)
        local, dom = e.split("@")
        if dom == "gmail.com" and self.r.random() < 0.25:
            local = self.r.choice([local.replace(".", ""), local + "+shop", ".".join(local)[:40] if "." not in local else local])
        if self.r.random() < 0.08:
            local = local.upper()
        return f"{local}@{dom}"

    def phone(self, p: Person) -> str | None:
        num = p.mobile if (not p.home_phone or self.r.random() < 0.65) else p.home_phone
        fmt = self.r.choice(["({a}) {b}-{c}", "{a}-{b}-{c}", "{a}.{b}.{c}", "+1 {a} {b} {c}", "1{a}{b}{c}", "{a}{b}{c}"])
        return fmt.format(a=num[:3], b=num[3:6], c=num[6:])

    def addr(self, p: Person, old: bool) -> dict[str, str]:
        a = dict(p.old_address if (old and p.old_address) else p.address)
        if self.r.random() < 0.3:
            for full, ab in (("Street", "St"), ("Avenue", "Ave"), ("Road", "Rd"), ("Drive", "Dr"), ("Lane", "Ln"),
                             ("Court", "Ct"), ("Boulevard", "Blvd")):
                a["line1"] = a["line1"].replace(full, ab)
        if self.r.random() < 0.1:
            a["line1"] = a["line1"].upper()
        if self.r.random() < 0.1:
            a["zip"] = a["zip"] + f"-{self.r.randint(1000, 9999)}"
        return a

    def ts(self, lo_year=2023, hi_year=2026) -> str:
        y = self.r.randint(lo_year, hi_year)
        return f"{y}-{self.r.randint(1, 8 if y == 2026 else 12):02d}-{self.r.randint(1, 28):02d}T" \
               f"{self.r.randint(0, 23):02d}:{self.r.randint(0, 59):02d}:{self.r.randint(0, 59):02d}Z"


def _sh(*parts) -> int:
    import hashlib
    return int(hashlib.sha256("|".join(map(str, parts)).encode()).hexdigest()[:15], 16)


def _sfx(p: Person, r: random.Random) -> str | None:
    return p.suffix if p.suffix and r.random() < 0.5 else None


def build_sources(people: list[Person], seed: int = 11) -> dict[str, list[dict[str, Any]]]:
    nz = Noiser(seed)
    r = nz.r
    src: dict[str, list[dict[str, Any]]] = {k: [] for k in ("salesforce", "hubspot", "shopify", "stripe", "legacy_csv", "segment")}
    presence = {"salesforce": 0.55, "hubspot": 0.5, "shopify": 0.45, "stripe": 0.3, "legacy_csv": 0.25, "segment": 0.35}
    consent_truth: dict[str, list[tuple[str, bool]]] = {}
    for p in people:
        chosen = [s for s, pr in presence.items() if r.random() < pr] or [r.choice(list(presence))]
        for s in chosen:
            copies = 2 if (s == "hubspot" and r.random() < 0.06) else 1
            for c in range(copies):
                old = r.random() < 0.3
                ts = nz.ts(2023, 2024) if old else nz.ts(2025, 2026)
                rec = _render(s, p, nz, old, ts, c)
                if rec is None:
                    continue
                granted = rec.pop("_consent", None)
                if granted is not None:
                    consent_truth.setdefault(p.pid, []).append((ts, granted))
                rec["_truth"] = p.pid
                src[s].append(rec)
    for s in src:
        r.shuffle(src[s])
    src["_consent_truth"] = consent_truth  # type: ignore[assignment]
    return src


def _render(s: str, p: Person, nz: Noiser, old: bool, ts: str, copy: int) -> dict[str, Any] | None:
    r = nz.r
    a = nz.addr(p, old)
    fn, ln = nz.first(p), nz.last(p, old)
    consent = r.random() < 0.55
    if s == "salesforce":
        return {"Id": f"003{_sh(p.pid, copy, s) % 10**12:012d}", "attributes": {"type": "Contact"},
                "FirstName": fn, "LastName": ln, "Suffix": _sfx(p, r),
                "Email": nz.email(p) if r.random() < 0.9 else None,
                "Phone": nz.phone(p) if r.random() < 0.7 else None,
                "MobilePhone": None, "Birthdate": p.dob if r.random() < 0.5 else None,
                "MailingStreet": a["line1"], "MailingCity": a["city"], "MailingState": a["state"],
                "MailingPostalCode": a["zip"], "MailingCountry": "United States", "Title": None,
                "HasOptedOutOfEmail": not consent, "SystemModstamp": ts.replace("Z", ".000+0000"), "_consent": consent}
    if s == "hubspot":
        return {"id": f"{_sh(p.pid, copy, s) % 10**9}", "properties": {
            "email": nz.email(p), "firstname": fn, "lastname": ln,
            "phone": nz.phone(p) if r.random() < 0.5 else None, "mobilephone": None,
            "address": a["line1"] if r.random() < 0.6 else None, "city": a["city"], "state": a["state"],
            "zip": a["zip"] if r.random() < 0.8 else None, "country": "US", "company": None, "jobtitle": None,
            "date_of_birth": p.dob if r.random() < 0.2 else None,
            "hs_email_optout": "false" if consent else "true", "lastmodifieddate": ts}, "_consent": consent}
    if s == "shopify":
        state = "subscribed" if consent else r.choice(["not_subscribed", "unsubscribed"])
        placeholder = r.random() < 0.03  # POS clerks typing a dummy address: a real-world placeholder identifier
        return {"id": _sh(p.pid, copy, s) % 10**13,
                "email": "noemail@acme-store.com" if placeholder else nz.email(p, allow_work=False),
                "first_name": fn, "last_name": ln, "phone": ("+1" + p.mobile) if r.random() < 0.4 else None,
                "created_at": ts, "updated_at": ts, "state": "enabled", "verified_email": True, "tags": "",
                "email_marketing_consent": {"state": state, "opt_in_level": "single_opt_in", "consent_updated_at": ts},
                "default_address": {"address1": a["line1"], "address2": None, "city": a["city"],
                                    "province": a["state"], "zip": a["zip"], "country": "United States",
                                    "country_code": "US"}, "_consent": consent}
    if s == "stripe":
        name = f"{fn} {ln}" + (f" {p.suffix}" if p.suffix and r.random() < 0.3 else "")
        return {"id": f"cus_{_sh(p.pid, copy, s) % 10**14:014d}", "object": "customer",
                "email": nz.email(p), "name": name, "phone": nz.phone(p) if r.random() < 0.5 else None,
                "address": {"line1": a["line1"], "line2": None, "city": a["city"], "state": a["state"],
                            "postal_code": a["zip"], "country": "US"} if r.random() < 0.7 else None,
                "created": 1700000000 + r.randint(0, 60_000_000), "livemode": False, "metadata": {}}
    if s == "legacy_csv":
        y, m, d = p.dob.split("-")
        return {"Customer #": f"L{_sh(p.pid, copy, s) % 10**7:07d}",
                "Client Name": f"{ln}, {fn}" + (f" {p.suffix}" if p.suffix and r.random() < 0.5 else ""),
                "E-Mail Addr": nz.email(p) if r.random() < 0.7 else "",
                "Tel": nz.phone(p) if r.random() < 0.8 else "",
                "DOB (mm/dd/yyyy)": f"{m}/{d}/{y}" if r.random() < 0.7 else "",
                "Street": a["line1"], "Zip": a["zip"][:5],
                "Last Order": f"{r.randint(1, 12):02d}/{r.randint(1, 28):02d}/{r.randint(2019, 2025)}",
                "Contact Pref": r.choice(["Email", "Phone", "Mail", ""]),
                "Lifetime Value": f"{r.randint(10, 9000)}.{r.randint(0, 99):02d}"}
    if s == "segment":
        return {"type": "identify", "userId": f"u_{_sh(p.pid, copy, s) % 10**10}", "timestamp": ts,
                "traits": {"email": nz.email(p), "firstName": fn, "lastName": ln,
                           "phone": nz.phone(p) if r.random() < 0.3 else None,
                           "address": {"postalCode": a["zip"], "city": a["city"]} if r.random() < 0.6 else None,
                           "birthday": p.dob if r.random() < 0.15 else None,
                           "plan": r.choice(["free", "pro", "team"])}}
    return None


# Ground-truth mapping of identity fields, used for the mapping-accuracy gate.
MAPPING_TRUTH = {
    "salesforce": {"FirstName": "first_name", "LastName": "last_name", "Email": "email", "Phone": "phone",
                   "Birthdate": "birth_date", "MailingStreet": "address_line1", "MailingPostalCode": "postal_code"},
    "hubspot": {"email": "email", "firstname": "first_name", "lastname": "last_name", "phone": "phone",
                "address": "address_line1", "zip": "postal_code", "date_of_birth": "birth_date"},
    "shopify": {"email": "email", "first_name": "first_name", "last_name": "last_name", "phone": "phone",
                "default_address.address1": "address_line1", "default_address.zip": "postal_code"},
    "stripe": {"email": "email", "name": "full_name", "phone": "phone", "address.line1": "address_line1",
               "address.postal_code": "postal_code"},
    "legacy_csv": {"Client Name": "full_name", "E-Mail Addr": "email", "Tel": "phone",
                   "DOB (mm/dd/yyyy)": "birth_date", "Street": "address_line1", "Zip": "postal_code",
                   "Last Order": None},
    "segment": {"traits.email": "email", "traits.firstName": "first_name", "traits.lastName": "last_name",
                "traits.phone": "phone", "traits.address.postalCode": "postal_code", "traits.birthday": "birth_date"},
}
