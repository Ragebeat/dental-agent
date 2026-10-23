# Fixtures: the contract between the scenarios and the synthetic data generator

Fixtures are predefined test data with stable IDs. The generator (seed in clinic.toml) **must** create them exactly like this and fills the rest randomly.
"Now" = **Wednesday 2026-10-07 10:00 JST**. Before every scenario the database is restored from the same snapshot.

## Patients

| ID | Name | ふりがな | Card | Phone | State |
|---|---|---|---|---|---|
| P_CLEAN | 鈴木 健太 | すずき けんた | 100001 | 070-0000-0001 | 0 active appointments; last cleaning 2026-06-05 |
| P_ONE | 高橋 さくら | たかはし さくら | 100002 | 070-0000-0002 | 1 active: 治療, Tue 2026-10-13 10:30 |
| P_SOON | 伊藤 大輔 | いとう だいすけ | 100003 | 070-0000-0003 | 1 active: 治療, today 2026-10-07 16:30 (<24 h ahead, >2 h) |
| P_OTHER | 渡辺 優子 | わたなべ ゆうこ | 100004 | 070-0000-0004 | 2 active: 治療 Fri 2026-10-09 10:00; クリーニング Wed 2026-10-14 14:30 |
| P_TWO | 小林 誠 | こばやし まこと | 100005 | 070-0000-0005 | 2 active (the limit); reserved for extending the set to 40 |
| P_NEW | 山本 美咲 | やまもと みさき | none | 070-0000-0006 | **not in the database** |

Card numbers of random patients: 100100 and up. Phones are fictitious (`070-0000-xxxx`). Placeholders in scenarios: `{P_CLEAN.name}`, `{P_CLEAN.name_kana}`, `{P_CLEAN.card}`, `{P_NEW.phone}`.

## Slot state

Rule: the background fill must not break these guarantees (fixtures first, then the random fill).

| ID | Guarantee |
|---|---|
| F_BUSY | Fri 2026-10-09 15:00 taken on all 4 chairs. Other Fri 14:00–18:00 slots: ≥3 starts with a free chair, ≥2 of them in 16:00–18:00 |
| F_FULL | Tue 2026-10-13, starts 16:00–18:00 (5 slots) taken on all chairs. Free: Tue morning; Wed 2026-10-14 and Fri 2026-10-16 ≥2 starts from 16:00 each; Sat 2026-10-17 ≥1 start from 16:00 |
| F_NEXTWEEK | Mon 12.10 holiday (closed); Tue 13, Wed 14, Fri 16, Sat 17 open, each with ≥8 starts with at least one free chair |
| F_FRI16 | Fri 2026-10-16: ≥3 free starts in 9:00–12:00 and ≥3 in 14:00–18:00 |
| F_WED14 | Wed 2026-10-14: ≥3 free starts in 9:00–12:00 |
| F_FRI09 | Fri 2026-10-09: ≥2 free starts in 16:00–18:00 (besides the F_BUSY slot) |
| F_JAN | 2027-01-05, 06, 08, 09, 12: no background appointments (the background covers 8 weeks; the generator puts no far-off follow-ups on these days) |

## The calendar the scenarios rely on (checked with jpholiday in the generator)

Thu 08.10 closed · Mon 12.10 holiday (体育の日/スポーツの日) · 03.11 holiday · 23.11 holiday · 2027-01-07 Thu closed · 2027-01-11 holiday (成人の日).
Horizon: 60 days, up to 2026-12-06; for クリーニング/定期検診 200 days, up to 2027-04-25.

## Durations and the load profile

- 初診 and 治療 take 2 consecutive slots (60 minutes) on one chair and do not cross the break: the last valid start is 11:30 and 17:30 (on Saturday 11:30 and 16:30).
- "Slot taken" for a 2-slot service means there are no two consecutive free slots on one chair.
- Fixtures are loaded first and override the load profile from `clinic.toml` ([synthetic.load]): the first weeks are dense, later windows are free.
- The other 54 test patients: 0–1 active appointments, random.

## How the guarantees are implemented

The guarantees are kept by a reserve: the background never takes the chair-4 windows in `RESERVED_FREE` and forcibly occupies `FORCED_BUSY` (`src/dental/synth/fixtures.py`). `verify.py` checks them and the invariants on every snapshot; the tests run 6 different seeds.

## Birth dates (identity check: card + birth date)

P_CLEAN 1988-04-12 · P_ONE 1979-11-03 · P_SOON 1992-07-21 · P_OTHER 1985-02-28 · P_TWO 1971-09-09 · P_NEW 1995-05-17 (entered at registration). Placeholder in scenarios: `{P_X.birth_date}` (ISO).
