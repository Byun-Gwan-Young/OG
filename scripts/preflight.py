"""배포 시작 전 점검. 웹 서비스 시작 명령 앞에서 실행한다.

    python scripts/preflight.py && streamlit run app.py ...

- OFFICEGENIE_ENV=production인데 DATABASE_URL이 없거나 SQLite면 종료 코드 2로 멈춘다
  (컨테이너는 다시 만들 때 안의 파일이 사라지므로 SQLite로 넘어가면 안 된다).
- PWA(pwa_setup.py)가 적용되지 않았으면 경고만 남기고 계속한다.
"""
from __future__ import annotations

import logging
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import storage  # noqa: E402

log = logging.getLogger("officegenie.preflight")


def main() -> int:
    logging.basicConfig(level=logging.INFO, stream=sys.stderr,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    try:
        storage.check_runtime_config()
    except (storage.ConfigurationError, ValueError) as exc:
        log.critical("%s", exc)
        return 2
    log.info("설정 확인 완료 (운영 모드: %s)", "예" if storage.is_production() else "아니오")
    check_pwa()
    return 0


def check_pwa() -> list[str]:
    """배포 빌드에서 pwa_setup.py가 적용됐는지 확인한다. 미적용이면 경고만 남긴다."""
    from scripts import pwa_setup

    try:
        problems = pwa_setup.check(pwa_setup.streamlit_static_dir())
    except Exception as exc:  # 점검 실패로 서비스 시작을 막지 않는다
        log.warning("PWA 적용 여부를 확인하지 못했습니다 (%s)", type(exc).__name__)
        return ["check_failed"]
    if problems:
        log.warning("PWA 미적용: 빌드 명령에 'python scripts/pwa_setup.py'가 있는지 확인하세요. %s",
                    "; ".join(problems))
    else:
        log.info("PWA 적용 확인")
    return problems


if __name__ == "__main__":
    raise SystemExit(main())
