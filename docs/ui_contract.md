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

### 2-1. 로그인 · 비밀번호 변경 · 회사 등록

회사 목록·사원 목록 조회(`ui_list_companies`, `ui_list_employees`)는 **없앴다.** 신원은 로그인으로만 정한다.

```python
agent.login(company_code, account_id, password)   # 회사 코드: 대소문자·앞뒤 공백 무시
# 사원 → {"ok": True, "code": "OK", "data": {
#   "company_id": "bridgesoft", "company_name": "(주)브릿지소프트", "role": "employee",
#   "employee_id": "EMP001", "name": "김신입", "department": "백엔드개발팀",
#   "position": "사원" | None, "must_change": False}}
# 관리자 → {"ok": True, "data": {"company_id": ..., "company_name": ..., "role": "admin",
#   "account_id": "ADMIN", "must_change": False}}
# 실패 → {"ok": False, "code": "LOGIN_FAILED",
#   "message": "회사 코드, 아이디 또는 비밀번호가 올바르지 않습니다."}   # 무엇이 틀렸는지 구분 안 함
# 5회 연속 실패 → {"ok": False, "code": "ACCOUNT_LOCKED", "message": "... 10분간 잠겼습니다 ..."}

agent.change_password(company_id, account_id, old_password, new_password)
# 성공 PASSWORD_CHANGED / 실패 PASSWORD_WRONG · PASSWORD_TOO_SHORT(사원 4자, 관리자 8자) ·
#      PASSWORD_SAME · ACCOUNT_LOCKED · INVALID_SESSION

agent.signup_settings()
# → {"ok": True, "data": {"signup_code_required": bool}}   # 가입 코드 값은 돌려주지 않음

agent.register_company(company_name, admin_password, signup_code="")
# → {"ok": True, "code": "REGISTERED", "data": {"company_id": "c_…", "company_name": "…",
#      "company_code": "MHUGS5", "account_id": "ADMIN"}}
# 실패 코드: SIGNUP_CODE_INVALID · COMPANY_NAME_INVALID · ADMIN_PASSWORD_SHORT
```

- 로그인 응답의 `company_id`, `account_id`/`employee_id`, `role`은 **`st.session_state["auth"]`에만** 저장한다. URL 쿼리·입력으로 바꿀 수 있는 경로를 만들지 않는다.
- `must_change=True`면 비밀번호 변경 화면을 먼저 보여 준다.
- 로그아웃·30분 무동작 시 `st.session_state`를 전부 지운다(대화 포함).
- 사원 로그인 후 새 채팅 세션을 만든다.

```python
st.session_state.chat = agent.new_session(auth["company_id"], auth["employee_id"])
```

### 2-1-1. 새로고침 후 로그인 유지 · 메뉴 주소

```python
# 로그인 성공 응답 data에 session_token(무작위 43자)이 추가된다. DB에는 SHA-256 해시만 저장.
agent.resume_session(session_token)
# → 성공 시 agent.login과 같은 data(session_token 제외). 신원·역할은 서버가 accounts에서 다시 정한다.
# → 모르는 토큰·30분 무동작·발급 후 12시간 경과·잠금·비활성 계정: {"ok": False, "code": "INVALID_SESSION"}
agent.logout(session_token)          # 서버에서 토큰 폐기 → {"ok": True, "code": "LOGGED_OUT"}
agent.change_password(...)           # 성공 시 data = {"session_token": 새 토큰}. 이 계정의 기존 세션은 모두 폐기
agent.employee_call("ui_record_page_denied", company_id, employee_id, page="data")
# → 사원이 관리자 메뉴 주소(?page=employees|data)로 들어왔을 때 감사 로그(page_denied)를 남긴다.
```

- app.py는 토큰을 `og_session` 쿠키(SameSite=Strict, HTTPS면 Secure, 12시간)에만 둔다.
  새로고침하면 `st.context.cookies`로 읽어 `resume_session`으로 복원한다. 토큰은 `auth`에 넣지 않는다.
- 관리자가 비밀번호를 초기화하거나 계정을 비활성화하면 그 사원의 세션은 폐기된다
  (열려 있는 화면은 5분 안의 다음 확인 때 로그아웃).
- 메뉴는 URL `?page=`와 동기화한다. 사원: `home|notices|library|equipment`,
  관리자: 위 4개 + `employees|data`. 목록에 없는 값은 홈으로 보낸다. page 값은 신원·권한을 바꾸지 않는다.
- `employee_call`은 `ui_`로 시작하고 `ui_admin_`이 아닌 툴만 부른다. company_id·employee_id는 세션 값으로 채운다.

### 2-2. 우측 사이드바 (잔여 연차 + 신청 현황판)

```python
agent.get_sidebar(company_id, employee_id)
# → {"ok": True, "data": {
#      "company_name": "(주)브릿지소프트",
#      "employee": {"employee_id": "EMP001", "name": "김신입", "department": "백엔드개발팀", "position": "사원"},
#      "annual_leave": {"remaining_days": 11, "granted_days": 15, "used_days": 4, "as_of": "2026-11-20"},
#      "tickets": [   # 본인 티켓만, 최신순
#        {"ticket_id": "REQ-003", "type": "비품", "item": "마우스", "quantity": 1,
#         "purpose": "개발 업무용", "status": "접수완료",
#         "created_at": "2026-11-19T13:15:00+09:00", "scheduled_date": None}, ...],
#      "announcements": [{ "announcement_id": "…", "title": "…", "body": "…",
#         "is_public": True, "created_at": "…", "published_at": "…" }]}}
```

- 현황판 표 컬럼 제안: `티켓 번호 | 구분 | 품목 | 수량 | 상태 | 신청일`
- 상태 값은 5개 고정: `접수완료`, `검토중`, `승인`, `반려`, `지급완료` (`승인`은 대시보드에 "지급 예정"으로 표시)
- `scheduled_date`는 YYYY-MM-DD 또는 `None`입니다. `announcements`는 로그인한 사원의 회사에 공개된 공지만 반환합니다.
- **다시 불러오는 시점**: 응답의 `tickets_changed`가 `True`일 때 (아래 3장)

### 2-2-1. 홈(사원증) · 관리자 홈 · 직원 화면 미리보기

```python
agent.employee_call("ui_get_home", company_id, employee_id)
# → data = {"company_name", "employee": {employee_id, name, department, position|None, job_title|None,
#            onboarding_week|None}, "annual_leave": {...}, "ticket_counts": {상태: 개수}, "tickets_total",
#           "latest_ticket": {...}|None, "announcements": [공개 공지 최대 3개], "announcements_total",
#           "first_week": {"title", "tasks": [소속 부서 인수인계서의 '1주 차 할 일']}}
agent.admin_call("ui_admin_home", company_id, account_id)
# → {"company_name", "company_code", "employee_count", "employees": [{employee_id, name, department}],
#    "pending_count", "pending_tickets": [최대 5건], "announcements": [최근 3개], "recent_uploads": [최근 3건],
#    "has_data"}
agent.admin_call("ui_admin_preview_employee", company_id, account_id, employee_id="EMP001")
# → ui_get_home과 같은 data + {"preview": True}. 같은 회사 사원만(없으면 NOT_FOUND), 감사 로그 preview_employee.
```

- 미리보기 화면은 읽기 전용이다. 신청 폼·챗봇·더보기 버튼을 그리지 않고, 관리자 세션으로는
  `ui_confirm_ticket`이 INVALID_SESSION이라 티켓이 만들어지지 않는다.

### 2-2-2. 공지사항 메뉴

```python
agent.employee_call("ui_list_notices", company_id, employee_id)
# → {"announcements": [공개 공지만, 최신순 {announcement_id, title, body, is_public, created_at, published_at}]}
```

- 사원 화면은 7일 이내 공지에 NEW 배지를 붙이고, 제목·본문 검색과 펼쳐 보기를 제공한다.
- 관리자 "공지사항 관리"는 기존 `ui_admin_list_announcements` / `ui_admin_save_announcement`를 그대로 쓴다
  (목록에서 바로 수정·공개 전환).

### 2-2-3. 자료실

```python
agent.employee_call("ui_list_resources", company_id, employee_id)
# → {"department", "resources": [볼 수 있는 자료(파일 바이트 제외)], "policies": [{category, is_reference_only,
#    policies: [{id, article, title, content}]}](회사 내규 먼저), "handovers": [소속 부서 인수인계서]}
agent.employee_call("ui_download_resource", company_id, employee_id, resource_id="RES-…")
# → 파일: {"kind": "file", "file_name", "mime_type", "size_bytes", "content_base64"}
#   링크: {"kind": "link", "url", "title"}
#   다른 부서 전용·비공개·없는 자료: {"ok": False, "code": "NOT_FOUND", "message": MSG["DENIED_OR_NOT_FOUND"]}

agent.admin_call("ui_admin_list_resources", company_id, account_id)
# → {"resources": [비공개 포함, 바이트 제외], "categories", "departments", "max_bytes", "allowed_types"}
agent.admin_call("ui_admin_download_resource", company_id, account_id, resource_id=...)
agent.admin_call("ui_admin_save_resource", company_id, account_id, title=..., category=..., kind="file"|"link",
                 description="", url="", file_name="", content_base64="", visibility="all"|"department",
                 department="", is_public=True, resource_id=None)
# 실패 코드: RESOURCE_TYPE(허용 형식 아님) · RESOURCE_MISMATCH(내용이 확장자와 다름) · RESOURCE_TOO_LARGE(5MB 초과)
#           RESOURCE_EMPTY · RESOURCE_URL(http/https만) · RESOURCE_INVALID(제목·카테고리·부서) · RESOURCE_FILE_REQUIRED
agent.admin_call("ui_admin_delete_resource", company_id, account_id, resource_id=...)
```

- 허용 형식: pdf, docx, xlsx, pptx, hwp, png, jpg(jpeg), txt. MIME은 확장자로 서버가 정하고, 파일 앞부분으로 형식을 확인한다.
  파일명은 경로·제어 문자를 지우고 정리한다. 변경(등록·수정·삭제)은 모두 감사 로그에 남고, 파일 내용은 남기지 않는다.
- app.py는 `st.download_button(data=callable)`로 버튼을 누를 때만 파일을 받는다(서버가 그때 권한을 다시 확인).

### 2-2-4. 비품 신청 메뉴

```python
agent.employee_call("ui_equipment_options", company_id, employee_id)
# → {"items": [{"item", "limit"|None, "held_or_pending", "remaining"|None, "policy_reference"}]}
agent.employee_call("ui_preview_ticket", company_id, employee_id, item=..., quantity=1, purpose="")
# → create_support_ticket(confirmed=False)와 같다: NEEDS_CONFIRMATION + data.preview, 또는 REJECTED. 저장하지 않는다.
agent.employee_call("ui_confirm_ticket", company_id, employee_id, item=..., quantity=..., purpose=...)
# → CREATED(티켓 번호) · DUPLICATE_IGNORED · REJECTED. 서버가 한도를 다시 확인한다.
agent.employee_call("ui_my_tickets", company_id, employee_id)
# → {"tickets": [본인 티켓 최신순 {ticket_id, type, item, quantity, purpose, status, created_at, updated_at,
#    scheduled_date, history: [{at, from, to, note}]}]}   (처리자 아이디는 넣지 않는다)
```

- 관리자 "비품 관리"는 기존 `ui_admin_list_tickets` · `ui_admin_save_equipment` · `ui_admin_update_ticket`을 그대로 쓴다.
- 챗봇용 LLM 툴 `get_my_tickets`(인자 없음, 세션 값 주입)가 추가되었다. LLM_TOOLS는 6개.
  `scheduled_date`가 없으면 지급 예정일을 지어내지 않도록 시스템 프롬프트에 적었다.

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

### 2-5. 관리자 화면 (`role == "admin"`)

모든 관리자 호출은 `agent.admin_call(툴 이름, company_id, actor_account_id, **인자)` 하나로 한다. 서버가 매번 `accounts`에서 `role=admin`인지 다시 확인하고, 아니면 `{"ok": False, "code": "PERMISSION_DENIED", "message": "관리자만 사용할 수 있는 기능입니다."}`를 준다(받으면 로그아웃). 항상 로그인한 관리자 회사의 데이터만 다룬다.

| 툴 | 추가 인자 | 성공 `data` |
|---|---|---|
| `ui_admin_overview` | - | `company_id, company_name, company_code, data_version, counts{policies, employees, locations, handovers, equipment, tickets}, has_data, cache_ttl_seconds` |
| `ui_admin_preview_upload` | `files=[{"name", "content"}]` | `base_version, fingerprint, kinds, changes[{kind, label, added, updated, removed, unchanged, added_ids, updated_ids, removed_ids, settings_changed}], initial_passwords_to_create, cache_ttl_seconds`. **DB는 바꾸지 않는다** |
| `ui_admin_apply_upload` | `files, base_version, fingerprint` (미리보기 값 그대로) | `data_version, changes, initial_passwords[{employee_id, name, initial_password}], cache_ttl_seconds` |
| `ui_admin_list_employees` | - | `employees[{employee_id, name, department, position, job_title, employment_status, remaining_days, account_status, account_status_label}]` |
| `ui_admin_employee_detail` | `employee_id` | `employee, annual_leave, tickets, handovers[{handover_id, title, last_updated, summary}], account_status, account_status_label` |
| `ui_admin_save_employee` | `employee_id, employee_name, department, position, remaining_days, employment_status, create` | `initial_password` (등록 직후 `{employee_id, name, initial_password}`, 수정 시 `None`) |
| `ui_admin_list_tickets` | - | `tickets[{ticket_id, employee_id, name, department, type, item, quantity, purpose, status, created_at, scheduled_date, history, allowed_next}]` (최신순) |
| `ui_admin_update_ticket` | `ticket_id, to_status, note=""` | `ticket` (위와 같은 형태) |
| `ui_admin_save_equipment` | `ticket_id, to_status, expected_status, scheduled_date=None, expected_scheduled_date=None` | `ticket` (비품 상태와 예정일 저장) |
| `ui_admin_list_announcements` | - | `announcements` (위 사원 공지 형태와 동일, 비공개 포함) |
| `ui_admin_save_announcement` | `title, body, is_public, announcement_id=None` | `None`. ID가 없으면 작성, 있으면 수정 |
| `ui_admin_list_policies` | - | `policies[{id, category, article, title, content, keywords, source, source_version}], data_version` (챗봇 규정 원문, 조회 전용) |
| `ui_admin_list_accounts` | - | `accounts[{employee_id, name, department, account_status, account_status_label, failed_count, locked_until}]` |
| `ui_admin_reset_password` | `employee_id` | `{employee_id, initial_password}` (새 4자리, `must_change=True`) |
| `ui_admin_unlock_account` | `employee_id` | `None` |

- 업로드 `files`는 메모리의 텍스트다(파일로 저장하지 않음). 종류는 파일 이름(`policies.json` 등)이나 내용의 목록 키로 정한다. 파일당 2MB, JSON만.
- 업로드 실패: `UPLOAD_EMPTY`, `UPLOAD_INVALID`(`data.issues[{file, item, field, problem, message}]`), `UPLOAD_STALE`(미리보기 이후 데이터·파일이 바뀜 → 다시 미리보기).
- 티켓 상태: `접수완료 → 검토중 → 승인 → 지급완료`, `반려`는 접수완료·검토중·승인에서만(사유 필수). 실패 코드 `INVALID_TRANSITION`, `NOTE_REQUIRED`, `NOT_FOUND`.
- 비품 관리 탭에서는 접수완료·검토중을 접수, 승인을 지급 예정, 지급완료를 지급 완료로 표시합니다. 저장할 때 상세를 연 시점의 상태와 예정일이 달라졌으면 `STALE_TICKET`을 반환합니다.
- 관리자 비밀번호 변경은 `agent.change_password(company_id, "ADMIN", old, new)`.
- 초기 비밀번호는 `ui_admin_apply_upload`, `ui_admin_reset_password`, `ui_admin_save_employee`가 새로 발급할 때만 관리자에게 표시합니다. 비밀번호 원문은 영구 저장하지 않으며 해시·솔트는 응답에 들어가지 않습니다.
- 로그인·등록·관리자 툴은 `LLM_TOOLS`에 넣지 않는다.

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

auth = st.session_state.get("auth")
if not auth:
    with st.form("login"):
        code = st.text_input("회사 코드")
        account = st.text_input("아이디")
        password = st.text_input("비밀번호", type="password")
        if st.form_submit_button("로그인"):
            r = agent.login(code, account, password)
            if r["ok"]:
                st.session_state.auth = r["data"]
                st.rerun()
            st.error(r["message"])
    st.stop()

key = (auth["company_id"], auth["employee_id"])
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

시연 후 티켓 데이터 초기화: `python scripts/import_company.py --all --reset-tickets` (seed 티켓으로 교체. 처음 가져온 DB라면 REQ-004부터, 이미 더 큰 번호를 쓴 적이 있으면 그다음 번호부터 생성됨)
