# Intake form 問診票: fields and rules v1

Sources for the form: typical Japanese dental intake forms (the MHLW 歯科問診票, university hospital forms).
Synthetic data only.

## Principle: the form is run by code, not by the LLM

The agent does not read, ask or retell the form's questions. Its part: after a 初診 booking, say that the form can be filled in through a link and that it is optional.
Reasons: health data never reaches the LLM or the traces; no tokens are spent; the form is covered by ordinary tests; one injection channel is closed.

## Flow

1. A 初診 booking (name, ふりがな, phone) → a patient record and card number → the booking is confirmed.
2. The booking tool returns `intake_url`: a page of our own service (FastAPI) with a signed token.
3. The page: fill it in now, partly and come back later through the same link, or press "I will fill it in at the clinic".
4. Fields are saved as they are typed: a dropped connection or a dead phone loses nothing.
5. The token is valid until `expires_at` (14 days) or until the form is completed; it is reusable, so one can come back.
6. In LINE (E7) the same page opens through a link or a Flex Message card.

## Form status

`not_started` → `in_progress` → `completed` | `deferred_to_clinic`. Declining the form does not cancel the booking.

## Fields (all optional, null = not answered)

| Field | Type | Comment |
|---|---|---|
| `chief_complaint` | text | main complaint / reason for the visit |
| `tooth_areas` | list | areas: upper/lower × right/left × front/back; FDI numbers (the international tooth numbering) optional |
| `onset` | text | since when |
| `pain_now` | bool | does it hurt now |
| `last_dental_visit` | text | roughly when |
| `anesthesia_trouble` | text | problems with anaesthesia or tooth extraction |
| `bleeding_tendency` | bool | bleeding is hard to stop |
| `allergy_drug` / `allergy_food` / `allergy_metal` | text | allergies |
| `intolerance` | text | intolerances, side effects |
| `medications` | text | current medication |
| `conditions` | list | diabetes, blood pressure, heart, kidneys/liver, asthma, osteoporosis, other |
| `pregnancy_lactation` | text | asked neutrally, with no assumptions about sex |
| `smoking` | enum | no / yes / former |

## Rules

- The page does not interpret answers and gives no advice. A question like "what should I take?" outside the form still goes to a human through the agent.
- Free text is data: stored as is, escaped on display, never executed anywhere.
- Raw form text is passed to no LLM (the owner summary included): only structured fields and numbers.
