"""Playwright 브라우저 테스트 공통: Chromium 실행 파일 찾기.

pip로 설치한 playwright가 기대하는 브라우저가 없으면, 미리 설치된 Chromium
(PLAYWRIGHT_CHROMIUM 환경 변수 또는 /opt/pw-browsers)을 쓴다. 둘 다 없으면 건너뛴다.
"""
from __future__ import annotations

import glob
import os

import pytest


def launch_chromium(pw):
    try:
        return pw.chromium.launch()
    except Exception as first:
        candidates = [os.environ.get("PLAYWRIGHT_CHROMIUM", "")]
        candidates += sorted(glob.glob("/opt/pw-browsers/chromium-*/chrome-linux64/chrome"), reverse=True)
        for path in candidates:
            if path and os.path.exists(path):
                return pw.chromium.launch(executable_path=path)
        pytest.skip(f"Chromium을 띄울 수 없습니다: {type(first).__name__}")
