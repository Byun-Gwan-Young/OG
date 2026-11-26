[← README](../README.md) · [직원 가이드](guide-employee.md) · [AI 앱 연결](guide-mcp.md) · **관리자 가이드** · [구조와 보안](architecture.md)

# 🛠 관리자 가이드: 설치와 운영

설치는 관리자가 서버 PC 한 곳에서 한 번만 합니다. 직원들은 관리자가 공유한 주소로 접속만 합니다.

## 1. 설치

```bash
git clone https://github.com/Byun-Gwan-Young/OG.git officegenie
cd officegenie
python -m venv .venv

# macOS / Linux
source .venv/bin/activate
# Windows PowerShell
Set-ExecutionPolicy -Scope Process Bypass
.\.venv\Scripts\Activate.ps1

pip install -r requirements.txt
python -m pytest -q        # API 키 없이 실행, 임시 SQLite 사용
```

## 2. LLM API 키 설정

`env.example`을 복사해 `.env`로 저장하고 값을 채웁니다. `.env`는 저장소에 올라가지 않습니다.

```bash
LLM_API_KEY=발급받은 키
LLM_BASE_URL=https://.../v1      # Jitda 설정 화면의 API 주소
```

## 3. 데이터베이스 준비 (Neon PostgreSQL)

서버는 실행 중에 JSON 파일을 읽지 않고 **DB만** 씁니다. `data/`의 JSON은 DB에 넣기 위한 원본입니다.

1. [Neon](https://neon.tech)에서 프로젝트를 만들고 브랜치를 두 개 둡니다.
   - `main`: 실제 운영용
   - `dev`: 테스트·연습용 (운영 데이터와 분리)
2. 각 브랜치의 연결 주소(`postgresql://...`)를 복사해 `.env`에 넣습니다.

   ```bash
   DATABASE_URL=postgresql://사용자:비밀번호@호스트/DB이름?sslmode=require        # main 브랜치
   TEST_DATABASE_URL=postgresql://사용자:비밀번호@호스트/DB이름?sslmode=require   # dev 브랜치 (선택)
   ```

3. 회사 데이터를 DB로 가져옵니다.

   ```bash
   python scripts/import_company.py --all
   ```

   SQLite가 아닌 DB에 넣을 때는 비밀번호를 뺀 호스트, 회사, 작업 내용을 보여 주고 `y` 확인을 받습니다. 자동화할 때는 `--yes`를 붙입니다.

> 🔐 **DB 주소는 비밀번호가 들어 있는 비밀 정보입니다.** `.env`에만 두고 채팅·문서·스크린샷·커밋에 넣지 마세요. 서버 로그와 오류 메시지에는 주소가 나오지 않습니다.

- **테스트는 dev 브랜치에서만** 돌립니다. `pytest`는 `DATABASE_URL`(운영 DB)에 절대 연결하지 않고, 기본은 임시 SQLite입니다. `TEST_DATABASE_URL`이 있으면 그 DB에 임시 스키마를 만들어 같은 테스트를 한 번 더 돌린 뒤 지웁니다. 두 주소가 같으면 테스트가 바로 중단됩니다.
- **인터넷이 안 되거나 DB 없이 써 보고 싶으면** `.env`에서 `DATABASE_URL` 줄을 지우세요. 프로젝트 폴더의 `officegenie.db`(로컬 SQLite)로 동작하며 시작 로그에 "로컬 SQLite 사용 중"이 나옵니다. 이 경우에도 처음 한 번은 `python scripts/import_company.py --all`을 실행합니다.
- DB가 비어 있으면 서버가 "python scripts/import_company.py --all을 먼저 실행하세요"라고 안내합니다. DB에 연결할 수 없으면 화면에는 "사내 시스템 연결이 일시적으로 원활하지 않습니다"가 나옵니다.

## 4. 직원용 웹 화면 실행

```bash
streamlit run app.py
```

터미널에 나오는 `Network URL`(예: `http://192.168.0.10:8501`)을 직원들에게 공유합니다. 같은 네트워크에 있는 사람은 이 주소로 바로 접속할 수 있습니다.

- MCP 툴은 앱이 자동으로 띄우므로 따로 실행하지 않습니다.
- Windows에서는 처음 실행할 때 방화벽 창에서 "개인 네트워크 허용"을 선택하세요.

> 현재 웹 화면은 시연용으로 첫 번째 회사(브릿지소프트)의 EMP001(김신입) 계정으로 열립니다. 실제 도입 시에는 사내 로그인과 연결해 접속한 직원을 판별해야 합니다.

## 5. AI 앱용 MCP 서버 실행 (선택)

```bash
python scripts/issue_token.py bridgesoft EMP001 --name "김신입"   # 사람마다 토큰 발급
python scripts/issue_token.py --list                              # 발급 목록
python scripts/issue_token.py --revoke og_abcd1234                # 폐기 (앞 8자리)
python remote_server.py                                           # http://<서버 주소>:8000
```

| 주소 | 내용 | 토큰 |
|---|---|---|
| `/` | 안내 홈페이지 (앱별 연결 설정) | 불필요 |
| `/mcp` | MCP 엔드포인트 (Streamable HTTP) | **필요** |
| `/health` | 상태 확인 | 불필요 |

- 직원에게는 `http://<서버 주소>:8000/mcp` 주소와 **본인 토큰**을 전달합니다. 연결 방법은 [AI 앱 연결 가이드](guide-mcp.md)를 공유하면 됩니다.
- 토큰을 발급하거나 폐기한 뒤에는 서버를 다시 시작하세요.

> ⚠️ 토큰은 `tokens.json`에 평문으로 저장되는 **개발 · 시연용 방식**입니다(저장소에는 올라가지 않음). 실제 운영에서는 OAuth 등 정식 인증과 HTTPS를 사용하세요. 사내 네트워크 밖에서 접속하게 하려면 서버 배포나 터널 도구가 필요합니다.

## 6. 운영

| 하고 싶은 것 | 방법 |
|---|---|
| 규정 · 장소 · 사원 정보 수정 | `data/<회사>/`의 JSON을 고친 뒤 `python scripts/import_company.py data/<회사>`로 다시 가져옵니다. 회사 단위로 통째로 교체되므로 JSON에서 지운 항목은 DB에서도 지워집니다. 서버는 60초 안에 바뀐 내용을 반영하므로 재시작이 필요 없습니다. |
| 재가져오기와 신청 내역 | 이미 DB에 신청 내역이 있으면 티켓과 다음 티켓 번호는 그대로 둡니다("티켓은 기존 DB 상태 유지"). |
| 시연 후 신청 내역 초기화 | `python scripts/import_company.py --all --reset-tickets` (seed/의 티켓으로 교체, 이미 쓴 번호는 다시 쓰지 않음) |
| 가져오기 오류 | JSON을 전부 검사한 뒤에 DB를 바꿉니다. 오류가 있으면 파일명과 항목 번호를 보여 주고 아무것도 바꾸지 않습니다. |
| 새 회사 추가 | [다른 회사에 적용하기](architecture.md#-다른-회사에-적용하기) |
| LLM 연결 확인 | `python scripts/cli_chat.py bridgesoft EMP001` (실패하면 `[오류 원인]` 출력) |

## 7. 문제 해결

| 증상 | 원인과 해결 |
|---|---|
| `requirements.txt`를 찾을 수 없음 | zip을 풀면 폴더가 한 겹 더 생깁니다. 안쪽 폴더로 `cd` 하세요. |
| `ModuleNotFoundError: dotenv` / `sqlalchemy` / `psycopg` | `pip install -r requirements.txt`를 다시 실행하세요. |
| 화면에 "사내 시스템 연결이 일시적으로 원활하지 않습니다" | DB 연결 실패입니다. 인터넷과 `.env`의 `DATABASE_URL`을 확인하세요. 급하면 `DATABASE_URL`을 지워 로컬 SQLite로 동작시킬 수 있습니다. |
| 회사 목록이 비어 있음 | DB가 비어 있습니다. `python scripts/import_company.py --all`을 실행하세요. |
| 챗봇이 "불러오지 못했어요"만 표시 | `.env`의 `LLM_API_KEY`, `LLM_BASE_URL`을 확인하세요. `scripts/cli_chat.py`로 실행하면 `[오류 원인]`이 출력됩니다. |
| 다른 PC에서 웹 화면 접속 불가 | Windows 방화벽에서 "개인 네트워크 허용"을 선택하고, 같은 네트워크인지 확인하세요. |
| AI 앱에서 MCP 연결 시 401 | 토큰이 틀렸거나 서버 재시작 전 발급된 토큰입니다. `--list`로 확인 후 서버를 다시 시작하세요. |
| Windows에서 한글 깨짐 | `chcp 65001` 입력 후 다시 실행하세요. |

## 8. 설정 값

| 환경 변수 | 기본값 | 설명 |
|---|---|---|
| `LLM_API_KEY` | (필수) | Jitda LLM API 키 |
| `LLM_BASE_URL` | (필수) | Jitda API 주소 (`/v1`로 끝나는 주소) |
| `OFFICEGENIE_LLM_TIMEOUT` | `30` | LLM 응답 대기 시간(초) |
| `DATABASE_URL` | (없으면 `./officegenie.db`) | PostgreSQL(Neon) 주소. 비밀 정보 |
| `TEST_DATABASE_URL` | (선택) | 테스트 전용 DB(dev 브랜치) 주소. `DATABASE_URL`과 달라야 함 |
| `OFFICEGENIE_LOG_LEVEL` | `INFO` | `DEBUG`로 두면 툴 호출마다 DB 왕복 횟수를 기록 |
| `OFFICEGENIE_PORT` | `8000` | 원격 MCP 서버 포트 |
| `OFFICEGENIE_TOKENS` | `./tokens.json` | 원격 MCP 서버 토큰 파일 |

---

[← AI 앱 연결](guide-mcp.md) · 다음 문서: [구조와 보안 →](architecture.md)
