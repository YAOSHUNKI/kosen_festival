"""ROS なしで注文画面を試すデモ。

    cd src/shop_robot && python3 -m shop_robot.demo
    → http://localhost:8000/ （スタッフ画面）, http://localhost:8000/display （呼び出し表示）

キューと準備動作は同じプロセス内で模擬する。マーカーは「待ち行列」タブの
「動作確認」から読んだことにできる。
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
    args = ap.parse_args()

    new = not Path(args.products).exists()
    products = ProductStore(args.products)
    if new:
        for pid, name, price in SAMPLE:
            products.add(pid, name, price)

    app = create_app(LocalBridge(args.seconds_per_item), products, args.dict)
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
