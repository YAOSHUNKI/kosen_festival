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
            p = Product(int(d["id"]), str(d["name"]), int(d.get("price", 0)))
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

    def add(self, product_id: int, name: str, price: int = 0) -> Product:
        name = name.strip()
        if product_id < 0:
            raise ProductError("商品IDは0以上にしてください")
        if not name:
            raise ProductError("商品名を入力してください")
        if price < 0:
            raise ProductError("価格は0以上にしてください")
        with self._lock:
            if product_id in self._products:
                raise ProductError(f"商品ID {product_id} は既に使われています")
            p = Product(product_id, name, price)
            self._products[product_id] = p
            self._save()
            return p

    def update(self, product_id: int, name: str, price: int = 0) -> Product:
        name = name.strip()
        if not name:
            raise ProductError("商品名を入力してください")
        with self._lock:
            if product_id not in self._products:
                raise ProductError(f"商品ID {product_id} はありません")
            p = Product(product_id, name, max(0, price))
            self._products[product_id] = p
            self._save()
            return p

    def delete(self, product_id: int) -> None:
        with self._lock:
            if self._products.pop(product_id, None) is None:
                raise ProductError(f"商品ID {product_id} はありません")
            self._save()
