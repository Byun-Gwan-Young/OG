"""PWA 설정 테스트: pwa_setup.py 멱등·--check·--undo, manifest·아이콘·서비스 워커 내용.

설치된 streamlit 패키지는 건드리지 않는다. index.html 복사본으로 임시 static 폴더를 만들어 확인한다.
"""
from __future__ import annotations

import importlib.metadata
import json
import re
import shutil
from pathlib import Path

import pytest
from PIL import Image

from scripts import pwa_setup

ROOT = Path(__file__).resolve().parents[1]
PWA = ROOT / "pwa"


@pytest.fixture
def static_dir(tmp_path) -> Path:
    target = tmp_path / "static"
    target.mkdir()
    index = pwa_setup.streamlit_static_dir() / "index.html"
    original = index.read_text("utf-8")
    if pwa_setup.START in original:  # 이미 적용된 설치본이면 원본 상태로 되돌린 복사본을 쓴다
        original = pwa_setup.restored_index(original)
    (target / "index.html").write_text(original, "utf-8")
    return target


def test_apply_is_idempotent_and_check_reports_state(static_dir, capsys):
    original = (static_dir / "index.html").read_text("utf-8")
    assert pwa_setup.main(["--check", "--static-dir", str(static_dir)]) == 1
    assert "python scripts/pwa_setup.py" in capsys.readouterr().out

    assert pwa_setup.main(["--static-dir", str(static_dir)]) == 0
    once = (static_dir / "index.html").read_text("utf-8")
    assert pwa_setup.main(["--static-dir", str(static_dir)]) == 0
    assert (static_dir / "index.html").read_text("utf-8") == once  # 두 번 실행해도 한 번만
    assert once.count(pwa_setup.START) == 1 and once.count('rel="manifest"') == 1
    assert pwa_setup.main(["--check", "--static-dir", str(static_dir)]) == 0

    head = once.split("</head>")[0]
    assert 'rel="manifest" href="./pwa/manifest.webmanifest?v=' in head
    assert '<meta name="theme-color" content="#3868BE" />' in head
    assert 'name="apple-mobile-web-app-capable" content="yes"' in head
    assert 'rel="apple-touch-icon" href="./pwa/apple-touch-icon.png?v=' in head
    assert 'register("./sw.js"' in head and "isSecureContext" in head
    assert "viewport-fit=cover" in head
    assert original != once


def test_undo_restores_original_exactly(static_dir):
    original = (static_dir / "index.html").read_text("utf-8")
    assert pwa_setup.main(["--static-dir", str(static_dir)]) == 0
    assert (static_dir / "sw.js").is_file() and (static_dir / "pwa" / "manifest.webmanifest").is_file()
    assert pwa_setup.main(["--undo", "--static-dir", str(static_dir)]) == 0
    assert (static_dir / "index.html").read_text("utf-8") == original
    assert not (static_dir / "sw.js").exists() and not (static_dir / "pwa").exists()
    assert pwa_setup.main(["--undo", "--static-dir", str(static_dir)]) == 0  # 두 번 되돌려도 안전


def test_check_detects_stale_files(static_dir):
    assert pwa_setup.main(["--static-dir", str(static_dir)]) == 0
    (static_dir / "sw.js").write_text("// old", "utf-8")
    assert "sw.js 파일이 최신이 아닙니다." in pwa_setup.check(static_dir)
    (static_dir / "sw.js").unlink()
    assert "sw.js 파일이 없습니다." in pwa_setup.check(static_dir)


def test_keeps_existing_viewport_fit(static_dir):
    index = static_dir / "index.html"
    index.write_text(index.read_text("utf-8").replace("shrink-to-fit=no", "shrink-to-fit=no, viewport-fit=cover"), "utf-8")
    before = index.read_text("utf-8")
    pwa_setup.main(["--static-dir", str(static_dir)])
    pwa_setup.main(["--undo", "--static-dir", str(static_dir)])
    assert index.read_text("utf-8") == before  # 원래 있던 값은 지우지 않는다


def test_missing_head_fails_cleanly(tmp_path):
    (tmp_path / "index.html").write_text("<html><body></body></html>", "utf-8")
    assert pwa_setup.main(["--static-dir", str(tmp_path)]) == 2


def _tokens() -> dict[str, str]:
    app = (ROOT / "app.py").read_text("utf-8")
    root = app[app.index(":root {"):app.index("}", app.index(":root {"))]
    return dict(re.findall(r"--([a-z-]+):\s*(#[0-9A-Fa-f]{6})", root))


def test_manifest_matches_spec_and_design_tokens():
    manifest = json.loads((PWA / "manifest.webmanifest").read_text("utf-8"))
    tokens = _tokens()
    assert manifest["name"] == manifest["short_name"] == "오피스지니"
    assert manifest["start_url"] == "/?source=pwa"
    assert manifest["display"] == "standalone"
    assert manifest["theme_color"] == tokens["blue"]
    assert manifest["background_color"] == tokens["page"]
    shortcuts = {s["name"]: s["url"] for s in manifest["shortcuts"]}
    assert set(shortcuts) == {"공지사항", "비품 신청", "자료실"}
    for name, page in (("공지사항", "notices"), ("비품 신청", "equipment"), ("자료실", "library")):
        assert f"page={page}" in shortcuts[name]
    purposes = {(i["sizes"], i["purpose"]) for i in manifest["icons"]}
    assert {("192x192", "any"), ("512x512", "any"), ("512x512", "maskable")} <= purposes


@pytest.mark.parametrize(
    "name,size",
    [("icon-192.png", 192), ("icon-512.png", 512), ("icon-maskable-512.png", 512), ("apple-touch-icon.png", 180)],
)
def test_icons_exist_with_right_size(name, size):
    with Image.open(PWA / name) as image:
        assert image.size == (size, size)
        if "maskable" in name or name.startswith("apple"):
            assert image.mode == "RGB"  # 투명 배경 없음


def test_service_worker_never_stores_personal_or_live_data():
    sw = (PWA / "sw.js").read_text("utf-8")
    assert "/_stcore/" in sw  # 실시간 연결·API는 건드리지 않는다
    assert "cache.put" not in sw and ".put(" not in sw  # 실행 중에 응답을 저장하지 않는다
    stored = re.findall(r'new URL\("(pwa/[^"]+)"', sw)  # 미리 저장하는 주소 전부
    assert sorted(stored) == sorted(["pwa/offline.html", "pwa/icon-192.png", "pwa/icon-512.png",
                                     "pwa/icon-maskable-512.png", "pwa/apple-touch-icon.png"])
    assert 'request.mode === "navigate"' in sw and "fetch(request).catch" in sw  # 네트워크 우선


def test_offline_page_is_self_contained():
    html = (PWA / "offline.html").read_text("utf-8")
    assert not re.search(r"(src|href)=\"https?://", html)
    assert "다시 시도" in html


def test_streamlit_is_pinned_to_installed_version():
    lines = (ROOT / "requirements.txt").read_text("utf-8").splitlines()
    pinned = next(line for line in lines if line.startswith("streamlit"))
    assert pinned == f"streamlit=={importlib.metadata.version('streamlit')}"


def test_icon_script_regenerates_same_sizes(tmp_path):
    from scripts import make_pwa_icons

    assert make_pwa_icons.main([str(tmp_path)]) == 0
    for name, size, *_ in make_pwa_icons.ICONS:
        with Image.open(tmp_path / name) as image:
            assert image.size == (size, size)
    shutil.rmtree(tmp_path)
