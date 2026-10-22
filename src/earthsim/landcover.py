"""Land-cover layer: Sentinel-2 2025 composite classified against ESA WorldCover 2021.

Honest framing: WorldCover 2021 v200 is itself a classification product
(ESA-reported global overall accuracy 76.7%), not ground truth, and it is
four years older than the 2025 imagery. Scores here are therefore
*agreement with WorldCover*, and real 2021->2025 change counts as error.

Evaluation: spatially blocked K-fold (GroupKFold on square blocks), so
train and test pixels are never neighbours. Baselines: majority class and a
transparent spectral-index rule set. Metrics: OA, Cohen's kappa, macro-F1,
per-class F1 and confusion matrix on the held-out folds (natural class mix).
"""

from __future__ import annotations

from typing import Any

import numpy as np

WC_CLASSES = {10: "tree_cover", 20: "shrubland", 30: "grassland", 40: "cropland", 50: "built_up",
              60: "bare_sparse", 70: "snow_ice", 80: "permanent_water", 90: "herbaceous_wetland",
              95: "mangroves", 100: "moss_lichen"}


def feature_stack(bands: dict[str, np.ndarray], extra: dict[str, np.ndarray] | None = None) -> tuple[np.ndarray, list[str]]:
    from .remote_sensing import compute_indices

    idx = compute_indices(bands, ["ndvi", "mndwi", "ndbi", "ndwi"])
    names = list(bands) + list(idx) + list(extra or {})
    arrs = list(bands.values()) + list(idx.values()) + list((extra or {}).values())
    return np.stack([a.astype("float32") for a in arrs]), names


def rule_baseline(f: dict[str, np.ndarray]) -> np.ndarray:
    """Transparent index thresholds (no training): water, built, tree, crop/other."""
    out = np.full(f["ndvi"].shape, 40, dtype="int16")
    out[f["ndvi"] >= 0.6] = 10
    out[(f["ndbi"] > -0.05) & (f["ndvi"] < 0.45)] = 50
    out[f["mndwi"] > 0.0] = 80
    return out


def block_ids(rows: np.ndarray, cols: np.ndarray, block_px: int, ncols: int) -> np.ndarray:
    return (rows // block_px) * (ncols // block_px + 1) + (cols // block_px)


def metrics(y_true: np.ndarray, y_pred: np.ndarray, labels: list[int]) -> dict[str, Any]:
    from sklearn.metrics import cohen_kappa_score, confusion_matrix, f1_score

    cm = confusion_matrix(y_true, y_pred, labels=labels)
    f1 = f1_score(y_true, y_pred, labels=labels, average=None, zero_division=0)
    present = [i for i, l in enumerate(labels) if (y_true == l).any()]
    return {"n": int(y_true.size), "overall_accuracy": float((y_true == y_pred).mean()),
            "kappa": float(cohen_kappa_score(y_true, y_pred)),
            "macro_f1_present_classes": float(np.mean(f1[present])) if present else 0.0,
            "per_class_f1": {WC_CLASSES.get(l, str(l)): round(float(f1[i]), 4) for i, l in enumerate(labels)},
            "support": {WC_CLASSES.get(l, str(l)): int((y_true == l).sum()) for l in labels},
            "labels": labels, "confusion_matrix": cm.tolist()}


def spatial_cv(X: np.ndarray, y: np.ndarray, groups: np.ndarray, base_feats: dict[str, np.ndarray],
               labels: list[int], k: int = 5, seed: int = 42, n_estimators: int = 150) -> dict[str, Any]:
    from sklearn.ensemble import RandomForestClassifier
    from sklearn.model_selection import GroupKFold

    gkf = GroupKFold(n_splits=k)
    preds = {"random_forest": np.zeros_like(y), "majority": np.zeros_like(y), "index_rules": np.zeros_like(y)}
    fold_scores = []
    rule_all = rule_baseline(base_feats)
    for fold, (tr, te) in enumerate(gkf.split(X, y, groups)):
        rf = RandomForestClassifier(n_estimators=n_estimators, min_samples_leaf=2, n_jobs=-1, random_state=seed + fold,
                                    class_weight="balanced_subsample", max_features="sqrt")
        rf.fit(X[tr], y[tr])
        preds["random_forest"][te] = rf.predict(X[te])
        vals, cnt = np.unique(y[tr], return_counts=True)
        preds["majority"][te] = vals[np.argmax(cnt)]
        preds["index_rules"][te] = rule_all[te]
        fold_scores.append(float((preds["random_forest"][te] == y[te]).mean()))
    out = {name: metrics(y, p, labels) for name, p in preds.items()}
    out["random_forest"]["fold_overall_accuracy"] = fold_scores
    out["random_forest"]["fold_oa_std"] = float(np.std(fold_scores))
    return out
