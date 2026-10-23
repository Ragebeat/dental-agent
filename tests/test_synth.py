import hashlib

import pytest

from dental.config import load_config
from dental.scenarios import HOLDOUT_DIR, load_scenarios, validate_scenario
from dental.synth.generate import build_snapshot, dumps
from dental.synth.verify import verify_snapshot

CFG = load_config()


def test_deterministic_for_seed():
    a, b = dumps(build_snapshot(CFG)), dumps(build_snapshot(CFG))
    assert hashlib.sha256(a.encode()).digest() == hashlib.sha256(b.encode()).digest()


def test_seed_changes_background_but_not_fixtures():
    base = build_snapshot(CFG)
    other_cfg = load_config()
    other_cfg.synthetic.seed += 1
    other = build_snapshot(other_cfg)
    assert base["meta"]["snapshot_sha"] != other["meta"]["snapshot_sha"]
    assert base["appointments"] != other["appointments"]
    assert base["patients"][:5][0]["card_number"] == other["patients"][:5][0]["card_number"] == 100001


@pytest.mark.parametrize("seed", [20261007, 1, 2, 3, 42, 777])
def test_all_guarantees_hold_for_any_seed(seed):
    cfg = load_config()
    cfg.synthetic.seed = seed
    assert verify_snapshot(build_snapshot(cfg), cfg) == []


def test_sizes():
    snap = build_snapshot(CFG)
    assert len(snap["patients"]) == 5 + 54 + 450      # P_NEW is not in the database
    assert len({p["card_number"] for p in snap["patients"]}) == len(snap["patients"])


def test_scenarios_consistent_with_config_and_fixtures():
    scs = load_scenarios()
    assert len(scs) == 15
    cats = {c for s in scs for c in s.categories}
    assert cats == set(range(1, 13)), f"SPEC categories not covered: {set(range(1, 13)) - cats}"
    for sc in scs:
        assert validate_scenario(sc, CFG) == [], sc.id


def test_holdout_scenarios_consistent_and_separate_from_golden():
    held = load_scenarios(HOLDOUT_DIR)
    golden_ids = {s.id for s in load_scenarios()}
    assert [s.id.split("_")[0] for s in held] == [f"h{i:02d}" for i in range(1, 11)]
    assert not golden_ids & {s.id for s in held}
    for sc in held:
        assert validate_scenario(sc, CFG) == [], sc.id

