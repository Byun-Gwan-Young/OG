"""
오피스지니 에이전트: Gemini + MCP(stdio) 연결 모듈
==================================================

app.py(Streamlit)는 이 파일의 두 클래스만 사용한다.

    agent = OfficeGenieAgent()                     # 앱 전체에서 1개 (st.cache_resource)
    chat  = agent.new_session("bridgesoft", "EMP001")  # 사용자 세션마다 1개 (st.session_state)
    result = chat.send("점심 식대 한도랑 내 남은 연차 며칠이야?")

반환 형식과 에러 코드는 docs/ui_contract.md 참고.

[구조]
- OfficeGenieAgent: mcp_server.py를 stdio 하위 프로세스로 띄우고 연결을 유지한다.
  Streamlit은 동기 코드라서, 내부에 asyncio 루프 스레드를 하나 두고 동기 메서드로 감싼다.
- ChatSession: 대화 기록, 선택된 회사·사번, 확인 대기 중인 티켓을 가진다.

[보안]
- LLM에게는 LLM_TOOLS 5개만, company_id·employee_id·confirmed 인자를 뺀 스키마로 보여준다.
- 툴 호출 시 세션 값을 서버로 주입하고, 티켓은 항상 confirmed=False(미리보기)로만 부른다.
  실제 접수는 사용자가 확정 버튼을 누르거나 "네"라고 답했을 때 confirm_ticket()으로만 진행한다.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import copy
import logging
import os
import re
import sys
import threading
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from fastmcp import Client
from fastmcp.client.transports import PythonStdioTransport

BASE_DIR = Path(__file__).resolve().parent
DEFAULT_MODEL = os.environ.get("OFFICEGENIE_MODEL", "gemini-3.6-flash")
THINKING_LEVEL = os.environ.get(
    "OFFICEGENIE_THINKING_LEVEL", "low"
)  # low | medium | high
MAX_INPUT_CHARS = 500
MAX_TOOL_ROUNDS = 6
KST = timezone(timedelta(hours=9))
LLM_TIMEOUT_SEC = int(os.environ.get("OFFICEGENIE_LLM_TIMEOUT", "30"))
log = logging.getLogger("officegenie")

LLM_TOOLS = {
    "search_policy",
    "get_employee_info",
    "get_office_location",
    "get_handover_doc",
    "create_support_ticket",
}
HIDDEN_ARGS = {"company_id", "employee_id", "confirmed"}

UI_MSG = {
    "INPUT_EMPTY": "질문을 입력해 주세요.",
    "INPUT_TOO_LONG": f"질문은 {MAX_INPUT_CHARS}자 이내로 입력해 주세요.",
    "SERVER_UNAVAILABLE": "사내 시스템 연결이 일시적으로 원활하지 않습니다. 잠시 후 다시 시도해 주세요.",
    "LLM_UNAVAILABLE": "AI 응답 서비스에 일시적으로 연결할 수 없습니다. 잠시 후 다시 시도해 주세요.",
    "NO_PENDING": "확인을 기다리는 신청이 없습니다.",
    "CANCELLED": "신청을 취소했습니다. 티켓은 발급되지 않았습니다.",
}

# 짧은 확인/취소 답만 인식한다 ("네트워크 규정"처럼 '네'로 시작하는 질문은 해당 없음)
_TAIL = r"\s*(요|주세요|줘|해 ?줘|할게요?|해 ?주세요)?\s*[.!~]*\s*$"
_YES = re.compile(r"^\s*(네|넵|예|응|ㅇㅇ|좋아|그래|맞아|확정|접수|진행|신청)" + _TAIL)
_NO = re.compile(r"^\s*(아니|아니요|아뇨|취소|그만|안 ?할래|됐어)" + _TAIL)


def _system_prompt(company: str, me: dict) -> str:
    today = datetime.now(KST).strftime("%Y-%m-%d")
    return f"""너는 {company}의 사내 온보딩 도우미 '오피스지니'다. 오늘은 {today}.
현재 사용자: {me.get("name")} ({me.get("department")}, {me.get("position")}).

[원칙]
1. 회사 정보는 반드시 툴 결과로만 답한다. 툴 결과에 없는 내용은 추측하거나 지어내지 않는다.
2. 툴 결과의 ok가 false이면 message 문구를 그대로 사용자에게 전달하고, 다른 정보를 덧붙여 추측하지 않는다.
3. 다른 직원의 정보나 다른 부서 문서를 요청받으면 직접 거절하지 말고, 해당 툴에 target 또는 department를 넣어 호출해 서버 판단을 따른다.
4. 한 질문에 여러 주제가 있으면 필요한 툴을 모두 호출한 뒤 한 번에 답한다.

[툴 선택]
- 회사 규칙·기준·한도·절차 → search_policy (query는 주제만 짧게)
- 본인 잔여 연차·부서·직급 → get_employee_info
- 장소 위치 → get_office_location (place는 장소 이름만)
- 인수인계서·1주 차 할 일 → get_handover_doc
- 비품·계정 신청 → create_support_ticket. 결과가 NEEDS_CONFIRMATION이면 신청 요약(품목, 수량, 목적, 경고)을 보여주고
  "접수할까요? 화면의 [접수 확정] 버튼을 누르거나 '네'라고 답해 주세요."라고 안내한다. 접수 완료라고 말하지 않는다.
  REJECTED이면 message를 전달하고, 허용 품목(allowed_items)이 있으면 함께 안내한다.

[답변 형식]
- 규정 답변: 아래 세 단락을 이 순서로 쓴다.
  **핵심 요약**: 한두 문장
  **상세 기준**: 조건·예외를 짧은 목록으로
  **근거 조항**: 툴 결과의 citation (여러 개면 모두)
  is_reference_only가 true인 조항은 "회사 자체 규정이 아닌 표준취업규칙 참고 조문"이라고 밝힌다.
- 본인 정보(연차 등)는 규정 답변과 별도로 한두 줄로 덧붙인다. 연차는 "잔여 N일 (부여 N일, 사용 N일, 기준일)" 형식.
- 인수인계서: **핵심 담당 업무** / **사용 툴** / **1주 차 할 일** 순서로 요약하고, notes가 있으면 '주의사항'으로 덧붙인다.
- 장소: "건물 N층 구역" + 안내 문구. 출입 제한(access)이 있으면 함께 알린다.
- 한국어 존댓말, 간결하게. 사번·내부 코드(company_id 등)는 사용자에게 언급하지 않는다.
"""


def _strip_schema(schema: dict) -> dict:
    """LLM에게 보여줄 스키마에서 세션 인자를 제거한다."""
    s = copy.deepcopy(schema or {"type": "object", "properties": {}})
    props = s.get("properties", {})
    for key in HIDDEN_ARGS:
        props.pop(key, None)
    if "required" in s:
        s["required"] = [r for r in s["required"] if r not in HIDDEN_ARGS]
        if not s["required"]:
            s.pop("required")
    s.pop("additionalProperties", None)
    return s


@dataclass
class ChatResult:
    ok: bool
    code: str
    reply: str
    tool_calls: list[dict] = field(default_factory=list)
    pending_ticket: dict | None = None
    tickets_changed: bool = False
    ticket: dict | None = None
    debug: str | None = None  # 개발용: LLM 오류 원인 (화면에는 표시하지 않아도 됨)

    def to_dict(self) -> dict:
        return self.__dict__.copy()


class ServerUnavailable(Exception):
    pass


# ---------------------------------------------------------------------------
# OfficeGenieAgent: MCP 연결 + LLM 클라이언트 (앱 전체 공유)
# ---------------------------------------------------------------------------


class OfficeGenieAgent:
    def __init__(
        self,
        server_path: str | Path = BASE_DIR / "mcp_server.py",
        model: str = DEFAULT_MODEL,
        api_key: str | None = None,
        data_root: str | Path | None = None,
        llm: Any = None,
    ):
        self.server_path = Path(server_path)
        self.model = model
        self._env = dict(os.environ)
        if data_root:
            self._env["OFFICEGENIE_DATA_ROOT"] = str(data_root)
        self._api_key = (
            api_key
            or os.environ.get("GEMINI_API_KEY")
            or os.environ.get("GOOGLE_API_KEY")
        )
        self._llm = llm  # 테스트용 주입. None이면 Gemini 사용
        self._client: Client | None = None
        self._tool_decls: list[dict] | None = None
        self._lock = None  # asyncio.Lock (루프 스레드에서 생성)
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(
            target=self._loop.run_forever, daemon=True, name="officegenie-loop"
        )
        self._thread.start()

    # ---- 동기 래퍼 ---------------------------------------------------------
    def _run(self, coro, timeout: float = LLM_TIMEOUT_SEC * MAX_TOOL_ROUNDS + 30):
        return asyncio.run_coroutine_threadsafe(coro, self._loop).result(timeout)

    def close(self) -> None:
        async def _close():
            if self._client is not None:
                try:
                    await self._client.__aexit__(None, None, None)
                except Exception:
                    pass
                self._client = None

        try:
            self._run(_close(), timeout=10)
        finally:
            self._loop.call_soon_threadsafe(self._loop.stop)

    # ---- MCP 연결 ----------------------------------------------------------
    async def _connect(self) -> Client:
        if self._lock is None:
            self._lock = asyncio.Lock()
        async with self._lock:
            if self._client is not None and self._client.is_connected():
                return self._client
            transport = PythonStdioTransport(
                script_path=str(self.server_path),
                env=self._env,
                cwd=str(self.server_path.parent),
                python_cmd=sys.executable,
            )
            client = Client(transport, timeout=30)
            try:
                await client.__aenter__()
            except Exception as e:
                raise ServerUnavailable(str(e)) from e
            self._client = client
            return client

    async def _call(self, name: str, args: dict, _retry: bool = True) -> dict:
        try:
            client = await self._connect()
            result = await client.call_tool(name, args, raise_on_error=False)
        except ServerUnavailable:
            raise
        except Exception as e:
            self._client = None  # 다음 호출에서 재연결
            if _retry:
                return await self._call(name, args, _retry=False)
            raise ServerUnavailable(str(e)) from e
        data = result.structured_content
        if isinstance(data, dict) and "result" in data and "ok" not in data:
            data = data["result"]
        if not isinstance(data, dict):
            data = {
                "ok": False,
                "code": "SERVER_ERROR",
                "message": UI_MSG["SERVER_UNAVAILABLE"],
                "data": None,
            }
        return data

    async def _llm_tool_decls(self) -> list[dict]:
        if self._tool_decls is None:
            client = await self._connect()
            tools = await client.list_tools()
            self._tool_decls = [
                {
                    "name": t.name,
                    "description": t.description or "",
                    "parameters_json_schema": _strip_schema(
                        getattr(t, "input_schema", None)
                        or getattr(t, "inputSchema", None)
                    ),
                }
                for t in tools
                if t.name in LLM_TOOLS
            ]
        return self._tool_decls

    def call_tool(self, name: str, args: dict | None = None) -> dict:
        """UI에서 서버 툴을 직접 부를 때 사용. 연결 실패 시 SERVER_UNAVAILABLE 응답."""
        try:
            return self._run(self._call(name, args or {}))
        except Exception:
            return {
                "ok": False,
                "code": "SERVER_UNAVAILABLE",
                "message": UI_MSG["SERVER_UNAVAILABLE"],
                "data": None,
            }

    # ---- UI 편의 메서드 ------------------------------------------------------
    def list_companies(self) -> dict:
        return self.call_tool("ui_list_companies")

    def list_employees(self, company_id: str) -> dict:
        return self.call_tool("ui_list_employees", {"company_id": company_id})

    def get_sidebar(self, company_id: str, employee_id: str) -> dict:
        return self.call_tool(
            "ui_get_sidebar", {"company_id": company_id, "employee_id": employee_id}
        )

    def new_session(self, company_id: str, employee_id: str) -> "ChatSession":
        return ChatSession(self, company_id, employee_id)

    # ---- LLM 호출 ----------------------------------------------------------
    async def _generate(self, contents: list, system: str, decls: list[dict]):
        if self._llm is not None:
            return await self._llm(contents=contents, system=system, decls=decls)
        from google import genai
        from google.genai import types

        if not self._api_key:
            raise RuntimeError("GEMINI_API_KEY가 설정되지 않았습니다.")
        if not hasattr(self, "_genai"):
            self._genai = genai.Client(
                api_key=self._api_key,
                http_options=types.HttpOptions(timeout=LLM_TIMEOUT_SEC * 1000),
            )
        config = types.GenerateContentConfig(
            system_instruction=system,
            tools=[
                types.Tool(
                    function_declarations=[
                        types.FunctionDeclaration(**d) for d in decls
                    ]
                )
            ],
            automatic_function_calling=types.AutomaticFunctionCallingConfig(
                disable=True
            ),
            thinking_config=types.ThinkingConfig(thinking_level=THINKING_LEVEL),
        )
        return await asyncio.wait_for(
            self._genai.aio.models.generate_content(
                model=self.model, contents=contents, config=config
            ),
            timeout=LLM_TIMEOUT_SEC + 5,
        )


# ---------------------------------------------------------------------------
# ChatSession: 사용자 세션 1개
# ---------------------------------------------------------------------------


class ChatSession:
    def __init__(self, agent: OfficeGenieAgent, company_id: str, employee_id: str):
        self.agent = agent
        self.company_id = company_id
        self.employee_id = employee_id
        self.history: list = []
        self.pending_ticket: dict | None = None
        self._notes: list[str] = []

    # ---- 공개 메서드 (동기) --------------------------------------------------
    def send(self, text: str) -> dict:
        """사용자 메시지 1개를 처리하고 ChatResult(dict)를 돌려준다."""
        text = (text or "").strip()
        if not text:
            return ChatResult(False, "INPUT_EMPTY", UI_MSG["INPUT_EMPTY"]).to_dict()
        if len(text) > MAX_INPUT_CHARS:
            return ChatResult(
                False, "INPUT_TOO_LONG", UI_MSG["INPUT_TOO_LONG"]
            ).to_dict()
        if self.pending_ticket and _YES.match(text):
            return self.confirm_ticket()
        if self.pending_ticket and _NO.match(text):
            return self.cancel_ticket()
        try:
            return self.agent._run(self._send(text)).to_dict()
        except ServerUnavailable:
            return ChatResult(
                False, "SERVER_UNAVAILABLE", UI_MSG["SERVER_UNAVAILABLE"]
            ).to_dict()
        except (concurrent.futures.TimeoutError, TimeoutError):
            return ChatResult(
                False,
                "LLM_UNAVAILABLE",
                UI_MSG["LLM_UNAVAILABLE"],
                debug="전체 응답 시간 초과",
            ).to_dict()

    def confirm_ticket(self) -> dict:
        """[접수 확정] 버튼 또는 '네' 응답 → 티켓 실제 생성."""
        if not self.pending_ticket:
            return ChatResult(False, "NO_PENDING", UI_MSG["NO_PENDING"]).to_dict()
        p = self.pending_ticket
        r = self.agent.call_tool(
            "ui_confirm_ticket",
            {
                "company_id": self.company_id,
                "employee_id": self.employee_id,
                "item": p["item"],
                "quantity": p["quantity"],
                "purpose": ""
                if "미기재" in p.get("purpose", "")
                else p.get("purpose", ""),
                "request_type": p.get("type", "비품"),
            },
        )
        if r.get("code") == "SERVER_UNAVAILABLE":
            return ChatResult(
                False, "SERVER_UNAVAILABLE", r["message"], pending_ticket=p
            ).to_dict()
        self.pending_ticket = None
        ticket = (r.get("data") or {}).get("ticket")
        if r.get("ok"):
            self._notes.append(
                f"(사용자가 신청을 확정했고 티켓 {ticket['ticket_id']}이(가) 발급되었다.)"
            )
        return ChatResult(
            r.get("ok", False),
            r.get("code", "SERVER_ERROR"),
            r.get("message", ""),
            tickets_changed=r.get("code") == "CREATED",
            ticket=ticket,
        ).to_dict()

    def cancel_ticket(self) -> dict:
        self.pending_ticket = None
        self._notes.append("(사용자가 직전 신청을 취소했다. 티켓은 발급되지 않았다.)")
        return ChatResult(True, "CANCELLED", UI_MSG["CANCELLED"]).to_dict()

    def reset(self) -> None:
        self.history.clear()
        self.pending_ticket = None
        self._notes.clear()

    # ---- 내부 ---------------------------------------------------------------
    async def _send(self, text: str) -> ChatResult:
        from google.genai import types

        side = await self.agent._call(
            "ui_get_sidebar",
            {"company_id": self.company_id, "employee_id": self.employee_id},
        )
        if not side.get("ok"):
            return ChatResult(
                False, side.get("code", "INVALID_SESSION"), side.get("message", "")
            )
        me = side["data"]["employee"]
        companies = await self.agent._call("ui_list_companies", {})
        company = next(
            (
                c["company_name"]
                for c in (companies.get("data") or {}).get("companies", [])
                if c["company_id"] == self.company_id
            ),
            self.company_id,
        )
        system = _system_prompt(company, me)
        decls = await self.agent._llm_tool_decls()

        user_text = "\n".join([*self._notes, text]) if self._notes else text
        self._notes.clear()
        checkpoint = len(self.history)
        self.history.append(
            types.Content(role="user", parts=[types.Part(text=user_text)])
        )

        result = ChatResult(True, "OK", "")
        try:
            for _ in range(MAX_TOOL_ROUNDS):
                resp = await self.agent._generate(self.history, system, decls)
                content = resp.candidates[0].content
                self.history.append(
                    content
                )  # thought signature 보존을 위해 원본 그대로 저장
                calls = resp.function_calls or []
                if not calls:
                    result.reply = (resp.text or "").strip()
                    break
                parts = []
                for fc in calls:
                    tool_result = await self._run_tool(
                        fc.name, dict(fc.args or {}), result
                    )
                    parts.append(
                        types.Part(
                            function_response=types.FunctionResponse(
                                id=fc.id, name=fc.name, response={"result": tool_result}
                            )
                        )
                    )
                self.history.append(types.Content(role="user", parts=parts))
            else:
                result.reply = "요청을 처리하는 중 단계가 너무 많아졌습니다. 질문을 나누어 다시 입력해 주세요."
        except ServerUnavailable:
            del self.history[checkpoint:]
            raise
        except Exception as e:
            # LLM 장애: 대화 기록을 되돌리고 키워드 폴백으로 응답
            del self.history[checkpoint:]
            reason = f"{type(e).__name__}: {e}"[:500]
            log.warning("Gemini 호출 실패 → 폴백: %s", reason)
            fb = await self._fallback(text)
            fb.debug = reason
            return fb

        result.pending_ticket = self.pending_ticket
        return result

    async def _run_tool(self, name: str, args: dict, result: ChatResult) -> dict:
        if name not in LLM_TOOLS:
            r = {
                "ok": False,
                "code": "UNKNOWN_TOOL",
                "message": "사용할 수 없는 기능입니다.",
                "data": None,
            }
        else:
            for key in HIDDEN_ARGS:
                args.pop(key, None)  # LLM이 임의로 넣은 세션 값은 무시
            call_args = {
                **args,
                "company_id": self.company_id,
                "employee_id": self.employee_id,
            }
            if name == "create_support_ticket":
                call_args["confirmed"] = False
            r = await self.agent._call(name, call_args)
        result.tool_calls.append({"name": name, "args": args, "code": r.get("code")})
        if r.get("code") == "NEEDS_CONFIRMATION":
            self.pending_ticket = r["data"]["preview"]
        return r

    async def _fallback(self, text: str) -> ChatResult:
        """LLM이 응답하지 않을 때 최소한의 키워드 응답 (시연 백업용)."""
        res = ChatResult(False, "LLM_UNAVAILABLE", UI_MSG["LLM_UNAVAILABLE"])
        lines = []
        if "연차" in text and any(k in text for k in ("내", "남은", "잔여")):
            r = await self.agent._call(
                "get_employee_info",
                {"company_id": self.company_id, "employee_id": self.employee_id},
            )
            res.tool_calls.append(
                {"name": "get_employee_info", "args": {}, "code": r.get("code")}
            )
            if r.get("ok"):
                al = r["data"]["annual_leave"]
                lines.append(
                    f"- 잔여 연차: {al['remaining_days']}일 (부여 {al['granted_days']}일, "
                    f"사용 {al['used_days']}일, {al['as_of']} 기준)"
                )
        r = await self.agent._call(
            "search_policy",
            {
                "query": text,
                "company_id": self.company_id,
                "employee_id": self.employee_id,
            },
        )
        res.tool_calls.append(
            {"name": "search_policy", "args": {"query": text}, "code": r.get("code")}
        )
        if r.get("ok"):
            top = r["data"]["results"][0]
            lines.append(f"- 관련 규정: {top['citation']}\n  {top['content']}")
        if lines:
            res.code = "FALLBACK"
            res.reply = (
                "AI 응답이 지연되어 사내 데이터에서 바로 찾은 결과를 보여드립니다.\n"
                + "\n".join(lines)
            )
        return res
