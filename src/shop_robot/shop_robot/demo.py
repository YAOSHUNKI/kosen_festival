"""ROS なしで注文画面を試すデモ。

    cd src/shop_robot && python3 -m shop_robot.demo
    → http://localhost:8000/ （スタッフ画面）, http://localhost:8000/display （呼び出し表示）

キューとロボット動作は同じプロセス内で模擬する。マーカーは「待ち行列」タブの
「動作確認」から読んだことにできる。--camera 0 で PC の Web カメラを使えば、
番号札をかざしての読み取りとカメラ映像の表示まで試せる。
"""
import argparse
from pathlib import Path

import uvicorn

from .core.products import ProductStore
from .web.app import create_app
from .web.bridge import LocalBridge

SAMPLE = [(1, "たこ焼き", 300), (2, "フランクフルト", 200), (3, "ラムネ", 150), (4, "わたあめ", 250)]


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--port", type=int, default=8000)
    ap.add_argument("--products", default="./demo_data/products.json")
    ap.add_argument("--dict", default="DICT_4X4_1000")
    ap.add_argument("--seconds-per-item", type=float, default=2.0)
    ap.add_argument("--camera", default=None, help="カメラ番号（例: 0）または動画ファイル。省略時はカメラなし")
    ap.add_argument("--no-mirror", action="store_true", help="カメラ映像を鏡像にしない")
    args = ap.parse_args()

    new = not Path(args.products).exists()
    products = ProductStore(args.products)
    if new:
        for pid, name, price in SAMPLE:
            products.add(pid, name, price)

    bridge = LocalBridge(args.seconds_per_item, args.dict, mirror=not args.no_mirror)
    if args.camera is not None:
        bridge.start_camera(int(args.camera) if args.camera.isdigit() else args.camera)
    app = create_app(bridge, products, args.dict)
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
