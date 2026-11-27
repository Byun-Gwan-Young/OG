"""Streamlit 정적 폴더에 PWA 설정을 넣는다(멱등).

Streamlit은 페이지 <head>를 앱 코드에서 바꿀 수 없어서, 설치된 streamlit 패키지의 static 폴더에
직접 파일을 복사하고 index.html <head>에 PWA 태그를 넣는다.

    python scripts/pwa_setup.py            # 적용(여러 번 실행해도 한 번만 들어간다)
    python scripts/pwa_setup.py --check    # 적용 여부만 확인(적용됨 0, 아니면 1)
    python scripts/pwa_setup.py --undo     # 되돌리기

- 복사: pwa/sw.js → static/sw.js(범위 "/"), pwa/manifest.webmanifest·offline.html·install.js·아이콘 → static/pwa/
  sw.js는 복사할 때 __OFFICEGENIE_BUILD__를 빌드 버전(PWA 파일 + streamlit 버전 해시)으로 바꾼다 → 캐시 이름이 바뀌어
  새 버전이 설치되면 옛 캐시가 지워진다.
- index.html: manifest 링크, theme-color, apple-mobile-web-app-* 메타, apple-touch-icon,
  서비스 워커 등록 코드, 앱 설치 안내 스크립트(pwa/install.js), 아이콘 폰트 미리 불러오기(preload),
  viewport-fit=cover(아이폰 하단 안전 영역). 모두 표시 주석 사이에 넣는다.
- streamlit 버전을 바꾸면(패키지를 다시 설치하면) static 폴더가 새로 깔리므로 다시 실행한다.
- 서비스 워커는 HTTPS 또는 localhost에서만 동작한다.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PWA_DIR = ROOT / "pwa"
START = "<!-- officegenie-pwa:start -->"
END = "<!-- officegenie-pwa:end -->"
VIEWPORT_MARK = "<!-- officegenie-pwa:viewport-fit-added -->"
VIEWPORT_FIT = ", viewport-fit=cover"
SUB_FILES = (
    "manifest.webmanifest",
    "offline.html",
    "icon-192.png",
    "icon-512.png",
    "icon-maskable-512.png",
    "apple-touch-icon.png",
    "install.js",
)
ROOT_FILES = ("sw.js",)
# 블록은 줄 단위로 넣고 줄 단위로 지운다 → 몇 번을 실행해도, 되돌려도 원래 글자 그대로.
_BLOCK_RE = re.compile(r"^[ \t]*" + re.escape(START) + r".*?" + re.escape(END) + r"[ \t]*\n", re.S | re.M)
_HEAD_END_RE = re.compile(r"^([ \t]*)</head>", re.M)
_VIEWPORT_RE = re.compile(r"(<meta\s+name=\"viewport\"\s+content=\")([^\"]*)(\")", re.S)


def streamlit_static_dir() -> Path:
    import streamlit

    return Path(streamlit.__file__).resolve().parent / "static"


def _version(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()[:10]


def _theme_color() -> str:
    manifest = json.loads((PWA_DIR / "manifest.webmanifest").read_text("utf-8"))
    return manifest["theme_color"]


BUILD_PLACEHOLDER = "__OFFICEGENIE_BUILD__"


def build_version() -> str:
    """PWA 원본 파일 전부와 설치된 streamlit 버전으로 만든 빌드 버전(서비스 워커 캐시 이름에 쓴다)."""
    import importlib.metadata

    digest = hashlib.sha256()
    for name in sorted((*ROOT_FILES, *SUB_FILES)):
        digest.update(name.encode())
        digest.update((PWA_DIR / name).read_bytes())
    try:
        digest.update(importlib.metadata.version("streamlit").encode())
    except importlib.metadata.PackageNotFoundError:
        pass
    return digest.hexdigest()[:12]


def rendered(name: str) -> bytes:
    """정적 폴더에 놓일 내용. sw.js만 빌드 버전을 채운다."""
    data = (PWA_DIR / name).read_bytes()
    if name == "sw.js":
        data = data.replace(BUILD_PLACEHOLDER.encode(), build_version().encode())
    return data


def icon_font(static_dir: Path | None) -> str | None:
    """Streamlit 아이콘 폰트(Material Symbols) 파일 이름. 없으면 None."""
    if static_dir is None:
        return None
    found = sorted((static_dir / "static" / "media").glob("MaterialSymbols-Rounded*.woff2"))
    return found[0].name if found else None


def head_block(static_dir: Path | None = None, *, viewport_added: bool = False) -> str:
    """index.html <head>에 넣을 태그. 경로는 상대 경로(./)라 baseUrlPath에서도 동작한다."""
    manifest_v = _version(PWA_DIR / "manifest.webmanifest")
    icon_v = _version(PWA_DIR / "apple-touch-icon.png")
    install_v = _version(PWA_DIR / "install.js")
    font = icon_font(static_dir)
    lines = [
        START,
        f'<link rel="manifest" href="./pwa/manifest.webmanifest?v={manifest_v}" />',
        f'<meta name="theme-color" content="{_theme_color()}" />',
        '<meta name="mobile-web-app-capable" content="yes" />',
        '<meta name="apple-mobile-web-app-capable" content="yes" />',
        '<meta name="apple-mobile-web-app-title" content="오피스지니" />',
        '<meta name="apple-mobile-web-app-status-bar-style" content="default" />',
        f'<link rel="apple-touch-icon" href="./pwa/apple-touch-icon.png?v={icon_v}" />',
    ]
    if font:
        # 아이콘 폰트를 화면 코드보다 먼저 받기 시작해 아이콘 이름 글자가 보이는 시간을 없앤다.
        lines.append(f'<link rel="preload" href="./static/media/{font}" as="font" type="font/woff2" crossorigin />')
    lines += [
        "<script>",
        '  if ("serviceWorker" in navigator && window.isSecureContext) {',
        '    window.addEventListener("load", function () {',
        "      navigator.serviceWorker",
        '        .register("./sw.js", { scope: "./", updateViaCache: "none" })',
        "        .catch(function () {});",
        "    });",
        "  }",
        "</script>",
        f'<script src="./pwa/install.js?v={install_v}" defer></script>',
    ]
    if viewport_added:
        lines.append(VIEWPORT_MARK)
    lines.append(END)
    return "".join(f"    {line}\n" for line in lines)


def _strip(html: str) -> tuple[str, bool]:
    """블록을 지운다. (지운 결과, 이 스크립트가 viewport-fit을 넣었었는지)."""
    match = _BLOCK_RE.search(html)
    added = bool(match and VIEWPORT_MARK in match.group(0))
    return _BLOCK_RE.sub("", html), added


def patched_index(html: str, static_dir: Path | None = None) -> str:
    """표시 주석 사이 블록을 새로 넣는다(있으면 교체). 몇 번을 실행해도 결과가 같다."""
    html, added = _strip(html)
    viewport = _VIEWPORT_RE.search(html)
    if viewport and "viewport-fit" not in viewport.group(2):
        html = _VIEWPORT_RE.sub(lambda m: m.group(1) + m.group(2) + VIEWPORT_FIT + m.group(3), html, count=1)
        added = True
    if not _HEAD_END_RE.search(html):
        raise ValueError("index.html에서 </head>를 찾지 못했습니다.")
    block = head_block(static_dir, viewport_added=added)
    return _HEAD_END_RE.sub(lambda m: block + m.group(0), html, count=1)


def restored_index(html: str) -> str:
    html, added = _strip(html)
    if added:
        html = _VIEWPORT_RE.sub(
            lambda m: m.group(1) + m.group(2).replace(VIEWPORT_FIT, "") + m.group(3), html, count=1
        )
    return html


def _targets(static_dir: Path) -> list[tuple[Path, Path]]:
    pairs = [(PWA_DIR / name, static_dir / name) for name in ROOT_FILES]
    pairs += [(PWA_DIR / name, static_dir / "pwa" / name) for name in SUB_FILES]
    return pairs


def check(static_dir: Path) -> list[str]:
    """적용되지 않은 항목 목록. 비어 있으면 적용 완료."""
    problems = []
    index = static_dir / "index.html"
    if not index.is_file():
        return [f"{index}가 없습니다."]
    html = index.read_text("utf-8")
    if html.count(START) != 1 or html.count(END) != 1:
        problems.append("index.html에 PWA 태그가 없습니다.")
    elif patched_index(html, static_dir) != html:
        problems.append("index.html의 PWA 태그가 최신이 아닙니다.")
    if "viewport-fit=cover" not in html:
        problems.append("viewport-fit=cover가 없습니다.")
    for source, target in _targets(static_dir):
        if not target.is_file():
            problems.append(f"{target.relative_to(static_dir)} 파일이 없습니다.")
        elif target.read_bytes() != rendered(source.name):
            problems.append(f"{target.relative_to(static_dir)} 파일이 최신이 아닙니다.")
    return problems


def apply(static_dir: Path) -> None:
    for source, target in _targets(static_dir):
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(rendered(source.name))
    index = static_dir / "index.html"
    html = index.read_text("utf-8")
    new_html = patched_index(html, static_dir)
    if new_html != html:
        index.write_text(new_html, "utf-8")


def undo(static_dir: Path) -> None:
    index = static_dir / "index.html"
    if index.is_file():
        html = index.read_text("utf-8")
        restored = restored_index(html)
        if restored != html:
            index.write_text(restored, "utf-8")
    for _, target in _targets(static_dir):
        if target.is_file():
            target.unlink()
    pwa_dir = static_dir / "pwa"
    if pwa_dir.is_dir() and not any(pwa_dir.iterdir()):
        pwa_dir.rmdir()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--check", action="store_true", help="적용 여부만 확인(적용됨 0, 아니면 1)")
    mode.add_argument("--undo", action="store_true", help="넣은 태그와 파일을 되돌린다")
    parser.add_argument("--static-dir", type=Path, help="대상 static 폴더(기본: 설치된 streamlit)")
    args = parser.parse_args(argv)
    static_dir = args.static_dir or streamlit_static_dir()

    try:
        if args.check:
            problems = check(static_dir)
            if problems:
                print("PWA 미적용: python scripts/pwa_setup.py 를 실행하세요.")
                for problem in problems:
                    print(f"  - {problem}")
                return 1
            print(f"PWA 적용됨: {static_dir}")
            return 0
        if args.undo:
            undo(static_dir)
            print(f"PWA 설정을 되돌렸습니다: {static_dir}")
            return 0
        apply(static_dir)
    except PermissionError:
        print(f"{static_dir}에 쓸 권한이 없습니다. 가상환경을 쓰거나 관리자 권한으로 실행하세요.", file=sys.stderr)
        return 2
    except (OSError, ValueError) as exc:
        print(f"PWA 설정 실패: {exc}", file=sys.stderr)
        return 2
    problems = check(static_dir)
    if problems:
        print("PWA 설정 후 확인 실패:", *problems, sep="\n  - ", file=sys.stderr)
        return 2
    print(f"PWA 설정을 적용했습니다: {static_dir}")
    print("서비스 워커는 HTTPS 또는 localhost에서만 동작합니다. streamlit 버전을 바꾸면 다시 실행하세요.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
