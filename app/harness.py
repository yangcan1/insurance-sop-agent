"""SOP harness: the code that owns phase order, gates, memory, and escalation.

Every turn:  extract (LLM) -> apply (code) -> respond (LLM) -> guard (code).
The responder only ever sees the data the current phase allows, so the VERIFY_ID gate
holds even if the model is jailbroken: there is nothing in its context to leak.
"""
import json
import uuid
from dataclasses import asdict, dataclass, field

from . import data
from .data import FIELD_LABELS, ID_FIELDS, MIN_MATCHES

PHASES = ("VERIFY_ID", "RESOLVE_INTENT", "PROCESS_CASE", "POST_PROCESS")
TERMINAL = ("HUMAN_HANDOFF", "ENDED")
MAX_FAILED_VERIFICATIONS = 3
OFF_TOPIC_LIMIT = 3
FRUSTRATION_LIMIT = 3

GREETING = ("Hi, thanks for contacting claims support. I can help with questions about your policy and claims. "
            "To get started, could you tell me your full name and what you're calling about today?")
SAFE_UNVERIFIED_REPLY = (
    "I want to help with that, but I can't share any claim or account details until I've verified your identity. "
    "Could you give me at least three of these: your full name, date of birth, phone number, email on file, "
    "or the last four digits of your SSN?")
EMOTION_NOTES = {
    "frustrated": "Caller is frustrated. Start with one short, genuine acknowledgment, then explain why this step matters, then give the concrete options.",
    "angry": "Caller is angry. Stay calm and respectful; acknowledge briefly, don't argue or over-apologize, explain why this step matters, offer the options including a human representative.",
    "anxious": "Caller sounds anxious. Reassure them briefly and make the next step clear and simple.",
    "confused": "Caller seems confused. Slow down, explain in plain words, and ask one thing at a time.",
    "sad": "Caller sounds upset. Acknowledge it with empathy before continuing.",
}


@dataclass
class Session:
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    consent_scenario: str = "default"
    phase: str = "VERIFY_ID"
    history: list = field(default_factory=list)       # [{"role", "content"}]
    # memory: anything the caller says is kept, whatever phase it belongs to
    claimed: dict = field(default_factory=dict)       # identity fields as stated by the caller
    declined: list = field(default_factory=list)
    caller_role: str = "unknown"
    rep_name: str = None
    relationship: str = None
    case_hints: dict = field(default_factory=dict)    # case_id / case_type / status / month / year
    intent: str = None
    questions: list = field(default_factory=list)
    # gates
    party_id: str = None                              # set only once fully authorized
    failed_attempts: int = 0
    last_failed: str = None
    pending_party: str = None                         # representative verified, waiting on consent
    consent: str = None                               # pending / approved / timeout
    consent_checks: int = 0
    # case work
    case_id: str = None
    discussed: list = field(default_factory=list)
    # conversation health
    off_topic: int = 0
    frustration: int = 0
    emotion: str = "calm"
    # outcomes
    email: dict = None
    email_choice: str = None
    email_offered: bool = False
    handoff: dict = None
    trace: list = field(default_factory=list)

    def __post_init__(self):
        if not self.history:
            self.history.append({"role": "assistant", "content": GREETING})


def turn(s, text, llm):
    events = []
    s.history.append({"role": "user", "content": text})
    if s.phase in TERMINAL:
        reply = ("You've been transferred to a human representative, who will pick this up shortly."
                 if s.phase == "HUMAN_HANDOFF" else
                 "This conversation has ended. Please start a new conversation if you need more help.")
    else:
        x = llm.extract(extract_context(s, text))
        events.append({"type": "extract", "data": x.model_dump(exclude_defaults=True)})
        phase_before = s.phase
        notes = apply(s, x, text, llm, events)
        if s.phase != phase_before:
            events.append({"type": "transition", "data": f"{phase_before} -> {s.phase}"})
        prompt = harness_state(s, notes, x, text)
        events.append({"type": "directive", "data": notes})
        reply = guard(s, llm.respond(prompt, llm_messages(s)), events)
    s.history.append({"role": "assistant", "content": reply})
    s.trace.append({"turn": sum(m["role"] == "user" for m in s.history), "phase": s.phase, "events": events})
    return reply


# ---------- memory ----------

def remember(s, x, events):
    fields = {"name": x.full_name, "dob": x.dob, "phone": x.phone, "email": x.email,
              "id_last4": x.id_last4, "policy_number": x.policy_number}
    if fields["name"] and s.claimed.get("name") and data.is_partial_name(fields["name"], s.claimed["name"]):
        fields["name"] = None  # an echoed first name ("Thanks, Margaret") never replaces the full name
    new = {k: v for k, v in fields.items() if v}
    for f in set(x.declined_fields) - set(new):  # refused or withdrawn ("forget the email"): stop checking it
        s.claimed.pop(f, None)
    s.claimed.update(new)
    s.declined = sorted((set(s.declined) | set(x.declined_fields)) - set(s.claimed))
    if x.caller_role != "unknown" and s.caller_role != "representative":  # once acting for someone else, stays so
        s.caller_role = x.caller_role
    if x.representative_name and not (s.rep_name and data.is_partial_name(x.representative_name, s.rep_name)):
        s.rep_name = x.representative_name
    s.relationship = x.relationship or s.relationship
    hints = turn_hints(x)
    old_id = s.case_hints.get("case_id")
    if old_id and hints and "case_id" not in hints and not data.match_claims(
            [c for c in data.CLAIMS if c["case_id"].upper() == old_id.upper()], hints):
        del s.case_hints["case_id"]  # the caller now describes a different claim than the number given earlier
    s.case_hints.update(hints)
    s.intent = x.intent or s.intent
    if x.question:
        s.questions.append(x.question)
    if new or hints or x.intent:
        events.append({"type": "memory", "data": {"identity": sorted(new), "case_hints": hints, "intent": x.intent}})


def turn_hints(x):
    hints = {"case_id": x.case_id, "case_type": x.case_type, "status": x.case_status,
             "month": x.case_month, "year": x.case_year}
    return {k: v for k, v in hints.items() if v}


# ---------- the SOP ----------

def apply(s, x, text, llm, events):
    """Deterministic workflow step. Returns instructions for the responder."""
    notes = []
    remember(s, x, events)
    s.emotion = x.emotion
    if x.emotion in ("frustrated", "angry"):
        s.frustration += 1
    if x.emotion in EMOTION_NOTES:
        notes.append(EMOTION_NOTES[x.emotion])

    if x.wants_human:
        return handoff(s, "caller asked for a human representative", notes)
    if x.off_topic:
        s.off_topic += 1
        notes.append(
            "Part of the message is outside what you can help with. Politely decline that part in one sentence "
            "(do not answer it) and steer back to their insurance needs."
            + (" The caller keeps asking unrelated questions: offer to connect them with a human representative "
               "for help with their insurance needs (a human won't answer unrelated questions either)."
               if s.off_topic >= OFF_TOPIC_LIMIT else ""))

    if s.party_id and (s.caller_role == "representative" or s.rep_name) and s.consent != "approved":
        # the speaker turned out not to be the policyholder: back through the representative + consent gate
        s.party_id, s.case_id, s.phase = None, None, "VERIFY_ID"
        events.append({"type": "transition", "data": "re-gated: caller is acting for someone else"})

    if s.phase == "VERIFY_ID":
        verify_step(s, notes, events)
    if s.phase == "RESOLVE_INTENT":
        resolve_step(s, x, text, llm, notes, events)
    elif s.phase == "PROCESS_CASE":
        process_step(s, x, text, llm, notes, events)
    elif s.phase == "POST_PROCESS":
        post_step(s, x, text, llm, notes, events)

    if s.phase == "VERIFY_ID" and s.frustration >= FRUSTRATION_LIMIT:
        notes.append("The caller has been frustrated for several turns: also offer a transfer to a human "
                     "representative (who will also need to verify them).")
    return notes


def verify_step(s, notes, events):
    if s.pending_party:
        return consent_step(s, notes, events)
    provided = data.provided_fields(s.claimed)
    r = data.verify_identity(s.claimed)
    events.append({"type": "tool", "name": "verify_identity", "data": {"provided": provided, "verified": r["verified"]}})
    remaining = [FIELD_LABELS[f] for f in ID_FIELDS if f not in provided and f not in s.declined]

    if r["verified"]:
        if s.caller_role == "representative" or s.rep_name:
            return representative_step(s, r["party_id"], notes, events)
        return authorize(s, r["party_id"], notes)

    if len(provided) >= MIN_MATCHES:
        attempt = json.dumps(s.claimed, sort_keys=True)
        if attempt != s.last_failed:  # only count a new set of details as a new attempt
            s.last_failed = attempt
            s.failed_attempts += 1
        if s.failed_attempts >= MAX_FAILED_VERIFICATIONS:
            return handoff(s, "identity could not be verified after several attempts", notes)
        notes.append(
            "Verification FAILED: the check has already run on everything the caller has given, including this "
            "message, and the details do not all match our records. Say so now; never say you are still checking. "
            "Do NOT say which detail is wrong. Adding more details won't fix a wrong one: ask them to re-check and "
            "restate their details, or tell you which one they're unsure of so it can be set aside"
            + (f" (other details they could use: {', '.join(remaining)})" if remaining else "") + ". "
            f"Attempts left before transfer to a human: {MAX_FAILED_VERIFICATIONS - s.failed_attempts}.")
    elif len(provided) + len(remaining) < MIN_MATCHES:
        notes.append("The caller has declined too many details to complete verification. Explain kindly that "
                     "three details are required to protect their account, and offer a human representative.")
    else:
        got = ", ".join(FIELD_LABELS[f] for f in provided) or "none yet"
        notes.append(
            f"Identity NOT verified yet. Received {len(provided)} of the {MIN_MATCHES} required details ({got}). "
            f"Ask for {MIN_MATCHES - len(provided)} more from: {', '.join(remaining)}. "
            "The caller may choose any of these. Do not discuss any claim yet.")
    if s.case_hints or s.intent:
        notes.append("The caller already told you what they're calling about (see 'Remembered'). Briefly "
                     "acknowledge you've noted it and will look into it right after verification. Do not ask again.")


def representative_step(s, party_id, notes, events):
    if s.consent == "timeout":
        notes.append("The policyholder's authorization already timed out; you still cannot continue on their behalf. "
                     "Offer a human representative or suggest the policyholder contact us directly.")
        return
    rep = data.find_representative(s.rep_name, party_id)
    events.append({"type": "tool", "name": "find_representative", "data": {"rep_name": s.rep_name, "authorized": bool(rep)}})
    if not s.rep_name:
        notes.append("The caller is calling on someone else's behalf. Ask for the caller's own full name.")
    elif not rep:
        notes.append("The caller is NOT an authorized representative on file for this policy, so you cannot discuss "
                     "the account. Explain kindly; the policyholder can contact us directly, or you can offer a human representative.")
    else:
        s.pending_party, s.consent, s.consent_checks = party_id, "pending", 0
        events.append({"type": "tool", "name": "request_consent", "data": {"party_id": party_id, "status": "pending"}})
        notes.append(
            "SOP requires the policyholder's consent before you can discuss anything on this account. Say you've "
            "sent an authorization request to the policyholder's phone number on file and ask the caller to let "
            "you know once it's approved. Don't confirm whether the details matched or whether the caller is on "
            "file, and don't discuss any claim.")


def consent_step(s, notes, events):
    s.consent_checks += 1
    s.consent = data.consent_status(s.consent_scenario, s.consent_checks)
    events.append({"type": "tool", "name": "check_consent", "data": {"check": s.consent_checks, "status": s.consent}})
    if s.consent == "approved":
        return authorize(s, s.pending_party, notes, via_rep=True)
    if s.consent == "timeout":
        s.pending_party = None
        notes.append("The policyholder's authorization was not received in time, so you cannot continue on their "
                     "behalf. Explain why consent protects the policyholder. The only options are a human "
                     "representative or the policyholder contacting us directly; don't promise a new request or a callback.")
    else:
        notes.append("Authorization from the policyholder is still pending. Explain you can't discuss the account "
                     "until they approve it, and that you'll check again when the caller is ready.")


def authorize(s, party_id, notes, via_rep=False):
    s.party_id, s.pending_party, s.phase = party_id, None, "RESOLVE_INTENT"
    who = data.policyholder(party_id)["name"]
    notes.append(f"Identity verified{' and consent approved' if via_rep else ''}: the account belongs to {who}. "
                 "Thank the caller briefly for verifying.")


def resolve_step(s, x, text, llm, notes, events):
    claims = data.claims_for(s.party_id)
    if not claims:
        if x.no_more_questions or x.email_choice != "none":
            return post_step(s, x, text, llm, notes, events)
        notes.append("This policyholder has no claims on file. Say so, and ask if there's anything else about "
                     "their policy you can help with.")
        return
    matches = data.match_claims(claims, s.case_hints) if s.case_hints else []
    if s.case_hints and not matches and turn_hints(x):
        matches = data.match_claims(claims, turn_hints(x))  # accumulated hints conflict; trust the latest words
    events.append({"type": "tool", "name": "match_claims",
                   "data": {"hints": s.case_hints, "matches": [c["case_id"] for c in matches]}})
    if len(matches) == 1:
        return select_case(s, matches[0], s.intent, notes, events)
    if (x.no_more_questions and not x.question) or x.email_choice != "none":
        return post_step(s, x, text, llm, notes, events)
    if x.question:
        notes.append("If CLAIMS ON FILE answers the caller's question (e.g. statuses or amounts across claims), "
                     "answer it from there.")
    if len(matches) > 1:
        notes.append("Several claims match what the caller described. Ask which one they mean, briefly listing "
                     "only the matching claims (id, type, filed date, status) from the data section.")
    elif s.case_hints:
        notes.append("No claim matches what the caller described. Say so kindly and list the claims on file "
                     "(id, type, filed date, status) so they can pick one.")
    else:
        notes.append("Ask what they're calling about today; you may briefly list the claims on file "
                     "(id, type, filed date, status).")


def select_case(s, claim, intent, notes, events):
    s.case_id, s.phase = claim["case_id"], "PROCESS_CASE"
    # default path for the case when the caller didn't say what they need
    s.intent = intent or {"denied": "denial_question", "open": "status_inquiry"}.get(claim["status"], "general_claim_question")
    if claim["case_id"] not in s.discussed:
        s.discussed.append(claim["case_id"])
    events.append({"type": "tool", "name": "get_claim", "data": claim["case_id"]})
    notes.append(f"Claim {claim['case_id']} is selected (intent: {s.intent}). Confirm which claim you're looking at "
                 "in a few words, then address the caller's need using the CLAIM FACTS. If they haven't asked a "
                 "specific question, give the key status and the most useful next step.")


def process_step(s, x, text, llm, notes, events):
    hints = turn_hints(x)
    current = next(c for c in data.CLAIMS if c["case_id"] == s.case_id)
    if hints and not data.match_claims([current], hints):  # caller moved to a different claim
        matches = data.match_claims(data.claims_for(s.party_id), hints)
        if len(matches) == 1:
            return select_case(s, matches[0], x.intent, notes, events)
        s.case_hints, s.phase, s.intent = hints, "RESOLVE_INTENT", x.intent
        return resolve_step(s, x, text, llm, notes, events)
    if x.intent:
        s.intent = x.intent
    if (x.no_more_questions and not x.question) or x.email_choice != "none":
        return post_step(s, x, text, llm, notes, events)
    notes.append("Answer using only the CLAIM FACTS; guidance marked matches_caller_wording is most likely "
                 "relevant. If the facts don't cover the question, use followup_fallback or offer a human "
                 "representative. When their question is answered, ask if there's anything else.")


def post_step(s, x, text, llm, notes, events):
    """Wrap-up. The email needs an explicit send/skip; open questions are answered before the call ends."""
    email = data.mask_email(data.policyholder(s.party_id)["email"])
    s.phase = "POST_PROCESS"
    if x.email_choice != "none":
        s.email_choice = x.email_choice  # kept even if they also ask something
    hints = turn_hints(x)
    if hints and s.case_id and not data.match_claims([c for c in data.CLAIMS if c["case_id"] == s.case_id], hints):
        s.phase = "PROCESS_CASE"  # a different claim: back to case work (the email choice is remembered)
        return process_step(s, x, text, llm, notes, events)
    if x.question:
        notes.append(
            "Answer their question using only the data section (if it's about the summary email: it covers what was "
            f"discussed, claim status and next steps, and goes only to the email on file, {email}). "
            + (f"They already chose to {'receive' if s.email_choice == 'send' else 'skip'} the summary email; it "
               "will be handled when they're done. Ask if there's anything else."
               if s.email_choice else f"Then ask whether they'd like the summary emailed to {email}, or to skip it."))
        return
    if s.email_choice == "send":
        s.email, s.phase = compose_email(s, llm), "ENDED"
        events.append({"type": "tool", "name": "send_email", "data": {"to": s.email["to"]}})
        notes.append(f"The summary email has been sent to {email}. Confirm, mention the key next step in one "
                     "sentence, and close the conversation warmly.")
    elif s.email_choice == "skip":
        s.phase = "ENDED"
        notes.append("The caller chose not to receive the email. Confirm nothing will be sent and close warmly.")
    elif not s.email_offered:
        s.email_offered = True
        notes.append(f"Offer to email a summary of this conversation (what was discussed, claim status, and next "
                     f"steps) to the email on file, {email}. Make clear they can choose to receive it or skip it. "
                     "Only this address can be used.")
    else:
        notes.append(f"Ask clearly whether they'd like the summary emailed to {email}, or prefer to skip it. "
                     "Only the email on file can be used.")


def compose_email(s, llm):
    rec = data.policyholder(s.party_id)
    claims = [c for c in data.CLAIMS if c["case_id"] in s.discussed]
    summary = llm.summarize([data.claim_facts(c) for c in claims], transcript(s))
    status = [f"{c['case_id']} ({c['case_type']}, filed {c['created_at']}): {c['status'].upper()} - {c['summary']}"
              + (f". Appeal deadline: {c['appeal_deadline']}" if c.get("appeal_deadline") else "") for c in claims]
    body = "\n".join([
        f"Hi {rec['name']},", "",
        "Thank you for contacting claims support. Here is a summary of our conversation.", "",
        "What we discussed:", *[f"- {d}" for d in summary.discussed], "",
        "Claim status:", *([f"- {x}" for x in status] or ["- No specific claim was reviewed."]), "",
        "Next steps:", *[f"- {n}" for n in summary.next_steps], "",
        "If you have questions, just reply or contact claims support.",
    ])
    subject = f"Summary of your claims support conversation{' - ' + ', '.join(s.discussed) if s.discussed else ''}"
    return {"to": rec["email"], "subject": subject, "body": body}


def handoff(s, reason, notes):
    s.handoff = {"reason": reason, "phase": s.phase, "verified": bool(s.party_id), "party_id": s.party_id,
                 "case_id": s.case_id, "case_hints": s.case_hints, "emotion": s.emotion,
                 "questions": s.questions}
    s.phase = "HUMAN_HANDOFF"
    notes.append(f"Transfer the caller to a human representative (reason: {reason}). Tell them warmly that you're "
                 "connecting them and the representative will see the context of this conversation"
                 + ("." if s.party_id else ", though they will also need to verify identity."))
    return notes


# ---------- what each LLM call is allowed to see ----------

def extract_context(s, text):
    last = next((m["content"] for m in reversed(s.history[:-1]) if m["role"] == "assistant"), "")
    ctx = {"today": str(data.today()), "phase": s.phase,
           "earlier_caller_messages": [m["content"] for m in s.history[:-1] if m["role"] == "user"][-6:],
           "agent_last_message": last, "caller_message": text}
    if s.party_id:  # claim list only after verification
        ctx["claims_on_file"] = [data.claim_brief(c) for c in data.claims_for(s.party_id)]
        ctx["current_case_id"] = s.case_id
    return ctx


def harness_state(s, notes, x, text):
    verified = bool(s.party_id)
    lines = [
        "HARNESS STATE (authoritative)",
        f"Phase: {s.phase}. Identity verified: {'yes' if verified else 'NO'}.",
        f"Caller emotion this turn: {x.emotion}.",
        "Instructions for this reply:", *[f"- {n}" for n in notes],
    ]
    remembered = {k: v for k, v in {"case_hints": s.case_hints, "intent": s.intent}.items() if v}
    if remembered:
        lines.append(f"Remembered from the caller: {json.dumps(remembered)}")
    if not verified:
        lines.append("DATA: none. No account or claim information is available until identity is verified.")
        return "\n".join(lines)
    rec = data.policyholder(s.party_id)
    lines.append(f"Caller: {rec['name']}" + (f" (speaking: {s.rep_name}, {s.relationship})" if s.rep_name else ""))
    lines.append("CLAIMS ON FILE: " + json.dumps([data.claim_brief(c) for c in data.claims_for(s.party_id)]))
    if s.case_id and s.phase in ("PROCESS_CASE", "POST_PROCESS", "ENDED"):
        claim = next(c for c in data.CLAIMS if c["case_id"] == s.case_id)
        lines.append("CLAIM FACTS: " + json.dumps(data.claim_facts(claim, " ".join([*s.questions[-3:], text])), indent=1))
    return "\n".join(lines)


def llm_messages(s):
    msgs = list(s.history)
    while msgs and msgs[0]["role"] == "assistant":  # API conversations must start with the user
        msgs.pop(0)
    return msgs


def guard(s, reply, events):
    """Defense in depth: block claim data that isn't this caller's, and any claim data before verification."""
    said = set(data.leaked_claim_data(" ".join(m["content"] for m in s.history if m["role"] == "user")))
    leaked = [t for t in data.leaked_claim_data(reply, allowed_party=s.party_id) if t not in said]  # echoing the caller is fine
    if not leaked:
        return reply
    events.append({"type": "guard_blocked", "data": leaked})
    return SAFE_UNVERIFIED_REPLY if not s.party_id else (
        "Sorry, let me rephrase that. Could you tell me which of your claims you'd like to go over?")


def transcript(s):
    return [f"{m['role']}: {m['content']}" for m in s.history]


def public_state(s):
    """Operator/debug view for the UI side panel."""
    st = asdict(s)
    for k in ("history", "pending_party", "last_failed"):
        st.pop(k)
    st["trace"] = s.trace[-1:]  # latest turn only
    # progress only; match counts would let a caller probe which detail is wrong
    st["verification"] = {"provided": data.provided_fields(s.claimed), "required": MIN_MATCHES}
    st["phases"] = PHASES
    st["email_on_file"] = data.mask_email(data.policyholder(s.party_id)["email"]) if s.party_id else None
    return st
