"""Silent like-and-subscribe overlay: a bouncing thumbs-up next to a subscribe
button, then a cursor clicks it and it turns into SUBSCRIBED.
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

W, H, FPS, DUR = 900, 400, 30, 4.0
N = int(FPS * DUR)
EMOJI = ImageFont.truetype(r"C:/Windows/Fonts/seguiemj.ttf", 150)
BOLD = ImageFont.truetype(r"C:/Windows/Fonts/segoeuib.ttf", 54)

# Layout
THUMB_X, THUMB_BASE_Y = 40, 110
BTN = (290, 150, 840, 260)  # x0, y0, x1, y1
CLICK_T = 2.1  # seconds

# Thumbs-up sprite, rendered once (colour emoji)
_t = Image.new("RGBA", (260, 260), (0, 0, 0, 0))
ImageDraw.Draw(_t).text((20, 40), "\U0001f44d", font=EMOJI, embedded_color=True)
THUMB = _t.crop(_t.getbbox())


def ease_out(x: float) -> float:
    return 1 - (1 - x) ** 3


def cursor(scale: float) -> Image.Image:
    """White arrow cursor with a dark outline."""
    s = 3 * scale
    pts = [(0, 0), (0, 17), (4, 13), (7, 20), (10, 19), (7, 12), (12, 12)]
    pts = [(x * s + 6, y * s + 6) for x, y in pts]
    im = Image.new("RGBA", (int(16 * s) + 14, int(24 * s) + 14), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)
    d.polygon(pts, fill=(255, 255, 255, 255), outline=(20, 20, 20, 255), width=max(2, int(s)))
    return im


def frame(i: int) -> Image.Image:
    t = i / FPS
    im = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)

    # Fade in / out
    alpha = min(1.0, t / 0.25, (DUR - t) / 0.35)
    alpha = max(0.0, alpha)

    # Thumbs-up: bounces, faster and higher right after the click
    amp, freq = (28, 2.2) if t < CLICK_T else (40, 3.2)
    bounce = abs(math.sin(math.pi * freq * t)) * amp
    squash = 1.0 + (0.08 if bounce < 4 else 0.0)
    th = THUMB.resize((int(THUMB.width * squash), int(THUMB.height / squash)))
    im.alpha_composite(th, (THUMB_X, int(THUMB_BASE_Y - bounce + (THUMB.height - th.height))))

    # Button (with a soft shadow); pressed state shrinks slightly
    subscribed = t >= CLICK_T
    press = 1.0 - (0.06 if CLICK_T <= t < CLICK_T + 0.12 else 0.0)
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
        k = ease_out(min(1.0, (t - CLICK_T) / 0.25))
        r = 18 * k
        d.ellipse((box[2] - 40 - r, box[1] - 10 - r, box[2] - 40 + r, box[1] - 10 + r), fill=(40, 180, 80, 255))
        if k > 0.6:
            ax, ay = box[2] - 40, box[1] - 10
            d.line([(ax - 8, ay), (ax - 2, ay + 7), (ax + 9, ay - 7)], fill=(255, 255, 255, 255), width=4)

    # Click ripple
    if CLICK_T <= t < CLICK_T + 0.45:
        k = (t - CLICK_T) / 0.45
        rr = 20 + 70 * k
        target = (cx + 238, cy + 10)
        d.ellipse(
            (target[0] - rr, target[1] - rr, target[0] + rr, target[1] + rr),
            outline=(255, 255, 255, int(200 * (1 - k))),
            width=5,
        )

    # Cursor: glides in from the bottom right, clicks, then rests
    start, end = (W + 40, H + 60), (cx + 238, cy + 10)
    k = ease_out(min(1.0, max(0.0, (t - 0.9) / (CLICK_T - 0.9))))
    px = start[0] + (end[0] - start[0]) * k
    py = start[1] + (end[1] - start[1]) * k
    if t >= CLICK_T:
        px, py = end[0] + 18 * ease_out(min(1.0, (t - CLICK_T - 0.3) / 0.6)) if t > CLICK_T + 0.3 else end[0], end[1]
    cur = cursor(0.85 if CLICK_T <= t < CLICK_T + 0.12 else 1.0)
    im.alpha_composite(cur, (int(px) - 6, int(py) - 6))

    if alpha < 1.0:
        a = im.getchannel("A").point(lambda v: int(v * alpha))
        im.putalpha(a)
    return im


for i in range(N):
    frame(i).save(FR / f"f{i:04d}.png")

src = ["-framerate", str(FPS), "-i", str(FR / "f%04d.png")]
run = lambda args: subprocess.run(["ffmpeg", "-v", "error", "-y", *args], check=True)
run([*src, "-c:v", "prores_ks", "-profile:v", "4444", "-pix_fmt", "yuva444p10le", str(OUT / "subscribe_alpha.mov")])
run([*src, "-c:v", "libvpx-vp9", "-pix_fmt", "yuva420p", "-b:v", "2M", str(OUT / "subscribe_alpha.webm")])
run(
    [
        "-f", "lavfi", "-i", f"color=c=0x00ff00:s={W}x{H}:r={FPS}:d={DUR}",
        *src,
        "-filter_complex", "[0:v][1:v]overlay=format=auto,format=yuv420p",
        "-c:v", "libx264", "-crf", "16", str(OUT / "subscribe_greenscreen.mp4"),
    ]
)
# Preview sheet: 4 key frames on a dark background
sheet = Image.new("RGB", (W * 2, H * 2), (30, 30, 34))
for n, sec in enumerate((0.6, 1.8, 2.25, 3.0)):
    fr = frame(int(sec * FPS))
    bg = Image.new("RGBA", (W, H), (30, 30, 34, 255))
    bg.alpha_composite(fr)
    sheet.paste(bg.convert("RGB"), ((n % 2) * W, (n // 2) * H))
sheet.save(OUT / "preview.png")
print("ok", N, "frames")
