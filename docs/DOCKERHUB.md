# Insurance Claims SOP Agent

**🎥 Demo video: [watch here](https://drive.google.com/file/d/12VzFdR5fcOxpFE4-Srud-9FxQ4YAt7wE/view?usp=sharing)**
(Recorded on the first submission; v2 rewords some replies, defaults to `claude-sonnet-5` and adds sample chips. The workflow shown is unchanged.)

![Demo: the frustrated caller from the brief during identity verification, with the harness panel on the right](https://raw.githubusercontent.com/yangcan1/insurance-sop-agent/main/docs/img/demo-verify.jpg)

A claims-support chat agent that follows a fixed business workflow but still converses naturally.
**Code owns the workflow; the LLM owns the language.**

```
VERIFY_ID -> RESOLVE_INTENT -> PROCESS_CASE -> POST_PROCESS
```

- **Strict identity gate:** at least 3 of 5 identity details must match. No claim data reaches the model before verification.
- **Cross-phase memory:** "my denied healthcare claim from January", said during verification, picks the claim right after.
- **Grounded answers** from claim records and document guidelines only.
- **Empathy and de-escalation**, scope limits, and escalation to a human.
- **Email summary** sent only on explicit consent; representative callers need policyholder consent.
- Chat UI with a live **harness debug panel** showing the phase, gates, memory, and per-turn trace.

## Run

```bash
docker run --rm -p 8000:8000 -e ANTHROPIC_API_KEY=sk-ant-... cannnnn2/insurance-sop-agent
```

Open http://localhost:8000. You can also start it without `-e ANTHROPIC_API_KEY` and paste a key into the UI
(the UI field takes an API key; for an auth token use `-e ANTHROPIC_AUTH_TOKEN=...`). Keys are sent per request
and never written to disk or logs.

**Walkthrough (about 11 model calls):** **Demo case** → **Documents?** → **How long?** → **Done** → **Send email**.
Bonus paths: **Frustrated** → **Wrong DOB** → **Human**; `consent: timeout` → **Representative**; **Ambiguous** →
**The denied one**. The first message of a fresh deployment takes a few seconds longer (one-time schema compile).

| Variable | Default | |
|---|---|---|
| `ANTHROPIC_API_KEY` / `ANTHROPIC_AUTH_TOKEN` | – | model credentials (or enter a key in the UI) |
| `MODEL` | `claude-sonnet-5` | verified default; `claude-opus-5` also runs (longer replies, ~2.5x cost) |
| `EFFORT` | `low` | reasoning effort (dropped automatically for Haiku) |
| `DEMO_TODAY` | `2026-03-05` | fixed "today" so the fixture deadlines are still open |
| `PORT` | `8000` | container port |

Images: `linux/amd64` and `linux/arm64`.

## Source, design notes, tests, red-team report

https://github.com/yangcan1/insurance-sop-agent
