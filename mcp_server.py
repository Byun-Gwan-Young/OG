"""
오피스지니 (OfficeGenie) MCP 서버
=================================

회사 데이터를 DB(storage.py: PostgreSQL 또는 로컬 SQLite)에서 읽어 온보딩 툴을 제공하는 FastMCP 서버.
서버는 실행 중에 JSON 파일을 읽거나 쓰지 않는다. 회사 JSON은 scripts/import_company.py로 DB에 넣는다.

실행:   python mcp_server.py                                   (stdio, agent.py가 자동으로 띄움)
가져오기: python scripts/import_company.py --all
초기화:   python scripts/import_company.py --all --reset-tickets  (seed/ 티켓으로 시연 데이터 복구)

[보안 원칙]
- 모든 툴은 company_id, employee_id(세션 값)를 인자로 받는다.
  이 두 값은 agent.py가 UI 세션에서 주입하며, LLM에게는 스키마에서 숨긴다.
  → LLM이 다른 사번을 임의로 지정해 조회할 수 없다.
- 타인/타 부서 조회 요청은 서버에서 PERMISSION_DENIED로 차단한다.
- DB에 없는 정보는 NOT_FOUND로 반환하고, LLM이 추측하지 않도록 고정 문구를 준다.
- 비품 티켓은 화이트리스트·수량 한도를 서버에서 검증하고, confirmed=True일 때만 저장한다.
"""

from __future__ import annotations

import argparse
import hmac
import logging
import math
import os
import re
import sys
from functools import wraps
from datetime import date, datetime, timedelta, timezone
from typing import Any, Literal

from fastmcp import FastMCP

import company_data
import storage

# ---------------------------------------------------------------------------
# 설정
# ---------------------------------------------------------------------------

KST = timezone(timedelta(hours=9))
DUPLICATE_WINDOW_SEC = 10  # 같은 신청이 이 시간 안에 다시 오면 새 티켓을 만들지 않음
log = logging.getLogger("officegenie.mcp")

# 사용자에게 그대로 보여줄 고정 문구 (UI·LLM 공통)
MSG = {
    "DENIED_OR_NOT_FOUND": (
        "사내 시스템에 등록되지 않았거나 열람 권한이 없는 정보입니다. "
        "본인 정보 및 소속 부서 문서만 조회 가능하며, "
        "추가 문의는 인사총무팀 공식 메일로 연락해 주세요."
    ),
    "INVALID_SESSION": "로그인 정보를 확인할 수 없습니다. 다시 로그인해 주세요.",
    "INVALID_INPUT": "요청 내용을 이해하지 못했습니다. 조금 더 구체적으로 입력해 주세요.",
    "INVALID_QUANTITY": "신청 수량은 1 이상의 숫자로 입력해 주세요.",
    "NEEDS_CONFIRMATION": "아래 내용으로 신청을 접수할까요? 확인하시면 티켓이 발급됩니다.",
    "SERVER_UNAVAILABLE": "사내 시스템 연결이 일시적으로 원활하지 않습니다. 잠시 후 다시 시도해 주세요.",
    # 로그인: 무엇이 틀렸는지(회사 코드/아이디/비밀번호) 구분하지 않는다.
    "LOGIN_FAILED": "회사 코드, 아이디 또는 비밀번호가 올바르지 않습니다.",
    "LOGIN_LOCKED": (
        "로그인에 5회 연속 실패해 10분간 잠겼습니다. 잠시 후 다시 시도하거나 회사 관리자에게 문의하세요."
    ),
    "PASSWORD_WRONG": "현재 비밀번호가 올바르지 않습니다.",
    "PASSWORD_SAME": "새 비밀번호는 이전 비밀번호와 달라야 합니다.",
    "PASSWORD_CHANGED": "비밀번호를 변경했습니다.",
    "SIGNUP_CODE_INVALID": "서비스 가입 코드가 올바르지 않습니다. 서비스 운영자에게 받은 코드를 입력해 주세요.",
    "COMPANY_NAME_INVALID": "회사명을 1자 이상 100자 이하로 입력해 주세요.",
    "ADMIN_PASSWORD_SHORT": "관리자 비밀번호는 8자 이상이어야 합니다.",
    "ADMIN_ONLY": "관리자만 사용할 수 있는 기능입니다.",
    "UPLOAD_EMPTY": "올릴 파일을 선택해 주세요.",
    "UPLOAD_INVALID": "파일에 문제가 있어 미리보기를 만들지 못했습니다. 아래 표를 확인해 주세요.",
    "UPLOAD_PREVIEW": "변경 내용을 확인한 뒤 '반영하기'를 눌러 주세요. 아직 반영되지 않았습니다.",
    "UPLOAD_STALE": "미리보기 이후 데이터나 파일이 바뀌었습니다. 다시 미리보기를 만들어 주세요.",
    "UPLOAD_APPLIED": "반영했습니다.",
    "TICKET_NOT_FOUND": "신청 내역을 찾을 수 없습니다.",
    "TICKET_BAD_TRANSITION": "이 상태로는 바꿀 수 없습니다. 접수완료 → 검토중 → 승인 → 지급완료 순서로만 진행됩니다.",
    "TICKET_NOTE_REQUIRED": "반려할 때는 사유를 입력해 주세요.",
    "TICKET_UPDATED": "신청 상태를 변경했습니다.",
    "ACCOUNT_NOT_FOUND": "해당 사원 계정을 찾을 수 없습니다.",
    "PASSWORD_RESET": "초기 비밀번호를 새로 만들었습니다. 이 화면에서만 한 번 보입니다.",
    "ACCOUNT_UNLOCKED": "잠금을 해제했습니다.",
    "COMPANY_REGISTERED": "회사를 등록했습니다. 아래 회사 코드를 사원들에게 알려주세요.",
}

mcp = FastMCP(
    name="OfficeGenie",
    instructions=(
        "중소기업 신규 입사자용 온보딩 툴. 사내 규정 검색, 본인 인사 정보, 사내 장소, "
        "소속 부서 인수인계서, 비품·계정 신청 티켓을 제공한다."
    ),
)

# ---------------------------------------------------------------------------
# 공통 유틸
# ---------------------------------------------------------------------------


def ok(code: str = "OK", data: Any = None, message: str = "") -> dict:
    return {"ok": True, "code": code, "message": message, "data": data}


def fail(code: str, message: str, data: Any = None) -> dict:
    return {"ok": False, "code": code, "message": message, "data": data}


def _storage_safe(function):
    """Keep database failures inside the existing MCP result envelope."""

    @wraps(function)
    def wrapped(*args, **kwargs):
        try:
            with storage.track_database_roundtrips(function.__name__):
                return function(*args, **kwargs)
        except storage.StorageUnavailable:
            log.warning("MCP database unavailable tool=%s", function.__name__)
            return fail("SERVER_UNAVAILABLE", MSG["SERVER_UNAVAILABLE"])

    return wrapped


def _norm(text: str | None) -> str:
    """비교용 정규화: 공백 제거 + 소문자."""
    return re.sub(r"\s+", "", (text or "")).lower()


def list_company_ids() -> list[str]:
    return storage.list_company_ids()


def load(company_id: str, name: str) -> dict:
    """회사 문서를 DB에서 읽는다 (레거시 내부 호출 형식 호환)."""
    return storage.get_company_document(company_id, name)


def resolve_session(
    company_id: str, employee_id: str
) -> tuple[dict | None, dict | None]:
    """(사원 정보, 에러 응답) 반환. 세션 값이 잘못되면 에러 응답을 준다."""
    try:
        employee = storage.get_employee(company_id, employee_id)
    except KeyError:
        return None, fail("INVALID_SESSION", MSG["INVALID_SESSION"])
    if employee is None:
        return None, fail("INVALID_SESSION", MSG["INVALID_SESSION"])
    return employee, None


def department_aliases(company_id: str) -> dict[str, str]:
    """부서명 별칭 → 정식 부서명. (locations 데이터의 부서 위치 별칭을 재사용)"""
    departments = {e["department"] for e in load(company_id, "employees")["employees"]}
    mapping = {_norm(d): d for d in departments}
    for loc in load(company_id, "locations")["locations"]:
        if loc["name"] in departments:
            for alias in loc.get("aliases", []):
                mapping[_norm(alias)] = loc["name"]
    return mapping


# ---------------------------------------------------------------------------
# 1. 사내 규정 검색 (MVP)
# ---------------------------------------------------------------------------

_PARTICLES = (
    "이랑",
    "에서",
    "으로",
    "이야",
    "인가요",
    "인가",
    "나요",
    "까지",
    "부터",
    "은",
    "는",
    "이",
    "가",
    "을",
    "를",
    "랑",
    "와",
    "과",
    "도",
    "의",
    "에",
    "로",
    "야",
    "요",
)


def _tokens(query: str) -> list[str]:
    tokens = []
    for raw in re.split(r"[\s,.?!·/]+", query):
        tok = raw.strip()
        for p in _PARTICLES:
            if len(tok) > len(p) + 1 and tok.endswith(p):
                tok = tok[: -len(p)]
                break
        if len(tok) >= 2:
            tokens.append(tok)
    return tokens


def _score_policy(policy: dict, query_n: str, tokens: list[str]) -> float:
    score = 0.0
    keywords = [_norm(k) for k in policy.get("keywords", [])]
    title_n = _norm(policy.get("title"))
    content_n = _norm(policy.get("content"))
    # 키워드 전체가 질문 안에 들어 있으면 강하게 가산 (긴 키워드일수록 정확)
    for kw in keywords:
        if len(kw) >= 2 and kw in query_n:
            score += 2 + len(kw) * 0.5
    if title_n and title_n in query_n:
        score += 4
    # 질문의 단어가 제목/키워드/본문에 들어 있으면 가산
    for tok in tokens:
        t = _norm(tok)
        if t in title_n:
            score += 2
        if any(t in kw for kw in keywords):
            score += 1
        if t in content_n:
            score += 0.5
    return score


@mcp.tool
@_storage_safe
def search_policy(query: str, company_id: str, employee_id: str) -> dict:
    """사내 규정(취업규칙·복리후생·비품·계정·재택·회의실 등)을 검색한다.

    호출 조건: 사용자가 회사 규칙·기준·한도·절차를 물을 때.
    (예: 식대 한도, 야근 식대, 재택근무, 비품 신청 가능 품목, 연차 부여 기준)
    호출 금지: '내 남은 연차'처럼 본인 개인 수치는 get_employee_info를 사용한다.

    query: 규정 주제를 짧게 (예: "점심 식대 한도", "재택근무 신청").
    결과의 policy_type이 standard_reference이면 회사가 실제 채택한 규정이 아닌
    표준취업규칙 참고 조문이므로, 답변에 '참고용'임을 반드시 밝힌다.
    """
    _, err = resolve_session(company_id, employee_id)
    if err:
        return err
    if not query or not query.strip():
        return fail("INVALID_INPUT", MSG["INVALID_INPUT"])

    doc = load(company_id, "policies")
    config = doc.get("search_policy_config", {})
    preferred = config.get("preferred_policy_type", "company")
    query_n, tokens = _norm(query), _tokens(query)

    scored = []
    for p in doc["policies"]:
        s = _score_policy(p, query_n, tokens)
        if s <= 0:
            continue
        if p.get("policy_type") == preferred:
            s *= 1.5  # 회사 내규 우선
        scored.append((s, p))
    scored.sort(key=lambda x: x[0], reverse=True)

    if not scored or scored[0][0] < 3:
        return fail("NOT_FOUND", MSG["DENIED_OR_NOT_FOUND"], {"query": query})

    top = scored[0][0]
    results = []
    for s, p in scored[:3]:
        if s < top * 0.4:  # 1위와 점수 차가 크면 제외 (엉뚱한 조항 노출 방지)
            break
        results.append(
            {
                "policy_id": p["id"],
                "category": p["category"],
                "article": p["article"],
                "title": p["title"],
                "content": p["content"],
                "policy_type": p["policy_type"],
                "citation": f"{p['category']} {p['article']} ({p['title']})",
                "is_reference_only": p["policy_type"] != "company",
            }
        )
    return ok(
        data={
            "company_name": doc.get("company_name") or doc.get("company"),
            "results": results,
            "reference_disclaimer": config.get("fallback_disclaimer"),
        }
    )


# ---------------------------------------------------------------------------
# 2. 본인 인사 정보 (MVP)
# ---------------------------------------------------------------------------


@mcp.tool
@_storage_safe
def get_employee_info(
    company_id: str, employee_id: str, target: str | None = None
) -> dict:
    """로그인한 사용자 '본인'의 인사 정보(이름, 부서, 직급, 잔여 연차)를 조회한다.

    호출 조건: "내 남은 연차", "내 부서", "내 직급"처럼 본인 정보를 물을 때.
    target: 사용자가 다른 사람(사번 또는 이름)의 정보를 물었을 때만 그 값을 그대로 넣는다.
            (예: "EMP002 연차" → target="EMP002", "박대리 연차" → target="박대리")
            본인 정보를 물으면 비워 둔다. 타인 정보는 서버가 차단한다.
    """
    me, err = resolve_session(company_id, employee_id)
    if err:
        return err
    if target and target.strip():
        t = _norm(target)
        if t not in {_norm(me["employee_id"]), _norm(me["name"]), "나", "본인", "내"}:
            return fail(
                "PERMISSION_DENIED", MSG["DENIED_OR_NOT_FOUND"], {"target": target}
            )
    leave = me.get("annual_leave", {})
    return ok(
        data={
            "employee_id": me["employee_id"],
            "name": me["name"],
            "department": me["department"],
            "position": me.get("position"),
            "job_title": me.get("job_title"),
            "employment_status": me.get("employment_status"),
            "onboarding_week": me.get("onboarding_week"),
            "annual_leave": {
                "granted_days": leave.get("granted_days"),
                "used_days": leave.get("used_days"),
                "remaining_days": leave.get("remaining_days"),
                "as_of": leave.get("as_of"),
            },
        }
    )


# ---------------------------------------------------------------------------
# 3. 사내 장소 조회 (추가 기능)
# ---------------------------------------------------------------------------


@mcp.tool
@_storage_safe
def get_office_location(place: str, company_id: str, employee_id: str) -> dict:
    """사옥 안 장소(회의실, 복합기, 휴게실, 부서 자리, 교육실 등)의 건물·층·구역을 조회한다.

    호출 조건: "어디야", "위치", "몇 층" 등 장소를 물을 때.
    place: 장소 이름만 짧게 (예: "대회의실", "복합기", "탕비실").
    등록되지 않은 장소는 NOT_FOUND를 반환한다. 이때 위치를 추측하지 말 것.
    """
    _, err = resolve_session(company_id, employee_id)
    if err:
        return err
    place_n = _norm(place)
    if len(place_n) < 2:
        return fail("INVALID_INPUT", MSG["INVALID_INPUT"])

    doc = load(company_id, "locations")
    for blocked in doc.get("explicitly_not_registered", []):
        b = _norm(blocked)
        if b in place_n or place_n in b:
            return fail("NOT_FOUND", MSG["DENIED_OR_NOT_FOUND"], {"place": place})

    matches = []
    for loc in doc["locations"]:
        names = [loc["name"], *loc.get("aliases", [])]
        best = 0
        for name in names:
            n = _norm(name)
            if n == place_n:
                best = max(best, 100)
            elif n in place_n or place_n in n:
                best = max(best, len(n))
        if best:
            matches.append((best, loc))
    if not matches:
        return fail("NOT_FOUND", MSG["DENIED_OR_NOT_FOUND"], {"place": place})

    matches.sort(key=lambda x: x[0], reverse=True)
    top = matches[0][0]
    picked = [loc for s, loc in matches if s == top or top < 100][:5]
    return ok(
        data={
            "locations": [
                {
                    "name": loc["name"],
                    "building": loc["building"],
                    "floor": loc["floor"],
                    "zone": loc["zone"],
                    "guide": loc.get("guide"),
                    "access": loc.get("access"),
                    "capacity": loc.get("capacity"),
                    "display": f"{loc['building']} {loc['floor']}층 {loc['zone']}",
                }
                for loc in picked
            ]
        }
    )


# ---------------------------------------------------------------------------
# 4. 소속 부서 인수인계서 (추가 기능)
# ---------------------------------------------------------------------------


@mcp.tool
@_storage_safe
def get_handover_doc(
    company_id: str, employee_id: str, department: str | None = None
) -> dict:
    """사용자 '소속 부서'의 신입사원 인수인계 업무서를 조회한다.

    호출 조건: "내 직무 인수인계서", "1주 차에 뭐 해야 해" 등.
    department: 사용자가 특정 부서를 직접 언급했을 때만 그 부서명을 넣는다.
                (예: "재무회계팀 인수인계서" → department="재무회계팀")
                본인 부서 문서를 원하면 비워 둔다. 타 부서 문서는 서버가 차단한다.
    답변은 [핵심 담당 업무 / 사용 툴 / 1주 차 할 일] 순서로 요약한다.
    """
    me, err = resolve_session(company_id, employee_id)
    if err:
        return err
    if (
        department
        and department.strip()
        and _norm(department) not in {"내", "우리", "우리팀", "본인", "내부서"}
    ):
        canonical = department_aliases(company_id).get(_norm(department))
        if canonical != me["department"]:
            return fail(
                "PERMISSION_DENIED",
                MSG["DENIED_OR_NOT_FOUND"],
                {"department": department},
            )

    for doc in load(company_id, "handovers")["handovers"]:
        if doc["department"] == me["department"]:
            return ok(
                data={
                    "handover_id": doc["handover_id"],
                    "department": doc["department"],
                    "title": doc["title"],
                    "last_updated": doc.get("last_updated"),
                    "summary": doc.get("summary", {}),
                    "notes": doc.get("notes", []),
                }
            )
    return fail(
        "NOT_FOUND", MSG["DENIED_OR_NOT_FOUND"], {"department": me["department"]}
    )


# ---------------------------------------------------------------------------
# 5. 비품·계정 신청 티켓 (추가 기능)
# ---------------------------------------------------------------------------


def _canonical_item(item: str, allowed: list[dict]) -> dict | None:
    item_n = _norm(item)
    # 정확히 일치하는 이름/별칭 우선, 없으면 포함 관계 중 가장 긴 것
    best, best_len = None, 0
    for eq in allowed:
        for name in [eq["item"], *eq.get("aliases", [])]:
            n = _norm(name)
            if n == item_n:
                return eq
            if n in item_n and len(n) > best_len:
                best, best_len = eq, len(n)
    return best


def _held_quantity(
    tickets: list[dict], employee_id: str, eq: dict, allowed: list[dict]
) -> int:
    """이미 지급됐거나 처리 중인 동일 품목 수량 (반려 제외)."""
    total = 0
    for t in tickets:
        if (
            t["employee_id"] != employee_id
            or t.get("type") != "비품"
            or t.get("status") == "반려"
        ):
            continue
        held = _canonical_item(t["item"], allowed)
        if held and held["item"] == eq["item"]:
            total += int(t.get("quantity", 1))
    return total


@mcp.tool
@_storage_safe
def create_support_ticket(
    item: str,
    company_id: str,
    employee_id: str,
    quantity: int = 1,
    purpose: str = "",
    request_type: Literal["비품", "계정"] = "비품",
    confirmed: bool = False,
) -> dict:
    """비품 또는 업무 계정 신청 티켓을 만든다.

    호출 조건: "모니터 신청해 줘", "슬랙 계정 만들어 줘"처럼 신청을 요청할 때.
    item: 품목 또는 서비스명 (예: "듀얼 모니터", "노트북 거치대", "Slack").
    quantity: 수량 (기본 1).
    purpose: 사용자가 말한 사용 목적. 말하지 않았으면 비워 둔다.
    request_type: "비품" 또는 "계정".
    confirmed: 항상 False로 호출한다. 서버가 신청 요약을 돌려주면 사용자 확인 후 UI가 접수를 확정한다.

    허용 목록에 없는 품목·수량 초과는 REJECTED로 반환되며 티켓이 생성되지 않는다.
    """
    me, err = resolve_session(company_id, employee_id)
    if err:
        return err
    if not item or not item.strip():
        return fail("INVALID_INPUT", MSG["INVALID_INPUT"])
    try:
        quantity = int(quantity)
    except (TypeError, ValueError):
        return fail("INVALID_INPUT", MSG["INVALID_QUANTITY"])
    if quantity < 1:
        return fail("INVALID_INPUT", MSG["INVALID_QUANTITY"])

    request_type = (
        "계정" if str(request_type).strip() in {"계정", "account"} else "비품"
    )

    doc = load(company_id, "tickets")
    rule = doc.get("creation_rule", {})
    allowed = doc.get("allowed_equipment", [])
    policy_reference = None
    limit = None

    if request_type == "비품":
        equipment = _canonical_item(item, allowed)
        if not equipment:
            return fail(
                "REJECTED",
                rule.get("unknown_equipment_message", MSG["DENIED_OR_NOT_FOUND"]),
                {
                    "item": item,
                    "reason": "UNKNOWN_EQUIPMENT",
                    "allowed_items": [entry["item"] for entry in allowed],
                },
            )
        item_name = equipment["item"]
        policy_reference = equipment.get("policy_reference")
        limit = equipment.get("max_quantity_per_employee")
    else:
        item_name = item.strip()
        if quantity != 1:
            return fail(
                "REJECTED",
                "업무 계정은 서비스별 1인 1계정만 신청할 수 있습니다.",
                {"item": item_name, "reason": "ACCOUNT_QUANTITY"},
            )

    purpose_text = purpose.strip() or "업무용 (사용 목적 미기재)"

    def validate_quantity(existing_tickets: list[dict]) -> dict | None:
        if (
            request_type == "비품"
            and equipment.get("quantity_limit_defined")
            and limit is not None
        ):
            held = _held_quantity(
                existing_tickets, me["employee_id"], equipment, allowed
            )
            if held + quantity > limit:
                return fail(
                    "REJECTED",
                    rule.get("excess_quantity_message", MSG["DENIED_OR_NOT_FOUND"]),
                    {
                        "item": item_name,
                        "reason": "EXCESS_QUANTITY",
                        "limit": limit,
                        "already_held_or_pending": held,
                        "requested": quantity,
                    },
                )
        return None

    if not confirmed:
        existing_tickets = doc["tickets"]
        rejected = validate_quantity(existing_tickets)
        if rejected is not None:
            return rejected
        pending_same = [
            ticket
            for ticket in existing_tickets
            if ticket["employee_id"] == me["employee_id"]
            and _norm(ticket["item"]) == _norm(item_name)
            and ticket.get("status") in {"접수완료", "검토중"}
        ]
        warnings = []
        if pending_same:
            ids = ", ".join(ticket["ticket_id"] for ticket in pending_same)
            warnings.append(
                f"이미 처리 중인 동일 품목 신청이 있습니다({ids}). 추가 신청이 맞는지 확인해 주세요."
            )
        preview = {
            "employee_id": me["employee_id"],
            "employee_name": me["name"],
            "type": request_type,
            "item": item_name,
            "quantity": quantity,
            "purpose": purpose_text,
            "policy_reference": policy_reference,
            "warnings": warnings,
        }
        return ok("NEEDS_CONFIRMATION", {"preview": preview}, MSG["NEEDS_CONFIRMATION"])

    now = datetime.now(KST)
    outcome = storage.create_ticket_atomic(
        company_id=company_id,
        employee_id=me["employee_id"],
        request_type=request_type,
        item=item_name,
        quantity=quantity,
        purpose=purpose_text,
        initial_status=rule.get("initial_status", "접수완료"),
        now=now,
        duplicate_window_seconds=DUPLICATE_WINDOW_SEC,
        validate_existing=validate_quantity,
    )
    if outcome["kind"] == "rejected":
        return outcome["result"]
    ticket = outcome["ticket"]
    if outcome["kind"] == "duplicate":
        return ok(
            "DUPLICATE_IGNORED",
            {"ticket": ticket},
            f"이미 접수된 신청입니다. 티켓 번호: {ticket['ticket_id']}",
        )
    return ok(
        "CREATED",
        {"ticket": ticket},
        f"신청이 접수되었습니다. 티켓 번호: {ticket['ticket_id']} ({item_name} {quantity}개, 상태: {ticket['status']})",
    )


# ---------------------------------------------------------------------------
# UI 전용 툴 (LLM에게는 노출하지 않음 — agent.py의 LLM_TOOLS 참고)
# ---------------------------------------------------------------------------


def _password_too_short(role: str) -> str:
    minimum = (
        storage.ADMIN_MIN_PASSWORD
        if role == storage.ROLE_ADMIN
        else storage.EMPLOYEE_MIN_PASSWORD
    )
    return f"새 비밀번호는 {minimum}자 이상이어야 합니다."


def _input_shape(value: object) -> str:
    """입력 형태만 요약한다(값·길이는 남기지 않음). 자동완성·전각 문자·공백 진단용."""
    text = "" if value is None else str(value)
    flags = []
    if not text:
        return "empty"
    if text != text.strip():
        flags.append("surrounding_space")
    if any(ord(ch) > 127 for ch in text):
        flags.append("non_ascii")
    if text.strip().isdigit() and text.strip().isascii():
        flags.append("ascii_digits")
    return ",".join(flags) or "other"


def _log_login_failure(
    result: dict, company_code: object, account_id: object, password: object
) -> None:
    # 비밀번호·해시·DB 주소는 남기지 않는다. 아이디는 공개 식별자라 그대로 남긴다.
    log.warning(
        "login failed reason=%s company_id=%s code_shape=%s account_id=%r password_shape=%s",
        result.get("reason", "unknown"),
        result.get("company_id", "-"),
        _input_shape(company_code),
        str(account_id or "")[:40],
        _input_shape(password),
    )


@mcp.tool
@_storage_safe
def ui_login(company_code: str, account_id: str, password: str) -> dict:
    """[UI 전용] 회사 코드 + 아이디(사번 또는 ADMIN) + 비밀번호 로그인."""
    result = storage.authenticate(company_code, account_id, password)
    if result["status"] != "ok":
        _log_login_failure(result, company_code, account_id, password)
    if result["status"] == "locked":
        return fail("ACCOUNT_LOCKED", MSG["LOGIN_LOCKED"])
    if result["status"] != "ok":
        return fail("LOGIN_FAILED", MSG["LOGIN_FAILED"])
    company = result["company"]
    base = {
        "company_id": company["company_id"],
        "company_name": company["company_name"],
        "role": result["role"],
        "must_change": result["must_change"],
    }
    if result["role"] == storage.ROLE_ADMIN:
        return ok(data={**base, "account_id": storage.ADMIN_ACCOUNT_ID})
    employee = storage.get_employee(company["company_id"], result["account_id"])
    if employee is None:
        log.warning(
            "login failed reason=account_without_employee company_id=%s account_id=%r",
            company["company_id"],
            result["account_id"],
        )
        return fail("LOGIN_FAILED", MSG["LOGIN_FAILED"])
    return ok(
        data={
            **base,
            "employee_id": employee["employee_id"],
            "name": employee["name"],
            "department": employee["department"],
            "position": employee.get("position") or None,
        }
    )


@mcp.tool
@_storage_safe
def ui_change_password(
    company_id: str, account_id: str, old_password: str, new_password: str
) -> dict:
    """[UI 전용] 본인 비밀번호 변경. 사원 4자 이상, 관리자 8자 이상."""
    account = storage.get_account(company_id, account_id)
    if account is None or account["disabled"]:
        return fail("INVALID_SESSION", MSG["INVALID_SESSION"])
    status = storage.change_password(company_id, account_id, old_password, new_password)
    if status == "ok":
        return ok("PASSWORD_CHANGED", message=MSG["PASSWORD_CHANGED"])
    if status == "locked":
        return fail("ACCOUNT_LOCKED", MSG["LOGIN_LOCKED"])
    if status == "too_short":
        return fail("PASSWORD_TOO_SHORT", _password_too_short(account["role"]))
    if status == "same":
        return fail("PASSWORD_SAME", MSG["PASSWORD_SAME"])
    return fail("PASSWORD_WRONG", MSG["PASSWORD_WRONG"])


def _signup_code() -> str:
    return os.environ.get("OFFICEGENIE_SIGNUP_CODE", "").strip()


@mcp.tool
def ui_signup_settings() -> dict:
    """[UI 전용] 회사 등록 화면 설정. 가입 코드 값 자체는 돌려주지 않는다."""
    return ok(data={"signup_code_required": bool(_signup_code())})


@mcp.tool
@_storage_safe
def ui_register_company(
    company_name: str, admin_password: str, signup_code: str = ""
) -> dict:
    """[UI 전용] 회사 등록: 회사 코드 자동 생성 + ADMIN 계정 생성. 데이터는 비어 있다."""
    expected = _signup_code()
    if expected and not hmac.compare_digest(
        str(signup_code or "").strip().encode("utf-8"), expected.encode("utf-8")
    ):
        return fail("SIGNUP_CODE_INVALID", MSG["SIGNUP_CODE_INVALID"])
    name = (company_name or "").strip()
    if not name or len(name) > storage.COMPANY_NAME_MAX:
        return fail("COMPANY_NAME_INVALID", MSG["COMPANY_NAME_INVALID"])
    if len(admin_password or "") < storage.ADMIN_MIN_PASSWORD:
        return fail("ADMIN_PASSWORD_SHORT", MSG["ADMIN_PASSWORD_SHORT"])
    company = storage.register_company(name, admin_password)
    log.info("company registered company_id=%s", company["company_id"])
    return ok("REGISTERED", data=company, message=MSG["COMPANY_REGISTERED"])


# ---------------------------------------------------------------------------
# 관리자 툴 (UI 전용). 화면 분기를 믿지 않고 매번 accounts로 관리자인지 다시 확인한다.
# 데이터는 항상 인자로 받은 company_id(=로그인한 관리자의 회사)에만 적용한다.
# ---------------------------------------------------------------------------


def _require_admin(company_id: str, actor_account_id: str) -> dict | None:
    account = storage.get_account(str(company_id or ""), str(actor_account_id or ""))
    if (
        account is None
        or account["role"] != storage.ROLE_ADMIN
        or account["disabled"]
    ):
        log.warning("admin tool denied company_id=%s", company_id)
        return fail("PERMISSION_DENIED", MSG["ADMIN_ONLY"])
    return None


@mcp.tool
@_storage_safe
def ui_admin_overview(company_id: str, actor_account_id: str) -> dict:
    """[UI 전용·관리자] 회사 코드, 데이터 종류별 건수, 반영 주기."""
    denied = _require_admin(company_id, actor_account_id)
    if denied:
        return denied
    overview = storage.company_overview(company_id)
    if overview is None:
        return fail("PERMISSION_DENIED", MSG["ADMIN_ONLY"])
    storage.record_admin_view(company_id, actor_account_id, "view_overview")
    return ok(data=overview)


def _parse_upload_files(files: Any) -> tuple[dict[str, dict], list[dict]]:
    """[{name, content}] → (종류별 정규화 문서, 오류 목록). 파일로 저장하지 않는다."""
    documents: dict[str, dict] = {}
    issues: list[dict] = []
    if not isinstance(files, list) or not files:
        return documents, issues
    for item in files:
        if not isinstance(item, dict):
            issues.append(company_data.issue("upload", "파일 형식을 알 수 없습니다."))
            continue
        name = str(item.get("name") or "upload.json")
        content = item.get("content")
        if not isinstance(content, str):
            issues.append(company_data.issue(name, "파일 내용을 읽을 수 없습니다."))
            continue
        document, parse_issues = company_data.parse_upload(name, content.encode("utf-8"))
        if parse_issues:
            issues.extend(parse_issues)
            continue
        kind = company_data.detect_kind(name, document)
        if kind is None:
            issues.append(
                company_data.issue(
                    name,
                    "어떤 데이터인지 알 수 없습니다. 파일 이름을 policies.json, employees.json, "
                    "locations.json, handovers.json, tickets.json 중 하나로 해 주세요.",
                )
            )
            continue
        if kind in documents:
            issues.append(company_data.issue(name, f"{kind}.json이 두 번 올라왔습니다."))
            continue
        normalized = company_data.normalize_upload(kind, document)
        kind_issues = company_data.validate_document(kind, normalized, mode="upload")
        if name != f"{kind}.json":
            for entry in kind_issues:
                entry["file"] = name
                entry["message"] = entry["message"].replace(f"{kind}.json", name, 1)
        issues.extend(kind_issues)
        documents[kind] = normalized
    return documents, issues


def _upload_preview(company_id: str, files: Any) -> tuple[dict | None, dict | None]:
    """(미리보기 데이터, 실패 응답). DB는 바꾸지 않는다."""
    if not isinstance(files, list) or not files:
        return None, fail("UPLOAD_EMPTY", MSG["UPLOAD_EMPTY"])
    documents, issues = _parse_upload_files(files)
    if issues:
        return None, fail(
            "UPLOAD_INVALID", MSG["UPLOAD_INVALID"], data={"issues": issues}
        )
    current = storage.current_company_documents(company_id)
    if current is None:
        return None, fail("PERMISSION_DENIED", MSG["ADMIN_ONLY"])
    version, current_docs = current
    changes = [
        company_data.diff_document(kind, current_docs[kind], documents[kind])
        for kind in company_data.DOC_KINDS
        if kind in documents
    ]
    new_accounts = 0
    if "employees" in documents:
        new_accounts = sum(
            1
            for row in documents["employees"]["employees"]
            if not row.get("password")
            and str(row["employee_id"])
            not in {str(e["employee_id"]) for e in current_docs["employees"]["employees"]}
        )
    return {
        "base_version": version,
        "fingerprint": company_data.upload_fingerprint(files),
        "kinds": [c["kind"] for c in changes],
        "changes": changes,
        "initial_passwords_to_create": new_accounts,
        "documents": documents,
        "current": current_docs,
    }, None


@mcp.tool
@_storage_safe
def ui_admin_preview_upload(
    company_id: str, actor_account_id: str, files: list[dict]
) -> dict:
    """[UI 전용·관리자] 업로드 파일 검증 + 변경 미리보기. DB는 바꾸지 않는다.

    files: [{"name": "policies.json", "content": "<JSON 텍스트>"}]. 파일 속 company_id는 무시.
    """
    denied = _require_admin(company_id, actor_account_id)
    if denied:
        return denied
    preview, error = _upload_preview(company_id, files)
    if error:
        return error
    storage.record_admin_view(company_id, actor_account_id, "upload_preview")
    return ok(
        "PREVIEW",
        data={
            key: preview[key]
            for key in (
                "base_version",
                "fingerprint",
                "kinds",
                "changes",
                "initial_passwords_to_create",
            )
        }
        | {"cache_ttl_seconds": storage.STATIC_CACHE_TTL_SECONDS},
        message=MSG["UPLOAD_PREVIEW"],
    )


@mcp.tool
@_storage_safe
def ui_admin_apply_upload(
    company_id: str,
    actor_account_id: str,
    files: list[dict],
    base_version: int,
    fingerprint: str,
) -> dict:
    """[UI 전용·관리자] 미리보기한 파일을 트랜잭션 하나로 반영하고 data_version을 올린다.

    기존 신청 내역(티켓)과 다음 번호는 바꾸지 않는다. 새로 만든 초기 비밀번호는
    응답의 initial_passwords로 이번 한 번만 돌려준다(DB에는 해시만 저장).
    """
    denied = _require_admin(company_id, actor_account_id)
    if denied:
        return denied
    preview, error = _upload_preview(company_id, files)
    if error:
        return error
    if (
        preview["fingerprint"] != str(fingerprint or "")
        or preview["base_version"] != int(base_version or 0)
    ):
        return fail("UPLOAD_STALE", MSG["UPLOAD_STALE"])
    company = storage.get_company(company_id)
    merged = company_data.merge_documents(preview["current"], preview["documents"])
    try:
        result = storage.import_company_documents(
            company_id,
            merged,
            actor=actor_account_id,
            company_name=company["company_name"],
            expected_version=preview["base_version"],
            preserve_tickets=True,
            audit_action="upload_applied",
            audit_detail={
                "kinds": preview["kinds"],
                "changes": [
                    {k: c[k] for k in ("kind", "added", "updated", "removed")}
                    for c in preview["changes"]
                ],
            },
        )
    except storage.VersionConflict:
        return fail("UPLOAD_STALE", MSG["UPLOAD_STALE"])
    return ok(
        "APPLIED",
        data={
            "data_version": result["data_version"],
            "changes": preview["changes"],
            "initial_passwords": result.get("initial_passwords") or [],
            "cache_ttl_seconds": storage.STATIC_CACHE_TTL_SECONDS,
        },
        message=MSG["UPLOAD_APPLIED"],
    )


@mcp.tool
@_storage_safe
def ui_admin_list_employees(company_id: str, actor_account_id: str) -> dict:
    """[UI 전용·관리자] 사원 표: 사번, 이름, 부서, 직급, 직무, 잔여 연차, 계정 상태."""
    denied = _require_admin(company_id, actor_account_id)
    if denied:
        return denied
    storage.record_admin_view(company_id, actor_account_id, "view_employees")
    return ok(data={"employees": storage.admin_list_employees(company_id)})


@mcp.tool
@_storage_safe
def ui_admin_save_employee(
    company_id: str, actor_account_id: str, employee_id: str, employee_name: str,
    department: str, position: str, remaining_days: float, employment_status: str,
    create: bool,
) -> dict:
    """[UI 전용·관리자] 현재 회사의 사원 등록 또는 인사 정보 수정."""
    denied = _require_admin(company_id, actor_account_id)
    if denied:
        return denied
    employee_id, name, department, position = (
        value.strip() for value in (employee_id, employee_name, department, position)
    )
    if (
        not employee_id or len(employee_id) > 100 or employee_id.upper() == storage.ADMIN_ACCOUNT_ID
        or not name or len(name) > 300 or not department or len(department) > 300
        or not position or len(position) > 200
        or not math.isfinite(remaining_days) or remaining_days < 0
        or employment_status not in {"재직", "휴직", "퇴사"}
    ):
        return fail("INVALID_EMPLOYEE", "사원 정보와 잔여 연차를 확인해 주세요.")
    result = storage.admin_save_employee(
        company_id, employee_id,
        {
            "name": name, "department": department, "position": position,
            "remaining_days": remaining_days, "employment_status": employment_status,
        },
        actor_account_id, create=create,
    )
    if result["status"] == "duplicate":
        return fail("DUPLICATE_EMPLOYEE", "이미 이 회사에 등록된 사번입니다.")
    if result["status"] == "not_found":
        return fail("NOT_FOUND", MSG["DENIED_OR_NOT_FOUND"])
    return ok(
        "REGISTERED" if create else "UPDATED",
        data={"initial_password": result["initial_password"]},
        message="사원이 등록되었습니다." if create else "사원 정보가 저장되었습니다.",
    )


@mcp.tool
@_storage_safe
def ui_admin_employee_detail(
    company_id: str, actor_account_id: str, employee_id: str
) -> dict:
    """[UI 전용·관리자] 사원 상세: 인사 정보, 연차, 신청 내역, 소속 부서 인수인계서 요약."""
    denied = _require_admin(company_id, actor_account_id)
    if denied:
        return denied
    employee = storage.get_employee(company_id, str(employee_id or ""))
    if employee is None:
        return fail("NOT_FOUND", MSG["DENIED_OR_NOT_FOUND"])
    employee = {k: v for k, v in employee.items() if k != "password"}
    handovers = [
        {
            "handover_id": h.get("handover_id"),
            "title": h.get("title"),
            "last_updated": h.get("last_updated"),
            "summary": h.get("summary") or {},
        }
        for h in load(company_id, "handovers")["handovers"]
        if h.get("department") == employee.get("department")
    ]
    account = storage.get_account(company_id, employee["employee_id"])
    status = account["status"] if account else "none"
    storage.record_admin_view(
        company_id, actor_account_id, "view_employee", employee["employee_id"]
    )
    return ok(
        data={
            "employee": employee,
            "annual_leave": employee.get("annual_leave") or {},
            "tickets": storage.get_tickets(company_id, employee["employee_id"]),
            "handovers": handovers,
            "account_status": status,
            "account_status_label": storage.ACCOUNT_STATUS_LABELS[status],
        }
    )


@mcp.tool
@_storage_safe
def ui_admin_list_tickets(company_id: str, actor_account_id: str) -> dict:
    """[UI 전용·관리자] 신청 관리 표(최신순). 각 티켓에 바꿀 수 있는 다음 상태 포함."""
    denied = _require_admin(company_id, actor_account_id)
    if denied:
        return denied
    storage.record_admin_view(company_id, actor_account_id, "view_tickets")
    return ok(data={"tickets": storage.admin_list_tickets(company_id)})


@mcp.tool
@_storage_safe
def ui_admin_update_ticket(
    company_id: str,
    actor_account_id: str,
    ticket_id: str,
    to_status: str,
    note: str = "",
) -> dict:
    """[UI 전용·관리자] 신청 상태 변경. 접수완료→검토중→승인→지급완료, 반려는 사유 필수."""
    denied = _require_admin(company_id, actor_account_id)
    if denied:
        return denied
    result = storage.admin_update_ticket(
        company_id, str(ticket_id or ""), str(to_status or ""), actor_account_id, note
    )
    if result["status"] == "not_found":
        return fail("NOT_FOUND", MSG["TICKET_NOT_FOUND"])
    if result["status"] == "invalid_transition":
        return fail("INVALID_TRANSITION", MSG["TICKET_BAD_TRANSITION"])
    if result["status"] == "note_required":
        return fail("NOTE_REQUIRED", MSG["TICKET_NOTE_REQUIRED"])
    return ok("UPDATED", data={"ticket": result["ticket"]}, message=MSG["TICKET_UPDATED"])


@mcp.tool
@_storage_safe
def ui_admin_save_equipment(
    company_id: str, actor_account_id: str, ticket_id: str,
    to_status: Literal["접수완료", "승인", "지급완료"], expected_status: str,
    scheduled_date: str | None = None, expected_scheduled_date: str | None = None,
) -> dict:
    """[UI 전용·관리자] 비품 지급 상태와 예정일을 한 번에 저장한다."""
    denied = _require_admin(company_id, actor_account_id)
    if denied:
        return denied
    if scheduled_date is not None:
        try:
            scheduled_date = date.fromisoformat(scheduled_date).isoformat()
        except ValueError:
            return fail("INVALID_DATE", "지급 예정일을 확인해 주세요.")
    result = storage.admin_update_ticket(
        company_id, ticket_id, to_status, actor_account_id, equipment_only=True,
        scheduled_date=scheduled_date, expected_status=expected_status,
        expected_scheduled_date=expected_scheduled_date,
    )
    if result["status"] == "not_found":
        return fail("NOT_FOUND", MSG["TICKET_NOT_FOUND"])
    if result["status"] == "stale":
        return fail("STALE_TICKET", "신청 정보가 변경되었습니다. 목록에서 다시 열어 주세요.")
    if result["status"] != "ok":
        return fail("INVALID_TRANSITION", "비품 지급 상태는 접수, 지급 예정, 지급 완료 순으로 변경할 수 있습니다.")
    return ok("UPDATED", data={"ticket": result["ticket"]}, message="비품 신청 정보가 저장되었습니다.")


@mcp.tool
@_storage_safe
def ui_admin_list_announcements(company_id: str, actor_account_id: str) -> dict:
    """[UI 전용·관리자] 현재 회사의 공개·비공개 공지 목록."""
    denied = _require_admin(company_id, actor_account_id)
    if denied:
        return denied
    return ok(data={"announcements": storage.list_announcements(company_id)})


@mcp.tool
@_storage_safe
def ui_admin_save_announcement(
    company_id: str, actor_account_id: str, title: str, body: str, is_public: bool,
    announcement_id: str | None = None,
) -> dict:
    """[UI 전용·관리자] 현재 회사의 공지 작성 또는 수정."""
    denied = _require_admin(company_id, actor_account_id)
    if denied:
        return denied
    title = title.strip()
    if not title or len(title) > 300 or not body.strip() or len(body) > 12000:
        return fail("INVALID_ANNOUNCEMENT", "제목과 본문을 확인해 주세요.")
    if not storage.admin_save_announcement(company_id, actor_account_id, announcement_id, title, body, is_public):
        return fail("NOT_FOUND", MSG["DENIED_OR_NOT_FOUND"])
    message = "공지사항이 등록되었습니다." if announcement_id is None else "공지사항이 저장되었습니다."
    return ok("SAVED", message=message)


@mcp.tool
@_storage_safe
def ui_admin_list_policies(company_id: str, actor_account_id: str) -> dict:
    """[UI 전용·관리자] 챗봇이 사용하는 현재 회사 규정과 조항 원문."""
    denied = _require_admin(company_id, actor_account_id)
    if denied:
        return denied
    document = load(company_id, "policies")
    rows = [
        {
            **{k: row.get(k) for k in ("id", "category", "article", "title", "content", "keywords")},
            "source": row.get("source") or document.get("source_title"),
            "source_version": row.get("source_version") or row.get("version"),
        }
        for row in document["policies"]
    ]
    return ok(data={"policies": rows, "data_version": document.get("data_version")})


@mcp.tool
@_storage_safe
def ui_admin_list_accounts(company_id: str, actor_account_id: str) -> dict:
    """[UI 전용·관리자] 사원 계정 상태 표(정상/잠금/변경 필요/비활성)."""
    denied = _require_admin(company_id, actor_account_id)
    if denied:
        return denied
    storage.record_admin_view(company_id, actor_account_id, "view_accounts")
    return ok(data={"accounts": storage.admin_list_accounts(company_id)})


@mcp.tool
@_storage_safe
def ui_admin_reset_password(
    company_id: str, actor_account_id: str, employee_id: str
) -> dict:
    """[UI 전용·관리자] 사원 비밀번호 초기화(새 4자리, 다음 로그인 때 변경 필요).

    새 비밀번호는 이 응답에서 한 번만 돌려준다.
    """
    denied = _require_admin(company_id, actor_account_id)
    if denied:
        return denied
    password = storage.admin_reset_password(
        company_id, str(employee_id or ""), actor_account_id
    )
    if password is None:
        return fail("NOT_FOUND", MSG["ACCOUNT_NOT_FOUND"])
    return ok(
        "PASSWORD_RESET",
        data={"employee_id": employee_id, "initial_password": password},
        message=MSG["PASSWORD_RESET"],
    )


@mcp.tool
@_storage_safe
def ui_admin_unlock_account(
    company_id: str, actor_account_id: str, employee_id: str
) -> dict:
    """[UI 전용·관리자] 로그인 실패로 잠긴 사원 계정의 잠금을 푼다."""
    denied = _require_admin(company_id, actor_account_id)
    if denied:
        return denied
    if not storage.admin_unlock_account(
        company_id, str(employee_id or ""), actor_account_id
    ):
        return fail("NOT_FOUND", MSG["ACCOUNT_NOT_FOUND"])
    return ok("UNLOCKED", message=MSG["ACCOUNT_UNLOCKED"])


@mcp.tool
@_storage_safe
def ui_get_sidebar(company_id: str, employee_id: str) -> dict:
    """[UI 전용] 우측 사이드바: 잔여 연차 + 본인 신청 현황판(최신순)."""
    me, err = resolve_session(company_id, employee_id)
    if err:
        return err
    tickets = storage.get_tickets(company_id, employee_id)
    tickets.sort(key=lambda t: t.get("created_at", ""), reverse=True)
    leave = me.get("annual_leave", {})
    company = storage.get_company(company_id) or {}
    return ok(
        data={
            "company_name": company.get("company_name") or company_id,
            "employee": {
                "employee_id": me["employee_id"],
                "name": me["name"],
                "department": me["department"],
                "position": me.get("position"),
            },
            "annual_leave": {
                "remaining_days": leave.get("remaining_days"),
                "granted_days": leave.get("granted_days"),
                "used_days": leave.get("used_days"),
                "as_of": leave.get("as_of"),
            },
            "tickets": [
                {
                    k: t.get(k)
                    for k in (
                        "ticket_id",
                        "type",
                        "item",
                        "quantity",
                        "purpose",
                        "status",
                        "created_at",
                        "scheduled_date",
                    )
                }
                for t in tickets
            ],
            "announcements": storage.list_announcements(company_id, public_only=True),
        }
    )


@mcp.tool
@_storage_safe
def ui_confirm_ticket(
    company_id: str,
    employee_id: str,
    item: str,
    quantity: int = 1,
    purpose: str = "",
    request_type: str = "비품",
) -> dict:
    """[UI 전용] 사용자가 '접수 확정'을 눌렀을 때 티켓을 실제로 생성한다."""
    return create_support_ticket(
        item=item,
        company_id=company_id,
        employee_id=employee_id,
        quantity=quantity,
        purpose=purpose,
        request_type=request_type,
        confirmed=True,
    )


# ---------------------------------------------------------------------------
# 실행
# ---------------------------------------------------------------------------


RESET_MOVED_MESSAGE = (
    "티켓 초기화는 가져오기 스크립트로 옮겨졌습니다. 다음 명령을 사용하세요:\n"
    "  python scripts/import_company.py --all --reset-tickets"
)
EMPTY_DB_MESSAGE = (
    "DB에 회사 데이터가 없습니다. python scripts/import_company.py --all을 먼저 실행하세요."
)


def prepare_database() -> bool:
    """서버 시작 시 테이블을 만들고, 비어 있으면 가져오기 안내를 남긴다."""
    try:
        storage.init_db()
        if not storage.has_companies():
            log.warning(EMPTY_DB_MESSAGE)
            return False
    except storage.StorageUnavailable:
        log.warning("DB에 연결하지 못했습니다. 툴은 SERVER_UNAVAILABLE을 반환합니다.")
        return False
    return True


def configure_logging() -> None:
    # stdio MCP는 stdout을 프로토콜에 쓰므로 로그는 stderr로만 보낸다.
    level = os.environ.get("OFFICEGENIE_LOG_LEVEL", "INFO").upper()
    logging.basicConfig(
        level=getattr(logging, level, logging.INFO),
        stream=sys.stderr,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--reset-tickets", action="store_true", help=argparse.SUPPRESS
    )
    args = parser.parse_args()
    if args.reset_tickets:
        print(RESET_MOVED_MESSAGE, file=sys.stderr)
        raise SystemExit(2)
    configure_logging()
    prepare_database()
    mcp.run(show_banner=False)  # stdio
