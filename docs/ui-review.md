[← README](../README.md) · [구조와 보안](architecture.md)

# 🔍 화면 점검 기록: 아이콘 깨짐 · UI 다듬기 · 앱 설치

배포 서버(EC2 + Caddy)에서 아이콘 자리에 `chevron_right`, `keyboard_arrow_down` 같은 글자가 보이고 "더보기"·닫기 버튼 등이
어긋난다는 보고를 받고 원인을 확인한 뒤, 전체 화면을 PC(1440px)·모바일(390px)에서 점검했다.

- 스크린샷: [`images/ui-review/before/`](images/ui-review/before/) → [`images/ui-review/after/`](images/ui-review/after/)
  (같은 파일 이름끼리 비교, `pc-*`·`mobile-*` 화면 34장 + 상황별 추가 캡처)
- 다시 찍기: `python scripts/ui_screenshots.py <웹 주소> --out <폴더> --code <회사 코드> --password <사원 비밀번호> --admin-password <관리자 비밀번호>`
  (Playwright 필요. 화면마다 아이콘 이름 글자·가로 넘침을 검사해 `report.json`에 남기고, 문제가 있으면 종료 코드 1)

## 아이콘 깨짐 원인

| 확인한 것 | 결과 |
|---|---|
| 앱 CSS가 아이콘 글꼴을 덮어쓰는지 | 아니었다. `*`·`span`에 `font-family !important`를 거는 규칙이 없고, 로컬에서 아이콘 요소의 글꼴은 모두 `Material Symbols Rounded` |
| 서비스 워커가 옛 파일·폰트를 캐시하는지 | 아니었다. 아이콘·offline.html만 미리 저장하고 폰트·JS는 가로채지 않음 |
| 이미지의 streamlit 버전과 `pwa_setup.py` 대상 | 같았다. `requirements.txt`에 `streamlit==1.67.0` 고정, 이미지 빌드에서 `pwa_setup.py --check` 통과, 배포 서버 `index.html`이 로컬과 같음 |
| 폰트 파일 응답 | 배포 서버 `/static/media/MaterialSymbols-Rounded.*.woff2` → 200, `font/woff2`, 정상 크기 |
| **Caddy의 HTTP/3 광고** | **원인.** Caddy가 기본으로 `Alt-Svc: h3=":443"; ma=2592000`(30일)을 보내는데, 보안 그룹은 TCP 80/443만 열어 UDP 443(HTTP/3)이 막혀 있다. HTTP/3를 강제한 브라우저로 접속하면 `ERR_QUIC_PROTOCOL_ERROR`. 첫 화면은 TCP로 받지만 그 뒤에 따로 받는 **아이콘 폰트** 요청이 막힌 HTTP/3로 가다 실패·지연되고, Streamlit의 `font-display: block`이 3초 뒤 아이콘 이름 글자를 그대로 보여 준다 |

같은 증상을 로컬에서 폰트 요청을 막아 재현했다: [`before/pc-home-icon-font-failed.png`](images/ui-review/before/pc-home-icon-font-failed.png)
("더보기"와 `chevron_right` 겹침, 폐기 버튼의 `expand_more`, 펼치기의 `keyboard_arrow_down`).
고친 뒤 같은 조건: [`after/pc-home-icon-font-failed.png`](images/ui-review/after/pc-home-icon-font-failed.png) (이름 글자 없음, 겹침 없음).

## 발견한 문제와 수정

| 화면 | 문제 | 수정 |
|---|---|---|
| 전체(배포) | 아이콘 자리에 `chevron_right`·`keyboard_arrow_down`·`expand_more` 글자, "더보기"와 겹침 | Caddy HTTP/3 끄기(`servers { protocols h1 h2 }`), 브라우저가 기억한 HTTP/3 지우기(`Alt-Svc: clear`), compose의 `443/udp` 제거 |
| 전체 | 아이콘 글꼴이 늦게 오면 이름 글자가 잠깐 보일 수 있음 | `pwa_setup.py`가 `index.html`에 아이콘 폰트 `preload`를 넣어 화면 코드보다 먼저 받기 시작 |
| 전체 | 글꼴을 끝내 받지 못하면 긴 이름 글자가 옆 요소를 밀어냄 | 아이콘 요소에 Material 글꼴을 `!important`로 고정하고 폭을 1글자로 묶음. `install.js`가 글꼴 실패를 감지하면 이름 글자를 투명하게(자리만 유지) |
| 전체(PWA) | 서비스 워커 캐시 이름이 `v1` 고정이라 배포가 바뀌어도 옛 캐시가 남을 수 있음 | 캐시 이름에 빌드 버전(PWA 파일 + streamlit 버전 해시)을 넣고 activate에서 옛 `officegenie-*` 캐시 삭제. HTML·JS는 계속 가로채지 않음(항상 네트워크) |
| 챗봇 | 닫기 버튼이 CSS로 그린 `×` 글자(글꼴에 따라 굵기·위치가 다름) | Material `close` 아이콘 버튼으로 바꾸고 "닫기"는 스크린리더용으로만 남김 |
| 로그인·회사 등록 | "← 뒤로가기"의 화살표가 유니코드 글자 | `arrow_back` 아이콘 + "뒤로가기" |
| 로그인 | 성공 표시 "✓ 로그인 성공"이 유니코드 체크 | `check` 아이콘 + "로그인 성공" |
| 관리자 사원·공지·자료실 | "＋ 사원 추가", "＋ 공지 작성", "＋ 자료 등록"이 전각 ＋ 글자 | `add` 아이콘 + 글자로 통일. 빈 화면 안내 문구도 맞춤 |
| 관리자 사내 규정 | "← 규정 목록" 유니코드 화살표 | `arrow_back` 아이콘 |
| 관리자 사원 목록 | "수정" 버튼이 같은 행의 글자보다 위로 떠 보임 | 표 행 안 마크다운의 음수 아래 여백을 없애 세로 가운데를 맞춤 |
| 관리자 사원 목록(모바일) | 표가 옆으로 잘려 "부서"까지만 보이는데 밀 수 있다는 단서가 없음 | 모바일에서만 "표를 옆으로 밀면 나머지 칸과 관리 버튼이 보입니다" 안내 |
| 관리자 데이터·계정 | 파일을 고르기 전 "미리보기" 버튼이 파란 바탕에 파란 글자로 거의 안 보임 | 사용할 수 없는 기본 버튼을 회색 바탕·회색 글자로 통일 |
| 관리자 사원 목록·AI 앱 연결 카드 | 검색 칸·셀렉트·토큰 이름 칸이 베이지 바탕이라 로그인 입력 칸(흰 바탕)과 다름 | 입력 칸·셀렉트·텍스트 영역을 흰 바탕 + 같은 테두리로 통일 |
| 사원 홈 AI 앱 연결 | "마지막 사용 사용 기록 없음"(같은 말 반복) | "마지막 사용 없음" |
| 사원 홈 | 위젯과 AI 앱 연결 카드 사이 간격이 다른 카드 간격의 두 배 | 카드 사이 간격을 20px로 맞춤 |
| 전체 | Streamlit 기본 장식(상단 색 띠·Deploy·메인 메뉴)이 보일 여지 | `stDecoration`·`stAppDeployButton`·`stMainMenu`도 숨김(헤더·툴바·footer는 이미 숨김) |
| 사원 홈 | 앱 설치 방법이 화면에 없음 | "앱으로 설치하기" 카드 추가(설치 창 또는 아이폰 안내, 이미 앱으로 실행 중이면 숨김) |
| 전체 | 설치 버튼이 없음 | 크롬·엣지: 왼쪽 아래 "앱 설치" 버튼(`beforeinstallprompt`), 아이폰 Safari: "공유 → 홈 화면에 추가" 안내(닫으면 기억). 로그인·등록 입력 화면에서는 띄우지 않고, 모바일에서는 챗봇 버튼·탭바를 가리지 않는 위치 |

선택자는 버전 따라 바뀌는 `st-emotion-cache-*` 대신 `data-testid`와 `st-key-*`(코드에서 정한 key)만 썼다.
기능(로그인, 신청 확정, 업로드, 토큰 발급·폐기)은 바꾸지 않았다.

## 확인 결과

- `scripts/ui_screenshots.py`: 수정 전·후 34개 화면 모두 아이콘 이름 글자·가로 넘침 0건(로컬은 원래 글꼴이 정상이라 수정 전에도 0건).
  배포 증상은 폰트 요청을 막은 조건으로 재현·확인했다.
- 설치 상태 캡처: [`after/mobile-install-button.png`](images/ui-review/after/mobile-install-button.png)(크롬 설치 버튼),
  [`after/mobile-iphone-install-hint.png`](images/ui-review/after/mobile-iphone-install-hint.png)(아이폰 안내)
- 자동 테스트: `tests/test_browser_install.py`(설치 버튼·아이폰 안내·홈 카드·아이콘 이름 글자·글꼴 실패),
  `tests/test_pwa.py`(빌드 버전·설치 스크립트·preload), `tests/test_deploy_config.py`(Caddy HTTP/3 끔, UDP 포트 없음)
- 로컬에 Docker가 없어 이미지 빌드·compose 실행으로 찍지는 못했다. 같은 streamlit 버전·운영 환경 변수
  (`STREAMLIT_CLIENT_TOOLBAR_MODE=viewer` 등)로 띄운 로컬 서버에서 찍었다.
