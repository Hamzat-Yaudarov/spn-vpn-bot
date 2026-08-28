#!/usr/bin/env python3
"""Собрать чистый статический Telegram custom emoji pack Way SPN.

Каждый значок рисуется отдельно на холсте 1024×1024, затем уменьшается до 100×100
с качественным сглаживанием. Выходные PNG имеют реальную прозрачность.
"""

from pathlib import Path

from PIL import Image, ImageDraw


SIZE = 1024
INK = (18, 20, 25, 255)
INK_2 = (31, 34, 40, 255)
GOLD = (237, 190, 91, 255)
GOLD_DARK = (142, 99, 37, 255)
IVORY = (255, 244, 205, 255)
TRANSPARENT = (0, 0, 0, 0)

ROOT = Path(__file__).resolve().parents[1]
MASTER_DIR = ROOT / "assets" / "telegram-emoji" / "v2-masters"
OUTPUT_DIR = ROOT / "assets" / "telegram-emoji" / "v2-png"


def canvas():
    image = Image.new("RGBA", (SIZE, SIZE), TRANSPARENT)
    return image, ImageDraw.Draw(image)


def box(x1, y1, x2, y2):
    return (x1, y1, x2, y2)


def rounded(draw, bounds, radius=80, fill=INK, outline=GOLD, width=48):
    draw.rounded_rectangle(bounds, radius=radius, fill=fill, outline=GOLD_DARK, width=width + 18)
    draw.rounded_rectangle(bounds, radius=radius, outline=outline, width=width)


def line(draw, points, fill=IVORY, width=54):
    draw.line(points, fill=GOLD_DARK, width=width + 18, joint="curve")
    draw.line(points, fill=fill, width=width, joint="curve")


def ellipse(draw, bounds, fill=INK, outline=GOLD, width=48):
    draw.ellipse(bounds, fill=fill, outline=GOLD_DARK, width=width + 18)
    draw.ellipse(bounds, outline=outline, width=width)


def arc(draw, bounds, start, end, fill=GOLD, width=48):
    draw.arc(bounds, start=start, end=end, fill=GOLD_DARK, width=width + 18)
    draw.arc(bounds, start=start, end=end, fill=fill, width=width)


def polygon(draw, points, fill=INK, outline=GOLD, width=48):
    draw.polygon(points, fill=fill)
    closed = list(points) + [points[0]]
    line(draw, closed, fill=outline, width=width)


def shield(draw):
    points = [(512, 74), (808, 182), (764, 628), (512, 894), (260, 628), (216, 182)]
    polygon(draw, points, fill=INK, outline=GOLD, width=50)
    line(draw, [(512, 114), (757, 204)], fill=IVORY, width=16)
    return points


def icon_home():
    image, draw = canvas()
    shield(draw)
    line(draw, [(330, 355), (408, 650), (512, 470), (616, 650), (694, 355)], width=70)
    line(draw, [(408, 650), (512, 732), (616, 650)], fill=GOLD, width=50)
    return image


def icon_buy():
    image, draw = canvas()
    crown = [(160, 325), (315, 435), (420, 212), (530, 432), (680, 235), (790, 520), (205, 520)]
    polygon(draw, crown, fill=INK, outline=GOLD, width=45)
    rounded(draw, box(205, 520, 790, 650), radius=34, width=40)
    ellipse(draw, box(575, 540, 805, 770), fill=INK_2, width=42)
    ellipse(draw, box(648, 613, 732, 697), fill=TRANSPARENT, width=34)
    line(draw, [(618, 728), (470, 876)], fill=GOLD, width=62)
    line(draw, [(504, 842), (552, 890)], fill=GOLD, width=42)
    return image


def icon_subscriptions():
    image, draw = canvas()
    ellipse(draw, box(130, 130, 810, 810), fill=INK, width=48)
    ellipse(draw, box(360, 250, 540, 430), fill=INK_2, width=38)
    arc(draw, box(270, 380, 630, 735), 190, 350, fill=IVORY, width=48)
    ellipse(draw, box(610, 570, 840, 800), fill=INK, width=42)
    ellipse(draw, box(685, 645, 765, 725), fill=TRANSPARENT, width=30)
    line(draw, [(650, 760), (525, 884)], fill=GOLD, width=58)
    return image


def icon_antijam():
    image, draw = canvas()
    shield(draw)
    line(draw, [(512, 390), (512, 720)], fill=IVORY, width=48)
    ellipse(draw, box(466, 326, 558, 418), fill=GOLD, outline=IVORY, width=20)
    arc(draw, box(350, 315, 674, 650), 115, 245, fill=GOLD, width=42)
    arc(draw, box(350, 315, 674, 650), 295, 65, fill=GOLD, width=42)
    arc(draw, box(270, 235, 754, 730), 120, 240, fill=IVORY, width=34)
    arc(draw, box(270, 235, 754, 730), 300, 60, fill=IVORY, width=34)
    return image


def icon_connect():
    image, draw = canvas()

    def capsule(angle, center):
        layer = Image.new("RGBA", (SIZE, SIZE), TRANSPARENT)
        ld = ImageDraw.Draw(layer)
        rounded(ld, box(290, 400, 734, 620), radius=110, fill=TRANSPARENT, width=66)
        layer = layer.rotate(angle, resample=Image.Resampling.BICUBIC, center=(512, 512))
        image.alpha_composite(layer, (center[0] - 512, center[1] - 512))

    capsule(-42, (420, 590))
    capsule(-42, (604, 408))
    draw = ImageDraw.Draw(image)
    line(draw, [(405, 618), (619, 404)], fill=IVORY, width=38)
    arc(draw, box(72, 230, 360, 630), 285, 65, fill=GOLD, width=30)
    arc(draw, box(664, 395, 952, 795), 105, 245, fill=GOLD, width=30)
    return image


def icon_support():
    image, draw = canvas()
    arc(draw, box(170, 150, 854, 830), 180, 360, fill=IVORY, width=62)
    rounded(draw, box(140, 435, 290, 720), radius=62, width=42)
    rounded(draw, box(734, 435, 884, 720), radius=62, width=42)
    arc(draw, box(555, 500, 850, 835), 5, 105, fill=GOLD, width=42)
    line(draw, [(656, 790), (780, 790)], fill=GOLD, width=42)
    ellipse(draw, box(600, 740, 690, 830), fill=INK, width=28)
    return image


def icon_more():
    image, _ = canvas()
    layer = Image.new("RGBA", (SIZE, SIZE), TRANSPARENT)
    ld = ImageDraw.Draw(layer)
    rounded(ld, box(260, 260, 764, 764), radius=110, width=48)
    layer = layer.rotate(45, resample=Image.Resampling.BICUBIC, center=(512, 512))
    image.alpha_composite(layer)
    draw = ImageDraw.Draw(image)
    for cx in (375, 512, 649):
        ellipse(draw, box(cx - 42, 470, cx + 42, 554), fill=IVORY, outline=GOLD, width=22)
    return image


def icon_renew():
    image, draw = canvas()
    arc(draw, box(170, 170, 854, 854), 205, 30, fill=GOLD, width=76)
    arc(draw, box(170, 170, 854, 854), 25, 210, fill=IVORY, width=76)
    polygon(draw, [(790, 175), (875, 385), (650, 355)], fill=GOLD, outline=GOLD, width=20)
    polygon(draw, [(235, 849), (150, 639), (375, 669)], fill=IVORY, outline=IVORY, width=20)
    return image


def icon_device():
    image, draw = canvas()
    rounded(draw, box(215, 95, 690, 900), radius=90, width=48)
    line(draw, [(350, 178), (555, 178)], fill=GOLD, width=30)
    line(draw, [(380, 815), (525, 815)], fill=IVORY, width=28)
    ellipse(draw, box(575, 560, 890, 875), fill=INK, width=44)
    line(draw, [(732, 640), (732, 795)], fill=IVORY, width=54)
    line(draw, [(655, 718), (810, 718)], fill=IVORY, width=54)
    return image


def icon_traffic():
    image, draw = canvas()
    top = [(512, 100), (805, 260), (512, 420), (219, 260)]
    left = [(219, 260), (512, 420), (512, 860), (219, 700)]
    right = [(512, 420), (805, 260), (805, 700), (512, 860)]
    polygon(draw, top, fill=INK_2, outline=GOLD, width=35)
    polygon(draw, left, fill=INK, outline=GOLD, width=35)
    polygon(draw, right, fill=INK_2, outline=GOLD, width=35)
    for x, height in ((610, 120), (680, 200), (750, 285)):
        rounded(draw, box(x, 690 - height, x + 42, 690), radius=18, fill=GOLD, outline=IVORY, width=12)
    return image


def icon_bank_card():
    image, draw = canvas()
    rounded(draw, box(110, 230, 914, 795), radius=88, width=48)
    draw.rectangle(box(145, 350, 879, 470), fill=GOLD)
    rounded(draw, box(205, 535, 390, 665), radius=28, fill=GOLD, outline=IVORY, width=18)
    line(draw, [(295, 545), (295, 655)], fill=INK, width=14)
    line(draw, [(215, 600), (378, 600)], fill=INK, width=14)
    ellipse(draw, box(690, 575, 795, 680), fill=GOLD, outline=IVORY, width=18)
    ellipse(draw, box(755, 575, 860, 680), fill=INK_2, outline=GOLD, width=18)
    return image


def icon_crypto():
    image, draw = canvas()
    ellipse(draw, box(105, 105, 919, 919), fill=INK, width=52)
    polygon(draw, [(512, 230), (720, 512), (512, 790), (304, 512)], fill=INK_2, outline=GOLD, width=44)
    line(draw, [(512, 230), (512, 790)], fill=IVORY, width=24)
    line(draw, [(304, 512), (512, 620), (720, 512)], fill=IVORY, width=24)
    line(draw, [(304, 512), (512, 405), (720, 512)], fill=GOLD, width=24)
    return image


def icon_gift():
    image, draw = canvas()
    rounded(draw, box(150, 400, 874, 850), radius=65, width=46)
    rounded(draw, box(105, 320, 919, 500), radius=55, fill=INK_2, width=44)
    draw.rectangle(box(442, 350, 582, 850), fill=GOLD)
    line(draw, [(442, 350), (442, 850)], fill=IVORY, width=18)
    arc(draw, box(240, 100, 520, 410), 175, 355, fill=GOLD, width=54)
    arc(draw, box(504, 100, 784, 410), 185, 5, fill=GOLD, width=54)
    ellipse(draw, box(455, 270, 569, 384), fill=GOLD, outline=IVORY, width=20)
    return image


def icon_promo():
    image, draw = canvas()
    points = [(115, 290), (909, 290), (909, 430), (830, 512), (909, 594), (909, 734), (115, 734), (115, 594), (194, 512), (115, 430)]
    polygon(draw, points, fill=INK, outline=GOLD, width=42)
    line(draw, [(570, 325), (570, 699)], fill=GOLD, width=28)
    ellipse(draw, box(260, 365, 430, 535), fill=INK_2, width=34)
    line(draw, [(300, 650), (455, 390)], fill=IVORY, width=42)
    ellipse(draw, box(360, 545, 470, 655), fill=INK_2, width=28)
    return image


def icon_news():
    image, draw = canvas()
    polygon(draw, [(150, 435), (520, 435), (785, 235), (785, 735), (520, 585), (150, 585)], fill=INK, outline=GOLD, width=44)
    rounded(draw, box(205, 555, 370, 875), radius=50, fill=INK_2, width=40)
    arc(draw, box(700, 315, 940, 655), 285, 75, fill=IVORY, width=34)
    arc(draw, box(735, 245, 1010, 725), 290, 70, fill=GOLD, width=28)
    return image


def icon_invite():
    image, draw = canvas()
    ellipse(draw, box(205, 185, 425, 405), fill=INK, width=40)
    ellipse(draw, box(535, 185, 755, 405), fill=INK, width=40)
    arc(draw, box(100, 360, 540, 820), 185, 355, fill=IVORY, width=56)
    arc(draw, box(420, 360, 860, 820), 185, 355, fill=GOLD, width=56)
    ellipse(draw, box(590, 590, 885, 885), fill=INK, width=44)
    line(draw, [(738, 660), (738, 815)], fill=IVORY, width=52)
    line(draw, [(660, 738), (815, 738)], fill=IVORY, width=52)
    return image


ICONS = {
    "home": icon_home,
    "buy": icon_buy,
    "subscriptions": icon_subscriptions,
    "antijam": icon_antijam,
    "connect": icon_connect,
    "support": icon_support,
    "more": icon_more,
    "renew": icon_renew,
    "device": icon_device,
    "traffic": icon_traffic,
    "bank-card": icon_bank_card,
    "crypto": icon_crypto,
    "gift": icon_gift,
    "promo": icon_promo,
    "news": icon_news,
    "invite": icon_invite,
}


def main():
    MASTER_DIR.mkdir(parents=True, exist_ok=True)
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    for name, factory in ICONS.items():
        master = factory()
        master.save(MASTER_DIR / f"way-spn-{name}.png", optimize=True)
        output = master.resize((100, 100), Image.Resampling.LANCZOS)
        output.save(OUTPUT_DIR / f"way-spn-{name}.png", optimize=True)
    print(f"Built {len(ICONS)} Way SPN emoji in {OUTPUT_DIR}")


if __name__ == "__main__":
    main()
