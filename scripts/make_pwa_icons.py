"""PWA 아이콘 만들기: pwa/ 폴더에 192·512·maskable 512·apple-touch-icon(180) PNG를 만든다.

저장소에 로고 이미지(assets/logo.png 또는 assets/office-genie-logo.png)가 있으면 그것을 가운데에 두고,
없으면 브랜드 색(디자인 토큰 --blue) 배경에 "OG" 글자를 넣는다. 외부 리소스는 쓰지 않는다.

실행: python scripts/make_pwa_icons.py
"""
from __future__ import annotations

import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parents[1]
PWA_DIR = ROOT / "pwa"
BRAND = "#3868BE"  # app.py :root --blue
TEXT = "#FFFFFF"
LOGO_CANDIDATES = (ROOT / "assets" / "logo.png", ROOT / "assets" / "office-genie-logo.png")
FONT_CANDIDATES = (
    "/usr/share/fonts/opentype/inter/Inter-Bold.otf",
    "/usr/share/fonts/opentype/inter/InterDisplay-Bold.otf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/Library/Fonts/Arial Bold.ttf",
    "C:/Windows/Fonts/arialbd.ttf",
)
# (파일 이름, 크기, 내용이 차지할 비율, 모서리 둥글기 비율)
ICONS = (
    ("icon-192.png", 192, 0.50, 0.22),
    ("icon-512.png", 512, 0.50, 0.22),
    # maskable: 런처가 원·둥근 사각형으로 잘라도 글자가 안전 영역(가운데 80%) 안에 있게 꽉 채운 배경 + 작은 글자
    ("icon-maskable-512.png", 512, 0.38, 0.0),
    # iOS는 모서리를 직접 둥글게 자르므로 사각형으로 둔다
    ("apple-touch-icon.png", 180, 0.50, 0.0),
)


def _font(size: int) -> ImageFont.FreeTypeFont:
    for path in FONT_CANDIDATES:
        if Path(path).is_file():
            return ImageFont.truetype(path, size)
    return ImageFont.load_default(size=size)


def _logo() -> Image.Image | None:
    for path in LOGO_CANDIDATES:
        if path.is_file():
            return Image.open(path).convert("RGBA")
    return None


def make_icon(size: int, content_ratio: float, radius_ratio: float, logo: Image.Image | None) -> Image.Image:
    scale = 4  # 크게 그린 뒤 줄여서 가장자리를 매끄럽게
    big = size * scale
    image = Image.new("RGBA", (big, big), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    radius = int(big * radius_ratio)
    if radius:
        draw.rounded_rectangle((0, 0, big - 1, big - 1), radius=radius, fill=BRAND)
    else:
        draw.rectangle((0, 0, big, big), fill=BRAND)
    target = int(big * content_ratio)
    if logo is not None:
        mark = logo.copy()
        mark.thumbnail((target, target), Image.LANCZOS)
        image.alpha_composite(mark, ((big - mark.width) // 2, (big - mark.height) // 2))
    else:
        font = _font(target)
        left, top, right, bottom = draw.textbbox((0, 0), "OG", font=font)
        width, height = right - left, bottom - top
        if width > target:  # 글자 폭이 정해진 영역을 넘으면 줄인다
            font = _font(int(target * target / width))
            left, top, right, bottom = draw.textbbox((0, 0), "OG", font=font)
            width, height = right - left, bottom - top
        draw.text(((big - width) / 2 - left, (big - height) / 2 - top), "OG", font=font, fill=TEXT)
    return image.resize((size, size), Image.LANCZOS)


def main(argv: list[str] | None = None) -> int:
    out = Path(argv[0]) if argv else PWA_DIR
    out.mkdir(parents=True, exist_ok=True)
    logo = _logo()
    for name, size, content, radius in ICONS:
        icon = make_icon(size, content, radius, logo)
        if name.startswith("apple") or "maskable" in name:
            icon = icon.convert("RGB")  # 투명 영역이 없어야 하는 아이콘
        icon.save(out / name, optimize=True)
        print(f"{out / name} ({size}x{size})")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
