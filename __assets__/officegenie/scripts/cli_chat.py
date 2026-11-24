"""터미널에서 Gemini 연동을 바로 확인하는 테스트용 채팅 (UI 없이).

실행: GEMINI_API_KEY=... python scripts/cli_chat.py [company_id] [employee_id]
예시: python scripts/cli_chat.py bridgesoft EMP001
명령: /yes 접수 확정, /no 취소, /side 사이드바 보기, /quit 종료
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from agent import OfficeGenieAgent  # noqa: E402


def main():
    company = sys.argv[1] if len(sys.argv) > 1 else "bridgesoft"
    employee = sys.argv[2] if len(sys.argv) > 2 else "EMP001"
    agent = OfficeGenieAgent()
    chat = agent.new_session(company, employee)
    print(f"[오피스지니] {company} / {employee} — /quit 로 종료")
    try:
        while True:
            text = input("\n나> ").strip()
            if text == "/quit":
                break
            if text == "/side":
                print(agent.get_sidebar(company, employee))
                continue
            r = chat.confirm_ticket() if text == "/yes" else chat.cancel_ticket() if text == "/no" else chat.send(text)
            print(f"\n지니> {r['reply']}")
            print(f"  · code={r['code']} tools={[c['name'] + ':' + str(c['code']) for c in r['tool_calls']]}")
            if r.get("debug"):
                print(f"  · [오류 원인] {r['debug']}")
            if r.get("pending_ticket"):
                print("  · 확인 대기 중: /yes 접수 확정, /no 취소")
    finally:
        agent.close()


if __name__ == "__main__":
    main()
