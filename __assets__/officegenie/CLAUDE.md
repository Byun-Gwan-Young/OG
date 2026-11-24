# 오피스지니 (OfficeGenie) — Claude Code 작업 기준

중소기업 신규 입사자용 온보딩 에이전트. Gemini(gemini-3.8-flash) + FastMCP(stdio) + Streamlit + 회사별 JSON 5종.

## 파일과 담당
- `mcp_server.py` — MCP 툴 (테크 리드). LLM용 5개 + `ui_` 접두사 UI 전용 4개
- `agent.py` — Gemini ↔ MCP 연결, 세션 값 주입 (테크 리드)
- `app.py` — Streamlit UI (UI 담당). `agent.py`만 import한다
- `data/<company_id>/` — policies, employees, locations, handovers, tickets (데이터·QA 담당)
- `seed/` — 시연 초기화용 tickets.json 원본
- `docs/ui_contract.md` — app.py ↔ agent.py 규격. **반환 형식을 바꾸면 이 문서도 같이 고친다**

## 반드시 지킬 규칙
1. 회사별 내용(회사명, 금액, 품목 등)을 코드에 쓰지 않는다. 전부 data/의 JSON에서 읽는다.
2. `company_id`, `employee_id`, `confirmed`는 LLM에게 보이지 않는다. agent.py가 세션 값으로 주입한다.
3. 권한 검증(타인 정보, 타 부서 문서)과 비품 검증(화이트리스트, 수량 한도)은 mcp_server.py에서 한다. 프롬프트에만 의존하지 않는다.
4. 툴 반환 형식: `{"ok": bool, "code": str, "message": str, "data": ...}`. 사용자에게 보일 문구는 `message`에 넣는다.
5. 차단·미등록 시 고정 문구는 `MSG["DENIED_OR_NOT_FOUND"]` 하나만 쓴다.
6. 티켓은 사용자 확인(confirm_ticket) 후에만 생성한다. 거절 시 tickets.json을 수정하지 않는다.
7. 파이썬 예외를 UI로 올리지 않는다. agent 메서드는 항상 ChatResult dict를 돌려준다.

## 확인 기준 (PRD)
- 정상 1: bridgesoft/EMP001 "점심 식대 한도랑 내 남은 연차 며칠이야?" → 12,000원(복리후생 규정 제4조), 잔여 11일
- 정상 2: "대회의실 위치, 내 인수인계서 요약, 노트북 거치대 1개 신청" → 본관 3층 A구역, 백엔드개발팀 요약, 확정 후 REQ-004
- 예외 1: "사내 수면실 위치", "재무회계팀 인수인계서", "EMP002 연차" → 고정 차단 문구
- 예외 2: 공백 입력 / MCP 서버 중단 → 경고 또는 "사내 시스템 연결이 일시적으로 원활하지 않습니다"
- 다기업: nextwave로 바꾸면 식대 15,000원, 모니터 한도 1대

## 명령
- 테스트: `pytest -q` (LLM 없이 실행됨, 원본 data/를 건드리지 않음)
- 터미널 채팅: `python scripts/cli_chat.py bridgesoft EMP001` (GEMINI_API_KEY 필요)
- 티켓 초기화: `python mcp_server.py --reset-tickets`

## Gemini 3.8 Flash 주의
- temperature, top_p, top_k를 넣지 않는다. 추론 강도는 `thinking_level`(low/medium/high)로 조절한다.
- 모델 응답 Content는 그대로 history에 넣는다 (thought signature 보존). FunctionResponse에는 id와 name을 모두 넣는다.
