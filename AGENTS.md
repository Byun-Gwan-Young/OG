# 오피스지니 (OfficeGenie) — Claude Code 작업 기준

중소기업 신규 입사자용 온보딩 에이전트. Jitda LLM API(jitda-chat, OpenAI-compatible) + FastMCP(stdio) + Streamlit + 회사별 JSON 5종.

## 파일과 담당
- `mcp_server.py` — MCP 툴 (테크 리드). LLM용 6개(`get_my_tickets` 포함) + `ui_` 접두사 UI 전용(로그인·세션·등록·홈·공지·자료실·비품·티켓 확정·`ui_admin_*`)
- `agent.py` — Jitda LLM API ↔ MCP 연결, 세션 값 주입 (테크 리드)
- `app.py` — Streamlit UI (UI 담당). `agent.py`만 import한다. 메뉴형 화면, 현재 메뉴는 `?page=`와 동기화(신원·권한은 세션과 서버가 정함)
- `storage.py` — DB 저장소 (SQLAlchemy Core, PostgreSQL(Neon)/SQLite). 서버의 모든 데이터 접근은 여기로만. 회사 코드·계정(`accounts`)·감사 로그(`audit_log`)도 여기
- `company_data.py` — 회사 문서 5종 검증·업로드 해석·변경 비교 (파일·DB 접근 없음). import_company.py와 관리자 업로드가 같이 씀
- `templates/` — 관리자 화면 업로드 양식 5종 (필드 설명 + 일반화한 예시 1건)
- `scripts/import_company.py` — 회사 JSON 검증 → DB 가져오기, `--all`, `--reset-tickets`, `--reset-employee-passwords`, `--yes`. 회사 코드 출력
- `scripts/list_companies.py`, `scripts/set_admin_password.py` — 운영자용 회사 코드 확인, ADMIN 비밀번호 설정
- `data/<company_id>/` — policies, employees, locations, handovers, tickets (데이터·QA 담당). DB 가져오기 원본
- `seed/` — 시연 초기화용 tickets.json 원본
- `pwa/`, `scripts/pwa_setup.py`, `scripts/make_pwa_icons.py` — PWA 파일과 Streamlit 정적 폴더 주입(멱등, `--check`, `--undo`)
- `docs/ui_contract.md` — app.py ↔ agent.py 규격. **반환 형식을 바꾸면 이 문서도 같이 고친다**

## 반드시 지킬 규칙
1. 회사별 내용(회사명, 금액, 품목 등)을 코드에 쓰지 않는다. 전부 data/의 JSON에서 읽는다.
2. `company_id`, `employee_id`, `confirmed`는 LLM에게 보이지 않는다. agent.py가 세션 값으로 주입한다.
3. 권한 검증(타인 정보, 타 부서 문서)과 비품 검증(화이트리스트, 수량 한도)은 mcp_server.py에서 한다. 프롬프트에만 의존하지 않는다.
4. 툴 반환 형식: `{"ok": bool, "code": str, "message": str, "data": ...}`. 사용자에게 보일 문구는 `message`에 넣는다.
5. 차단·미등록 시 고정 문구는 `MSG["DENIED_OR_NOT_FOUND"]` 하나만 쓴다.
6. 티켓은 사용자 확인(confirm_ticket) 후에만 생성한다. 거절 시 tickets.json을 수정하지 않는다.
7. 파이썬 예외를 UI로 올리지 않는다. agent 메서드는 항상 ChatResult dict를 돌려준다.
8. 서버 코드는 JSON 파일을 직접 읽거나 쓰지 않는다. 데이터 접근은 storage.py로만. 테스트는 DATABASE_URL(공유 DB)에 연결하지 않는다.
9. 커밋 메시지에 Co-Authored-By 등 AI 관련 문구를 넣지 않는다.
10. 로그인·관리자·등록·업로드 툴은 LLM_TOOLS에 넣지 않는다. 관리자 권한은 서버에서 accounts로 다시 확인한다.
    비밀번호는 해시로만 저장하고 어떤 응답에도 포함하지 않는다(예외: 새로 만든 초기 비밀번호를 관리자에게 한 번만 보여 주는 응답).
    업로드 데이터는 항상 로그인한 관리자의 회사로만 반영한다.
11. 부서 전용 자료는 서버에서 부서를 확인한다(목록과 내려받기 모두). 화면에서 숨기는 것에 의존하지 않는다.
12. 서비스 워커는 `/_stcore/`와 API 응답(페이지 응답 포함)을 저장하지 않는다. 미리 저장하는 것은 아이콘과 offline.html뿐이다.
13. streamlit 버전을 바꾸면 `python scripts/pwa_setup.py`를 다시 실행하고 PWA 동작(manifest 응답, 서비스 워커 등록, 오프라인 화면)을 확인한다.

## 확인 기준 (PRD)
- 정상 1: bridgesoft/EMP001 "점심 식대 한도랑 내 남은 연차 며칠이야?" → 12,000원(복리후생 규정 제4조), 잔여 11일
- 정상 2: "대회의실 위치, 내 인수인계서 요약, 노트북 거치대 1개 신청" → 본관 3층 A구역, 백엔드개발팀 요약, 확정 후 REQ-004
- 예외 1: "사내 수면실 위치", "재무회계팀 인수인계서", "EMP002 연차" → 고정 차단 문구
- 예외 2: 공백 입력 / MCP 서버 중단 → 경고 또는 "사내 시스템 연결이 일시적으로 원활하지 않습니다"
- 다기업: nextwave로 바꾸면 식대 15,000원, 모니터 한도 1대
- 로그인: 회사 코드 + EMP001 + data/의 비밀번호 → 본인 홈(사원증). 새로고침해도 로그인 유지. 틀린 회사 코드·아이디·비밀번호는 같은 문구, 5회 실패 시 10분 잠금
- 관리자: 회사 등록 → 회사 코드 표시 → ADMIN 로그인 → 업로드 미리보기(DB 불변) → 반영 후 search_policy 결과 변경

## 명령
- 테스트: `pytest -q --durations=10` (LLM 없이 실행됨, 원본 data/를 건드리지 않음). PostgreSQL은 `@pytest.mark.pg` 테스트만, 전부는 `--pg-all`. 정적 데이터를 바꾸는 테스트에는 `@pytest.mark.reimport`
- PWA 적용·확인: `python scripts/pwa_setup.py`, `python scripts/pwa_setup.py --check` (서비스 워커는 HTTPS 또는 localhost에서만 동작)
- 터미널 채팅: `python scripts/cli_chat.py bridgesoft EMP001` (`LLM_API_KEY`, `LLM_BASE_URL` 필요)
- 데이터 가져오기: `python scripts/import_company.py --all` (`DATABASE_URL` 없으면 로컬 SQLite)
- 티켓 초기화: `python scripts/import_company.py --all --reset-tickets`
- 시연용 사원 비밀번호 초기화(employees.json 값, ADMIN 제외): `python scripts/import_company.py --all --reset-employee-passwords`
- 회사 코드 확인 / 관리자 비밀번호: `python scripts/list_companies.py`, `python scripts/set_admin_password.py <company_id>`

## Jitda API 보안 및 호환 규칙
- API 키는 서버의 `LLM_API_KEY`에서 읽고 브라우저로 전달하지 않는다.
- 기본 주소는 `LLM_BASE_URL`, 모델은 `jitda-chat`을 사용한다.
- OpenAI-compatible `/chat/completions`의 도구 호출과 응답 형식을 유지한다.
- FunctionResponse에는 id와 name을 모두 넣는다.
