"""番号札（注文番号 = ArUco マーカーID）を A4 の PDF にまとめて出力する。

    ros2 run shop_robot make_marker_cards --start 1 --end 60 -o cards.pdf
    （ROS なし）cd src/shop_robot && python3 -m shop_robot.tools.make_marker_cards --start 1 --end 60

印刷は「実際のサイズ / 100%」で。切り取り線で切って配る。
"""
import argparse
import glob
import os

from PIL import Image, ImageDraw, ImageFont

from ..core.aruco import dictionary_size, generate_marker

DPI = 300
MM = DPI / 25.4
A4 = (int(210 * MM), int(297 * MM))

FONT_CANDIDATES = [
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Black.ttc",
    "/usr/share/fonts/truetype/noto/NotoSansCJK-Bold.ttc",
    "/usr/share/fonts/noto-cjk/NotoSansCJK-Bold.ttc",
    "/usr/share/fonts/truetype/fonts-japanese-gothic.ttf",
    "/usr/share/fonts/truetype/takao-gothic/TakaoPGothic.ttf",
    "/System/Library/Fonts/ヒラギノ角ゴシック W6.ttc",
    "C:/Windows/Fonts/meiryob.ttc",
]


def find_font(path=None):
    if path:
        return path
    for p in FONT_CANDIDATES + sorted(glob.glob("/usr/share/fonts/**/*CJK*Bold*", recursive=True)):
        if os.path.exists(p):
            return p
    return None


def load(font_path, size):
    if font_path:
        return ImageFont.truetype(font_path, size)
    try:
        return ImageFont.truetype("DejaVuSans-Bold.ttf", size)
    except OSError:
        return ImageFont.load_default()


def card(n, w, h, dict_name, marker_mm, font_path, shop_name):
    img = Image.new("L", (w, h), 255)
    d = ImageDraw.Draw(img)
    big, small = load(font_path, int(12 * MM)), load(font_path, int(3.2 * MM))
    jp = font_path is not None  # 日本語フォントがなければ日本語の文言は省く

    y = int(3 * MM)
    title = shop_name if jp and shop_name else "No."
    d.text((w / 2, y), title, font=small, fill=0, anchor="mt")
    y += int(4.5 * MM)
    d.text((w / 2, y), str(n), font=big, fill=0, anchor="mt")
    y += int(12.5 * MM)

    size = int(marker_mm * MM)
    m = Image.fromarray(generate_marker(dict_name, n, size))
    img.paste(m, ((w - size) // 2, y))
    y += size + int(2.5 * MM)
    if jp:
        d.text((w / 2, y), "呼ばれたらこの面をカメラへ", font=small, fill=80, anchor="mt")
    return img


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--start", type=int, default=1)
    ap.add_argument("--end", type=int, default=60, help="この番号まで（含む）")
    ap.add_argument("--dict", default="DICT_4X4_1000", help="params.yaml の aruco_dictionary と同じにする")
    ap.add_argument("--marker-mm", type=float, default=40.0, help="マーカーの一辺 [mm]")
    ap.add_argument("--cols", type=int, default=3)
    ap.add_argument("--rows", type=int, default=4)
    ap.add_argument("--shop-name", default="", help="札の上部に入れる店名")
    ap.add_argument("--font", default=None, help="日本語フォントのパス（自動検出できない場合）")
    ap.add_argument("-o", "--out", default="marker_cards.pdf")
    args = ap.parse_args()

    limit = dictionary_size(args.dict)
    if not (0 <= args.start <= args.end < limit):
        ap.error(f"{args.dict} の番号は 0〜{limit - 1} です")

    font = find_font(args.font)
    if font is None:
        print("注意: 日本語フォントが見つからないため、数字とマーカーのみで出力します（--font で指定可）")

    margin = int(8 * MM)
    cw = (A4[0] - 2 * margin) // args.cols
    ch = (A4[1] - 2 * margin) // args.rows
    need = 3 + 4.5 + 12.5 + args.marker_mm + 2.5 + 4  # 札の縦方向レイアウト [mm]
    if ch / MM < need or cw / MM < args.marker_mm + 8:
        ap.error(f"札が小さすぎます（{cw / MM:.0f}×{ch / MM:.0f}mm）。--marker-mm を小さくするか --rows/--cols を減らしてください")

    numbers = list(range(args.start, args.end + 1))
    per_page = args.cols * args.rows
    pages = []
    for p0 in range(0, len(numbers), per_page):
        page = Image.new("L", A4, 255)
        d = ImageDraw.Draw(page)
        for i, n in enumerate(numbers[p0:p0 + per_page]):
            x = margin + (i % args.cols) * cw
            y = margin + (i // args.cols) * ch
            page.paste(card(n, cw, ch, args.dict, args.marker_mm, font, args.shop_name), (x, y))
        # 切り取り線（破線）
        for c in range(args.cols + 1):
            x = margin + c * cw
            for yy in range(margin, margin + args.rows * ch, 30):
                d.line([(x, yy), (x, yy + 15)], fill=170, width=2)
        for r in range(args.rows + 1):
            y = margin + r * ch
            for xx in range(margin, margin + args.cols * cw, 30):
                d.line([(xx, y), (xx + 15, y)], fill=170, width=2)
        pages.append(page)

    pages[0].save(args.out, save_all=True, append_images=pages[1:], resolution=DPI)
    print(f"{args.out}: {len(numbers)}枚（{len(pages)}ページ, {args.dict}, マーカー {args.marker_mm:.0f}mm）")


if __name__ == "__main__":
    main()
