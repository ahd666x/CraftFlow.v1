"""ساخت طیف رنگی (۵۰ تا ۹۵۰) از یک رنگ پایه، هماهنگ با پالت فعلی پروژه."""

# نسبت ترکیب با سفید (روشن‌ترها) و با مشکی (تیره‌ترها)؛ پایه = شیدِ ۶۰۰
_LIGHT = {50: .93, 100: .845, 200: .71, 300: .52, 400: .32, 500: .12}
_DARK = {700: .19, 800: .355, 900: .52, 950: .645}


def hex_to_rgb(value):
    value = value.lstrip('#')
    return tuple(int(value[i:i + 2], 16) for i in (0, 2, 4))


def _mix(rgb, target, t):
    return tuple(round(c + (target - c) * t) for c in rgb)


def rgb_str(rgb):
    return ' '.join(str(c) for c in rgb)


def build_palette(hex_color):
    base = hex_to_rgb(hex_color)
    palette = {600: rgb_str(base)}
    for shade, t in _LIGHT.items():
        palette[shade] = rgb_str(_mix(base, 255, t))
    for shade, t in _DARK.items():
        palette[shade] = rgb_str(_mix(base, 0, t))
    return dict(sorted(palette.items()))