"""격자 100단계 가중치 로더.

SPEC.md 51~53줄: "가중치는 재유도하지 말고 엑셀 E열 100개 값을 그대로
config/weights.csv로 추출해서 쓴다. 로드 시 sum(weights) == 17130 을 assert 하라."
"""
from __future__ import annotations

import csv
from decimal import Decimal
from pathlib import Path

WEIGHTS_CSV_PATH = Path(__file__).resolve().parent.parent / "config" / "weights.csv"
EXPECTED_SUM = Decimal("17130")
EXPECTED_COUNT = 100


def load_weights(path: Path = WEIGHTS_CSV_PATH) -> list[Decimal]:
    with open(path, encoding="utf-8") as f:
        reader = csv.DictReader(f)
        weights = [Decimal(row["weight"]) for row in reader]

    if len(weights) != EXPECTED_COUNT:
        raise ValueError(f"weights count mismatch: expected {EXPECTED_COUNT}, got {len(weights)}")
    total = sum(weights)
    if total != EXPECTED_SUM:
        raise ValueError(f"weights sum mismatch: expected {EXPECTED_SUM}, got {total}")

    return weights
