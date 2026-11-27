"""MCP 툴 단위 테스트 (LLM 없이 툴 함수 직접 호출).

실행: pytest -q tests/test_tools.py
원본 data/는 건드리지 않고, 테스트마다 임시 DB(tests/conftest.py의 fresh_db)에
data/를 가져온 뒤 티켓을 seed 상태로 초기화해서 테스트한다.
"""
import pytest

import mcp_server as s
import storage

DENY = s.MSG["DENIED_OR_NOT_FOUND"]
pytestmark = pytest.mark.usefixtures("fresh_db")


def tickets_doc(company_id):
    return storage.get_company_document(company_id, "tickets")


A, B = "bridgesoft", "nextwave"


# ---- 정상 상황 1 (MVP): 식대 한도 + 잔여 연차 -------------------------------

def test_normal1_meal_limit():
    r = s.search_policy("점심 식대 한도", A, "EMP001")
    top = r["data"]["results"][0]
    assert r["ok"] and top["policy_id"] == "BS-103"
    assert "12,000원" in top["content"]
    assert top["citation"].startswith("복리후생 규정 제4조")


def test_normal1_remaining_leave():
    r = s.get_employee_info(A, "EMP001")
    assert r["ok"] and r["data"]["name"] == "김신입"
    assert r["data"]["annual_leave"]["remaining_days"] == 11


def test_policy_whole_sentence_still_finds_meal_rule():
    r = s.search_policy("점심 식대 한도랑 내 남은 연차 며칠이야?", A, "EMP001")
    assert "BS-103" in [x["policy_id"] for x in r["data"]["results"]]


@pytest.mark.parametrize("q,expected", [
    ("야근 식대", "BS-102"),
    ("재택근무 신청", "BS-115"),
    ("비품 신청 가능 품목", "BS-105"),
    ("회의실 예약 취소", "BS-118"),
    ("슬랙 계정 신청", "BS-112"),
])
def test_policy_search_topics(q, expected):
    r = s.search_policy(q, A, "EMP001")
    assert r["data"]["results"][0]["policy_id"] == expected


def test_policy_not_found():
    r = s.search_policy("우주여행 지원금", A, "EMP001")
    assert r["code"] == "NOT_FOUND" and r["message"] == DENY


# ---- 정상 상황 2: 장소·인수인계·비품 신청 -----------------------------------

def test_location_big_meeting_room():
    r = s.get_office_location("대회의실", A, "EMP001")
    assert r["data"]["locations"][0]["display"] == "본관 3층 A구역"


def test_location_alias():
    r = s.get_office_location("프린터", A, "EMP001")
    assert r["data"]["locations"][0]["name"] == "복합기"


def test_handover_own_department():
    r = s.get_handover_doc(A, "EMP001")
    assert r["ok"] and r["data"]["department"] == "백엔드개발팀"
    assert set(r["data"]["summary"]) == {"핵심 담당 업무", "사용 툴", "1주 차 할 일"}


def test_handover_own_department_by_alias():
    assert s.get_handover_doc(A, "EMP001", department="백엔드팀")["ok"]


def test_ticket_preview_does_not_write():
    before = tickets_doc(A)["next_sequence"]
    r = s.create_support_ticket("노트북 거치대", A, "EMP001")
    assert r["code"] == "NEEDS_CONFIRMATION"
    assert r["data"]["preview"]["item"] == "노트북 거치대"
    assert tickets_doc(A)["next_sequence"] == before


def test_ticket_created_req004_and_sidebar_updates():
    r = s.ui_confirm_ticket(A, "EMP001", "노트북 거치대", 1)
    assert r["code"] == "CREATED" and r["data"]["ticket"]["ticket_id"] == "REQ-004"
    assert r["data"]["ticket"]["status"] == "접수완료"
    side = s.ui_get_sidebar(A, "EMP001")
    assert side["data"]["tickets"][0]["ticket_id"] == "REQ-004"
    assert tickets_doc(A)["next_sequence"] == 5


def test_dual_monitor_alias_creates_monitor_ticket():
    r = s.ui_confirm_ticket(A, "EMP001", "듀얼 모니터", 1)
    assert r["code"] == "CREATED" and r["data"]["ticket"]["item"] == "모니터"


def test_rapid_duplicate_submission_ignored():
    s.ui_confirm_ticket(A, "EMP001", "노트북 거치대", 1)
    r = s.ui_confirm_ticket(A, "EMP001", "노트북 거치대", 1)
    assert r["code"] in {"DUPLICATE_IGNORED", "REJECTED"}
    assert len(tickets_doc(A)["tickets"]) == 4


# ---- 예외 상황 1: 미등록 정보 / 권한 초과 ----------------------------------

def test_unregistered_location_sleeping_room():
    r = s.get_office_location("사내 수면실", A, "EMP001")
    assert r["code"] == "NOT_FOUND" and r["message"] == DENY


def test_other_department_handover_denied():
    r = s.get_handover_doc(A, "EMP001", department="재무회계팀")
    assert r["code"] == "PERMISSION_DENIED" and r["message"] == DENY


def test_other_employee_leave_denied():
    for target in ("EMP002", "박대리"):
        r = s.get_employee_info(A, "EMP001", target=target)
        assert r["code"] == "PERMISSION_DENIED" and r["message"] == DENY


def test_self_target_allowed():
    assert s.get_employee_info(A, "EMP001", target="EMP001")["ok"]


def test_invalid_session():
    assert s.get_employee_info(A, "EMP999")["code"] == "INVALID_SESSION"
    assert s.get_employee_info("nocompany", "EMP001")["code"] == "INVALID_SESSION"


# ---- 비품 규칙: 화이트리스트 / 수량 한도 -----------------------------------

@pytest.mark.parametrize("item", ["업무용 태블릿", "게임기"])
def test_unknown_equipment_rejected_without_mutation(item):
    before = tickets_doc(A)
    r = s.ui_confirm_ticket(A, "EMP001", item, 1)
    after = tickets_doc(A)
    assert r["code"] == "REJECTED" and r["data"]["reason"] == "UNKNOWN_EQUIPMENT"
    assert after["next_sequence"] == before["next_sequence"]
    assert len(after["tickets"]) == len(before["tickets"])


def test_excess_quantity_rejected():
    # EMP001은 마우스 1개가 이미 접수완료 상태 → 한도 1 초과
    r = s.create_support_ticket("무선 마우스", A, "EMP001")
    assert r["code"] == "REJECTED" and r["data"]["reason"] == "EXCESS_QUANTITY"
    # 모니터 한도 2 → 3대 요청은 거절
    assert s.create_support_ticket("모니터", A, "EMP001", quantity=3)["code"] == "REJECTED"


def test_invalid_quantity():
    assert s.create_support_ticket("모니터", A, "EMP001", quantity=0)["code"] == "INVALID_INPUT"


def test_account_ticket():
    r = s.ui_confirm_ticket(A, "EMP001", "Slack", 1, request_type="계정")
    assert r["code"] == "CREATED" and r["data"]["ticket"]["type"] == "계정"


# ---- 다기업 전환: 같은 코드, 다른 데이터 ------------------------------------

def test_company_b_uses_its_own_rules():
    r = s.search_policy("점심 식대 한도", B, "EMP001")
    assert r["data"]["results"][0]["policy_id"] == "NW-103"
    assert "15,000원" in r["data"]["results"][0]["content"]
    assert s.get_employee_info(B, "EMP001")["data"]["name"] == "이하준"
    # B사는 모니터 한도 1대
    assert s.create_support_ticket("모니터", B, "EMP001", quantity=2)["code"] == "REJECTED"
    # B사 EMP001은 무선 키보드 검토중 → 키보드 추가는 거절
    assert s.create_support_ticket("키보드", B, "EMP001")["code"] == "REJECTED"
