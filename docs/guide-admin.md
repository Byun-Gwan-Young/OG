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

1. 첫 화면에서 **회사 등록**을 누릅니다.
2. 회사명과 관리자 비밀번호(8자 이상)를 입력합니다. 서비스 운영자가 가입 코드를 정해 두었다면 **서비스 가입 코드**도 입력합니다.
3. 등록이 끝나면 **회사 코드**(영문 대문자·숫자 6자리, 예: `MHUGS5`)가 크게 표시됩니다. 복사 버튼으로 복사해 두세요.
   - 같은 회사명으로 다시 등록해도 코드는 항상 새로 만들어집니다.

## 2. 관리자로 로그인

**"관리자로 로그인하기"** 를 누르면 회사 코드와 아이디 `ADMIN`이 자동으로 채워집니다. 다음부터는 로그인 화면에서 **회사 코드 + `ADMIN` + 관리자 비밀번호** 로 들어옵니다. 관리자 화면에는 챗봇이 없습니다.

관리자 메뉴는 **홈 · 공지사항 관리 · 자료실 관리 · 비품 관리 · 사원 · 데이터·계정**입니다. PC(1180px 이상)는 위쪽 메뉴,
태블릿·휴대폰은 아래쪽 탭바를 씁니다. 메뉴는 주소(`?page=equipment` 등)와 함께 바뀌어 새로고침해도 그 메뉴가 열립니다.
사원 계정으로 관리자 메뉴 주소(`?page=employees`, `?page=data`)를 열면 홈으로 돌아가고 감사 로그(`page_denied`)에 남습니다.

<p align="center"><img src="images/ui-review/after-pc-admin-home.png" alt="관리자 홈" width="820"></p>

**홈**에는 회사 카드(회사명, 회사 코드와 복사 버튼, 사원 수), 처리 대기 신청, 최근 공지(비공개 표시), 최근 업로드가 있습니다.

## 3. 데이터·계정 메뉴: 데이터 올리기 (양식 → 업로드 → 미리보기 → 반영)

**데이터·계정** 메뉴 위쪽에 회사 코드가 있고, 아래에 **자료 업로드 · 사내 규정 · 계정** 탭이 있습니다.
처음 등록한 회사는 데이터가 비어 있어 "아직 회사 데이터가 없습니다. 자료 업로드에서 등록하세요."가 표시됩니다.

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

## 5. 메뉴별 관리

| 메뉴 · 탭 | 할 수 있는 것 |
|---|---|
| **공지사항 관리** | **＋ 공지 작성**, 목록에서 바로 **수정**(같은 자리에 폼이 열림), **공개하기/비공개로** 바로 전환. 공개 공지만 사원 공지사항·홈에 보입니다 |
| **비품 관리 › 비품 지급** | 비품 신청 목록·검색·상태 필터, 상세에서 지급 상태(접수/지급 예정/지급 완료)와 지급 예정일 |
| **비품 관리 › 신청 상태·이력** | 상태·부서 필터. 상태 변경은 `접수완료 → 검토중 → 승인 → 지급완료` 순서로만, `반려`는 사유를 적어야 가능. 변경 이력(시각·처리자·이전·이후·메모)이 남고 사원의 "내 신청 내역"과 챗봇 답변에 바로 반영됩니다(승인 = "지급 예정") |
| **사원 › 사원 목록** | 사원 추가·수정(부서, 직급, 잔여 연차, 재직 상태) |
| **사원 › 사원 상세** | 부서 필터, 이름·사번 검색. 행을 선택하면 인사 정보, 연차, 신청 내역, 소속 부서 인수인계서 요약 |
| **데이터·계정 › 사내 규정** | 챗봇이 답변에 쓰는 규정 원문 검색·열람 |
| **데이터·계정 › 계정** | 사원 계정 상태(정상/잠금/변경 필요/비활성), 비밀번호 초기화(새 4자리, 한 번만 표시), 잠금 해제, 관리자 비밀번호 변경. 비밀번호를 초기화하면 그 사원의 로그인 세션도 끝납니다 |
| **데이터·계정 › AI 앱 연결** | 사원이 홈에서 발급한 AI 앱 연결 토큰 현황(사번·이름·토큰 이름·발급일·마지막 사용·상태)과 **강제 폐기**. 토큰 원문은 저장하지 않아 관리자도 볼 수 없고, 폐기하면 그 AI 앱은 바로 연결이 끊깁니다 |

관리자의 조회·변경·업로드는 모두 감사 로그(`audit_log`)에 기록됩니다.

## 6. 자료실 관리

사원 **자료실**에 보일 파일과 링크를 관리합니다.

1. **＋ 자료 등록** → 제목, 카테고리(양식 · 가이드 · 교육 자료 · 부서 자료 · 기타), 설명(선택)
2. 종류: **파일**(pdf, docx, xlsx, pptx, hwp, png, jpg, txt · 파일당 5MB 이하) 또는 **링크**(`http://`·`https://` 주소만)
3. 공개 범위: **전체** 또는 **특정 부서**(그 부서 사원에게만 보임), **사원에게 공개** 켜기/끄기(끄면 관리자만 봄)
4. **저장**. 목록에서 **내려받기/열기 · 수정 · 삭제**(삭제는 한 번 더 확인)

- 형식·용량이 맞지 않으면 이유를 보여 주고 저장하지 않습니다. 확장자만 바꾼 파일(내용이 다른 형식)도 거부합니다.
- 파일 이름은 경로·특수 문자를 정리해 저장합니다. 수정할 때 새 파일을 고르지 않으면 기존 파일을 그대로 둡니다.
- 부서 전용 자료는 서버가 사원의 부서를 확인해 다른 부서에는 목록에도 보이지 않고 내려받기도 거부합니다.
- 등록·수정·삭제는 감사 로그(`resource_created`·`resource_updated`·`resource_deleted`)에 남습니다(파일 내용은 남기지 않음).

## 7. 직원 화면 미리보기

관리자 **홈 › 직원 화면 미리보기**에서 사원을 고르고 **미리보기**를 누르면 그 사원이 보는 홈(사원증·위젯)을 볼 수 있습니다.

- 위쪽 노란 띠에 "직원 화면 미리보기 · 이름(사번) · 읽기 전용"이 표시됩니다. **미리보기 닫기**로 돌아갑니다.
- 읽기 전용입니다. 신청·변경 버튼과 챗봇이 없고, 관리자 세션으로는 신청이 만들어지지 않습니다.
- **AI 앱 연결** 카드는 안내 문구만 보이고 토큰 발급·폐기 버튼이 없습니다. 서버도 관리자 세션의 발급·폐기를 거부합니다.
- 서버가 관리자 계정과 같은 회사 사원인지 다시 확인하고, 볼 때마다 감사 로그(`preview_employee`)를 남깁니다.

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
- **인터넷이 안 되거나 DB 없이 써 보고 싶으면**(로컬 개발 한정) `.env`에서 `DATABASE_URL` 줄을 지우세요. 프로젝트 폴더의 `officegenie.db`(로컬 SQLite)로 동작하며 시작 로그에 "로컬 SQLite 사용 중"이 나옵니다. 이 경우에도 처음 한 번은 `python scripts/import_company.py --all`을 실행합니다.
- DB가 비어 있으면 서버가 "python scripts/import_company.py --all을 먼저 실행하세요"라고 안내합니다. DB에 연결할 수 없으면 화면에는 "사내 시스템 연결이 일시적으로 원활하지 않습니다"가 나옵니다.

## 4. 웹 화면 실행

```bash
python scripts/pwa_setup.py     # 처음 한 번, 그리고 streamlit을 다시 설치·업그레이드할 때마다
streamlit run app.py
```

- `pwa_setup.py`는 설치된 streamlit 패키지의 static 폴더에 PWA 파일(manifest, 서비스 워커, 오프라인 화면, 아이콘)을
  복사하고 `index.html` `<head>`에 태그를 넣습니다. 여러 번 실행해도 한 번만 들어갑니다.
  - `python scripts/pwa_setup.py --check`: 적용 여부만 확인(적용됨이면 0)
  - `python scripts/pwa_setup.py --undo`: 넣은 태그와 파일을 원래대로 되돌림
  - 패키지 폴더에 쓸 권한이 필요합니다. 가상환경(.venv) 사용을 권장합니다.
- 적용하지 않고 실행하면 서버 로그에 "PWA 설정이 적용되지 않았습니다. python scripts/pwa_setup.py 실행 필요" 경고가 나옵니다(화면은 정상 동작).
- **서비스 워커와 홈 화면 설치는 HTTPS 또는 localhost에서만 동작합니다.** 직원 휴대폰에서 설치하게 하려면
  HTTPS 주소(리버스 프록시·배포 플랫폼의 인증서)로 서비스하세요. `http://192.168.x.x` 주소에서는 웹 화면은 되지만 설치는 안 됩니다.
- `requirements.txt`의 streamlit 버전은 고정(`==`)되어 있습니다. 버전을 바꾸면 `pwa_setup.py`를 다시 실행하고
  manifest 응답·서비스 워커 등록·오프라인 화면을 확인하세요(`pytest tests/test_browser_mobile.py`).

터미널에 나오는 `Network URL`(예: `http://192.168.0.10:8501`)을 직원들에게 공유합니다. 같은 네트워크에 있는 사람은 이 주소로 바로 접속할 수 있습니다.

- MCP 툴은 앱이 자동으로 띄우므로 따로 실행하지 않습니다.
- Windows에서는 처음 실행할 때 방화벽 창에서 "개인 네트워크 허용"을 선택하세요.

첫 화면에서 로그인(회사 코드 · 아이디 · 비밀번호) 또는 회사 등록을 고릅니다. 회사 목록은 화면에 나오지 않습니다.
로그인하면 `og_session` 쿠키(서버가 만든 무작위 토큰, SameSite=Strict, HTTPS면 Secure)로 새로고침해도 로그인이 유지됩니다.
DB에는 토큰의 해시만 저장되며, 30분 무동작·12시간 경과·로그아웃·비밀번호 변경/초기화·계정 비활성화 때 끝납니다.

## 5. AI 앱용 MCP 서버 실행 (선택)

```bash
python remote_server.py          # http://localhost:8000 (PORT 환경변수로 포트 변경)
```

| 주소 | 내용 | 토큰 |
|---|---|---|
| `/` | 안내 홈페이지 (앱별 연결 설정) | 불필요 |
| `/mcp` | MCP 엔드포인트 (Streamable HTTP) | **필요** |
| `/health` | 상태 확인 | 불필요 |

- **토큰은 사원이 직접 발급합니다.** 웹에 로그인해 홈의 **AI 앱 연결** 카드에서 이름을 붙여 발급하고(1인 최대 3개),
  필요 없어지면 폐기합니다. 원문은 발급할 때 한 번만 보입니다. 연결 방법은 [AI 앱 연결 가이드](guide-mcp.md)를 공유하세요.
- 토큰은 DB의 `mcp_tokens` 표에 **SHA-256 해시로만** 저장됩니다. MCP 서버는 요청마다 DB를 확인하므로
  발급·폐기가 **서버 재시작 없이 바로** 반영됩니다. 웹 서비스와 MCP 서버가 같은 `DATABASE_URL`을 써야 합니다.
- 회사 관리자는 **데이터·계정 > AI 앱 연결** 탭에서 사원별 토큰 현황(이름·발급일·마지막 사용일)을 보고 강제 폐기할 수 있습니다.
  발급·폐기·조회는 감사 로그에 남습니다. 계정이 비활성화·잠금되면 그 사원의 토큰도 거부됩니다.
- 사원 홈의 연결 안내에 나오는 주소는 `OFFICEGENIE_MCP_PUBLIC_URL`(예: `https://officegenie-mcp.onrender.com`)입니다.
  비어 있으면 "운영자가 아직 설정하지 않았다"고 표시됩니다.
- 운영자용 CLI (지원·점검용):

  ```bash
  python scripts/issue_token.py bridgesoft EMP001 --name "점검용"   # 발급 (원문은 이때 한 번만 출력)
  python scripts/issue_token.py --list [--all]                      # 목록 (--all: 폐기된 것 포함)
  python scripts/issue_token.py --revoke <토큰 원문>                 # 원문으로 폐기
  python scripts/issue_token.py --revoke-id bridgesoft <token_id>   # 목록의 token_id로 폐기
  ```

> 예전 방식의 `tokens.json`은 더 이상 읽지 않습니다. 남아 있다면 지워도 되며, 사원에게 홈에서 새 토큰을 발급받게 하세요.

## 6. Render 배포하기

웹(`officegenie-web`)과 원격 MCP 서버(`officegenie-mcp`) 두 서비스를 Render 무료 플랜에 올리고, 둘 다 같은 Neon PostgreSQL을 씁니다.
설정은 저장소 루트의 `render.yaml`(Blueprint)에 있습니다.

```
사원 브라우저 ──https──▶ officegenie-web (Streamlit) ─┐
사원 AI 앱   ──https──▶ officegenie-mcp (/mcp)       ─┴──▶ Neon PostgreSQL (DATABASE_URL)
```

> ⚠️ Render 무료 서비스는 재시작하거나 잠들 때 **서버 안의 파일이 사라집니다.** 그래서 모든 상태(회사 데이터, 계정, 세션, 티켓,
> 자료실 파일, MCP 토큰)는 DB에만 저장합니다. `OFFICEGENIE_ENV=production`에서 `DATABASE_URL`이 없거나 SQLite이면
> 서비스가 시작 단계에서 오류로 멈춥니다(로컬 SQLite로 넘어가지 않음).

1. **GitHub 연결**: Render 대시보드 → Account Settings → GitHub에서 이 저장소에 접근을 허용합니다.
2. **Blueprint로 배포**: New → Blueprint → 저장소 선택. `render.yaml`을 읽어 두 서비스가 보입니다.
   - 웹 빌드 `pip install -r requirements.txt && python scripts/pwa_setup.py`, 시작 `python scripts/preflight.py && streamlit run app.py …`
   - MCP 빌드 `pip install -r requirements.txt`, 시작 `python remote_server.py` (Render가 주는 `PORT`, 0.0.0.0)
   - 헬스 체크: 웹 `/_stcore/health`, MCP `/health`. Python 버전은 `PYTHON_VERSION`(= `.python-version`)으로 고정
3. **환경변수 입력**: Blueprint 화면에서 `sync: false` 값을 두 서비스 모두에 넣습니다.

   | 변수 | 값 |
   |---|---|
   | `DATABASE_URL` | Neon **main** 브랜치 연결 주소 (두 서비스 같은 값) |
   | `LLM_API_KEY`, `LLM_BASE_URL` | Jitda 키와 주소 (웹 챗봇이 씀) |
   | `OFFICEGENIE_SIGNUP_CODE` | 회사 등록 가입 코드 (비우면 누구나 등록) |
   | `OFFICEGENIE_MCP_PUBLIC_URL` | 처음에는 비워 둡니다(4단계에서 입력) |

   `OFFICEGENIE_ENV=production`은 `render.yaml`에 이미 들어 있습니다.
4. **첫 배포 후 MCP 주소 설정**: 배포가 끝나면 `officegenie-mcp`의 주소(예: `https://officegenie-mcp.onrender.com`)를 복사해
   **두 서비스의** `OFFICEGENIE_MCP_PUBLIC_URL`에 넣고 다시 배포(Manual Deploy → Deploy latest commit)합니다.
   웹은 사원 홈의 연결 안내에, MCP 서버는 안내 홈페이지에 이 주소를 씁니다.
5. **Neon 데이터 확인**: 내 PC에서 `.env`의 `DATABASE_URL`을 같은 main 브랜치로 두고 실행합니다.

   ```bash
   python scripts/import_company.py --all       # 처음 한 번 (이미 있으면 티켓은 그대로 둠)
   python scripts/list_companies.py             # 회사 코드 확인
   python scripts/set_admin_password.py <company_id>
   ```

6. **깨우기와 점검**:

   ```bash
   python scripts/warmup.py https://officegenie-web.onrender.com https://officegenie-mcp.onrender.com
   python scripts/deploy_check.py https://officegenie-web.onrender.com https://officegenie-mcp.onrender.com
   ```

   `deploy_check.py`는 웹 응답, PWA 파일(manifest·서비스 워커·오프라인 화면), HTTPS, MCP `/health`, 잘못된 토큰 거부를 자동으로
   확인하고, 실제 로그인·토큰 발급이 필요한 항목은 수동 확인 목록으로 출력합니다. 실패가 0개면 종료 코드 0입니다.
7. **회사 관리자에게 웹 주소 공유**: `https://officegenie-web.onrender.com`과 회사 코드를 전달합니다. 사원은 같은 주소에서
   로그인하고, 휴대폰에서는 홈 화면에 추가(PWA)할 수 있습니다.

- **잠들기**: 무료 플랜은 15분 동안 요청이 없으면 잠들고, 깨는 데 최대 1분쯤 걸립니다. 로그인 화면에 안내가 있고,
  시연 전에는 `warmup.py`로 미리 깨워 두세요.
- **프록시**: Render는 HTTPS를 앞단에서 처리하고 앱에는 HTTP로 넘깁니다. 앱은 `X-Forwarded-Proto`/`X-Forwarded-Host`를 보고
  로그인 쿠키에 `Secure`를 붙이고, MCP 안내 홈페이지에 https 주소를 표시합니다.
- **PWA**: 웹 시작 시 `preflight.py`와 앱이 `pwa_setup.py` 적용 여부를 확인하고, 미적용이면 로그에 경고를 남깁니다.
- 운영 화면에서는 `render.yaml`의 `STREAMLIT_CLIENT_SHOW_ERROR_DETAILS=none`, `STREAMLIT_CLIENT_TOOLBAR_MODE=viewer`로
  오류 상세와 개발자 메뉴를 숨깁니다.

## 7. 운영

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
| 기존 DB 업그레이드 | 서버·스크립트가 시작할 때 `init_db()`가 새 테이블(`accounts`, `audit_log`, `resources`, `login_sessions`, `mcp_tokens`)과 컬럼(`company_code`, 티켓 `history`)을 추가하고, 코드가 없는 회사에 회사 코드를 채웁니다. 기존 데이터는 그대로입니다. |
| PWA 설정 확인·되돌리기 | `python scripts/pwa_setup.py --check` / `--undo`. 아이콘을 바꾸려면 `assets/logo.png`를 두고 `python scripts/make_pwa_icons.py` 후 `pwa_setup.py` |
| 새 회사 추가 | 회사 관리자가 웹에서 직접 등록하는 것이 기본입니다. 폴더로 넣을 때는 [다른 회사에 적용하기](architecture.md#-다른-회사에-적용하기) |
| LLM 연결 확인 | `python scripts/cli_chat.py bridgesoft EMP001` (실패하면 `[오류 원인]` 출력) |

## 8. 문제 해결

| 증상 | 원인과 해결 |
|---|---|
| `requirements.txt`를 찾을 수 없음 | zip을 풀면 폴더가 한 겹 더 생깁니다. 안쪽 폴더로 `cd` 하세요. |
| `ModuleNotFoundError: dotenv` / `sqlalchemy` / `psycopg` | `pip install -r requirements.txt`를 다시 실행하세요. |
| 화면에 "사내 시스템 연결이 일시적으로 원활하지 않습니다" | DB 연결 실패입니다. 인터넷과 `.env`의 `DATABASE_URL`을 확인하세요. 급하면 `DATABASE_URL`을 지워 로컬 SQLite로 동작시킬 수 있습니다. |
| 로그인 시 "회사 코드, 아이디 또는 비밀번호가 올바르지 않습니다" | 어느 값이 틀렸는지는 일부러 알려 주지 않습니다. `list_companies.py`로 회사 코드를 확인하세요. 같은 계정으로 5번 연속 틀리면 10분간 잠기며, 회사 관리자가 계정 탭에서 바로 풀 수 있습니다. |
| 관리자 비밀번호 분실 | `python scripts/set_admin_password.py <company_id>` |
| 챗봇이 "불러오지 못했어요"만 표시 | `.env`의 `LLM_API_KEY`, `LLM_BASE_URL`을 확인하세요. `scripts/cli_chat.py`로 실행하면 `[오류 원인]`이 출력됩니다. |
| 다른 PC에서 웹 화면 접속 불가 | Windows 방화벽에서 "개인 네트워크 허용"을 선택하고, 같은 네트워크인지 확인하세요. |
| AI 앱에서 MCP 연결 시 401 | 토큰이 틀렸거나 폐기된 토큰, 또는 계정이 잠금·비활성 상태입니다. 사원이 홈의 AI 앱 연결 카드에서 새로 발급하면 바로 쓸 수 있습니다(재시작 불필요). 웹과 MCP 서버의 `DATABASE_URL`이 같은지도 확인하세요. |
| Render에서 서비스가 바로 종료됨 | 로그에 "DATABASE_URL이 없습니다"가 있으면 환경변수를 입력하세요. 운영 모드에서는 SQLite로 동작하지 않습니다. |
| Render 첫 접속이 1분 가까이 걸림 | 무료 플랜이 잠들어 있던 것입니다. `scripts/warmup.py`로 미리 깨우세요. |
| Windows에서 한글 깨짐 | `chcp 65001` 입력 후 다시 실행하세요. |
| 휴대폰에 "홈 화면에 추가/설치"가 안 나옴 | HTTPS 주소인지, `python scripts/pwa_setup.py --check`가 "적용됨"인지 확인하세요. streamlit을 다시 설치했다면 `pwa_setup.py`를 다시 실행합니다. |
| 새로고침하면 로그인이 풀림 | 30분 무동작·12시간 경과·비밀번호 변경 시에는 정상입니다. 브라우저가 쿠키를 막고 있지 않은지 확인하세요. |

## 9. 설정 값

| 환경 변수 | 기본값 | 설명 |
|---|---|---|
| `LLM_API_KEY` | (필수) | Jitda LLM API 키 |
| `LLM_BASE_URL` | (필수) | Jitda API 주소 (`/v1`로 끝나는 주소) |
| `OFFICEGENIE_LLM_TIMEOUT` | `30` | LLM 응답 대기 시간(초) |
| `DATABASE_URL` | (없으면 `./officegenie.db`) | PostgreSQL(Neon) 주소. 비밀 정보. 운영 모드에서는 필수 |
| `OFFICEGENIE_ENV` | (없음) | `production`이면 `DATABASE_URL` 필수(없거나 SQLite면 시작 실패) |
| `OFFICEGENIE_MCP_PUBLIC_URL` | (없음) | 사원에게 안내할 원격 MCP 공개 주소(예: `https://officegenie-mcp.onrender.com`) |
| `TEST_DATABASE_URL` | (선택) | 테스트 전용 DB(dev 브랜치) 주소. `DATABASE_URL`과 달라야 함 |
| `OFFICEGENIE_SIGNUP_CODE` | (없음 = 누구나 등록) | 회사 등록에 필요한 서비스 가입 코드 |
| `OFFICEGENIE_CACHE_TTL` | `60` | 관리자 반영 후 다른 서버 프로세스가 새 데이터를 확인하는 주기(초) |
| `OFFICEGENIE_LOG_LEVEL` | `INFO` | `DEBUG`로 두면 툴 호출마다 DB 왕복 횟수를 기록 |
| `PORT` / `OFFICEGENIE_PORT` | `8000` | 원격 MCP 서버 포트 (`PORT` 우선, Render가 지정) |

---

[← AI 앱 연결](guide-mcp.md) · 다음 문서: [구조와 보안 →](architecture.md)
