"""Deterministic reference agent.

Serves three roles: (1) the default offline proposer, (2) the fallback when an
LLM proposal is rejected or the agent budget is exhausted, and (3) the
consensus baseline an LLM mapping is cross-checked against.
"""
from __future__ import annotations

import re
from typing import Any

SYNONYMS: dict[str, tuple[str, str]] = {}


def _syn(target: str, names: list[str], transform: str = "default") -> None:
    for n in names:
        SYNONYMS[n] = (target, transform)


_syn("email", ["email", "emailaddress", "emailaddr", "mail", "primaryemail", "contactemail", "workemail",
               "personalemail", "useremail", "traitsemail", "customeremail"])
_syn("phone", ["phone", "phonenumber", "tel", "telephone", "mobile", "mobilephone", "cell", "cellphone",
               "homephone", "workphone", "phone1", "phone2", "traitsphone", "contactphone", "mobilenumber"])
_syn("first_name", ["firstname", "fname", "givenname", "first", "forename", "traitsfirstname"])
_syn("last_name", ["lastname", "lname", "surname", "familyname", "last", "traitslastname"])
_syn("full_name", ["name", "fullname", "customername", "contactname", "displayname", "traitsname", "clientname"],
     "split_full_name")
_syn("name_suffix", ["suffix", "namesuffix", "generation"])
_syn("birth_date", ["dob", "birthdate", "dateofbirth", "birthday", "traitsbirthday", "bday"])
_syn("address_line1", ["address", "address1", "street", "streetaddress", "mailingstreet", "addr", "line1",
                       "addressline1", "street1", "billingaddressline1", "traitsaddressstreet"])
_syn("address_line2", ["address2", "line2", "apt", "suite", "addressline2", "street2"])
_syn("city", ["city", "town", "mailingcity", "traitsaddresscity", "billingaddresscity"])
_syn("region", ["state", "province", "region", "provincecode", "mailingstate", "statecode", "traitsaddressstate",
                "billingaddressstate"])
_syn("postal_code", ["zip", "zipcode", "postalcode", "postcode", "mailingpostalcode", "zip5",
                     "traitsaddresspostalcode", "billingaddresspostalcode"])
_syn("country", ["country", "countrycode", "mailingcountry", "traitsaddresscountry", "billingaddresscountry"])
_syn("company", ["company", "companyname", "employer", "organization", "organisation", "accountname"])
_syn("job_title", ["title", "jobtitle", "position"])
_syn("updated_at", ["updatedat", "lastmodifieddate", "systemmodstamp", "lastmodified", "modified",
                    "hslastmodifieddate", "updated", "modifiedat", "lastupdated", "timestamp"])
_syn("created_at", ["createdat", "createddate", "created", "createdate"])
_syn("consent.email_marketing", ["emailoptin", "acceptsmarketing", "marketingoptin", "newsletter",
                                 "emailmarketingconsentstate", "subscribed", "emailsubscribed"])
_syn("consent.email_marketing", ["hasoptedoutofemail", "hsemailoptout", "emailoptout", "unsubscribed"],
     "invert_bool")
_syn("consent.sms_marketing", ["smsoptin", "smsmarketingconsentstate", "smssubscribed"])
_syn("consent.sms_marketing", ["smsoptout"], "invert_bool")
_syn("consent.analytics", ["analyticsconsent", "trackingconsent"])

IGNORE = {"id", "uuid", "objectid", "attributestype", "attributesurl", "archived", "object", "type",
          "messageid", "anonymousid", "userid", "vid", "hsobjectid", "adminpgraphqlapiid", "livemode",
          "currency", "balance", "delinquent", "invoiceprefix", "state", "note", "tags", "verifiedemail",
          "properties", "hsobjectsource"}


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", s.lower())


class DeterministicAgent:
    name = "deterministic/v1"

    def propose_mapping(self, task: dict[str, Any]) -> dict[str, Any]:
        out: dict[str, Any] = {}
        used_single: set[str] = set()
        for f in task["fields"]:
            name = f["name"]
            full, last = _norm(name), _norm(name.split(".")[-1])
            valid = f.get("stats", {}).get("valid_rate", {})
            hit = SYNONYMS.get(full) or SYNONYMS.get(last)
            # "state" is ambiguous (Shopify customer state = enabled/disabled). Require validity.
            if hit and full not in IGNORE:
                target, transform = hit
                vr = valid.get(target, 0.0)
                if target in used_single and target not in {"email", "phone"}:
                    out[name] = {"target": "attribute", "transform": "default", "confidence": 0.7}
                    continue
                if vr >= 0.9 or target in {"company", "job_title"}:
                    out[name] = {"target": target, "transform": transform, "confidence": 0.95}
                    used_single.add(target)
                else:
                    out[name] = {"target": None, "transform": "default", "confidence": 0.6}
                continue
            if last in IGNORE or full in IGNORE or f.get("stats", {}).get("non_null_rate", 0) == 0:
                out[name] = {"target": None, "transform": "default", "confidence": 0.9}
                continue
            # Substring cues, accepted only when the values validate for that target.
            cue = None
            for sub, tgt in (("birth", "birth_date"), ("dob", "birth_date"), ("email", "email"), ("mail", "email"),
                             ("phone", "phone"), ("tel", "phone"), ("mobile", "phone"), ("zip", "postal_code"),
                             ("postal", "postal_code"), ("surname", "last_name")):
                if sub in full and valid.get(tgt, 0) >= 0.9 and tgt not in used_single:
                    cue = tgt
                    break
            if cue:
                out[name] = {"target": cue, "transform": "default", "confidence": 0.85}
                if cue not in {"email", "phone"}:
                    used_single.add(cue)
                continue
            # Shape-based fallback for unknown headers.
            if valid.get("email", 0) >= 0.95:
                out[name] = {"target": "email", "transform": "default", "confidence": 0.85}
            elif valid.get("phone", 0) >= 0.95 and valid.get("birth_date", 0) < 0.5:
                out[name] = {"target": "phone", "transform": "default", "confidence": 0.8}
            else:
                out[name] = {"target": "attribute", "transform": "default", "confidence": 0.8}
        return {"fields": out}

    def adjudicate(self, task: dict[str, Any]) -> dict[str, Any]:
        c = task["comparisons"]
        st = {k: v["status"] for k, v in c.items()}
        ev = lambda *fs: [{"field": f, "status": st[f]} for f in fs if f in st]  # noqa: E731
        name_ok = st.get("first_name") in {"agree", "partial"} and st.get("last_name") in {"agree", "partial"}
        exact_name = st.get("first_name") == "agree" and st.get("last_name") == "agree"
        if "conflict" in st.values():
            return {"decision": "no_match", "confidence": 0.99, "evidence": ev(*[k for k, v in st.items() if v == "conflict"]),
                    "rationale": "hard attribute conflict"}
        if st.get("first_name") == "disagree":
            return {"decision": "no_match", "confidence": 0.9, "evidence": ev("first_name"),
                    "rationale": "different given names"}
        shared_contact = [f for f in ("email", "phone") if st.get(f) == "shared"]
        name_agrees = (st.get("first_name") in {"agree", "partial"}
                       and st.get("last_name") in {"agree", "partial"}
                       and (st.get("first_name") == "agree" or st.get("last_name") == "agree"))
        postal_city = (st.get("postal_code") == "agree" and st.get("city") == "agree"
                       and st.get("address") != "disagree")
        street_locality = (st.get("address") == "agree"
                           and (st.get("postal_code") == "agree" or st.get("city") == "agree")
                           and st.get("postal_code") != "disagree" and st.get("city") != "disagree")
        if (name_agrees and st.get("name_suffix") != "partial" and (postal_city or street_locality)
                and shared_contact and not (st.get("email") == "disagree" and st.get("phone") == "disagree")):
            location = (["postal_code", "city"] if postal_city else ["address"] +
                        (["city"] if st.get("city") == "agree" else ["postal_code"]))
            return {"decision": "match", "confidence": 0.89,
                    "evidence": ev("first_name", "last_name", *location, *shared_contact),
                    "rationale": "name, locality, and a shared contact corroborate identity"}
        if name_ok and st.get("birth_date") == "agree":
            if (st.get("email") != "agree" and st.get("phone") != "agree"
                    and st.get("last_name") != "agree"):
                return {"decision": "abstain", "confidence": 0.5, "evidence": [],
                        "rationale": "DOB without an exact surname is insufficient for an automatic match"}
            return {"decision": "match", "confidence": 0.95, "evidence": ev("first_name", "last_name", "birth_date"),
                    "rationale": "name and date of birth agree"}
        if name_ok and (st.get("email") == "agree" or st.get("phone") == "agree"):
            f = "email" if st.get("email") == "agree" else "phone"
            return {"decision": "match", "confidence": 0.93, "evidence": ev("first_name", "last_name", f),
                    "rationale": f"name and {f} agree"}
        if (name_ok and st.get("address") == "agree" and st.get("postal_code") == "agree"
                and st.get("first_name") == "agree" and st.get("last_name") == "agree"
                and not (st.get("email") == "disagree" and st.get("phone") == "disagree")):
            return {"decision": "match", "confidence": 0.88,
                    "evidence": ev("first_name", "last_name", "address", "postal_code"),
                    "rationale": "exact name at the same street address"}
        if (st.get("first_name") == "agree" and st.get("last_name") == "agree" and st.get("postal_code") == "agree"
                and st.get("name_frequency") == "rare"
                and not (st.get("email") == "disagree" and st.get("phone") == "disagree")):
            return {"decision": "match", "confidence": 0.87,
                    "evidence": ev("first_name", "last_name", "postal_code", "name_frequency"),
                    "rationale": "exact name, unique to this locality across the dataset, same postal code"}
        if st.get("last_name") == "disagree" and st.get("email") != "agree" and st.get("phone") != "agree" \
                and st.get("birth_date") != "agree":
            return {"decision": "no_match", "confidence": 0.9, "evidence": ev("last_name"),
                    "rationale": "different family names and no strong identifier"}
        if name_ok and st.get("postal_code") == "disagree" and st.get("address") != "agree":
            return {"decision": "no_match", "confidence": 0.86, "evidence": ev("postal_code"),
                    "rationale": "same name in a different locality with no shared identifier"}
        return {"decision": "abstain", "confidence": 0.5, "evidence": [], "rationale": "insufficient evidence"}
