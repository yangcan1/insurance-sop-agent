"""Deterministic SOP tests: a scripted fake LLM stands in for the model, so these check the
harness itself (gates, memory, transitions, data isolation) with no API key."""
import json

from app import data, harness
from app.llm import EmailSummary, Extraction as X


class FakeLLM:
    def __init__(self, *extractions, reply="ok"):
        self.extractions, self.reply, self.prompts = list(extractions), reply, []

    def extract(self, ctx):
        return self.extractions.pop(0)

    def respond(self, harness_state, messages):
        self.prompts.append(harness_state)
        self.seen = getattr(self, "seen", []) + [messages]
        return self.reply

    def summarize(self, facts, transcript):
        return EmailSummary(discussed=["Why claim CL-2048 was denied"], next_steps=["Upload the pathology report"])


def run(llm, *messages, **session_kw):
    s = harness.Session(**session_kw)
    for m in messages:
        harness.turn(s, m, llm)
    return s


DEMO = X(full_name="Margaret Chen", policy_number="POL-9921", dob="1985-03-15", id_last4="4472",
         caller_role="policyholder", case_type="healthcare", case_status="denied", case_month=1,
         intent="denial_question")


def test_demo_case_verifies_and_uses_remembered_hint():
    llm = FakeLLM(DEMO)
    s = run(llm, "demo")
    assert s.party_id == "P9"
    assert s.case_id == "CL-2048"  # denied + healthcare + January disambiguates from CL-2011 (Jan 2025, closed)
    assert s.phase == "PROCESS_CASE"
    assert "pathology report" in llm.prompts[-1]


def test_hint_before_verification_is_remembered_but_not_disclosed():
    llm = FakeLLM(X(case_type="healthcare", case_status="denied", case_month=1),
                  X(full_name="Margaret Chen", dob="March 15th, 1985"),
                  X(id_last4="4472"))
    s = harness.Session()
    harness.turn(s, "calling about my denied healthcare claim from January", llm)
    harness.turn(s, "Margaret Chen, March 15th 1985", llm)
    assert s.phase == "VERIFY_ID" and s.case_hints == {"case_type": "healthcare", "status": "denied", "month": 1}
    for prompt in llm.prompts:  # data isolation: nothing claim-specific reaches the model before verification
        assert "CL-" not in prompt and "pathology" not in prompt and "DATA: none" in prompt
    harness.turn(s, "4472", llm)
    assert s.case_id == "CL-2048" and s.phase == "PROCESS_CASE"


def test_two_fields_are_not_enough():
    s = run(FakeLLM(X(full_name="Margaret Chen", id_last4="4472")), "m")
    assert s.phase == "VERIFY_ID" and s.party_id is None


def test_mismatch_never_verifies_and_three_failures_hand_off():
    wrong = [X(full_name="Margaret Chen", dob=f"1985-03-1{d}", id_last4="4472") for d in (6, 7, 8)]
    llm = FakeLLM(*wrong)
    s = run(llm, "a", "b", "c")
    assert s.party_id is None and s.failed_attempts == 3 and s.phase == "HUMAN_HANDOFF"
    assert all("which detail" in p for p in llm.prompts[:2])


def test_extra_wrong_field_blocks_even_with_three_matches():
    s = run(FakeLLM(X(full_name="Margaret Chen", dob="1985-03-15", id_last4="4472", email="wrong@x.com")), "m")
    assert s.party_id is None


def test_alternate_fields_aliases_and_formats():
    s = run(FakeLLM(X(full_name="Yaven Li", phone="(650) 521-2830", email="YAWEN.LI@example.com")), "m")
    assert s.party_id == "P13"
    assert s.phase == "RESOLVE_INTENT"  # P13 has no claims


def test_declined_fields_and_partial_answers_accumulate():
    llm = FakeLLM(X(full_name="Ava Lopez", declined_fields=["id_last4"]), X(dob="1990-08-21"), X(email="ava.lopez@email.com"))
    s = run(llm, "a", "b", "c")
    assert s.party_id == "P7"
    assert "last 4 digits" not in llm.prompts[1].split("Ask for")[1]  # declined field is not requested again


def test_ambiguous_case_asks_instead_of_guessing():
    s = run(FakeLLM(X(full_name="Margaret Chen", dob="1985-03-15", id_last4="4472", case_type="healthcare", case_month=1)), "m")
    assert s.phase == "RESOLVE_INTENT" and s.case_id is None  # CL-2048 and CL-2011 are both January healthcare


def test_switch_case_and_follow_through_to_email():
    llm = FakeLLM(DEMO, X(case_type="auto"), X(no_more_questions=True), X(email_choice="send"))
    s = run(llm, "demo", "auto?", "done", "yes")
    assert s.discussed == ["CL-2048", "CL-2102"]
    assert s.phase == "ENDED" and s.email["to"] == "margaret@email.com"
    assert "CL-2048" in s.email["body"] and "Upload the pathology report" in s.email["body"]


def test_email_is_only_sent_on_explicit_choice():
    s = run(FakeLLM(DEMO, X(no_more_questions=True), X(), X(email_choice="skip")), "demo", "done", "hmm", "no")
    assert s.email is None and s.email_choice == "skip" and s.phase == "ENDED"


def test_off_topic_strikes_offer_human():
    llm = FakeLLM(*[X(off_topic=True)] * 3)
    s = run(llm, "what is RL?", "what is RL?", "what is RL?")
    assert s.off_topic == 3 and "human representative" in llm.prompts[-1]
    assert "human representative" not in llm.prompts[0]


def test_asking_for_a_human_hands_off():
    s = run(FakeLLM(X(emotion="angry", wants_human=True)), "get me a person")
    assert s.phase == "HUMAN_HANDOFF" and s.handoff["verified"] is False


def test_frustration_gets_empathy_but_no_bypass():
    llm = FakeLLM(*[X(emotion="frustrated", case_status="denied")] * 3)
    s = run(llm, "just tell me", "ridiculous", "come on")
    assert s.phase == "VERIFY_ID" and s.party_id is None
    assert "acknowledgment" in llm.prompts[0] and "offer a transfer" in llm.prompts[-1]


def test_representative_needs_consent():
    rep = X(full_name="Margaret Chen", dob="1985-03-15", phone="650-521-2836",
            caller_role="representative", representative_name="David Chen", relationship="son")
    s = run(FakeLLM(rep), "rep")
    assert s.party_id is None and s.consent == "pending"
    harness.turn(s, "she approved", FakeLLM(X()))
    assert s.party_id == "P9" and s.consent == "approved"


def test_representative_consent_timeout_blocks():
    rep = X(full_name="Margaret Chen", dob="1985-03-15", phone="650-521-2836",
            caller_role="representative", representative_name="David Chen")
    s = run(FakeLLM(rep, *[X()] * 6), *["m"] * 7, consent_scenario="timeout")
    assert s.consent == "timeout" and s.party_id is None


def test_unknown_representative_is_refused():
    rep = X(full_name="Margaret Chen", dob="1985-03-15", phone="650-521-2836",
            caller_role="representative", representative_name="Bob Stranger")
    s = run(FakeLLM(rep), "m")
    assert s.party_id is None and s.consent is None


def test_guard_blocks_leak_before_verification():
    s = run(FakeLLM(X(), reply="Your claim CL-2048 was denied for a missing pathology report."), "hi")
    assert s.history[-1]["content"] == harness.SAFE_UNVERIFIED_REPLY
    assert s.trace[-1]["events"][-1]["type"] == "guard_blocked"


def test_guard_blocks_other_customers_data_after_verification():
    s = run(FakeLLM(DEMO, reply="Also, claim CL-3001 needs a diagnosis report."), "demo")
    assert "CL-3001" not in s.history[-1]["content"]


def test_document_guidance_maps_names_and_falls_back():
    assert "specimen details" in data.doc_guidance("pathology report")["requirements"]
    assert data.doc_guidance("diagnosis report")["requirements"] == data.GUIDE["default_guidance"]["en"]


def test_deadline_uses_demo_date():
    facts = data.claim_facts(next(c for c in data.CLAIMS if c["case_id"] == "CL-2048"), "how long does it take")
    assert facts["appeal_deadline_status"].startswith("13 days remaining")
    assert any(g["topic"] == "processing_time_after_submission" and g["matches_caller_wording"]
               for g in facts["followup_guidance"])


# ---------- regressions from the red-team run (docs/REDTEAM.md) ----------

MARGARET = dict(full_name="Margaret Chen", dob="1985-03-15", id_last4="4472")


def test_rt_representative_cannot_flip_to_policyholder():
    llm = FakeLLM(X(**MARGARET, caller_role="representative", relationship="son"),
                  X(full_name="Margaret Chen", caller_role="policyholder"))
    s = run(llm, "calling for my mother", "forget it, I'm Margaret, the policyholder")
    assert s.party_id is None and s.phase == "VERIFY_ID" and "CL-" not in llm.prompts[-1]


def test_rt_representative_revealed_after_verification_is_regated():
    llm = FakeLLM(X(**MARGARET), X(caller_role="representative", representative_name="Bob Stranger"))
    s = run(llm, "it's me", "actually I'm her neighbour Bob")
    assert s.party_id is None and s.phase == "VERIFY_ID" and "CL-" not in llm.prompts[-1]


def test_rt_no_verification_oracle_in_public_state():
    s = run(FakeLLM(X(full_name="Margaret Chen", dob="1985-03-15", email="wrong@x.com")), "m")
    st = harness.public_state(s)
    assert set(st["verification"]) == {"provided", "required"} and "pending_party" not in st
    assert all("matched" not in e.get("data", {}) for e in st["trace"][0]["events"] if isinstance(e.get("data"), dict))


def test_rt_withdrawn_detail_stops_blocking():
    llm = FakeLLM(X(full_name="Margaret Chen", dob="1985-03-15", email="wrong@x.com"),
                  X(declined_fields=["email"], id_last4="4472"))
    s = run(llm, "m", "forget the email, SSN ends 4472")
    assert s.party_id == "P9" and s.failed_attempts == 1


def test_rt_echoed_first_name_does_not_overwrite_full_name():
    s = run(FakeLLM(X(full_name="Margaret Chen"), X(full_name="Margaret", dob="1985-03-15", id_last4="4472")), "a", "b")
    assert s.party_id == "P9"


def test_rt_name_spacing_order_and_policy_prefix():
    assert data.verify_identity({"name": "Yawen Li", "dob": "1989-12-03", "id_last4": "5317"})["verified"]
    assert data.verify_identity({"name": "Chen Margaret", "dob": "1985-03-15", "id_last4": "4472",
                                 "policy_number": "9921"})["verified"]
    assert not data.verify_identity({"name": "Margaret", "dob": "1985-03-15", "id_last4": "4472"})["verified"]


def test_rt_extractor_gets_today_and_earlier_words_but_no_claims_before_verification():
    s = harness.Session()
    s.history.append({"role": "user", "content": "claim CL-2048 from this year"})
    ctx = harness.extract_context(s, "the one I mentioned")
    assert ctx["today"] == "2026-03-05" and ctx["earlier_caller_messages"] == []  # current message excluded
    assert "claims_on_file" not in ctx


def test_rt_contradicted_claim_id_is_dropped():
    llm = FakeLLM(X(case_id="CL-2048"), X(case_type="dental"), X(**MARGARET))
    s = run(llm, "CL-2048", "actually the dental one", "id")
    assert s.case_id == "CL-1899"


def test_rt_claim_list_has_amounts_after_verification_only():
    llm = FakeLLM(X(**MARGARET, question="How much was paid on each claim?"))
    s = run(llm, "m")
    assert s.phase == "RESOLVE_INTENT" and "$780.00" in llm.prompts[-1]


def test_rt_question_about_email_keeps_offer_open():
    llm = FakeLLM(DEMO, X(no_more_questions=True), X(question="what would be in it?"), X(email_choice="send"))
    s = run(llm, "demo", "done", "what's in it?", "ok yes")
    assert s.phase == "ENDED" and s.email


def test_rt_yes_plus_new_question_answers_first_then_sends():
    llm = FakeLLM(DEMO, X(no_more_questions=True), X(email_choice="send", question="how long after I submit?"),
                  X(no_more_questions=True))
    s = harness.Session()
    for m in ("demo", "done", "yes, and how long?"):
        harness.turn(s, m, llm)
    assert s.phase == "POST_PROCESS" and s.email is None and s.email_choice == "send"
    harness.turn(s, "that's all", llm)
    assert s.phase == "ENDED" and s.email


def test_rt_intent_reset_when_switching_via_disambiguation():
    llm = FakeLLM(DEMO, X(case_status="closed"), X(case_type="dental"))
    s = run(llm, "demo", "what about my closed claim?", "the dental one")
    assert s.case_id == "CL-1899" and s.intent == "general_claim_question"


def test_consent_does_not_transfer_to_a_different_speaker():
    rep = X(full_name="Margaret Chen", dob="1985-03-15", phone="650-521-2836",
            caller_role="representative", representative_name="David Chen")
    llm = FakeLLM(rep, X(), X(representative_name="Bob Stranger", relationship="neighbour"))
    s = run(llm, "rep", "she approved", "actually this is Bob, her neighbour")
    assert s.party_id is None and s.phase == "VERIFY_ID" and "CL-" not in llm.prompts[-1]


# ---------- fixes from interview-prep fact-checking ----------

def test_leaving_a_claim_while_finishing_does_not_loop():
    s = run(FakeLLM(DEMO, X(case_status="closed", no_more_questions=True)), "demo", "that's all, the closed ones")
    assert s.phase == "POST_PROCESS" and s.case_id is None and s.email_offered


def test_email_needs_an_offer_before_a_send_counts():
    llm = FakeLLM(DEMO, X(email_choice="send"), X(email_choice="send"))
    s = run(llm, "demo", "email me the summary")
    assert s.email is None and s.email_offered and s.phase == "POST_PROCESS"
    harness.turn(s, "yes", llm)
    assert s.phase == "ENDED" and s.email


def test_failed_turn_rolls_back_the_session(monkeypatch):
    from fastapi import HTTPException
    from app import server

    class Broken(FakeLLM):
        def respond(self, harness_state, messages):
            raise ValueError("boom")

    monkeypatch.setattr(server.llm, "LLM", lambda api_key=None: Broken(X(**MARGARET)))
    sid = server.new_session(server.NewSession())["session_id"]
    try:
        server.chat(server.ChatIn(session_id=sid, message="Margaret Chen ..."))
        assert False, "expected a 500"
    except HTTPException as e:
        assert e.status_code == 500
    s = server.SESSIONS[sid]
    assert len(s.history) == 1 and s.claimed == {} and s.trace == []  # nothing half-applied


def test_names_in_any_script(monkeypatch):
    assert data.norm_name("José García") == data.norm_name("Jose Garcia")
    assert data.norm_name("García José") in data.name_keys("José García")
    assert data.provided_fields({"name": "陈美玲"}) == ["name"]  # CJK full name, no spaces
    assert data.provided_fields({"name": "Margaret"}) == []      # a Latin first name alone still doesn't count
    rec = {"party_id": "PX", "name": "陈美玲", "policy_number": "POL-1", "dob": "1990-01-01",
           "id_type": "national_id_last4", "id_last4": "1234", "phone": "+15550001111", "email": "c@x.com"}
    monkeypatch.setattr(data, "POLICYHOLDERS", data.POLICYHOLDERS + [rec])
    assert data.verify_identity({"name": "陈美玲", "dob": "1990-01-01", "id_last4": "1234"})["party_id"] == "PX"
    assert data.verify_identity({"name": "陈美玲", "dob": "1990-01-01", "id_last4": "1234"})["verified"]


def test_random_conversations_terminate_and_never_leak_before_verification():
    import random
    rng = random.Random(7)
    pool = [X(), DEMO, X(**MARGARET), X(full_name="Margaret Chen", dob="1985-03-16"), X(declined_fields=["dob"]),
            X(case_status="closed", no_more_questions=True), X(case_type="auto"), X(case_type="dental", case_status="denied"),
            X(no_more_questions=True), X(email_choice="send"), X(email_choice="skip"), X(question="how long?"),
            X(off_topic=True), X(emotion="angry"), X(case_id="CL-3001"), X(case_month=1, question="which one?"),
            X(caller_role="representative", representative_name="David Chen", relationship="son"),
            X(representative_name="Bob Stranger"), X(wants_human=True), X(email_choice="send", question="and how long?")]
    for _ in range(400):
        llm = FakeLLM(*[rng.choice(pool) for _ in range(10)])
        s = run(llm, *["msg"] * 10, consent_scenario=rng.choice(["default", "timeout"]))
        assert s.phase in harness.PHASES + harness.TERMINAL
        assert not s.email or s.email_offered
        for p in llm.prompts:
            if "Identity verified: NO" in p:
                assert "DATA: none" in p and "pathology" not in p and "$" not in p


# ---------- fixes from the adversarial review of the above ----------

def _extra_record(monkeypatch, name):
    rec = {"party_id": "PX", "name": name, "policy_number": "POL-1", "dob": "1990-01-01",
           "id_type": "national_id_last4", "id_last4": "1234", "phone": "+15550001111", "email": "c@x.com"}
    monkeypatch.setattr(data, "POLICYHOLDERS", data.POLICYHOLDERS + [rec])


def test_other_scripts_keep_their_marks_so_different_names_stay_different(monkeypatch):
    _extra_record(monkeypatch, "सीता शर्मा")  # Sita Sharma
    ok = {"phone": "+15550001111", "id_last4": "1234"}
    assert data.verify_identity({"name": "सीता शर्मा", **ok})["verified"]
    assert not data.verify_identity({"name": "सुता शर्मी", **ok})["verified"]  # a different name must not collide
    assert data.norm_name("ヨシダ") != data.norm_name("ヨシタ")
    assert not data.is_full_name("김") and data.is_full_name("김민준") and not data.is_full_name("राम")
    assert data.norm_name("Muñoz") == "munoz" and data.norm_name("Ｍａｒｇａｒｅｔ") == "margaret"


def test_name_correction_that_drops_a_middle_name_is_accepted():
    llm = FakeLLM(X(full_name="Margaret Anne Chen", dob="1985-03-15", id_last4="4472"), X(full_name="Margaret Chen"))
    s = run(llm, "m", "sorry, just Margaret Chen")
    assert s.party_id == "P9"


def test_echoed_cjk_given_name_does_not_replace_full_name():
    s = run(FakeLLM(X(full_name="陈美玲"), X(full_name="美玲")), "a", "b")
    assert s.claimed["name"] == "陈美玲"


def test_consent_does_not_carry_to_someone_sharing_the_reps_surname():
    rep = X(full_name="Margaret Chen", dob="1985-03-15", phone="650-521-2836",
            caller_role="representative", representative_name="David Chen", relationship="son")
    llm = FakeLLM(rep, X(), X(representative_name="Chen", relationship="husband"))
    s = run(llm, "rep", "approved", "this is Chen, her husband")
    assert s.party_id is None and "CL-" not in llm.prompts[-1]
    s = run(FakeLLM(rep, X(), X(representative_name="David", relationship="her son")), "rep", "approved", "David here")
    assert s.party_id == "P9"  # the same rep echoing his first name keeps consent


def test_naming_a_claim_during_wrap_up_with_none_selected_goes_back_to_it():
    llm = FakeLLM(DEMO, X(case_status="closed", no_more_questions=True), X(case_type="dental"))
    s = run(llm, "demo", "that's all, the closed ones", "the dental one")
    assert s.phase == "PROCESS_CASE" and s.case_id == "CL-1899" and "CLAIM FACTS" in llm.prompts[-1]


def test_invalid_structured_output_fails_closed():
    from app import llm as llm_mod

    class BadParse:
        def parse(self, **kw):
            llm_mod.Extraction.model_validate_json('{"full_name": "Marg')  # raises ValidationError

    real = llm_mod.LLM.__new__(llm_mod.LLM)
    real.client = type("C", (), {"messages": BadParse()})()
    assert real.extract({}) == llm_mod.Extraction()
    assert real.summarize([], []).discussed == []


def test_bad_profile_credentials_are_a_401(monkeypatch):
    import anthropic
    from fastapi import HTTPException
    from app import server

    class NoCreds(FakeLLM):
        def extract(self, ctx):
            raise anthropic.CredentialsError("Config file not found at /secret/path")

    monkeypatch.setattr(server.llm, "LLM", lambda api_key=None: NoCreds())
    sid = server.new_session(server.NewSession())["session_id"]
    try:
        server.chat(server.ChatIn(session_id=sid, message="hi"))
        assert False
    except HTTPException as e:
        assert e.status_code == 401 and "/secret/path" not in e.detail


# ---------- final-pass behaviours (escalation, naturalness, trace, intent) ----------

def test_frustration_offer_needs_consecutive_turns():
    llm = FakeLLM(*[X(emotion="frustrated")] * 2, X())
    run(llm, "a", "b", "c")
    assert "offer a transfer" not in llm.prompts[0] and "human representative" in llm.prompts[0]  # alternative from turn 1
    assert "offer a transfer" in llm.prompts[1] and "offer a transfer" not in llm.prompts[2]  # calm turn resets it


def test_frustration_offer_also_after_verification():
    llm = FakeLLM(DEMO, *[X(emotion="angry")] * 2)
    s = run(llm, "demo", "a", "b")
    assert s.phase == "PROCESS_CASE" and "offer a transfer" in llm.prompts[-1]


def test_field_list_is_read_once():
    llm = FakeLLM(X(case_status="denied"), X(full_name="Margaret Chen"))
    run(llm, "a", "b")
    assert "don't read the whole list" not in llm.prompts[0] and "don't read the whole list" in llm.prompts[1]


def test_attempt_countdown_only_on_last_try():
    wrong = [X(full_name="Margaret Chen", dob=f"1985-03-1{d}", id_last4="4472") for d in (6, 7, 8)]
    llm = FakeLLM(*wrong)
    run(llm, "a", "b", "c")
    assert "last try" not in llm.prompts[0] and "last try" in llm.prompts[1]


def test_demo_trace_shows_both_transitions():
    s = run(FakeLLM(DEMO), "demo")
    t = [e["data"] for e in s.trace[-1]["events"] if e["type"] == "transition"]
    assert t == ["VERIFY_ID -> RESOLVE_INTENT", "RESOLVE_INTENT -> PROCESS_CASE"]


def test_intent_flags_matching_guidance():
    claim = next(c for c in data.CLAIMS if c["case_id"] == "CL-2048")
    g = {x["topic"]: x for x in data.claim_facts(claim, "how long", intent="denial_question")["followup_guidance"]}
    assert g["submission_timing"]["matches_intent"] and not g["submission_method"]["matches_intent"]


def test_regate_trims_earlier_claim_talk_from_responder_context():
    llm = FakeLLM(X(**MARGARET), X(caller_role="representative", representative_name="Bob Stranger"),
                  reply="Your claim CL-2048 was denied.")
    s = run(llm, "it's me", "actually I'm her neighbour Bob")
    assert s.party_id is None and "CL-2048" not in json.dumps(llm.seen[-1])


def test_empty_summary_does_not_produce_blank_email_sections():
    class Empty(FakeLLM):
        def summarize(self, facts, transcript):
            return EmailSummary(discussed=[], next_steps=[])
    s = run(Empty(DEMO, X(no_more_questions=True), X(email_choice="send")), "demo", "done", "yes")
    assert "(none recorded)" in s.email["body"]
