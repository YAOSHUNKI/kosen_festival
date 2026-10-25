"""商品マスタ（JSON ファイルに保存）。"""
from __future__ import annotations

import json
import os
import tempfile
import threading
from dataclasses import dataclass, asdict
from pathlib import Path


class ProductError(ValueError):
    pass


@dataclass
class Product:
    id: int
    name: str
    price: int = 0  # 円。0 なら画面で合計を出さない
    robot_id: int | None = None  # ロボットに送るID。None なら id と同じ（割引版など中身が同じ商品は元の商品のIDにする）

    @property
    def send_id(self) -> int:
        return self.id if self.robot_id is None else self.robot_id


class ProductStore:
    def __init__(self, path: str | os.PathLike):
        self.path = Path(path).expanduser()
        self._lock = threading.Lock()
        self._products: dict[int, Product] = {}
        self._load()

    # --- 読み書き ---------------------------------------------------------
    def _load(self) -> None:
        if not self.path.exists():
            return
        data = json.loads(self.path.read_text(encoding="utf-8"))
        for d in data.get("products", []):
            rid = d.get("robot_id")
            p = Product(int(d["id"]), str(d["name"]), int(d.get("price", 0)), None if rid is None else int(rid))
            self._products[p.id] = p

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"products": [asdict(p) for p in self.list()]}
        # 途中で落ちても壊れないよう一時ファイル経由で置き換える
        fd, tmp = tempfile.mkstemp(dir=self.path.parent, suffix=".tmp")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
        os.replace(tmp, self.path)

    # --- 操作 -------------------------------------------------------------
    def list(self) -> list[Product]:
        return sorted(self._products.values(), key=lambda p: p.id)

    def get(self, product_id: int) -> Product | None:
        return self._products.get(product_id)

    @staticmethod
    def _robot_id(product_id: int, robot_id: int | None) -> int | None:
        if robot_id is None or robot_id == product_id:
            return None   # 商品IDと同じなら持たない
        if robot_id < 0:
            raise ProductError("ロボット用IDは0以上にしてください")
        return robot_id

    def add(self, product_id: int, name: str, price: int = 0, robot_id: int | None = None) -> Product:
        name = name.strip()
        if product_id < 0:
            raise ProductError("商品IDは0以上にしてください")
        if not name:
            raise ProductError("商品名を入力してください")
        if price < 0:
            raise ProductError("価格は0以上にしてください")
        rid = self._robot_id(product_id, robot_id)
        with self._lock:
            if product_id in self._products:
                raise ProductError(f"商品ID {product_id} は既に使われています")
            p = Product(product_id, name, price, rid)
            self._products[product_id] = p
            self._save()
            return p

    def update(self, product_id: int, name: str, price: int = 0, robot_id: int | None = None) -> Product:
        name = name.strip()
        if not name:
            raise ProductError("商品名を入力してください")
        rid = self._robot_id(product_id, robot_id)
        with self._lock:
            if product_id not in self._products:
                raise ProductError(f"商品ID {product_id} はありません")
            p = Product(product_id, name, max(0, price), rid)
            self._products[product_id] = p
            self._save()
            return p

    def delete(self, product_id: int) -> None:
        with self._lock:
            if self._products.pop(product_id, None) is None:
                raise ProductError(f"商品ID {product_id} はありません")
            self._save()
