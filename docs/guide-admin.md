[← README](../README.md) · [직원 가이드](guide-employee.md) · [AI 앱 연결](guide-mcp.md) · **관리자 가이드** · [구조와 보안](architecture.md)

# 🛠 관리자 가이드

오피스지니를 쓰는 관리자는 두 종류입니다.

| 누구 | 하는 일 | 이 문서 |
|---|---|---|
| 🏢 **회사 관리자** (고객사 담당자) | 웹에서 회사 등록, 데이터 업로드, 사원 계정·신청 관리 | [1부](#1부-회사-관리자) |
| 🧑‍🔧 **서비스 운영자** (우리) | 서버 배포, 가입 코드 관리, 운영 스크립트 | [2부](#2부-서비스-운영자) |

---

# 1부. 회사 관리자

설치할 것은 없습니다. 서비스 주소에 접속해 회사를 등록하고 데이터만 올리면 바로 사용할 수 있습니다.

## 1. 회사 등록

1. 로그인 화면 아래 **"처음 오셨나요? 회사 등록하기"** 를 누릅니다.
2. 회사명과 관리자 비밀번호(8자 이상)를 입력합니다. 서비스 운영자가 가입 코드를 정해 두었다면 **서비스 가입 코드**도 입력합니다.
3. 등록이 끝나면 **회사 코드**(영문 대문자·숫자 6자리, 예: `MHUGS5`)가 크게 표시됩니다. 복사 버튼으로 복사해 두세요.
   - 같은 회사명으로 다시 등록해도 코드는 항상 새로 만들어집니다.

## 2. 관리자로 로그인

**"관리자로 로그인하기"** 를 누르면 회사 코드와 아이디 `ADMIN`이 자동으로 채워집니다. 다음부터는 로그인 화면에서 **회사 코드 + `ADMIN` + 관리자 비밀번호** 로 들어옵니다. 관리자 화면에는 챗봇이 없습니다.

## 3. 데이터 관리 (양식 → 업로드 → 미리보기 → 반영)

처음 등록한 회사는 데이터가 비어 있어 "아직 회사 데이터가 없습니다. 데이터 관리에서 업로드하세요"가 표시됩니다.

1. **양식 다운로드**: 종류별(규정·사원·장소·인수인계서·비품·신청 규칙) 양식에 필드 설명과 예시 1건이 들어 있습니다. `_안내` 항목과 예시는 지우거나 그대로 둬도 됩니다(업로드할 때 무시).
2. **파일 올리기**: `policies.json`, `employees.json`, `locations.json`, `handovers.json`, `tickets.json` 중 하나 또는 여러 개를 한 번에 올립니다. 파일당 2MB 이하, JSON만 가능합니다.
   - 파일 안의 `company_id`·회사명은 무시되고 항상 **내 회사**로 저장됩니다.
   - 문법 오류는 줄 번호로, 빠진 필드는 "파일 · 몇 번째 항목 · 필드 · 문제" 표로 알려 주고 아무것도 바꾸지 않습니다.
3. **미리보기**: 종류별 추가·수정·삭제 개수와 바뀐 항목 ID를 보여 줍니다. 이 단계에서는 아직 반영되지 않습니다.
4. **반영하기**: 한 번에(트랜잭션 하나로) 반영됩니다. 중간에 실패하면 전부 되돌립니다. 미리보기 이후 다른 반영이 먼저 있었다면 다시 미리보기를 만들라고 안내합니다.
   - 올린 종류만 바뀌고 나머지 종류는 그대로입니다. 올린 종류는 통째로 교체되므로 파일에서 뺀 항목은 삭제됩니다.
   - **비품·신청 규칙(tickets.json)** 은 허용 비품·수량 한도·안내 문구만 반영합니다. 기존 신청 내역과 다음 신청 번호는 바뀌지 않습니다.
   - 챗봇 답변에는 **최대 N초**(화면에 표시, 기본 60초) 안에 반영됩니다.

## 4. 사원에게 회사 코드와 초기 비밀번호 전달

- 사원 파일에 `password`를 넣으면 그 값이 초기 비밀번호가 됩니다(4자 이상).
- `password`를 비워 두면 4자리 초기 비밀번호를 만들어 **반영 직후 한 번만** 표와 CSV로 보여 줍니다. 서버에는 원문이 남지 않으니 바로 내려받거나 전달하세요. 이 사원들은 첫 로그인 때 비밀번호를 바꿔야 합니다.
- 이미 계정이 있는 사원은 사원 파일을 다시 올려도 비밀번호가 바뀌지 않습니다(사원이 바꾼 비밀번호 보존).
- 사원 파일에서 빠진 사원의 계정은 삭제되지 않고 로그인만 막힙니다(다시 넣으면 다시 열림).
- 사원에게는 **회사 코드 · 사번 · 초기 비밀번호** 를 알려 주고 [직원 가이드](guide-employee.md)를 공유하세요.

## 5. 사원 · 신청 관리 · 계정

| 탭 | 할 수 있는 것 |
|---|---|
| **사원** | 부서 필터, 이름·사번 검색. 행을 선택하면 인사 정보, 연차, 신청 내역, 소속 부서 인수인계서 요약 |
| **신청 관리** | 상태·부서 필터. 상태 변경은 `접수완료 → 검토중 → 승인 → 지급완료` 순서로만, `반려`는 접수완료·검토중·승인에서 사유를 적어야 가능. 변경 이력(시각·처리자·이전·이후·메모)이 남고 직원 대시보드 "내 비품 내역"에 바로 보입니다(승인 = "지급 예정") |
| **계정** | 사원 계정 상태(정상/잠금/변경 필요/비활성), 비밀번호 초기화(새 4자리, 한 번만 표시), 잠금 해제, 관리자 비밀번호 변경 |

관리자의 조회·변경·업로드는 모두 감사 로그(`audit_log`)에 기록됩니다.

---

# 2부. 서비스 운영자

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

- **테스트는 dev 브랜치에서만** 돌립니다. `pytest`는 `DATABASE_URL`(운영 DB)에 절대 연결하지 않고, 기본은 임시 SQLite입니다. `TEST_DATABASE_URL`이 있으면 그 DB에 임시 스키마를 만들어 DB 종류에 따라 결과가 달라질 수 있는 테스트(`@pytest.mark.pg`)만 한 번 더 돌린 뒤 지웁니다. 전부 돌리려면 `pytest --pg-all`(오래 걸림). 두 주소가 같으면 테스트가 바로 중단됩니다.
- **인터넷이 안 되거나 DB 없이 써 보고 싶으면** `.env`에서 `DATABASE_URL` 줄을 지우세요. 프로젝트 폴더의 `officegenie.db`(로컬 SQLite)로 동작하며 시작 로그에 "로컬 SQLite 사용 중"이 나옵니다. 이 경우에도 처음 한 번은 `python scripts/import_company.py --all`을 실행합니다.
- DB가 비어 있으면 서버가 "python scripts/import_company.py --all을 먼저 실행하세요"라고 안내합니다. DB에 연결할 수 없으면 화면에는 "사내 시스템 연결이 일시적으로 원활하지 않습니다"가 나옵니다.

## 4. 웹 화면 실행

```bash
streamlit run app.py
```

터미널에 나오는 `Network URL`(예: `http://192.168.0.10:8501`)을 직원들에게 공유합니다. 같은 네트워크에 있는 사람은 이 주소로 바로 접속할 수 있습니다.

- MCP 툴은 앱이 자동으로 띄우므로 따로 실행하지 않습니다.
- Windows에서는 처음 실행할 때 방화벽 창에서 "개인 네트워크 허용"을 선택하세요.

첫 화면은 로그인입니다(회사 코드 · 아이디 · 비밀번호). 회사 목록은 화면에 나오지 않습니다. 30분 동안 조작이 없으면 자동으로 로그아웃됩니다.

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
| 회사 등록 제한 | `.env`에 `OFFICEGENIE_SIGNUP_CODE=<가입 코드>`를 두면 그 코드를 아는 사람만 회사를 등록할 수 있습니다. 비워 두면 누구나 등록(시연용). 가입 코드 값은 화면·로그·응답에 나오지 않습니다. |
| 회사 목록 · 회사 코드 확인 | `python scripts/list_companies.py` → company_id, 회사명, 회사 코드, 사원 수 |
| 관리자 비밀번호 설정·분실 | `python scripts/set_admin_password.py <company_id>` (화면에서 입력, 8자 이상). 자동화는 `--password '...'`. 계정이 없으면 만들고 잠금도 풉니다. |
| JSON 폴더로 회사 가져오기 | `python scripts/import_company.py data/<회사>` 또는 `--all`. 처음 가져올 때 회사 코드를 만들고 "회사명 / 회사 코드"를 출력합니다. 다시 가져와도 코드는 바뀌지 않습니다. `password`가 없는 사원의 초기 비밀번호는 이때 화면에 한 번만 출력됩니다. |
| 재가져오기와 신청 내역 | 이미 DB에 신청 내역이 있으면 티켓과 다음 티켓 번호는 그대로 둡니다("티켓은 기존 DB 상태 유지"). |
| 시연 후 신청 내역 초기화 | `python scripts/import_company.py --all --reset-tickets` (seed/의 티켓으로 교체, 이미 쓴 번호는 다시 쓰지 않음) |
| 시연 전 사원 비밀번호 되돌리기 | `python scripts/import_company.py --all --reset-employee-passwords` (employees.json의 password로 다시 해시, 잠금·실패 횟수 초기화, ADMIN 제외. 바꾼 인원 수만 출력) |
| 가져오기 오류 | JSON을 전부 검사한 뒤에 DB를 바꿉니다(관리자 업로드와 같은 검증). 오류가 있으면 파일명과 항목 번호를 보여 주고 아무것도 바꾸지 않습니다. |
| 기존 DB 업그레이드 | 서버·스크립트가 시작할 때 `init_db()`가 새 테이블(`accounts`, `audit_log`)과 컬럼(`company_code`, 티켓 `history`)을 추가하고, 코드가 없는 회사에 회사 코드를 채웁니다. 기존 데이터는 그대로입니다. |
| 새 회사 추가 | 회사 관리자가 웹에서 직접 등록하는 것이 기본입니다. 폴더로 넣을 때는 [다른 회사에 적용하기](architecture.md#-다른-회사에-적용하기) |
| LLM 연결 확인 | `python scripts/cli_chat.py bridgesoft EMP001` (실패하면 `[오류 원인]` 출력) |

## 7. 문제 해결

| 증상 | 원인과 해결 |
|---|---|
| `requirements.txt`를 찾을 수 없음 | zip을 풀면 폴더가 한 겹 더 생깁니다. 안쪽 폴더로 `cd` 하세요. |
| `ModuleNotFoundError: dotenv` / `sqlalchemy` / `psycopg` | `pip install -r requirements.txt`를 다시 실행하세요. |
| 화면에 "사내 시스템 연결이 일시적으로 원활하지 않습니다" | DB 연결 실패입니다. 인터넷과 `.env`의 `DATABASE_URL`을 확인하세요. 급하면 `DATABASE_URL`을 지워 로컬 SQLite로 동작시킬 수 있습니다. |
| 로그인 시 "회사 코드, 아이디 또는 비밀번호가 올바르지 않습니다" | 어느 값이 틀렸는지는 일부러 알려 주지 않습니다. `list_companies.py`로 회사 코드를 확인하세요. 같은 계정으로 5번 연속 틀리면 10분간 잠기며, 회사 관리자가 계정 탭에서 바로 풀 수 있습니다. |
| 관리자 비밀번호 분실 | `python scripts/set_admin_password.py <company_id>` |
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
| `OFFICEGENIE_SIGNUP_CODE` | (없음 = 누구나 등록) | 회사 등록에 필요한 서비스 가입 코드 |
| `OFFICEGENIE_CACHE_TTL` | `60` | 관리자 반영 후 다른 서버 프로세스가 새 데이터를 확인하는 주기(초) |
| `OFFICEGENIE_LOG_LEVEL` | `INFO` | `DEBUG`로 두면 툴 호출마다 DB 왕복 횟수를 기록 |
| `OFFICEGENIE_PORT` | `8000` | 원격 MCP 서버 포트 |
| `OFFICEGENIE_TOKENS` | `./tokens.json` | 원격 MCP 서버 토큰 파일 |

---

[← AI 앱 연결](guide-mcp.md) · 다음 문서: [구조와 보안 →](architecture.md)
