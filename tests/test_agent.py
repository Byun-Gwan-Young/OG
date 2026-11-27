"""에이전트 통합 테스트: 실제 stdio MCP 서버 + 가짜 LLM.

Gemini API 키 없이 '툴 호출 → 서버 응답 → 결과 전달' 흐름과 보안 주입, 확인 절차,
장애 처리를 검증한다. 실행: pytest -q tests/test_agent.py
"""

import pytest
from google.genai import types

from agent import ChatSession, OfficeGenieAgent, _YES, _NO



def fc_response(*calls):
    parts = [types.Part(function_call=types.FunctionCall(id=f"c{i}", name=n, args=a))
             for i, (n, a) in enumerate(calls)]
    return types.GenerateContentResponse(candidates=[types.Candidate(
        content=types.Content(role="model", parts=parts))])


def text_response(text):
    return types.GenerateContentResponse(candidates=[types.Candidate(
        content=types.Content(role="model", parts=[types.Part(text=text)]))])


class ScriptedLLM:
    """사용자 질문에 따라 정해진 툴을 부르고, 툴 결과를 그대로 요약해 돌려주는 가짜 LLM."""

    def __init__(self, plan):
        self.plan = plan  # {질문 키워드: [(tool, args), ...]}
        self.seen_decls = None
        self.last_tool_results = []

    async def __call__(self, contents, system, decls):
        self.seen_decls = decls
        last = contents[-1]
        if any(p.function_response for p in last.parts):
            self.last_tool_results = [p.function_response.response["result"] for p in last.parts]
            return text_response(" | ".join(str(r) for r in self.last_tool_results))
        user = last.parts[0].text
        for key, calls in self.plan.items():
            if key in user:
                return fc_response(*calls)
        return text_response("일반 답변")


@pytest.fixture
def make_agent(fresh_db):
    # MCP 하위 프로세스는 conftest가 지정한 임시 테스트 DB를 환경 변수로 물려받는다.
    agents = []

    def _make(llm):
        a = OfficeGenieAgent(llm=llm)
        agents.append(a)
        return a
    yield _make
    for a in agents:
        a.close()


def test_hidden_args_not_shown_to_llm_and_ui_tools_hidden(make_agent):
    llm = ScriptedLLM({"연차": [("get_employee_info", {})]})
    chat = make_agent(llm).new_session("bridgesoft", "EMP001")
    chat.send("내 연차")
    names = {d["name"] for d in llm.seen_decls}
    assert names == {"search_policy", "get_employee_info", "get_office_location",
                     "get_handover_doc", "create_support_ticket"}
    for d in llm.seen_decls:
        props = d["parameters_json_schema"].get("properties", {})
        assert not {"company_id", "employee_id", "confirmed"} & set(props)


def test_normal1_meal_and_leave(make_agent):
    llm = ScriptedLLM({"식대": [("search_policy", {"query": "점심 식대 한도"}), ("get_employee_info", {})]})
    r = make_agent(llm).new_session("bridgesoft", "EMP001").send("점심 식대 한도랑 내 남은 연차 며칠이야?")
    assert r["ok"] and [c["name"] for c in r["tool_calls"]] == ["search_policy", "get_employee_info"]
    assert "12,000원" in r["reply"] and "'remaining_days': 11" in r["reply"]


def test_llm_cannot_spoof_employee_id(make_agent):
    # LLM이 employee_id를 EMP002로 바꿔치기해도 세션 값(EMP001)으로 조회된다
    llm = ScriptedLLM({"연차": [("get_employee_info", {"employee_id": "EMP002", "company_id": "nextwave"})]})
    r = make_agent(llm).new_session("bridgesoft", "EMP001").send("내 연차")
    assert llm.last_tool_results[0]["data"]["name"] == "김신입"


def test_other_employee_denied(make_agent):
    llm = ScriptedLLM({"EMP002": [("get_employee_info", {"target": "EMP002"})]})
    make_agent(llm).new_session("bridgesoft", "EMP001").send("EMP002 연차 알려줘")
    assert llm.last_tool_results[0]["code"] == "PERMISSION_DENIED"


def test_ticket_requires_confirmation_then_created(make_agent):
    # LLM이 confirmed=True로 바로 접수하려 해도 미리보기로만 처리된다
    llm = ScriptedLLM({"거치대": [("create_support_ticket", {"item": "노트북 거치대", "quantity": 1, "confirmed": True})]})
    agent = make_agent(llm)
    chat = agent.new_session("bridgesoft", "EMP001")
    r = chat.send("노트북 거치대 1개 신청해 줘")
    assert r["pending_ticket"]["item"] == "노트북 거치대" and not r["tickets_changed"]
    assert len(agent.get_sidebar("bridgesoft", "EMP001")["data"]["tickets"]) == 3

    r = chat.confirm_ticket()
    assert r["code"] == "CREATED" and r["ticket"]["ticket_id"] == "REQ-004" and r["tickets_changed"]
    assert agent.get_sidebar("bridgesoft", "EMP001")["data"]["tickets"][0]["ticket_id"] == "REQ-004"


def test_yes_reply_confirms_and_no_reply_cancels(make_agent):
    llm = ScriptedLLM({"모니터": [("create_support_ticket", {"item": "듀얼 모니터", "quantity": 1})]})
    chat = make_agent(llm).new_session("bridgesoft", "EMP001")
    chat.send("듀얼 모니터 1대 신청해 줘")
    assert chat.send("네")["code"] == "CREATED"
    chat.send("듀얼 모니터 1대 신청해 줘")
    assert chat.send("취소")["code"] == "CANCELLED" and chat.pending_ticket is None


def test_confirmation_patterns():
    for t in ["네", "네!", "응", "접수해줘", "확정", "신청해 주세요", "좋아요"]:
        assert _YES.match(t), t
    for t in ["네트워크 규정", "어디야", "응급실 위치", "신청 방법 알려줘"]:
        assert not _YES.match(t), t
    for t in ["아니요", "취소", "취소할게요"]:
        assert _NO.match(t), t


def test_input_guardrails(make_agent):
    chat = make_agent(ScriptedLLM({})).new_session("bridgesoft", "EMP001")
    assert chat.send("   ")["code"] == "INPUT_EMPTY"
    assert chat.send("가" * 501)["code"] == "INPUT_TOO_LONG"


def test_llm_failure_falls_back_to_keywords(make_agent):
    async def broken_llm(**_):
        raise RuntimeError("quota exceeded")
    chat = make_agent(broken_llm).new_session("bridgesoft", "EMP001")
    r = chat.send("점심 식대 한도랑 내 남은 연차 며칠이야?")
    assert r["code"] == "FALLBACK" and "11일" in r["reply"] and "12,000원" in r["reply"]
    assert chat.history == []


def test_server_unavailable(tmp_path):
    agent = OfficeGenieAgent(server_path=tmp_path / "missing_server.py", llm=ScriptedLLM({}))
    try:
        r = agent.new_session("bridgesoft", "EMP001").send("안녕")
        assert r["code"] == "SERVER_UNAVAILABLE"
        assert "Traceback" not in r["reply"]
        assert agent.login("ABCDEF", "EMP001", "0000")["code"] == "SERVER_UNAVAILABLE"
    finally:
        agent.close()


def test_company_switch_same_server(make_agent):
    llm = ScriptedLLM({"식대": [("search_policy", {"query": "점심 식대 한도"})]})
    agent = make_agent(llm)
    assert "12,000원" in agent.new_session("bridgesoft", "EMP001").send("식대")["reply"]
    assert "15,000원" in agent.new_session("nextwave", "EMP001").send("식대")["reply"]


def test_llm_failure_reason_exposed_in_debug(make_agent):
    async def broken_llm(**_):
        raise RuntimeError("API key not valid")
    r = make_agent(broken_llm).new_session("bridgesoft", "EMP001").send("식대 한도")
    assert "API key not valid" in r["debug"]


def test_llm_hang_times_out(make_agent, monkeypatch):
    import asyncio, agent as agent_mod
    async def hanging_llm(**_):
        await asyncio.sleep(3600)
    monkeypatch.setattr(agent_mod, "LLM_TIMEOUT_SEC", 1)
    a = make_agent(hanging_llm)
    a._llm = lambda **k: asyncio.wait_for(hanging_llm(**k), timeout=agent_mod.LLM_TIMEOUT_SEC)
    r = a.new_session("bridgesoft", "EMP001").send("식대 한도")
    assert r["code"] in {"FALLBACK", "LLM_UNAVAILABLE"} and r["debug"]


@pytest.mark.reimport
def test_agent_restarts_mcp_subprocess_when_server_code_changes(make_agent):
    """화면(캐시된 agent)만 옛 서버 코드로 로그인을 검증하던 문제 방지."""
    import storage

    storage.reset_employee_passwords("bridgesoft", [{"employee_id": "EMP001", "password": "0899"}])
    code = storage.get_company("bridgesoft")["company_code"]
    agent = make_agent(ScriptedLLM({}))
    assert agent.login(code, "EMP001", "0899")["ok"]
    first = agent._client
    assert agent.login(code, "EMP001", "0899")["ok"]
    assert agent._client is first  # 코드가 그대로면 같은 프로세스를 쓴다

    real_stamp = agent._server_code_stamp
    agent._server_code_stamp = lambda: real_stamp() + (("changed", 1),)
    assert agent.login(code, "EMP001", "0899")["ok"]
    assert agent._client is not first
