"""Silent like-and-subscribe overlay: a cursor clicks a greyed-out thumbs-up
(it lights up and starts bouncing), then clicks the subscribe button next to
it, which turns into SUBSCRIBED.
Original design (no channel's artwork, no YouTube logo).
usage: python subscribe_anim.py <out_dir>
Writes frames/ (RGBA PNG), subscribe_alpha.mov (ProRes 4444 with alpha),
subscribe_alpha.webm (VP9 with alpha) and subscribe_greenscreen.mp4."""

import math
import subprocess
import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageFont

OUT = Path(sys.argv[1])
FR = OUT / "frames"
FR.mkdir(parents=True, exist_ok=True)
for old in FR.glob("f*.png"):
    old.unlink()

W, H, FPS, DUR = 900, 400, 30, 5.0
N = int(FPS * DUR)
EMOJI = ImageFont.truetype(r"C:/Windows/Fonts/seguiemj.ttf", 150)
BOLD = ImageFont.truetype(r"C:/Windows/Fonts/segoeuib.ttf", 54)

# Layout
THUMB_X, THUMB_BASE_Y = 40, 110
BTN = (290, 150, 840, 260)  # x0, y0, x1, y1
# Timeline (owner 2026-11-26): the cursor clicks the greyed thumb (it lights
# up and starts bouncing), then glides to SUBSCRIBE and clicks it.
LIKE_T = 1.3
SUB_T = 2.8
PRESS_S = 0.12
RIPPLE_S = 0.45

# Thumbs-up sprite, rendered once (colour emoji), plus a greyed-out copy
_t = Image.new("RGBA", (260, 260), (0, 0, 0, 0))
ImageDraw.Draw(_t).text((20, 40), "👍", font=EMOJI, embedded_color=True)
THUMB = _t.crop(_t.getbbox())
_g = THUMB.convert("LA").convert("RGBA")
_g.putalpha(THUMB.getchannel("A").point(lambda v: int(v * 0.75)))
THUMB_GREY = _g

THUMB_C = (THUMB_X + THUMB.width / 2, THUMB_BASE_Y + THUMB.height / 2)


def ease_out(x: float) -> float:
    return 1 - (1 - x) ** 3


def ease_in_out(x: float) -> float:
    x = min(1.0, max(0.0, x))
    return 3 * x * x - 2 * x * x * x


def cursor(scale: float) -> Image.Image:
    """White arrow cursor with a dark outline."""
    s = 3 * scale
    pts = [(0, 0), (0, 17), (4, 13), (7, 20), (10, 19), (7, 12), (12, 12)]
    pts = [(x * s + 6, y * s + 6) for x, y in pts]
    im = Image.new("RGBA", (int(16 * s) + 14, int(24 * s) + 14), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)
    d.polygon(pts, fill=(255, 255, 255, 255), outline=(20, 20, 20, 255), width=max(2, int(s)))
    return im


def ripple(d: ImageDraw.ImageDraw, t: float, at: float, c: tuple[float, float]) -> None:
    if at <= t < at + RIPPLE_S:
        k = (t - at) / RIPPLE_S
        rr = 20 + 70 * k
        d.ellipse(
            (c[0] - rr, c[1] - rr, c[0] + rr, c[1] + rr),
            outline=(255, 255, 255, int(200 * (1 - k))),
            width=5,
        )


def frame(i: int) -> Image.Image:
    t = i / FPS
    im = Image.new("RGBA", (W, H), (0, 0, 0, 0))

    # Fade in / out
    alpha = max(0.0, min(1.0, t / 0.25, (DUR - t) / 0.35))

    # Thumbs-up: grey and still until clicked, then pops in colour and
    # bounces, harder after the subscribe click
    liked = t >= LIKE_T
    if not liked:
        th, dy = THUMB_GREY, 0.0
    else:
        amp, freq = (28, 2.2) if t < SUB_T else (40, 3.2)
        bounce = abs(math.sin(math.pi * freq * (t - LIKE_T))) * amp
        pop = 1.0 + 0.25 * max(0.0, 1 - (t - LIKE_T) / 0.25)
        squash = 1.0 + (0.08 if bounce < 4 else 0.0)
        th = THUMB.resize((int(THUMB.width * squash * pop), int(THUMB.height / squash * pop)))
        dy = -bounce
    tx = int(THUMB_C[0] - th.width / 2)
    ty = int(THUMB_BASE_Y + THUMB.height - th.height + dy)
    im.alpha_composite(th, (tx, ty))

    # Button (with a soft shadow); pressed state shrinks slightly
    subscribed = t >= SUB_T
    press = 1.0 - (0.06 if SUB_T <= t < SUB_T + PRESS_S else 0.0)
    x0, y0, x1, y1 = BTN
    cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
    hw, hh = (x1 - x0) / 2 * press, (y1 - y0) / 2 * press
    box = (cx - hw, cy - hh, cx + hw, cy + hh)
    shadow = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    ImageDraw.Draw(shadow).rounded_rectangle(
        (box[0] + 6, box[1] + 8, box[2] + 6, box[3] + 8), radius=22, fill=(0, 0, 0, 110)
    )
    im.alpha_composite(shadow.filter(ImageFilter.GaussianBlur(6)))
    d = ImageDraw.Draw(im)
    fill = (90, 90, 90, 255) if subscribed else (230, 33, 23, 255)
    d.rounded_rectangle(box, radius=22, fill=fill)
    label = "SUBSCRIBED" if subscribed else "SUBSCRIBE"
    tw = d.textlength(label, font=BOLD)
    d.text((cx - tw / 2, cy - 36), label, font=BOLD, fill=(255, 255, 255, 255))
    if subscribed:  # a check mark pops in
        k = ease_out(min(1.0, (t - SUB_T) / 0.25))
        r = 18 * k
        ax, ay = box[2] - 40, box[1] - 10
        d.ellipse((ax - r, ay - r, ax + r, ay + r), fill=(40, 180, 80, 255))
        if k > 0.6:
            d.line(
                [(ax - 8, ay), (ax - 2, ay + 7), (ax + 9, ay - 7)],
                fill=(255, 255, 255, 255),
                width=4,
            )

    # Cursor: enters bottom right -> thumb (click) -> subscribe (click) -> rests
    enter = (W + 40, H + 60)
    on_thumb = (THUMB_C[0] + 20, THUMB_C[1] + 15)
    on_btn = (cx + 238, cy + 10)
    ripple(d, t, LIKE_T, on_thumb)
    ripple(d, t, SUB_T, on_btn)
    if t < LIKE_T:
        k = ease_out(min(1.0, max(0.0, (t - 0.3) / (LIKE_T - 0.35))))
        a, b = enter, on_thumb
    elif t < SUB_T:
        k = ease_in_out((t - LIKE_T - 0.35) / (SUB_T - LIKE_T - 0.45))
        a, b = on_thumb, on_btn
    else:
        k = ease_out(min(1.0, max(0.0, (t - SUB_T - 0.3) / 0.6)))
        a, b = on_btn, (on_btn[0] + 18, on_btn[1])
    px, py = a[0] + (b[0] - a[0]) * k, a[1] + (b[1] - a[1]) * k
    pressed = LIKE_T <= t < LIKE_T + PRESS_S or SUB_T <= t < SUB_T + PRESS_S
    im.alpha_composite(cursor(0.85 if pressed else 1.0), (int(px) - 6, int(py) - 6))

    if alpha < 1.0:
        a_ch = im.getchannel("A").point(lambda v: int(v * alpha))
        im.putalpha(a_ch)
    return im


for i in range(N):
    frame(i).save(FR / f"f{i:04d}.png")

src = ["-framerate", str(FPS), "-i", str(FR / "f%04d.png")]
run = lambda args: subprocess.run(["ffmpeg", "-v", "error", "-y", *args], check=True)
run(
    [
        *src,
        "-c:v",
        "prores_ks",
        "-profile:v",
        "4444",
        "-pix_fmt",
        "yuva444p10le",
        str(OUT / "subscribe_alpha.mov"),
    ]
)
run(
    [
        *src,
        "-c:v",
        "libvpx-vp9",
        "-pix_fmt",
        "yuva420p",
        "-b:v",
        "2M",
        str(OUT / "subscribe_alpha.webm"),
    ]
)
run(
    [
        "-f",
        "lavfi",
        "-i",
        f"color=c=0x00ff00:s={W}x{H}:r={FPS}:d={DUR}",
        *src,
        "-filter_complex",
        "[0:v][1:v]overlay=format=auto,format=yuv420p",
        "-c:v",
        "libx264",
        "-crf",
        "16",
        str(OUT / "subscribe_greenscreen.mp4"),
    ]
)
# Preview sheet: 4 key frames on a dark background
sheet = Image.new("RGB", (W * 2, H * 2), (30, 30, 34))
for n, sec in enumerate((0.9, 1.6, 2.5, 3.3)):
    fr = frame(int(sec * FPS))
    bg = Image.new("RGBA", (W, H), (30, 30, 34, 255))
    bg.alpha_composite(fr)
    sheet.paste(bg.convert("RGB"), ((n % 2) * W, (n // 2) * H))
sheet.save(OUT / "preview.png")
print("ok", N, "frames")
