#!/usr/bin/env python3
"""docs/og.png: the 1200x630 picture shown when the page link is shared (Messenger, WhatsApp, LinkedIn, Slack).
Needs Pillow. Run: python3 tools/make_og.py"""
from pathlib import Path
from PIL import Image, ImageDraw, ImageFilter, ImageFont

R = Path(__file__).resolve().parent.parent
W, H = 1200, 630
img = Image.new("RGBA", (W, H))
d = ImageDraw.Draw(img)
for y in range(H):  # navy, a little lighter at the top
    t = y / H
    d.line([(0, y), (W, y)], fill=(round(32 - 14 * t), round(44 - 18 * t), round(64 - 26 * t), 255))
glow = Image.new("RGBA", (W, H), (0, 0, 0, 0))
ImageDraw.Draw(glow).ellipse([60, 80, 560, 580], fill=(132, 206, 24, 70))
img.alpha_composite(glow.filter(ImageFilter.GaussianBlur(90)))
icon = Image.open(R / "assets" / "icon-1024.png").convert("RGBA").resize((430, 430), Image.LANCZOS)
img.alpha_composite(icon, (70, 100))
bold = ImageFont.truetype(str(R / "assets" / "Poppins-Bold.ttf"), 76)
reg = ImageFont.truetype(str(R / "assets" / "Poppins-Regular.ttf"), 40)
small = ImageFont.truetype(str(R / "assets" / "Poppins-Regular.ttf"), 27)
x = 545
d.text((x, 168), "RM Activity", font=bold, fill=(248, 250, 252))
d.text((x, 270), "See where your day", font=reg, fill=(194, 203, 217))
d.text((x, 322), "actually", font=reg, fill=(132, 206, 24))
d.text((x + d.textlength("actually ", font=reg), 322), "goes.", font=reg, fill=(194, 203, 217))
line = "Free Mac app  ·  Nothing leaves your Mac"
pill = Image.new("RGBA", (W, H), (0, 0, 0, 0))  # its own layer, so the faint outline blends
ImageDraw.Draw(pill).rounded_rectangle([x, 410, x + d.textlength(line, font=small) + 56, 464], radius=27,
                                       outline=(248, 250, 252, 70), width=2)
img.alpha_composite(pill)
d = ImageDraw.Draw(img)
d.text((x + 28, 419), line, font=small, fill=(194, 203, 217))
img.convert("RGB").save(R / "docs" / "og.png", optimize=True)
print("wrote docs/og.png")
