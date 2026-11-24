from __future__ import annotations

import html
from concurrent.futures import Future, ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import streamlit as st
from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(PROJECT_ROOT / ".env")

from agent import OfficeGenieAgent


st.set_page_config(page_title="오피스 지니 | 신입사원 온보딩", layout="wide")

WELCOME_TEXT = "안녕하세요. 온보딩이나 비품 지급에 대해 궁금한 점을 물어보세요."
SUGGESTED_QUESTION = "제 모니터는 언제 받을 수 있나요?"
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
    is_open = bool(st.session_state.get("chat_panel_open", False))
    _new_chat_state(company_id, employee_id, not is_open)


def _start_send(text: str, *, append_user: bool = True) -> None:
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


def _render_dashboard(employee: dict, tickets: list[dict]) -> None:
    now = datetime.now(ZoneInfo("Asia/Seoul"))
    weekdays = ("월", "화", "수", "목", "금", "토", "일")
    date_label = f"{now.month}월 {now.day}일 {weekdays[now.weekday()]}요일"
    name = _escape(employee.get("name", "신입사원"))
    department = _escape(employee.get("department", ""))
    position = _escape(employee.get("position", ""))

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
            <div class="profile-meta"><span>{department}</span><i></i><span>{position}</span></div>
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
        }
        .st-key-chat_scroll [class*="st-key-chat_bubble_user_"] {
            width: fit-content !important;
            max-width: 86% !important;
            box-sizing: border-box !important;
            padding: 13px 15px !important;
            border: 0 !important;
            border-radius: 14px 14px 4px 14px !important;
            background: var(--blue) !important;
            color: #fff !important;
            font-size: 14px !important;
            line-height: 1.55 !important;
            overflow-wrap: anywhere;
        }
        .st-key-chat_scroll [class*="st-key-chat_bubble_assistant_"] p,
        .st-key-chat_scroll [class*="st-key-chat_bubble_user_"] p { margin: 0 0 8px; line-height: 1.6; }
        .st-key-chat_scroll [class*="st-key-chat_bubble_assistant_"] p:last-child,
        .st-key-chat_scroll [class*="st-key-chat_bubble_user_"] p:last-child { margin-bottom: 0; }
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
            .new-employee-badge { position: absolute; right: 26px; margin: 0; padding: 5px 10px; font-size: 11px; }
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


def main() -> None:
    _render_styles()

    agent = get_agent()
    companies_result = agent.list_companies()
    if not companies_result.get("ok"):
        st.error(companies_result.get("message", "회사 정보를 불러오지 못했습니다."))
        st.stop()
    companies = companies_result.get("data", {}).get("companies", [])
    if not companies:
        st.error("등록된 회사 정보가 없습니다.")
        st.stop()

    company_id = companies[0]["company_id"]
    employees_result = agent.list_employees(company_id)
    if not employees_result.get("ok"):
        st.error(employees_result.get("message", "사원 정보를 불러오지 못했습니다."))
        st.stop()
    employees = employees_result.get("data", {}).get("employees", [])
    if not employees:
        st.error("등록된 사원 정보가 없습니다.")
        st.stop()
    employee = next(
        (row for row in employees if row.get("employee_id") == "EMP001"), employees[0]
    )
    employee_id = employee["employee_id"]

    dashboard_identity = (company_id, employee_id)
    if st.session_state.get("dashboard_identity") != dashboard_identity:
        st.session_state.dashboard_identity = dashboard_identity
        _new_chat_state(company_id, employee_id, False)

    sidebar_result = agent.get_sidebar(company_id, employee_id)
    if sidebar_result.get("ok"):
        sidebar_data = sidebar_result.get("data", {})
        dashboard_employee = sidebar_data.get("employee", employee)
        tickets = sidebar_data.get("tickets", [])
    else:
        dashboard_employee = employee
        tickets = []

    with st.container(key="dashboard_root"):
        _render_dashboard(dashboard_employee, tickets)

    with st.container(key="chat_fab"):
        st.button(
            "챗봇 닫기" if st.session_state.get("chat_panel_open") else "챗봇 열기",
            key="chat_fab_toggle",
            on_click=_toggle_chat_panel,
            args=(company_id, employee_id),
        )

    if st.session_state.get("chat_panel_open"):
        _render_chat_panel(company_id, employee_id)


main()
