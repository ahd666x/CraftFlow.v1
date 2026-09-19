"""Convert Vazirmatn woff2 to ttf (force TTF flavor) and place in static/fonts/."""
import os
from fontTools.ttLib import TTFont

base = "static/fonts"
os.makedirs(base, exist_ok=True)

src_dir = os.path.join("static", "selvi", "fonts")
weights = ["Thin", "ExtraLight", "Light", "Regular", "Medium", "SemiBold", "Bold", "ExtraBold", "Black"]

for w in weights:
    woff2 = os.path.join(src_dir, f"Vazirmatn-{w}.woff2")
    out = os.path.join(base, f"Vazirmatn-{w}.ttf")
    if not os.path.exists(woff2):
        print("missing source:", woff2)
        continue
    font = TTFont(woff2)
    font.flavor = None  # force plain TTF output
    font.save(out)
    # verify it's a real ttf now
    with open(out, "rb") as fh:
        head = fh.read(4)
    print("converted", woff2, "->", out, "size", os.path.getsize(out), "magic", head.hex())

from fontTools.ttLib import TTFont as TT
f = TT(os.path.join(base, "Vazirmatn-Regular.ttf"))
print("tables:", sorted(f.keys()))
print("OK")
