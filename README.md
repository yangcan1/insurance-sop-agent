# Insurance Claims SOP Agent

**🎥 Demo video (4 min): [watch here](https://drive.google.com/file/d/12VzFdR5fcOxpFE4-Srud-9FxQ4YAt7wE/view?usp=sharing)**

A claims-support chat agent that follows a fixed business workflow but still talks naturally.

**Core idea: code owns the workflow, the LLM owns the language.** A small deterministic harness enforces the phase
order, the identity gate, consent, and allowed actions. The LLM is used for what it's good at: reading messy
caller language, and phrasing grounded answers with empathy.

```
VERIFY_ID  ->  RESOLVE_INTENT  ->  PROCESS_CASE  ->  POST_PROCESS
 (strict)       (flexible)          (flexible,        (strict consent
                                     grounded)          for email)
        any phase ─> HUMAN_HANDOFF  (asked for a human / 3 failed verifications; after consent timeout or
                                     repeated off-topic/frustration the agent offers a human)
```

## Quick start

You need an Anthropic API key (or an auth token).

### Docker (prebuilt image, amd64 + arm64)

```bash
docker run --rm -p 8000:8000 -e ANTHROPIC_API_KEY=sk-ant-... cannnnn2/insurance-sop-agent
```

Or build it yourself from this repo:

```bash
docker build -t insurance-sop-agent .
docker run --rm -p 8000:8000 -e ANTHROPIC_API_KEY=sk-ant-... insurance-sop-agent
```

Open http://localhost:8000. You can also start the container **without** a key and paste one into the key field
in the UI; it is sent per request and never stored. `-e ANTHROPIC_AUTH_TOKEN=...` works too.

A prebuilt multi-arch image (amd64 + arm64) can be produced with:

```bash
docker buildx build --platform linux/amd64,linux/arm64 -t cannnnn2/insurance-sop-agent:latest --push .
```

### Local (Python 3.11+)

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env        # put your key in .env
uvicorn app.server:app --port 8000
```

### Configuration

| Variable | Default | Meaning |
|---|---|---|
| `ANTHROPIC_API_KEY` / `ANTHROPIC_AUTH_TOKEN` | – | Model credentials (or enter a key in the UI) |
| `MODEL` | `claude-opus-5` | Any Claude model id, e.g. `claude-sonnet-5` for lower latency/cost |
| `EFFORT` | `low` | Reasoning effort per call; set empty for models without effort support (Haiku 4.5) |
| `DEMO_TODAY` | `2026-03-05` | Fixed "today". The fixture appeal deadlines are Mar/Apr 2026, so a real clock would make every deadline "passed" |
| `PORT` | `8000` | Container port (e.g. 7860 for Hugging Face Spaces) |

## Using the demo UI

- Left: the chat. Sample chips under the input pre-fill common test messages (demo case, frustrated caller,
  off-topic, wrong DOB, representative, prompt injection, follow-ups, email yes/no, ask for a human).
- Right: an **operator/debug panel** (not something a caller would see) showing the live harness state: phase,
  identity gate progress, what's been remembered, counters, consent, the mock email outbox, the handoff ticket,
  and a trace of the last turn (extraction, tool calls, transitions, instructions given to the responder, guard
  blocks).
- The header selects the consent scenario for representative callers (`default` approves on the next check,
  `timeout` never does).

Suggested walkthrough: click **Demo case** → send; then **Documents?**, **How long?**, **Done**, **Send email**.

## How a turn works

```
caller message
  1. Extractor (LLM, structured output)  -> identity fields, case hints, intent, emotion, off-topic,
                                            wants-human, email choice ... every turn, in every phase
  2. Harness (plain Python)              -> merge into memory, verify identity against records, count
                                            attempts/strikes, pick the phase transition, build instructions
  3. Responder (LLM)                     -> natural reply from the instructions + ONLY the data this phase allows
  4. Guard (plain Python)                -> blocks claim data that isn't this caller's (all claim data before
                                            verification); replaces the reply with a safe one
```

### Different freedom per phase

| Phase | Who decides | What the LLM can see | Leaves when |
|---|---|---|---|
| VERIFY_ID | Code | **No claim or account data at all** — only "N of 3 details received" | ≥3 of 5 PII fields match one record, none contradicts it (+ consent for representatives) |
| RESOLVE_INTENT | LLM interprets, code bounds | This caller's claim list + remembered hints | Exactly one claim matches the caller's words |
| PROCESS_CASE | LLM, grounded | The selected claim + document/follow-up guidance from the fixtures | Caller has no more questions |
| POST_PROCESS | Code (consent gate) | Session summary | Caller explicitly chooses send or skip |

### Key design decisions

- **Data isolation beats prompt rules.** During VERIFY_ID the responder's context contains no claim data, so a
  jailbroken or confused model has nothing to leak. The prompt rule and the output guard are backups.
- **Verification is code, not judgment.** Fields are normalized (name order, DOB formats, phone formats, email
  case, fixture name/email/phone aliases, SSN or national-ID last 4). Any contradicting detail blocks verification,
  and the result never says *which* detail failed. Policy number helps lookup but does not count toward the 3.
  Three failed attempts hand off to a human.
- **Memory is automatic.** The extractor reads every message against the full schema regardless of phase, so
  "I'm calling about my denied healthcare claim from January" said during verification becomes case hints
  (`healthcare / denied / month 1`). After verification those hints pick **CL-2048** directly. Note that Margaret has
  *two* January healthcare claims (CL-2048 2026 denied, CL-2011 2025 closed); "denied" is what disambiguates, and
  without it the agent asks.
- **Bounded paths.** Intents come from the fixture guidance (`denial_question`, `status_inquiry`,
  `document_submission`, `next_steps`, `general_claim_question`); claim choices are limited to this caller's claims.
- **Grounded answers.** PROCESS_CASE gets the claim record, deadline math against `DEMO_TODAY`, per-document
  requirements and alternatives, and every applicable follow-up rule (with keyword hits flagged). Anything not
  covered falls back to the fixture's fallback text or a human.
- **Representatives and consent.** `representatives.json` + `consent_scenarios.json` imply a third-party caller
  flow: the policyholder's details must verify, the caller must be the representative on file, and the
  policyholder's consent must be approved before anything is disclosed. Timeout means no disclosure and an offer
  of a human.
- **Scope and escalation.** Off-topic requests are declined politely; on the 3rd strike the agent offers a human.
  Explicit requests for a human are honored immediately with a handoff ticket carrying context.
- **Emotional support.** The extractor tags emotion each turn; the harness tells the responder to acknowledge
  first, explain why the step matters (verification protects health and financial data; consent protects the
  policyholder), and offer alternatives (other ID fields, a human). After repeated frustration during verification
  it proactively offers a human. It never relaxes a gate.
- **Email summary.** Offered in POST_PROCESS to the masked email on file; sent only on an explicit "yes" (mock
  outbox shown in the panel). The claim-status lines are built by code from the record; the LLM only writes the
  "what we discussed" and "next steps" bullets from the transcript and claim facts.

### Fixture details the implementation accounts for

- Claims list `pathology report` / `office note` but the guideline keys are `original pathology report` /
  `treating provider office note`; `diagnosis report` has no entry → matched by containment, else default guidance.
- `Ya Wen Li` has the alias `Yaven Li` (looks like a speech-to-text error) and alternate email/phone.
- Ma Tian and Ya Wen Li use `national_id_last4`, not SSN.
- Ava Lopez and Ya Wen Li have no claims.

## Tests

```bash
pip install pytest
pytest                              # 45 deterministic tests incl. a 4,000-turn fuzz, fake LLM, no API key
python -m tests.live_scenarios      # 10 end-to-end conversations against the real model (a few cents each)
```

The deterministic tests check the harness itself: the demo case, memory across the verification boundary,
**that no claim data reaches the model before verification**, 2-of-5 not enough, contradicting details, aliases and
formats, declined fields, ambiguity, case switching, email only on explicit choice, off-topic strikes, human
handoff, frustration without bypass, representative consent (approve / timeout / unknown rep), the output guard,
document-name mapping, and deadline math, plus regression tests for every red-team and review finding and a
randomized test (400 conversations x 10 turns) asserting termination and no claim data before verification.

The live scenarios: `demo`, `hint_first`, `frustrated`, `off_topic`, `rep`, `wrong_then_right`, `injection`,
`asr_alias`, `other_insurer`, `refusal`.

### Red-team testing

Six attacker agents (identity gate, emotional callers, scope, memory, grounding, email/consent) and a code
reviewer attacked the live agent. Every finding was then verified independently by replaying it through the
harness. **The model never disclosed claim data before verification.** The confirmed issues (3 critical,
12 major) were harness-logic gaps around who is speaking and what the caller can retract, for example a
representative claiming to be the policyholder mid-call. All are fixed and pinned by regression tests.
Full report with the conversations: [docs/REDTEAM.md](docs/REDTEAM.md).

## Limitations / next steps

- Sessions are in memory in one process; use Redis or a database for multiple workers.
- Email and human handoff are mocked (outbox and ticket shown in the UI).
- The UI has no login; for a public deployment keep "bring your own key" or add auth.
- The output guard matches exact claim tokens (ids, dates, amounts, document names). It is defense in depth;
  the real protection is that the model never has the data before verification.
- Two model calls per turn (about 3–8 s with `claude-opus-5` at low effort). `MODEL=claude-sonnet-5` is faster
  and cheaper.

## Layout

```
app/data.py       fixtures, identity verification, claim matching, guideline retrieval, output guard (no LLM)
app/harness.py    session state + SOP state machine: extract -> apply -> respond -> guard
app/llm.py        extractor (structured output) and responder prompts, email summarizer
app/server.py     FastAPI: /api/session, /api/chat, static UI
app/static/       single-page chat UI with the harness debug panel
fixtures/         provided sample data
tests/            deterministic tests + live scenario runner
```
