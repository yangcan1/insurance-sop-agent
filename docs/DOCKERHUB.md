# Insurance Claims SOP Agent

**🎥 Demo video (4 min): [watch here](https://drive.google.com/file/d/12VzFdR5fcOxpFE4-Srud-9FxQ4YAt7wE/view?usp=sharing)**

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

Open http://localhost:8000. You can also start it without `-e ANTHROPIC_API_KEY` and paste a key into the UI.
The key is sent per request and never stored.

| Variable | Default | |
|---|---|---|
| `ANTHROPIC_API_KEY` / `ANTHROPIC_AUTH_TOKEN` | – | model credentials (or enter a key in the UI) |
| `MODEL` | `claude-opus-5` | e.g. `claude-sonnet-5` for lower latency/cost |
| `EFFORT` | `low` | set empty for models without effort support |
| `DEMO_TODAY` | `2026-03-05` | fixed "today" so the fixture deadlines are still open |
| `PORT` | `8000` | container port |

Images: `linux/amd64` and `linux/arm64`.

## Try it

Click **Demo case** under the chat input, press Enter, then try **Documents?**, **How long?**, **Done**, and
**Send email**. Other chips: frustrated caller, off-topic question, wrong DOB, representative caller, prompt injection.

## Source, design notes, tests, red-team report

https://github.com/yangcan1/insurance-sop-agent
