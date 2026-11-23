"""Star-rating preference learning: clips the user rates teach the scorer.

Every 1-5 star rating is appended to DATA_DIR/feedback.jsonl together
with the clip's feature values (loud/shout/motion/... from
signals.window_features). Once at least 8 rated clips with features
exist, learned_weights() nudges each scoring weight by how strongly
that feature correlates with the ratings (Pearson): features the user
keeps rating highly count for more, features in low-rated clips for less.

Simplified from NeonClipping learn.py. Pure stdlib;
learned_weights() never raises (any problem returns the base weights).
"""
import json
import math
import time
from pathlib import Path

from . import config

MIN_RATINGS = 8
_GAIN = 0.6
_LO, _HI = 0.02, 0.5

# Fallback scoring weights if brain cannot be imported (mirrors brain._WEIGHTS).
_FALLBACK_WEIGHTS = {"loud": .12, "shout": .10, "spike": .08, "words": .16,
                     "motion": .12, "cuts": .02, "chat": .06, "game": .10, "llm": .24}


def _feedback_path() -> Path:
    return config.DATA_DIR / "feedback.jsonl"


def _numeric_features(features) -> dict:
    """Only finite numeric values survive (bools and None are dropped)."""
    out = {}
    if isinstance(features, dict):
        for key, value in features.items():
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                continue
            if math.isfinite(value):
                out[str(key)] = float(value)
    return out


def record_rating(job_id, clip_idx, rating, features, layout="", style="") -> dict:
    """Append one star rating to feedback.jsonl and return the stored row.

    rating is clamped to 1..5; features keeps numeric values only.
    Row shape: {"t", "job", "clip", "rating", "features", "layout", "style"}."""
    row = {
        "t": round(time.time(), 3),
        "job": str(job_id),
        "clip": int(clip_idx),
        "rating": int(max(1, min(5, round(float(rating))))),
        "features": _numeric_features(features),
        "layout": str(layout or ""),
        "style": str(style or ""),
    }
    config.DATA_DIR.mkdir(parents=True, exist_ok=True)
    with open(_feedback_path(), "a", encoding="utf-8") as fh:
        fh.write(json.dumps(row, ensure_ascii=False) + "\n")
    return row


def _read_rows() -> list:
    """All parseable feedback rows (bad lines are skipped). Never raises."""
    rows = []
    try:
        with open(_feedback_path(), encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except ValueError:
                    continue
                if isinstance(row, dict) and isinstance(row.get("rating"), (int, float)):
                    rows.append(row)
    except OSError:
        pass
    return rows


def _pearson(xs, ys) -> float:
    """Pearson correlation, 0.0 when undefined (too few points / no variance)."""
    n = len(xs)
    if n < 3:
        return 0.0
    mx = sum(xs) / n
    my = sum(ys) / n
    sxy = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    sxx = sum((x - mx) ** 2 for x in xs)
    syy = sum((y - my) ** 2 for y in ys)
    if sxx <= 0 or syy <= 0:
        return 0.0
    return max(-1.0, min(1.0, sxy / math.sqrt(sxx * syy)))


def learned_weights(base: dict) -> dict:
    """Base scoring weights nudged by star ratings (Pearson per feature).

    With >= 8 feature-bearing ratings: new = base * (1 + 0.6*corr),
    clamped to [0.02, 0.5], then renormalised to the base weight sum.
    Below that (or on any problem) the base weights come back unchanged.
    Never raises."""
    try:
        if not isinstance(base, dict) or not base:
            return base
        out = dict(base)
        rows = [r for r in _read_rows() if _numeric_features(r.get("features"))]
        if len(rows) < MIN_RATINGS:
            return out
        numeric = {k: float(v) for k, v in base.items()
                   if isinstance(v, (int, float)) and not isinstance(v, bool)
                   and math.isfinite(v)}
        if not numeric:
            return out
        adjusted = {}
        for key, weight in numeric.items():
            xs, ys = [], []
            for row in rows:
                feats = row.get("features")
                value = feats.get(key) if isinstance(feats, dict) else None
                if isinstance(value, (int, float)) and not isinstance(value, bool) \
                        and math.isfinite(value):
                    xs.append(float(value))
                    ys.append(float(row["rating"]))
            corr = _pearson(xs, ys)
            adjusted[key] = max(_LO, min(_HI, weight * (1.0 + _GAIN * corr)))
        base_sum = sum(numeric.values())
        adj_sum = sum(adjusted.values())
        if adj_sum <= 0 or base_sum <= 0:
            return out
        scale = base_sum / adj_sum
        for key, value in adjusted.items():
            out[key] = round(value * scale, 4)
        return out
    except Exception:  # noqa: BLE001 - learning must never break scoring
        return dict(base) if isinstance(base, dict) else base


def _base_weights() -> dict:
    """brain._WEIGHTS when importable, else the local mirror."""
    try:
        from . import brain
        weights = getattr(brain, "_WEIGHTS", None)
        if isinstance(weights, dict) and weights:
            return dict(weights)
    except Exception:  # noqa: BLE001
        pass
    return dict(_FALLBACK_WEIGHTS)


def summary() -> dict:
    """{"ratings", "avg", "fitted", "weights"} over all stored ratings.

    fitted once >= 8 ratings carry features; weights is then the learned
    adjustment of the brain's base weights (None before that)."""
    rows = _read_rows()
    n = len(rows)
    avg = round(sum(float(r["rating"]) for r in rows) / n, 2) if n else 0.0
    fitted = len([r for r in rows if _numeric_features(r.get("features"))]) >= MIN_RATINGS
    return {"ratings": n, "avg": avg, "fitted": fitted,
            "weights": learned_weights(_base_weights()) if fitted else None}
