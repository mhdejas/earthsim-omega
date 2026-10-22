"""Offline tests for rain triggers, alert levels and the bulletin (no network)."""
from __future__ import annotations

from datetime import datetime, timedelta

import numpy as np
import pytest

from earthsim import rain_trigger as rt
from earthsim.forecast import LEVELS, bulletin, rain_level, rain_outlook


def _hours(start: str, n: int) -> list[str]:
    t0 = datetime.fromisoformat(start)
    return [(t0 + timedelta(hours=i)).isoformat(timespec="minutes") for i in range(n)]


def test_imd_categories_boundaries():
    assert rt.category(64.4) == "below_heavy"
    assert rt.category(64.5) == "heavy"
    assert rt.category(115.6) == "very_heavy"
    assert rt.category(204.5) == "extremely_heavy"


def test_daily_0830_windows_and_drops_partial_days():
    t = _hours("2024-07-01T00:00", 72)
    mm = np.ones(72)
    d = rt.daily_0830(t, mm)
    # 2024-07-01 label covers 06-30 09:00..07-01 08:00 -> only 9 hours present -> dropped
    assert "2024-07-01" not in d
    assert d["2024-07-02"] == 24.0 and d["2024-07-03"] == 24.0
    assert "2024-07-04" not in d          # 07-03 09:00..07-03 23:00 only 15 h
    mm[30] = np.nan
    assert "2024-07-02" not in rt.daily_0830(t, mm)


def test_contingency_scores():
    fc = np.array([70, 70, 10, 10, 80.0])
    ob = np.array([70, 10, 70, 10, 90.0])
    c = rt.contingency(fc, ob, 64.5)
    assert (c["hits"], c["false_alarms"], c["misses"], c["correct_negatives"]) == (2, 1, 1, 1)
    assert c["pod_hit_rate"] == pytest.approx(2 / 3)
    assert c["far_false_alarm_ratio"] == pytest.approx(1 / 3)
    assert c["csi"] == pytest.approx(0.5)


def test_loyo_tuning_never_scores_on_training_year():
    rng = np.random.default_rng(0)
    daily = {}
    for y in ("2024", "2025", "2026"):
        for i in range(60):
            d = (datetime(int(y), 7, 1) + timedelta(days=i)).date().isoformat()
            o = float(rng.gamma(1.2, 20))
            daily[d] = {"era5": o, **{f"precipitation_previous_day{k}": o * 0.7 + 5 for k in (1, 2, 3)}}
    out = rt.tune_trigger_loyo({"daily": daily}, leads=(1,))
    folds = out["by_lead"]["day1"]["folds"]
    assert [f["held_out_year"] for f in folds] == ["2024", "2025", "2026"]
    pooled = out["by_lead"]["day1"]["pooled_held_out"]
    assert pooled["hits"] + pooled["false_alarms"] + pooled["misses"] + pooled["correct_negatives"] == 180
    # forecast = 0.7*obs + 5 -> the trigger that maps 64.5 obs is ~50 mm
    assert out["by_lead"]["day1"]["deploy_T_mm_all_years"] in (45, 50)


def _day(**kw):
    d = {"prob_heavy": 0.0, "prob_very_heavy": 0.0, "prob_extremely_heavy": 0.0, "deterministic_ge_trigger": False}
    d.update(kw)
    return d


@pytest.mark.parametrize("kw,level", [
    ({}, "GREEN"),
    ({"deterministic_ge_trigger": True}, "YELLOW"),
    ({"prob_heavy": 0.2}, "YELLOW"),
    ({"prob_heavy": 0.5}, "ORANGE"),
    ({"prob_very_heavy": 0.2}, "ORANGE"),
    ({"prob_very_heavy": 0.5}, "RED"),
    ({"prob_extremely_heavy": 0.2}, "RED"),
])
def test_rain_level_ladder(kw, level):
    assert rain_level(_day(**kw))[0] == level
    assert level in LEVELS


def _live(det, members, start="2026-10-18T00:00"):
    n = len(det)
    return {"times": _hours(start, n), "deterministic_mm": np.asarray(det, float),
            "members_mm": np.asarray(members, float), "member_names": [f"m{i}" for i in range(len(members))]}


def test_rain_outlook_probabilities_and_past_days_excluded():
    n = 24 * 8
    det = np.full(n, 1.0)
    members = np.full((20, n), 1.0)
    members[:10, 24 * 5 + 9: 24 * 6 + 9] = 4.0      # half the members: 96 mm in one 08:30 day
    now = datetime(2026, 10, 23, 3, 0)
    o = rain_outlook(_live(det, members), now, trigger_mm=50)
    labels = [d["day_ending_0830_ist"] for d in o["days"]]
    assert "2026-10-22" not in labels               # fully in the past
    assert o["days"][0]["in_progress"] is True
    hit = next(d for d in o["days"] if d["prob_heavy"] > 0)
    assert hit["prob_heavy"] == pytest.approx(0.5)
    assert rain_level(hit)[0] == "ORANGE"
    assert o["antecedent_5day_mm"] > 0


def test_bulletin_always_carries_disclaimer():
    a = {"disclaimer": "EXPERIMENTAL research output - not an official warning.", "issued_ist": "2026-10-23T03:00",
         "run_id": "x", "overall_level_next_72h": "GREEN",
         "rain_outlook": {"days": [{"day_ending_0830_ist": "2026-10-24", "in_progress": False, "deterministic_mm": 3.0,
                                    "ensemble_p10_p50_p90_mm": [0, 1, 5], "prob_heavy": 0.0, "prob_very_heavy": 0.0,
                                    "rain_level": "GREEN"}], "antecedent_5day_mm": 10.0},
         "flood_scenarios": {"run": False},
         "verified_skill": {"lead_day1_held_out": {"pod_hit_rate": 0.6, "far_false_alarm_ratio": 0.67, "csi": 0.27}}}
    b = bulletin(a)
    assert "not an official warning" in b and "GREEN" in b and "south-west monsoon" in b
