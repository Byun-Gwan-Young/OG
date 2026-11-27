from __future__ import annotations

import csv
import html
import io
import logging
import time
import traceback
from concurrent.futures import Future, ThreadPoolExecutor
from datetime import datetime
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
_SAFE_MESSAGE_ERRORS = (KeyError, IndexError, AttributeError, TypeError, ValueError)

st.set_page_config(page_title="오피스 지니 | 신입사원 온보딩", layout="wide")

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
SESSION_TIMEOUT_SEC = 30 * 60  # 30분 무동작 시 자동 로그아웃
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


def _escape(value: object) -> str:
    return html.escape(str(value if value is not None else ""))


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


def _render_dashboard(
    employee: dict, tickets: list[dict], remaining_leave: object = None
) -> None:
    now = datetime.now(ZoneInfo("Asia/Seoul"))
    weekdays = ("월", "화", "수", "목", "금", "토", "일")
    date_label = f"{now.month}월 {now.day}일 {weekdays[now.weekday()]}요일"
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

    announcements = (
        ("신입사원 오리엔테이션 안내", f"{now.month}월 24일"),
        ("사내 계정 초기 설정 안내", f"{now.month}월 22일"),
        ("사무실 출입카드 수령 안내", f"{now.month}월 21일"),
    )
    announcement_rows = "".join(
        "<div class='notice-row'>"
        f"<span>{_escape(title)}</span><span class='notice-date'>{_escape(date)}</span>"
        "</div>"
        for title, date in announcements
    )

    equipment = [ticket for ticket in tickets if ticket.get("type") == "비품"]
    equipment_count = sum(int(ticket.get("quantity") or 1) for ticket in equipment)
    if equipment:
        equipment_rows = "".join(
            "<div class='equipment-row'>"
            f"{_equipment_icon(str(ticket.get('item') or '비품'))}"
            "<div class='equipment-name'>"
            f"{_escape(ticket.get('item') or '비품')}"
            f"<span class='equipment-quantity'>× {_escape(ticket.get('quantity') or 1)}</span>"
            "</div>"
            f"<span class='status-badge status-{_equipment_status(str(ticket.get('status') or ''))[1]}'>"
            f"<i></i>{_escape(_equipment_status(str(ticket.get('status') or ''))[0])}</span>"
            "</div>"
            for ticket in equipment
        )
    else:
        equipment_rows = (
            "<div class='equipment-empty'>아직 등록된 비품 내역이 없습니다.</div>"
        )

    chat_logo = (
        "<svg viewBox='0 0 32 32' fill='none' aria-hidden='true'>"
        "<path d='M8.3 8.2h15.4a2.5 2.5 0 0 1 2.5 2.5v8.1a2.5 2.5 0 0 1-2.5 2.5h-7.2l-5.2 3.5v-3.5h-3a2.5 2.5 0 0 1-2.5-2.5v-8.1a2.5 2.5 0 0 1 2.5-2.5Z' stroke='white' stroke-width='1.8' stroke-linejoin='round'/>"
        "<path d='M11 13h10M11 16.5h7' stroke='white' stroke-width='1.7' stroke-linecap='round'/>"
        "</svg>"
    )
    person_icon = (
        "<svg viewBox='0 0 64 64' fill='none' aria-hidden='true'>"
        "<circle cx='32' cy='23' r='9.5' stroke='#8D9EB5' stroke-width='1.7'/>"
        "<path d='M16 51v-4.5C16 37.9 23.2 31 32 31s16 6.9 16 15.5V51' stroke='#8D9EB5' stroke-width='1.7' stroke-linecap='round'/>"
        "</svg>"
    )
    dashboard_html = f"""
    <div class="dashboard-ui">
      <header class="site-header">
        <div class="header-inner">
          <span class="header-logo">{chat_logo}</span>
          <span class="brand-name">오피스 지니</span>
          <span class="brand-divider"></span>
          <span class="brand-subtitle">신입사원 온보딩</span>
          <span class="header-user">{name}({department})</span>
        </div>
      </header>
      <div class="dashboard-main">
        <div class="welcome-block">
          <div>
            <h1>나의 온보딩</h1>
            <p>{name}님, 반가워요. 회사 생활에 필요한 정보를 한눈에 확인하세요.</p>
          </div>
          <span class="dashboard-date">{_escape(date_label)}</span>
        </div>
        <div class="profile-card">
          <div class="employee-placeholder">{person_icon}</div>
          <div class="profile-copy">
            <span class="eyebrow">내 정보</span>
            <h2>{name}</h2>
            <div class="profile-meta">{profile_meta}</div>
          </div>
          <div class="leave-summary">
            <span class="eyebrow">잔여 연차</span>
            {_remaining_leave_value(remaining_leave)}
          </div>
          <span class="new-employee-badge">신입사원</span>
        </div>
        <div class="dashboard-grid">
          <div class="dashboard-card notice-card">
            <div class="card-heading">
              <div><h2>공지사항</h2><p>새로운 회사 소식을 확인하세요.</p></div>
              <span class="card-count">전체 3건</span>
            </div>
            <div class="notice-list">{announcement_rows}</div>
          </div>
          <div class="dashboard-card equipment-card">
            <div class="card-heading">
              <div><h2>내 비품 내역</h2><p>나에게 배정된 비품과 지급 상태예요.</p></div>
              <span class="card-count">총 {equipment_count}개</span>
            </div>
            <div class="equipment-list">{equipment_rows}</div>
          </div>
        </div>
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
        .st-key-dashboard_root { gap: 0 !important; margin-top: -16px !important; }
        .dashboard-ui { min-height: 100vh; background: var(--page); color: var(--title); }
        .site-header {
            height: 80px;
            box-sizing: border-box;
            background: #fff;
            border: 1px solid var(--line);
        }
        .header-inner {
            width: min(1280px, calc(100% - 48px));
            height: 100%;
            margin: 0 auto;
            display: flex;
            align-items: center;
            gap: 12px;
        }
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
        .brand-name {
            color: var(--title);
            font-size: 23px;
            font-weight: 720;
            letter-spacing: -0.7px;
            white-space: nowrap;
        }
        .brand-divider { width: 1px; height: 20px; margin: 0 8px 0 26px; background: var(--line); }
        .brand-subtitle { color: #71819A; font-size: 14px; white-space: nowrap; }
        .header-user {
            margin-left: auto;
            margin-right: 92px;
            color: var(--body);
            font-size: 14px;
            font-weight: 600;
            white-space: nowrap;
        }
        .st-key-header_logout {
            position: fixed !important;
            top: 22px;
            right: max(24px, calc((100vw - 1280px) / 2));
            z-index: 1000;
            width: auto !important;
        }
        .st-key-header_logout button {
            min-height: 36px !important;
            padding: 4px 14px !important;
            border-radius: 10px !important;
            border: 1px solid var(--line) !important;
            background: #fff !important;
            color: var(--body) !important;
            font-size: 14px !important;
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
        .auth-brand { display: flex; align-items: center; gap: 10px; margin-bottom: 6px; }
        .auth-brand strong { font-size: 22px; color: var(--title); }
        .auth-sub { color: var(--muted); font-size: 14px; margin: 0 0 18px 0; }
        .auth-foot { margin: 14px 0 0 0; }
        .company-code-label { color: var(--muted); font-size: 13px; margin-top: 8px; }
        .company-code-big {
            font-size: 44px; font-weight: 800; letter-spacing: 0.18em;
            color: var(--blue); margin: 4px 0 8px 0;
            font-family: "SFMono-Regular", Menlo, Consolas, monospace;
        }
        .st-key-admin_root {
            width: min(1280px, calc(100% - 48px));
            margin: 24px auto 48px auto !important;
        }
        .admin-header {
            display: flex; align-items: center; gap: 12px;
            height: 64px; padding: 0 4px; margin-bottom: 12px;
            border-bottom: 1px solid var(--line);
        }
        .admin-header .brand-name { font-size: 20px; font-weight: 700; }
        .admin-header .header-user { margin-left: auto; }
        .st-key-admin_root [data-testid="stMarkdownContainer"] h4 { margin-top: 18px; }
        .dashboard-main {
            width: min(1280px, calc(100% - 48px));
            margin: 0 auto;
            padding: 50px 0 36px;
        }
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
        .new-employee-badge {
            align-self: flex-start;
            margin: 1px 0 0 auto;
            padding: 7px 18px;
            border-radius: 999px;
            background: #EDF3FD;
            color: var(--blue);
            font-size: 13px;
            font-weight: 550;
            white-space: nowrap;
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
        .equipment-empty { padding: 22px 4px; color: var(--muted); font-size: 14px; }
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
            .dashboard-main, .header-inner { width: calc(100% - 40px); }
            .dashboard-grid { grid-template-columns: minmax(0, 1.15fr) minmax(0, 1fr); gap: 16px; }
            .dashboard-card { padding: 22px 20px 18px; }
        }
        @media (max-width: 680px) {
            .site-header { height: 68px; }
            .header-inner, .dashboard-main { width: calc(100% - 32px); }
            .header-logo { width: 32px; height: 32px; flex-basis: 32px; }
            .brand-name { font-size: 20px; }
            .brand-divider { margin-left: 7px; margin-right: 0; }
            .brand-subtitle { font-size: 12px; }
            .dashboard-main { padding-top: 30px; }
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
            .new-employee-badge { position: absolute; top: 14px; right: 20px; margin: 0; padding: 5px 10px; font-size: 11px; }
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


def _touch_activity() -> None:
    st.session_state.last_activity = time.monotonic()


def _session_expired() -> bool:
    if not st.session_state.get("auth"):
        return False
    last = st.session_state.get("last_activity")
    return last is not None and time.monotonic() - last > SESSION_TIMEOUT_SEC


def _logout(notice: str = "") -> None:
    """세션 전체(신원·대화·대기 중인 요청)를 지운다."""
    for key in list(st.session_state.keys()):
        del st.session_state[key]
    if notice:
        st.session_state.auth_notice = notice


def _on_logout_click() -> None:
    _logout()


@st.fragment(run_every=60)
def _idle_watch() -> None:
    """아무 조작이 없어도 30분이 지나면 화면을 로그인으로 되돌린다."""
    if _session_expired():
        st.rerun(scope="app")


def _brand_html() -> str:
    return (
        "<div class='auth-brand'>"
        "<span class='header-logo'><svg viewBox='0 0 32 32' fill='none' aria-hidden='true'>"
        "<path d='M8.3 8.2h15.4a2.5 2.5 0 0 1 2.5 2.5v8.1a2.5 2.5 0 0 1-2.5 2.5h-7.2l-5.2 3.5v-3.5h-3a2.5 2.5 0 0 1-2.5-2.5v-8.1a2.5 2.5 0 0 1 2.5-2.5Z' stroke='white' stroke-width='1.8' stroke-linejoin='round'/>"
        "<path d='M11 13h10M11 16.5h7' stroke='white' stroke-width='1.7' stroke-linecap='round'/>"
        "</svg></span><strong>오피스 지니</strong></div>"
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


def _login_as_registered_admin() -> None:
    registered = st.session_state.pop("registered_company", None) or {}
    st.session_state.login_prefill_code = registered.get("company_code", "")
    st.session_state.login_prefill_account = registered.get("account_id", "ADMIN")
    st.session_state.auth_view = "login"


def _render_register() -> None:
    settings = get_agent().signup_settings()
    needs_code = bool((settings.get("data") or {}).get("signup_code_required"))
    with st.container(key="auth_card"):
        st.markdown(
            _brand_html()
            + "<p class='auth-sub'>회사를 등록하면 회사 코드가 만들어집니다. "
            "관리자 화면에서 규정과 사원 데이터를 올리면 바로 사용할 수 있어요.</p>",
            unsafe_allow_html=True,
        )
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
            submitted = st.form_submit_button(
                "회사 등록", type="primary", use_container_width=True
            )
        st.button("로그인 화면으로", key="register_back", on_click=_go_to, args=("login",))
        if submitted:
            if not company_name.strip():
                st.warning("회사명을 입력해 주세요.")
                return
            if len(password) < 8:
                st.error("관리자 비밀번호는 8자 이상이어야 합니다.")
                return
            if password != again:
                st.error("관리자 비밀번호와 확인 값이 다릅니다.")
                return
            result = get_agent().register_company(company_name, password, signup_code)
            if not result.get("ok"):
                st.error(result.get("message") or "회사를 등록하지 못했습니다.")
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
    with st.container(key="auth_card"):
        st.markdown(
            _brand_html()
            + f"<p class='auth-sub'>{_escape(registered['company_name'])} 등록을 마쳤습니다.</p>"
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


def _render_login() -> None:
    if "login_prefill_code" in st.session_state:
        st.session_state.login_company_code = st.session_state.pop("login_prefill_code")
        st.session_state.login_account_id = st.session_state.pop(
            "login_prefill_account", ""
        )
    with st.container(key="auth_card"):
        st.markdown(
            _brand_html()
            + "<p class='auth-sub'>회사 코드와 사번(관리자는 ADMIN)으로 로그인하세요.</p>",
            unsafe_allow_html=True,
        )
        notice = st.session_state.pop("auth_notice", "")
        if notice:
            st.info(notice)
        with st.form("login_form", clear_on_submit=False, border=False):
            company_code = st.text_input(
                "회사 코드", key="login_company_code", placeholder="예: AB3K7Q"
            )
            account_id = st.text_input(
                "아이디", key="login_account_id", placeholder="사번 또는 ADMIN"
            )
            password = st.text_input("비밀번호", type="password", key="login_password")
            submitted = st.form_submit_button(
                "로그인", type="primary", use_container_width=True
            )
        st.markdown("<p class='auth-sub auth-foot'>처음 오셨나요?</p>", unsafe_allow_html=True)
        st.button(
            "회사 등록하기", key="go_register", on_click=_go_to, args=("register",)
        )
        if submitted:
            if not company_code.strip() or not account_id.strip() or not password:
                st.warning("회사 코드, 아이디, 비밀번호를 모두 입력해 주세요.")
                return
            result = get_agent().login(company_code, account_id, password)
            if not result.get("ok"):
                st.error(result.get("message") or "로그인하지 못했습니다.")
                return
            try:
                auth = _auth_from_login(result["data"])
            except Exception as exc:  # 로그인은 성공했으나 응답 처리에서 실패
                _log_post_login_failure("login_response", exc, None)
                st.error(POST_LOGIN_ERROR)
                return
            # 로그인 전 입력값은 남기지 않는다.
            for key in list(st.session_state.keys()):
                del st.session_state[key]
            st.session_state.auth = auth
            _touch_activity()
            st.rerun()


def _render_change_password(auth: dict) -> None:
    minimum = 8 if auth["role"] == "admin" else 4
    with st.container(key="auth_card"):
        st.markdown(
            _brand_html()
            + "<p class='auth-sub'>처음 로그인하셨거나 비밀번호가 초기화되었습니다. "
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
        st.button("로그아웃", key="pw_logout", on_click=_on_logout_click)
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
            st.rerun()


def _render_logout_button() -> None:
    with st.container(key="header_logout"):
        st.button("로그아웃", key="logout_button", on_click=_on_logout_click)


def _render_employee(agent: OfficeGenieAgent, auth: dict) -> None:
    company_id = auth["company_id"]
    employee_id = auth["employee_id"]

    dashboard_identity = (company_id, employee_id)
    if st.session_state.get("dashboard_identity") != dashboard_identity:
        st.session_state.dashboard_identity = dashboard_identity
        _new_chat_state(company_id, employee_id, False)

    fallback_employee = {
        "employee_id": employee_id,
        "name": auth.get("name"),
        "department": auth.get("department"),
        "position": auth.get("position"),
    }
    sidebar_result = agent.get_sidebar(company_id, employee_id)
    if sidebar_result.get("ok"):
        sidebar_data = sidebar_result.get("data", {})
        dashboard_employee = sidebar_data.get("employee", fallback_employee)
        tickets = sidebar_data.get("tickets", [])
        annual_leave = sidebar_data.get("annual_leave")
        remaining_leave = (
            annual_leave.get("remaining_days")
            if isinstance(annual_leave, dict)
            else None
        )
    elif sidebar_result.get("code") == "INVALID_SESSION":
        _logout(sidebar_result.get("message", ""))
        st.rerun()
    else:
        st.warning(
            sidebar_result.get("message") or "사내 시스템 연결이 일시적으로 원활하지 않습니다."
        )
        dashboard_employee = fallback_employee
        tickets = []
        remaining_leave = None

    _render_logout_button()
    with st.container(key="dashboard_root"):
        _render_dashboard(dashboard_employee, tickets, remaining_leave)

    with st.container(key="chat_fab"):
        st.button(
            "챗봇 닫기" if st.session_state.get("chat_panel_open") else "챗봇 열기",
            key="chat_fab_toggle",
            on_click=_toggle_chat_panel,
            args=(company_id, employee_id),
        )

    if st.session_state.get("chat_panel_open"):
        _render_chat_panel(company_id, employee_id)


# ---------------------------------------------------------------------------
# 관리자 화면. 모든 호출은 agent.admin_call로 가고, 서버가 관리자 여부를 다시 확인한다.
# ---------------------------------------------------------------------------


def _admin(agent: OfficeGenieAgent, auth: dict, name: str, **args) -> dict:
    result = agent.admin_call(name, auth["company_id"], auth["account_id"], **args)
    if result.get("code") == "PERMISSION_DENIED":
        _logout(result.get("message", ""))
        st.rerun()
    return result


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
    if not info["has_data"]:
        st.info("아직 회사 데이터가 없습니다. 데이터 관리에서 업로드하세요.")
    counts = info.get("counts") or {}
    metric_cols = st.columns(6)
    for col, (key, label) in zip(
        metric_cols,
        (
            ("policies", "규정"),
            ("employees", "사원"),
            ("locations", "장소"),
            ("handovers", "인수인계서"),
            ("equipment", "신청 가능 비품"),
            ("tickets", "신청 내역"),
        ),
    ):
        col.metric(label, counts.get(key, 0))

    applied = st.session_state.pop("admin_applied", None)
    if applied:
        st.success(
            f"반영했습니다 (데이터 버전 {applied['data_version']}). "
            f"챗봇 답변에는 최대 {applied.get('cache_ttl_seconds', ttl)}초 안에 반영됩니다."
        )
    _render_one_time_passwords("admin_initial_passwords", "새로 만든 사원 초기 비밀번호입니다.")

    st.markdown("#### 1. 양식 다운로드")
    st.caption("필드 설명과 예시 1건이 들어 있습니다. 예시를 지우고 회사 내용으로 채워 올리세요.")
    template_cols = st.columns(len(DATA_KINDS))
    for col, (kind, label) in zip(template_cols, DATA_KINDS):
        payload = _template_bytes(kind)
        if payload is not None:
            col.download_button(
                f"{label} 양식",
                data=payload,
                file_name=f"{kind}.json",
                mime="application/json",
                key=f"template_{kind}",
                on_click="ignore",
            )

    st.markdown("#### 2. 파일 올리기")
    st.caption(
        "policies.json · employees.json · locations.json · handovers.json · tickets.json 중 "
        "하나 또는 여러 개를 올리세요. 파일당 2MB 이하. 올린 종류만 바뀌고 나머지는 그대로입니다. "
        "비품·신청 규칙을 올려도 기존 신청 내역과 신청 번호는 바뀌지 않습니다."
    )
    uploaded = st.file_uploader(
        "회사 데이터 JSON",
        type=["json"],
        accept_multiple_files=True,
        key=f"admin_uploader_{st.session_state.get('admin_uploader_n', 0)}",
    )
    _collect_upload_files(uploaded)
    local_issues = st.session_state.get("admin_upload_local_issues") or []
    if local_issues:
        st.error("올릴 수 없는 파일이 있습니다.")
        _issues_table(local_issues)
    files = st.session_state.get("admin_upload_files") or []
    if files:
        st.caption("선택한 파일: " + ", ".join(f["name"] for f in files))
    st.button(
        "미리보기",
        key="admin_preview_button",
        disabled=not files,
        on_click=_admin_preview,
        args=(agent, auth),
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
    result = _admin(agent, auth, "ui_admin_list_employees")
    if not result.get("ok"):
        st.error(result.get("message") or "사원 목록을 불러오지 못했습니다.")
        return
    people = result["data"]["employees"]
    if not people:
        st.info("등록된 사원이 없습니다. 데이터 관리에서 사원(employees.json)을 올려 주세요.")
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
    detail = _admin(agent, auth, "ui_admin_employee_detail", employee_id=employee_id)
    if not detail.get("ok"):
        st.error(detail.get("message") or "사원 정보를 불러오지 못했습니다.")
        return
    data = detail["data"]
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
    result = _admin(agent, auth, "ui_admin_list_tickets")
    if not result.get("ok"):
        st.error(result.get("message") or "신청 내역을 불러오지 못했습니다.")
        return
    tickets = result["data"]["tickets"]
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
    result = _admin(agent, auth, "ui_admin_list_accounts")
    if not result.get("ok"):
        st.error(result.get("message") or "계정 목록을 불러오지 못했습니다.")
        return
    accounts = result["data"]["accounts"]
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


def _render_admin(agent: OfficeGenieAgent, auth: dict) -> None:
    _render_logout_button()
    with st.container(key="admin_root"):
        st.markdown(
            "<div class='admin-header'>"
            "<span class='brand-name'>오피스 지니</span>"
            "<span class='brand-subtitle'>회사 관리</span>"
            f"<span class='header-user'>관리자 · {_escape(auth.get('company_name'))}</span>"
            "</div>",
            unsafe_allow_html=True,
        )
        overview = _admin(agent, auth, "ui_admin_overview")
        if not overview.get("ok"):
            st.error(overview.get("message") or "사내 시스템 연결이 일시적으로 원활하지 않습니다.")
            return
        info = overview["data"]
        code_cols = st.columns([2, 3])
        with code_cols[0]:
            st.markdown(
                "<div class='company-code-label'>회사 코드 · 사원들에게 알려주세요</div>"
                f"<div class='company-code-big'>{_escape(info['company_code'])}</div>",
                unsafe_allow_html=True,
            )
        with code_cols[1]:
            st.caption("복사")
            st.code(info["company_code"], language=None)
        data_tab, employees_tab, tickets_tab, accounts_tab = st.tabs(
            ["데이터 관리", "사원", "신청 관리", "계정"]
        )
        with data_tab:
            _admin_data_tab(agent, auth, info)
        with employees_tab:
            _admin_employees_tab(agent, auth)
        with tickets_tab:
            _admin_tickets_tab(agent, auth)
        with accounts_tab:
            _admin_accounts_tab(agent, auth)


def main() -> None:
    _render_styles()

    if _session_expired():
        _logout("30분 동안 사용하지 않아 로그아웃되었습니다. 다시 로그인해 주세요.")

    auth = st.session_state.get("auth")
    if not auth:
        view = st.session_state.get("auth_view", "login")
        if view == "register":
            _render_register()
        elif view == "registered":
            _render_registered()
        else:
            _render_login()
        return

    try:
        _touch_activity()
        _idle_watch()
        if auth.get("must_change"):
            _render_change_password(auth)
            return

        agent = get_agent()
        if auth.get("role") == "admin":
            _render_admin(agent, auth)
        else:
            _render_employee(agent, auth)
    except (RerunException, StopException):
        raise
    except Exception as exc:
        # 로그인 이후 화면 오류를 "로그인 실패"처럼 보이게 하지 않는다.
        _log_post_login_failure("render", exc, auth)
        _render_post_login_error()

main()
