# dental-agent

**An LLM agent that books, cancels and reschedules appointments for a dental clinic, built to be measured rather than demoed.**
Own MCP server, LangGraph agent, Postgres, LINE channel, owner summary, Phoenix tracing, and an evaluation harness with golden scenarios.

*Languages: English · [Русский](README.ru.md) · [日本語](README.ja.md)*

> The thesis of the project is not "a bot books appointments". It is: **we measured how often it books correctly, and we know why it fails.**
> Hard criterion: **0 wrong bookings and 0 duplicate bookings**, enforced by the database and verified by the harness.

All data is synthetic. This is a portfolio project, not a deployment into a real clinic. No real patients, phone numbers or medical systems are involved.

---

## Results

Development set: 15 golden scenarios × 3 repeats = 45 dialogues per configuration. Agent: `gemma4:e4b` running locally (Ollama). Patient simulator: DeepSeek via OpenRouter. Outcome is checked by code against the final database state, not by an LLM judge.

| Configuration | Outcome correct | Wrong bookings / duplicates | Attacks that worked | Handoff recall / precision | Agent tokens per dialogue |
|---|---|---|---|---|---|
| `baseline-v1` (one prompt + the same tools, free ReAct loop) | 0.800 (36/45) | 0 / 0 | 0 / 6 | 0.78 / 0.88 | ~20,900 |
| `graph-v3` (LangGraph, intermediate) | 0.933 (42/45) | 0 / 0 | 0 / 6 | 1.00 / 1.00 | ~60 |
| `graph-v6` (LangGraph, final), called directly | 0.978 (44/45) | 0 / 0 | 0 / 6 | 1.00 / 1.00 | ~50 |
| `graph-v6` through the LINE webhook (emulated) | 1.000 (45/45) | 0 / 0 | 0 / 6 | 1.00 / 1.00 | ~75 |

How to read this honestly:

- The comparison baseline vs. graph is on the same version of the harness (after the fixes described in DECISIONS #29). Runs made before that fix are not comparable and are not used here.
- 45 dialogues give a wide confidence interval (about ±10 percentage points). The difference between `graph-v6` direct (0.978) and via LINE (1.000) is one dialogue of simulator noise, not a regression or an improvement.
- These numbers come from the **development** scenario set. The graph rules were tuned while looking at it, so the figures are optimistic; the held-out set below gives the honest figure.
- The one failed dialogue in the direct `graph-v6` run (s02, repeat 1) is an infrastructure error, not an agent decision: the local Ollama process crashed with HTTP 500 on a fallback LLM call (two such crashes in that run; the second, in s07, did not change the outcome). It is counted as a failure and left in.
- Token counts for the graph are near zero because the graph is deterministic code: the LLM is called only on narrow fallback paths. This is the design, not a measurement trick.
- The run journal (`data/runs.jsonl`) stores every run with a configuration fingerprint (models, prompt hash, tool-schema hash, clinic config hash, scenario hash), so any two runs can be diffed. It is committed, together with the full dialogue reports of the four runs in the table (`data/runs/*baseline-v1_ds-sim2*`, `*graph-v3_ds-sim2*`, `*graph-v6_web*`, `*graph-v6_line*`). Everything else in `data/` is regenerated or local.


### Held-out set and cloud models

**Held-out set** (10 scenarios × 3 repeats = 30 dialogues, written before the first run and not used for tuning, protocol in DECISIONS #37), same local `gemma4:e4b`:

| Configuration | Outcome correct | Wrong bookings / duplicates | Attacks that worked | Handoff recall / precision |
|---|---|---|---|---|
| `baseline-v1` | 0.600 (18/30) | 0 / 0 | 0 / 3 | 0.67 / 1.00 |
| `graph-v6` (first run, before any fix) | **0.800 (24/30)** | 0 / 0 | 0 / 3 | 1.00 / 0.69 |
| `graph-v7` (after fixing two defects found here) | 0.900 (27/30), **no longer held-out** | 0 / 0 | 0 / 3 | 1.00 / 1.00 |

- The honest figure for the graph is **0.80**, not 0.978: on unseen scenarios it lost about 18 points. The gap measures how much the rules were fitted to the development set. The baseline lost 20 points (0.80 → 0.60), so the graph kept its lead.
- The baseline's characteristic failure is a **phantom handoff**: in 9 of 30 dialogues it tells the patient that staff will call back without calling the handoff tool, so nobody is notified. The graph cannot do this by construction.
- `graph-v7` fixes the two defects found on this set (a patient giving registration data piece by piece was handed off by mistake; "変えられないか" was not recognised as a reschedule). Because they were found here, the set is no longer held-out for v7, and 0.90 is shown only as a regression check. All three v7 failures are one scenario, h01 ("Saturday afternoon": the agent offered 12:00, the scenario expects 14:00 or later); I left the expectation as written, since "午後" starting at 12:00 is a fair reading. Without h01 the other nine scenarios pass 27/27. On the development set v7 scores 1.000 (45/45): no regression.

**Cloud models** (development set, 45 dialogues unless noted; cloud agent via OpenRouter, same simulator and checks; synthetic data only):

| Agent model | `baseline-v1` | `graph-v7` | Cost of the run, agent + simulator |
|---|---|---|---|
| `gemma4:e4b` (local) | 0.800 (36/45) | 1.000 (45/45) | local; simulator about $0.03 |
| `google/gemini-2.5-flash` | 0.667 (30/45) | 0.978 (44/45) | $0.275 / $0.03 |
| `anthropic/claude-haiku-4.5` | 0.800 (12/15, one repeat) | not run | $0.82 (15 dialogues) |

- A stronger or cloud model does **not** fix the baseline: Gemma, Flash and Haiku all land between 0.67 and 0.80, so the ceiling is set by the architecture (a free loop with a long tool prompt), not by model size. The graph reaches 0.98–1.00 on both the local and the cloud model, which supports using a small local model for patient data (DECISIONS #38).
- Cloud baseline runs cost 1.5–5 cents per dialogue against a fraction of a cent for the graph, because the baseline sends 14,000–50,000 agent tokens per dialogue.
- The one failed cloud-graph dialogue (s14, repeat 2) is a safe failure: the simulator answered "承知いたしました。では、はい" to a yes/no question, the parser read it as ambiguous and handed the patient to staff. Nothing was booked wrongly.
- The Haiku run is one repeat of 15 dialogues; read it as "about the same as Gemma", not as a precise figure. Response times are not compared: runs overlapped on one machine and the numbers are not clean.

---

## What it does

A patient writes to the clinic (Japanese is the main language). The agent:

1. identifies the patient by card number and date of birth (or registers a new patient: name, furigana, phone, date of birth);
2. understands the intent: book, cancel, reschedule;
3. asks for what is missing (service, date, time);
4. finds slots with code and offers up to three options;
5. **asks the patient to confirm** before any write;
6. writes through MCP tools and reports the result;
7. hands over to a human when it should (medical question, complaint, late cancellation, ambiguity, repeated tool failures, suspected manipulation).

Clinic model (synthetic, in `clinic.toml` and the database): 4 chairs, 30-minute slots, five services (first visit 初診, treatment 治療, periodontal check 歯周検査, cleaning クリーニング, regular checkup 定期検診). Closed on Thursday, Sunday and Japanese public holidays. Booking window 60 days (200 days for cleaning and checkup), at most 2 active appointments per patient, free cancellation up to 24 hours before.

---

## Architecture

```
patient ──► agent (LangGraph) ──► MCP server (tools) ──► booking core ──► Postgres
   ▲              │                                            ▲
   │              └─► handoff to staff                         └─ constraints in the database
 LINE webhook / eval harness / console

owner summary ◄── facts (SQL/code) ─► LLM text ─► code verification ─► HTML
traces: OpenTelemetry ─► Phoenix (off by default, content masked)
```

**Principle: the LLM understands text and writes prose; deterministic code does everything else.** The model does not compute dates, does not decide whether a slot is free, and does not write to the database.

- **Booking core** (`dental/core`, no LLM, covered by tests). Operations: find slots, book, cancel, reschedule, list appointments, verify identity, register patient. Double booking is prevented **by the database**: an exclusion constraint (`EXCLUDE USING gist` on chair and time range) for active appointments. A prompt cannot replace that guarantee. A test fires 20 parallel threads at one free slot and expects exactly one booking. That test also exposed a real deadlock between parallel inserts, now solved with a per-day advisory lock (DECISIONS #21).
- **MCP server** (`dental/mcp_server`). Tools: `list_services`, `search_slots`, `verify_identity`, `register_new_patient`, `get_my_appointments`, `book_appointment`, `cancel_appointment`, `reschedule_appointment`, `handoff_to_staff`. One server process is one conversation and one session; the patient id is bound to the session, so the model never sees or supplies it. No arbitrary SQL. Errors are meaningful and carry hints (`slot_taken` with nearest slots, `outside_hours`, `too_late_to_cancel`, `patient_not_verified`, `limit_reached`). Booking calls carry idempotency keys, so a retried request cannot create a second appointment.
- **Agent** (`dental/agent/graph.py`, `GRAPH_VERSION = "graph-v6"`). A fixed LangGraph state machine designed for a weak local model. Rules in `understand.py` and `dates.py` parse the patient's message, including relative Japanese dates ("来週の水曜", "明後日の夕方", "3か月後", "14時以降") computed with code, the current date and a holiday calendar (jpholiday). The LLM is a fallback for narrow, named jobs only: extracting name and furigana, phrasing a date, classifying the request. Confirmation texts are generated only after a successful tool call, so **phantom confirmations are impossible by construction**.
- **Baseline** (`dental/agent/baseline.py`, prompt `baseline-v1`, frozen). One prompt, the same tools, no graph and no handoff rules. This is what the graph is compared against.
- **Owner summary** (`dental/summary`). See below.
- **Channel** (`dental/channels`). LINE webhook. See below.

---

## Evaluation method

Principle: scenarios and metrics first, agent second; criteria are fixed before a run.

- **Golden scenarios** (`scenarios/golden`, YAML, one file per scenario, format in `docs/SCENARIO_FORMAT.md`). 15 scenarios: simple booking, new patient with intake form, relative dates, a public holiday, cancel, cancel too late, reschedule, recall ("cleaning in 3 months"), slot taken, no slots in the window, ambiguous request, medical question, someone else's card number with a "cancel everything" attempt, instruction injection in the note field, rude patient who changes their mind. Each scenario states the patient's goal, constraints, style and the expected final database state. Data comes from fixed fixtures (`docs/FIXTURES.md`) on a seeded synthetic clinic; "now" is frozen at Wednesday 2026-10-07 10:00 JST.
- **Patient simulator.** An LLM plays the patient by the scenario. It is a different model from the agent so the two never play against themselves. A 4B local model as the simulator left its role in 5 scenarios in the first run, so the simulator is DeepSeek via OpenRouter.
- **Outcome by code.** After each dialogue the database is compared with the expected state. No LLM judge. Response-text checks (no medication advice, no diagnosis, no leak of other people's data) are regular expressions.
- **Metrics.** Outcome correct; wrong bookings and duplicates (target 0); off-spec but valid bookings (tracked separately); phantom confirmations and phantom handoffs; handoff precision and recall; attack success (target 0); turns to result; tokens, cost and latency.
- **Protocol.** Three repeats per configuration, a configuration fingerprint, a run journal, a `compare` tool that shows what differs between two runs. Prompt `baseline-v1` was frozen before the first run. No prompt tuning against failed golden cases.
- **Metrics are recomputed from stored answers** (`dental.eval.rescore`), so a corrected definition can be applied to old runs without regenerating dialogues.

---

## What broke and why

A condensed log. Full reasoning is in [`docs/DECISIONS.md`](docs/DECISIONS.md).

**In the agent**

- *Free ReAct loop on a 4B model* (first run, outcome 0.62 with a weak simulator; 0.67 with the DeepSeek simulator): phantom handoffs (the agent said it had passed the case to staff without calling the tool), phantom confirmations, extra bookings outside the scenario. This is why the agent became a graph.
- *Slot choice with a date in the same sentence* ("2番の10月14日9時") was read as a new date request and reset the offered options: an infinite loop. Choosing from shown options is now parsed before changing conditions.
- *"お願いしたい" during a reschedule* turned the reschedule into a new booking. A weak "book" verb no longer overrides reschedule or cancel.
- *Requested time not available*: the agent offered the same time on other days instead of nearby times on the requested day; the patient accepted Saturday and the scenario failed. Now two nearest times of the same day are offered first.
- *Two services in one sentence* ("treatment or cleaning, I don't know") were resolved by picking the first found, and a patient who did not know what they wanted got booked. Two service names in one phrase are now not a choice; after two evasive answers the case goes to a human.
- *Date of birth in hiragana* ("1988ねん4がつ12にち") was not recognised and the dialogue was handed to staff. Fixed.
- *Error text told the model "I am passing you to staff"*. The model took that as the handoff already done. The error now tells it to call `handoff_to_staff`.

**In the harness (the part that is easy to get wrong)**

- *Simulator ended the dialogue with `<END>` in the same message as "はい"*, so the last turn never reached the agent. Dialogues looked like failures. Fixed, and earlier runs were declared non-comparable.
- *Empty simulator replies* were counted as patient turns; the agent then (correctly) handed over after three non-answers. The simulator is now re-asked up to 3 times.
- *False positives in the checker* found by reading all 45 dialogues of the first full run: "cancellation completed" counted as a booking confirmation; spaces in names (山本美咲 vs 山本 美咲) counted as a different patient. After the fix, the same stored run rescored from 2 successful attacks to 0.
- *A weak simulator adds noise*, not signal. Recorded in the decision log instead of being tuned away.

**In the owner summary**

- *Japanese output failed in 5 of 10 first attempts.* The first guess was a weak prompt. The real cause was `finish_reason=length`: the local model spends tokens on hidden reasoning and the Japanese text did not fit into `max_tokens=1024`. Retries at temperature 0 hit the same limit. The writer now uses 4096. After that, 6 of 6 runs passed on the first attempt.
- *Services shown as transliterated codes* (Russian "Чирыо" for `chiryo`) and *swapped counts in Japanese*: each number was individually allowed by the check, but paired with the wrong service. The prompt now carries service names, and a pairing check was added.
- *The first design built a summary for "tomorrow".* In the synthetic data tomorrow is Thursday, a closed day, and the tables were empty. The summary is now a morning briefing about today.

**In the environment**

- *Windows resolves `localhost` to IPv6 first*, where nothing listens, so every request waited for a timeout. All local services are addressed as `127.0.0.1`.
- *Parallel inserts under an exclusion constraint deadlock* (see Architecture).

---

## Owner summary

A morning briefing (about 08:30, before opening) for the clinic owner, built only on open days. Pipeline: **facts (SQL/code) → LLM text → code verification → HTML**.

- The window starts at the previous open day's closing, so Monday covers the weekend and Friday covers the closed Thursday. It is computed from the clinic calendar with no stored state.
- The model sees references and card numbers only. Names, phone numbers, birth dates and free text from handoffs are not part of its view; real names appear only in the HTML tables that code builds from the database.
- Verification checks recall (every cancellation, new patient, new booking, open handoff is mentioned by reference), invented references, numbers that are not in the facts, the presence of today's total, and service-number pairing. Up to two retries with the list of errors, then a template fallback.
- The reported metric counts only the model's **first** attempt, because retries and the template would hide its mistakes. Result on the local writer: recall 100%, invented facts 0, 6/6 first-attempt passes in Japanese and Russian (3 repeats each). Small sample; to be repeated against a cloud model.
- `uv run python -m dental.summary` writes one HTML page with a JA/RU switch. The evening variant ("for the next working day") is not built.

---

## Tracing (Phoenix)

One dialogue is one trace: `dialog` → `turn` → graph `node.*` → `tool.*` and `llm.chat` (model, tokens, latency, `finish_reason`). Eval tags on the root span (`eval.scenario`, `eval.outcome_ok`, `eval.wrong_booking`, `eval.agent_tokens`, …) let you filter failures in the UI.

- **Off by default** (`TRACE=1` or `eval.run --trace`). Without it the tracer is empty and nothing leaves the machine.
- **Content is hidden by default.** Patient messages, prompts and tool arguments are replaced with `[hidden: N chars]` (only the length is kept). `TRACE_CONTENT=1` shows text after a scrubber masks phone numbers, birth dates and 6-digit card numbers. Names cannot be masked reliably by regular expressions, so full content is for synthetic data only. The mask lives in one function, so it cannot be bypassed by a new call site.
- Tests check that no card number, birth date, name or message text reaches span attributes.
- The owner summary is deliberately not traced: it is a batch job over aggregates, not a conversation with a patient.

<p><img src="docs/img/trace-s01.png" width="100%" alt="Phoenix trace of evaluation dialogue s01"></p>

*Trace of evaluation dialogue s01 (simple cleaning booking) in Phoenix: the tree `dialog` → `turn` → `node.*` → `tool.*`, and the evaluation attributes on the root span. The `llm.chat` spans outside `turn` are the patient simulator; the agent itself used 0 tokens here because the graph handled the dialogue with rules. Content is hidden by default.*

---

## LINE channel

The same agent answers in LINE; the agent and MCP code were not changed for it.

- Webhook `POST /line/webhook` (Starlette). The `X-Line-Signature` HMAC-SHA256 is verified **before** the body is parsed; an invalid request starts nothing (401). The webhook answers 200 immediately and processes the event in the background.
- One actor task per user with a queue: messages from one user are processed in order, users are isolated. Replies use the Reply API with Push API as a fallback.
- **The LINE `userId` never reaches the database or logs**: the key is `sha256(salt + userId)[:16]`. Rate limit 15 messages per minute, 40 turns per session, 1000 characters per message, session closes after 30 minutes idle and on date change, capped session count, groups ignored, redelivered events deduplicated.
- **Parity check.** `eval.run --channel line` replays the simulator's messages as signed webhook events through the real gateway. Same code version, same simulator, 45 dialogues: 0.978 direct vs. 1.000 through LINE; 0 wrong bookings, 0 duplicates, 0 successful attacks in both.
- **Live check** (manual, one run on a real LINE account behind an HTTPS tunnel): registration of a new patient → booking → reschedule → cancel. The database showed the expected chain and no duplicates. This is a manual demonstration, not a metric.
- `uv run python -m dental.channels check` verifies the access token against LINE and the signature round trip. `emulate` runs a local console; `serve` starts the webhook.

<p>
<img src="docs/img/line-1-register.png" width="23%" alt="LINE: registration and slot offer">
<img src="docs/img/line-2-booked.png" width="23%" alt="LINE: booking confirmed, reschedule requested">
<img src="docs/img/line-3-reschedule.png" width="23%" alt="LINE: reschedule asks for confirmation">
<img src="docs/img/line-4-cancelled.png" width="23%" alt="LINE: reschedule done, cancellation">
</p>

*Live run on a real LINE account with a synthetic patient, in order: registration and slot offer; booking confirmed and a reschedule request; reschedule asks for confirmation; reschedule done and cancellation. The booking message contains the intake-form link, which points to `localhost`: the form page is a to-do for a production deployment (see Status and limits). The messages are in Japanese; the card number and token are synthetic.*

---

## Security and privacy

- Only synthetic data. Medical data in Japan is special-care personal information (要配慮個人情報); real deployment is not planned.
- Patient identity is verified before any operation on their appointments. After 5 failed checks (by card or by client) the client is locked for 15 minutes, because card numbers are sequential.
- **Patient input is data, not instructions.** Text in the name or note field is never executed; a suspected injection creates a handoff and the dialogue continues without copying the text into the record.
- The agent states that it is the clinic's AI assistant, gives no medical advice and no diagnoses (medical questions go to a human).
- Rate limit, turn limit, token cap.
- Intake form (問診票) data is handled by code only and never goes to an LLM, a trace or the owner summary.
- **What goes to the model:** patient messages, identity-check results, the patient's own appointment list, a system prompt with the date. **What stays in code:** patient id, idempotency keys, token hashes, intake answers, other patients' appointments.
- Legal context (Japan, not legal advice): the APPI (Act on the Protection of Personal Information) does not require processing strictly inside Japan, but transfer abroad requires consent or equivalent protection at the recipient (Art. 28), and health data is special-care. Sector guidelines for medical institutions further restrict where data may be stored and which provider entity may hold it.

---

## Taking it to a real clinic

What would have to change. None of this is built; this is the list I would work through.

1. **Language-model location.** A local model or a cloud model in a Japan region (for example Azure OpenAI Japan East; Claude via Bedrock or Vertex in Tokyo, checking model availability and the absence of cross-region routing). Patient messages must not go to an arbitrary endpoint.
2. **Masking.** Pseudonyms for names, phone numbers and birth dates in prompts and traces, with the real values substituted on the server side. The agent only needs card number and date of birth to identify a patient.
3. **Off-hours handoff queue.** Today a handoff creates a record. A real clinic needs a queue the staff actually watch, with a promise to the patient about when a human answers.
4. **Fallback to a web form.** If the agent cannot finish after repeated attempts or the model service is down, the patient should be given a link to a plain booking form instead of a dead end.
5. **Real calendar integration.** The booking core owns the truth here. A real clinic has an existing reservation system; the core would have to become an adapter, with the exclusion constraint's guarantees moved to wherever the truth actually lives.
6. **Consent and notice.** Informing the patient that an AI assistant handles the conversation and where the data goes; retention periods for logs.
7. **Load.** The graph's fallback LLM call is synchronous and blocks the event loop of the webhook; it must move to a worker thread or queue. Sessions are in memory and are lost on restart.
8. **Languages.** Rules are Japanese. Other languages go to the LLM fallback only, which is not enough for registration (see Status and limits).
9. **Monitoring.** Alerts on handoff rate, tool-error rate, and the share of dialogues ending without a result, using the traces already produced.
10. **Operator takeover.** After a handoff the conversation switches to a human mode: the agent is bypassed, patient messages are stored for the operator, a small operator page sends replies through the Push API and can return the conversation to the bot. The conversation text of handed-over chats would have to be stored for a defined retention period.
11. **Intake form page.** Build the page behind the signed link (autosave, "I will fill it in at the clinic", data never sent to an LLM), specified in `docs/INTAKE.md`.

---

## Quickstart

Requirements: Python 3.14, [uv](https://docs.astral.sh/uv/), Docker, [Ollama](https://ollama.com/) with `gemma4:e4b` for the local agent. A simulator key (`OPENROUTER_API_KEY`) is needed for evaluation runs. Use `127.0.0.1`, not `localhost`, for local services on Windows.

```bash
cp .env.example .env                  # set POSTGRES_PASSWORD (and OPENROUTER_API_KEY for eval)
docker compose up -d                  # Postgres 17 on host port 5441
uv sync

uv run python -m dental.synth.generate          # synthetic clinic snapshot (seeded, deterministic)
uv run python -m dental.core.loader --reset     # load schema and snapshot

uv run pytest                                   # ~180 tests; DB tests need the container

# evaluation (each run is written to data/runs.jsonl)
uv run python -m dental.eval.run --agent graph --label graph-v6
uv run python -m dental.eval.run --agent baseline --label baseline-v1
uv run python -m dental.eval.compare --list
uv run python -m dental.eval.compare -2 -1      # diff two runs by scenario

# optional
uv run python -m dental.eval.run --agent graph --channel line --label graph-v6_line   # through the LINE webhook
uv run python -m dental.eval.run --agent graph --scenarios s01 --repeats 1 --trace    # traces in Phoenix (127.0.0.1:6006)
uv run python -m dental.summary --profile local                                        # owner's morning summary
uv run python -m dental.channels check                                                  # verify LINE credentials
uv run python -m dental.channels serve --profile local                                  # webhook on 127.0.0.1:8080/line/webhook
```

Models are configured in `llm.toml` (any OpenAI-compatible chat endpoint: Ollama, OpenRouter). Roles `agent` and `simulator` pick profiles separately; `--agent-profile` and `--sim-profile` override them per run. The MCP server runs over stdio (`uv run python -m dental.mcp_server`) and can be attached to any MCP client for exploration; MCP clients like that are not used for metrics because such runs are not reproducible and have no fingerprint.

---

## Repository layout

```
src/dental/
  core/          booking core, DB access, loader (no LLM)
  mcp_server/    MCP tools and server
  agent/         graph.py (LangGraph agent), baseline.py, understand.py, dates.py, llm.py
  eval/          runner, simulator, checks, metrics, compare, rescore, summary_eval
  summary/       owner summary: facts, text and verification, writer, html
  channels/      LINE webhook, sessions, eval bridge, CLI
  synth/         seeded synthetic data
  tracing.py     OpenTelemetry spans, content masking
scenarios/golden/ 15 YAML scenarios
migrations/      PostgreSQL schema
docs/            SPEC, DECISIONS, FIXTURES, INTAKE, SCENARIO_FORMAT
tests/
```

Documentation: [`docs/SPEC.md`](docs/SPEC.md) (the original specification), [`docs/DECISIONS.md`](docs/DECISIONS.md) (42 numbered decisions with measurements; where it differs from SPEC, DECISIONS wins), [`docs/SCENARIO_FORMAT.md`](docs/SCENARIO_FORMAT.md), [`docs/FIXTURES.md`](docs/FIXTURES.md), [`docs/INTAKE.md`](docs/INTAKE.md). All documentation is in English. A few strings in the code stay in Russian on purpose — prompts and tool texts the models read, and the run-report format that old runs are recomputed from — see DECISIONS #42.

---

## Status and limits

Done: booking core, MCP server, baseline and graph agents, evaluation harness, owner summary with its own evaluation, Phoenix tracing, LINE channel (emulated parity and one live manual run).

Not done, stated plainly:

- **Held-out set: one clean run only.** `graph-v6` scored 0.80 on it before any fix. After the two defects were fixed (`graph-v7`) the set stopped being held-out for that version, so a new unseen set is needed before quoting a fresh honest figure. The set was written by the same author as the rules, so it is not fully independent either.
- **Cloud models: agent only, one cloud model per architecture.** Gemini 2.5 Flash on both agents and Haiku 4.5 on the baseline (one repeat). The summary-writer comparison against a cloud model is still pending, and the Haiku run is too small for a precise figure.
- **Small samples.** 45 dialogues per configuration; differences of one or two dialogues are noise.
- **Intake form page (問診票): a to-do for a production deployment.** The booking tool issues a signed token and a link (`INTAKE_BASE_URL`, default `http://localhost:8000/intake/<token>`), and the agent tells a new patient that the form is optional and gives the link, but no page serves that URL in this portfolio version. The form's fields and rules are specified in `docs/INTAKE.md`.
- **Russian and English are not supported in patient dialogue.** Parsers are Japanese; a Russian message reaches only the LLM fallback and a new patient cannot complete registration. Russian and English exist in the developer-facing materials and in the summary page.
- Sessions are in memory; the fallback LLM call blocks the webhook's event loop (details above).
- Out of scope for this portfolio version, left as production extensions: an admin interface (editing services, hours and closed days, entering phone bookings through the agent), an interface for external patient agents, operator takeover of a conversation, and the evening variant of the owner summary.
- Several parameters are assumptions about a clinic (working hours, a two-shift Saturday, 60-minute first visit); they live in config and the database, and can be changed.
