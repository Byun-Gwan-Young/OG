"""
오피스지니 (OfficeGenie) MCP 서버
=================================

회사별 표준 JSON 5종(data/<company_id>/*.json)을 읽어 온보딩 툴을 제공하는 FastMCP 서버.
코드 수정 없이 data/ 아래에 회사 폴더를 추가하거나 교체하면 다른 회사에 그대로 재사용된다.

실행:  python mcp_server.py               (stdio, agent.py가 자동으로 띄움)
초기화: python mcp_server.py --reset-tickets   (seed/ 의 tickets.json으로 시연 데이터 복구)

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
import re
import logging
from functools import wraps
from datetime import datetime, timedelta, timezone
from typing import Any, Literal

from fastmcp import FastMCP

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
    "INVALID_SESSION": "선택한 회사 또는 사번 정보를 확인할 수 없습니다. 화면 상단에서 다시 선택해 주세요.",
    "INVALID_INPUT": "요청 내용을 이해하지 못했습니다. 조금 더 구체적으로 입력해 주세요.",
    "INVALID_QUANTITY": "신청 수량은 1 이상의 숫자로 입력해 주세요.",
    "NEEDS_CONFIRMATION": "아래 내용으로 신청을 접수할까요? 확인하시면 티켓이 발급됩니다.",
    "SERVER_UNAVAILABLE": "사내 시스템 연결이 일시적으로 원활하지 않습니다. 잠시 후 다시 시도해 주세요.",
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
    """부서명 별칭 → 정식 부서명. (locations.json의 부서 위치 별칭을 재사용)"""
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


@mcp.tool
@_storage_safe
def ui_list_companies() -> dict:
    """[UI 전용] 회사 선택 드롭다운용 목록."""
    return ok(data={"companies": storage.list_companies()})


@mcp.tool
@_storage_safe
def ui_list_employees(company_id: str) -> dict:
    """[UI 전용] 사번 선택 드롭다운용 목록."""
    try:
        employees = load(company_id, "employees")["employees"]
    except KeyError:
        return fail("INVALID_SESSION", MSG["INVALID_SESSION"])
    return ok(
        data={
            "employees": [
                {
                    "employee_id": e["employee_id"],
                    "name": e["name"],
                    "department": e["department"],
                    "position": e.get("position"),
                    "onboarding_week": e.get("onboarding_week"),
                    "label": f"{e['employee_id']} {e['name']} / {e['department']}",
                }
                for e in employees
            ]
        }
    )


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
    return ok(
        data={
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
                    )
                }
                for t in tickets
            ],
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


def reset_tickets() -> None:
    for cid in list_company_ids():
        seed = SEED_ROOT / cid / "tickets.json"
        if seed.is_file():
            shutil.copyfile(seed, DATA_ROOT / cid / "tickets.json")
            print(f"[reset] {cid}/tickets.json ← seed")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--reset-tickets", action="store_true", help="시연용 티켓 데이터를 seed로 복구"
    )
    args = parser.parse_args()
    if args.reset_tickets:
        reset_tickets()
    else:
        mcp.run(show_banner=False)  # stdio
