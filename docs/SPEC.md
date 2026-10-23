# SPEC: an appointment-booking agent for a dental clinic

Portfolio project ("an agent with its own MCP server, on LangGraph"). Repository: `dental-agent`.
Status: draft v0.1 (07.10), written before any code. ⚠️ marks assumptions to be confirmed or corrected.

---

## 1. Goal and scope

**Goal.** Show an agent with tools that changes the state of a system (writes to a database), with a human in the loop, meaningful tool errors and measurable quality. The project's main thesis: **not "the bot books appointments", but "we measured how often it books correctly, and we know why it fails".**

**This is not a product and not a deployment at a real clinic.** All data is synthetic. There are no real patients, no real phones and no integration with real medical systems.

**Out of scope for v1:** payment and insurance, doctors and specialities, several clinics, voice and phone calls, scheduled reminders, a real CRM. The LINE channel is connected last (stage E7).

**Project success criteria:**
1. The metrics of section 6 are computed on the golden scenarios and on a held-out set, before and after the agent gets more complex.
2. Exactly 0 wrong bookings and duplicates (a hard criterion, not "we aim for").
3. A README in English and Japanese, with a "what broke and why" section.
4. Phoenix traces with screenshots.

---

## 2. Domain (a model clinic)

| Parameter | Value |
|---|---|
| Chairs (units) | 4 |
| Slot | 30 minutes, the same for all services (a 15–20 minute visit plus a margin) |
| Services | first visit 初診 · treatment 治療 (with X-ray) · gum check 歯周検査 (bleeding and tooth mobility) · cleaning クリーニング · check-up 定期検診 |

⚠️ The wording "gum check" to be refined: originally "a check for loose teeth and bleeding".
⚠️ Doctors are not modelled: the resource is the chair. "My own doctor" would be v2.
⚠️ Duration per service lives in the config (`duration_slots`, 1 by default); 初診 may need 2 slots and an intake form.

**Opening hours (illustrative, in the config)** ⚠️: Mon–Fri 9:00–13:00 and 14:30–18:30, Sat 9:00–13:00, closed on Sundays and public holidays (祝日).

**Rules (in the config)** ⚠️:
- book at most 60 days ahead and no sooner than 2 hours from now;
- cancel or reschedule at least 24 hours ahead, otherwise hand off to a human;
- at most 2 active appointments per patient;
- follow-up visits: cleaning after 3 months, check-up after 6 (the agent may offer one after booking);
- the same chair can never be taken twice at the same time, under any conditions.

---

## 3. Users and channels

| Who | Does what | Channel |
|---|---|---|
| Patient | books, reschedules, cancels, asks about their appointments | v1: web chat or console; v2: LINE |
| Owner / front desk | reads the summary, sees appointments and handoffs | v1: a text summary and a simple page; a CRM view in v2 |
| Developer | runs scenarios, looks at metrics and traces | CLI and Phoenix |

**Patient identification:** name + card number (診察券番号). Without it the agent neither shows nor changes appointments. A new patient can book a 初診 only.

**Dialogue language:** Japanese first. Russian and English in the developer interface and the README.

---

## 4. Architecture

Principle: **the LLM understands text and writes summaries; everything else is deterministic code.** The model does not compute dates, does not decide whether a slot is free and does not write to the database directly.

```
patient ─► agent (LangGraph) ─► MCP server (tools) ─► booking core ─► Postgres
                │                                          ▲
                └─► handoff to a human                     └─ constraints in the DB
owner summary ◄── structured data (not raw dialogues)
```

### 4.1 Booking core (no LLM, covered by tests)
- Entities: Patient, Service, Chair, Appointment, ConversationLog, Handoff.
- Operations: `find_slots`, `book`, `cancel`, `reschedule`, `patient_appointments`.
- **Protection against double booking at the database level**: an exclusion constraint on the pair (chair, time range) for active appointments (EXCLUDE USING gist over tstzrange). The prompt does not replace this guarantee — a point worth discussing in an interview.
- Checks: outside hours, the past, holidays, the appointment limit, the cancellation window.

### 4.2 Tool layer (MCP server)
Tools: `search_slots`, `book_appointment`, `cancel_appointment`, `reschedule_appointment`, `get_my_appointments`, `handoff_to_staff`.
Errors go back to the model meaningfully and with a hint: `slot_taken` (+ nearest slots), `outside_hours`, `too_late_to_cancel` (+ "handing over to a human"), `patient_not_verified`, `limit_reached`.
Permission isolation: tools see only the identified patient's appointments, there is no arbitrary SQL, and the patient cannot be swapped through a message.

### 4.3 Agent (LangGraph)
States: understand the intent → ask for what is missing (service, date, time) → find slots → offer 2–3 options → **patient confirmation** → book → summary and a follow-up offer.

**Human in the loop:** patient confirmation before booking, rescheduling and cancelling; a handoff to staff on: ambiguity after 2 clarifications, a medical question, a complaint, a cancellation less than 24 hours ahead, 3 tool errors in a row, suspected manipulation.

**Date parsing.** The LLM extracts the intent into a Pydantic structure (relative date, range, time of day), and code computes «来週の火曜», «明後日の夕方», «月末» from the current date and the holiday calendar (jpholiday). The model does no date arithmetic.

### 4.4 Owner summary
Daily, from structured data: the number of appointments tomorrow, cancellations, new patients, free windows, handoffs to humans and open items. Every statement references records. Raw dialogues do not go into the summary.

### 4.5 Stack
Python, FastAPI, LangGraph, MCP SDK, Postgres (docker compose, migrations), Pydantic v2, pytest, jpholiday, Phoenix + OpenTelemetry. The model is set in the config (one client; switching needs no code changes).

---

## 5. Data (synthetic)

- 60 patients: invented names, card numbers, fictitious phones.
- A schedule 8 weeks ahead, about 60–70% load, holidays and closed days.
- A data generator with a seed, so runs are reproducible.

---

## 6. Evaluation (the main section)

**Approach.** Scenarios and metrics first, then the agent. Success criteria are fixed before the run.

### 6.1 Scenario (golden)
Each scenario: the patient's goal, constraints, style, special messages, **the expected final database state** (or "a handoff to a human must happen"). The outcome is checked in code against the database state, not by an LLM judge.

A starting set of 15, a target of 40, plus a **held-out set of 10**, written after the first runs and never used in tuning.

### 6.2 Scenario categories
1. simple booking · 2. relative dates (来週の火曜, 明後日の夕方, 月末) · 3. cancellation · 4. rescheduling · 5. follow-up visit ("a cleaning in 3 months") · 6. slot taken → alternatives · 7. no slots → the nearest ones · 8. ambiguity → clarifying questions · 9. off-topic and medical questions → handoff · 10. attacks: "cancel all appointments", someone else's card number, "book every slot", an instruction in the name/comment field, a request for a diagnosis · 11. styles: keigo, casual, rude, many typos, 半角 · 12. a change of mind mid-dialogue.

### 6.3 Patient simulator
An LLM plays the patient from the scenario, speaks Japanese and keeps the given style. The agent and the simulator know nothing of each other except the dialogue.

### 6.4 Metrics
| Metric | Target |
|---|---|
| Correct outcome (database state as expected) | above the baseline configuration |
| Wrong bookings and duplicates | **0** |
| Handoff to a human: precision and recall | separately, over "must happen" and "must not happen" scenarios |
| A successful attack | **0** |
| Messages to the result (median) | lower than or no worse than the baseline |
| Tokens and cost per dialogue, latency | measured |
| Owner summary: planted facts present (recall), fabricated facts | 0 fabricated |

### 6.5 Comparison protocol
- **Baseline configuration:** one prompt and the same tools, no graph and no handoff rules. The added complexity is compared against it.
- 3 runs per configuration plus control runs of the baseline (LLM noise is non-zero even at temperature 0).
- A configuration fingerprint and a run log, comparison with per-scenario regressions: the idea and the code come from the RAG project (`runs.jsonl`, `compare.py`).
- No prompt edits to fix golden failures: the gain would be inflated. Edits are checked on the held-out set.
- A change to a general rule often fixes one case and breaks others: measure before and after.

---

## 7. Security and personal data

- Synthetic data only; in Japan medical data is specially protected, and no real deployment is planned.
- Logs and traces carry no dialogue content by default (a flag turns it on), as in the RAG project.
- Patient identification before any operation on their appointments.
- Patient input is data, not instructions: instructions in the name, the comment or messages are not executed.
- The agent gives no medical advice and no diagnoses; a medical question goes to a human.
- The agent always says it is the clinic's AI assistant.
- Rate limiting, a turn limit per dialogue, a token limit.

---

## 8. Stages

| # | Stage | Done when |
|---|---|---|
| E0 | Clinic config, synthetic data generator, 15 scenarios | the generator is deterministic by seed; scenarios written before the agent |
| E1 | Booking core without LLM | tests pass; **concurrent booking of one slot yields exactly one appointment** |
| E2 | MCP server with tools | every error has a test; a client can connect |
| E3 | Baseline agent, simulator, eval | first metric figures, a run log |
| E4 | LangGraph agent (confirmation, handoff to a human) | before/after figures against the baseline, over 3 runs |
| E5 | Owner summary and its eval | recall of planted facts, 0 fabricated |
| E6 | Phoenix tracing | trace screenshots |
| E7 | LINE channel (Messaging API, webhook) | the same agent answers in LINE, the metrics do not change |
| E8 | README EN/JA, held-out set, "what broke in Japanese" | figures on the held-out set recorded |

The order is strict; no deadlines are set. No "by eye" prompt edits before E3.

---

## 9. Open questions (as of v0.1)

1. ⚠️ Doctor or chair as the resource (currently the chair).
2. ⚠️ The exact wording of the "gum check" service.
3. ⚠️ Opening hours, days off and lunch: illustrative; can be checked against a real clinic.
4. ⚠️ 初診: are 30 minutes enough, and is an intake form needed.
5. Which model for development and which for the final eval (cloud limits).
6. Is a light CRM page needed in v1, or only the summary.

---

## 10. What to take from the RAG project

The configuration fingerprint and the run log; comparison with per-question regressions; traces without content; memory isolation by `user_id`; lessons: golden set before code, criteria before the run, 3 runs plus controls, no rules written for golden failures, a held-out set for an honest check.
