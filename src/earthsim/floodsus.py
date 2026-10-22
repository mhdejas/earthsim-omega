"""Baseline flood susceptibility and its evaluation against SAR-observed extents.

What this is: an *interpretable susceptibility* layer (a relative ranking of
where open-water flooding is more likely, given terrain). It is NOT an
inundation depth, NOT a calibrated probability and NOT a forecast.

Models
------
* ``hand_threshold``: flooded iff HAND <= h. One parameter, h, calibrated on
  training blocks of the calibration event only.
* ``logistic``: logistic regression on terrain features (HAND, log upstream
  area, slope, elevation, distance to stream) fitted on training blocks of the
  calibration event. The output is a susceptibility *score* in [0, 1]; we do
  not claim it is calibrated, because the reference itself is incomplete.
* ``ksdma_*``: KSDMA historical 10/25/50-year rasters used as a binary
  footprint (external agency model; units undocumented). No fitting.
* ``distance_threshold``: flooded iff distance to stream <= d (naive baseline).

Evaluation (leakage control)
----------------------------
Parameters are fitted on the calibration event (2018-08-14, the only scene
covering the whole district) inside training blocks (GroupKFold, 5 km square
blocks) and scored (a) on the same event in the held-out blocks (spatial CV)
and (b) on a different year's event (2019-08-10) in the held-out blocks
(spatial + temporal hold-out; that scene covers only the eastern ~45%).
Reference pixels: valid SAR inside the district, excluding permanent water.

Metrics (binary, on the reference grid)
---------------------------------------
precision = TP/(TP+FP); recall (POD) = TP/(TP+FN); CSI = TP/(TP+FP+FN), which
equals IoU of the two flooded sets; FAR = FP/(TP+FP); missed ratio = FN/(TP+FN)
= 1 - recall; F1. Plus average precision (area under PR) for scores.
All are computed against SAR, which misses water under canopy and in urban
areas, so precision/FAR are pessimistic and recall is relative to what SAR saw.
"""

from __future__ import annotations

from typing import Any

import numpy as np


def binary_metrics(pred: np.ndarray, obs: np.ndarray) -> dict[str, float]:
    pred = pred.astype(bool)
    obs = obs.astype(bool)
    tp = int((pred & obs).sum())
    fp = int((pred & ~obs).sum())
    fn = int((~pred & obs).sum())
    tn = int((~pred & ~obs).sum())

    def r(a, b):
        return float(a / b) if b else float("nan")

    return {"tp": tp, "fp": fp, "fn": fn, "tn": tn, "precision": r(tp, tp + fp), "recall": r(tp, tp + fn),
            "csi": r(tp, tp + fp + fn), "iou": r(tp, tp + fp + fn), "far": r(fp, tp + fp),
            "missed_ratio": r(fn, tp + fn), "f1": r(2 * tp, 2 * tp + fp + fn),
            "predicted_fraction": r(tp + fp, tp + fp + fn + tn), "observed_fraction": r(tp + fn, tp + fp + fn + tn)}


def best_threshold(score: np.ndarray, obs: np.ndarray, candidates: np.ndarray, flooded_if_below: bool = True,
                   objective: str = "csi") -> tuple[float, float]:
    best, bt = -1.0, float(candidates[0])
    for t in candidates:
        pred = score <= t if flooded_if_below else score >= t
        m = binary_metrics(pred, obs)[objective]
        if np.isfinite(m) and m > best:
            best, bt = m, float(t)
    return bt, best


def evaluate_models(feats: dict[str, np.ndarray], obs_cal: np.ndarray, obs_test: np.ndarray, ref_cal: np.ndarray,
                    ref_test: np.ndarray, groups: np.ndarray, external: dict[str, np.ndarray],
                    k: int = 5, seed: int = 42) -> dict[str, Any]:
    """All arrays are 1-D over the same pixel set. ref_* = pixel usable as reference on that date."""
    from sklearn.linear_model import LogisticRegression
    from sklearn.metrics import average_precision_score
    from sklearn.model_selection import GroupKFold
    from sklearn.pipeline import make_pipeline
    from sklearn.preprocessing import StandardScaler

    names = ["hand", "log_upstream_km2", "slope_deg", "elevation", "dist_to_stream_m"]
    X = np.column_stack([feats[n] for n in names])
    n = X.shape[0]
    pred = {m: np.zeros(n, bool) for m in ("hand_threshold", "logistic", "distance_threshold")}
    score = np.full(n, np.nan, "float32")
    folds = []
    gkf = GroupKFold(n_splits=k)
    for f, (tr, te) in enumerate(gkf.split(X, groups=groups)):
        trc = tr[ref_cal[tr]]
        h, csi_h = best_threshold(feats["hand"][trc], obs_cal[trc], np.arange(0.5, 15.01, 0.5))
        d, csi_d = best_threshold(feats["dist_to_stream_m"][trc], obs_cal[trc], np.arange(30, 1501, 30))
        lr = make_pipeline(StandardScaler(), LogisticRegression(max_iter=500, class_weight="balanced",
                                                                random_state=seed))
        lr.fit(X[trc], obs_cal[trc])
        s_tr = lr.predict_proba(X[trc])[:, 1]
        t_lr, _ = best_threshold(s_tr, obs_cal[trc], np.linspace(0.05, 0.95, 91), flooded_if_below=False)
        pred["hand_threshold"][te] = feats["hand"][te] <= h
        pred["distance_threshold"][te] = feats["dist_to_stream_m"][te] <= d
        score[te] = lr.predict_proba(X[te])[:, 1]
        pred["logistic"][te] = score[te] >= t_lr
        coefs = dict(zip(names, lr[-1].coef_[0].round(4).tolist()))
        folds.append({"fold": f, "train_pixels_cal_ref": int(trc.size), "test_pixels": int(te.size),
                      "hand_threshold_m": h, "train_csi_hand": round(csi_h, 4), "dist_threshold_m": d,
                      "logistic_threshold": round(t_lr, 3), "logistic_std_coefs": coefs})
    out: dict[str, Any] = {"folds": folds, "features": names, "test": {}, "calibration_event_spatial_cv": {}}
    for m, p in {**pred, **{k_: v.astype(bool) for k_, v in external.items()}}.items():
        out["test"][m] = binary_metrics(p[ref_test], obs_test[ref_test])
        # out-of-fold (spatially held-out blocks) predictions scored on the calibration event itself
        out["calibration_event_spatial_cv"][m] = binary_metrics(p[ref_cal], obs_cal[ref_cal])
    ok = ref_test & np.isfinite(score)
    out["test"]["logistic"]["average_precision"] = float(average_precision_score(obs_test[ok], score[ok]))
    hs = -feats["hand"]
    out["test"]["hand_threshold"]["average_precision"] = float(average_precision_score(obs_test[ref_test],
                                                                                       hs[ref_test]))
    out["test_prevalence"] = float(obs_test[ref_test].mean())
    return out, score
