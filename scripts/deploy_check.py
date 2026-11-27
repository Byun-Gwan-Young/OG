"""배포 후 점검을 자동으로 한다. 실제 로그인·토큰 발급이 필요한 항목은 수동 확인 목록으로 안내한다.

    python scripts/deploy_check.py https://officegenie.onrender.com https://officegenie-mcp.onrender.com

자동 점검
- 웹: 첫 화면 응답, Streamlit 헬스 경로, PWA 태그(manifest 링크·서비스 워커 등록)
- PWA 파일: manifest.webmanifest, sw.js, offline.html 응답과 내용
- HTTPS: 두 주소가 https인지(서비스 워커·PWA 설치·로그인 쿠키 Secure의 전제)
- MCP: /health 상태, 안내 홈페이지에 https MCP 주소 표시, 잘못된 토큰·토큰 없음 거부(401)

--allow-http: 로컬 점검용. HTTPS 항목을 실패 대신 경고로 표시한다.
먼저 scripts/warmup.py로 서비스를 깨워 두면 빠르다. 종료 코드: 실패 0개면 0, 아니면 1.
"""
from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scripts.warmup import fetch, mcp_base, print_table, web_base  # noqa: E402

PASS, WARN, FAIL = "통과", "경고", "실패"

MANUAL_CHECKS = [
    "웹 주소에서 회사 코드 + 사번 + 비밀번호로 로그인 → 사원증 홈이 보이는지",
    "로그인한 채로 새로고침 → 로그인이 유지되는지 (개발자 도구 > 쿠키에 Secure 표시)",
    "틀린 비밀번호 5회 → 10분 잠금 안내가 나오는지",
    "사원 홈 'AI 앱 연결'에서 토큰 발급 → 원문이 한 번만 보이고, 안내 명령에 배포 MCP 주소가 들어가는지",
    "발급한 토큰으로 Claude Code 연결 → '점심 식대 한도랑 내 남은 연차 며칠이야?' 답변 확인",
    "토큰 폐기 → 같은 AI 앱에서 바로 401(연결 끊김)이 되는지 (서버 재시작 없이)",
    "관리자(ADMIN) 로그인 → 데이터·계정 > AI 앱 연결 탭에서 사원 토큰 현황·강제 폐기",
    "관리자 직원 화면 미리보기에서 토큰 발급·폐기 버튼이 없는지",
    "챗봇에 질문 → 답변이 오는지 (LLM_API_KEY, LLM_BASE_URL 확인)",
    "휴대폰 브라우저에서 웹 주소 열기 → 홈 화면에 추가(PWA 설치)가 되는지",
]


@dataclass
class Check:
    group: str
    name: str
    result: str
    detail: str = ""


def _status(resp) -> str:
    return f"HTTP {resp.status}" if resp.status else (resp.error or "응답 없음")


def check_https(url: str, label: str, allow_http: bool) -> Check:
    if url.startswith("https://"):
        return Check("HTTPS", label, PASS, url)
    return Check("HTTPS", label, WARN if allow_http else FAIL, "https 주소가 아닙니다")


def check_web(base: str) -> list[Check]:
    checks = []
    home = fetch(base + "/")
    ok = home.status == 200 and "<html" in home.text.lower()
    checks.append(Check("웹", "첫 화면 응답", PASS if ok else FAIL, f"{_status(home)}, {home.seconds:.1f}초"))
    health = fetch(base + "/_stcore/health")
    ok = health.status == 200 and health.text.strip() == "ok"
    checks.append(Check("웹", "헬스 경로 /_stcore/health", PASS if ok else FAIL, _status(health)))
    has_manifest = 'rel="manifest"' in home.text
    has_sw = "serviceWorker" in home.text
    checks.append(Check("PWA", "index.html PWA 태그", PASS if (has_manifest and has_sw) else FAIL,
                        "manifest 링크·서비스 워커 등록" if has_manifest and has_sw
                        else "빌드 명령에 python scripts/pwa_setup.py가 있는지 확인"))

    manifest = fetch(base + "/pwa/manifest.webmanifest")
    try:
        data = json.loads(manifest.text) if manifest.status == 200 else {}
    except ValueError:
        data = {}
    ok = bool(data.get("name") and data.get("start_url") and data.get("icons"))
    checks.append(Check("PWA", "manifest.webmanifest", PASS if ok else FAIL, _status(manifest)))
    sw = fetch(base + "/sw.js")
    ctype = sw.headers.get("Content-Type", "") or sw.headers.get("content-type", "")
    ok = sw.status == 200 and "officegenie-pwa" in sw.text and "javascript" in ctype
    checks.append(Check("PWA", "서비스 워커 sw.js", PASS if ok else FAIL, f"{_status(sw)} {ctype}".strip()))
    offline = fetch(base + "/pwa/offline.html")
    checks.append(Check("PWA", "offline.html", PASS if offline.status == 200 else FAIL, _status(offline)))
    return checks


def check_mcp(base: str, allow_http: bool) -> list[Check]:
    checks = []
    health = fetch(base + "/health")
    try:
        status = json.loads(health.text).get("status") if health.status == 200 else None
    except ValueError:
        status = None
    detail = f"{_status(health)}, status={status}"
    checks.append(Check("MCP", "/health", PASS if status == "ok" else FAIL, detail))

    home = fetch(base + "/")
    expected = base + "/mcp"
    if home.status != 200:
        checks.append(Check("MCP", "안내 홈페이지 주소", FAIL, _status(home)))
    elif expected in home.text and expected.startswith("https://"):
        checks.append(Check("MCP", "안내 홈페이지 주소", PASS, expected))
    elif expected in home.text:
        checks.append(Check("MCP", "안내 홈페이지 주소", WARN if allow_http else FAIL, f"{expected} (https 아님)"))
    else:
        checks.append(Check("MCP", "안내 홈페이지 주소", FAIL,
                            "홈페이지에 이 주소가 없습니다. OFFICEGENIE_MCP_PUBLIC_URL 값을 확인하세요."))

    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
        "protocolVersion": "2025-06-18", "capabilities": {},
        "clientInfo": {"name": "deploy-check", "version": "1"}}}).encode()
    headers = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream"}
    for label, extra in (("잘못된 토큰 거부", {"Authorization": "Bearer og_invalid_deploy_check"}),
                         ("토큰 없음 거부", {})):
        resp = fetch(base + "/mcp", method="POST", headers={**headers, **extra}, data=body)
        checks.append(Check("MCP", label, PASS if resp.status == 401 else FAIL, _status(resp)))
    return checks


def run(web_url: str, mcp_url: str, *, allow_http: bool = False) -> list[Check]:
    web, mcp = web_base(web_url), mcp_base(mcp_url)
    return [
        check_https(web, "웹 주소", allow_http),
        check_https(mcp, "MCP 주소", allow_http),
        *check_web(web),
        *check_mcp(mcp, allow_http),
    ]


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="배포 후 점검")
    p.add_argument("web_url")
    p.add_argument("mcp_url")
    p.add_argument("--allow-http", action="store_true", help="로컬 점검용: HTTPS 항목을 경고로만 표시")
    a = p.parse_args(argv)
    checks = run(a.web_url, a.mcp_url, allow_http=a.allow_http)
    print_table([[c.group, c.name, c.result, c.detail] for c in checks], ["구분", "항목", "결과", "내용"])
    failed = [c for c in checks if c.result == FAIL]
    warned = [c for c in checks if c.result == WARN]
    print(f"\n자동 점검: 통과 {len(checks) - len(failed) - len(warned)}, 경고 {len(warned)}, 실패 {len(failed)}")
    print("\n수동 확인 목록 (실제 로그인·토큰 발급이 필요한 항목)")
    for i, item in enumerate(MANUAL_CHECKS, 1):
        print(f"  [ ] {i}. {item}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
