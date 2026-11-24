# 오피스지니 UI 연동 규격 (app.py ↔ agent.py)

> 작성: 테크 리드 (mcp_server.py, agent.py 담당)
> 대상: UI 담당 (app.py)
> 원칙: **app.py는 `agent.py`만 import한다.** MCP 서버(`mcp_server.py`)는 agent가 자동으로 띄우므로 따로 실행하지 않는다.

---

## 1. 객체 두 개만 쓰면 된다

| 객체 | 만드는 곳 | 개수 | 역할 |
|---|---|---|---|
| `OfficeGenieAgent` | `@st.cache_resource` | 앱 전체 1개 | MCP 서버 연결, Jitda LLM API 클라이언트 |
| `ChatSession` | `st.session_state` | 사용자 세션마다 1개 | 대화 기록, 선택한 회사·사번, 확인 대기 티켓 |

```python
from agent import OfficeGenieAgent

@st.cache_resource
def get_agent():
    return OfficeGenieAgent()          # LLM_API_KEY / LLM_BASE_URL 환경변수 사용
```

---

## 2. 화면별 호출 함수

### 2-1. 상단 드롭다운 (회사 → 사번)

```python
agent.list_companies()
# → {"ok": True, "code": "OK", "data": {"companies": [
#      {"company_id": "bridgesoft", "company_name": "(주)브릿지소프트"},
#      {"company_id": "nextwave",   "company_name": "(주)넥스트웨이브"}]}}

agent.list_employees("bridgesoft")
# → {"ok": True, "data": {"employees": [
#      {"employee_id": "EMP001", "name": "김신입", "department": "백엔드개발팀",
#       "position": "사원", "label": "EMP001 김신입 / 백엔드개발팀"}, ...]}}
```

- 드롭다운 표시는 `label`, 내부 값은 `company_id` / `employee_id`를 쓴다.
- **회사나 사번이 바뀌면 새 세션을 만들고 채팅 기록을 비운다.**

```python
st.session_state.chat = agent.new_session(company_id, employee_id)
st.session_state.messages = []
```

### 2-2. 우측 사이드바 (잔여 연차 + 신청 현황판)

```python
agent.get_sidebar(company_id, employee_id)
# → {"ok": True, "data": {
#      "employee": {"employee_id": "EMP001", "name": "김신입", "department": "백엔드개발팀", "position": "사원"},
#      "annual_leave": {"remaining_days": 11, "granted_days": 15, "used_days": 4, "as_of": "2026-11-20"},
#      "tickets": [   # 본인 티켓만, 최신순
#        {"ticket_id": "REQ-003", "type": "비품", "item": "마우스", "quantity": 1,
#         "purpose": "개발 업무용", "status": "접수완료", "created_at": "2026-11-19T13:15:00+09:00"}, ...]}}
```

- 현황판 표 컬럼 제안: `티켓 번호 | 구분 | 품목 | 수량 | 상태 | 신청일`
- 상태 값은 5개 고정: `접수완료`, `검토중`, `승인`, `반려`, `지급완료`
- **다시 불러오는 시점**: 응답의 `tickets_changed`가 `True`일 때 (아래 3장)

### 2-3. 채팅 입력

```python
result = st.session_state.chat.send(user_text)
```

- 공백·500자 초과도 agent가 검사해서 에러 코드로 돌려준다. UI에서 먼저 막아도 되고(권장: `max_chars=500`), 안 막아도 된다.

### 2-4. 비품 신청 확인 버튼

신청 요청은 바로 접수되지 않고 **확인 대기** 상태가 된다 (데이터 규칙 `user_confirmation_required`).

```python
if result["pending_ticket"]:
    p = result["pending_ticket"]
    # 요약 카드: p["item"], p["quantity"], p["purpose"], p["warnings"]
    if st.button("접수 확정"):
        r = st.session_state.chat.confirm_ticket()   # → 티켓 생성, tickets_changed=True
    if st.button("취소"):
        r = st.session_state.chat.cancel_ticket()
```

- 사용자가 채팅창에 "네", "접수해줘", "취소"라고 답해도 같은 동작을 한다.
- 버튼을 연달아 눌러도 10초 안의 같은 신청은 티켓이 하나만 생긴다.

---

## 3. `send()` / `confirm_ticket()` 반환 형식 (ChatResult)

```python
{
  "ok": True,                 # False면 reply에 안내 문구가 들어 있음
  "code": "OK",               # 아래 표 참고
  "reply": "**핵심 요약**: ...",   # 채팅창에 그대로 표시 (마크다운)
  "tool_calls": [             # 디버그·발표용 (어떤 툴이 호출됐는지)
    {"name": "search_policy", "args": {"query": "점심 식대 한도"}, "code": "OK"},
    {"name": "get_employee_info", "args": {}, "code": "OK"}
  ],
  "pending_ticket": None,     # 확인 대기 중인 신청 요약 (2-4 참고)
  "tickets_changed": False,   # True면 사이드바를 다시 불러온다
  "ticket": None,             # confirm_ticket() 성공 시 생성된 티켓
  "debug": None               # 개발용 오류 원인 (FALLBACK 등). 화면에는 표시하지 않아도 됨
}
```

**UI 처리 규칙은 세 가지뿐이다.**
1. `reply`는 성공이든 실패든 항상 채팅창에 표시한다 (사용자용 문구가 이미 들어 있음).
2. `pending_ticket`이 있으면 확인 카드와 버튼을 띄운다.
3. `tickets_changed`가 `True`면 `get_sidebar()`를 다시 호출한다.

### 코드 표

| code | ok | 의미 | reply 문구 (agent가 넣어 줌) | UI 추가 처리 |
|---|---|---|---|---|
| `OK` | ✅ | 정상 답변 | LLM 답변 | - |
| `CREATED` | ✅ | 티켓 생성 | "신청이 접수되었습니다. 티켓 번호: REQ-004 ..." | 사이드바 갱신 |
| `DUPLICATE_IGNORED` | ✅ | 연타로 같은 신청 재요청 | "이미 접수된 신청입니다. 티켓 번호: ..." | - |
| `CANCELLED` | ✅ | 신청 취소 | "신청을 취소했습니다. ..." | 확인 카드 닫기 |
| `REJECTED` | ❌ | 등록 안 된 품목·수량 초과 | 데이터의 거절 문구 | - |
| `INPUT_EMPTY` | ❌ | 공백 입력 | "질문을 입력해 주세요." | 경고 표시 (`st.warning`) |
| `INPUT_TOO_LONG` | ❌ | 500자 초과 | "질문은 500자 이내로 입력해 주세요." | 경고 표시 |
| `SERVER_UNAVAILABLE` | ❌ | MCP 서버 연결 실패 | "사내 시스템 연결이 일시적으로 원활하지 않습니다. ..." | 에러 표시 (`st.error`) |
| `LLM_UNAVAILABLE` | ❌ | LLM 장애 + 폴백도 실패 | "AI 응답 서비스에 일시적으로 연결할 수 없습니다. ..." | 에러 표시 |
| `FALLBACK` | ❌ | LLM 장애 → 키워드 검색 결과로 대체 | "AI 응답이 지연되어 ... 바로 찾은 결과를 보여드립니다." | 안내 배지 (선택) |
| `INVALID_SESSION` | ❌ | 회사·사번 값이 잘못됨 | "선택한 회사 또는 사번 정보를 확인할 수 없습니다. ..." | 드롭다운 다시 선택 유도 |
| `NO_PENDING` | ❌ | 확인할 신청이 없는데 확정 클릭 | "확인을 기다리는 신청이 없습니다." | - |

> 타인 연차·타 부서 인수인계서·미등록 장소는 툴 단계에서 차단되고, LLM이 고정 문구를 `reply`에 넣어 `code="OK"`로 돌아온다.
> "사내 시스템에 등록되지 않았거나 열람 권한이 없는 정보입니다. 본인 정보 및 소속 부서 문서만 조회 가능하며, 추가 문의는 인사총무팀 공식 메일로 연락해 주세요."

**파이썬 예외(Traceback)는 UI로 올라오지 않는다.** agent 메서드는 항상 위 형식의 dict를 돌려준다.

---

## 4. 최소 예시 (app.py 뼈대)

```python
import streamlit as st
from agent import OfficeGenieAgent

agent = get_agent()   # 1장 참고

companies = agent.list_companies()["data"]["companies"]
company = st.selectbox("회사", companies, format_func=lambda c: c["company_name"])
emps = agent.list_employees(company["company_id"])["data"]["employees"]
emp = st.selectbox("사번", emps, format_func=lambda e: e["label"])

key = (company["company_id"], emp["employee_id"])
if st.session_state.get("key") != key:
    st.session_state.key = key
    st.session_state.chat = agent.new_session(*key)
    st.session_state.messages = []

with st.sidebar:
    side = agent.get_sidebar(*key)["data"]
    st.metric("잔여 연차", f"{side['annual_leave']['remaining_days']}일")
    st.dataframe(side["tickets"])

for m in st.session_state.messages:
    st.chat_message(m["role"]).markdown(m["content"])

if text := st.chat_input("궁금한 점을 물어보세요", max_chars=500):
    st.session_state.messages.append({"role": "user", "content": text})
    with st.spinner("확인 중..."):
        r = st.session_state.chat.send(text)
    st.session_state.messages.append({"role": "assistant", "content": r["reply"]})
    st.rerun()

chat = st.session_state.chat
if chat.pending_ticket:
    p = chat.pending_ticket
    st.info(f"신청 확인: {p['item']} {p['quantity']}개 / 목적: {p['purpose']}")
    for w in p["warnings"]:
        st.warning(w)
    c1, c2 = st.columns(2)
    if c1.button("접수 확정"):
        r = chat.confirm_ticket()
        st.session_state.messages.append({"role": "assistant", "content": r["reply"]})
        st.rerun()   # rerun 시 사이드바가 다시 그려져 새 티켓이 보임
    if c2.button("취소"):
        r = chat.cancel_ticket()
        st.session_state.messages.append({"role": "assistant", "content": r["reply"]})
        st.rerun()
```

> Streamlit은 버튼을 누를 때마다 스크립트 전체를 다시 실행하므로, 사이드바를 매 실행마다 `get_sidebar()`로 그리면 `tickets_changed`를 따로 처리하지 않아도 자동으로 갱신된다. (서버가 파일 변경을 감지해 다시 읽으므로 비용이 거의 없음)

---

## 5. 추천 질문 버튼 (추가 기능)

버튼을 누르면 `chat.send("<문구>")`를 부르면 된다. 시연 시나리오와 일치하는 문구:

1. `점심 식대 한도랑 내 남은 연차 며칠이야?`
2. `대회의실 위치랑 내 직무 인수인계서 요약해 줘`
3. `듀얼 모니터 1대 신청해 줘`

---

## 6. 실행 환경

```bash
pip install -r requirements.txt
export LLM_API_KEY=...             # 서버 환경에 안전하게 설정
export LLM_BASE_URL=...            # Jitda OpenAI-compatible 기본 주소
streamlit run app.py
```

기본 요청 모델은 `jitda-chat`이며 `/chat/completions` 호환 API를 사용한다. UI는 API 키를 브라우저로 보내지 않는다.

시연 후 티켓 데이터 초기화: `python mcp_server.py --reset-tickets` (REQ-004부터 다시 생성됨)
