"""Labels schema, play matcher and the precision gate (game understanding, 4.6).

Labels are hand-written once per game (`WORKSPACE/labels/<asset>.json`) in
recording seconds. A detected play matches a label when the windows overlap
by IoU >= `iou_min` and the starts are within `start_tol_s` (R8). Pure; the
CLI lives in `plays_eval_cli`."""

from __future__ import annotations

from collections import defaultdict
from pathlib import Path

from pydantic import BaseModel, Field

from ..config import settings
from .play_models import Play

FALLBACK_LEAD_S = 8.0
FALLBACK_TAIL_S = 3.0


class LabelRow(BaseModel):
    type: str
    approach_s: float
    engage_s: float
    outcome_s: float
    numbers: str | None = None
    flank: bool | None = None
    note: str = ""


class Labels(BaseModel):
    asset_id: str
    rows: list[LabelRow] = Field(default_factory=list)


class EvalReport(BaseModel):
    precision: float
    recall: float
    matched: list[tuple[str, int]]
    unmatched_detected: list[str]
    unmatched_labels: list[int]
    per_type: dict[str, dict[str, float]]
    type_confusion: list[tuple[str, str]]


def labels_path(asset_id: str) -> Path:
    return Path(settings.workspace_dir) / "labels" / f"{asset_id}.json"


def load_labels(path: Path) -> Labels:
    return Labels.model_validate_json(path.read_text(encoding="utf-8"))


def detected_window(p: Play) -> tuple[float, float]:
    w = p.window
    start = (
        w.approach_s
        if w.approach_s is not None
        else p.recording_time.first_kill_s - FALLBACK_LEAD_S
    )
    end = w.outcome_s if w.outcome_s is not None else p.recording_time.last_kill_s + FALLBACK_TAIL_S
    return start, end


def _iou(a: tuple[float, float], b: tuple[float, float]) -> float:
    inter = max(0.0, min(a[1], b[1]) - max(a[0], b[0]))
    union = (a[1] - a[0]) + (b[1] - b[0]) - inter
    return inter / union if union > 0 else 0.0


def _ratio(num: int, den: int) -> float:
    return num / den if den else 0.0


def match_plays(
    detected: list[Play],
    labels: list[LabelRow],
    *,
    iou_min: float = 0.5,
    start_tol_s: float = 5.0,
) -> EvalReport:
    windows = [detected_window(p) for p in detected]
    pairs: list[tuple[float, int, int]] = []
    for di, dw in enumerate(windows):
        for li, lab in enumerate(labels):
            iou = _iou(dw, (lab.approach_s, lab.outcome_s))
            if iou >= iou_min and abs(dw[0] - lab.approach_s) <= start_tol_s:
                pairs.append((iou, di, li))
    pairs.sort(key=lambda t: -t[0])

    used_d: set[int] = set()
    used_l: set[int] = set()
    matched: list[tuple[str, int]] = []
    confusion: list[tuple[str, str]] = []
    typed_hits: dict[str, int] = defaultdict(int)
    for _score, di, li in pairs:
        if di in used_d or li in used_l:
            continue
        used_d.add(di)
        used_l.add(li)
        matched.append((detected[di].id, li))
        if detected[di].type == labels[li].type:
            typed_hits[labels[li].type] += 1
        else:
            confusion.append((labels[li].type, detected[di].type))

    n_det: dict[str, int] = defaultdict(int)
    for p in detected:
        n_det[p.type] += 1
    n_lab: dict[str, int] = defaultdict(int)
    for r in labels:
        n_lab[r.type] += 1
    per_type = {
        t: {
            "precision": _ratio(typed_hits[t], n_det[t]),
            "recall": _ratio(typed_hits[t], n_lab[t]),
            "n_labels": float(n_lab[t]),
            "n_detected": float(n_det[t]),
        }
        for t in sorted(set(n_det) | set(n_lab))
    }
    return EvalReport(
        precision=_ratio(len(matched), len(detected)),
        recall=_ratio(len(matched), len(labels)),
        matched=matched,
        unmatched_detected=[p.id for i, p in enumerate(detected) if i not in used_d],
        unmatched_labels=[i for i in range(len(labels)) if i not in used_l],
        per_type=per_type,
        type_confusion=confusion,
    )


def passes_gate(report: EvalReport, min_precision: float = 0.8) -> bool:
    return report.precision >= min_precision
