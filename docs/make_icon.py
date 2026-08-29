"""Draw the app icon.

Drawn from the silhouettes of the two structures this app watches, not from
CCP's art. An Orbital Skyhook reads as a large angled sail with a ring-wrapped
beam running through it, tethered down to the planet; a Sovereignty Hub reads
as a flat grid of panels. Those two shapes are what someone who plays this game
recognises at a glance, and they are simple enough to survive being 32 pixels
wide -- which the structures themselves, in all their detail, are not.

Colours are the app's own: Alliance Auth's slate for the ground, the teal its
active links use, and the amber this app paints a hub that is running low. The
lit core is amber on purpose. A skyhook nobody is watching is the problem this
exists to solve.

Drawn at 4x and downsampled, because PIL has no antialiasing of its own.

    python docs/make_icon.py
"""

import math
import pathlib

from PIL import Image, ImageDraw

OUT = pathlib.Path(__file__).resolve().parent / "images" / "icon.png"
SIZE = 512
SCALE = 4
S = SIZE * SCALE

SLATE = (44, 62, 80)
SLATE_DEEP = (30, 43, 56)
PANEL = (72, 94, 114)
TEAL = (26, 188, 156)
TEAL_DIM = (18, 130, 108)
AMBER = (240, 173, 78)
AMBER_HOT = (252, 211, 141)
PALE = (222, 230, 234)


def main():
    image = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)

    draw.rounded_rectangle((0, 0, S, S), radius=int(S * 0.18), fill=SLATE)

    # The planet's limb, cropped by the tile. Drawing the whole circle and
    # letting the tile cut it is what makes it read as a world rather than a
    # ball sitting on a shelf.
    planet_r = int(S * 0.66)
    pcx, pcy = S // 2, int(S * 1.36)
    draw.ellipse(
        (pcx - planet_r, pcy - planet_r, pcx + planet_r, pcy + planet_r),
        fill=SLATE_DEEP,
    )
    draw.arc(
        (pcx - planet_r, pcy - planet_r, pcx + planet_r, pcy + planet_r),
        start=205,
        end=335,
        fill=TEAL_DIM,
        width=int(S * 0.016),
    )

    # The sail: one big angled plane, which is the half of a skyhook you can
    # identify from across a system. Panelled, because that is also what a
    # sovereignty hub looks like -- one shape doing both jobs.
    sail = [
        (int(S * 0.20), int(S * 0.10)),
        (int(S * 0.50), int(S * 0.22)),
        (int(S * 0.44), int(S * 0.66)),
        (int(S * 0.14), int(S * 0.54)),
    ]
    draw.polygon(sail, fill=PANEL)

    # Panel seams, following the sail's own perspective rather than the tile's
    # axes, or it stops looking like a plane in space.
    def lerp(a, b, t):
        return (a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t)

    for t in (0.25, 0.5, 0.75):
        draw.line(
            (lerp(sail[0], sail[3], t), lerp(sail[1], sail[2], t)),
            fill=SLATE_DEEP,
            width=int(S * 0.011),
        )
    draw.line(
        (lerp(sail[0], sail[1], 0.5), lerp(sail[3], sail[2], 0.5)),
        fill=SLATE_DEEP,
        width=int(S * 0.011),
    )
    # A lit edge along the top, so the sail separates from the background.
    draw.line((sail[0], sail[1]), fill=TEAL, width=int(S * 0.016))

    # The beam, running down from the sail to the planet at the same angle the
    # tether would take.
    beam_top = (int(S * 0.55), int(S * 0.26))
    beam_bottom = (int(S * 0.72), int(S * 0.80))
    draw.line((beam_top, beam_bottom), fill=PALE, width=int(S * 0.030))

    # Rings around the beam. Ellipses rather than bars: that is the detail that
    # says "skyhook" rather than "mast", and it survives being shrunk because
    # it is only three shapes.
    angle = math.atan2(beam_bottom[1] - beam_top[1], beam_bottom[0] - beam_top[0])
    for t in (0.18, 0.44, 0.70):
        x = beam_top[0] + (beam_bottom[0] - beam_top[0]) * t
        y = beam_top[1] + (beam_bottom[1] - beam_top[1]) * t
        rx, ry = int(S * 0.075), int(S * 0.028)
        ring = Image.new("RGBA", (rx * 2 + 40, ry * 2 + 40), (0, 0, 0, 0))
        ImageDraw.Draw(ring).ellipse(
            (20, 20, 20 + rx * 2, 20 + ry * 2),
            outline=TEAL,
            width=int(S * 0.016),
        )
        ring = ring.rotate(-math.degrees(angle) + 90, resample=Image.BICUBIC, expand=True)
        image.alpha_composite(ring, (int(x - ring.width / 2), int(y - ring.height / 2)))

    # The lit core where the beam meets the sail: the one warm thing in the
    # picture, and the thing the app is actually about.
    core_r = int(S * 0.062)
    draw.ellipse(
        (beam_top[0] - core_r, beam_top[1] - core_r,
         beam_top[0] + core_r, beam_top[1] + core_r),
        fill=AMBER,
    )
    inner = int(core_r * 0.45)
    draw.ellipse(
        (beam_top[0] - inner, beam_top[1] - inner,
         beam_top[0] + inner, beam_top[1] + inner),
        fill=AMBER_HOT,
    )

    OUT.parent.mkdir(parents=True, exist_ok=True)
    image.resize((SIZE, SIZE), Image.LANCZOS).save(OUT, optimize=True)

    kilobytes = OUT.stat().st_size / 1024
    print(f"{OUT}  {SIZE}x{SIZE}  {kilobytes:.0f} KB")
    if kilobytes > 200:
        print("!! over the 200 KB the app directory allows")


if __name__ == "__main__":
    main()
