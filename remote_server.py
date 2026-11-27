"""
오피스지니 원격 MCP 서버 (홈페이지 + /mcp 엔드포인트)
=====================================================

다른 사람이 자기 AI 앱(Claude Code, Cursor, VS Code 등)에서 주소로 접속해 쓰는 버전.
툴 로직은 mcp_server.py를 그대로 재사용하고, 신원만 '접속 토큰'에서 가져온다.

    http://<주소>:8000/        → 소개·연결 방법 홈페이지
    http://<주소>:8000/mcp     → MCP 엔드포인트 (Streamable HTTP, 토큰 필요)
    http://<주소>:8000/health  → 상태 확인

실행:
    python remote_server.py                     # 서버 시작 (PORT 환경변수, 기본 8000)

토큰은 사원이 웹 홈의 "AI 앱 연결" 카드에서 직접 발급한다(운영자 CLI: scripts/issue_token.py).
토큰은 DB(mcp_tokens)에 해시로만 저장되고 요청마다 DB를 조회하므로 발급·폐기가 재시작 없이 반영된다.

[stdio 버전과의 차이]
- 툴 인자에 company_id / employee_id가 없다. 서버가 토큰을 보고 "누구인지" 정한다.
  → 접속한 사람이 다른 사번을 넣을 방법 자체가 없다.
- 비품 신청은 2단계 툴로 나뉜다: create_support_ticket(미리보기) → confirm_support_ticket(접수).
"""

from __future__ import annotations

import asyncio
import html
import json
import logging
import os
from typing import Literal

from fastmcp import FastMCP
from fastmcp.server.auth import AccessToken, TokenVerifier
from fastmcp.server.dependencies import get_access_token
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse

import mcp_server as core

log = logging.getLogger("officegenie.remote")

HOST = os.environ.get("OFFICEGENIE_HOST", "0.0.0.0")
# Render 등 호스팅은 PORT를 정해 준다. 로컬 실행·테스트는 OFFICEGENIE_PORT도 받는다.
PORT = int(os.environ.get("PORT") or os.environ.get("OFFICEGENIE_PORT") or "8000")


class DbTokenVerifier(TokenVerifier):
    """요청마다 DB(mcp_tokens)를 조회해 토큰을 검증한다. 메모리에 토큰 목록을 두지 않는다."""

    async def verify_token(self, token: str) -> AccessToken | None:
        try:
            owner = await asyncio.to_thread(core.storage.verify_mcp_token, token)
        except core.storage.StorageUnavailable:
            log.warning("token check failed: DB unavailable")
            return None
        if owner is None:
            return None
        return AccessToken(
            token=token,
            client_id=f"{owner['company_id']}:{owner['employee_id']}",
            scopes=[],
            claims={**owner},
        )


mcp = FastMCP(
    name="OfficeGenie",
    instructions=(
        "중소기업 신규 입사자용 온보딩 MCP 서버. 접속 토큰의 주인(회사·사번) 기준으로 "
        "사내 규정, 본인 인사 정보, 사내 장소, 소속 부서 인수인계서, 비품·계정 신청을 제공한다. "
        "툴 결과의 ok가 false이면 message를 그대로 전달하고 추측하지 않는다. "
        "규정 답변은 핵심 요약 / 상세 기준 / 근거 조항(citation) 순서로 쓴다."
    ),
    website_url="https://github.com/",  # 저장소 주소로 바꾸세요
    auth=DbTokenVerifier(),
)


def _me() -> tuple[str, str]:
    """현재 요청의 토큰에서 (company_id, employee_id)를 꺼낸다."""
    token = get_access_token()
    if token is None:
        raise PermissionError("인증 토큰이 없습니다.")
    claims = token.claims or {}
    return claims["company_id"], claims["employee_id"]


# ---------------------------------------------------------------------------
# 원격 공개 툴 (회사·사번 인자 없음)
# ---------------------------------------------------------------------------

@mcp.tool
def search_policy(query: str) -> dict:
    """사내 규정(복리후생·비품·계정·재택·회의실·취업규칙 등)을 검색한다.

    query: 규정 주제를 짧게 (예: "점심 식대 한도", "재택근무 신청").
    is_reference_only가 true인 조항은 회사 자체 규정이 아닌 표준취업규칙 참고 조문임을 밝힌다.
    """
    cid, eid = _me()
    return core.search_policy(query=query, company_id=cid, employee_id=eid)


@mcp.tool
def get_employee_info(target: str | None = None) -> dict:
    """접속한 사용자 '본인'의 이름·부서·직급·잔여 연차를 조회한다.

    target: 사용자가 다른 사람(사번 또는 이름)의 정보를 물었을 때만 그 값을 넣는다. 서버가 차단한다.
    """
    cid, eid = _me()
    return core.get_employee_info(company_id=cid, employee_id=eid, target=target)


@mcp.tool
def get_office_location(place: str) -> dict:
    """사옥 안 장소(회의실, 복합기, 휴게실, 부서 자리 등)의 건물·층·구역을 조회한다.

    place: 장소 이름만 짧게 (예: "대회의실"). 등록되지 않은 장소는 NOT_FOUND. 위치를 추측하지 말 것.
    """
    cid, eid = _me()
    return core.get_office_location(place=place, company_id=cid, employee_id=eid)


@mcp.tool
def get_handover_doc(department: str | None = None) -> dict:
    """사용자 소속 부서의 신입사원 인수인계서를 조회한다.

    department: 사용자가 특정 부서를 직접 언급했을 때만 넣는다. 타 부서는 서버가 차단한다.
    답변은 핵심 담당 업무 / 사용 툴 / 1주 차 할 일 순서로 요약한다.
    """
    cid, eid = _me()
    return core.get_handover_doc(company_id=cid, employee_id=eid, department=department)


@mcp.tool
def create_support_ticket(item: str, quantity: int = 1, purpose: str = "",
                          request_type: Literal["비품", "계정"] = "비품") -> dict:
    """[1단계] 비품·계정 신청 요약을 만든다. 이 툴은 티켓을 발급하지 않는다.

    결과가 NEEDS_CONFIRMATION이면 요약(품목, 수량, 목적, 경고)을 사용자에게 보여주고 접수할지 묻는다.
    사용자가 명확히 동의한 경우에만 같은 값으로 confirm_support_ticket을 호출한다.
    REJECTED이면 message를 전달한다 (허용 품목이 아니거나 1인 수량 한도 초과).
    """
    cid, eid = _me()
    return core.create_support_ticket(item=item, company_id=cid, employee_id=eid, quantity=quantity,
                                      purpose=purpose, request_type=request_type, confirmed=False)


@mcp.tool
def confirm_support_ticket(item: str, quantity: int = 1, purpose: str = "",
                           request_type: Literal["비품", "계정"] = "비품") -> dict:
    """[2단계] 사용자가 신청 요약을 보고 '접수'에 동의한 뒤에만 호출한다. 티켓(REQ-xxx)을 발급한다.

    사용자 동의 없이 호출하지 말 것. 서버가 허용 품목·수량 한도를 다시 검증한다.
    """
    cid, eid = _me()
    return core.create_support_ticket(item=item, company_id=cid, employee_id=eid, quantity=quantity,
                                      purpose=purpose, request_type=request_type, confirmed=True)


@mcp.tool
def get_my_tickets() -> dict:
    """본인의 비품·계정 신청 현황(티켓 번호, 품목, 상태)을 최신순으로 조회한다."""
    cid, eid = _me()
    return core.get_my_tickets(company_id=cid, employee_id=eid)


# ---------------------------------------------------------------------------
# 홈페이지 (토큰 없이 열림)
# ---------------------------------------------------------------------------

TOOLS_DOC = [
    ("search_policy", "📘", "사내 규정 검색", "회사 내규 우선, 근거 조항 포함"),
    ("get_employee_info", "👤", "본인 인사 정보", "부서 · 직급 · 잔여 연차"),
    ("get_office_location", "📍", "사내 장소 안내", "건물 · 층 · 구역, 별칭 인식"),
    ("get_handover_doc", "📂", "인수인계서", "소속 부서 문서만"),
    ("create_support_ticket", "🎫", "비품 · 계정 신청 요약", "허용 품목 · 수량 한도 검증"),
    ("confirm_support_ticket", "✅", "신청 확정", "사용자 동의 후 티켓 발급"),
    ("get_my_tickets", "📋", "내 신청 현황", "티켓 번호와 처리 상태"),
]


def render_home(base: str) -> str:
    url = f"{base}/mcp"
    e = html.escape
    tools = "".join(
        f'<div class="tool"><div class="ico">{i}</div><div><div class="tn">{e(t)}</div>'
        f'<code>{e(n)}</code><div class="td">{e(d)}</div></div></div>'
        for n, i, t, d in TOOLS_DOC
    )
    claude_code = f'claude mcp add --transport http officegenie {url} \\\n  --header "Authorization: Bearer 발급받은_토큰"'
    json_cfg = json.dumps({"mcpServers": {"officegenie": {
        "url": url, "headers": {"Authorization": "Bearer 발급받은_토큰"}}}}, ensure_ascii=False, indent=2)
    vscode_cfg = json.dumps({"servers": {"officegenie": {
        "type": "http", "url": url, "headers": {"Authorization": "Bearer 발급받은_토큰"}}}},
        ensure_ascii=False, indent=2)
    return f"""<!DOCTYPE html>
<html lang="ko"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>오피스지니 MCP 서버</title>
<style>
:root{{--bg:#0b1020;--card:#121a33;--line:#24305a;--fg:#e8ecff;--mut:#9aa6d1;--acc:#7c9cff;--acc2:#5eead4}}
*{{box-sizing:border-box}}body{{margin:0;font-family:-apple-system,"Pretendard","Apple SD Gothic Neo","Malgun Gothic",sans-serif;
background:radial-gradient(1200px 600px at 10% -10%,#1d2b66 0,transparent 60%),var(--bg);color:var(--fg);line-height:1.6}}
.wrap{{max-width:980px;margin:0 auto;padding:56px 24px 80px}}
.badge{{display:inline-flex;gap:8px;align-items:center;font-size:13px;color:var(--acc2);border:1px solid #1f4d4a;
background:#0e2a2a;padding:4px 12px;border-radius:999px}}.dot{{width:8px;height:8px;border-radius:50%;background:var(--acc2)}}
h1{{font-size:44px;line-height:1.2;margin:18px 0 12px;letter-spacing:-.02em}}h1 span{{color:var(--acc)}}
.lead{{color:var(--mut);font-size:18px;max-width:680px}}
.url{{margin:28px 0 8px;display:flex;gap:8px;flex-wrap:wrap}}
.url code{{flex:1;min-width:260px;background:#070b18;border:1px solid var(--line);padding:14px 16px;border-radius:12px;font-size:15px;color:var(--acc2)}}
button{{background:var(--acc);color:#0b1020;border:0;border-radius:12px;padding:0 18px;font-weight:700;cursor:pointer;font-size:14px}}
h2{{margin:56px 0 16px;font-size:24px}}.mut{{color:var(--mut)}}
.grid{{display:grid;grid-template-columns:repeat(auto-fill,minmax(280px,1fr));gap:12px}}
.tool{{display:flex;gap:12px;background:var(--card);border:1px solid var(--line);border-radius:14px;padding:16px}}
.ico{{font-size:22px}}.tn{{font-weight:700}}.td{{color:var(--mut);font-size:14px}}
.tool code{{font-size:12px;color:var(--acc)}}
.tabs{{display:flex;gap:6px;margin-bottom:-1px}}.tab{{background:transparent;color:var(--mut);border:1px solid var(--line);
border-bottom:0;border-radius:10px 10px 0 0;padding:8px 14px}}.tab.on{{background:var(--card);color:var(--fg)}}
pre{{margin:0;background:var(--card);border:1px solid var(--line);border-radius:0 12px 12px 12px;padding:18px;overflow:auto;font-size:13px}}
.panel{{display:none}}.panel.on{{display:block}}
.steps{{counter-reset:s;list-style:none;padding:0;display:grid;gap:10px}}
.steps li{{counter-increment:s;background:var(--card);border:1px solid var(--line);border-radius:12px;padding:14px 16px 14px 56px;position:relative}}
.steps li:before{{content:counter(s);position:absolute;left:16px;top:13px;width:26px;height:26px;border-radius:50%;
background:var(--acc);color:#0b1020;font-weight:800;display:grid;place-items:center;font-size:14px}}
.co{{list-style:none;padding:0;display:flex;gap:10px;flex-wrap:wrap}}.co li{{background:var(--card);border:1px solid var(--line);
border-radius:12px;padding:10px 14px;display:flex;gap:10px;align-items:center}}.co code{{color:var(--mut);font-size:12px}}
.sec{{display:grid;grid-template-columns:repeat(auto-fill,minmax(220px,1fr));gap:12px}}
.sec div{{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:14px;font-size:14px}}
.sec b{{display:block;margin-bottom:4px}}
footer{{margin-top:64px;color:var(--mut);font-size:13px;border-top:1px solid var(--line);padding-top:20px}}
</style></head><body><div class="wrap">
<span class="badge"><span class="dot"></span>MCP 서버 운영 중 · Streamable HTTP</span>
<h1>🧞 오피스지니<br><span>온보딩 MCP 서버</span></h1>
<p class="lead">신입사원의 "식대 얼마야?", "연차 며칠 남았어?", "회의실 어디야?"를 사내 데이터 근거로만 답합니다.
쓰고 있는 AI 앱에 아래 주소를 추가하면 바로 연결됩니다.</p>
<div class="url"><code id="u">{e(url)}</code><button onclick="navigator.clipboard.writeText(document.getElementById('u').textContent);this.textContent='복사됨'">주소 복사</button></div>
<p class="mut" style="font-size:14px">접속에는 개인 토큰이 필요합니다. 토큰은 사람마다 하나이며, 토큰의 주인(회사 · 사번) 기준으로만 정보가 조회됩니다.</p>

<h2>연결하기</h2>
<ol class="steps">
<li>운영자에게 <b>개인 토큰</b>을 받습니다.</li>
<li>쓰는 AI 앱에 아래 설정을 추가하고, <code>발급받은_토큰</code>을 받은 토큰으로 바꿉니다.</li>
<li>"점심 식대 한도랑 내 남은 연차 며칠이야?"라고 물어보세요.</li>
</ol>
<div class="tabs" style="margin-top:20px">
<button class="tab on" data-p="p1">Claude Code</button><button class="tab" data-p="p2">Cursor · 기타</button><button class="tab" data-p="p3">VS Code</button></div>
<div id="p1" class="panel on"><pre>{e(claude_code)}</pre></div>
<div id="p2" class="panel"><pre>{e(json_cfg)}</pre></div>
<div id="p3" class="panel"><pre>{e(vscode_cfg)}</pre></div>

<h2>제공하는 툴</h2><div class="grid">{tools}</div>

<p class="mut" style="font-size:14px">회사 목록은 공개하지 않습니다. 토큰이 가리키는 회사의 데이터만 조회됩니다.</p>

<h2>안전장치</h2><div class="sec">
<div><b>🔑 토큰 = 신원</b>툴 인자에 사번이 없습니다. 다른 사람 정보를 지정할 방법 자체가 없습니다.</div>
<div><b>🛡 서버가 판단</b>타인 정보 · 타 부서 문서는 서버에서 차단됩니다.</div>
<div><b>🚫 추측 금지</b>등록되지 않은 장소 · 규정은 "없음"으로 응답합니다.</div>
<div><b>✅ 2단계 신청</b>요약 확인 후 확정해야 티켓이 발급됩니다.</div>
</div>
<footer>OfficeGenie · FastMCP · 상태 확인 <a href="/health" style="color:var(--acc)">/health</a></footer>
</div>
<script>
document.querySelectorAll('.tab').forEach(b=>b.onclick=()=>{{
document.querySelectorAll('.tab').forEach(x=>x.classList.toggle('on',x===b));
document.querySelectorAll('.panel').forEach(p=>p.classList.toggle('on',p.id===b.dataset.p));}});
</script></body></html>"""


def _first_header(request: Request, name: str) -> str:
    # 프록시를 여러 번 거치면 "https, http"처럼 쉼표로 이어진다. 맨 앞이 사용자 쪽 값이다.
    return request.headers.get(name, "").split(",")[0].strip()


def _base_url(request: Request) -> str:
    """안내 홈페이지에 보여 줄 공개 주소. Render 프록시 뒤에서는 x-forwarded-*를 따른다.

    OFFICEGENIE_MCP_PUBLIC_URL이 있으면 그 값을 우선한다(끝의 /mcp는 떼고 쓴다).
    """
    public = os.environ.get("OFFICEGENIE_MCP_PUBLIC_URL", "").strip().rstrip("/")
    if public.startswith(("https://", "http://")):
        return public.removesuffix("/mcp")
    proto = _first_header(request, "x-forwarded-proto").lower()
    if proto not in ("http", "https"):
        proto = request.url.scheme
    host = (_first_header(request, "x-forwarded-host")
            or request.headers.get("host") or f"localhost:{PORT}")
    return f"{proto}://{host}"


@mcp.custom_route("/", methods=["GET"])
async def home(request: Request) -> HTMLResponse:
    return HTMLResponse(render_home(_base_url(request)))


@mcp.custom_route("/health", methods=["GET"])
async def health(request: Request) -> JSONResponse:
    try:
        company_count = len(core.list_company_ids())
        status = "ok"
    except core.storage.StorageUnavailable:
        company_count, status = 0, "db_unavailable"
    # 회사 ID 목록은 공개하지 않는다(멀티테넌트). 개수만 알린다.
    # DB가 잠시 느려도 헬스 체크 실패로 재시작 반복이 생기지 않도록 상태는 본문으로만 알린다.
    return JSONResponse({"status": status, "company_count": company_count})


if __name__ == "__main__":
    core.configure_logging()
    try:
        core.storage.check_runtime_config()
    except core.storage.ConfigurationError as exc:
        log.critical("%s", exc)
        raise SystemExit(2) from None
    core.prepare_database()
    print(f"홈페이지: http://localhost:{PORT}/   MCP: http://localhost:{PORT}/mcp")
    mcp.run(transport="http", host=HOST, port=PORT, show_banner=False)
