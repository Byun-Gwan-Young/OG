[← README](../README.md) · [직원 가이드](guide-employee.md) · **AI 앱 연결** · [관리자 가이드](guide-admin.md) · [구조와 보안](architecture.md)

# 🤖 AI 앱에서 쓰기 (MCP)

이미 Claude Code, Cursor, VS Code 같은 AI 앱을 쓰고 있다면, 그 앱에 오피스지니를 연결해 대화창에서 바로 물어볼 수 있습니다.

회사 파일을 AI 앱에 올리지 않습니다. 데이터는 회사 서버에 그대로 있고, AI 앱은 질문할 때마다 필요한 결과만 받아 갑니다.

```
나 ──질문──▶ 내 AI 앱 ──툴 호출──▶ 오피스지니 MCP 서버 ──▶ 회사 데이터
                       ◀──결과───
```

<p align="center"><img src="images/mcp-homepage.png" alt="MCP 서버 안내 홈페이지" width="760"><br><sub>MCP 서버 주소로 접속하면 열리는 안내 홈페이지: 앱별 연결 설정을 복사할 수 있습니다</sub></p>

## 연결 방법

### 1. 주소와 토큰 받기

관리자에게 **MCP 주소**(예: `http://192.168.0.10:8000/mcp`)와 **개인 토큰**을 받습니다.

### 2. AI 앱에 추가하기

**Claude Code**

```bash
claude mcp add --transport http officegenie http://<서버 주소>:8000/mcp \
  --header "Authorization: Bearer <개인 토큰>"
```

**Cursor · 기타 (JSON 설정)**

```json
{
  "mcpServers": {
    "officegenie": {
      "url": "http://<서버 주소>:8000/mcp",
      "headers": { "Authorization": "Bearer <개인 토큰>" }
    }
  }
}
```

**VS Code**

```json
{
  "servers": {
    "officegenie": {
      "type": "http",
      "url": "http://<서버 주소>:8000/mcp",
      "headers": { "Authorization": "Bearer <개인 토큰>" }
    }
  }
}
```

### 3. 물어보기

AI 앱에서 "점심 식대 한도랑 내 남은 연차 며칠이야?"라고 물어봅니다.

## 쓸 수 있는 기능

| 툴 | 하는 일 |
|---|---|
| `search_policy` | 사내 규정 검색 (근거 조항 포함) |
| `get_employee_info` | 내 부서 · 직급 · 잔여 연차 |
| `get_office_location` | 사내 장소의 건물 · 층 · 구역 |
| `get_handover_doc` | 소속 부서 인수인계서 |
| `create_support_ticket` | 비품 · 계정 신청 요약 (아직 발급 안 됨) |
| `confirm_support_ticket` | 신청 확정, 티켓 발급 (내가 동의한 뒤에만) |
| `get_my_tickets` | 내 신청 현황 |

## 알아두세요

- **토큰이 곧 내 신원입니다.** 토큰에 연결된 회사 · 사번 기준으로만 정보가 조회되고, 다른 사람의 정보를 지정할 방법은 없습니다.
- **토큰은 공유하지 마세요.** 잃어버렸거나 노출됐다면 관리자에게 폐기와 재발급을 요청하세요.
- **비품 신청은 두 단계입니다.** AI 앱이 신청 요약을 보여주면, 확인하고 동의해야 티켓이 발급됩니다.
- **연결이 안 되면(401):** 토큰이 틀렸거나 아직 서버에 반영되지 않은 토큰입니다. 관리자에게 확인하세요.

---

[← 직원 가이드](guide-employee.md) · 다음 문서: [관리자 가이드 →](guide-admin.md)
