"""배포한 웹·MCP 서비스가 응답하는지 확인하고 응답 시간·상태를 표로 출력한다.

    python scripts/warmup.py https://officegenie.example.com https://mcp.officegenie.example.com

- 배포·재시작 직후에는 컨테이너가 준비될 때까지 잠시 걸릴 수 있다.
- 두 서비스에 동시에 요청을 보내고, 응답이 올 때까지(기본 최대 120초) 다시 시도한다.
- MCP 주소는 끝에 /mcp가 있어도 된다. 표준 라이브러리만 쓴다.
- 모두 정상이면 종료 코드 0, 하나라도 실패하면 1.
"""
from __future__ import annotations

import argparse
import sys
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass

USER_AGENT = "officegenie-deploy-tools/1.0"


@dataclass
class Response:
    status: int | None
    body: bytes
    headers: dict
    seconds: float
    error: str = ""

    @property
    def text(self) -> str:
        return self.body.decode("utf-8", "replace")


def fetch(url: str, *, method: str = "GET", headers: dict | None = None,
          data: bytes | None = None, timeout: float = 70) -> Response:
    """HTTP 요청 한 번. 오류 응답(4xx/5xx)도 Response로 돌려준다."""
    req = urllib.request.Request(url, data=data, method=method,
                                 headers={"User-Agent": USER_AGENT, **(headers or {})})
    started = time.monotonic()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return Response(resp.status, resp.read(), dict(resp.headers), time.monotonic() - started)
    except urllib.error.HTTPError as exc:
        return Response(exc.code, exc.read() or b"", dict(exc.headers or {}), time.monotonic() - started)
    except Exception as exc:  # 연결 실패·시간 초과
        return Response(None, b"", {}, time.monotonic() - started, type(exc).__name__)


def web_base(url: str) -> str:
    return url.strip().rstrip("/")


def mcp_base(url: str) -> str:
    return url.strip().rstrip("/").removesuffix("/mcp")


@dataclass
class WakeResult:
    name: str
    url: str
    ok: bool
    status: str
    first_seconds: float
    total_seconds: float
    attempts: int


def wake(name: str, url: str, check, deadline_seconds: float) -> WakeResult:
    """url이 정상 응답할 때까지 다시 시도한다. check(Response) -> bool."""
    started = time.monotonic()
    attempts, first = 0, None
    last = None
    while True:
        attempts += 1
        remaining = max(5.0, deadline_seconds - (time.monotonic() - started))
        last = fetch(url, timeout=min(70.0, remaining))
        if first is None:
            first = last.seconds
        if check(last):
            break
        if time.monotonic() - started >= deadline_seconds:
            break
        time.sleep(3)
    status = str(last.status) if last.status else (last.error or "응답 없음")
    return WakeResult(name, url, check(last), status, first or 0.0,
                      time.monotonic() - started, attempts)


def _health_ok(resp: Response) -> bool:
    return resp.status == 200


def _mcp_health_ok(resp: Response) -> bool:
    return resp.status == 200 and '"status":"ok"' in resp.text.replace(" ", "")


def print_table(rows: list[list[str]], headers: list[str]) -> None:
    import unicodedata

    def width(text: str) -> int:
        return sum(2 if unicodedata.east_asian_width(ch) in "WF" else 1 for ch in text)

    widths = [max(width(h), *(width(r[i]) for r in rows)) for i, h in enumerate(headers)]

    def line(cells: list[str]) -> str:
        return "  ".join(c + " " * (w - width(c)) for c, w in zip(cells, widths))

    print(line(headers))
    print("  ".join("-" * w for w in widths))
    for row in rows:
        print(line(row))


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="배포 서비스 응답 확인")
    p.add_argument("web_url", help="웹 주소 (예: https://officegenie.example.com)")
    p.add_argument("mcp_url", help="MCP 주소 (예: https://mcp.officegenie.example.com 또는 …/mcp)")
    p.add_argument("--timeout", type=float, default=120, help="서비스별 최대 대기 시간(초)")
    a = p.parse_args(argv)

    targets = [
        ("웹", web_base(a.web_url) + "/_stcore/health", _health_ok),
        ("웹 첫 화면", web_base(a.web_url) + "/", _health_ok),
        ("MCP", mcp_base(a.mcp_url) + "/health", _mcp_health_ok),
    ]
    print(f"서비스 응답을 확인하는 중입니다. 재시작 직후라면 조금 걸릴 수 있습니다 (최대 {a.timeout:.0f}초 대기).")
    with ThreadPoolExecutor(max_workers=len(targets)) as pool:
        results = list(pool.map(lambda t: wake(t[0], t[1], t[2], a.timeout), targets))
    print_table(
        [[r.name, r.url, "정상" if r.ok else "실패", r.status,
          f"{r.first_seconds:.1f}초", f"{r.total_seconds:.1f}초", str(r.attempts)] for r in results],
        ["서비스", "주소", "결과", "HTTP", "첫 응답", "정상까지", "시도"],
    )
    if all(r.ok for r in results):
        print("모두 정상 응답합니다.")
        return 0
    print("응답하지 않는 서비스가 있습니다. 서버에서 deploy/logs.sh 로 로그를 확인하세요.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
