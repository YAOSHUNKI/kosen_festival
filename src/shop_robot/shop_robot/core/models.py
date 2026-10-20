"""ROS に依存しないデータモデル。標準メッセージ型との変換は core/codec.py で行う。"""
from __future__ import annotations

import time
from dataclasses import dataclass, field, asdict


@dataclass
class OrderItem:
    product_id: int
    product_name: str
    quantity: int

    @classmethod
    def from_dict(cls, d: dict) -> "OrderItem":
        return cls(int(d["product_id"]), str(d.get("product_name", "")), int(d["quantity"]))


@dataclass
class Order:
    order_number: int
    items: list[OrderItem]
    stamp: float = field(default_factory=time.time)  # UNIX 秒

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "Order":
        return cls(
            order_number=int(d["order_number"]),
            items=[OrderItem.from_dict(i) for i in d["items"]],
            stamp=float(d.get("stamp", time.time())),
        )

    def summary(self) -> str:
        return " / ".join(f"{i.product_name or i.product_id}×{i.quantity}" for i in self.items)


# キュー状態（/shop/queue_status の JSON の state）
STATE_IDLE = 0          # 注文なし
STATE_PENDING = 1       # 準備中の注文のみ
STATE_CALLING = 2       # 呼び出し中（マーカー待ち）
STATE_ROBOT_BUSY = 3    # ロボット動作中
