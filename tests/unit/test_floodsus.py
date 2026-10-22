import numpy as np

from earthsim import floodsus as fs


def test_binary_metrics_hand_computed():
    pred = np.array([1, 1, 0, 0, 1, 0], bool)
    obs = np.array([1, 0, 1, 0, 1, 0], bool)
    m = fs.binary_metrics(pred, obs)  # TP=2 FP=1 FN=1 TN=2
    assert (m["tp"], m["fp"], m["fn"], m["tn"]) == (2, 1, 1, 2)
    assert np.isclose(m["csi"], 0.5) and m["csi"] == m["iou"]
    assert np.isclose(m["precision"], 2 / 3) and np.isclose(m["far"], 1 / 3)
    assert np.isclose(m["recall"], 2 / 3) and np.isclose(m["missed_ratio"], 1 / 3)
    assert np.isclose(m["f1"], 2 / 3)


def test_binary_metrics_empty_prediction_is_nan_not_zero_division():
    m = fs.binary_metrics(np.zeros(4, bool), np.array([1, 0, 0, 0], bool))
    assert np.isnan(m["precision"]) and m["recall"] == 0


def test_best_threshold_recovers_generating_threshold():
    rng = np.random.default_rng(3)
    hand = rng.uniform(0, 20, 20000)
    obs = hand <= 4.0
    t, csi = fs.best_threshold(hand, obs, np.arange(0.5, 15.01, 0.5))
    assert t == 4.0 and csi == 1.0


def test_evaluate_models_is_blockwise_and_scores_heldout_event():
    rng = np.random.default_rng(0)
    n = 40000
    hand = rng.uniform(0, 20, n)
    feats = {"hand": hand, "log_upstream_km2": rng.normal(0, 1, n), "slope_deg": rng.uniform(0, 30, n),
             "elevation": hand * 2 + rng.normal(0, 1, n), "dist_to_stream_m": hand * 50}
    obs_cal = hand <= 3.0
    obs_test = hand <= 3.5  # a different (synthetic) event
    ref = np.ones(n, bool)
    groups = np.arange(n) // 400  # 100 blocks
    res, score = fs.evaluate_models(feats, obs_cal, obs_test, ref, ref, groups, {"all": np.ones(n, bool)}, k=5)
    assert len(res["folds"]) == 5
    assert all(f["hand_threshold_m"] == 3.0 for f in res["folds"])
    assert res["test"]["hand_threshold"]["csi"] > 0.8
    assert res["test"]["all"]["precision"] < 0.3  # trivial "everything floods" baseline is poor
    assert np.isfinite(score).all()
