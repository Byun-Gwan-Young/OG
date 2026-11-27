"""테스트 기대값을 data/·seed/ JSON에서 읽는 도우미 (읽기 전용).

티켓 수·다음 번호·사원/규정 수·이름·비밀번호처럼 데이터 담당이 바꿀 수 있는 값은
테스트에 숫자로 박지 않고 여기서 읽는다. PRD 시연 기대값(식대 12,000원, 잔여 11일,
REQ-004 등)은 의도된 검증이므로 테스트에 그대로 둔다.
"""
from __future__ import annotations

import json
import re
from functools import lru_cache
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DATA_ROOT = ROOT / "data"
SEED_ROOT = ROOT / "seed"


@lru_cache(maxsize=None)
def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def data_doc(company_id: str, kind: str) -> dict:
    """data/<회사>/<kind>.json (호출마다 새 객체)."""
    return json.loads(_read(DATA_ROOT / company_id / f"{kind}.json"))


def baseline_tickets_doc(company_id: str) -> dict:
    """--reset-tickets 기준 티켓 문서: seed/<회사>/tickets.json, 없으면 data/<회사>/tickets.json."""
    seed_path = SEED_ROOT / company_id / "tickets.json"
    if seed_path.is_file():
        return json.loads(_read(seed_path))
    return data_doc(company_id, "tickets")


def next_sequence(document: dict) -> int:
    """가져오기 직후 다음 티켓 번호: max(next_sequence, 최대 번호 + 1)."""
    numbers = [
        int(m.group(1))
        for t in document.get("tickets") or []
        if (m := re.search(r"(\d+)$", str(t.get("ticket_id", ""))))
    ]
    return max(int(document.get("next_sequence") or 1), max(numbers, default=0) + 1)


def baseline_ticket_count(company_id: str, employee_id: str | None = None) -> int:
    tickets = baseline_tickets_doc(company_id)["tickets"]
    if employee_id is not None:
        tickets = [t for t in tickets if t["employee_id"] == employee_id]
    return len(tickets)


def baseline_next_sequence(company_id: str) -> int:
    return next_sequence(baseline_tickets_doc(company_id))


def employee(company_id: str, employee_id: str) -> dict:
    return next(e for e in data_doc(company_id, "employees")["employees"] if e["employee_id"] == employee_id)


def password(company_id: str, employee_id: str) -> str:
    return str(employee(company_id, employee_id)["password"])


def count(company_id: str, kind: str) -> int:
    """data/<회사>/<kind>.json의 주 목록 길이 (policies, employees 등)."""
    return len(data_doc(company_id, kind)[kind])
