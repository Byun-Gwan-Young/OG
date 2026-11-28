"""발표자료 정적 사이트 빌드.

presentation/src (deck.json, deck.css, slides/*.html, assets/) → presentation/site/index.html
GitHub Actions(.github/workflows/presentation.yml)가 이 스크립트를 실행해 GitHub Pages에 올린다.
발표자 노트(<aside>)는 공개 페이지에 넣지 않는다.

    python presentation/build.py
"""
from __future__ import annotations

import json
import re
import shutil
from pathlib import Path

ROOT = Path(__file__).resolve().parent
SRC = ROOT / "src"
OUT = ROOT / "site"

# Slides 원본의 업로드 이미지 주소 → 저장소 안 파일
BLOBS = {
    "9f9a81b3b84a605f64dd05eedf1c976a": "pc-home-top.png",
    "5207efc6b8cec90c066724ffd34760f6": "ai-card.png",
    "d145676b71d61a93df49a5452c7cdec0": "mobile-install.png",
    "2122e20c1258d5deb76bf1d4d8e5b9b9": "admin-preview.png",
    "48914022638192459c2de56f89cb7054": "chat-policy.png",
    "e2964f8b45dcfeeeb81f8670c7024ede": "chat-ticket-confirm.png",
    "5b7cfad2f4ff86ed2d613bd9fe1d6e8f": "chat-denied.png",
    "0466609fb6595aac0eece3bb8dd78201": "qr_github.png",
    "dc91b498ad76403e3f515b230eaa42e6": "qr_web.png",
    "6bc00f4745fcb93c55b79022a10372b2": "qr_mcp.png",
}

CHECK_SVG = (
    '<svg class="ico" viewBox="0 0 24 24" aria-hidden="true" width="28" height="28">'
    '<path d="M5 12.5l4.5 4.5L19 7.5" fill="none" stroke="currentColor" '
    'stroke-width="3" stroke-linecap="round" stroke-linejoin="round"/></svg>'
)

VIEWER_CSS = """
html, body { margin: 0; height: 100%; background: #0B1324; overflow: hidden; }
#stage { position: absolute; left: 50%; top: 50%; width: 1920px; height: 1080px; transform-origin: 0 0; }
#stage > section { position: absolute; inset: 0; width: 1920px; height: 1080px; box-sizing: border-box; overflow: hidden; display: none; }
#stage > section.on { display: flex; }
#stage > section * { box-sizing: border-box; }
#bar { position: fixed; left: 0; bottom: 0; height: 4px; background: #3461C1; transition: width .3s; z-index: 5; }
#count { position: fixed; right: 16px; bottom: 12px; font: 600 13px/1 'Noto Sans KR', sans-serif; color: rgba(255,255,255,.55); z-index: 5; }
#help { position: fixed; left: 16px; bottom: 12px; font: 13px/1 'Noto Sans KR', sans-serif; color: rgba(255,255,255,.45); z-index: 5; }
@media (prefers-reduced-motion: reduce) { #stage * { animation: none !important; } }
"""

VIEWER_JS = """
(function () {
  var slides = Array.prototype.slice.call(document.querySelectorAll('#stage > section'));
  var stage = document.getElementById('stage'), bar = document.getElementById('bar'), count = document.getElementById('count');
  var i = 0;
  function fit() {
    var s = Math.min(window.innerWidth / 1920, window.innerHeight / 1080);
    stage.style.transform = 'scale(' + s + ') translate(-50%, -50%)';
  }
  function show(n) {
    n = Math.max(0, Math.min(slides.length - 1, n));
    slides[i].classList.remove('on');
    void slides[n].offsetWidth;
    slides[n].classList.add('on');
    i = n;
    bar.style.width = ((i + 1) / slides.length * 100) + '%';
    count.textContent = (i + 1) + ' / ' + slides.length;
    if (location.hash !== '#' + (i + 1)) history.replaceState(null, '', '#' + (i + 1));
  }
  document.addEventListener('keydown', function (e) {
    if (['ArrowRight', 'PageDown', ' ', 'Enter'].indexOf(e.key) >= 0) { e.preventDefault(); show(i + 1); }
    else if (['ArrowLeft', 'PageUp', 'Backspace'].indexOf(e.key) >= 0) { e.preventDefault(); show(i - 1); }
    else if (e.key === 'Home') show(0);
    else if (e.key === 'End') show(slides.length - 1);
    else if (e.key === 'f' || e.key === 'F') {
      if (!document.fullscreenElement) document.documentElement.requestFullscreen && document.documentElement.requestFullscreen();
      else document.exitFullscreen && document.exitFullscreen();
    }
  });
  document.addEventListener('click', function (e) {
    if (e.target.closest('a')) return;
    show(e.clientX > window.innerWidth / 3 ? i + 1 : i - 1);
  });
  var x0 = null;
  document.addEventListener('touchstart', function (e) { x0 = e.touches[0].clientX; }, { passive: true });
  document.addEventListener('touchend', function (e) {
    if (x0 === null) return;
    var dx = e.changedTouches[0].clientX - x0;
    if (Math.abs(dx) > 40) show(dx < 0 ? i + 1 : i - 1);
    x0 = null;
  });
  window.addEventListener('resize', fit);
  fit();
  var start = parseInt((location.hash || '#1').slice(1), 10);
  slides[0].classList.add('on');
  show(isNaN(start) ? 0 : start - 1);
})();
"""


def slide_html(slide_id: str) -> str:
    html = (SRC / "slides" / f"{slide_id}.html").read_text(encoding="utf-8")
    html = re.sub(r"<aside>.*?</aside>", "", html, flags=re.S)
    html = re.sub(r'<x-icon name="Check"></x-icon>', CHECK_SVG, html)
    html = re.sub(r"\sdata-transition=\"[^\"]*\"", "", html)

    def blob(m: re.Match) -> str:
        name = BLOBS.get(m.group(1))
        if not name:
            raise SystemExit(f"알 수 없는 이미지: {m.group(0)} ({slide_id})")
        return f"assets/{name}"

    return re.sub(r"/_blob/([0-9a-f]+)", blob, html)


def build() -> Path:
    deck = json.loads((SRC / "deck.json").read_text(encoding="utf-8"))
    css = (SRC / "deck.css").read_text(encoding="utf-8")
    fonts = "".join(
        f'<link rel="stylesheet" href="{f["href"]}">\n' for f in deck.get("faces", {}).values() if f.get("href")
    )
    slides = "\n".join(slide_html(s) for s in deck["order"])
    page = f"""<!DOCTYPE html>
<html lang="ko">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{deck["title"]}</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
{fonts}<style>
{css}
.ico {{ vertical-align: -4px; }}
{VIEWER_CSS}
</style>
</head>
<body>
<div id="stage">
{slides}
</div>
<div id="bar"></div>
<div id="count"></div>
<div id="help">← → 넘기기 · F 전체 화면</div>
<script>{VIEWER_JS}</script>
</body>
</html>
"""
    if OUT.exists():
        shutil.rmtree(OUT)
    (OUT / "assets").mkdir(parents=True)
    for name in BLOBS.values():
        shutil.copy2(SRC / "assets" / name, OUT / "assets" / name)
    (OUT / "index.html").write_text(page, encoding="utf-8")
    (OUT / ".nojekyll").write_text("", encoding="utf-8")
    print(f"built {OUT / 'index.html'} ({len(deck['order'])} slides)")
    return OUT


if __name__ == "__main__":
    build()
