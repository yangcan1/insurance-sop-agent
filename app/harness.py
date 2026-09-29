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
FRUSTRATION_LIMIT = 2  # consecutive frustrated/angry turns before we stop persuading

GREETING = ("Hi, thanks for contacting claims support. I can help with questions about your policy and claims. "
            "To get started, could you tell me your full name and what you're calling about today?")
SAFE_UNVERIFIED_REPLY = (
    "I want to help with that, but I can't share any claim or account details until I've verified your identity. "
    "Could you give me at least three of these: your full name, date of birth, phone number, email on file, "
    "or the last four digits of your SSN?")
EMOTION_NOTES = {
    "frustrated": "Caller is frustrated. Open with one short acknowledgment of the specific thing frustrating them, in "
                  "your own words (not a stock line like 'I understand your frustration'). If they say they already gave "
                  "their details but fewer than 3 are received here, say plainly and without blame that you don't have "
                  "them in this chat yet. If a verification or consent step is what's blocking them: the reason in one "
                  "clause, the quickest way through, and that they can talk to a human representative instead. If "
                  "nothing is blocking them, skip the explanation and just help.",
    "angry": "Caller is angry. Stay calm and steady: one brief acknowledgment, no arguing, no repeated apologies, don't "
             "match their tone. If a verification or consent step is blocking them, say in one clause why it protects "
             "them, then the fastest way through, and offer a human representative. If nothing is blocking them, just "
             "help. Don't lecture.",
    "anxious": "Caller sounds anxious. Reassure them in one sentence using something true from your instructions or data "
               "(no 'the good news is'), then make the single next step clear. Don't pile on details.",
    "confused": "Caller seems confused. Plain words, one idea per sentence, and ask for exactly one thing.",
    "sad": "Caller sounds upset. One sentence of genuine empathy first, then continue gently.",
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
    frustration: int = 0                              # consecutive frustrated/angry turns
    emotion: str = "calm"
    id_asked: bool = False                            # the accepted ID details were listed once already
    context_start: int = 0                            # history index the responder may see from (moves on re-gate)
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
        logged = [e["data"] for e in events if e["type"] == "transition"]
        if s.phase != phase_before and not (logged and str(logged[-1]).endswith(s.phase)):
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
    if fields["name"] and s.claimed.get("name") and data.is_echo(fields["name"], s.claimed["name"]):
        fields["name"] = None  # an echoed first name ("Thanks, Margaret") never replaces the full name
    new = {k: v for k, v in fields.items() if v}
    for f in set(x.declined_fields) - set(new):  # refused or withdrawn ("forget the email"): stop checking it
        s.claimed.pop(f, None)
    s.claimed.update(new)
    s.declined = sorted((set(s.declined) | set(x.declined_fields)) - set(s.claimed))
    if x.caller_role != "unknown" and s.caller_role != "representative":  # once acting for someone else, stays so
        s.caller_role = x.caller_role
    new_rel, old_rel = (x.relationship or "").lower(), (s.relationship or "").lower()
    same_rep = s.rep_name and data.is_echo(x.representative_name or "", s.rep_name) and (
        not new_rel or not old_rel or new_rel in old_rel or old_rel in new_rel)  # "Chen, her husband" is not David
    if x.representative_name and not same_rep:
        if s.rep_name and data.norm_name(x.representative_name) != data.norm_name(s.rep_name):
            s.consent = None  # consent belongs to the person it was granted for, not to the session
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
    s.frustration = s.frustration + 1 if x.emotion in ("frustrated", "angry") else 0  # a calm turn resets it
    if x.emotion in EMOTION_NOTES:
        notes.append(EMOTION_NOTES[x.emotion])

    if x.wants_human:
        return handoff(s, "caller asked for a human representative", notes)
    if x.off_topic:
        s.off_topic += 1
        notes.append(
            "Part of the message is outside what you can help with. Decline that part in one plain sentence without "
            "answering any of it (no partial facts or hints), then steer back to their insurance needs in one sentence."
            + (" They keep asking unrelated things: offer to connect them with a human representative for help with "
               "their policy or claims. Don't comment on what the representative can or can't answer."
               if s.off_topic >= OFF_TOPIC_LIMIT else ""))

    if s.party_id and (s.caller_role == "representative" or s.rep_name) and s.consent != "approved":
        # the speaker turned out not to be the policyholder: back through the representative + consent gate
        s.party_id, s.case_id, s.phase = None, None, "VERIFY_ID"
        s.context_start = len(s.history) - 1  # earlier replies held claim facts: the responder starts fresh
        events.append({"type": "transition", "data": "re-gated: caller is acting for someone else"})

    if s.phase == "VERIFY_ID":
        verify_step(s, notes, events)
    if s.phase == "RESOLVE_INTENT":
        resolve_step(s, x, text, llm, notes, events)
    elif s.phase == "PROCESS_CASE":
        process_step(s, x, text, llm, notes, events)
    elif s.phase == "POST_PROCESS":
        post_step(s, x, text, llm, notes, events)

    if s.frustration >= FRUSTRATION_LIMIT and s.phase not in TERMINAL:  # explained once already: stop persuading
        notes.append(
            "The caller has stayed frustrated: stop explaining; answer as directly as the instructions allow and "
            "offer a transfer to a human representative in one short clause."
            if s.party_id else
            "The caller has stayed frustrated: stop explaining why verification is needed and offer a transfer to a "
            "human representative as one of two short choices: give the remaining details now, or be connected to a "
            "person now (who will also confirm their identity). No more persuasion.")
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
        return authorize(s, r["party_id"], notes, events)

    if len(provided) >= MIN_MATCHES:
        attempt = json.dumps(s.claimed, sort_keys=True)
        if attempt != s.last_failed:  # only count a new set of details as a new attempt
            s.last_failed = attempt
            s.failed_attempts += 1
        if s.failed_attempts >= MAX_FAILED_VERIFICATIONS:
            return handoff(s, "identity could not be verified after several attempts", notes)
        notes.append(
            "Verification FAILED: the check has already run on everything the caller has given, including this "
            "message, and the details do not all match our records. Say so now, plainly and kindly; never say you "
            "are still checking. Do NOT say which detail is wrong (if they ask which one, say you can't tell them, "
            "because it would help an impostor). Adding more details won't fix a wrong one: ask them to re-check "
            "what they gave, or to name the one they're least sure of so it can be set aside"
            + (f", or to use a different detail instead ({', '.join(remaining)})" if remaining else "") + "."
            + (" This is their last try before you'd connect them to a human representative who can verify them "
               "another way; say that as help, not a warning."
               if MAX_FAILED_VERIFICATIONS - s.failed_attempts == 1 else ""))
    elif len(provided) + len(remaining) < MIN_MATCHES:
        notes.append("The caller has ruled out too many details to reach three. Say kindly, in one or two sentences, "
                     "that three matching details are needed to protect their account, and offer a human representative "
                     "(or to reconsider one of the details they set aside). No pressure beyond that.")
    else:
        got = ", ".join(FIELD_LABELS[f] for f in provided) or "none yet"
        need = MIN_MATCHES - len(provided)
        notes.append(
            f"Identity NOT verified yet. Received {len(provided)} of the {MIN_MATCHES} required details ({got}). "
            + (f"Ask for {need} more from: {', '.join(remaining)}. Any {need} of these will do; the caller chooses. "
               if not s.id_asked else
               f"Ask for {need} more in one short sentence. You already listed the accepted details in an earlier reply, "
               f"so don't read the whole list again; name one or two that would finish it, unless they ask what counts. "
               f"Still accepted: {', '.join(remaining)}. ")
            + "One question. Do not discuss any claim yet.")
        s.id_asked = True
    if s.case_hints or s.intent:
        notes.append("The caller already said what they're calling about (see 'Remembered'). If none of your earlier "
                     "replies acknowledged it, say in a few words that you've noted it and will look into it right after "
                     "verification; otherwise don't repeat that. Don't ask what they're calling about, and don't discuss it.")


def representative_step(s, party_id, notes, events):
    if s.consent == "timeout":
        notes.append("The policyholder's authorization already timed out; you still cannot continue on their behalf. "
                     "Say so in one sentence; the only options are a human representative or the policyholder "
                     "contacting us directly.")
        return
    rep = data.find_representative(s.rep_name, party_id)
    events.append({"type": "tool", "name": "find_representative", "data": {"rep_name": s.rep_name, "authorized": bool(rep)}})
    if not s.rep_name:
        notes.append("The caller is calling on someone else's behalf. Ask for the caller's own full name, as one "
                     "question. Don't say whether the policyholder's details matched, and don't discuss the account.")
    elif not rep:
        notes.append("The caller is NOT an authorized representative on file for this policy, so you cannot discuss "
                     "the account. Say kindly, in one or two sentences, that you can't go over this account with them; "
                     "the policyholder can contact us directly, or you can offer a human representative. Don't say "
                     "whether the policyholder's details matched.")
    else:
        s.pending_party, s.consent, s.consent_checks = party_id, "pending", 0
        events.append({"type": "tool", "name": "request_consent", "data": {"party_id": party_id, "status": "pending"}})
        notes.append(
            "The policyholder's consent is required before anything on this account can be discussed with someone "
            "else; give that reason in one clause, in plain words (never say 'SOP' or 'policy requires'). Say you've "
            "sent an authorization request to the policyholder's phone number on file and ask, as one question, that "
            "they let you know once it's approved. Don't confirm whether the details matched or whether the caller is "
            "on file, and don't discuss any claim.")


def consent_step(s, notes, events):
    s.consent_checks += 1
    s.consent = data.consent_status(s.consent_scenario, s.consent_checks)
    events.append({"type": "tool", "name": "check_consent", "data": {"check": s.consent_checks, "status": s.consent}})
    if s.consent == "approved":
        return authorize(s, s.pending_party, notes, events, via_rep=True)
    if s.consent == "timeout":
        s.pending_party = None
        notes.append("The policyholder's authorization was not received in time, so you cannot continue on their "
                     "behalf. Say so plainly, with one clause on why consent protects the policyholder. The only "
                     "options are a human representative or the policyholder contacting us directly; don't promise a "
                     "new request or a callback.")
    else:
        notes.append("Authorization from the policyholder is still pending. Say in one sentence that you can't discuss "
                     "the account until they approve it and that you'll check again when the caller says so. Don't "
                     "re-explain the whole process.")


def authorize(s, party_id, notes, events, via_rep=False):
    events.append({"type": "transition", "data": f"{s.phase} -> RESOLVE_INTENT"})
    s.party_id, s.pending_party, s.phase = party_id, None, "RESOLVE_INTENT"
    who = data.policyholder(party_id)["name"]
    notes.append(f"Identity verified{' and consent approved' if via_rep else ''}: the account belongs to {who}. "
                 "Thank the caller in a few words, then move straight on; no 'you're all set' ceremony.")


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
        notes.append("Several claims match what the caller described. Ask which one they mean, as one question, naming "
                     "only the matching claims in plain words (type, filed date in words, status; claim id last).")
    elif s.case_hints:
        notes.append("No claim matches what the caller described. Say so kindly in one sentence and name the claims on "
                     "file in plain words (type, filed date in words, status; claim id last) so they can pick one.")
    else:
        notes.append("Ask what they'd like help with, as one question. If there are more than two claims on file, "
                     "don't read them all out: name the one or two most likely relevant (denied or open first) in plain "
                     "words and mention there are others.")


def select_case(s, claim, intent, notes, events):
    if s.phase != "PROCESS_CASE":
        events.append({"type": "transition", "data": f"{s.phase} -> PROCESS_CASE"})
    s.case_id, s.phase = claim["case_id"], "PROCESS_CASE"
    # default path for the case when the caller didn't say what they need
    s.intent = intent or {"denied": "denial_question", "open": "status_inquiry"}.get(claim["status"], "general_claim_question")
    if claim["case_id"] not in s.discussed:
        s.discussed.append(claim["case_id"])
    events.append({"type": "tool", "name": "get_claim", "data": claim["case_id"]})
    notes.append(f"Claim {claim['case_id']} is selected (intent: {s.intent}). Name the claim in a few words (type and "
                 "filed month, plus the id), then answer what they came for from the CLAIM FACTS: for a denied claim, "
                 "the denial reason and the appeal deadline in one clause; for an open claim, the status; otherwise the "
                 "key status. Then the single most useful next step. Don't recite amounts, day counts, or document "
                 "requirements unless asked. At most one question, offering the most likely next detail.")


def process_step(s, x, text, llm, notes, events):
    hints = turn_hints(x)
    current = next(c for c in data.CLAIMS if c["case_id"] == s.case_id)
    if hints and not data.match_claims([current], hints):  # caller moved to a different claim
        matches = data.match_claims(data.claims_for(s.party_id), hints)
        if len(matches) == 1:
            return select_case(s, matches[0], x.intent, notes, events)
        # the caller left this claim: drop it, otherwise post_step would bounce back to it forever
        s.case_hints, s.phase, s.intent, s.case_id = hints, "RESOLVE_INTENT", x.intent, None
        return resolve_step(s, x, text, llm, notes, events)
    if x.intent:
        s.intent = x.intent
    if (x.no_more_questions and not x.question) or x.email_choice != "none":
        return post_step(s, x, text, llm, notes, events)
    notes.append("Answer using only the CLAIM FACTS; guidance marked matches_caller_wording or matches_intent is most "
                 "likely relevant. If the facts don't cover the question, use followup_fallback or offer a human "
                 "representative. If they ask again about something you already explained, don't repeat it word for "
                 "word: say that's the full reason on file and add only what's new. Close with at most one short "
                 "question, preferably a specific next-step offer (how to upload, what a document needs); ask if there's "
                 "anything else only once their questions seem covered, and never reuse last turn's closing question.")


def post_step(s, x, text, llm, notes, events):
    """Wrap-up. The email needs an explicit send/skip; open questions are answered before the call ends."""
    email = data.mask_email(data.policyholder(s.party_id)["email"])
    s.phase = "POST_PROCESS"
    if x.email_choice != "none" and s.email_offered:  # only an answer to an offer we made counts as consent
        s.email_choice = x.email_choice  # kept even if they also ask something
    hints = turn_hints(x)
    if hints and s.case_id and not data.match_claims([c for c in data.CLAIMS if c["case_id"] == s.case_id], hints):
        s.phase = "PROCESS_CASE"  # a different claim: back to case work (the email choice is remembered)
        return process_step(s, x, text, llm, notes, events)
    picked = data.match_claims(data.claims_for(s.party_id), hints) if hints and not s.case_id else []
    if len(picked) == 1:  # no claim selected yet and the caller names one (select directly: resolve_step would loop)
        return select_case(s, picked[0], x.intent, notes, events)
    if x.question:
        s.email_offered = s.email_offered or not s.email_choice  # the note below asks about the email
        notes.append(
            "Answer their question using only the data section (if it's about the summary email: it covers what was "
            f"discussed, claim status and next steps, and goes only to the email on file, {email}). "
            + (f"They already chose to {'receive' if s.email_choice == 'send' else 'skip'} the summary email; it "
               "will be handled when they're done. End with at most one short question."
               if s.email_choice else
               f"Then ask, as one yes/no question, whether they'd like the summary emailed to {email} or would rather skip it."))
        return
    if s.email_choice == "send":
        s.email, s.phase = compose_email(s, llm), "ENDED"
        events.append({"type": "tool", "name": "send_email", "data": {"to": s.email["to"]}})
        notes.append(f"The summary email has been sent to {email}. Confirm that in one sentence, give the key next step "
                     "(with its date, if there is one) in one sentence, and close warmly in one more. No questions.")
    elif s.email_choice == "skip":
        s.phase = "ENDED"
        notes.append("The caller chose not to receive the email. Say nothing will be sent and close warmly, in one or "
                     "two sentences. No questions.")
    elif not s.email_offered:
        s.email_offered = True
        notes.append(f"Offer, as one yes/no question, to email a summary of this conversation (what was discussed, claim "
                     f"status, and next steps) to the email on file, {email}, the only address that can be used. Say the "
                     "address as given (not 'ending in'). The yes/no question already leaves the choice to them, so don't "
                     "add 'it's optional' or 'up to you'.")
    else:
        notes.append(f"Ask, as one yes/no question, whether they'd like the summary emailed to {email} or would rather "
                     "skip it. Only the email on file can be used.")


def compose_email(s, llm):
    rec = data.policyholder(s.party_id)
    claims = [c for c in data.CLAIMS if c["case_id"] in s.discussed]
    summary = llm.summarize([data.claim_facts(c) for c in claims], transcript(s))
    status = [f"{c['case_id']} ({c['case_type']}, filed {c['created_at']}): {c['status'].upper()} - {c['summary']}"
              + (f". Appeal deadline: {c['appeal_deadline']}" if c.get("appeal_deadline") else "") for c in claims]
    body = "\n".join([
        f"Hi {rec['name']},", "",
        "Thank you for contacting claims support. Here is a summary of our conversation.", "",
        "What we discussed:", *([f"- {d}" for d in summary.discussed] or ["- (none recorded)"]), "",
        "Claim status:", *([f"- {x}" for x in status] or ["- No specific claim was reviewed."]), "",
        "Next steps:", *([f"- {n}" for n in summary.next_steps] or ["- (none recorded)"]), "",
        "If you have questions, just reply or contact claims support.",
    ])
    subject = f"Summary of your claims support conversation{' - ' + ', '.join(s.discussed) if s.discussed else ''}"
    return {"to": rec["email"], "subject": subject, "body": body}


def handoff(s, reason, notes):
    s.handoff = {"reason": reason, "phase": s.phase, "verified": bool(s.party_id), "party_id": s.party_id,
                 "case_id": s.case_id, "case_hints": s.case_hints, "emotion": s.emotion,
                 "questions": s.questions}
    s.phase = "HUMAN_HANDOFF"
    notes.append(f"Transfer the caller to a human representative (reason: {reason}). In two or three sentences: "
                 "acknowledge briefly if they're upset, say you're connecting them now and that the representative will "
                 "see this conversation" + ("." if s.party_id else ", and will confirm their identity before going into "
                 "the account.") + " No questions, no re-explaining the rules.")
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
        lines.append("CLAIM FACTS: " + json.dumps(
            data.claim_facts(claim, " ".join([*s.questions[-3:], text]), intent=s.intent), indent=1))
    return "\n".join(lines)


def llm_messages(s):
    msgs = list(s.history[s.context_start:])
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
