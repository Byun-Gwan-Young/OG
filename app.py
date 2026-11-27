from __future__ import annotations

import base64
import csv
import hashlib
import html
import io
import json
import logging
import re
import time
import traceback
from concurrent.futures import Future, ThreadPoolExecutor
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from zoneinfo import ZoneInfo

import streamlit as st
from dotenv import load_dotenv
from streamlit.runtime.scriptrunner_utils.exceptions import (
    RerunException,
    StopException,
)

PROJECT_ROOT = Path(__file__).resolve().parent
load_dotenv(PROJECT_ROOT / ".env")

from agent import OfficeGenieAgent

log = logging.getLogger("officegenie.app")
POST_LOGIN_ERROR = (
    "화면을 불러오는 중 문제가 발생했습니다. 잠시 후 다시 시도하거나 다시 로그인해 주세요."
)
CONNECTION_ERROR = "사내 시스템 연결이 일시적으로 원활하지 않습니다."
_SAFE_MESSAGE_ERRORS = (KeyError, IndexError, AttributeError, TypeError, ValueError)

_PWA_ICON = PROJECT_ROOT / "pwa" / "icon-192.png"
st.set_page_config(
    page_title="오피스 지니 | 신입사원 온보딩",
    page_icon=str(_PWA_ICON) if _PWA_ICON.is_file() else None,
    layout="wide",
)
PWA_MARKER = "<!-- officegenie-pwa:start -->"


@st.cache_resource
def _check_pwa_setup() -> bool:
    """서버 시작 시 한 번: PWA 태그가 Streamlit 정적 폴더에 들어갔는지 확인한다(화면 오류는 아님)."""
    static_dir = Path(st.__file__).resolve().parent / "static"
    try:
        applied = PWA_MARKER in (static_dir / "index.html").read_text("utf-8") and (static_dir / "sw.js").is_file()
    except OSError:
        applied = False
    if not applied:
        log.warning("PWA 설정이 적용되지 않았습니다. python scripts/pwa_setup.py 실행 필요")
    return applied

WELCOME_TEXT = "안녕하세요. 온보딩이나 비품 지급에 대해 궁금한 점을 물어보세요."
SUGGESTED_QUESTION = "제 모니터는 언제 받을 수 있나요?"
TEMPLATE_DIR = PROJECT_ROOT / "templates"
UPLOAD_MAX_BYTES = 2 * 1024 * 1024
DATA_KINDS = (
    ("policies", "규정"),
    ("employees", "사원"),
    ("locations", "장소"),
    ("handovers", "인수인계서"),
    ("tickets", "비품·신청 규칙"),
)
_company_code_copy = st.components.v2.component(
    "officegenie_company_code_copy",
    html="""
      <label for="settings-company-code">회사 코드 · 읽기 전용</label>
      <div class="code-copy-row">
        <input id="settings-company-code" type="text" readonly>
        <button type="button">복사</button>
      </div>
      <span role="status" class="code-copy-status"></span>
    """,
    css="""
      label { display: block; font-size: 14px; color: #526885; margin-bottom: 8px; }
      .code-copy-row { display: flex; gap: 12px; }
      input, button { box-sizing: border-box; font: inherit; min-height: 48px; border-radius: 10px; }
      input { flex: 1; min-width: 0; padding: 0 14px; border: 1px solid #D8E2F1; background: #F3F6F9; color: #202D40; }
      button { padding-inline: clamp(18px, 3vw, 44px); border: 0; background: #3868BE; color: #fff; cursor: pointer; }
      button:hover { background: #2B539E; }
      .code-copy-status:not(:empty) { display: block; margin-top: 6px; font-size: 13px; color: #395987; }
    """,
    js="""
      export default function ({parentElement, data}) {
        const field = parentElement.querySelector("input");
        const button = parentElement.querySelector("button");
        const status = parentElement.querySelector("[role=status]");
        field.value = data.code;
        button.onclick = async () => {
          try {
            await navigator.clipboard.writeText(field.value);
            status.textContent = "복사했습니다.";
          } catch {
            field.select();
            status.textContent = document.execCommand("copy")
              ? "복사했습니다." : "선택된 회사 코드를 직접 복사해 주세요.";
          }
        };
      }
    """,
)
SESSION_TIMEOUT_SEC = 30 * 60  # 30분 무동작 시 자동 로그아웃
# 새로고침 후 로그인 유지: 쿠키에는 서버가 만든 무작위 토큰만 둔다(DB에는 해시만 저장).
SESSION_COOKIE = "og_session"
SESSION_COOKIE_MAX_AGE = 12 * 60 * 60
SESSION_KEEPALIVE_SEC = 5 * 60
_TOKEN_RE = re.compile(r"[A-Za-z0-9_-]{20,100}")
CHAT_FAILURE_CODES = {
    "FALLBACK",
    "LLM_UNAVAILABLE",
    "SERVER_UNAVAILABLE",
    "INVALID_SESSION",
}


@st.cache_resource
def get_agent() -> OfficeGenieAgent:
    return OfficeGenieAgent()


@st.cache_resource
def get_chat_executor() -> ThreadPoolExecutor:
    return ThreadPoolExecutor(max_workers=4, thread_name_prefix="officegenie-chat")


@st.cache_resource
def get_login_executor() -> ThreadPoolExecutor:
    return ThreadPoolExecutor(max_workers=4, thread_name_prefix="officegenie-login")


def _escape(value: object) -> str:
    return html.escape(str(value if value is not None else ""))


def _escaped_lines(value: object) -> str:
    return _escape(value).replace("\r\n", "\n").replace("\r", "\n").replace("\n", "&#10;")


def _new_chat_state(company_id: str, employee_id: str, is_open: bool) -> None:
    st.session_state.chat_generation = st.session_state.get("chat_generation", 0) + 1
    st.session_state.chat_session = get_agent().new_session(company_id, employee_id)
    st.session_state.chat_panel_open = is_open
    st.session_state.chat_messages = (
        [{"role": "assistant", "content": WELCOME_TEXT}] if is_open else []
    )
    st.session_state.chat_pending = False
    st.session_state.chat_future = None
    st.session_state.chat_future_generation = None
    st.session_state.chat_job_kind = None
    st.session_state.chat_error = False
    st.session_state.chat_last_question = ""
    st.session_state.chat_pending_ticket = None
    st.session_state.chat_input = ""


def _toggle_chat_panel(company_id: str, employee_id: str) -> None:
    _touch_activity()
    is_open = bool(st.session_state.get("chat_panel_open", False))
    _new_chat_state(company_id, employee_id, not is_open)


def _start_send(text: str, *, append_user: bool = True) -> None:
    _touch_activity()
    text = (text or "").strip()
    if (
        not text
        or not st.session_state.get("chat_panel_open")
        or st.session_state.get("chat_pending")
    ):
        return

    if append_user:
        st.session_state.chat_messages.append({"role": "user", "content": text})
        st.session_state.chat_last_question = text

    chat = st.session_state.chat_session
    generation = st.session_state.chat_generation
    st.session_state.chat_error = False
    st.session_state.chat_pending = True
    st.session_state.chat_job_kind = "send"
    st.session_state.chat_future_generation = generation
    st.session_state.chat_future = get_chat_executor().submit(chat.send, text)


def _send_suggestion(question: str) -> None:
    _start_send(question)


def _send_form_message(input_key: str) -> None:
    question = str(st.session_state.get(input_key, ""))
    st.session_state[input_key] = ""
    _start_send(question)


def _retry_last_question() -> None:
    question = st.session_state.get("chat_last_question", "")
    _start_send(question, append_user=False)


def _start_ticket_action(action: str) -> None:
    _touch_activity()
    if st.session_state.get("chat_pending") or not st.session_state.get(
        "chat_panel_open"
    ):
        return
    chat = st.session_state.chat_session
    generation = st.session_state.chat_generation
    st.session_state.chat_error = False
    st.session_state.chat_pending = True
    st.session_state.chat_job_kind = action
    st.session_state.chat_future_generation = generation
    method = chat.confirm_ticket if action == "confirm" else chat.cancel_ticket
    st.session_state.chat_future = get_chat_executor().submit(method)


def _finish_chat_request() -> None:
    future: Future | None = st.session_state.get("chat_future")
    if not future or not future.done():
        return

    generation = st.session_state.get("chat_future_generation")
    job_kind = st.session_state.get("chat_job_kind")
    st.session_state.chat_future = None
    st.session_state.chat_future_generation = None
    st.session_state.chat_job_kind = None
    st.session_state.chat_pending = False

    # A response from a closed/replaced conversation is deliberately discarded.
    if not st.session_state.get(
        "chat_panel_open"
    ) or generation != st.session_state.get("chat_generation"):
        return

    try:
        result = future.result()
    except Exception:
        st.session_state.chat_error = True
        return

    code = result.get("code")
    if job_kind == "send" and code in CHAT_FAILURE_CODES:
        st.session_state.chat_error = True
        st.session_state.chat_pending_ticket = None
        return

    reply = result.get("reply", "")
    if reply:
        st.session_state.chat_messages.append({"role": "assistant", "content": reply})
    st.session_state.chat_error = False
    st.session_state.chat_pending_ticket = (
        result.get("pending_ticket") or st.session_state.chat_session.pending_ticket
    )

    # Re-render the dashboard from get_sidebar() after a confirmed ticket is saved.
    if result.get("tickets_changed"):
        st.rerun(scope="app")


def _equipment_icon(item: str) -> str:
    name = item.lower()
    if "키보드" in item:
        drawing = '<rect x="4" y="8" width="16" height="9" rx="1.5"/><path d="M7 11h.01M10 11h.01M13 11h.01M16 11h.01M7 14h10"/>'
    elif "마우스" in item:
        drawing = '<rect x="8" y="3" width="8" height="18" rx="4"/><path d="M12 3v5"/>'
    elif "모니터" in item:
        drawing = '<rect x="3.5" y="4" width="17" height="12" rx="1.5"/><path d="M12 16v4M8 20h8"/>'
    elif "노트북" in item or "거치대" in item:
        drawing = '<rect x="5.5" y="4" width="13" height="11" rx="1.2"/><path d="M3 18h18l-1.5 2H4.5L3 18Z"/>'
    else:
        drawing = '<rect x="4" y="5" width="16" height="12" rx="1.5"/><path d="M12 17v3M8 20h8"/>'
    return (
        "<span class='equipment-icon' aria-hidden='true'>"
        f"<svg viewBox='0 0 24 24' fill='none' stroke='#71819A' stroke-width='1.6' "
        f"stroke-linecap='round' stroke-linejoin='round'>{drawing}</svg></span>"
    )


def _equipment_status(status: str) -> tuple[str, str]:
    if status == "지급완료":
        return "지급 완료", "paid"
    if status == "승인":
        return "지급 예정", "scheduled"
    if status == "검토중":
        return "검토 중", "review"
    if status == "반려":
        return "반려", "rejected"
    return "접수 완료", "received"


def _equipment_row(ticket: dict) -> str:
    item = str(ticket.get("item") or "비품")
    status = str(ticket.get("status") or "")
    label, tone = _equipment_status(status)
    scheduled = ticket.get("scheduled_date")
    date_html = (
        f"<small class='equipment-date'>지급 예정일 {_escape(scheduled)}</small>"
        if scheduled and status != "지급완료"
        else ""
    )
    return (
        f"<div class='equipment-row'>{_equipment_icon(item)}"
        f"<div class='equipment-name'>{_escape(item)}"
        f"<span class='equipment-quantity'>× {_escape(ticket.get('quantity') or 1)}</span>"
        f"{date_html}</div>"
        f"<span class='status-badge status-{tone}'><i></i>{_escape(label)}</span></div>"
    )


def _decimal_text(value: Decimal) -> str:
    text = format(value, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text or "0"


def _remaining_leave_value(value: object) -> str:
    try:
        if value is None or isinstance(value, bool):
            raise InvalidOperation
        remaining_days = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return "<div class='leave-value leave-value-unavailable'>확인 불가</div>"

    if not remaining_days.is_finite() or remaining_days < 0:
        return "<div class='leave-value leave-value-unavailable'>확인 불가</div>"

    whole_days = int(remaining_days)
    remaining_hours = (remaining_days - Decimal(whole_days)) * Decimal(8)
    day_markup = ""
    if whole_days > 0 or remaining_hours == 0:
        day_markup = (
            "<span class='leave-group leave-day-group'>"
            f"<span class='leave-number'>{whole_days}</span>"
            "<span class='leave-unit'>일</span>"
            "</span>"
        )

    hour_markup = ""
    if remaining_hours > 0:
        hour_markup = (
            "<span class='leave-group leave-hour-group'>"
            f"<span class='leave-number'>{_escape(_decimal_text(remaining_hours))}</span>"
            "<span class='leave-unit'>시간</span>"
            "</span>"
        )

    return f"<div class='leave-value'>{day_markup}{hour_markup}</div>"


def _onboarding_welcome(name: str, greeting: str) -> str:
    now = datetime.now(ZoneInfo("Asia/Seoul"))
    weekdays = ("월", "화", "수", "목", "금", "토", "일")
    date_label = f"{now.month}월 {now.day}일 {weekdays[now.weekday()]}요일"
    return (
        "<div class='welcome-block'><div><h1>나의 온보딩</h1>"
        f"<p>{_escape(name)}님, 반가워요. {_escape(greeting)}</p></div>"
        f"<span class='dashboard-date'>{_escape(date_label)}</span></div>"
    )


def _onboarding_profile(employee: dict, remaining_leave: object) -> str:
    name = _escape(employee.get("name", "신입사원"))
    department = _escape(employee.get("department", ""))
    position_value = employee.get("position")
    position = _escape(position_value) if position_value else ""
    # 직급이 없는 회사(예: position 필드가 없는 데이터)는 직급과 구분선을 숨긴다.
    profile_meta = (
        f"<span>{department}</span><i></i><span>{position}</span>"
        if position
        else f"<span>{department}</span>"
    )

    person_icon = (
        "<svg viewBox='0 0 64 64' fill='none' aria-hidden='true'>"
        "<circle cx='32' cy='23' r='9.5' stroke='#8D9EB5' stroke-width='1.7'/>"
        "<path d='M16 51v-4.5C16 37.9 23.2 31 32 31s16 6.9 16 15.5V51' stroke='#8D9EB5' stroke-width='1.7' stroke-linecap='round'/>"
        "</svg>"
    )
    return (
        "<div class='profile-card'>"
        f"<div class='employee-placeholder'>{person_icon}</div>"
        "<div class='profile-copy'><span class='eyebrow'>내 정보</span>"
        f"<h2>{name}</h2><div class='profile-meta'>{profile_meta}</div></div>"
        "<div class='leave-summary'><span class='eyebrow'>잔여 연차</span>"
        f"{_remaining_leave_value(remaining_leave)}</div></div>"
    )


def _onboarding_cards(
    tickets: list[dict] | None,
    announcements: list[dict] | None,
    *,
    equipment_unavailable: str = "비품 내역을 불러올 수 없습니다.",
) -> str:
    announcement_count = (
        f"<span class='card-count'>전체 {len(announcements)}건</span>"
        if announcements is not None else ""
    )
    if announcements:
        announcement_rows = "".join(
            "<details class='notice-entry'><summary class='notice-row'>"
            f"<span>{_escape(notice['title'])}</span>"
            f"<time class='notice-date'>{_escape(str(notice.get('published_at') or '')[:10])}</time>"
            "</summary>"
            f"<div class='notice-body'>{_escaped_lines(notice.get('body'))}</div></details>"
            for notice in announcements
        )
    else:
        notice_message = (
            "공지사항을 불러올 수 없습니다."
            if announcements is None else "등록된 공지사항이 없습니다."
        )
        announcement_rows = f"<div class='dashboard-empty'>{notice_message}</div>"

    equipment = [ticket for ticket in (tickets or []) if ticket.get("type") == "비품"]
    equipment_count = sum(int(ticket.get("quantity") or 1) for ticket in equipment)
    equipment_total = (
        f"<span class='card-count'>총 {equipment_count}개</span>"
        if tickets is not None else ""
    )
    if equipment:
        equipment_rows = "".join(_equipment_row(ticket) for ticket in equipment)
    else:
        equipment_message = (
            _escape(equipment_unavailable)
            if tickets is None else "아직 등록된 비품 내역이 없습니다."
        )
        equipment_rows = f"<div class='dashboard-empty'>{equipment_message}</div>"

    return f"""
        <div class="dashboard-grid">
          <div class="dashboard-card notice-card">
            <div class="card-heading">
              <div><h2>공지사항</h2><p>새로운 회사 소식을 확인하세요.</p></div>{announcement_count}
            </div>
            <div class="notice-list">{announcement_rows}</div>
          </div>
          <div class="dashboard-card equipment-card">
            <div class="card-heading">
              <div><h2>내 비품 내역</h2><p>나에게 배정된 비품과 지급 상태예요.</p></div>{equipment_total}
            </div>
            <div class="equipment-list">{equipment_rows}</div>
          </div>
        </div>
    """.strip()


def _render_dashboard(
    employee: dict,
    tickets: list[dict] | None,
    remaining_leave: object,
    announcements: list[dict] | None,
) -> None:
    name = employee.get("name", "신입사원")
    dashboard_html = f"""
    <div class="dashboard-ui">
      <div class="dashboard-main">
        {_onboarding_welcome(name, "회사 생활에 필요한 정보를 한눈에 확인하세요.")}
        {_onboarding_profile(employee, remaining_leave)}
        {_onboarding_cards(tickets, announcements)}
        <div class="dashboard-help">
          <svg viewBox="0 0 20 20" fill="none" aria-hidden="true">
            <circle cx="10" cy="10" r="7" stroke="currentColor" stroke-width="1.7"/>
            <path d="M8.1 7.7a2 2 0 1 1 3.6 1.2c-.8.8-1.7 1.1-1.7 2.3M10 14.1h.01" stroke="currentColor" stroke-width="1.6" stroke-linecap="round"/>
          </svg>
          <span>궁금한 점이 있나요? 오른쪽 아래 지니에게 물어보세요.</span>
        </div>
      </div>
    </div>
    """
    st.markdown(dashboard_html, unsafe_allow_html=True)


def _render_assistant_message(message: str, index: int) -> None:
    with st.container(key=f"chat_row_assistant_{index}"):
        st.markdown(
            "<div class='chat-speaker'>오피스 지니</div>", unsafe_allow_html=True
        )
        with st.container(key=f"chat_bubble_assistant_{index}"):
            st.markdown(message)


def _render_user_message(message: str, index: int) -> None:
    with st.container(key=f"chat_row_user_{index}"):
        with st.container(key=f"chat_bubble_user_{index}"):
            st.markdown(_escape(message), unsafe_allow_html=True)


def _render_pending_ticket() -> None:
    pending_ticket = st.session_state.get("chat_pending_ticket")
    if not pending_ticket:
        return
    with st.container(key="chat_pending_ticket_card"):
        st.markdown(
            "<div class='ticket-check-title'>신청 내용을 확인해 주세요.</div>",
            unsafe_allow_html=True,
        )
        st.markdown(
            "<div class='ticket-check-details'>"
            f"<span>품목</span><strong>{_escape(pending_ticket.get('item'))}</strong>"
            f"<span>수량</span><strong>{_escape(pending_ticket.get('quantity'))}개</strong>"
            f"<span>신청 목적</span><strong>{_escape(pending_ticket.get('purpose'))}</strong>"
            "</div>",
            unsafe_allow_html=True,
        )
        for warning in pending_ticket.get("warnings") or []:
            st.markdown(
                f"<div class='ticket-warning'>{_escape(warning)}</div>",
                unsafe_allow_html=True,
            )
        confirm_col, cancel_col = st.columns(2, gap="small")
        with confirm_col:
            st.button(
                "접수 확정",
                key="chat_ticket_confirm",
                type="primary",
                use_container_width=True,
                disabled=st.session_state.get("chat_pending", False),
                on_click=_start_ticket_action,
                args=("confirm",),
            )
        with cancel_col:
            st.button(
                "신청 취소",
                key="chat_ticket_cancel",
                use_container_width=True,
                disabled=st.session_state.get("chat_pending", False),
                on_click=_start_ticket_action,
                args=("cancel",),
            )


@st.fragment(run_every=0.4, key="office_genie_chat_fragment")
def _render_chat_panel(company_id: str, employee_id: str) -> None:
    if not st.session_state.get("chat_panel_open"):
        return
    if _session_expired():
        st.rerun(scope="app")

    _finish_chat_request()

    icon = (
        "<svg viewBox='0 0 32 32' fill='none' aria-hidden='true'>"
        "<path d='M8.3 8.2h15.4a2.5 2.5 0 0 1 2.5 2.5v8.1a2.5 2.5 0 0 1-2.5 2.5h-7.2l-5.2 3.5v-3.5h-3a2.5 2.5 0 0 1-2.5-2.5v-8.1a2.5 2.5 0 0 1 2.5-2.5Z' stroke='#3868BE' stroke-width='1.8' stroke-linejoin='round'/>"
        "<path d='M11 13h10M11 16.5h7' stroke='#3868BE' stroke-width='1.7' stroke-linecap='round'/>"
        "</svg>"
    )
    with st.container(key="chat_panel"):
        with st.container(key="chat_panel_header"):
            st.markdown(
                "<div class='chat-panel-head'>"
                f"<span class='chat-panel-logo'>{icon}</span>"
                "<strong>오피스 지니 챗봇</strong></div>",
                unsafe_allow_html=True,
            )
            st.button(
                "닫기",
                key="chat_close_button",
                help="대화창 닫기",
                on_click=_toggle_chat_panel,
                args=(company_id, employee_id),
            )

        with st.container(height=348, border=False, key="chat_scroll", autoscroll=True):
            messages = st.session_state.get("chat_messages", [])
            for index, message in enumerate(messages):
                if message.get("role") == "user":
                    _render_user_message(message.get("content", ""), index)
                else:
                    _render_assistant_message(message.get("content", ""), index)

            if st.session_state.get("chat_pending"):
                with st.container(key="chat_row_loading"):
                    st.markdown(
                        "<div class='chat-speaker'>오피스 지니</div>",
                        unsafe_allow_html=True,
                    )
                    with st.container(key="chat_loading_bubble"):
                        loading_text = (
                            "비품 정보를 확인하고 있어요."
                            if any(
                                word in st.session_state.get("chat_last_question", "")
                                for word in (
                                    "비품",
                                    "모니터",
                                    "노트북",
                                    "키보드",
                                    "마우스",
                                )
                            )
                            else "회사 정보를 확인하고 있어요."
                        )
                        st.markdown(
                            f"<div class='loading-copy'>{loading_text}</div>"
                            "<div class='typing-dots' aria-label='답변 준비 중'>"
                            "<i></i><i></i><i></i></div>",
                            unsafe_allow_html=True,
                        )

            elif st.session_state.get("chat_error"):
                with st.container(key="chat_error_state"):
                    st.markdown(
                        "<div class='chat-error-title'>비품 정보를 불러오지 못했어요.</div>"
                        "<div class='chat-error-copy'>잠시 후 다시 시도해 주세요.</div>",
                        unsafe_allow_html=True,
                    )
                    st.button(
                        "다시 시도",
                        key="chat_retry_button",
                        disabled=st.session_state.get("chat_pending", False),
                        on_click=_retry_last_question,
                    )

            elif len(messages) == 1 and messages[0].get("role") == "assistant":
                st.markdown(
                    "<div class='suggestion-label'>이렇게 물어보세요</div>",
                    unsafe_allow_html=True,
                )
                st.button(
                    SUGGESTED_QUESTION,
                    key="chat_suggested_question",
                    use_container_width=True,
                    disabled=st.session_state.get("chat_pending", False),
                    on_click=_send_suggestion,
                    args=(SUGGESTED_QUESTION,),
                )

            _render_pending_ticket()

        with st.container(key="chat_footer"):
            with st.form(
                key="chat_send_form",
                clear_on_submit=False,
                border=False,
                enter_to_submit=True,
            ):
                with st.container(
                    horizontal=True,
                    wrap=False,
                    gap="small",
                    vertical_alignment="center",
                    key="chat_input_row",
                ):
                    st.text_input(
                        "메시지",
                        placeholder="궁금한 점을 입력해 주세요",
                        max_chars=500,
                        key="chat_input",
                        label_visibility="collapsed",
                        disabled=st.session_state.get("chat_pending", False),
                        width="stretch",
                    )
                    st.form_submit_button(
                        "전송",
                        key="chat_send_button",
                        icon=":material/arrow_upward:",
                        width="content",
                        disabled=st.session_state.get("chat_pending", False),
                        on_click=_send_form_message,
                        args=("chat_input",),
                    )
            st.markdown(
                "<div class='chat-char-count'>"
                f"{len(str(st.session_state.get('chat_input', '') or ''))}/500"
                "</div>",
                unsafe_allow_html=True,
            )
            st.markdown(
                "<div class='chat-reset-note'>창을 닫으면 대화가 초기화됩니다.</div>",
                unsafe_allow_html=True,
            )


def _render_styles() -> None:
    st.markdown(
        r"""
        <style>
        :root {
            --page: #F5F7FA;
            --card: #FFFFFF;
            --title: #202D40;
            --body: #34445B;
            --muted: #67758A;
            --line: #E2E7EE;
            --input-line: #DCE5F4;
            --blue: #3868BE;
            --green-bg: #EAF5EF;
            --green: #28725B;
            --amber-bg: #FFF4DE;
            --amber: #94651C;
            --orange-bg: #FFF7F2;
            --orange-line: #F0D8CA;
            --orange: #91492F;
        }
        html, body, [data-testid="stAppViewContainer"], [data-testid="stMain"] {
            background: var(--page) !important;
            color: var(--title);
            font-family: Inter, "Noto Sans KR", "Apple SD Gothic Neo", sans-serif;
        }
        [data-testid="stHeader"], [data-testid="stToolbar"], #MainMenu, footer {
            display: none !important;
        }
        [data-testid="stMainBlockContainer"], .block-container {
            width: 100% !important;
            max-width: none !important;
            padding: 0 !important;
            margin: 0 !important;
        }
        .st-key-dashboard_root { min-height: 100dvh; margin-top: -16px !important; }
        .dashboard-ui { background: var(--page); color: var(--title); }
        :root { --site-header-height: 80px; --site-header-gutter: 24px; }
        .st-key-site_header {
            min-height: var(--site-header-height);
            margin: 0 !important;
            padding: 0 max(var(--site-header-gutter), calc((100% - 1280px) / 2)) !important;
            box-sizing: border-box;
            background: #fff;
            border: 1px solid var(--line);
        }
        .st-key-site_header [data-testid="stMarkdownContainer"] { margin-bottom: 0; }
        .header-logo {
            width: 34px;
            height: 34px;
            display: grid;
            place-items: center;
            flex: 0 0 34px;
            border-radius: 9px;
            background: var(--blue);
        }
        .header-logo svg { width: 25px; height: 25px; }
        .st-key-site_header .st-key-logout_button button {
            min-height: 30px !important;
            padding: 4px 20px !important;
            border: 0 !important;
            border-radius: 999px !important;
            background: #EDF3FD !important;
            color: var(--blue) !important;
            white-space: nowrap;
        }
        .st-key-auth_card {
            width: min(440px, calc(100% - 32px));
            margin: 72px auto 40px auto !important;
            padding: 32px 32px 24px 32px;
            background: #fff;
            border: 1px solid var(--line);
            border-radius: 20px;
            box-shadow: 0 12px 32px rgba(32, 45, 64, 0.06);
        }
        .auth-brand { display: flex; align-items: center; gap: 12px; white-space: nowrap; }
        .auth-brand strong { font-size: 23px; letter-spacing: -0.7px; color: var(--title); }
        .auth-sub { color: var(--muted); font-size: 14px; margin: 0 0 18px 0; }
        .st-key-entry_page {
            margin-top: -16px !important;
            background: #F3F6F9;
            min-height: 100dvh;
        }
        .st-key-entry_page > [data-testid="stLayoutWrapper"]:has(> .st-key-landing_hero) {
            flex: 1 0 auto;
        }
        .st-key-landing_hero {
            display: grid !important;
            grid-template-columns: minmax(0, 2fr) minmax(0, 2.9fr);
            align-items: center;
            gap: clamp(24px, 3.4vw, 48px);
            width: min(1280px, calc(100% - 2 * var(--site-header-gutter))) !important;
            margin-inline: auto;
            padding-block: clamp(36px, 5vw, 72px);
        }
        .st-key-landing_copy [data-testid="stMarkdownContainer"] { margin-bottom: 0; }
        .landing-title {
            padding: 0 !important;
            margin: 0 !important;
            font-size: clamp(24px, 2.65vw, 38px) !important;
            font-weight: 750;
            line-height: 1.44 !important;
            letter-spacing: -0.045em;
            color: var(--title);
        }
        .landing-subtitle { margin: 24px 0 30px !important; color: var(--muted); font-size: 17px; }
        .st-key-landing_actions {
            display: grid !important;
            grid-template-columns: repeat(2, minmax(0, 1fr));
            gap: 12px;
            max-width: 308px;
            margin: 0 !important;
            padding: 0 !important;
        }
        .st-key-landing_login button, .st-key-login_card [data-testid="stFormSubmitButton"] button {
            background: #3562B4;
            border-color: #3562B4;
            border-radius: 11px;
            color: white;
            font-weight: 700;
        }
        .st-key-landing_login button:hover,
        .st-key-login_card [data-testid="stFormSubmitButton"] button:not(:disabled):hover {
            background: #2B539E;
            border-color: #2B539E;
            color: white;
        }
        .st-key-landing_actions button { min-height: 52px; }
        .st-key-landing_register button {
            border: 1px solid #3562B4;
            border-radius: 11px;
            background: #fff;
            color: #3562B4;
        }
        .st-key-landing_register button:hover { background: #EDF3FD; }
        .st-key-landing_actions button p,
        .st-key-login_card [data-testid="stFormSubmitButton"] button p {
            font-size: 17px;
            font-weight: 700;
        }
        .st-key-landing_preview {
            border: 1px solid var(--line);
            border-radius: 20px;
            overflow: hidden;
            box-shadow: 0 10px 32px rgba(32, 45, 64, 0.08);
        }
        .st-key-landing_preview img { display: block; width: 100%; height: auto; }
        @media (max-width: 920px) {
            .st-key-landing_hero { grid-template-columns: minmax(0, 1fr); }
            .landing-title { font-size: clamp(24px, 5vw, 38px) !important; }
        }
        .st-key-entry_page > [data-testid="stLayoutWrapper"]:has(> .st-key-login_body) {
            flex: 1 0 auto;
        }
        .st-key-login_body {
            display: grid !important;
            grid-template-columns: minmax(0, 1fr);
            grid-template-rows: minmax(4rem, 1fr) auto minmax(4rem, 1fr);
        }
        .st-key-login_body > [data-testid="stLayoutWrapper"]:has(> .st-key-login_card) {
            grid-row: 2;
        }
        .st-key-login_body:has(.st-key-login_submit) {
            grid-template-rows: minmax(24px, 1fr) auto minmax(24px, 1fr);
        }
        .st-key-login_card {
            width: min(496px, calc(100% - 32px)) !important;
            margin-inline: auto !important;
            padding: 32px clamp(20px, 5vw, 39px);
            background: var(--card);
            border: 1px solid var(--line);
            border-radius: 16px;
            box-shadow: 0 8px 24px rgba(32, 45, 64, 0.04);
        }
        .login-title {
            padding: 0 !important;
            margin: 0 !important;
            font-size: 29px !important;
            color: var(--title);
        }
        .st-key-login_card_header {
            position: relative;
            min-height: 35px;
            margin: 0 0 16px;
            padding: 0 !important;
            overflow: visible !important;
        }
        .st-key-login_card_header [data-testid="stMarkdown"] { min-height: 35px; }
        .st-key-login_card_header :is(.st-key-login_back, .st-key-register_back) {
            position: absolute;
            top: 50%;
            right: 0;
            transform: translateY(-50%);
        }
        .st-key-login_card_header :is(.st-key-login_back, .st-key-register_back) button {
            min-height: 30px;
            padding: 4px 12px;
            border: 0;
            border-radius: 999px;
            background: #EDF3FD;
            color: var(--blue);
            white-space: nowrap;
        }
        .st-key-login_card_header button p { font-size: 13px; }
        .st-key-login_card_header button:not(:disabled):hover { background: #DCE8FA; }
        .st-key-login_card [data-testid="stForm"] { padding: 0; }
        .st-key-login_card [data-testid="stWidgetLabel"] p { font-size: 14px; font-weight: 550; }
        .st-key-login_card :is([data-testid="stTextInputRootElement"], [data-baseweb="input"]) {
            min-height: 52px;
            background: var(--card);
            border: 1px solid #D5DFEF;
            border-radius: 10px;
        }
        .st-key-login_card :is([data-testid="stTextInputRootElement"], [data-baseweb="input"]):focus-within {
            border-color: var(--blue);
        }
        .st-key-login_card input { color: var(--title); font-size: 16px; }
        .st-key-login_card input::placeholder { color: #71819A; opacity: 1; }
        .st-key-login_card [data-testid="stFormSubmitButton"] button { min-height: 52px; }
        .st-key-login_card [data-testid="stFormSubmitButton"] button:disabled { opacity: .85; }
        [data-testid="stElementContainer"]:has(.login-success-marker) { display: none; }
        /* Streamlit fades stale elements while the destination screen is being rendered. */
        [data-testid="stMain"]:has(.login-success-marker) [data-stale="true"],
        .st-key-login_card:has(.login-success-marker) [data-testid="stFormSubmitButton"] button:disabled {
            opacity: 1 !important;
            transition: none !important;
        }
        /* Keep the current screen readable while Streamlit retains its previous results. */
        .st-key-db_screen :is([data-stale], [role="tablist"], [role="tab"]) {
            opacity: 1 !important;
            transition: none !important;
        }
        .st-key-employee_read_status {
            width: min(1280px, calc(100% - 2 * var(--site-header-gutter))) !important;
            margin-inline: auto;
        }
        .st-key-login_card:has([data-testid="stAlertContentError"])
            :is([data-testid="stTextInputRootElement"], [data-baseweb="input"]) {
            border-color: #C84940;
        }
        .st-key-login_feedback { min-height: 14px; }
        .st-key-login_feedback [data-testid="stAlertContainer"] { padding: 0; background: transparent; }
        .st-key-login_feedback [data-testid="stAlertContainer"] p { font-size: 13px; }
        .company-code-label { color: var(--muted); font-size: 13px; margin-top: 8px; }
        .company-code-big {
            font-size: 44px; font-weight: 800; letter-spacing: 0.18em;
            color: var(--blue); margin: 4px 0 8px 0;
            font-family: "SFMono-Regular", Menlo, Consolas, monospace;
        }
        .st-key-company_settings { margin-top: -16px !important; min-height: 100dvh; background: #F3F6F9; }
        .st-key-admin_root {
            width: min(1280px, calc(100% - 2 * var(--site-header-gutter))) !important;
            margin: 40px auto 64px !important;
            gap: 24px;
        }
        .st-key-settings_heading h1 {
            margin: 0; padding: 0 !important; color: var(--title);
            font-size: clamp(24px, 3vw, 32px);
        }
        :is(.st-key-settings_heading, .st-key-settings_code_card, .st-key-settings_panel_templates, .st-key-settings_panel_upload)
            [data-testid="stMarkdownContainer"] { margin-bottom: 0; }
        .st-key-settings_heading button {
            min-height: 44px; padding-inline: 24px; white-space: nowrap;
            background: #fff; border-color: var(--input-line); border-radius: 10px;
        }
        :is(.st-key-settings_code_card, .st-key-settings_panel_templates, .st-key-settings_panel_upload) {
            padding: clamp(16px, 2vw, 24px);
            border: 1px solid var(--input-line);
            border-radius: 16px;
            background: #fff;
        }
        :is(.st-key-settings_code_card, .st-key-settings_metrics, .st-key-settings_templates) {
            display: grid !important;
            align-items: center;
            gap: 16px;
        }
        .st-key-settings_code_card { grid-template-columns: minmax(0, 1.6fr) minmax(0, 1fr); }
        .st-key-settings_code_card .company-code-label { margin-top: 0; font-size: 14px; }
        .st-key-settings_code_card strong { color: var(--title); margin-right: 16px; font-size: 16px; }
        .st-key-settings_code_card .company-code-big {
            margin: 8px 0 0; font-family: inherit;
            font-size: 36px; line-height: 1.25; letter-spacing: .13em;
        }
        .st-key-settings_data > [data-testid="stVerticalBlock"],
        .st-key-settings_data[data-testid="stVerticalBlock"] { gap: 24px; }
        .st-key-settings_metrics { grid-template-columns: repeat(auto-fit, minmax(min(140px, 100%), 1fr)); }
        .st-key-settings_metrics [data-testid="stMetric"] {
            min-height: 104px; padding: 16px 20px; border: 1px solid var(--input-line);
            border-radius: 12px; background: #fff;
        }
        .st-key-settings_metrics [data-testid="stMetricValue"] { font-size: 30px; font-weight: 750; color: var(--title); }
        .st-key-settings_templates { grid-template-columns: repeat(auto-fit, minmax(min(165px, 100%), 1fr)); gap: 12px; margin-top: 8px; }
        .st-key-settings_templates button { color: var(--blue); min-height: 44px; border-color: #CAD9EF; }
        .st-key-settings_tabs [role="tablist"] { gap: 4px; }
        .st-key-settings_tabs [role="tab"] { min-height: 52px; padding-inline: clamp(10px, 2.25vw, 32px); color: var(--muted); }
        .st-key-settings_tabs [aria-selected="true"] { color: var(--blue); font-weight: 700; }
        :is(.st-key-settings_panel_templates, .st-key-settings_panel_upload) h3 {
            margin: 0; padding: 0 !important; color: var(--title); font-size: 20px;
        }
        .settings-file-labels { display: flex; flex-wrap: wrap; gap: 8px; }
        .settings-file-labels span { background: #F3F6F9; color: #526885; border-radius: 6px; padding: 4px 10px; font-size: 13px; }
        .settings-upload-note { margin-top: 16px; padding: 14px 16px; border-radius: 10px; background: #EDF3FD; color: #395987; font-size: 14px; }
        .st-key-settings_panel_upload [data-testid="stFileUploaderDropzone"] {
            min-height: 64px; padding: 10px 14px; background: #fff;
            border: 1px solid #CAD9EF; border-radius: 10px;
            flex-direction: row; align-items: center;
        }
        .st-key-settings_panel_upload [data-testid="stFileUploaderDropzoneInstructions"] { flex: 1; order: -1; }
        .st-key-settings_panel_upload [data-testid="stFileUploaderDropzoneInstructions"] > * { display: none; }
        .st-key-settings_panel_upload [data-testid="stFileUploaderDropzoneInstructions"]::before {
            content: "선택된 파일이 없습니다"; color: var(--muted); font-size: 14px;
        }
        /* Keep Streamlit's file chips and their remove buttons next to the file picker. */
        .st-key-settings_panel_upload [data-testid="stFileUploaderDropzone"] > div:has([data-testid="stFileChips"]) {
            flex: 1; min-width: 0;
        }
        .st-key-settings_panel_upload [data-testid="stFileChips"] {
            display: grid; grid-template-columns: minmax(0, 1fr) max-content; align-items: center; gap: 12px;
        }
        .st-key-settings_panel_upload [data-testid="stFileChips"] > div { min-width: 0; width: auto; }
        .st-key-settings_panel_upload [data-testid="stFileUploaderDropzone"] button:is(
            [data-testid="stBaseButton-secondary"], [aria-label="Add files"]
        ) {
            min-width: clamp(88px, 14vw, 128px); min-height: 40px; height: auto;
            border: 1px solid var(--blue); border-radius: 8px;
            color: var(--blue); background: #fff; font-size: 0; flex-shrink: 0;
        }
        .st-key-settings_panel_upload [data-testid="stFileUploaderDropzone"] button:is(
            [data-testid="stBaseButton-secondary"], [aria-label="Add files"]
        ) > * { display: none; }
        .st-key-settings_panel_upload [data-testid="stFileUploaderDropzone"] button:is(
            [data-testid="stBaseButton-secondary"], [aria-label="Add files"]
        )::after { content: "파일 선택"; font-size: 14px; font-weight: 600; }
        .st-key-settings_upload_footer { border-top: 1px solid var(--line); padding-top: 10px; }
        .st-key-settings_upload_footer button { min-width: 116px; min-height: 44px; }
        .st-key-settings_upload_footer button:disabled {
            background: #E6EBF2; border-color: #E6EBF2; color: #66778F;
        }
        @media (max-width: 680px) {
            .st-key-admin_root { margin-top: 24px !important; }
            .st-key-settings_code_card { grid-template-columns: minmax(0, 1fr); }
        }
        .st-key-admin_root [data-testid="stMarkdownContainer"] h4 { margin-top: 18px; }
        .st-key-manager_dashboard { margin-top: -16px !important; }
        .manager-brand { display: flex; gap: 20px; align-items: center; }
        .manager-brand > span {
            color: var(--muted);
            font-size: 14px;
            border-left: 1px solid var(--line);
            padding-left: 20px;
        }
        .st-key-manager_header_actions { gap: 24px; }
        .st-key-manager_header_actions button {
            min-width: 88px; min-height: 30px; padding: 4px 12px;
            border: 0; border-radius: 999px;
            background: #EAF1FC; color: var(--blue); white-space: nowrap;
        }
        .st-key-manager_header_actions button p { font-size: 13px; }
        .st-key-manager_header_actions button:hover { background: #DFEAFB; color: var(--blue); }
        .st-key-manager_main {
            width: min(1280px, calc(100% - 48px)) !important;
            margin: 46px auto 28px !important;
        }
        .manager-heading { display: flex; align-items: baseline; gap: 20px; flex-wrap: wrap; }
        .manager-heading :is(h1, h2) { margin: 0; padding: 0 !important; color: var(--title); }
        .manager-heading h1 { font-size: clamp(25px, 3vw, 32px); }
        .manager-heading h2 { font-size: 21px; }
        .manager-heading > span { color: var(--blue); font-weight: 600; font-size: 13px; }
        .manager-subtitle { color: var(--muted); font-size: 14px; margin: 8px 0 0; }
        .st-key-manager_tabs [role="tablist"] { gap: 12px; }
        .st-key-manager_tabs [role="tab"] { min-width: 128px; min-height: 54px; color: var(--muted); }
        .st-key-manager_tabs [role="tab"][aria-selected="true"] { color: var(--blue); font-weight: 700; }
        .st-key-manager_tabs [role="tabpanel"] { padding-top: 22px; }
        .st-key-manager_card {
            min-height: 525px;
            padding: 24px 26px;
            border: 1px solid var(--input-line);
            border-radius: 16px;
            background: var(--card);
        }
        .st-key-manager_card [data-testid="stForm"] { padding: 0; }
        .st-key-manager_card button[kind^="primary"] { border-radius: 8px; background: var(--blue); }
        .st-key-manager_card :is([data-testid="stTextInputRootElement"], [data-baseweb="select"] > div) {
            min-height: 44px;
            border-radius: 8px;
        }
        .st-key-manager_table { overflow-x: auto; }
        .st-key-manager_table [class*="st-key-manager_row_"],
        .st-key-manager_table_head { min-width: 650px; padding: 0 12px; }
        .st-key-manager_table [class*="st-key-manager_row_"] {
            min-height: 64px; justify-content: center; border-bottom: 1px solid #E7ECF3;
        }
        .st-key-manager_table_head {
            min-height: 48px; justify-content: center; background: var(--page); color: var(--muted);
        }
        .manager-cell {
            display: flex; align-items: center; min-height: 60px;
            color: var(--title); font-size: 14px; overflow-wrap: anywhere;
        }
        .st-key-manager_table_head .manager-cell { min-height: 45px; color: var(--muted); font-size: 13px; }
        .st-key-manager_table button { border: 1px solid var(--input-line); color: var(--blue); }
        .manager-badge {
            display: inline-flex; justify-content: center; align-items: center;
            min-height: 29px; min-width: 64px; padding: 3px 12px;
            border-radius: 999px; font-size: 12px; font-weight: 650; text-align: center;
        }
        .manager-badge-green { background: var(--green-bg); color: var(--green); }
        .manager-badge-amber { background: var(--amber-bg); color: var(--amber); }
        .manager-badge-blue { background: #EDF3FD; color: var(--blue); }
        .manager-badge-gray { background: #EEF2F6; color: var(--muted); }
        .manager-info-grid {
            display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 24px;
            padding: 20px; border-radius: 11px; background: var(--page); margin: 0;
        }
        .manager-info-grid dt { color: var(--muted); font-size: 13px; margin-bottom: 6px; }
        .manager-info-grid dd { color: var(--title); margin: 0; overflow-wrap: anywhere; }
        .manager-policy-text, .manager-reason { color: var(--body); white-space: pre-wrap; overflow-wrap: anywhere; }
        .manager-policy-text { line-height: 1.7; }
        @media (max-width: 680px) {
            .manager-brand { gap: 8px; }
            .manager-brand > span { padding-left: 8px; }
            .st-key-manager_header_actions { gap: 8px; }
            .st-key-manager_header_actions button { min-width: 0; padding-inline: 9px; }
            .st-key-manager_main { width: calc(100% - 32px) !important; margin-top: 28px !important; }
            .st-key-manager_card { padding: 18px; }
            .st-key-manager_tabs [role="tab"] { min-width: max-content; }
            .manager-info-grid { grid-template-columns: repeat(2, minmax(0, 1fr)); }
        }
        .dashboard-main, .st-key-admin_home_main {
            margin: 0 auto;
            padding: 50px 0 36px;
        }
        .dashboard-main { width: min(1280px, calc(100% - 48px)); }
        .st-key-admin_home_main { width: min(1280px, calc(100% - 48px)) !important; }
        .welcome-block {
            display: flex;
            justify-content: space-between;
            align-items: flex-start;
            gap: 20px;
            margin-bottom: 38px;
        }
        .welcome-block h1 {
            margin: 0;
            padding: 0 !important;
            color: var(--title);
            font-size: 29px;
            font-weight: 720;
            line-height: 1.35;
            letter-spacing: -0.8px;
        }
        .welcome-block p {
            margin: 9px 0 0;
            color: var(--muted);
            font-size: 15px;
            line-height: 1.55;
        }
        .welcome-block .dashboard-date {
            padding-top: 8px;
            color: #71819A;
            font-size: 14px;
            white-space: nowrap;
        }
        .profile-card, .dashboard-card {
            box-sizing: border-box;
            background: var(--card);
            border: 1px solid var(--line);
            border-radius: 17px;
            box-shadow: 0 1px 3px rgba(32, 45, 64, 0.018);
        }
        .profile-card {
            position: relative;
            height: 184px;
            display: flex;
            align-items: center;
            gap: 28px;
            padding: 30px 32px;
        }
        .employee-placeholder {
            width: 112px;
            height: 112px;
            flex: 0 0 112px;
            display: grid;
            place-items: center;
            border-radius: 20px;
            background: #EEF3FA;
        }
        .employee-placeholder svg { width: 60px; height: 60px; }
        .profile-copy { min-width: 0; }
        .st-key-admin_home_profile { position: relative; }
        .st-key-admin_home_profile .profile-copy { max-width: 40%; overflow-wrap: anywhere; }
        .st-key-admin_home_profile > [data-testid="stLayoutWrapper"]:has(> .st-key-admin_home_action) {
            position: absolute;
            right: 32px;
            top: 22px;
            width: max-content !important;
        }
        .st-key-admin_home_action button {
            min-height: 68px;
            padding: 0 14px;
            border: 0;
            border-radius: 16px;
            background: #EDF3FD;
            color: var(--blue);
        }
        .st-key-admin_home_action button p { font-size: 23px; font-weight: 700; }
        .st-key-chat_fab .stButton button:disabled {
            opacity: .55;
            cursor: not-allowed;
            transform: none;
        }
        .eyebrow { display: block; color: #71819A; font-size: 13px; line-height: 1.4; }
        .profile-copy h2 {
            margin: 6px 0 10px;
            padding: 0 !important;
            color: var(--title);
            font-size: 32px;
            font-weight: 720;
            letter-spacing: -0.8px;
            line-height: 1.28;
        }
        .profile-meta { display: flex; align-items: center; gap: 16px; color: #71819A; font-size: 15px; }
        .profile-meta i { width: 1px; height: 17px; background: var(--line); }
        .leave-summary {
            position: absolute;
            left: 62%;
            top: 43px;
            min-width: 120px;
            text-align: left;
        }
        .leave-value {
            display: flex;
            align-items: baseline;
            gap: 10px;
            margin-top: 6px;
            color: var(--blue);
            white-space: nowrap;
        }
        .leave-group { display: inline-flex; align-items: baseline; gap: 2px; }
        .leave-number {
            color: var(--blue);
            font-size: 32px;
            font-weight: 720;
            line-height: 1.28;
            letter-spacing: -0.8px;
        }
        .leave-unit { color: var(--blue); font-size: 18px; font-weight: 620; line-height: 1.3; }
        .leave-hour-group .leave-number { font-size: 22px; letter-spacing: -0.35px; }
        .leave-hour-group .leave-unit { font-size: 15px; }
        .leave-value-unavailable {
            display: block;
            color: var(--muted);
            font-size: 15px;
            font-weight: 550;
            line-height: 1.8;
        }
        .dashboard-grid {
            display: grid;
            grid-template-columns: minmax(0, 1.45fr) minmax(0, 1fr);
            gap: 24px;
            margin-top: 24px;
        }
        .dashboard-card { min-height: 328px; padding: 26px 27px 20px; }
        .card-heading {
            display: flex;
            align-items: flex-start;
            justify-content: space-between;
            gap: 16px;
        }
        .card-heading h2 {
            margin: 0;
            padding: 0 !important;
            color: var(--title);
            font-size: 20px;
            line-height: 1.45;
            font-weight: 680;
            letter-spacing: -0.35px;
        }
        .card-heading p { margin: 7px 0 0; color: var(--muted); font-size: 14px; line-height: 1.45; }
        .card-count { padding-top: 4px; color: #71819A; font-size: 13px; white-space: nowrap; }
        .notice-list, .equipment-list { margin-top: 22px; border-top: 1px solid #E7ECF3; }
        .notice-row {
            min-height: 64px;
            box-sizing: border-box;
            display: flex;
            align-items: center;
            justify-content: space-between;
            gap: 16px;
            border-bottom: 1px solid #E7ECF3;
            color: var(--body);
            font-size: 15px;
        }
        .notice-row:last-child { border-bottom: 0; }
        .notice-entry { border-bottom: 1px solid #E7ECF3; }
        .notice-entry:last-child { border-bottom: 0; }
        .notice-entry summary { cursor: pointer; border-bottom: 0; }
        .notice-entry summary::-webkit-details-marker { display: none; }
        .notice-body { white-space: pre-wrap; overflow-wrap: anywhere; padding: 0 0 18px; color: var(--body); }
        .notice-row .notice-date { flex: 0 0 auto; color: #71819A; font-size: 13px; }
        .equipment-row {
            min-height: 64px;
            box-sizing: border-box;
            display: flex;
            align-items: center;
            gap: 13px;
            border-bottom: 1px solid #E7ECF3;
        }
        .equipment-row:last-child { border-bottom: 0; }
        .equipment-icon {
            width: 40px;
            height: 40px;
            flex: 0 0 40px;
            display: grid;
            place-items: center;
            border-radius: 11px;
            background: #F5F7FA;
        }
        .equipment-icon svg { width: 23px; height: 23px; }
        .equipment-name { min-width: 0; color: var(--body); font-size: 15px; font-weight: 570; }
        .equipment-quantity { margin-left: 6px; color: #8D9EB5; font-size: 12px; font-weight: 400; }
        .equipment-date { display: block; margin-top: 3px; color: var(--muted); font-size: 12px; }
        .status-badge {
            display: inline-flex;
            align-items: center;
            justify-content: center;
            gap: 6px;
            min-height: 30px;
            box-sizing: border-box;
            margin-left: auto;
            padding: 0 13px;
            border-radius: 999px;
            font-size: 12px;
            font-weight: 560;
            white-space: nowrap;
        }
        .status-badge i { width: 5px; height: 5px; display: block; border-radius: 50%; background: currentColor; }
        .status-paid { background: var(--green-bg); color: var(--green); }
        .status-scheduled { background: var(--amber-bg); color: var(--amber); }
        .status-review, .status-received { background: #EDF3FD; color: #5A77A9; }
        .status-rejected { background: #FFF1F0; color: #A34D46; }
        .dashboard-empty { padding: 22px 0; color: var(--muted); font-size: 14px; }
        .dashboard-help {
            display: flex;
            align-items: center;
            gap: 8px;
            margin-top: 24px;
            color: #71819A;
            font-size: 13px;
        }
        .dashboard-help svg { width: 18px; height: 18px; flex: 0 0 18px; }

        /* Only the chatbot components are fixed above the dashboard. */
        .st-key-chat_fab {
            position: fixed !important;
            z-index: 10002 !important;
            right: 32px !important;
            bottom: 32px !important;
            width: 64px !important;
            height: 64px !important;
            min-width: 64px !important;
            margin: 0 !important;
            padding: 0 !important;
            overflow: visible !important;
        }
        .st-key-chat_fab [data-testid="stButton"] { width: 64px !important; height: 64px !important; margin: 0 !important; }
        .st-key-chat_fab button {
            position: relative !important;
            width: 64px !important;
            min-width: 64px !important;
            max-width: 64px !important;
            height: 64px !important;
            min-height: 64px !important;
            padding: 0 !important;
            border: 0 !important;
            border-radius: 50% !important;
            background: var(--blue) !important;
            box-shadow: 0 7px 17px rgba(56, 104, 190, 0.27) !important;
            color: transparent !important;
            font-size: 0 !important;
            cursor: pointer;
        }
        .st-key-chat_fab button::after {
            position: absolute;
            inset: 0;
            display: block;
            width: 31px;
            height: 31px;
            margin: auto;
            background: center / contain no-repeat url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 32 32' fill='none'%3E%3Cpath d='M8.3 8.2h15.4a2.5 2.5 0 0 1 2.5 2.5v8.1a2.5 2.5 0 0 1-2.5 2.5h-7.2l-5.2 3.5v-3.5h-3a2.5 2.5 0 0 1-2.5-2.5v-8.1a2.5 2.5 0 0 1 2.5-2.5Z' stroke='white' stroke-width='1.8' stroke-linejoin='round'/%3E%3Cpath d='M11 13h10M11 16.5h7' stroke='white' stroke-width='1.7' stroke-linecap='round'/%3E%3C/svg%3E");
            content: "";
        }
        .st-key-chat_fab button:hover { background: #315EA9 !important; transform: translateY(-1px); }
        .st-key-chat_fab button:focus-visible { outline: 3px solid #BFCEEB !important; outline-offset: 3px; }
        .st-key-chat_fab [data-testid="stMarkdownContainer"] {
            position: absolute !important;
            width: 1px !important;
            height: 1px !important;
            padding: 0 !important;
            margin: -1px !important;
            overflow: hidden !important;
            clip: rect(0, 0, 0, 0) !important;
            white-space: nowrap !important;
            border: 0 !important;
        }

        .st-key-chat_panel {
            position: fixed !important;
            z-index: 10001 !important;
            right: 32px !important;
            bottom: 112px !important;
            width: 392px !important;
            min-width: 392px !important;
            max-width: 392px !important;
            height: 538px !important;
            min-height: 538px !important;
            max-height: 538px !important;
            box-sizing: border-box !important;
            display: flex !important;
            flex-direction: column !important;
            gap: 0 !important;
            margin: 0 !important;
            padding: 0 !important;
            overflow: hidden !important;
            border: 1px solid #DCE5F4 !important;
            border-radius: 20px !important;
            background: #fff !important;
            box-shadow: 0 15px 42px rgba(37, 57, 87, 0.17) !important;
            color: var(--title) !important;
        }
        .st-key-chat_panel > [data-testid="stVerticalBlock"] { gap: 0 !important; min-height: 0 !important; }
        .st-key-chat_panel [data-testid="stMarkdownContainer"] p { margin: 0; }
        .st-key-chat_panel_header {
            position: relative !important;
            flex: 0 0 75px !important;
            width: 100% !important;
            height: 75px !important;
            min-height: 75px !important;
            box-sizing: border-box !important;
            padding: 0 17px !important;
            border-bottom: 1px solid #E2E7EE !important;
        }
        .st-key-chat_panel_header .chat-panel-head {
            height: 74px;
            display: flex;
            align-items: center;
            gap: 12px;
            padding-right: 38px;
        }
        .chat-panel-logo {
            width: 36px;
            height: 36px;
            flex: 0 0 36px;
            display: grid;
            place-items: center;
            border-radius: 11px;
            background: #EDF3FD;
        }
        .chat-panel-logo svg { width: 25px; height: 25px; }
        .chat-panel-head strong { color: var(--title); font-size: 16px; font-weight: 670; letter-spacing: -0.25px; }
        .st-key-chat_close_button {
            position: absolute !important;
            z-index: 3 !important;
            top: 19px !important;
            right: 12px !important;
            width: 34px !important;
            height: 34px !important;
            margin: 0 !important;
        }
        .st-key-chat_close_button [data-testid="stButton"] { width: 34px !important; height: 34px !important; margin: 0 !important; }
        .st-key-chat_close_button button {
            width: 34px !important;
            min-width: 34px !important;
            height: 34px !important;
            min-height: 34px !important;
            padding: 0 !important;
            border: 0 !important;
            border-radius: 8px !important;
            background: transparent !important;
            color: transparent !important;
            font-size: 0 !important;
            box-shadow: none !important;
            cursor: pointer;
        }
        .st-key-chat_close_button button::after {
            color: #71819A;
            content: "×";
            font-size: 25px;
            font-weight: 350;
            line-height: 1;
        }
        .st-key-chat_close_button button:hover { background: #F5F7FA !important; }
        .st-key-chat_close_button button:focus-visible { outline: 2px solid #3868BE !important; }

        .st-key-chat_scroll {
            flex: 1 1 auto !important;
            width: 100% !important;
            min-height: 0 !important;
            box-sizing: border-box !important;
            padding: 15px 18px 10px !important;
            background: #fff !important;
            overflow-y: auto !important;
            scrollbar-color: #DCE5F4 transparent;
            scrollbar-width: thin;
        }
        .st-key-chat_scroll > [data-testid="stVerticalBlock"] { gap: 0 !important; }
        .st-key-chat_scroll [data-testid="stElementContainer"] { margin-bottom: 0 !important; }
        .st-key-chat_scroll .chat-speaker { margin: 0 0 8px; color: #71819A; font-size: 12px; line-height: 1.35; }
        .st-key-chat_scroll .st-key-chat_row_assistant_0,
        .st-key-chat_scroll [class*="st-key-chat_row_assistant_"] {
            width: 100% !important;
            display: flex !important;
            flex-direction: column !important;
            align-items: flex-start !important;
            margin: 0 0 14px !important;
        }
        .st-key-chat_scroll [class*="st-key-chat_row_user_"] {
            width: 100% !important;
            display: flex !important;
            flex-direction: column !important;
            align-items: flex-end !important;
            margin: 0 0 14px !important;
        }
        .st-key-chat_scroll [class*="st-key-chat_row_user_"] > [data-testid="stLayoutWrapper"] {
            width: fit-content !important;
            max-width: 86% !important;
            margin-left: auto !important;
            margin-right: 0 !important;
            align-self: flex-end !important;
            flex-direction: column !important;
            align-items: flex-end !important;
        }
        .st-key-chat_scroll [class*="st-key-chat_row_assistant_"] > [data-testid="stLayoutWrapper"] {
            width: 100% !important;
            max-width: 100% !important;
            margin-left: 0 !important;
            margin-right: auto !important;
            align-self: flex-start !important;
        }
        .st-key-chat_scroll [class*="st-key-chat_bubble_assistant_"] {
            width: min(326px, 100%) !important;
            max-width: 100% !important;
            box-sizing: border-box !important;
            padding: 14px 16px !important;
            border: 0 !important;
            border-radius: 14px 14px 14px 4px !important;
            background: #EEF3FA !important;
            color: #34445B !important;
            font-size: 14px !important;
            line-height: 1.6 !important;
            text-align: left !important;
            align-items: stretch !important;
            justify-content: center !important;
        }
        .st-key-chat_scroll [class*="st-key-chat_bubble_user_"] {
            width: max-content !important;
            max-width: 86% !important;
            min-width: 60px !important;
            flex: 0 1 auto !important;
            align-self: flex-end !important;
            margin-left: auto !important;
            box-sizing: border-box !important;
            padding: 10px 15px !important;
            border: 0 !important;
            border-radius: 14px 14px 4px 14px !important;
            background: var(--blue) !important;
            color: #fff !important;
            font-size: 14px !important;
            line-height: 1.55 !important;
            word-break: keep-all !important;
            overflow-wrap: break-word !important;
            text-align: left !important;
            align-items: stretch !important;
            justify-content: center !important;
        }
        .st-key-chat_scroll [class*="st-key-chat_bubble_assistant_"] [data-testid="stElementContainer"],
        .st-key-chat_scroll [class*="st-key-chat_bubble_user_"] [data-testid="stElementContainer"] {
            width: 100% !important;
            margin: 0 !important;
        }
        .st-key-chat_scroll [class*="st-key-chat_bubble_assistant_"] [data-testid="stMarkdownContainer"],
        .st-key-chat_scroll [class*="st-key-chat_bubble_user_"] [data-testid="stMarkdownContainer"] {
            width: 100% !important;
            margin: 0 !important;
            text-align: left !important;
        }
        .st-key-chat_scroll [class*="st-key-chat_bubble_assistant_"] p,
        .st-key-chat_scroll [class*="st-key-chat_bubble_user_"] p {
            margin: 0 0 8px !important;
            text-align: left !important;
            line-height: 1.6 !important;
        }
        .st-key-chat_scroll [class*="st-key-chat_bubble_user_"] p {
            word-break: keep-all !important;
            overflow-wrap: break-word !important;
        }
        .st-key-chat_scroll [class*="st-key-chat_bubble_assistant_"] p:last-child,
        .st-key-chat_scroll [class*="st-key-chat_bubble_user_"] p:last-child { margin-bottom: 0 !important; }
        .st-key-chat_scroll [class*="st-key-chat_bubble_assistant_"] ul,
        .st-key-chat_scroll [class*="st-key-chat_bubble_assistant_"] ol { margin: 6px 0 8px; padding-left: 20px; }
        .st-key-chat_scroll [class*="st-key-chat_bubble_assistant_"] li { margin: 3px 0; }
        .st-key-chat_scroll .suggestion-label { margin: 3px 0 8px; color: #71819A; font-size: 12px; }
        .st-key-chat_scroll [data-testid="stButton"] > button {
            min-height: 44px;
            border: 1px solid #BFCFEB !important;
            border-radius: 13px !important;
            background: #fff !important;
            color: var(--blue) !important;
            font-size: 13px !important;
            font-weight: 520 !important;
            line-height: 1.4;
            white-space: normal;
            text-align: left;
            box-shadow: none !important;
        }
        .st-key-chat_scroll [data-testid="stButton"] > button:hover { background: #F7F9FC !important; border-color: #8D9EB5 !important; }
        .st-key-chat_scroll .st-key-chat_suggested_question {
            width: min(326px, 100%) !important;
            max-width: 326px !important;
        }
        .st-key-chat_row_loading { width:100%; display:flex; flex-direction:column; align-items:flex-start; margin:0 0 14px; }
        .st-key-chat_loading_bubble {
            width: min(326px, 100%) !important;
            min-height: 76px;
            box-sizing: border-box;
            padding: 14px 16px !important;
            border-radius: 14px 14px 14px 4px !important;
            background: #EEF3FA !important;
            color: #34445B !important;
            font-size: 14px;
            line-height: 1.55;
        }
        .loading-copy { margin-bottom: 10px; }
        .typing-dots { height: 7px; display: flex; align-items: center; gap: 5px; }
        .typing-dots i { width: 6px; height: 6px; border-radius: 50%; background: #8D9EB5; animation: genie-dot 1.1s ease-in-out infinite; }
        .typing-dots i:nth-child(2) { animation-delay: .15s; }
        .typing-dots i:nth-child(3) { animation-delay: .3s; }
        @keyframes genie-dot { 0%, 70%, 100% { opacity: .35; transform: translateY(0); } 35% { opacity: 1; transform: translateY(-3px); } }
        .st-key-chat_error_state {
            width: 100% !important;
            box-sizing: border-box;
            margin: 0 0 14px !important;
            padding: 12px 14px !important;
            border: 1px solid var(--orange-line) !important;
            border-radius: 13px !important;
            background: var(--orange-bg) !important;
        }
        .chat-error-title { color: var(--orange); font-size: 13px; font-weight: 620; line-height: 1.45; }
        .chat-error-copy { margin-top: 3px; color: #77859A; font-size: 12px; line-height: 1.4; }
        .st-key-chat_error_state [data-testid="stButton"] { margin-top: 10px; }
        .st-key-chat_error_state [data-testid="stButton"] button {
            min-height: 34px !important;
            padding: 0 15px !important;
            border: 1px solid #BFCFEB !important;
            border-radius: 9px !important;
            background: #fff !important;
            color: var(--blue) !important;
            font-size: 12px !important;
            box-shadow: none !important;
        }
        .st-key-chat_pending_ticket_card {
            width: 100% !important;
            margin: 2px 0 14px !important;
            padding: 13px !important;
            box-sizing: border-box;
            border: 1px solid var(--input-line) !important;
            border-radius: 13px !important;
            background: #F7F9FC !important;
        }
        .ticket-check-title { margin-bottom: 9px; color: var(--title); font-size: 13px; font-weight: 620; }
        .ticket-check-details { display: grid; grid-template-columns: 58px 1fr; gap: 5px 8px; font-size: 12px; line-height: 1.45; }
        .ticket-check-details span { color: var(--muted); }
        .ticket-check-details strong { color: var(--body); font-weight: 550; }
        .ticket-warning { margin-top: 7px; color: #94651C; font-size: 11px; line-height: 1.45; }
        .st-key-chat_pending_ticket_card [data-testid="stButton"] button { min-height: 36px; font-size: 12px; }

        .st-key-chat_footer {
            flex: 0 0 auto !important;
            width: 100% !important;
            box-sizing: border-box !important;
            padding: 15px 14px 13px !important;
            border-top: 1px solid #E2E7EE !important;
            background: #fff !important;
        }
        .st-key-chat_footer [data-testid="stForm"] {
            width: 100% !important;
            box-sizing: border-box !important;
            margin: 0 !important;
            padding: 4px !important;
            border: 1px solid #DCE5F4 !important;
            border-radius: 13px !important;
            background: #F7F9FC !important;
        }
        .st-key-chat_footer [data-testid="stForm"] [data-testid="stHorizontalBlock"] { gap: 7px !important; align-items: center; }
        .st-key-chat_footer .st-key-chat_input_row [data-testid="stTextInput"] { flex: 1 1 0 !important; min-width: 0 !important; }
        .st-key-chat_footer [data-testid="stTextInput"] { margin: 0 !important; }
        .st-key-chat_footer .st-key-chat_input_row {
            padding-bottom: 19px !important;
        }
        .st-key-chat_footer [data-testid="stTextInput"] div:has(> [data-testid="InputInstructions"]) {
            top: calc(100% + 2px) !important;
            right: 0 !important;
            bottom: auto !important;
            left: 0 !important;
            width: 100% !important;
            height: 16px !important;
        }
        .st-key-chat_footer [data-testid="InputInstructions"] {
            position: static !important;
            display: flex !important;
            align-items: center !important;
            justify-content: flex-end !important;
            width: 100% !important;
            height: 16px !important;
            margin: 0 !important;
            padding: 0 43px 0 0 !important;
            box-sizing: border-box !important;
            color: #77859A !important;
            pointer-events: none !important;
        }
        .st-key-chat_footer [data-testid="InputInstructions"] span:not(:last-child) {
            display: none !important;
        }
        .st-key-chat_footer [data-testid="InputInstructions"] span:last-child {
            color: #77859A !important;
            font-size: 11px !important;
            line-height: 16px !important;
            white-space: nowrap !important;
        }
        .st-key-chat_footer .chat-char-count {
            box-sizing: border-box;
            height: 16px;
            margin-top: 2px;
            padding-right: 48px;
            color: #77859A;
            font-size: 11px;
            line-height: 16px;
            text-align: right;
        }
        .st-key-chat_footer:has([data-testid="InputInstructions"]) .chat-char-count {
            visibility: hidden;
        }
        .st-key-chat_footer [data-testid="stTextInput"] div,
        .st-key-chat_footer [data-testid="stTextInput"] input,
        .st-key-chat_footer [data-testid="stTextInput"] input:focus {
            border: 0 !important;
            outline: 0 !important;
            box-shadow: none !important;
            background: transparent !important;
        }
        .st-key-chat_footer [data-testid="stTextInput"] input {
            min-height: 37px !important;
            padding: 0 9px !important;
            color: #34445B !important;
            font-size: 12px !important;
        }
        .st-key-chat_footer [data-testid="stTextInput"] input::placeholder { color: #8D9EB5 !important; opacity: 1; }
        .st-key-chat_footer [data-testid="stForm"] [data-testid="stFormSubmitButton"] button,
        .st-key-chat_footer [data-testid="stForm"] button[kind="secondary"] {
            position: relative;
            width: 36px !important;
            min-width: 36px !important;
            max-width: 36px !important;
            height: 36px !important;
            min-height: 36px !important;
            padding: 0 !important;
            border: 0 !important;
            border-radius: 10px !important;
            background: #DCE5F4 !important;
            color: #5A77A9 !important;
            box-shadow: none !important;
        }
        .st-key-chat_footer [data-testid="stForm"] button [data-testid="stMarkdownContainer"] p {
            position: absolute !important;
            width: 1px !important;
            height: 1px !important;
            padding: 0 !important;
            margin: -1px !important;
            overflow: hidden !important;
            clip: rect(0, 0, 0, 0) !important;
            white-space: nowrap !important;
            border: 0 !important;
        }
        .st-key-chat_footer .chat-reset-note { margin-top: 13px; color: #77859A; font-size: 11px; line-height: 1.35; text-align: center; }
        .st-key-chat_footer [data-testid="stForm"] button:disabled { opacity: .52; }
        .st-key-chat_panel [data-testid="stButton"] button:focus-visible,
        .st-key-chat_footer [data-testid="stTextInput"] input:focus-visible {
            outline: 2px solid #8D9EB5 !important;
            outline-offset: 2px;
        }

        @media (max-width: 900px) {
            :root { --site-header-gutter: 20px; }
            .dashboard-main { width: calc(100% - 40px); }
            .st-key-admin_home_main { width: calc(100% - 40px) !important; }
            .dashboard-grid { grid-template-columns: minmax(0, 1.15fr) minmax(0, 1fr); gap: 16px; }
            .dashboard-card { padding: 22px 20px 18px; }
        }
        @media (max-width: 680px) {
            :root { --site-header-height: 68px; --site-header-gutter: 16px; }
            .dashboard-main { width: calc(100% - 32px); }
            .st-key-admin_home_main { width: calc(100% - 32px) !important; }
            .header-logo { width: 32px; height: 32px; flex-basis: 32px; }
            .auth-brand strong { font-size: 20px; }
            .st-key-site_header, .st-key-site_header [data-testid="stHorizontalBlock"] { gap: 8px; }
            .dashboard-main, .st-key-admin_home_main { padding-top: 30px; }
            .st-key-admin_home_profile {
                border: 1px solid var(--line); border-radius: 17px; background: var(--card);
            }
            .st-key-admin_home_profile .profile-card { border: 0; }
            .st-key-admin_home_profile .profile-copy { max-width: 35%; }
            .st-key-admin_home_profile > [data-testid="stLayoutWrapper"]:has(> .st-key-admin_home_action) {
                position: static; margin: 0 20px 20px auto;
            }
            .st-key-admin_home_action button { min-height: 44px; }
            .st-key-admin_home_action button p { font-size: 16px; }
            .welcome-block { margin-bottom: 24px; }
            .welcome-block h1 { font-size: 25px; }
            .welcome-block p { max-width: 250px; font-size: 13px; }
            .welcome-block .dashboard-date { padding-top: 6px; font-size: 12px; }
            .profile-card { height: auto; min-height: 150px; gap: 16px; padding: 20px; }
            .employee-placeholder { width: 76px; height: 76px; flex-basis: 76px; border-radius: 16px; }
            .employee-placeholder svg { width: 48px; height: 48px; }
            .profile-copy h2 { font-size: 25px; }
            .profile-meta { flex-wrap: wrap; gap: 8px; font-size: 13px; }
            .leave-summary { left: auto; right: 20px; top: 60px; min-width: 0; }
            .leave-summary .eyebrow { font-size: 12px; }
            .leave-number { font-size: 24px; }
            .leave-unit { font-size: 15px; }
            .leave-value-unavailable { font-size: 12px; }
            .dashboard-grid { grid-template-columns: 1fr; gap: 14px; }
            .dashboard-card { min-height: auto; padding: 20px 18px 16px; }
            .notice-row, .equipment-row { min-height: 58px; font-size: 13px; }
            .dashboard-help { margin: 18px 0 82px; font-size: 12px; }
            .st-key-chat_fab { right: 20px !important; bottom: 20px !important; }
            .st-key-chat_panel {
                left: 16px !important;
                right: 16px !important;
                bottom: 100px !important;
                width: auto !important;
                min-width: 0 !important;
                max-width: none !important;
                height: min(538px, calc(100dvh - 126px)) !important;
                min-height: 380px !important;
                max-height: 538px !important;
            }
            .st-key-chat_scroll { height: auto !important; }
        }
        @media (max-width: 430px) {
            .manager-brand > span { display: none; }
        }
        @media (prefers-reduced-motion: reduce) {
            *, *::before, *::after { scroll-behavior: auto !important; animation-duration: .01ms !important; animation-iteration-count: 1 !important; transition-duration: .01ms !important; }
        }
        </style>
        """,
        unsafe_allow_html=True,
    )


# ---------------------------------------------------------------------------
# 로그인 세션
# 신원(company_id, account_id, role)은 st.session_state["auth"]에만 둔다.
# URL 쿼리나 화면 입력으로 바꿀 수 있는 경로는 없다.
# ---------------------------------------------------------------------------


def _render_shell_styles() -> None:
    """메뉴형 화면 공통 스타일. 팀 디자인 토큰(_render_styles의 :root)을 그대로 쓴다."""
    st.markdown(
        r"""
        <style>
        :root {
            /* 기존 값에 이름만 붙인 토큰. 새 색은 추가하지 않는다. */
            --blue-soft: #EDF3FD;
            --blue-hover: #315EA9;
            --radius-card: 20px;
            --radius-section: 16px;
            --radius-control: 10px;
            --radius-pill: 999px;
            --shadow-card: 0 12px 32px rgba(32, 45, 64, 0.06);
            --shadow-soft: 0 1px 3px rgba(32, 45, 64, 0.018);
            --shadow-panel: 0 15px 42px rgba(37, 57, 87, 0.17);
            --shadow-fab: 0 7px 17px rgba(56, 104, 190, 0.27);
            --tabbar-height: 64px;
            --safe-bottom: env(safe-area-inset-bottom, 0px);
        }
        /* 한글은 어절 단위로 줄바꿈하고, 긴 영문·숫자는 넘치지 않게 끊는다. */
        [data-testid="stAppViewContainer"] :is(p, h1, h2, h3, h4, li, span, label, summary, dd, dt, td, th, button) {
            word-break: keep-all;
            overflow-wrap: break-word;
        }
        .st-key-cookie_sync { position: absolute !important; width: 1px; height: 1px; overflow: hidden; opacity: 0; pointer-events: none; }

        /* ---- 상단 헤더: 로고 + 메뉴 + 사용자 ---- */
        .st-key-dashboard_root .st-key-site_header {
            position: sticky; top: 0; z-index: 999;
            border-width: 0 0 1px 0;
        }
        .st-key-top_nav { gap: 4px !important; flex: 0 1 auto; min-width: 0; }
        .st-key-top_nav button {
            min-height: 44px; padding: 0 16px !important;
            border: 0 !important; border-radius: var(--radius-pill) !important;
            background: transparent !important; color: var(--muted) !important;
            white-space: nowrap;
        }
        .st-key-top_nav button p { font-size: 15px; font-weight: 600; }
        .st-key-top_nav button:hover { background: var(--page) !important; color: var(--title) !important; }
        .st-key-top_nav button[data-testid="stBaseButton-primary"] {
            background: var(--blue-soft) !important; color: var(--blue) !important;
        }
        .st-key-top_nav button[data-testid="stBaseButton-primary"] p { font-weight: 700; }
        .st-key-header_user { gap: 12px !important; }
        .header-user { color: var(--body); font-size: 14px; font-weight: 600; white-space: nowrap; }
        .st-key-header_user [data-testid="stMarkdownContainer"] { margin-bottom: 0; }
        .st-key-header_user .st-key-logout_button button {
            min-height: 36px !important; padding: 4px 18px !important;
            border: 0 !important; border-radius: var(--radius-pill) !important;
            background: var(--blue-soft) !important; color: var(--blue) !important; white-space: nowrap;
        }
        .st-key-header_user .st-key-logout_button button p { font-size: 13px; }

        /* ---- 본문 ---- */
        .st-key-page_main {
            width: min(1280px, calc(100% - 2 * var(--site-header-gutter))) !important;
            margin: 0 auto !important;
            padding: 40px 0 72px !important;
            gap: 20px;
        }
        .st-key-page_main .dashboard-main { width: 100%; padding-top: 0; }
        .page-heading h1 {
            margin: 0; padding: 0 !important; color: var(--title);
            font-size: clamp(24px, 2.4vw, 29px); font-weight: 720; letter-spacing: -0.8px; line-height: 1.35;
        }
        .page-heading p { margin: 8px 0 0; color: var(--muted); font-size: 15px; line-height: 1.55; }
        .st-key-page_main [data-testid="stMarkdownContainer"]:has(> .page-heading) { margin-bottom: 4px; }
        .st-key-page_main [data-testid="stTabs"] [role="tablist"] { gap: 8px; }
        .st-key-page_main [data-testid="stTabs"] [role="tab"] { min-height: 48px; padding-inline: 14px; color: var(--muted); }
        .st-key-page_main [data-testid="stTabs"] [role="tab"][aria-selected="true"] { color: var(--blue); font-weight: 700; }
        .st-key-page_main [data-testid="stTabs"] [role="tabpanel"] { padding-top: 18px; }

        /* ---- 홈: 사원증 + 위젯 ---- */
        .st-key-home_layout {
            display: grid !important;
            grid-template-columns: minmax(280px, 340px) minmax(0, 1fr);
            align-items: start;
            gap: 24px;
        }
        .st-key-home_id_column { position: sticky; top: 104px; }
        .id-card {
            overflow: hidden;
            background: var(--card);
            border: 1px solid var(--line);
            border-radius: var(--radius-card);
            box-shadow: var(--shadow-card);
        }
        .id-card-top {
            display: flex; justify-content: space-between; align-items: center; gap: 12px;
            min-height: 56px; padding: 14px 22px;
            background: var(--blue); color: #fff;
        }
        .id-company { font-size: 15px; font-weight: 700; letter-spacing: -0.2px; min-width: 0; }
        .id-kind { flex: 0 0 auto; font-size: 12px; font-weight: 600; opacity: .85; letter-spacing: .08em; }
        .id-card-body { display: flex; flex-direction: column; align-items: center; padding: 28px 24px 22px; text-align: center; }
        .id-avatar {
            width: 104px; height: 104px; display: grid; place-items: center;
            border-radius: 50%; background: var(--blue-soft); color: var(--blue);
            font-size: 42px; font-weight: 750; letter-spacing: -1px;
            border: 4px solid var(--card); box-shadow: 0 0 0 1px var(--input-line);
        }
        h2.id-name {
            margin: 18px 0 0; padding: 0 !important; color: var(--title);
            font-size: 28px; font-weight: 750; letter-spacing: -0.8px; line-height: 1.3;
        }
        .id-meta { margin: 6px 0 0; color: var(--body); font-size: 15px; font-weight: 600; }
        .id-job { margin: 4px 0 0; color: var(--muted); font-size: 14px; }
        .id-number {
            display: flex; flex-wrap: wrap; justify-content: center; align-items: center; gap: 8px;
            margin-top: 18px; padding: 10px 14px; width: 100%; box-sizing: border-box;
            border-radius: var(--radius-control); background: var(--page);
        }
        .id-number span { color: var(--muted); font-size: 13px; }
        .id-number strong { color: var(--title); font-size: 16px; letter-spacing: .06em; }
        .id-badge {
            display: inline-flex; align-items: center; min-height: 26px; padding: 0 10px;
            border-radius: var(--radius-pill); background: var(--amber-bg); color: var(--amber) !important;
            font-size: 12px !important; font-weight: 700; letter-spacing: 0 !important;
        }
        .id-code { width: 100%; margin-top: 18px; color: var(--title); }
        .id-barcode { display: block; width: 100%; height: 40px; opacity: .82; }
        .id-code small { display: block; margin-top: 6px; color: var(--muted); font-size: 11px; }

        .st-key-home_widgets {
            display: grid !important;
            grid-template-columns: repeat(2, minmax(0, 1fr));
            align-items: stretch;
            gap: 20px;
        }
        [class*="st-key-home_widget_"], .st-key-admin_company_card {
            box-sizing: border-box;
            min-height: 220px;
            padding: 22px 22px 14px;
            background: var(--card);
            border: 1px solid var(--line);
            border-radius: 17px;
            box-shadow: var(--shadow-soft);
            gap: 0 !important;
        }
        [class*="st-key-home_widget_"] [data-testid="stMarkdownContainer"] { margin-bottom: 0; }
        .widget-head { display: flex; justify-content: space-between; align-items: baseline; gap: 12px; margin-bottom: 12px; }
        .widget-head h3 { margin: 0; padding: 0 !important; color: var(--title); font-size: 18px; font-weight: 700; letter-spacing: -0.3px; }
        .widget-note { margin: 6px 0 0; color: var(--muted); font-size: 13px; line-height: 1.5; }
        .widget-label { margin: 14px 0 0; color: var(--muted); font-size: 12px; font-weight: 600; }
        .status-chips { display: flex; flex-wrap: wrap; gap: 6px; }
        .status-chips .status-badge { margin-left: 0; min-height: 28px; }
        .equipment-list.compact { margin-top: 4px; border-top: 0; }
        .equipment-list.compact .equipment-row { min-height: 56px; }
        ul.widget-list { list-style: none; margin: 0; padding: 0; border-top: 1px solid #E7ECF3; }
        ul.widget-list .notice-row { min-height: 52px; font-size: 14px; }
        .notice-title { min-width: 0; display: inline-flex; align-items: center; gap: 6px; flex-wrap: wrap; }
        .new-badge {
            display: inline-flex; align-items: center; min-height: 20px; padding: 0 7px;
            border-radius: var(--radius-pill); background: var(--blue); color: #fff;
            font-size: 10px; font-style: normal; font-weight: 750; letter-spacing: .04em;
        }
        ul.task-list { list-style: none; margin: 10px 0 0; padding: 0; }
        ul.task-list li { display: flex; gap: 10px; align-items: flex-start; padding: 7px 0; color: var(--body); font-size: 14px; line-height: 1.5; }
        .task-box { flex: 0 0 16px; width: 16px; height: 16px; margin-top: 3px; border: 1.5px solid #B8C6DB; border-radius: 5px; background: var(--card); }
        [class*="st-key-home_more_"] { margin-top: auto !important; align-self: flex-end; }
        [class*="st-key-home_more_"] button { min-height: 44px; padding: 0 6px !important; color: var(--blue) !important; }
        [class*="st-key-home_more_"] button p { font-size: 14px; font-weight: 600; }
        .dashboard-help { margin: 0; color: #71819A; font-size: 13px; }

        /* ---- 관리자 홈 ---- */
        .st-key-admin_home_layout {
            display: grid !important;
            grid-template-columns: minmax(280px, 340px) minmax(0, 1fr);
            align-items: start; gap: 24px;
        }
        .st-key-admin_company_card { min-height: 0; padding: 24px; border-radius: var(--radius-card); box-shadow: var(--shadow-card); }
        h2.company-name { margin: 4px 0 0; padding: 0 !important; color: var(--title); font-size: 24px; font-weight: 750; letter-spacing: -0.6px; }
        .st-key-admin_company_card .st-key-home_company_code { margin-top: 18px; }
        .st-key-home_widget_preview [data-testid="stSelectbox"] { margin-top: 12px; }
        .st-key-preview_open button { min-height: 44px; }
        .st-key-preview_banner {
            position: sticky; top: 80px; z-index: 5;
            padding: 10px 16px; border-radius: var(--radius-control);
            background: var(--amber-bg); border: 1px solid #EED9AE;
        }
        .preview-label { color: var(--amber); font-size: 14px; font-weight: 700; }
        .st-key-preview_close button { min-height: 40px; background: var(--card); border-color: #EED9AE; color: var(--amber); }

        /* ---- 공지사항 ---- */
        .st-key-notice_board, .st-key-library_board, .st-key-equipment_board {
            padding: 24px; background: var(--card); border: 1px solid var(--line);
            border-radius: var(--radius-section); box-shadow: var(--shadow-soft); gap: 12px !important;
        }
        .list-count { margin: 4px 0 8px; color: var(--muted); font-size: 13px; }
        .notice-page-entry summary.notice-row { min-height: 60px; padding: 4px 4px; }
        .notice-page-entry summary.notice-row:hover { background: var(--page); }
        .notice-page-entry[open] summary .notice-title { color: var(--blue); font-weight: 650; }
        .notice-page-entry .notice-body { padding: 4px 4px 20px; line-height: 1.7; }
        .empty-state {
            display: flex; flex-direction: column; align-items: center; gap: 6px;
            padding: 40px 16px; text-align: center; color: var(--muted); font-size: 14px;
        }
        .empty-state strong { color: var(--body); font-size: 15px; }
        [class*="st-key-notice_admin_row_"] {
            min-height: 64px; padding: 6px 4px; border-bottom: 1px solid #E7ECF3;
        }
        [class*="st-key-notice_admin_row_"] > div:first-child { flex: 1 1 auto !important; min-width: 0; }
        .notice-admin-title { display: flex; flex-direction: column; gap: 2px; min-width: 0; }
        .notice-admin-title strong { color: var(--title); font-size: 15px; font-weight: 600; }
        .notice-admin-title span { color: var(--muted); font-size: 12px; }
        [class*="st-key-notice_admin_row_"] button { min-height: 40px; white-space: nowrap; }
        .st-key-notice_inline_form {
            margin: 8px 0 12px; padding: 18px 20px; border-radius: 12px;
            background: var(--page); border: 1px solid var(--input-line);
        }

        /* ---- 자료실 ---- */
        [class*="st-key-lib_"][class*="_row_"], [class*="st-key-admin_res_row_"] {
            min-height: 72px; padding: 10px 4px; border-bottom: 1px solid #E7ECF3;
        }
        [class*="st-key-lib_"][class*="_row_"] > div:nth-child(2),
        [class*="st-key-admin_res_row_"] > div:nth-child(2) { flex: 1 1 auto !important; min-width: 0; }
        .resource-icon {
            display: inline-grid; place-items: center; width: 44px; height: 44px; flex: 0 0 44px;
            border-radius: 11px; background: var(--blue-soft); color: var(--blue);
            font-size: 10px; font-weight: 800; letter-spacing: .04em; text-transform: uppercase;
        }
        .resource-text { display: flex; flex-direction: column; gap: 3px; min-width: 0; }
        .resource-title { display: flex; flex-wrap: wrap; align-items: center; gap: 6px; }
        .resource-title strong { color: var(--title); font-size: 15px; font-weight: 650; }
        .resource-chip {
            display: inline-flex; align-items: center; min-height: 22px; padding: 0 8px;
            border-radius: var(--radius-pill); background: var(--page); color: var(--muted);
            font-size: 11px; font-weight: 650;
        }
        .resource-chip.dept { background: var(--green-bg); color: var(--green); }
        .resource-chip.muted { background: #EEF2F6; color: var(--muted); }
        .resource-desc { margin: 0; color: var(--body); font-size: 13px; line-height: 1.5; }
        .resource-meta { color: var(--muted); font-size: 12px; }
        [class*="st-key-lib_"] :is(button, a), [class*="st-key-admin_res_"] :is(button, a) { min-height: 40px; white-space: nowrap; }
        details.handover-card, details.policy-group {
            margin: 8px 0; border: 1px solid var(--line); border-radius: 12px; background: var(--card);
        }
        details.handover-card > summary, details.policy-group > summary {
            display: flex; align-items: center; gap: 12px; min-height: 60px; padding: 8px 14px;
            cursor: pointer; list-style: none;
        }
        details.handover-card > summary::-webkit-details-marker,
        details.policy-group > summary::-webkit-details-marker { display: none; }
        details.policy-group > summary strong { color: var(--title); font-size: 15px; }
        details.policy-group > summary .card-count { margin-left: auto; padding-top: 0; }
        details.policy-group .notice-list { margin: 0 14px 8px; }
        .handover-body { padding: 0 18px 16px 70px; color: var(--body); font-size: 14px; }
        .handover-body h4 { margin: 12px 0 4px; padding: 0 !important; font-size: 14px; color: var(--title); }
        .handover-body ul { margin: 0; padding-left: 18px; line-height: 1.7; }
        .ref-badge, .own-badge {
            display: inline-flex; align-items: center; min-height: 22px; padding: 0 8px;
            border-radius: var(--radius-pill); font-size: 11px; font-style: normal; font-weight: 700;
        }
        .ref-badge { background: var(--amber-bg); color: var(--amber); }
        .own-badge { background: var(--blue-soft); color: var(--blue); }
        .st-key-resource_form {
            margin: 8px 0 16px; padding: 18px 20px; border-radius: 12px;
            background: var(--page); border: 1px solid var(--input-line);
        }
        .st-key-resource_delete_confirm {
            padding: 10px 14px; border-radius: var(--radius-control);
            background: var(--amber-bg); border: 1px solid #EED9AE;
        }

        /* ---- 비품 신청 ---- */
        .st-key-equipment_layout {
            display: grid !important;
            grid-template-columns: minmax(0, 1fr) minmax(0, 1.1fr);
            align-items: start; gap: 24px;
        }
        .st-key-equipment_form, .st-key-equipment_history {
            padding: 22px; background: var(--card); border: 1px solid var(--line);
            border-radius: var(--radius-section); box-shadow: var(--shadow-soft); gap: 14px !important;
        }
        .st-key-equipment_form [data-testid="stPills"] button { min-height: 44px; padding-inline: 16px; }
        .st-key-equipment_form :is([data-testid="stNumberInput"] input, [data-testid="stTextInput"] input) { min-height: 44px; }
        .st-key-equipment_confirm_card {
            padding: 16px 18px; border-radius: 12px; background: var(--blue-soft);
            border: 1px solid var(--input-line); gap: 10px !important;
        }
        .st-key-equipment_confirm_card button, .st-key-eq_check button { min-height: 48px; }
        .st-key-equipment_confirm_card .ticket-check-title { color: var(--title); font-size: 15px; font-weight: 700; margin-bottom: 10px; }
        .st-key-equipment_confirm_card .ticket-check-details {
            display: grid; grid-template-columns: max-content minmax(0, 1fr); gap: 8px 16px;
            color: var(--body); font-size: 14px;
        }
        .st-key-equipment_confirm_card .ticket-check-details strong { color: var(--title); font-weight: 600; overflow-wrap: anywhere; }
        .st-key-equipment_confirm_card .ticket-warning { margin-top: 10px; padding: 8px 12px; border-radius: 8px; background: var(--amber-bg); color: var(--amber); font-size: 13px; }
        details.my-ticket { border-bottom: 1px solid #E7ECF3; }
        details.my-ticket > summary {
            display: flex; align-items: center; gap: 12px; min-height: 68px; padding: 6px 2px;
            cursor: pointer; list-style: none;
        }
        details.my-ticket > summary::-webkit-details-marker { display: none; }
        details.my-ticket > summary .resource-text { flex: 1 1 auto; }
        .my-ticket-body { padding: 0 4px 14px 54px; }
        ol.ticket-timeline { list-style: none; margin: 8px 0 0; padding: 0; position: relative; }
        ol.ticket-timeline::before { content: ""; position: absolute; left: 5px; top: 8px; bottom: 8px; width: 2px; background: var(--line); }
        .timeline-step { position: relative; display: flex; gap: 12px; padding: 6px 0; }
        .timeline-step > i { position: relative; z-index: 1; flex: 0 0 12px; height: 12px; margin-top: 4px; border-radius: 50%; background: currentColor; box-shadow: 0 0 0 3px var(--card); }
        .timeline-step strong { color: var(--title); font-size: 14px; font-weight: 650; margin-right: 8px; }
        .timeline-step time { color: var(--muted); font-size: 12px; }
        .timeline-step p { margin: 2px 0 0; color: var(--body); font-size: 13px; }
        .timeline-step.status-paid { color: var(--green); }
        .timeline-step.status-scheduled { color: var(--amber); }
        .timeline-step.status-received, .timeline-step.status-review { color: #5A77A9; }
        .timeline-step.status-rejected { color: #A34D46; }

        /* ---- 반응형: 위 영역들의 좁은 화면 규칙(기본 규칙 뒤에 둔다) ---- */
        @media (max-width: 1023.98px) {
            .st-key-home_widgets { grid-template-columns: minmax(0, 1fr); }
        }
        @media (max-width: 767.98px) {
            .st-key-home_layout, .st-key-admin_home_layout { grid-template-columns: minmax(0, 1fr); gap: 16px; }
            .st-key-home_id_column { position: static; }
            .id-card-body { padding: 22px 20px 18px; }
            .id-avatar { width: 88px; height: 88px; font-size: 36px; }
            h2.id-name { font-size: 25px; }
            .st-key-home_widgets { gap: 14px; }
            [class*="st-key-home_widget_"] { min-height: 0; padding: 18px 18px 8px; }
            .st-key-preview_banner { top: 60px; }
            .st-key-notice_board, .st-key-library_board, .st-key-equipment_board { padding: 16px; }
            [class*="st-key-notice_admin_row_"] { flex-wrap: wrap !important; }
            [class*="st-key-notice_admin_row_"] > div:first-child { flex-basis: 100% !important; }
            [class*="st-key-lib_"][class*="_row_"], [class*="st-key-admin_res_row_"] { flex-wrap: wrap !important; }
            [class*="st-key-lib_"][class*="_row_"] > div:nth-child(2),
            [class*="st-key-admin_res_row_"] > div:nth-child(2) { flex-basis: calc(100% - 60px) !important; }
            .handover-body { padding-left: 18px; }
            .st-key-equipment_layout { grid-template-columns: minmax(0, 1fr); gap: 16px; }
            .st-key-equipment_form, .st-key-equipment_history { padding: 16px; }
            .my-ticket-body { padding-left: 4px; }
        }

        /* ---- 모바일 하단 탭바: PC에서는 숨긴다 ---- */
        .st-key-bottom_tabs { display: none !important; }

        @media (max-width: 767.98px) {
            .st-key-top_nav { display: none !important; }
            .st-key-dashboard_root .st-key-site_header { min-height: 60px; }
            .st-key-header_user { gap: 8px !important; }
            .st-key-header_user .st-key-logout_button button { min-height: 36px !important; padding: 4px 12px !important; }
            .st-key-page_main {
                padding: 24px 0 calc(var(--tabbar-height) + 104px + var(--safe-bottom)) !important;
                gap: 16px;
            }
            .page-heading p { font-size: 14px; }
            .st-key-bottom_tabs {
                display: flex !important;
                position: fixed !important;
                z-index: 10000;
                left: 0; right: 0; bottom: 0;
                margin: 0 !important;
                padding: 4px 4px calc(4px + var(--safe-bottom)) !important;
                background: var(--card);
                border-top: 1px solid var(--line);
                box-shadow: 0 -4px 16px rgba(32, 45, 64, 0.06);
                gap: 0 !important;
            }
            .st-key-bottom_tabs > div { flex: 1 1 0 !important; min-width: 0 !important; width: auto !important; }
            .st-key-bottom_tabs button {
                width: 100%; min-height: 56px !important; padding: 4px 2px !important;
                display: flex; flex-direction: column; align-items: center; justify-content: center; gap: 2px;
                border: 0 !important; border-radius: 12px !important;
                background: transparent !important; color: var(--muted) !important;
            }
            .st-key-bottom_tabs button > div, .st-key-bottom_tabs button > span { flex-direction: column; gap: 2px; }
            .st-key-bottom_tabs button [data-testid="stIconMaterial"] { font-size: 24px; margin: 0 !important; }
            .st-key-bottom_tabs button p { font-size: 12px; font-weight: 600; line-height: 1.2; white-space: nowrap; }
            .st-key-bottom_tabs button[data-testid="stBaseButton-primary"] { color: var(--blue) !important; }
            .st-key-bottom_tabs button[data-testid="stBaseButton-primary"] p { font-weight: 750; }

            /* 챗봇 버튼은 탭바 위로 올린다(겹침 방지). */
            .st-key-chat_fab {
                right: 16px !important;
                bottom: calc(var(--tabbar-height) + 16px + var(--safe-bottom)) !important;
                width: 56px !important; height: 56px !important; min-width: 56px !important;
            }
            .st-key-chat_fab [data-testid="stButton"],
            .st-key-chat_fab button {
                width: 56px !important; min-width: 56px !important; max-width: 56px !important;
                height: 56px !important; min-height: 56px !important;
            }
            /* 챗봇은 화면 전체를 덮는 시트로 연다. */
            .st-key-chat_panel {
                inset: 0 !important;
                z-index: 10003 !important;
                width: 100vw !important; min-width: 0 !important; max-width: none !important;
                height: 100dvh !important; min-height: 0 !important; max-height: none !important;
                border: 0 !important; border-radius: 0 !important;
                padding-bottom: var(--safe-bottom) !important;
            }
            .st-key-chat_scroll { height: auto !important; flex: 1 1 auto !important; }
            .st-key-chat_close_button { width: 44px !important; height: 44px !important; top: 15px !important; }
            .st-key-chat_close_button [data-testid="stButton"],
            .st-key-chat_close_button button {
                width: 44px !important; min-width: 44px !important; height: 44px !important; min-height: 44px !important;
            }
        }
        </style>
        """,
        unsafe_allow_html=True,
    )


def _touch_activity() -> None:
    st.session_state.last_activity = time.monotonic()


def _session_expired() -> bool:
    if not st.session_state.get("auth"):
        return False
    last = st.session_state.get("last_activity")
    return last is not None and time.monotonic() - last > SESSION_TIMEOUT_SEC


def _logout(notice: str = "") -> None:
    """세션 전체(신원·대화·대기 중인 요청)를 지우고 서버의 로그인 세션도 폐기한다."""
    token = st.session_state.get("session_token")
    if token:
        get_agent().logout(token)  # 연결 실패는 call_tool이 응답으로 돌려준다
    previous_screen = st.session_state.get("_screen_slot")
    for key in list(st.session_state.keys()):
        del st.session_state[key]
    if previous_screen is not None:
        # Keep only the previous display slot so its content can be removed on logout.
        st.session_state["_screen_slot"] = previous_screen
    st.session_state.auth_view = "login"
    # 이 연결의 쿠키 값은 이미 폐기되었으므로 다시 복원하지 않고, 브라우저 쿠키도 지운다.
    st.session_state.resume_checked = True
    st.session_state.cookie_action = ("clear", "")
    if notice:
        st.session_state.auth_notice = notice


def _session_cookie_token() -> str:
    try:
        token = st.context.cookies.get(SESSION_COOKIE) or ""
    except Exception:  # AppTest 등 쿠키가 없는 실행 환경
        return ""
    return token if isinstance(token, str) and _TOKEN_RE.fullmatch(token) else ""


def _restore_login() -> None:
    """새로고침 후: 브라우저 쿠키의 세션 토큰을 서버에서 확인해 로그인 상태를 되살린다.

    쿠키에는 무작위 토큰만 있고, 신원·역할은 서버가 accounts에서 다시 정한다.
    """
    state = st.session_state
    if state.get("auth") or state.get("resume_checked"):
        return
    state.resume_checked = True
    token = _session_cookie_token()
    if not token:
        return
    result = get_agent().resume_session(token)
    if not result.get("ok"):
        if result.get("code") == "INVALID_SESSION":
            state.cookie_action = ("clear", "")
        return
    try:
        auth = _auth_from_login(result["data"])
    except (KeyError, TypeError) as exc:
        _log_post_login_failure("resume_response", exc, None)
        return
    state.auth = auth
    state.session_token = token
    state.session_checked_at = time.monotonic()
    _touch_activity()


def _session_still_valid(agent: OfficeGenieAgent) -> bool:
    """몇 분에 한 번 서버 세션을 확인·연장한다. 관리자가 비밀번호를 초기화했거나
    계정이 비활성화되면 False. 일시적인 연결 오류로는 로그아웃시키지 않는다."""
    token = st.session_state.get("session_token")
    if not token:
        return True
    if time.monotonic() - st.session_state.get("session_checked_at", 0) < SESSION_KEEPALIVE_SEC:
        return True
    result = agent.resume_session(token)
    if result.get("code") == "SERVER_UNAVAILABLE":
        return True
    st.session_state.session_checked_at = time.monotonic()
    return bool(result.get("ok"))


def _apply_cookie_action() -> None:
    """로그인·로그아웃 직후 한 번만 브라우저 쿠키를 쓰거나 지운다.

    쿠키 값은 서버가 만든 무작위 토큰뿐이다. SameSite=Strict, HTTPS면 Secure.
    """
    action = st.session_state.pop("cookie_action", None)
    if not action:
        return
    kind, token = action
    if kind == "set" and _TOKEN_RE.fullmatch(token or ""):
        cookie = f"{SESSION_COOKIE}={token}; Path=/; Max-Age={SESSION_COOKIE_MAX_AGE}; SameSite=Strict"
    else:
        cookie = f"{SESSION_COOKIE}=; Path=/; Max-Age=0; SameSite=Strict"
    with st.container(key="cookie_sync", height=1, border=False):
        st.html(
            f"<script>document.cookie = {json.dumps(cookie)} + "
            "(location.protocol === 'https:' ? '; Secure' : '');</script>",
            unsafe_allow_javascript=True,
        )


def _on_logout_click() -> None:
    _logout()
    # 직접 로그아웃하면 다음 사람이 홈에서 시작한다(시간 초과 로그아웃은 보던 메뉴를 유지).
    if "page" in st.query_params:
        del st.query_params["page"]


@st.fragment(run_every=60)
def _idle_watch() -> None:
    """아무 조작이 없어도 30분이 지나면 화면을 로그인으로 되돌린다."""
    if _session_expired():
        st.rerun(scope="app")


# ---------------------------------------------------------------------------
# 메뉴와 화면 이동
# 현재 메뉴는 URL 쿼리 ?page= 와 동기화한다(새로고침·뒤로가기·바로가기).
# page 값은 "어떤 화면을 보여 줄지"만 정한다. 신원·권한은 세션(auth)과 서버가 정한다.
# ---------------------------------------------------------------------------

# (page, PC 메뉴 이름, 모바일 탭 이름, 아이콘)
EMPLOYEE_NAV = (
    ("home", "홈", "홈", ":material/badge:"),
    ("notices", "공지사항", "공지", ":material/campaign:"),
    ("library", "자료실", "자료실", ":material/folder_open:"),
    ("equipment", "비품 신청", "비품", ":material/inventory_2:"),
)
ADMIN_NAV = (
    ("home", "홈", "홈", ":material/space_dashboard:"),
    ("notices", "공지사항 관리", "공지", ":material/campaign:"),
    ("library", "자료실 관리", "자료실", ":material/folder_open:"),
    ("equipment", "비품 관리", "비품", ":material/inventory_2:"),
    ("employees", "사원", "사원", ":material/group:"),
    ("data", "데이터·계정", "설정", ":material/settings:"),
)
ADMIN_ONLY_PAGES = frozenset(p for p, *_ in ADMIN_NAV) - frozenset(p for p, *_ in EMPLOYEE_NAV)
PAGE_DENIED_NOTICE = "관리자만 사용할 수 있는 메뉴입니다. 홈으로 이동했습니다."


def _nav_for(auth: dict) -> tuple:
    return ADMIN_NAV if auth.get("role") == "admin" else EMPLOYEE_NAV


def _resolve_page(auth: dict) -> tuple[str, str | None]:
    """URL의 page 값을 역할별 허용 목록과 대조한다. (보여 줄 page, 차단한 관리자 page)."""
    allowed = {page for page, *_ in _nav_for(auth)}
    requested = str(st.query_params.get("page") or "home")[:40]
    page = requested if requested in allowed else "home"
    denied = requested if requested != page and requested in ADMIN_ONLY_PAGES else None
    if st.query_params.get("page") != page:
        st.query_params["page"] = page
    return page, denied


def _go_page(page: str) -> None:
    _touch_activity()
    st.query_params["page"] = page


def _brand_html(*, admin: bool = False) -> str:
    brand = (
        "<div class='auth-brand'>"
        "<span class='header-logo'><svg viewBox='0 0 32 32' fill='none' aria-hidden='true'>"
        "<path d='M8.3 8.2h15.4a2.5 2.5 0 0 1 2.5 2.5v8.1a2.5 2.5 0 0 1-2.5 2.5h-7.2l-5.2 3.5v-3.5h-3a2.5 2.5 0 0 1-2.5-2.5v-8.1a2.5 2.5 0 0 1 2.5-2.5Z' stroke='white' stroke-width='1.8' stroke-linejoin='round'/>"
        "<path d='M11 13h10M11 16.5h7' stroke='white' stroke-width='1.7' stroke-linecap='round'/>"
        "</svg></span><strong>오피스 지니</strong></div>"
    )
    return f"<div class='manager-brand'>{brand}<span>관리자</span></div>" if admin else brand


def _render_site_header(*, logout: bool = False, auth: dict | None = None, page: str | None = None) -> None:
    with st.container(
        key="site_header", horizontal=True, wrap=False, horizontal_alignment="distribute",
        vertical_alignment="center",
    ):
        is_admin = bool(auth and auth.get("role") == "admin")
        st.markdown(_brand_html(admin=is_admin), unsafe_allow_html=True, width="content")
        if auth and page is not None:
            with st.container(
                key="top_nav", horizontal=True, wrap=False, width="content", gap=None,
                vertical_alignment="center",
            ):
                for key, label, _, _ in _nav_for(auth):
                    st.button(
                        label, key=f"nav_{key}", type="primary" if key == page else "tertiary",
                        on_click=_go_page, args=(key,),
                    )
        if auth or logout:
            with st.container(
                key="header_user", horizontal=True, wrap=False, width="content",
                vertical_alignment="center", gap="small",
            ):
                if auth:
                    who = "관리자" if is_admin else f"{auth.get('name') or ''}님"
                    st.markdown(
                        f"<span class='header-user'>{_escape(who)}</span>",
                        unsafe_allow_html=True, width="content",
                    )
                st.button("로그아웃", key="logout_button", on_click=_on_logout_click)


def _render_bottom_tabs(auth: dict, page: str) -> None:
    """모바일(768px 미만) 하단 고정 탭바. PC에서는 CSS로 숨긴다."""
    with st.container(key="bottom_tabs", horizontal=True, wrap=False, gap=None):
        for key, label, short, icon in _nav_for(auth):
            st.button(
                short, key=f"tab_{key}", icon=icon, help=label,
                type="primary" if key == page else "tertiary",
                on_click=_go_page, args=(key,), width="stretch",
            )


def _page_heading(title: str, subtitle: str = "") -> None:
    sub = f"<p>{_escape(subtitle)}</p>" if subtitle else ""
    st.markdown(
        f"<div class='page-heading'><h1>{_escape(title)}</h1>{sub}</div>",
        unsafe_allow_html=True,
    )
    notice = st.session_state.pop("manager_success", "")
    if notice:
        st.success(notice)


def _render_app(agent: OfficeGenieAgent, auth: dict, page: str) -> None:
    is_admin = auth.get("role") == "admin"
    pages = ADMIN_PAGES if is_admin else EMPLOYEE_PAGES
    if not is_admin:
        dashboard_identity = (auth["company_id"], auth["employee_id"])
        if st.session_state.get("dashboard_identity") != dashboard_identity:
            st.session_state.dashboard_identity = dashboard_identity
            _new_chat_state(auth["company_id"], auth["employee_id"], False)
    with st.container(key="dashboard_root", gap=None):
        _render_site_header(auth=auth, page=page)
        with st.container(key="page_main", gap=None):
            notice = st.session_state.pop("page_notice", "")
            if notice:
                st.warning(notice)
            pages[page](agent, auth)
        _render_bottom_tabs(auth, page)
    if not is_admin:
        _render_chat_launcher(auth)


def _render_chat_launcher(auth: dict) -> None:
    company_id, employee_id = auth["company_id"], auth["employee_id"]
    with st.container(key="chat_fab"):
        st.button(
            "챗봇 닫기" if st.session_state.get("chat_panel_open") else "챗봇 열기",
            key="chat_fab_toggle",
            on_click=_toggle_chat_panel,
            args=(company_id, employee_id),
        )
    if st.session_state.get("chat_panel_open"):
        _render_chat_panel(company_id, employee_id)


def _render_landing() -> None:
    with st.container(key="entry_page", gap=None):
        _render_site_header()
        with st.container(key="landing_hero", gap=None):
            with st.container(key="landing_copy", gap=None):
                st.markdown(
                    "<h1 class='landing-title'>첫 출근의 궁금증,<br>"
                    "오피스 지니에게 물어보세요</h1>"
                    "<p class='landing-subtitle'>공지사항과 내 비품 내역을 한눈에 확인하세요.</p>",
                    unsafe_allow_html=True,
                )
                with st.container(key="landing_actions", horizontal=True):
                    st.button(
                        "로그인", key="landing_login", type="primary", width="stretch",
                        on_click=_go_to, args=("login",),
                    )
                    st.button(
                        "회사 등록", key="landing_register", width="stretch",
                        on_click=_go_to, args=("register",),
                    )
            with st.container(key="landing_preview", gap=None):
                st.image(
                    str(PROJECT_ROOT / "assets" / "OfficeGenie_preview.png"),
                    width="stretch",
                    alt="사원용 나의 온보딩 대시보드 미리보기",
                )


def _auth_from_login(data: dict) -> dict:
    auth = {
        "company_id": data["company_id"],
        "company_name": data.get("company_name") or "",
        "role": data["role"],
        "must_change": bool(data.get("must_change")),
    }
    if data["role"] == "admin":
        auth["account_id"] = data.get("account_id") or "ADMIN"
    else:
        auth.update(
            account_id=data["employee_id"],
            employee_id=data["employee_id"],
            name=data.get("name") or "",
            department=data.get("department") or "",
            position=data.get("position"),
        )
    return auth


def _log_post_login_failure(stage: str, exc: BaseException, auth: dict | None) -> None:
    """로그인 이후 단계의 오류를 서버 로그에 남긴다(비밀번호·해시·DB 주소 제외).

    파일·줄·함수 위치와 예외 종류만 남기고, 메시지는 값이 섞이기 어려운
    단순 예외에서만 짧게 남긴다.
    """
    frames = traceback.extract_tb(exc.__traceback__)[-6:]
    where = " <- ".join(
        f"{Path(f.filename).name}:{f.lineno}:{f.name}" for f in reversed(frames)
    )
    detail = str(exc)[:160] if isinstance(exc, _SAFE_MESSAGE_ERRORS) else ""
    auth = auth or {}
    log.error(
        "post-login failure stage=%s role=%s company_id=%s account_id=%s error=%s %s at %s",
        stage,
        auth.get("role", "-"),
        auth.get("company_id", "-"),
        auth.get("account_id", "-"),
        type(exc).__name__,
        detail,
        where or "-",
    )


def _render_post_login_error() -> None:
    st.error(POST_LOGIN_ERROR)
    st.button("다시 로그인", key="post_login_error_logout", on_click=_on_logout_click)


def _go_to(view: str) -> None:
    st.session_state.auth_view = view


def _back_from_login() -> None:
    for key in (
        "login_company_code", "login_account_id", "login_password", "login_identity_fields",
        "login_clear_password", "login_error", "login_warning",
    ):
        st.session_state.pop(key, None)
    _go_to("landing")
    if st.query_params.get("view") == "login":
        del st.query_params["view"]


def _login_as_registered_admin() -> None:
    registered = st.session_state.pop("registered_company", None) or {}
    st.session_state.login_prefill_code = registered.get("company_code", "")
    st.session_state.login_prefill_account = registered.get("account_id", "ADMIN")
    st.session_state.auth_view = "login"


def _entry_form_body():
    with st.container(key="entry_page", gap=None):
        _render_site_header()
        return st.container(key="login_body", gap=None)


def _render_entry_form_header(
    title: str, back_key: str, on_back, *, back_args: tuple = (), disabled: bool = False,
) -> None:
    with st.container(
        key="login_card_header", horizontal=True, wrap=False,
        horizontal_alignment="distribute", vertical_alignment="center",
    ):
        st.markdown(
            f"<h1 class='login-title'>{_escape(title)}</h1>",
            unsafe_allow_html=True, width="content",
        )
        st.button(
            "← 뒤로가기", key=back_key, on_click=on_back, args=back_args, disabled=disabled,
        )


def _render_register() -> None:
    settings = get_agent().signup_settings()
    needs_code = bool((settings.get("data") or {}).get("signup_code_required"))
    with _entry_form_body():
        with st.container(key="login_card", gap=None):
            _render_entry_form_header("회사 등록", "register_back", _go_to, back_args=("login",))
            with st.form("register_form", border=False):
                company_name = st.text_input("회사명", key="register_company_name")
                password = st.text_input(
                    "관리자 비밀번호 (8자 이상)", type="password", key="register_password"
                )
                again = st.text_input(
                    "관리자 비밀번호 확인", type="password", key="register_password_again"
                )
                signup_code = (
                    st.text_input(
                        "서비스 가입 코드",
                        type="password",
                        key="register_signup_code",
                        help="서비스 운영자에게 받은 가입 코드",
                    )
                    if needs_code
                    else ""
                )
                feedback = st.container(key="login_feedback", gap=None)
                submitted = st.form_submit_button(
                    "회사 등록", type="primary", width="stretch"
                )
            if submitted:
                if not company_name.strip():
                    feedback.warning("회사명을 입력해 주세요.")
                    return
                if len(password) < 8:
                    feedback.error("관리자 비밀번호는 8자 이상이어야 합니다.")
                    return
                if password != again:
                    feedback.error("관리자 비밀번호와 확인 값이 다릅니다.")
                    return
                result = get_agent().register_company(company_name, password, signup_code)
                if not result.get("ok"):
                    feedback.error(result.get("message") or "회사를 등록하지 못했습니다.")
                    return
                for key in ("register_password", "register_password_again", "register_signup_code"):
                    st.session_state.pop(key, None)
                st.session_state.registered_company = result["data"]
                st.session_state.auth_view = "registered"
                st.rerun()


def _render_registered() -> None:
    registered = st.session_state.get("registered_company")
    if not registered:
        _go_to("login")
        st.rerun()
    code = registered["company_code"]
    with st.container(key="entry_page", gap=None):
        _render_site_header()
        _render_company_code_card(registered, code)


def _render_company_code_card(registered: dict, code: str) -> None:
    with st.container(key="auth_card"):
        st.markdown(
            f"<p class='auth-sub'>{_escape(registered['company_name'])} 등록을 마쳤습니다.</p>"
            "<div class='company-code-label'>회사 코드</div>"
            f"<div class='company-code-big'>{_escape(code)}</div>",
            unsafe_allow_html=True,
        )
        st.code(code, language=None)  # 오른쪽 위 복사 버튼
        st.success("사원들에게 이 코드를 알려주세요. 사원은 회사 코드 + 사번 + 비밀번호로 로그인합니다.")
        st.markdown(
            "<p class='auth-sub'>관리자 로그인: 회사 코드 "
            f"<b>{_escape(code)}</b> · 아이디 <b>ADMIN</b> · 방금 정한 관리자 비밀번호</p>",
            unsafe_allow_html=True,
        )
        st.button(
            "관리자로 로그인하기",
            key="registered_login",
            type="primary",
            use_container_width=True,
            on_click=_login_as_registered_admin,
        )


def _submit_login() -> None:
    state = st.session_state
    if state.get("login_future") or state.get("login_success"):
        return
    company_code = state.login_company_code
    account_id = state.login_account_id
    password = state.login_password
    state.login_error = ""
    state.login_warning = ""
    if not company_code.strip() or not account_id.strip() or not password:
        state.login_warning = "회사 코드, 사번, 비밀번호를 모두 입력해 주세요."
        return
    state.login_identity_fields = (company_code, account_id)
    # 비활성화된 폼에도 제출 당시 값을 표시한다. 비밀번호는 결과 처리 후 지운다.
    state.login_company_code = company_code
    state.login_account_id = account_id
    state.login_password = password
    try:
        state.login_future = get_login_executor().submit(
            get_agent().login, company_code, account_id, password
        )
    except Exception:
        state.login_error = CONNECTION_ERROR
        state.login_password = ""


def _render_login() -> None:
    state = st.session_state
    if "login_prefill_code" in st.session_state:
        state.login_company_code = state.pop("login_prefill_code")
        state.login_account_id = state.pop("login_prefill_account", "")
    if state.pop("login_clear_password", False):
        state.login_company_code, state.login_account_id = state.pop("login_identity_fields")
        state.login_password = ""

    future = state.get("login_future")
    success = state.get("login_success")
    busy = future is not None or success is not None
    label = "로그인"
    if future is not None:
        label = "확인 중…"
    elif success is not None:
        label = "✓ 로그인 성공"

    with _entry_form_body():
        with st.container(key="login_card", gap=None):
            _render_entry_form_header("로그인", "login_back", _back_from_login, disabled=busy)
            notice = state.pop("auth_notice", "")
            if notice:
                st.info(notice)
            with st.form("login_form", border=False, enter_to_submit=not busy):
                st.text_input(
                    "회사 코드", key="login_company_code",
                    placeholder="회사 코드를 입력해 주세요.", disabled=busy,
                )
                st.text_input(
                    "사번", key="login_account_id", help="관리자는 ADMIN을 입력해 주세요.",
                    placeholder="사번을 입력해 주세요.", disabled=busy,
                )
                st.text_input(
                    "비밀번호", type="password", key="login_password",
                    placeholder="비밀번호를 입력해 주세요.", disabled=busy,
                )
                with st.container(key="login_feedback", gap=None):
                    if success is not None:
                        st.markdown('<span class="login-success-marker" hidden></span>', unsafe_allow_html=True)
                    elif state.get("login_error"):
                        st.error(state.login_error)
                    elif state.get("login_warning"):
                        st.warning(state.login_warning)
                st.form_submit_button(
                    label, key="login_submit",
                    type="primary", width="stretch", disabled=busy, on_click=_submit_login,
                )
    if future is not None:
        try:
            result = future.result()
        except Exception:
            result = {"ok": False, "message": CONNECTION_ERROR}
        if result.get("ok"):
            try:
                state.login_success = (
                    _auth_from_login(result["data"]),
                    result["data"].get("session_token") or "",
                    time.monotonic(),
                )
            except (KeyError, TypeError) as exc:
                _log_post_login_failure("login_response", exc, None)
                state.login_error = POST_LOGIN_ERROR
        else:
            state.login_error = result.get("message") or "로그인하지 못했습니다."
        state.pop("login_future")
        state.login_clear_password = True
        st.rerun()
    if success is not None:
        auth, token, confirmed_at = success
        time.sleep(max(0, 0.8 - (time.monotonic() - confirmed_at)))
        for key in list(state.keys()):
            del state[key]
        state.auth = auth
        state.resume_checked = True
        if st.query_params.get("view") == "login":
            del st.query_params["view"]
        if token:
            state.session_token = token
            state.session_checked_at = time.monotonic()
            state.cookie_action = ("set", token)
        _touch_activity()
        st.rerun()


def _render_change_password(auth: dict) -> None:
    minimum = 8 if auth["role"] == "admin" else 4
    with st.container(key="entry_page", gap=None):
        _render_site_header(logout=True)
        _render_change_password_card(auth, minimum)


def _render_change_password_card(auth: dict, minimum: int) -> None:
    with st.container(key="auth_card"):
        st.markdown(
            "<p class='auth-sub'>처음 로그인하셨거나 비밀번호가 초기화되었습니다. "
            "새 비밀번호를 설정해 주세요.</p>",
            unsafe_allow_html=True,
        )
        with st.form("change_password_form", border=False):
            old = st.text_input("현재 비밀번호", type="password", key="pw_old")
            new = st.text_input(
                f"새 비밀번호 ({minimum}자 이상)", type="password", key="pw_new"
            )
            again = st.text_input("새 비밀번호 확인", type="password", key="pw_again")
            submitted = st.form_submit_button(
                "비밀번호 변경", type="primary", use_container_width=True
            )
        if submitted:
            if new != again:
                st.error("새 비밀번호와 확인 값이 다릅니다.")
                return
            result = get_agent().change_password(
                auth["company_id"], auth["account_id"], old, new
            )
            if result.get("code") == "INVALID_SESSION":
                _logout(result.get("message", ""))
                st.rerun()
            if result.get("code") == "ACCOUNT_LOCKED":
                _logout(result.get("message", ""))
                st.rerun()
            if not result.get("ok"):
                st.error(result.get("message") or "비밀번호를 변경하지 못했습니다.")
                return
            auth["must_change"] = False
            st.session_state.auth = auth
            # 비밀번호를 바꾸면 기존 세션이 모두 폐기되므로 새 토큰으로 쿠키를 바꾼다.
            token = (result.get("data") or {}).get("session_token")
            if token:
                st.session_state.session_token = token
                st.session_state.session_checked_at = time.monotonic()
                st.session_state.cookie_action = ("set", token)
            st.rerun()


# ---------------------------------------------------------------------------
# 관리자 화면. 모든 호출은 agent.admin_call로 가고, 서버가 관리자 여부를 다시 확인한다.
# ---------------------------------------------------------------------------


def _admin(agent: OfficeGenieAgent, auth: dict, name: str, **args) -> dict:
    result = agent.admin_call(name, auth["company_id"], auth["account_id"], **args)
    if result.get("code") == "PERMISSION_DENIED":
        _logout(result.get("message", ""))
        st.rerun()
    return result


def _read_error(message: str | None, key: str) -> None:
    st.error(message or CONNECTION_ERROR)
    st.button("다시 시도", key=f"retry_{key}")


def _admin_read(agent: OfficeGenieAgent, auth: dict, name: str, **args) -> dict | None:
    with st.spinner("데이터를 불러오는 중입니다."):
        result = _admin(agent, auth, name, **args)
    if result.get("ok"):
        return result["data"]
    _read_error(result.get("message"), name)
    return None


def _short_time(value: object) -> str:
    text = str(value or "")
    return text[:16].replace("T", " ") if text else "-"


def _csv_bytes(rows: list[dict], columns: list[tuple[str, str]]) -> bytes:
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow([label for _, label in columns])
    for row in rows:
        writer.writerow([row.get(key, "") for key, _ in columns])
    return buffer.getvalue().encode("utf-8-sig")


def _template_bytes(kind: str) -> bytes | None:
    path = TEMPLATE_DIR / f"{kind}.json"
    return path.read_bytes() if path.is_file() else None


def _collect_upload_files(uploaded: list) -> None:
    """업로더 파일을 메모리에만 담는다(디스크에 저장하지 않음)."""
    signature = tuple((f.name, f.size, getattr(f, "file_id", "")) for f in uploaded or [])
    if signature == st.session_state.get("admin_uploader_sig", ()):
        return
    st.session_state.admin_uploader_sig = signature
    st.session_state.admin_preview = None
    st.session_state.admin_preview_issues = None
    files, local_issues = [], []
    for item in uploaded or []:
        raw = item.getvalue()
        if len(raw) > UPLOAD_MAX_BYTES:
            local_issues.append(
                {"file": item.name, "item": "-", "field": "-", "problem": "파일 크기는 2MB 이하여야 합니다."}
            )
            continue
        try:
            content = raw.decode("utf-8-sig")
        except UnicodeDecodeError:
            local_issues.append(
                {"file": item.name, "item": "-", "field": "-", "problem": "UTF-8 텍스트 파일이 아닙니다."}
            )
            continue
        files.append({"name": item.name, "content": content})
    st.session_state.admin_upload_files = files
    st.session_state.admin_upload_local_issues = local_issues


def _issues_table(issues: list[dict]) -> None:
    st.dataframe(
        [
            {
                "파일": i.get("file", "-"),
                "항목": i.get("item", "-"),
                "필드": i.get("field", "-"),
                "문제": i.get("problem", ""),
            }
            for i in issues
        ],
        hide_index=True,
        use_container_width=True,
    )


def _admin_preview(agent: OfficeGenieAgent, auth: dict) -> None:
    files = st.session_state.get("admin_upload_files") or []
    if not files or st.session_state.get("admin_upload_local_issues"):
        return
    result = _admin(agent, auth, "ui_admin_preview_upload", files=files)
    if result.get("ok"):
        st.session_state.admin_preview = {"files": files, "data": result["data"]}
        st.session_state.admin_preview_issues = None
    else:
        st.session_state.admin_preview = None
        st.session_state.admin_preview_issues = {
            "message": result.get("message", ""),
            "issues": (result.get("data") or {}).get("issues") or [],
        }


def _admin_apply(agent: OfficeGenieAgent, auth: dict) -> None:
    preview = st.session_state.get("admin_preview")
    if not preview:
        return
    result = _admin(
        agent,
        auth,
        "ui_admin_apply_upload",
        files=preview["files"],
        base_version=preview["data"]["base_version"],
        fingerprint=preview["data"]["fingerprint"],
    )
    if not result.get("ok"):
        st.session_state.admin_preview = None
        st.session_state.admin_preview_issues = {
            "message": result.get("message", ""),
            "issues": (result.get("data") or {}).get("issues") or [],
        }
        return
    data = result["data"]
    st.session_state.admin_applied = {
        "data_version": data["data_version"],
        "cache_ttl_seconds": data.get("cache_ttl_seconds"),
    }
    if data.get("initial_passwords"):
        st.session_state.admin_initial_passwords = data["initial_passwords"]
    st.session_state.admin_preview = None
    st.session_state.admin_upload_files = []
    st.session_state.admin_uploader_sig = ()
    st.session_state.admin_uploader_n = st.session_state.get("admin_uploader_n", 0) + 1


def _cancel_preview() -> None:
    st.session_state.admin_preview = None


def _dismiss(key: str) -> None:
    st.session_state.pop(key, None)


def _render_one_time_passwords(key: str, title: str) -> None:
    rows = st.session_state.get(key)
    if not rows:
        return
    st.warning(
        f"{title} 이 목록은 지금 한 번만 보입니다. 서버에는 원문이 저장되지 않으니 "
        "CSV로 내려받거나 사원에게 바로 전달하세요."
    )
    st.dataframe(
        [
            {"사번": r["employee_id"], "이름": r.get("name", ""), "초기 비밀번호": r["initial_password"]}
            for r in rows
        ],
        hide_index=True,
        use_container_width=True,
    )
    cols = st.columns([1, 1, 3])
    cols[0].download_button(
        "CSV 다운로드",
        data=_csv_bytes(
            rows, [("employee_id", "사번"), ("name", "이름"), ("initial_password", "초기 비밀번호")]
        ),
        file_name="initial_passwords.csv",
        mime="text/csv",
        key=f"{key}_csv",
        on_click="ignore",
    )
    cols[1].button("확인했습니다", key=f"{key}_dismiss", on_click=_dismiss, args=(key,))


def _admin_data_tab(agent: OfficeGenieAgent, auth: dict, info: dict) -> None:
    ttl = int(info.get("cache_ttl_seconds") or 0)
    counts = info.get("counts") or {}
    with st.container(key="settings_metrics", gap=None):
        for key, label in (
            ("policies", "규정"),
            ("employees", "사원"),
            ("locations", "장소"),
            ("handovers", "인수인계서"),
            ("equipment", "신청 가능 비품"),
            ("tickets", "신청 내역"),
        ):
            st.metric(label, counts.get(key, 0))

    if not info["has_data"]:
        st.info("아직 회사 데이터가 없습니다. 자료 업로드에서 등록하세요.")
    applied = st.session_state.pop("admin_applied", None)
    if applied:
        st.success(
            f"반영했습니다 (데이터 버전 {applied['data_version']}). "
            f"챗봇 답변에는 최대 {applied.get('cache_ttl_seconds', ttl)}초 안에 반영됩니다."
        )
    _render_one_time_passwords("admin_initial_passwords", "새로 만든 사원 초기 비밀번호입니다.")

    with st.container(key="settings_panel_templates"):
        st.markdown("### 1. 양식 다운로드")
        st.caption("각 양식에는 필드 설명과 예시 1건이 들어 있습니다. 예시를 지우고 회사 데이터로 채워 주세요.")
        with st.container(key="settings_templates", gap=None):
            for kind, label in DATA_KINDS:
                payload = _template_bytes(kind)
                if payload is not None:
                    st.download_button(
                        f"{label} 양식",
                        data=payload,
                        file_name=f"{kind}.json",
                        mime="application/json",
                        key=f"template_{kind}",
                        icon=":material/download:",
                        width="stretch",
                        on_click="ignore",
                    )
    with st.container(key="settings_panel_upload"):
        st.markdown("### 2. 파일 올리기")
        limit_mb = UPLOAD_MAX_BYTES // (1024 * 1024)
        st.caption(f"JSON 파일을 한 개 또는 여러 개 선택할 수 있습니다. 파일당 {limit_mb}MB 이하로 올려 주세요.")
        labels = "".join(f"<span>{kind}.json</span>" for kind, _ in DATA_KINDS)
        st.markdown(
            f"<div class='settings-file-labels'>{labels}</div>"
            "<div class='settings-upload-note'>올린 파일 종류의 데이터만 갱신되며, 나머지 데이터는 유지됩니다.<br>"
            "비품·신청 규칙을 올려도 기존 신청 내역과 신청 번호는 유지됩니다.</div>",
            unsafe_allow_html=True,
        )
        uploaded = st.file_uploader(
            "회사 데이터 JSON",
            type=["json"],
            accept_multiple_files=True,
            max_upload_size=limit_mb,
            key=f"admin_uploader_{st.session_state.get('admin_uploader_n', 0)}",
        )
        _collect_upload_files(uploaded)
        local_issues = st.session_state.get("admin_upload_local_issues") or []
        if local_issues:
            st.error("올릴 수 없는 파일이 있습니다.")
            _issues_table(local_issues)
        files = st.session_state.get("admin_upload_files") or []
        with st.container(
            key="settings_upload_footer", horizontal=True, vertical_alignment="center",
            horizontal_alignment="right" if uploaded else "distribute",
        ):
            if not uploaded:
                st.caption("파일을 선택하면 미리볼 수 있습니다.", width="content")
            st.button(
                "미리보기", key="admin_preview_button", type="primary",
                disabled=not files or bool(local_issues), on_click=_admin_preview, args=(agent, auth),
            )

    problem = st.session_state.get("admin_preview_issues")
    if problem:
        st.error(problem["message"])
        if problem["issues"]:
            _issues_table(problem["issues"])

    preview = st.session_state.get("admin_preview")
    if preview:
        data = preview["data"]
        st.markdown("#### 3. 변경 미리보기")
        st.info(
            "아직 반영되지 않았습니다. 내용을 확인한 뒤 '반영하기'를 누르세요. "
            f"반영 후 챗봇 답변에는 최대 {data.get('cache_ttl_seconds', ttl)}초 안에 반영됩니다."
        )
        st.dataframe(
            [
                {
                    "종류": c["label"],
                    "추가": c["added"],
                    "수정": c["updated"],
                    "삭제": c["removed"],
                    "변경 없음": c["unchanged"],
                    "설정 변경": "예" if c["settings_changed"] else "-",
                }
                for c in data["changes"]
            ],
            hide_index=True,
            use_container_width=True,
        )
        for change in data["changes"]:
            ids = [
                ("추가", change["added_ids"]),
                ("수정", change["updated_ids"]),
                ("삭제", change["removed_ids"]),
            ]
            if any(values for _, values in ids):
                with st.expander(f"{change['label']} 변경 항목 ID"):
                    for label, values in ids:
                        if values:
                            st.markdown(f"**{label}**: " + ", ".join(_escape(v) for v in values))
        if data.get("initial_passwords_to_create"):
            st.caption(
                f"비밀번호가 없는 새 사원 {data['initial_passwords_to_create']}명은 "
                "반영할 때 4자리 초기 비밀번호가 만들어집니다."
            )
        cols = st.columns([1, 1, 4])
        cols[0].button(
            "반영하기",
            key="admin_apply_button",
            type="primary",
            on_click=_admin_apply,
            args=(agent, auth),
        )
        cols[1].button("취소", key="admin_cancel_preview", on_click=_cancel_preview)


def _admin_employees_tab(agent: OfficeGenieAgent, auth: dict) -> None:
    data = _admin_read(agent, auth, "ui_admin_list_employees")
    if data is None:
        return
    people = data["employees"]
    if not people:
        st.info("등록된 사원이 없습니다. 사원 관리에서 추가하거나 자료 업로드에서 사원 문서를 올려 주세요.")
        return
    departments = sorted({p["department"] for p in people})
    cols = st.columns([1, 2])
    department = cols[0].selectbox("부서", ["전체", *departments], key="admin_emp_dept")
    query = cols[1].text_input("이름·사번 검색", key="admin_emp_query").strip().lower()
    filtered = [
        p
        for p in people
        if (department == "전체" or p["department"] == department)
        and (not query or query in p["name"].lower() or query in p["employee_id"].lower())
    ]
    rows = [
        {
            "사번": p["employee_id"],
            "이름": p["name"],
            "부서": p["department"],
            "직급": p.get("position") or "-",
            "직무": p.get("job_title") or "-",
            "잔여 연차": "-" if p.get("remaining_days") is None else p["remaining_days"],
            "계정 상태": p["account_status_label"],
        }
        for p in filtered
    ]
    st.caption(f"{len(rows)}명 · 행을 선택하면 상세 정보를 볼 수 있습니다.")
    event = st.dataframe(
        rows,
        hide_index=True,
        use_container_width=True,
        on_select="rerun",
        selection_mode="single-row",
        key="admin_emp_table",
    )
    selected_rows = list(getattr(getattr(event, "selection", None), "rows", []) or [])
    if not selected_rows or selected_rows[0] >= len(filtered):
        return
    employee_id = filtered[selected_rows[0]]["employee_id"]
    data = _admin_read(agent, auth, "ui_admin_employee_detail", employee_id=employee_id)
    if data is None:
        return
    employee = data["employee"]
    st.markdown(f"#### {_escape(employee.get('name'))} ({_escape(employee.get('employee_id'))})")
    info_rows = [
        ("부서", employee.get("department")),
        ("직급", employee.get("position") or "-"),
        ("직무", employee.get("job_title") or "-"),
        ("재직 상태", employee.get("employment_status") or "-"),
        ("업무 메일", employee.get("work_email") or "-"),
        ("계정 상태", data.get("account_status_label")),
    ]
    st.dataframe(
        [{"항목": k, "값": str(v)} for k, v in info_rows],
        hide_index=True,
        use_container_width=True,
    )
    leave = data.get("annual_leave") or {}
    leave_cols = st.columns(3)
    leave_cols[0].metric("잔여 연차", leave.get("remaining_days", "-"))
    leave_cols[1].metric("부여", leave.get("granted_days", "-"))
    leave_cols[2].metric("사용", leave.get("used_days", "-"))
    st.markdown("**신청 내역**")
    if data["tickets"]:
        st.dataframe(
            [
                {
                    "티켓 번호": t.get("ticket_id"),
                    "품목": t.get("item"),
                    "수량": t.get("quantity"),
                    "상태": t.get("status"),
                    "신청일": _short_time(t.get("created_at")),
                }
                for t in data["tickets"]
            ],
            hide_index=True,
            use_container_width=True,
        )
    else:
        st.caption("신청 내역이 없습니다.")
    st.markdown("**소속 부서 인수인계서**")
    if not data["handovers"]:
        st.caption("등록된 인수인계서가 없습니다.")
    for handover in data["handovers"]:
        with st.expander(handover.get("title") or handover.get("handover_id")):
            for heading, items in (handover.get("summary") or {}).items():
                st.markdown(f"**{_escape(heading)}**")
                if isinstance(items, list):
                    st.markdown("\n".join(f"- {_escape(i)}" for i in items))
                else:
                    st.markdown(_escape(items))


def _admin_change_ticket(agent: OfficeGenieAgent, auth: dict) -> None:
    ticket_id = st.session_state.get("admin_ticket_pick")
    to_status = st.session_state.get("admin_ticket_next")
    note = st.session_state.get("admin_ticket_note", "")
    if not ticket_id or not to_status:
        return
    result = _admin(
        agent, auth, "ui_admin_update_ticket", ticket_id=ticket_id, to_status=to_status, note=note
    )
    st.session_state.admin_ticket_result = (bool(result.get("ok")), result.get("message", ""))
    if result.get("ok"):
        st.session_state.admin_ticket_note = ""


def _admin_tickets_tab(agent: OfficeGenieAgent, auth: dict) -> None:
    data = _admin_read(agent, auth, "ui_admin_list_tickets")
    if data is None:
        return
    tickets = data["tickets"]
    outcome = st.session_state.pop("admin_ticket_result", None)
    if outcome:
        (st.success if outcome[0] else st.error)(outcome[1])
    if not tickets:
        st.info("아직 신청 내역이 없습니다.")
        return
    statuses = ["전체", *dict.fromkeys(t["status"] for t in tickets)]
    departments = ["전체", *sorted({t["department"] for t in tickets})]
    cols = st.columns(2)
    status = cols[0].selectbox("상태", statuses, key="admin_ticket_status_filter")
    department = cols[1].selectbox("부서", departments, key="admin_ticket_dept_filter")
    filtered = [
        t
        for t in tickets
        if (status == "전체" or t["status"] == status)
        and (department == "전체" or t["department"] == department)
    ]
    st.dataframe(
        [
            {
                "티켓 번호": t["ticket_id"],
                "신청자": f"{t['name']} ({t['employee_id']})",
                "부서": t["department"],
                "품목": t["item"],
                "수량": t["quantity"],
                "상태": t["status"],
                "신청일": _short_time(t["created_at"]),
            }
            for t in filtered
        ],
        hide_index=True,
        use_container_width=True,
    )

    changeable = [t for t in tickets if t["allowed_next"]]
    st.markdown("#### 상태 변경")
    st.caption("접수완료 → 검토중 → 승인 → 지급완료 순서로만 바꿀 수 있습니다. 반려는 사유가 필요합니다.")
    if not changeable:
        st.caption("상태를 바꿀 수 있는 신청이 없습니다.")
        return
    by_id = {t["ticket_id"]: t for t in changeable}
    cols = st.columns([2, 1, 2])
    picked = cols[0].selectbox(
        "신청",
        list(by_id),
        format_func=lambda tid: f"{tid} · {by_id[tid]['name']} · {by_id[tid]['item']} ({by_id[tid]['status']})",
        key="admin_ticket_pick",
    )
    ticket = by_id[picked]
    cols[1].selectbox("바꿀 상태", ticket["allowed_next"], key="admin_ticket_next")
    cols[2].text_input("메모 (반려 시 사유 필수)", key="admin_ticket_note")
    st.button(
        "상태 변경",
        key="admin_ticket_apply",
        type="primary",
        on_click=_admin_change_ticket,
        args=(agent, auth),
    )
    if ticket["history"]:
        with st.expander(f"{picked} 변경 이력"):
            st.dataframe(
                [
                    {
                        "시각": _short_time(h.get("at")),
                        "처리자": h.get("by"),
                        "이전": h.get("from"),
                        "이후": h.get("to"),
                        "메모": h.get("note") or "",
                    }
                    for h in ticket["history"]
                ],
                hide_index=True,
                use_container_width=True,
            )


def _admin_account_action(agent: OfficeGenieAgent, auth: dict, action: str) -> None:
    employee_id = st.session_state.get("admin_account_pick")
    if not employee_id:
        return
    tool = "ui_admin_reset_password" if action == "reset" else "ui_admin_unlock_account"
    result = _admin(agent, auth, tool, employee_id=employee_id)
    st.session_state.admin_account_result = (bool(result.get("ok")), result.get("message", ""))
    if result.get("ok") and action == "reset":
        names = {a["employee_id"]: a["name"] for a in st.session_state.get("admin_accounts_cache", [])}
        st.session_state.admin_reset_passwords = [
            {
                "employee_id": employee_id,
                "name": names.get(employee_id, ""),
                "initial_password": result["data"]["initial_password"],
            }
        ]


def _admin_accounts_tab(agent: OfficeGenieAgent, auth: dict) -> None:
    outcome = st.session_state.pop("admin_account_result", None)
    if outcome:
        (st.success if outcome[0] else st.error)(outcome[1])
    _render_one_time_passwords("admin_reset_passwords", "초기화한 비밀번호입니다.")
    data = _admin_read(agent, auth, "ui_admin_list_accounts")
    if data is None:
        return
    accounts = data["accounts"]
    st.session_state.admin_accounts_cache = [
        {"employee_id": a["employee_id"], "name": a["name"]} for a in accounts
    ]
    st.markdown("#### 사원 계정")
    if accounts:
        st.dataframe(
            [
                {
                    "사번": a["employee_id"],
                    "이름": a["name"],
                    "부서": a["department"],
                    "상태": a["account_status_label"],
                    "연속 실패": a["failed_count"],
                    "잠금 해제 시각": _short_time(a["locked_until"]) if a["locked_until"] else "-",
                }
                for a in accounts
            ],
            hide_index=True,
            use_container_width=True,
        )
        active = [a for a in accounts if a["account_status"] != "disabled"]
        if active:
            labels = {a["employee_id"]: f"{a['employee_id']} {a['name']} ({a['account_status_label']})" for a in active}
            st.selectbox("사원 선택", list(labels), format_func=labels.get, key="admin_account_pick")
            cols = st.columns([1, 1, 3])
            cols[0].button(
                "비밀번호 초기화",
                key="admin_reset_button",
                on_click=_admin_account_action,
                args=(agent, auth, "reset"),
            )
            cols[1].button(
                "잠금 해제",
                key="admin_unlock_button",
                on_click=_admin_account_action,
                args=(agent, auth, "unlock"),
            )
            st.caption("초기화하면 새 4자리 비밀번호가 한 번만 보이고, 사원은 다음 로그인 때 비밀번호를 바꿔야 합니다.")
    else:
        st.info("사원 계정이 없습니다. 데이터 관리에서 사원(employees.json)을 올려 주세요.")

    st.markdown("#### 관리자 비밀번호 변경")
    with st.form("admin_password_form", clear_on_submit=True):
        old = st.text_input("현재 비밀번호", type="password", key="admin_pw_old")
        new = st.text_input("새 비밀번호 (8자 이상)", type="password", key="admin_pw_new")
        again = st.text_input("새 비밀번호 확인", type="password", key="admin_pw_again")
        submitted = st.form_submit_button("변경")
    if submitted:
        if new != again:
            st.error("새 비밀번호와 확인 값이 다릅니다.")
        else:
            changed = agent.change_password(auth["company_id"], auth["account_id"], old, new)
            (st.success if changed.get("ok") else st.error)(changed.get("message", ""))
            token = (changed.get("data") or {}).get("session_token") if changed.get("ok") else None
            if token:
                st.session_state.session_token = token
                st.session_state.session_checked_at = time.monotonic()
                st.session_state.cookie_action = ("set", token)
                _apply_cookie_action()


def _manager_select(section: str, mode: str | None, row_id: str | None = None) -> None:
    st.session_state[f"manager_{section}_selection"] = (mode, row_id)


def _manager_result(result: dict, section: str, success: str) -> None:
    if not result.get("ok"):
        st.error(result.get("message") or CONNECTION_ERROR)
        return
    st.session_state.manager_success = result.get("message") or success
    _manager_select(section, None)
    st.rerun()


def _manager_heading(title: str, subtitle: str, count: str = "") -> None:
    count_html = f"<span>{_escape(count)}</span>" if count else ""
    st.markdown(
        f"<div class='manager-heading'><h2>{_escape(title)}</h2>{count_html}</div>"
        f"<p class='manager-subtitle'>{_escape(subtitle)}</p>",
        unsafe_allow_html=True, width="content",
    )


def _manager_badge(label: str, tone: str) -> str:
    return f"<span class='manager-badge manager-badge-{tone}'>{_escape(label)}</span>"


def _manager_table(
    section: str,
    labels: tuple[str, ...],
    widths: tuple,
    rows: list[tuple[str, tuple]],
    action: str,
    mode: str,
    unit: str = "건",
) -> None:
    if not rows:
        st.info("표시할 검색 결과가 없습니다.")
        return
    page_size = 8
    page_count = (len(rows) + page_size - 1) // page_size
    page_key = f"manager_{section}_page"
    page = min(st.session_state.get(page_key, 1), page_count)
    focus = st.session_state.pop(f"manager_{section}_focus", None)
    if focus is not None:
        for index, (row_id, _) in enumerate(rows):
            if row_id == focus:
                page = index // page_size + 1
                st.session_state[page_key] = page
                break
    start = (page - 1) * page_size
    visible = rows[start:start + page_size]
    with st.container(key="manager_table", gap=None):
        with st.container(key="manager_table_head", gap=None):
            for col, label in zip(st.columns(widths), (*labels, "관리")):
                col.markdown(f"<div class='manager-cell'>{_escape(label)}</div>", unsafe_allow_html=True)
        for row_id, values in visible:
            with st.container(gap=None, key=f"manager_row_{row_id}"):
                columns = st.columns(widths, vertical_alignment="center")
                for col, value in zip(columns, values):
                    content = _manager_badge(*value) if isinstance(value, tuple) else _escape(value)
                    col.markdown(f"<div class='manager-cell'>{content}</div>", unsafe_allow_html=True)
                columns[-1].button(
                    action, key=f"manager_{section}_row_{row_id}", on_click=_manager_select,
                    args=(section, mode, row_id),
                )
    with st.container(horizontal=True, horizontal_alignment="distribute", vertical_alignment="center"):
        st.caption(f"총 {len(rows)}{unit} 중 {start + 1}–{start + len(visible)}{unit} 표시")
        if page_count > 1:
            def move_page(target: int) -> None:
                st.session_state[page_key] = target

            with st.container(horizontal=True, width="content", vertical_alignment="center"):
                st.button("이전", key=f"{page_key}_prev", disabled=page == 1, on_click=move_page, args=(page - 1,))
                st.caption(f"{page} / {page_count}")
                st.button("다음", key=f"{page_key}_next", disabled=page == page_count, on_click=move_page, args=(page + 1,))


def _manager_form_actions() -> tuple[bool, bool]:
    st.divider()
    with st.container(horizontal=True, horizontal_alignment="right"):
        cancelled = st.form_submit_button("취소")
        saved = st.form_submit_button("저장", type="primary")
    return saved, cancelled


def _manager_cancel(section: str) -> None:
    _manager_select(section, None)
    st.rerun()


def _manager_term(value: object) -> str:
    return "".join(str(value or "").casefold().split())


def _manager_filter(
    section: str, prompt: str, options: list[str] | None = None, filter_label: str = "",
) -> tuple[str, str | None]:
    # 위젯이 숨겨지는 상세 화면에서도 적용한 조건을 보존한다.
    state = st.session_state
    state_key = f"manager_{section}_filters"
    query_key, filter_key = f"{state_key}_query", f"{state_key}_choice"
    query, choice = state.get(state_key, ("", None))
    if options is not None and choice not in options:
        choice = None

    def apply_filters() -> None:
        state[state_key] = (state[query_key], state.get(filter_key))
        state[f"manager_{section}_page"] = 1

    def reset_filters() -> None:
        state[query_key] = ""
        state[filter_key] = None
        apply_filters()

    is_policy = section == "policies"
    with st.form(f"manager_{section}_filter_form", border=False):
        search, filter_col, buttons = st.columns([3, 2.4, 2], vertical_alignment="center")
        search.text_input(prompt, value=query, placeholder=prompt, key=query_key, label_visibility="collapsed")
        if options is not None:
            filter_col.selectbox(
                filter_label, [None, *options], index=0 if choice is None else options.index(choice) + 1,
                format_func=lambda value: filter_label if value is None else value,
                key=filter_key, label_visibility="collapsed",
            )
        with buttons:
            with st.container(horizontal=True):
                st.form_submit_button("검색", type="primary", on_click=apply_filters)
                if is_policy:
                    st.form_submit_button("초기화", on_click=reset_filters)
    return _manager_term(query), choice


def _manager_employee_form(
    agent: OfficeGenieAgent, auth: dict, employee: dict | None, departments: list[str],
) -> None:
    creating = employee is None
    employee = employee or {}
    leave = employee.get("annual_leave") or {}
    remaining_days = leave.get("remaining_days", 0 if creating else None)
    statuses = ["재직", "휴직", "퇴사"]
    current_status = employee.get("employment_status")
    _manager_heading(
        "사원 추가" if creating else "사원 수정",
        "새로운 사원의 기본 정보를 입력해 주세요." if creating else "변경할 정보를 확인한 뒤 저장해 주세요.",
    )
    with st.form("manager_employee_form", border=False, enter_to_submit=False):
        left, right = st.columns(2, gap="large")
        employee_id = left.text_input("사번", employee.get("employee_id", ""), disabled=not creating, max_chars=100, key="manager_employee_id")
        name = right.text_input("이름", employee.get("name", ""), max_chars=300, key="manager_employee_name")
        dept = employee.get("department")
        department = left.selectbox(
            "부서", departments, index=departments.index(dept) if dept in departments else None,
            accept_new_options=True, placeholder="부서를 선택하거나 입력해 주세요.", key="manager_employee_dept",
        )
        position = right.text_input("직급", employee.get("position") or "", max_chars=200, key="manager_employee_position")
        remaining = left.number_input(
            "잔여 연차 (일)", min_value=0.0, step=0.5,
            value=float(remaining_days) if remaining_days is not None else None,
            key="manager_employee_leave",
        )
        status = right.selectbox(
            "재직 상태", statuses, index=statuses.index(current_status) if current_status in statuses else 0,
            key="manager_employee_status",
        )
        saved, cancelled = _manager_form_actions()
    if cancelled:
        _manager_cancel("employees")
    if saved:
        if not all((employee_id.strip(), name.strip(), department, position.strip())) or remaining is None:
            st.error("사번, 이름, 부서, 직급과 잔여 연차를 입력해 주세요.")
            return
        employee_id = employee.get("employee_id", employee_id.strip())
        result = _admin(
            agent, auth, "ui_admin_save_employee", employee_id=employee_id,
            employee_name=name, department=department, position=position, remaining_days=remaining,
            employment_status=status, create=creating,
        )
        if result.get("ok"):
            state = st.session_state
            initial = (result.get("data") or {}).get("initial_password")
            if initial:
                state.manager_employee_password = initial
            query, selected_dept = state.get("manager_employees_filters", ("", None))
            term = _manager_term(query)
            if (
                (selected_dept is not None and department.strip() != selected_dept)
                or (term not in _manager_term(employee_id) and term not in _manager_term(name))
            ):
                state.manager_employees_filters = ("", None)
            state.manager_employees_focus = employee_id
        _manager_result(result, "employees", "사원 정보가 저장되었습니다.")


def _manager_employees(agent: OfficeGenieAgent, auth: dict) -> None:
    result = _admin_read(agent, auth, "ui_admin_list_employees")
    if result is None:
        return
    people = result["employees"]
    departments = sorted({p["department"] for p in people if p.get("department")})
    mode, employee_id = st.session_state.get("manager_employees_selection", (None, None))
    if mode == "add":
        _manager_employee_form(agent, auth, None, departments)
        return
    if mode == "edit":
        detail = _admin_read(agent, auth, "ui_admin_employee_detail", employee_id=employee_id)
        if detail is not None:
            _manager_employee_form(agent, auth, detail["employee"], departments)
        else:
            st.button("사원 목록", on_click=_manager_select, args=("employees", None))
        return

    with st.container(horizontal=True, horizontal_alignment="distribute"):
        _manager_heading("사원 목록", "회사에 소속된 사원 정보를 확인하고 관리하세요.", f"총 {len(people)}명")
        st.button("＋ 사원 추가", type="primary", on_click=_manager_select, args=("employees", "add"))
    initial = st.session_state.pop("manager_employee_password", None)
    if initial:
        st.info(f"{initial['employee_id']} 사원의 초기 비밀번호는 {initial['initial_password']}입니다. 최초 로그인 시 변경해야 합니다.")
    query, department = _manager_filter("employees", "이름 또는 사번 검색", departments, "전체 부서")
    rows = []
    for person in people:
        if department is not None and person["department"] != department:
            continue
        if query not in _manager_term(person["name"]) and query not in _manager_term(person["employee_id"]):
            continue
        leave = person["remaining_days"]
        status = person["employment_status"] or "미등록"
        tone = {"재직": "green", "휴직": "amber"}.get(status, "gray")
        rows.append((person["employee_id"], (
            person["name"], person["employee_id"], person["department"], person.get("position") or "—",
            "—" if leave is None else f"{leave:g}일", (status, tone),
        )))
    _manager_table(
        "employees", ("이름", "사번", "부서", "직급", "잔여 연차", "재직 상태"),
        (1.2, 1.2, 1.7, 1, 1, 1.1, 1), rows, "수정", "edit", "명",
    )


_EQUIPMENT_CHOICES = {"접수": "접수완료", "지급 예정": "승인", "지급 완료": "지급완료"}
_EQUIPMENT_BADGES = {
    "접수완료": ("접수", "blue"), "검토중": ("접수", "blue"),
    "승인": ("지급 예정", "amber"), "지급완료": ("지급 완료", "green"),
    "반려": ("반려", "gray"),
}


def _manager_info_grid(pairs: tuple[tuple[str, object], ...]) -> None:
    cells = "".join(
        f"<div><dt>{_escape(label)}</dt><dd>{_escape(value or '—')}</dd></div>"
        for label, value in pairs
    )
    st.markdown(f"<dl class='manager-info-grid'>{cells}</dl>", unsafe_allow_html=True)


def _manager_equipment_form(agent: OfficeGenieAgent, auth: dict, ticket: dict) -> None:
    status = ticket["status"]
    label, tone = _EQUIPMENT_BADGES.get(status, (status, "gray"))
    with st.container(horizontal=True, horizontal_alignment="distribute"):
        _manager_heading("비품 신청 상세", "신청 내용을 확인하고 지급 상태와 일정을 관리하세요.")
        st.markdown(_manager_badge(label, tone), unsafe_allow_html=True, width="content")
    _manager_info_grid((
        ("신청 번호", ticket["ticket_id"]), ("신청 사원", f"{ticket['name']} · {ticket['employee_id']}"),
        ("소속 부서", ticket["department"]), ("비품명", ticket["item"]),
        ("수량", f"{ticket['quantity']}개"), ("신청일", _short_time(ticket["created_at"])),
    ))
    st.markdown(
        f"<div class='manager-reason'><strong>신청 사유</strong> {_escaped_lines(ticket['purpose'] or '—')}</div>",
        unsafe_allow_html=True,
    )
    if label not in _EQUIPMENT_CHOICES:
        st.info("현재 상태의 신청은 지급 상태를 변경할 수 없습니다.")
        st.button("비품 목록", on_click=_manager_select, args=("equipment", None))
        return
    choices = list(_EQUIPMENT_CHOICES)
    current_index = choices.index(label)
    with st.form("manager_equipment_form", border=False, enter_to_submit=False):
        cols = st.columns(2, gap="large")
        target = cols[0].selectbox("변경할 상태", choices, index=current_index, key="manager_equipment_status")
        scheduled = ticket.get("scheduled_date")
        scheduled_date = cols[1].date_input(
            "지급 예정일 (선택)", value=date.fromisoformat(scheduled) if scheduled else None,
            format="YYYY.MM.DD", key="manager_equipment_date",
        )
        saved, cancelled = _manager_form_actions()
    if cancelled:
        _manager_cancel("equipment")
    if saved:
        original_status, original_date = st.session_state.manager_equipment_original
        result = _admin(
            agent, auth, "ui_admin_save_equipment", ticket_id=ticket["ticket_id"],
            to_status=_EQUIPMENT_CHOICES[target], expected_status=original_status,
            expected_scheduled_date=original_date,
            scheduled_date=scheduled_date.isoformat() if scheduled_date else None,
        )
        _manager_result(result, "equipment", "비품 신청 정보가 저장되었습니다.")


def _manager_equipment(agent: OfficeGenieAgent, auth: dict) -> None:
    result = _admin_read(agent, auth, "ui_admin_list_tickets")
    if result is None:
        return
    tickets = [ticket for ticket in result["tickets"] if ticket["type"] == "비품"]
    mode, ticket_id = st.session_state.get("manager_equipment_selection", (None, None))
    if mode == "detail":
        ticket = next((t for t in tickets if t["ticket_id"] == ticket_id), None)
        if ticket:
            st.session_state.setdefault("manager_equipment_original", (ticket["status"], ticket.get("scheduled_date")))
            _manager_equipment_form(agent, auth, ticket)
        else:
            st.info("해당 비품 신청 내역을 찾을 수 없습니다.")
            st.button("비품 목록", on_click=_manager_select, args=("equipment", None))
        return
    st.session_state.pop("manager_equipment_original", None)
    _manager_heading("비품 신청 내역", "사원이 신청한 비품과 지급 상태를 관리하세요.", f"총 {len(tickets)}건")
    query, status = _manager_filter(
        "equipment", "사원명 또는 비품명 검색", [*_EQUIPMENT_CHOICES, "반려"], "전체 상태",
    )
    rows = []
    for ticket in tickets:
        badge = _EQUIPMENT_BADGES.get(ticket["status"], (ticket["status"], "gray"))
        if status is not None and badge[0] != status:
            continue
        if query not in _manager_term(ticket["name"]) and query not in _manager_term(ticket["item"]):
            continue
        rows.append((ticket["ticket_id"], (
            ticket["ticket_id"], ticket["name"], ticket["item"], ticket["quantity"],
            str(ticket["created_at"])[:10].replace("-", "."), badge,
        )))
    _manager_table(
        "equipment", ("신청 번호", "신청 사원", "비품명", "수량", "신청일", "상태"),
        (1.35, 1.2, 1.8, 0.65, 1.35, 1.15, 1), rows, "상세", "detail",
    )


def _manager_announcement_form(agent: OfficeGenieAgent, auth: dict, notice: dict | None) -> None:
    editing = notice is not None
    notice = notice or {}
    _manager_heading("공지 수정" if editing else "공지 작성", "사원에게 전달할 공지와 공개 상태를 설정하세요.")
    with st.form("manager_notice_form", border=False, enter_to_submit=False):
        title = st.text_input("제목", notice.get("title", ""), max_chars=300, key="manager_notice_title")
        body = st.text_area("본문", notice.get("body", ""), height=154, max_chars=12000, key="manager_notice_body")
        public = st.selectbox(
            "공개 상태", [True, False], index=0 if notice.get("is_public", True) else 1,
            format_func=lambda value: "공개" if value else "비공개", key="manager_notice_public", width=360,
        )
        st.caption("공개된 공지는 사원용 대시보드에 표시됩니다.")
        saved, cancelled = _manager_form_actions()
    if cancelled:
        _manager_cancel("notices")
    if saved:
        if not title.strip() or not body.strip():
            st.error("제목과 본문을 입력해 주세요.")
            return
        result = _admin(
            agent, auth, "ui_admin_save_announcement", title=title, body=body, is_public=public,
            announcement_id=notice.get("announcement_id"),
        )
        _manager_result(result, "notices", "공지사항이 저장되었습니다.")


def _notice_matches(notice: dict, query: str) -> bool:
    return not query or query in _manager_term(notice.get("title")) or query in _manager_term(notice.get("body"))


def _notice_details_html(notices: list[dict]) -> str:
    return "".join(
        "<details class='notice-entry notice-page-entry'><summary class='notice-row'>"
        f"<span class='notice-title'>{_escape(n.get('title'))}"
        + ("<em class='new-badge'>NEW</em>" if _is_new(n.get("published_at") or n.get("created_at")) else "")
        + f"</span><time class='notice-date'>{_date_label(n.get('published_at') or n.get('created_at'))}</time>"
        "</summary>"
        f"<div class='notice-body'>{_escaped_lines(n.get('body'))}</div></details>"
        for n in notices
    )


def _page_employee_notices(agent: OfficeGenieAgent, auth: dict) -> None:
    _page_heading("공지사항", "회사 소식을 확인하세요. 제목을 누르면 내용이 펼쳐집니다.")
    result = agent.employee_call("ui_list_notices", auth["company_id"], auth["employee_id"])
    if result.get("code") == "INVALID_SESSION":
        _logout(result.get("message", ""))
        st.rerun()
    if not result.get("ok"):
        _read_error(result.get("message"), "employee_notices")
        return
    notices = result["data"]["announcements"]
    with st.container(key="notice_board", gap=None):
        query = _manager_term(
            st.text_input(
                "공지 검색", key="notice_query", placeholder="제목이나 내용으로 검색",
                label_visibility="collapsed", icon=":material/search:",
            )
        )
        matches = [n for n in notices if _notice_matches(n, query)]
        if not notices:
            body = "<div class='empty-state'><strong>등록된 공지사항이 없습니다.</strong><span>새 공지가 올라오면 여기에 표시됩니다.</span></div>"
        elif not matches:
            body = "<div class='empty-state'><strong>검색 결과가 없습니다.</strong><span>다른 단어로 검색해 보세요.</span></div>"
        else:
            body = f"<p class='list-count'>총 {len(matches)}건</p><div class='notice-list'>{_notice_details_html(matches)}</div>"
        st.markdown(body, unsafe_allow_html=True)


def _toggle_notice_public(agent: OfficeGenieAgent, auth: dict, notice: dict) -> None:
    result = _admin(
        agent, auth, "ui_admin_save_announcement", title=notice["title"], body=notice["body"],
        is_public=not notice["is_public"], announcement_id=notice["announcement_id"],
    )
    if result.get("ok"):
        st.session_state.manager_success = "공개 상태를 변경했습니다."
    else:
        st.session_state.manager_error = result.get("message") or CONNECTION_ERROR


def _manager_notices(agent: OfficeGenieAgent, auth: dict) -> None:
    """공지사항 관리: 목록에서 바로 수정·공개 전환, 위에서 새 공지 작성."""
    result = _admin_read(agent, auth, "ui_admin_list_announcements")
    if result is None:
        return
    notices = result["announcements"]
    mode, notice_id = st.session_state.get("manager_notices_selection", (None, None))
    error = st.session_state.pop("manager_error", "")
    if error:
        st.error(error)
    with st.container(horizontal=True, horizontal_alignment="distribute", vertical_alignment="center"):
        _manager_heading("공지사항 목록", "제목을 눌러 수정하거나 공개 상태를 바로 바꿀 수 있습니다.", f"총 {len(notices)}건")
        st.button("＋ 공지 작성", type="primary", on_click=_manager_select, args=("notices", "add"))
    if mode == "add":
        with st.container(key="notice_inline_form"):
            _manager_announcement_form(agent, auth, None)
    if not notices:
        st.markdown(
            "<div class='empty-state'><strong>등록된 공지사항이 없습니다.</strong>"
            "<span>‘＋ 공지 작성’으로 첫 공지를 올려 보세요.</span></div>",
            unsafe_allow_html=True,
        )
        return
    query, visibility = _manager_filter("notices", "제목 검색", ["공개", "비공개"], "전체 공개 상태")
    rows = [
        n for n in notices
        if query in _manager_term(n["title"])
        and (visibility is None or (visibility == "공개") == bool(n["is_public"]))
    ]
    if not rows:
        st.info("표시할 검색 결과가 없습니다.")
        return
    with st.container(key="notice_admin_list", gap=None):
        for notice in rows:
            nid = notice["announcement_id"]
            with st.container(key=f"notice_admin_row_{nid}", horizontal=True, vertical_alignment="center", gap="small"):
                badge = ("공개", "green") if notice["is_public"] else ("비공개", "gray")
                st.markdown(
                    f"<div class='notice-admin-title'><strong>{_escape(notice['title'])}</strong>"
                    f"<span>{_date_label(notice['published_at'] or notice['created_at'])}</span></div>",
                    unsafe_allow_html=True,
                )
                st.markdown(_manager_badge(*badge), unsafe_allow_html=True, width="content")
                st.button(
                    "비공개로" if notice["is_public"] else "공개하기", key=f"notice_toggle_{nid}",
                    on_click=_toggle_notice_public, args=(agent, auth, notice),
                )
                st.button(
                    "수정", key=f"manager_notices_row_{nid}", type="primary" if (mode, notice_id) == ("edit", nid) else "secondary",
                    on_click=_manager_select, args=("notices", "edit", nid),
                )
            if (mode, notice_id) == ("edit", nid):
                with st.container(key="notice_inline_form"):
                    _manager_announcement_form(agent, auth, notice)


def _page_admin_notices(agent: OfficeGenieAgent, auth: dict) -> None:
    _page_heading("공지사항 관리", "사원에게 전달할 소식을 작성하고 공개 여부를 관리하세요.")
    with st.container(key="manager_card"):
        _manager_notices(agent, auth)


def _manager_policies(agent: OfficeGenieAgent, auth: dict) -> None:
    result = _admin_read(agent, auth, "ui_admin_list_policies")
    if result is None:
        return
    policies = result["policies"]
    mode, policy_id = st.session_state.get("manager_policies_selection", (None, None))
    if mode == "detail":
        st.button("← 규정 목록", type="tertiary", on_click=_manager_select, args=("policies", None))
        policy = next((p for p in policies if p["id"] == policy_id), None)
        if not policy:
            st.info("해당 조항을 찾을 수 없습니다.")
            return
        with st.container(horizontal=True, horizontal_alignment="distribute"):
            st.subheader(policy["title"])
            st.markdown(_manager_badge(policy["category"], "blue"), unsafe_allow_html=True, width="content")
        version_label = "출처 문서 버전" if policy["source_version"] else "규정집 데이터 버전"
        _manager_info_grid((
            ("조항 번호", policy["article"]),
            ("출처", policy["source"]), (version_label, policy["source_version"] or result["data_version"]),
        ))
        prior_query = st.session_state.get("manager_policies_filters", ("", None))[0].strip()
        if prior_query:
            st.caption(f"검색 키워드 · {prior_query}")
        st.markdown("**조항 원문**")
        st.markdown(
            f"<div class='manager-policy-text'>{_escaped_lines(policy['content'])}</div>",
            unsafe_allow_html=True,
        )
        return
    categories = sorted({p["category"] for p in policies if p["category"]})
    query, category = _manager_filter("policies", "규정명, 조항 또는 키워드 검색", categories, "전체 분류")
    matches = [
        p for p in policies if (category is None or p["category"] == category)
        and any(query in _manager_term(field) for field in (p["title"], p["article"], *(p["keywords"] or [])))
    ]
    st.caption(f"총 {len(matches)}건")
    rows = [
        (p["id"], (
            p["category"], p["article"], p["title"],
            " · ".join(str(part) for part in (p["source"], p["source_version"]) if part) or "—",
        ))
        for p in matches
    ]
    _manager_table(
        "policies", ("분류", "조항", "규정명", "출처"),
        (1.55, 1, 2.4, 3, 1.05), rows, "원문 보기", "detail",
    )


# ---------------------------------------------------------------------------
# 사원 메뉴
# ---------------------------------------------------------------------------


def _employee_sidebar(agent: OfficeGenieAgent, auth: dict) -> dict | None:
    """본인 연차·신청·공지. 세션이 무효하면 로그아웃한다. 실패하면 None."""
    with st.container(key="employee_read_status", gap=None):
        with st.spinner("정보를 불러오는 중입니다."):
            result = agent.get_sidebar(auth["company_id"], auth["employee_id"])
        if result.get("code") == "INVALID_SESSION":
            _logout(result.get("message", ""))
            st.rerun()
        if not result.get("ok"):
            _read_error(result.get("message"), "employee_sidebar")
            return None
    return result.get("data") or {}


# ---------------------------------------------------------------------------
# 비품 신청: 품목 선택 → 남은 수량 안내 → 수량·목적 → 요약 확인 → 접수 확정 (검증은 서버)
# ---------------------------------------------------------------------------

EQUIPMENT_PURPOSE_MAX = 200


def _equipment_reset_form() -> None:
    for key in ("eq_item", "eq_quantity", "eq_purpose", "eq_preview", "eq_error"):
        st.session_state.pop(key, None)


def _equipment_preview(agent: OfficeGenieAgent, auth: dict) -> None:
    _touch_activity()
    state = st.session_state
    state.pop("eq_error", None)
    state.pop("eq_created", None)
    item = state.get("eq_item")
    if not item:
        state.eq_error = "신청할 품목을 선택해 주세요."
        return
    result = agent.employee_call(
        "ui_preview_ticket", auth["company_id"], auth["employee_id"],
        item=item, quantity=int(state.get("eq_quantity") or 1),
        purpose=str(state.get("eq_purpose") or "")[:EQUIPMENT_PURPOSE_MAX],
    )
    if result.get("code") == "NEEDS_CONFIRMATION":
        state.eq_preview = result["data"]["preview"]
    else:
        state.eq_preview = None
        state.eq_error = result.get("message") or CONNECTION_ERROR


def _equipment_confirm(agent: OfficeGenieAgent, auth: dict) -> None:
    _touch_activity()
    state = st.session_state
    preview = state.get("eq_preview")
    if not preview:
        return
    result = agent.employee_call(
        "ui_confirm_ticket", auth["company_id"], auth["employee_id"],
        item=preview["item"], quantity=int(preview["quantity"]), purpose=preview["purpose"],
    )
    if result.get("ok"):
        _equipment_reset_form()
        state.eq_created = result.get("message") or "신청이 접수되었습니다."
    else:
        state.eq_preview = None
        state.eq_error = result.get("message") or CONNECTION_ERROR


def _equipment_edit() -> None:
    st.session_state.pop("eq_preview", None)


def _quantity_note(option: dict) -> str:
    if option["remaining"] is None:
        return "이 품목은 1인 수량 한도가 정해져 있지 않습니다. 필요한 만큼 신청하면 검토 후 지급됩니다."
    if option["remaining"] == 0:
        return (
            f"1인 한도 {option['limit']}개를 이미 받았거나 신청 중입니다"
            f"(보유·처리 중 {option['held_or_pending']}개). 지금은 더 신청할 수 없습니다."
        )
    return (
        f"남은 신청 가능 수량 {option['remaining']}개 "
        f"(1인 한도 {option['limit']}개 · 보유·처리 중 {option['held_or_pending']}개)"
    )


def _render_equipment_form(agent: OfficeGenieAgent, auth: dict, options: list[dict]) -> None:
    state = st.session_state
    with st.container(key="equipment_form", gap=None):
        st.markdown("<div class='widget-head'><h3>새 비품 신청</h3></div>", unsafe_allow_html=True)
        created = state.pop("eq_created", "")
        if created:
            st.success(created)
        if not options:
            st.markdown(_empty_html("신청할 수 있는 비품이 아직 등록되지 않았습니다.", "관리자에게 문의해 주세요."), unsafe_allow_html=True)
            return
        preview = state.get("eq_preview")
        by_name = {o["item"]: o for o in options}
        if state.get("eq_item") not in by_name:
            state.pop("eq_item", None)
        st.pills(
            "1. 품목 선택", list(by_name), key="eq_item", disabled=bool(preview),
            format_func=lambda name: name if by_name[name]["remaining"] != 0 else f"{name} (한도 도달)",
        )
        option = by_name.get(state.get("eq_item"))
        if option is None:
            st.caption("허용된 품목만 신청할 수 있습니다.")
        else:
            blocked = option["remaining"] == 0
            (st.warning if blocked else st.info)(_quantity_note(option))
            maximum = option["remaining"] if option["remaining"] is not None else 20
            if not blocked:
                if int(state.get("eq_quantity") or 1) > maximum:
                    state.eq_quantity = maximum
                cols = st.columns([1, 2], gap="medium")
                cols[0].number_input("2. 수량", min_value=1, max_value=max(1, maximum), step=1, key="eq_quantity", disabled=bool(preview))
                cols[1].text_input(
                    "3. 사용 목적 (선택)", key="eq_purpose", max_chars=EQUIPMENT_PURPOSE_MAX,
                    placeholder="예: 개발 업무용 듀얼 모니터 구성", disabled=bool(preview),
                )
        error = state.pop("eq_error", "")
        if error:
            st.error(error)
        if preview:
            with st.container(key="equipment_confirm_card", gap=None):
                warnings = "".join(f"<div class='ticket-warning'>{_escape(w)}</div>" for w in preview.get("warnings") or [])
                reference = (
                    f"<span>근거 규정</span><strong>{_escape(preview['policy_reference'])}</strong>"
                    if preview.get("policy_reference") else ""
                )
                st.markdown(
                    "<div class='ticket-check-title'>4. 신청 내용을 확인해 주세요.</div>"
                    "<div class='ticket-check-details'>"
                    f"<span>품목</span><strong>{_escape(preview['item'])}</strong>"
                    f"<span>수량</span><strong>{_escape(preview['quantity'])}개</strong>"
                    f"<span>사용 목적</span><strong>{_escape(preview['purpose'])}</strong>{reference}</div>{warnings}"
                    "<p class='widget-note'>접수를 확정하면 신청 번호가 발급되고 관리자에게 전달됩니다.</p>",
                    unsafe_allow_html=True,
                )
                with st.container(horizontal=True, gap="small"):
                    st.button("접수 확정", key="eq_confirm", type="primary", on_click=_equipment_confirm, args=(agent, auth), width="stretch")
                    st.button("수정하기", key="eq_edit", on_click=_equipment_edit, width="stretch")
        elif option is not None and option["remaining"] != 0:
            st.button("신청 내용 확인", key="eq_check", type="primary", on_click=_equipment_preview, args=(agent, auth), width="stretch")


def _ticket_timeline_html(ticket: dict) -> str:
    steps = [("접수완료", ticket["created_at"], "신청 접수")]
    steps += [(h.get("to"), h.get("at"), h.get("note") or "") for h in ticket.get("history") or []]
    items = "".join(
        f"<li class='timeline-step status-{_equipment_status(str(status))[1]}'><i></i><div>"
        f"<strong>{_escape(_equipment_status(str(status))[0])}</strong>"
        f"<time>{_escape(_short_time(at))}</time>"
        + (f"<p>{_escape(note)}</p>" if note else "")
        + "</div></li>"
        for status, at, note in steps
    )
    return f"<ol class='ticket-timeline'>{items}</ol>"


def _my_ticket_html(ticket: dict) -> str:
    label, tone = _equipment_status(str(ticket["status"]))
    scheduled = ticket.get("scheduled_date")
    extra = (
        f"지급 예정일 {_escape(scheduled)}"
        if scheduled and ticket["status"] != "지급완료"
        else f"최근 변경 {_escape(_short_time(ticket.get('updated_at')))}"
    )
    purpose = f"<p class='resource-desc'>{_escape(ticket['purpose'])}</p>" if ticket.get("purpose") else ""
    return (
        "<details class='my-ticket'><summary>"
        f"{_equipment_icon(str(ticket['item']))}"
        f"<span class='resource-text'><strong>{_escape(ticket['item'])} × {_escape(ticket['quantity'])}</strong>"
        f"<span class='resource-meta'>{_escape(ticket['ticket_id'])} · {_escape(ticket['type'])} · "
        f"신청 {_escape(_date_label(ticket['created_at']))} · {extra}</span></span>"
        f"<span class='status-badge status-{tone}'><i></i>{_escape(label)}</span></summary>"
        f"<div class='my-ticket-body'>{purpose}{_ticket_timeline_html(ticket)}</div></details>"
    )


def _page_employee_equipment(agent: OfficeGenieAgent, auth: dict) -> None:
    _page_heading("비품 신청", "허용된 비품을 신청하고 처리 상황을 확인하세요.")
    options = agent.employee_call("ui_equipment_options", auth["company_id"], auth["employee_id"])
    mine = agent.employee_call("ui_my_tickets", auth["company_id"], auth["employee_id"])
    if "INVALID_SESSION" in (options.get("code"), mine.get("code")):
        _logout(options.get("message") or mine.get("message", ""))
        st.rerun()
    with st.container(key="equipment_layout", gap=None):
        if options.get("ok"):
            _render_equipment_form(agent, auth, options["data"]["items"])
        else:
            _read_error(options.get("message"), "equipment_options")
        with st.container(key="equipment_history", gap=None):
            tickets = (mine.get("data") or {}).get("tickets") if mine.get("ok") else None
            count = f"<span class='card-count'>전체 {len(tickets)}건</span>" if tickets is not None else ""
            st.markdown(f"<div class='widget-head'><h3>내 신청 내역</h3>{count}</div>", unsafe_allow_html=True)
            if tickets is None:
                _read_error(mine.get("message"), "my_tickets")
            elif not tickets:
                st.markdown(_empty_html("아직 신청한 내역이 없습니다.", "왼쪽에서 필요한 비품을 신청해 보세요."), unsafe_allow_html=True)
            else:
                st.markdown(
                    "<p class='widget-note'>항목을 누르면 처리 이력을 볼 수 있습니다.</p>"
                    + "".join(_my_ticket_html(t) for t in tickets),
                    unsafe_allow_html=True,
                )


# ---------------------------------------------------------------------------
# 홈: 사원증 + 요약 위젯
# ---------------------------------------------------------------------------

NEW_BADGE_DAYS = 7
STATUS_ORDER = ("접수완료", "검토중", "승인", "지급완료", "반려")


def _parse_time(value: object) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        try:
            parsed = datetime.fromisoformat(text[:10])
        except ValueError:
            return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=ZoneInfo("Asia/Seoul"))


def _is_new(value: object) -> bool:
    moment = _parse_time(value)
    if moment is None:
        return False
    age = datetime.now(ZoneInfo("Asia/Seoul")) - moment
    return age.total_seconds() >= -60 and age.days < NEW_BADGE_DAYS


def _date_label(value: object) -> str:
    moment = _parse_time(value)
    return moment.astimezone(ZoneInfo("Asia/Seoul")).strftime("%Y.%m.%d") if moment else "—"


def _barcode_svg(seed: str) -> str:
    """사번으로 만든 장식용 바코드(실제 인증 수단 아님)."""
    digest = hashlib.sha256(seed.encode("utf-8")).digest()
    x, bars = 0, []
    for byte in digest[:24]:
        for width in (1 + (byte & 3), 1 + ((byte >> 2) & 1)):
            bars.append(f"<rect x='{x}' y='0' width='{width}' height='36'/>")
            x += width + 1 + ((byte >> 4) & 1)
    return (
        f"<svg class='id-barcode' viewBox='0 0 {x} 36' preserveAspectRatio='none' "
        f"aria-hidden='true' fill='currentColor'>{''.join(bars)}</svg>"
    )


def _id_card_html(company_name: str, employee: dict) -> str:
    name = str(employee.get("name") or "")
    initial = name[:1] or "?"
    meta = " · ".join(
        _escape(part) for part in (employee.get("department"), employee.get("position")) if part
    )
    job = employee.get("job_title")
    week = employee.get("onboarding_week")
    badge = f"<span class='id-badge'>입사 {int(week)}주차</span>" if week else ""
    return (
        "<section class='id-card' aria-label='사원증'>"
        f"<header class='id-card-top'><span class='id-company'>{_escape(company_name)}</span>"
        "<span class='id-kind'>사원증</span></header>"
        "<div class='id-card-body'>"
        f"<div class='id-avatar' aria-hidden='true'>{_escape(initial)}</div>"
        f"<h2 class='id-name'>{_escape(name)}</h2>"
        f"<p class='id-meta'>{meta}</p>"
        + (f"<p class='id-job'>{_escape(job)}</p>" if job else "")
        + f"<div class='id-number'><span>사번</span><strong>{_escape(employee.get('employee_id'))}</strong>{badge}</div>"
        f"<div class='id-code'>{_barcode_svg(str(employee.get('employee_id') or ''))}"
        "<small>장식용 표시 · 출입·인증에 쓰지 않습니다</small></div>"
        "</div></section>"
    )


def _widget(key: str, title: str, body: str, *, more: str | None = None, count: str = "", interactive: bool = True) -> None:
    with st.container(key=f"home_widget_{key}", gap=None):
        count_html = f"<span class='card-count'>{_escape(count)}</span>" if count else ""
        st.markdown(
            f"<div class='widget-head'><h3>{_escape(title)}</h3>{count_html}</div>{body}",
            unsafe_allow_html=True,
        )
        if more and interactive:
            st.button(
                "더보기", key=f"home_more_{key}", type="tertiary", icon=":material/chevron_right:",
                icon_position="right", on_click=_go_page, args=(more,),
            )


def _leave_widget_body(leave: dict) -> str:
    details = []
    for label, key in (("부여", "granted_days"), ("사용", "used_days")):
        if leave.get(key) is not None:
            details.append(f"{label} {_escape(leave[key])}일")
    if leave.get("as_of"):
        details.append(f"기준일 {_escape(leave['as_of'])}")
    detail = f"<p class='widget-note'>{' · '.join(details)}</p>" if details else ""
    return _remaining_leave_value(leave.get("remaining_days")) + detail


def _ticket_widget_body(counts: dict, latest: dict | None) -> str:
    if not latest:
        return "<div class='dashboard-empty'>아직 신청한 비품이 없습니다.</div>"
    chips = "".join(
        f"<span class='status-badge status-{_equipment_status(status)[1]}'>"
        f"{_escape(_equipment_status(status)[0])} {counts[status]}</span>"
        for status in STATUS_ORDER
        if counts.get(status)
    )
    return (
        f"<div class='status-chips'>{chips}</div>"
        "<p class='widget-label'>최근 신청</p>"
        f"<div class='equipment-list compact'>{_equipment_row(latest)}</div>"
    )


def _notice_widget_body(notices: list[dict]) -> str:
    if not notices:
        return "<div class='dashboard-empty'>등록된 공지사항이 없습니다.</div>"
    rows = "".join(
        "<li class='notice-row'>"
        f"<span class='notice-title'>{_escape(n.get('title'))}"
        + ("<em class='new-badge'>NEW</em>" if _is_new(n.get("published_at") or n.get("created_at")) else "")
        + f"</span><time class='notice-date'>{_date_label(n.get('published_at') or n.get('created_at'))}</time></li>"
        for n in notices
    )
    return f"<ul class='notice-list widget-list'>{rows}</ul>"


def _tasks_widget_body(first_week: dict) -> str:
    tasks = first_week.get("tasks") or []
    if not tasks:
        return "<div class='dashboard-empty'>소속 부서의 1주 차 할 일이 아직 등록되지 않았습니다.</div>"
    items = "".join(
        "<li><span class='task-box' aria-hidden='true'></span>"
        f"<span>{_escape(task)}</span></li>"
        for task in tasks
    )
    title = first_week.get("title")
    source = f"<p class='widget-note'>{_escape(title)}</p>" if title else ""
    return f"{source}<ul class='task-list' aria-label='1주 차 할 일(읽기 전용)'>{items}</ul>"


def _render_home_widgets(data: dict, *, interactive: bool = True) -> None:
    with st.container(key="home_widgets", gap=None):
        _widget("leave", "잔여 연차", _leave_widget_body(data.get("annual_leave") or {}), interactive=interactive)
        _widget(
            "tickets", "내 비품 신청",
            _ticket_widget_body(data.get("ticket_counts") or {}, data.get("latest_ticket")),
            more="equipment", count=f"전체 {data.get('tickets_total', 0)}건", interactive=interactive,
        )
        _widget(
            "notices", "최근 공지", _notice_widget_body(data.get("announcements") or []),
            more="notices", count=f"전체 {data.get('announcements_total', 0)}건", interactive=interactive,
        )
        _widget(
            "tasks", "1주 차 할 일", _tasks_widget_body(data.get("first_week") or {}),
            more="library", interactive=interactive,
        )


def _render_employee_home(data: dict, *, interactive: bool = True) -> None:
    with st.container(key="home_layout", gap=None):
        with st.container(key="home_id_column", gap=None):
            st.markdown(_id_card_html(data.get("company_name") or "", data["employee"]), unsafe_allow_html=True)
        _render_home_widgets(data, interactive=interactive)


def _page_employee_home(agent: OfficeGenieAgent, auth: dict) -> None:
    name = auth.get("name") or ""
    _page_heading("나의 온보딩", f"{name}님, 반가워요. 회사 생활에 필요한 정보를 한눈에 확인하세요.")
    with st.spinner("정보를 불러오는 중입니다."):
        result = agent.employee_call("ui_get_home", auth["company_id"], auth["employee_id"])
    if result.get("code") == "INVALID_SESSION":
        _logout(result.get("message", ""))
        st.rerun()
    if not result.get("ok"):
        _read_error(result.get("message"), "employee_home")
        return
    _render_employee_home(result["data"])
    st.markdown(
        "<p class='dashboard-help'>궁금한 점이 있나요? 오른쪽 아래 지니에게 물어보세요.</p>",
        unsafe_allow_html=True,
    )


# ---- 관리자 홈 ----------------------------------------------------------------


def _close_preview() -> None:
    st.session_state.pop("preview_employee_id", None)


def _open_preview() -> None:
    picked = st.session_state.get("preview_pick")
    if picked:
        st.session_state.preview_employee_id = picked


def _render_admin_preview(agent: OfficeGenieAgent, auth: dict, employee_id: str) -> None:
    result = _admin(agent, auth, "ui_admin_preview_employee", employee_id=employee_id)
    with st.container(key="preview_banner", horizontal=True, horizontal_alignment="distribute", vertical_alignment="center"):
        if result.get("ok"):
            who = result["data"]["employee"]
            label = f"직원 화면 미리보기 · {who['name']} ({who['employee_id']}) · 읽기 전용"
        else:
            label = "직원 화면 미리보기"
        st.markdown(f"<span class='preview-label'>{_escape(label)}</span>", unsafe_allow_html=True, width="content")
        st.button("미리보기 닫기", key="preview_close", on_click=_close_preview)
    if not result.get("ok"):
        st.error(result.get("message") or CONNECTION_ERROR)
        return
    st.caption("사원이 보는 홈 화면입니다. 미리보기에서는 신청·변경을 할 수 없습니다.")
    _render_employee_home(result["data"], interactive=False)


def _page_admin_home(agent: OfficeGenieAgent, auth: dict) -> None:
    preview_id = st.session_state.get("preview_employee_id")
    if preview_id:
        _render_admin_preview(agent, auth, preview_id)
        return
    _page_heading("관리자 홈", "회사 현황을 확인하고 위 메뉴에서 업무를 관리하세요.")
    data = _admin_read(agent, auth, "ui_admin_home")
    if data is None:
        return
    with st.container(key="admin_home_layout", gap=None):
        with st.container(key="admin_company_card", gap=None):
            st.markdown(
                f"<span class='eyebrow'>회사</span><h2 class='company-name'>{_escape(data['company_name'])}</h2>"
                f"<p class='widget-note'>사원 {data['employee_count']}명</p>",
                unsafe_allow_html=True,
            )
            _company_code_copy(data={"code": data["company_code"]}, key="home_company_code")
        with st.container(key="home_widgets", gap=None):
            pending = data["pending_tickets"]
            pending_body = (
                "<div class='equipment-list compact'>" + "".join(
                    "<div class='equipment-row'>"
                    f"{_equipment_icon(str(t['item']))}<div class='equipment-name'>{_escape(t['item'])}"
                    f"<span class='equipment-quantity'>× {_escape(t['quantity'])}</span>"
                    f"<small class='equipment-date'>{_escape(t['name'])} · {_escape(t['department'])}</small></div>"
                    f"<span class='status-badge status-{_equipment_status(t['status'])[1]}'><i></i>"
                    f"{_escape(_equipment_status(t['status'])[0])}</span></div>"
                    for t in pending
                ) + "</div>"
                if pending else "<div class='dashboard-empty'>처리를 기다리는 신청이 없습니다.</div>"
            )
            _widget("pending", "처리 대기 신청", pending_body, more="equipment", count=f"{data['pending_count']}건")
            _widget("notices", "최근 공지", _notice_widget_body(data["announcements"]), more="notices")
            uploads = data["recent_uploads"]
            upload_body = (
                "<ul class='notice-list widget-list'>" + "".join(
                    f"<li class='notice-row'><span>{_escape(', '.join(u['kinds']) or '회사 데이터')}</span>"
                    f"<time class='notice-date'>{_date_label(u['at'])}</time></li>"
                    for u in uploads
                ) + "</ul>"
                if uploads else "<div class='dashboard-empty'>아직 업로드한 회사 데이터가 없습니다.</div>"
            )
            _widget("uploads", "최근 업로드", upload_body, more="data")
            with st.container(key="home_widget_preview", gap=None):
                st.markdown(
                    "<div class='widget-head'><h3>직원 화면 미리보기</h3></div>"
                    "<p class='widget-note'>사원을 골라 그 사원이 보는 홈 화면을 읽기 전용으로 확인합니다.</p>",
                    unsafe_allow_html=True,
                )
                people = data["employees"]
                if not people:
                    st.caption("등록된 사원이 없습니다.")
                else:
                    labels = {p["employee_id"]: f"{p['name']} · {p['department']} ({p['employee_id']})" for p in people}
                    st.selectbox("사원 선택", list(labels), format_func=labels.get, key="preview_pick")
                    st.button("미리보기", key="preview_open", type="primary", on_click=_open_preview)


# ---------------------------------------------------------------------------
# 자료실
# ---------------------------------------------------------------------------

LIBRARY_TABS = ("전체", "규정집", "부서 자료", "양식·기타")
RESOURCE_UPLOAD_TYPES = ["pdf", "docx", "xlsx", "pptx", "hwp", "png", "jpg", "jpeg", "txt"]
RESOURCE_MAX_MB = 5
_RESOURCE_ICONS = {
    "link": ":material/link:", "pdf": ":material/picture_as_pdf:", "png": ":material/image:",
    "jpg": ":material/image:", "jpeg": ":material/image:", "xlsx": ":material/table_chart:",
    "pptx": ":material/slideshow:", "docx": ":material/description:", "hwp": ":material/description:",
    "txt": ":material/article:",
}


def _resource_ext(resource: dict) -> str:
    name = str(resource.get("file_name") or "")
    return name.rpartition(".")[2].lower() if "." in name else ""


def _size_text(size: object) -> str:
    try:
        size = int(size or 0)
    except (TypeError, ValueError):
        return ""
    if size >= 1024 * 1024:
        return f"{size / (1024 * 1024):.1f}MB"
    return f"{max(1, size // 1024)}KB"


def _resource_matches(resource: dict, query: str) -> bool:
    return not query or any(
        query in _manager_term(resource.get(field)) for field in ("title", "description", "category", "file_name")
    )


def _download_loader(tool: str, auth: dict, resource_id: str, admin: bool = False):
    """다운로드 버튼을 누를 때만 서버에서 파일을 받는다(서버가 권한을 다시 확인)."""
    agent = get_agent()
    company_id = auth["company_id"]
    identity = auth["account_id"] if admin else auth["employee_id"]

    def load() -> bytes:
        if admin:
            result = agent.admin_call(tool, company_id, identity, resource_id=resource_id)
        else:
            result = agent.employee_call(tool, company_id, identity, resource_id=resource_id)
        data = result.get("data") or {}
        if not result.get("ok") or data.get("kind") != "file":
            return (result.get("message") or CONNECTION_ERROR).encode("utf-8")
        return base64.b64decode(data["content_base64"])

    return load


def _resource_meta_html(resource: dict, *, admin: bool = False) -> str:
    kind = "링크" if resource["kind"] == "link" else (_resource_ext(resource).upper() or "파일")
    parts = [kind]
    if resource["kind"] == "file" and resource.get("size_bytes"):
        parts.append(_size_text(resource["size_bytes"]))
    parts.append(_date_label(resource.get("updated_at")))
    chips = f"<span class='resource-chip'>{_escape(resource.get('category'))}</span>"
    if resource.get("visibility") == "department":
        chips += f"<span class='resource-chip dept'>{_escape(resource.get('department'))} 전용</span>"
    if admin and not resource.get("is_public"):
        chips += "<span class='resource-chip muted'>비공개</span>"
    description = (
        f"<p class='resource-desc'>{_escape(resource.get('description'))}</p>" if resource.get("description") else ""
    )
    return (
        f"<div class='resource-text'><div class='resource-title'><strong>{_escape(resource['title'])}</strong>{chips}</div>"
        f"{description}<span class='resource-meta'>{_escape(' · '.join(parts))}</span></div>"
    )


def _resource_action(resource: dict, auth: dict, key: str, *, admin: bool = False) -> None:
    if resource["kind"] == "link":
        st.link_button("열기", resource.get("url") or "#", icon=":material/open_in_new:")
        return
    st.download_button(
        "내려받기",
        data=_download_loader(
            "ui_admin_download_resource" if admin else "ui_download_resource",
            auth, resource["resource_id"], admin,
        ),
        file_name=resource.get("file_name") or "download",
        mime=(resource.get("mime_type") or "application/octet-stream").split(";")[0],
        key=key,
        icon=":material/download:",
        on_click="ignore",
    )


def _render_resource_rows(resources: list[dict], auth: dict, prefix: str) -> None:
    for resource in resources:
        rid = resource["resource_id"]
        with st.container(key=f"{prefix}_row_{rid}", horizontal=True, vertical_alignment="center", gap="small"):
            st.markdown(
                f"<span class='resource-icon'>{_escape(_resource_ext(resource) or 'URL')}</span>",
                unsafe_allow_html=True, width="content",
            )
            st.markdown(_resource_meta_html(resource), unsafe_allow_html=True)
            _resource_action(resource, auth, f"{prefix}_get_{rid}")


def _empty_html(title: str, hint: str = "") -> str:
    hint_html = f"<span>{_escape(hint)}</span>" if hint else ""
    return f"<div class='empty-state'><strong>{_escape(title)}</strong>{hint_html}</div>"


def _handover_html(handover: dict) -> str:
    sections = "".join(
        f"<h4>{_escape(heading)}</h4><ul>"
        + "".join(f"<li>{_escape(item)}</li>" for item in (items if isinstance(items, list) else [items]))
        + "</ul>"
        for heading, items in (handover.get("summary") or {}).items()
    )
    notes = handover.get("notes") or []
    notes_html = (
        "<h4>주의사항</h4><ul>" + "".join(f"<li>{_escape(n)}</li>" for n in notes) + "</ul>" if notes else ""
    )
    updated = f" · 최종 수정 {_escape(handover['last_updated'])}" if handover.get("last_updated") else ""
    return (
        "<details class='handover-card'><summary>"
        f"<span class='resource-icon'>DOC</span><span class='resource-text'><strong>{_escape(handover.get('title'))}</strong>"
        f"<span class='resource-meta'>인수인계서{updated}</span></span></summary>"
        f"<div class='handover-body'>{sections}{notes_html}</div></details>"
    )


def _policy_shelf_html(groups: list[dict], query: str) -> tuple[str, int]:
    blocks, total = [], 0
    for group in groups:
        rows = [
            p for p in group["policies"]
            if not query or any(query in _manager_term(p.get(f)) for f in ("title", "article", "content"))
        ]
        if not rows:
            continue
        total += len(rows)
        badge = "<em class='ref-badge'>참고용</em>" if group["is_reference_only"] else "<em class='own-badge'>회사 내규</em>"
        items = "".join(
            "<details class='notice-entry notice-page-entry'><summary class='notice-row'>"
            f"<span class='notice-title'>{_escape(p.get('article'))} {_escape(p.get('title'))}</span></summary>"
            f"<div class='notice-body'>{_escaped_lines(p.get('content'))}</div></details>"
            for p in rows
        )
        blocks.append(
            f"<details class='policy-group'{' open' if query else ''}><summary><strong>{_escape(group['category'])}</strong>"
            f"{badge}<span class='card-count'>{len(rows)}개 조항</span></summary>"
            f"<div class='notice-list'>{items}</div></details>"
        )
    note = (
        "<p class='widget-note'>‘참고용’은 회사 자체 규정이 아닌 표준취업규칙 참고 조문입니다.</p>"
        if any(g["is_reference_only"] for g in groups) else ""
    )
    return note + "".join(blocks), total


def _page_employee_library(agent: OfficeGenieAgent, auth: dict) -> None:
    _page_heading("자료실", "회사 규정과 부서 자료, 양식을 한곳에서 찾아보세요.")
    result = agent.employee_call("ui_list_resources", auth["company_id"], auth["employee_id"])
    if result.get("code") == "INVALID_SESSION":
        _logout(result.get("message", ""))
        st.rerun()
    if not result.get("ok"):
        _read_error(result.get("message"), "employee_library")
        return
    data = result["data"]
    with st.container(key="library_board", gap=None):
        query = _manager_term(
            st.text_input(
                "자료 검색", key="library_query", placeholder="제목이나 설명으로 검색",
                label_visibility="collapsed", icon=":material/search:",
            )
        )
        resources = [r for r in data["resources"] if _resource_matches(r, query)]
        dept_resources = [r for r in resources if r["visibility"] == "department"]
        common = [r for r in resources if r["visibility"] != "department"]
        handovers = [
            h for h in data["handovers"]
            if not query or query in _manager_term(h.get("title")) or query in _manager_term(json.dumps(h.get("summary"), ensure_ascii=False))
        ]
        tabs = st.tabs(list(LIBRARY_TABS), key="library_tabs", on_change="rerun")
        for tab, name in zip(tabs, LIBRARY_TABS):
            if not tab.open:
                continue
            with tab:
                if name == "규정집":
                    html_text, total = _policy_shelf_html(data["policies"], query)
                    st.markdown(
                        html_text if total else _empty_html("찾는 규정이 없습니다." if query else "등록된 규정이 없습니다."),
                        unsafe_allow_html=True,
                    )
                    continue
                if name == "부서 자료":
                    st.markdown(
                        f"<p class='list-count'>{_escape(data['department'])} 자료 · "
                        f"{len(handovers) + len(dept_resources)}건</p>"
                        + "".join(_handover_html(h) for h in handovers),
                        unsafe_allow_html=True,
                    )
                    _render_resource_rows(dept_resources, auth, "lib_dept")
                    if not handovers and not dept_resources:
                        st.markdown(_empty_html("소속 부서 자료가 없습니다.", "부서 자료는 같은 부서 사원에게만 보입니다."), unsafe_allow_html=True)
                    continue
                rows = common if name == "양식·기타" else resources
                if name == "전체":
                    st.markdown(
                        f"<p class='list-count'>파일·링크 {len(rows)}건 · 인수인계서 {len(handovers)}건 · "
                        "규정은 ‘규정집’ 탭에서 볼 수 있습니다.</p>"
                        + "".join(_handover_html(h) for h in handovers),
                        unsafe_allow_html=True,
                    )
                _render_resource_rows(rows, auth, f"lib_{LIBRARY_TABS.index(name)}")
                if not rows and (name != "전체" or not handovers):
                    st.markdown(
                        _empty_html("검색 결과가 없습니다." if query else "아직 등록된 자료가 없습니다.",
                                    "다른 단어로 검색해 보세요." if query else "관리자가 자료를 올리면 여기에 표시됩니다."),
                        unsafe_allow_html=True,
                    )


# ---- 자료실 관리(관리자) -------------------------------------------------------

_RESOURCE_FORM_KEYS = (
    "res_title", "res_category", "res_desc", "res_kind", "res_url", "res_visibility", "res_department", "res_public",
)


def _resource_form_open(resource: dict | None) -> None:
    state = st.session_state
    resource = resource or {}
    state.resource_editing = resource.get("resource_id") or "new"
    state.res_title = resource.get("title", "")
    state.res_category = resource.get("category")
    state.res_desc = resource.get("description") or ""
    state.res_kind = "링크" if resource.get("kind") == "link" else "파일"
    state.res_url = resource.get("url") or ""
    state.res_visibility = "특정 부서" if resource.get("visibility") == "department" else "전체"
    state.res_department = resource.get("department")
    state.res_public = bool(resource.get("is_public", True))
    state.res_uploader_n = state.get("res_uploader_n", 0) + 1
    state.pop("resource_delete", None)


def _resource_form_close() -> None:
    st.session_state.pop("resource_editing", None)
    for key in _RESOURCE_FORM_KEYS:
        st.session_state.pop(key, None)


def _resource_save(agent: OfficeGenieAgent, auth: dict) -> None:
    state = st.session_state
    editing = state.get("resource_editing")
    uploaded = state.get(f"res_file_{state.get('res_uploader_n', 0)}")
    kind = "link" if state.get("res_kind") == "링크" else "file"
    content = ""
    file_name = ""
    if kind == "file" and uploaded is not None:
        raw = uploaded.getvalue()
        if len(raw) > RESOURCE_MAX_MB * 1024 * 1024:
            state.resource_error = f"파일 크기는 {RESOURCE_MAX_MB}MB 이하여야 합니다. (선택한 파일: {_size_text(len(raw))})"
            return
        content = base64.b64encode(raw).decode("ascii")
        file_name = uploaded.name
    result = _admin(
        agent, auth, "ui_admin_save_resource",
        title=state.get("res_title", ""), category=state.get("res_category") or "",
        description=state.get("res_desc", ""), kind=kind, url=state.get("res_url", ""),
        file_name=file_name, content_base64=content,
        visibility="department" if state.get("res_visibility") == "특정 부서" else "all",
        department=state.get("res_department") or "", is_public=bool(state.get("res_public", True)),
        resource_id=None if editing == "new" else editing,
    )
    if not result.get("ok"):
        state.resource_error = result.get("message") or CONNECTION_ERROR
        return
    state.manager_success = result.get("message") or "자료를 저장했습니다."
    _resource_form_close()


def _resource_delete(agent: OfficeGenieAgent, auth: dict, resource_id: str) -> None:
    result = _admin(agent, auth, "ui_admin_delete_resource", resource_id=resource_id)
    st.session_state.pop("resource_delete", None)
    if result.get("ok"):
        st.session_state.manager_success = result.get("message") or "자료를 삭제했습니다."
    else:
        st.session_state.resource_error = result.get("message") or CONNECTION_ERROR


def _render_resource_form(agent: OfficeGenieAgent, auth: dict, data: dict) -> None:
    state = st.session_state
    editing = state.get("resource_editing")
    with st.container(key="resource_form"):
        _manager_heading("자료 등록" if editing == "new" else "자료 수정", "파일을 올리거나 링크를 등록하세요.")
        cols = st.columns(2, gap="large")
        cols[0].text_input("제목", key="res_title", max_chars=200)
        cols[1].selectbox("카테고리", data["categories"], key="res_category", placeholder="카테고리 선택", index=None)
        st.text_area("설명 (선택)", key="res_desc", max_chars=1000, height=90)
        st.radio("종류", ["파일", "링크"], key="res_kind", horizontal=True)
        if state.get("res_kind") == "링크":
            st.text_input("링크 주소", key="res_url", placeholder="https://")
        else:
            st.file_uploader(
                f"파일 ({data['allowed_types']} · {RESOURCE_MAX_MB}MB 이하)",
                type=RESOURCE_UPLOAD_TYPES, max_upload_size=RESOURCE_MAX_MB,
                key=f"res_file_{state.get('res_uploader_n', 0)}",
            )
            if editing != "new":
                st.caption("새 파일을 고르지 않으면 기존 파일을 그대로 둡니다.")
        cols = st.columns(2, gap="large")
        cols[0].radio("공개 범위", ["전체", "특정 부서"], key="res_visibility", horizontal=True)
        if state.get("res_visibility") == "특정 부서":
            cols[1].selectbox("부서", data["departments"], key="res_department", index=None, placeholder="부서 선택")
        st.toggle("사원에게 공개", key="res_public")
        error = state.pop("resource_error", "")
        if error:
            st.error(error)
        with st.container(horizontal=True, horizontal_alignment="right"):
            st.button("취소", key="resource_cancel", on_click=_resource_form_close)
            st.button("저장", key="resource_save", type="primary", on_click=_resource_save, args=(agent, auth))


def _page_admin_library(agent: OfficeGenieAgent, auth: dict) -> None:
    _page_heading("자료실 관리", "사원에게 공유할 파일과 링크를 올리고 공개 범위를 정하세요.")
    data = _admin_read(agent, auth, "ui_admin_list_resources")
    if data is None:
        return
    resources = data["resources"]
    with st.container(key="manager_card"):
        error = st.session_state.get("resource_error") if not st.session_state.get("resource_editing") else ""
        if error:
            st.session_state.pop("resource_error", None)
            st.error(error)
        with st.container(horizontal=True, horizontal_alignment="distribute", vertical_alignment="center"):
            _manager_heading("자료 목록", f"형식: {data['allowed_types']} · 파일당 {RESOURCE_MAX_MB}MB 이하", f"총 {len(resources)}건")
            st.button("＋ 자료 등록", key="resource_new", type="primary", on_click=_resource_form_open, args=(None,))
        if st.session_state.get("resource_editing"):
            _render_resource_form(agent, auth, data)
        if not resources:
            st.markdown(_empty_html("등록된 자료가 없습니다.", "‘＋ 자료 등록’으로 양식이나 안내 자료를 올려 보세요."), unsafe_allow_html=True)
            return
        query = _manager_term(st.text_input("자료 검색", key="admin_library_query", placeholder="제목·설명 검색", label_visibility="collapsed", icon=":material/search:"))
        rows = [r for r in resources if _resource_matches(r, query)]
        if not rows:
            st.info("표시할 검색 결과가 없습니다.")
        for resource in rows:
            rid = resource["resource_id"]
            with st.container(key=f"admin_res_row_{rid}", horizontal=True, vertical_alignment="center", gap="small"):
                st.markdown(f"<span class='resource-icon'>{_escape(_resource_ext(resource) or 'URL')}</span>", unsafe_allow_html=True, width="content")
                st.markdown(_resource_meta_html(resource, admin=True), unsafe_allow_html=True)
                _resource_action(resource, auth, f"admin_res_get_{rid}", admin=True)
                st.button("수정", key=f"admin_res_edit_{rid}", on_click=_resource_form_open, args=(resource,))
                st.button("삭제", key=f"admin_res_delete_{rid}", on_click=lambda r=rid: st.session_state.update(resource_delete=r))
            if st.session_state.get("resource_delete") == rid:
                with st.container(key="resource_delete_confirm", horizontal=True, vertical_alignment="center"):
                    st.markdown(f"<span class='preview-label'>‘{_escape(resource['title'])}’을(를) 삭제할까요? 되돌릴 수 없습니다.</span>", unsafe_allow_html=True)
                    st.button("삭제 확인", key="resource_delete_yes", type="primary", on_click=_resource_delete, args=(agent, auth, rid))
                    st.button("취소", key="resource_delete_no", on_click=lambda: st.session_state.pop("resource_delete", None))


# ---------------------------------------------------------------------------
# 관리자 메뉴. 모든 호출은 agent.admin_call로 가고, 서버가 관리자 여부를 다시 확인한다.
# ---------------------------------------------------------------------------


def _admin_section_tabs(key: str, sections: list[tuple[str, str, object]]) -> None:
    """관리자 메뉴 안의 하위 탭. 선택한 탭만 그린다."""
    tabs = st.tabs([label for label, _, _ in sections], key=key, on_change="rerun")
    for tab, (_, section, render) in zip(tabs, sections):
        if tab.open:
            with tab:
                with st.container(key="manager_card"):
                    selection = st.session_state.get(f"manager_{section}_selection", (None, None))
                    with _view_container(selection, f"_manager_{section}_slot"):
                        render()


def _page_admin_equipment(agent: OfficeGenieAgent, auth: dict) -> None:
    _page_heading("비품 관리", "사원이 신청한 비품의 지급 상태와 처리 이력을 관리하세요.")
    _admin_section_tabs("equipment_tabs", [
        ("비품 지급", "equipment", lambda: _manager_equipment(agent, auth)),
        ("신청 상태·이력", "tickets", lambda: _admin_tickets_tab(agent, auth)),
    ])


def _page_admin_employees(agent: OfficeGenieAgent, auth: dict) -> None:
    _page_heading("사원", "회사에 소속된 사원 정보를 확인하고 관리하세요.")
    _admin_section_tabs("employees_tabs", [
        ("사원 목록", "employees", lambda: _manager_employees(agent, auth)),
        ("사원 상세", "employee_detail", lambda: _admin_employees_tab(agent, auth)),
    ])


def _page_admin_data(agent: OfficeGenieAgent, auth: dict) -> None:
    _page_heading("데이터·계정", "회사 데이터를 올리고 규정 원문과 계정을 관리하세요.")
    info = _admin_read(agent, auth, "ui_admin_overview")
    if info is None:
        return
    with st.container(key="settings_code_card", gap=None):
        st.markdown(
            "<div class='company-code-label'><strong>회사 코드</strong>사원들에게 알려주세요</div>"
            f"<div class='company-code-big'>{_escape(info['company_code'])}</div>",
            unsafe_allow_html=True,
        )
        _company_code_copy(data={"code": info["company_code"]}, key="settings_company_code")

    def policies() -> None:
        if not st.session_state.get("manager_policies_selection", (None, None))[0]:
            _manager_heading("사내 규정", "챗봇이 답변에 참고하는 사내 규정 원문을 확인할 수 있습니다.")
        _manager_policies(agent, auth)

    def upload() -> None:
        with st.container(key="settings_data"):
            _admin_data_tab(agent, auth, info)

    _admin_section_tabs("data_tabs", [
        ("자료 업로드", "upload", upload),
        ("사내 규정", "policies", policies),
        ("계정", "accounts", lambda: _admin_accounts_tab(agent, auth)),
    ])


def _current_view(auth: dict | None) -> str:
    if not auth:
        entry_view = "login" if st.query_params.get("view") == "login" else "landing"
        return st.session_state.get("auth_view", entry_view)
    if auth.get("must_change"):
        return "change_password"
    return "app"


def _view_container(view: str | tuple, state_key: str = "_screen_slot"):
    # Streamlit keeps children at a block's old position until the new run finishes.
    # Reuse that position on refresh, and explicitly empty it before rendering another view.
    previous_view, slot = st.session_state.get(state_key, (view, 0))
    if view != previous_view:
        slot = 1 - slot
    with st.container(gap=None):
        if slot == 0:
            screen = st.container()
            st.empty()
        else:
            st.empty()
            screen = st.container()
    st.session_state[state_key] = (view, slot)
    return screen


def _render_view(view: str, auth: dict | None, page: str | None = None, denied: str | None = None) -> None:
    if not auth:
        if view == "register":
            _render_register()
        elif view == "registered":
            _render_registered()
        elif view == "login":
            _render_login()
        else:
            _render_landing()
        return

    try:
        _touch_activity()
        _idle_watch()
        if view == "change_password":
            _render_change_password(auth)
            return

        with st.container(key="db_screen"):
            agent = get_agent()
            if not _session_still_valid(agent):
                _logout("로그인 정보가 변경되어 다시 로그인이 필요합니다.")
                st.rerun()
            if denied and auth.get("role") != "admin":
                agent.employee_call(
                    "ui_record_page_denied", auth["company_id"], auth["employee_id"], page=denied,
                )
                st.session_state.page_notice = PAGE_DENIED_NOTICE
            _render_app(agent, auth, page or "home")
    except (RerunException, StopException):
        raise
    except Exception as exc:
        # 로그인 이후 화면 오류를 "로그인 실패"처럼 보이게 하지 않는다.
        _log_post_login_failure("render", exc, auth)
        _render_post_login_error()


EMPLOYEE_PAGES = {
    "home": _page_employee_home,
    "notices": _page_employee_notices,
    "library": _page_employee_library,
    "equipment": _page_employee_equipment,
}
ADMIN_PAGES = {
    "home": _page_admin_home,
    "notices": _page_admin_notices,
    "library": _page_admin_library,
    "equipment": _page_admin_equipment,
    "employees": _page_admin_employees,
    "data": _page_admin_data,
}


def main() -> None:
    _check_pwa_setup()
    _render_styles()
    _render_shell_styles()
    _restore_login()
    if _session_expired():
        _logout("30분 동안 사용하지 않아 로그아웃되었습니다. 다시 로그인해 주세요.")
    _apply_cookie_action()

    auth = st.session_state.get("auth")
    view = _current_view(auth)
    page = denied = None
    if view == "app":
        page, denied = _resolve_page(auth)
    with _view_container(f"app:{page}" if page else view):
        _render_view(view, auth, page, denied)


main()
