"""Draw the pinned M15 archive as a local-only, point-in-time structural SVG."""
import argparse
from datetime import datetime, timezone
import hashlib
from html import escape
from pathlib import Path

from research.data.loader import DEFAULT_DATA, DEFAULT_SHA256, ROOT, load_bars
from research.ultimate.structure import structural_features


def clock(stamp):
    return datetime.fromtimestamp(stamp / 1000, timezone.utc).strftime("%d.%m %H:%M")


def render_svg(bars, features, count=80):
    if not 24 <= count <= 120 or len(bars) != len(features) or len(bars) < count:
        raise ValueError("expected aligned candles and a window of 24–120 bars")
    bars, features = bars[-count:], features[-count:]
    if any(bar["time_close"] != row["available_at"] for bar, row in zip(bars, features)):
        raise ValueError("feature availability differs from candle close")

    left, top, width, height = 70, 95, 940, 395
    low, high = min(b["low"] for b in bars), max(b["high"] for b in bars)
    margin = max(.08 * (high - low), .05)
    low, high = low - margin, high + margin
    step = width / count

    def x(i):
        return left + (i + .5) * step

    def y(price):
        return top + (high - price) / (high - low) * height

    items = ['<svg xmlns="http://www.w3.org/2000/svg" width="1230" height="566" viewBox="0 0 1230 566">',
             '<title>QIRVANTA — затворена M15 ценова структура</title>',
             '<desc>Локална историческа визуализация. Маркерите започват на close на потвърждението.</desc>',
             '<rect width="1230" height="566" rx="10" fill="#101827"/>',
             f'<defs><clipPath id="chart"><rect x="{left}" y="{top}" width="{width}" height="{height}"/></clipPath></defs>']

    def line(x1, y1, x2, y2, color, extra=""):
        items.append(f'<line x1="{x1:.2f}" y1="{y1:.2f}" x2="{x2:.2f}" y2="{y2:.2f}" stroke="{color}" {extra}/>')

    def text(x0, y0, value, color="#bfcbdf", size=13):
        items.append(f'<text x="{x0:.2f}" y="{y0:.2f}" fill="{color}" font-family="Segoe UI, Arial, sans-serif" '
                     f'font-size="{size}">{escape(value)}</text>')

    text(left, 38, "QIRVANTA · OANDA:XAUUSD · M15", "#e7efff", 23)
    text(left, 65, f"{clock(bars[0]['time'])} – {clock(bars[-1]['time_close'])} UTC · {count} архивирани свещи")
    for n in range(6):
        value = low + n * (high - low) / 5
        line(left, y(value), left + width, y(value), "#29354a")
        text(13, y(value) + 4, f"{value:.1f}")
    for i in range(0, count, 12):
        line(x(i), top, x(i), top + height, "#243044")
        text(x(i) - 22, top + height + 24, clock(bars[i]["time"]), size=11)

    items.append('<g clip-path="url(#chart)">')
    for i, (bar, row) in enumerate(zip(bars, features)):
        if row["gap"]:
            line(x(i) - step / 2, top, x(i) - step / 2, top + height, "#f0be74",
                 'stroke-dasharray="3 5" opacity=".65"')
        asian = row["asian_range_utc"]
        if asian:
            items.append(f'<rect x="{x(i) + step / 2:.2f}" y="{y(asian["high"]):.2f}" width="{step:.2f}" '
                         f'height="{y(asian["low"]) - y(asian["high"]):.2f}" fill="#7585ef" opacity=".09"/>')
        side = row["causal_fvg"]
        if i >= 2 and side:
            old = bars[i - 2]
            upper, lower = (bar["low"], old["high"]) if side == 1 else (old["low"], bar["high"])
            color = "#2aceb3" if side == 1 else "#ef7b99"
            items.append(f'<rect x="{x(i) + step / 2:.2f}" y="{y(upper):.2f}" width="{step * 2:.2f}" '
                         f'height="{max(.9, y(lower) - y(upper)):.2f}" fill="{color}" opacity=".24">'
                         f'<title>FVG proxy от {clock(bar["time_close"])} UTC</title></rect>')
        for field, color in (("pivot_high", "#e0ae65"), ("pivot_low", "#70a8f4")):
            level = row[field]
            if level is not None:
                line(x(i) + step / 2, y(level), x(i) + 1.5 * step, y(level), color,
                     'stroke-dasharray="3 2" opacity=".70"')

    for i, (bar, row) in enumerate(zip(bars, features)):
        color = "#55c8b6" if bar["close"] >= bar["open"] else "#e47a8f"
        line(x(i), y(bar["high"]), x(i), y(bar["low"]), color)
        body_top = min(y(bar["open"]), y(bar["close"]))
        body_height = max(1.2, abs(y(bar["open"]) - y(bar["close"])))
        items.append(f'<rect x="{x(i) - step * .29:.2f}" y="{body_top:.2f}" width="{step * .58:.2f}" '
                     f'height="{body_height:.2f}" fill="{color}"/>')
        side = row["bos"]
        if side:
            label, marker = (("MSS", "#f6a6fa") if row["mss"] else
                             ("CHoCH", "#ba91ed") if row["choch"] else ("BOS", "#6ce3ff"))
            px = x(i) + step / 2
            py = y(bar["low"]) + 8 if side == 1 else y(bar["high"]) - 8
            items.append(f'<path d="M {px:.2f} {py:.2f} l -4 {side * 7} h 8 z" fill="{marker}">'
                         f'<title>{label} на {clock(bar["time_close"])} UTC</title></path>')
    items.append("</g>")

    text(1033, 112, "СТРУКТУРА", "#e7efff", 15)
    for i, (label, color) in enumerate((("BOS", "#6ce3ff"), ("CHoCH / MSS", "#ba91ed"),
                                         ("Swing high", "#e0ae65"), ("Swing low", "#70a8f4"),
                                         ("FVG proxy", "#55c8b6"), ("Азия след 06:00 UTC", "#a2a9f5"),
                                         ("Времева пауза", "#f0be74"))):
        yy = 145 + i * 31
        line(1034, yy - 4, 1053, yy - 4, color, 'stroke-width="2"')
        text(1060, yy, label)
    text(left, 545, f"Ценови proxy · начало при потвърден close · SHA256 {DEFAULT_SHA256[:16]}…", size=12)
    items.append("</svg>")
    return "\n".join(items) + "\n"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--bars", type=int, default=80)
    args = parser.parse_args()
    path = args.output.resolve()
    if path.suffix.lower() != ".svg" or path.is_relative_to(ROOT.resolve()) or path.exists():
        parser.error("choose a new .svg outside the repository")
    bars = load_bars()
    content = render_svg(bars, structural_features(bars), count=args.bars)
    with path.open("x", encoding="utf-8", newline="\n") as stream:
        stream.write(content)
    print(f"SVG {hashlib.sha256(content.encode('utf-8')).hexdigest()}  {path}")


if __name__ == "__main__":
    main()
