"""Fixture-backed "backend": identity checks, claim lookups, guideline retrieval.

Pure code, no LLM. Everything the SOP depends on for correctness lives here so it
is deterministic and unit-testable.
"""
import json
import os
import re
from datetime import date, datetime
from pathlib import Path

FIXTURES = Path(os.getenv("FIXTURES_DIR", Path(__file__).resolve().parent.parent / "fixtures"))


def _load(name):
    return json.loads((FIXTURES / name).read_text())


POLICYHOLDERS = _load("policyholders.json")
CLAIMS = _load("claims.json")
REPRESENTATIVES = _load("representatives.json")
CONSENT_SCENARIOS = _load("consent_scenarios.json")
GUIDE = _load("required_document_guideline.json")
CLAIM_SCHEMA = _load("claim_schema.json")

ID_FIELDS = ("name", "dob", "phone", "email", "id_last4")  # the 5 PII fields; any 3 must match
MIN_MATCHES = 3
FIELD_LABELS = {
    "name": "full name",
    "dob": "date of birth",
    "phone": "phone number",
    "email": "email address",
    "id_last4": "last 4 digits of SSN / national ID",
}
MONEY_FIELDS = ("expected_reimbursement_amount", "allowed_max_amount", "net_pay", "net_fee")


def today():
    # Fixture deadlines are in early 2026; a fixed demo date keeps the scenario coherent.
    return date.fromisoformat(os.getenv("DEMO_TODAY", "2026-03-05"))


# ---------- identity ----------

def name_tokens(s):
    return re.sub(r"[^a-z ]", " ", s.lower()).split()


def norm_name(s):
    return "".join(name_tokens(s))  # spacing-insensitive: "Yawen Li" == "Ya Wen Li"


def name_keys(n):
    t = name_tokens(n)
    return {"".join(t), "".join(t[-1:] + t[:-1])}  # given-name-first or family-name-first


def is_partial_name(new, old):
    return set(name_tokens(new)) < set(name_tokens(old))


def norm_dob(s):
    s = re.sub(r"(\d)(st|nd|rd|th)\b", r"\1", s.strip().replace(",", " "))
    s = " ".join(s.split())
    for fmt in ("%Y-%m-%d", "%Y/%m/%d", "%m/%d/%Y", "%m-%d-%Y", "%B %d %Y", "%b %d %Y", "%d %B %Y", "%d %b %Y"):
        try:
            return datetime.strptime(s, fmt).date().isoformat()
        except ValueError:
            pass
    return s


def norm_phone(s):
    return re.sub(r"\D", "", s)[-10:]


def norm_last4(s):
    return re.sub(r"\D", "", s)[-4:]


NORMALIZE = {
    "name": norm_name,
    "dob": norm_dob,
    "phone": norm_phone,
    "email": lambda s: s.strip().lower(),
    "id_last4": norm_last4,
    "policy_number": lambda s: re.sub(r"\D", "", s),  # "9921" == "POL-9921"
}


def _record_values(rec, field):
    if field == "name":
        return set().union(*(name_keys(n) for n in [rec["name"], *rec.get("name_aliases", [])]))
    if field == "phone":
        return {norm_phone(p) for p in [rec["phone"], *rec.get("phone_aliases", [])]}
    if field == "email":
        return {e.lower() for e in [rec["email"], *rec.get("email_aliases", [])]}
    if field == "policy_number":
        return {NORMALIZE["policy_number"](rec["policy_number"])}
    return {rec[field]}


def provided_fields(claimed):
    """ID fields that count toward verification. A first name alone neither verifies nor contradicts."""
    return [f for f in ID_FIELDS if claimed.get(f) and (f != "name" or len(name_tokens(claimed[f])) > 1)]


def verify_identity(claimed):
    """Match the caller's claimed fields against every record.

    Verified = at least MIN_MATCHES of the 5 PII fields match one record and no
    provided field (policy number included) contradicts it. The result never
    says *which* field failed, so callers cannot leak it.
    """
    usable = set(provided_fields(claimed)) | {"policy_number"}
    given = {f: NORMALIZE[f](v) for f, v in claimed.items() if v and f in usable}
    best = {"party_id": None, "matched": 0, "mismatch": False}
    for rec in POLICYHOLDERS:
        matched = sum(1 for f in ID_FIELDS if f in given and given[f] in _record_values(rec, f))
        mismatch = any(given[f] not in _record_values(rec, f) for f in given)
        if matched > best["matched"]:
            best = {"party_id": rec["party_id"], "matched": matched, "mismatch": mismatch}
    best["verified"] = best["matched"] >= MIN_MATCHES and not best["mismatch"]
    return best


def policyholder(party_id):
    return next(p for p in POLICYHOLDERS if p["party_id"] == party_id)


def mask_email(email):
    user, domain = email.split("@")
    return f"{user[0]}{'•' * max(len(user) - 1, 3)}@{domain}"  # not '*': models escape it as markdown


def mask_phone(phone):
    return f"***-***-{phone[-4:]}"


def find_representative(rep_name, party_id):
    return next(
        (r for r in REPRESENTATIVES
         if r["buyer_party_id"] == party_id and rep_name and norm_name(rep_name) in name_keys(r["rep_name"])),
        None,
    )


def consent_status(scenario, checks):
    """Status after `checks` polls; past the end of the sequence a pending request has timed out."""
    seq = CONSENT_SCENARIOS[scenario]["status_sequence"]
    if checks < len(seq):
        return seq[checks]
    return "approved" if seq[-1] == "approved" else "timeout"


# ---------- claims ----------

def claims_for(party_id):
    return [c for c in CLAIMS if c["party_id"] == party_id]


def match_claims(claims, hints):
    """Narrow claims by whatever the caller said (case id, type, status, month, year)."""
    if hints.get("case_id"):
        return [c for c in claims if c["case_id"].upper() == hints["case_id"].upper()]
    tests = {
        "case_type": lambda c, v: c["case_type"] == v,
        "status": lambda c, v: c["status"] == v,
        "month": lambda c, v: int(c["created_at"][5:7]) == v,
        "year": lambda c, v: int(c["created_at"][:4]) == v,
    }
    for key, test in tests.items():
        if hints.get(key):
            claims = [c for c in claims if test(c, hints[key])]
    return claims


def claim_brief(c):
    """Claim list entry; only ever shown after verification."""
    brief = {k: c[k] for k in ("case_id", "case_type", "status", "created_at", "summary")}
    return brief | {k: f"${float(c[k]):,.2f}" for k in MONEY_FIELDS}


def doc_guidance(doc):
    # Claims say "pathology report", guidelines say "original pathology report": match by containment.
    key = next((k for k in GUIDE["document_guidance"] if doc in k or k in doc), None)
    alt = GUIDE["document_alternative_guidance"].get(key) or GUIDE["document_alternative_guidance"]["default"]
    return {
        "document": doc,
        "requirements": GUIDE["document_guidance"][key]["en"] if key else GUIDE["default_guidance"]["en"],
        "if_unavailable": alt["en"],
    }


def followup_guidance(claim, text):
    """All follow-up rules that apply to this claim; keyword hits on the caller's words are flagged."""
    docs = claim.get("documents_needed") or []
    fill = {
        "case_id": claim["case_id"],
        "documents": " and ".join(docs),
        "average_processing_time_after_submission":
            GUIDE["claim_followup_settings"]["average_processing_time_after_submission"]["en"],
    }
    text = text.lower()
    out = []
    for g in GUIDE["claim_followup_guidance"]:
        if g.get("requires_documents") and not docs:
            continue
        out.append({
            "topic": g["topic"],
            "matches_caller_wording": any(k in text for k in g.get("match_any", [])),
            "text": g["en"].format(**fill),
        })
    return out


def claim_facts(claim, caller_text=""):
    """Everything the agent may say about one claim — the only claim data the responder ever sees."""
    facts = {k: v for k, v in claim.items() if k != "party_id"}
    for k in MONEY_FIELDS:
        facts[k] = f"${float(claim[k]):,.2f}"
    if claim.get("appeal_deadline"):
        days = (date.fromisoformat(claim["appeal_deadline"]) - today()).days
        facts["appeal_deadline_status"] = (
            f"{days} days remaining (today is {today()})" if days >= 0 else f"deadline passed {-days} days ago"
        )
    facts["documents_guidance"] = [doc_guidance(d) for d in claim.get("documents_needed", [])]
    facts["case_type_guidance"] = GUIDE["case_type_guidance"].get(claim["case_type"], {}).get("en")
    facts["general_submission_guidance"] = GUIDE["default_guidance"]["en"]
    facts["followup_guidance"] = followup_guidance(claim, caller_text)
    facts["followup_fallback"] = GUIDE["claim_followup_fallback"]["en"]
    facts["human_review_rule"] = GUIDE["claim_followup_settings"]["human_review_after_document_alternatives_exhausted"]["en"]
    facts["field_definitions"] = {k: v["description"] for k, v in CLAIM_SCHEMA["field_descriptions"].items()}
    return facts


# ---------- output guard ----------

def _claim_tokens(c):
    toks = {c["case_id"].lower(), c["created_at"]}
    if c.get("appeal_deadline"):
        toks.add(c["appeal_deadline"])
    toks |= {d.lower() for d in c.get("documents_needed", [])}
    for k in MONEY_FIELDS:
        if float(c[k]):
            toks.add(f"{float(c[k]):.0f}")  # matched against digits with $ and commas stripped
    return toks


def leaked_claim_data(text, allowed_party=None):
    """Claim-specific tokens in `text` that belong to anyone other than `allowed_party`."""
    plain = re.sub(r"[$,]", "", text.lower())
    hits = set()
    for c in CLAIMS:
        if c["party_id"] == allowed_party:
            continue
        for t in _claim_tokens(c):
            if re.search(rf"(?<![\w-]){re.escape(t)}(?:\.00)?(?![\w-])", plain):
                hits.add(t)
    return sorted(hits)
