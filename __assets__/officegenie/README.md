# 오피스지니 (OfficeGenie)

MCP 기반 중소기업 범용 온보딩 에이전트. 회사 데이터(JSON 5종)만 바꾸면 코드 수정 없이 다른 회사에 적용됩니다.

```
officegenie/
├── mcp_server.py        # FastMCP 서버 (툴 5개 + UI 전용 툴 4개)
├── agent.py             # Gemini ↔ MCP 연결, app.py가 사용하는 인터페이스
├── app.py               # (UI 담당 작성 예정)
├── data/
│   ├── bridgesoft/      # A사 (주)브릿지소프트
│   └── nextwave/        # B사 (주)넥스트웨이브
├── seed/                # 시연 초기화용 tickets.json
├── docs/ui_contract.md  # UI 연동 규격
├── scripts/cli_chat.py  # 터미널 테스트 채팅
└── tests/               # pytest (LLM 없이 실행)
```

## 시작하기

```bash
pip install -r requirements.txt
pytest -q                                   # 40개 테스트
export GEMINI_API_KEY=...                   # Google AI Studio에서 발급
python scripts/cli_chat.py bridgesoft EMP001
```

## MCP 툴

| 툴 | 단계 | 하는 일 | 서버 쪽 검증 |
|---|---|---|---|
| `search_policy` | MVP | 규정 검색 (회사 내규 우선, 표준취업규칙은 참고용 표시) | 점수 미달 시 NOT_FOUND |
| `get_employee_info` | MVP | 본인 이름·부서·직급·잔여 연차 | 타인 사번·이름 → PERMISSION_DENIED |
| `get_office_location` | 추가 | 건물·층·구역 안내 (별칭 지원) | 미등록 장소 → NOT_FOUND |
| `get_handover_doc` | 추가 | 소속 부서 인수인계서 | 타 부서 → PERMISSION_DENIED |
| `create_support_ticket` | 추가 | 비품·계정 신청 미리보기 | 화이트리스트, 수량 한도, 중복 |

## 다른 회사 추가

`data/<새 company_id>/`에 같은 구조의 JSON 5종을 넣으면 회사 드롭다운에 자동으로 나타납니다.
