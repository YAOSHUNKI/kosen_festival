"""ロボットとやり取りするトピックの形式（標準メッセージ型）。ROS 非依存。

/shop/prepare_order  std_msgs/Int32MultiArray   システム → ロボット
    data = [商品ID, 個数, 商品ID, 個数, ...]
    お客様が呼び出し中の番号札（AR マーカー）をかざしたときだけ、その注文の分を1回 publish する。

/shop/prepare_done   std_msgs/Bool              ロボット → システム
    data = true（成功） / false（失敗）
    受け取るまで次の注文は依頼しない（次のマーカーは「前のお客様の対応中です」になる）。
"""
from __future__ import annotations

from .models import Order, OrderItem


class CodecError(ValueError):
    pass


def encode_items(order: Order) -> list[int]:
    data: list[int] = []
    for it in order.items:
        data += [int(it.product_id), int(it.quantity)]
    return data


def decode_items(data, names: dict[int, str] | None = None) -> list[OrderItem]:
    """Int32MultiArray.data → 商品のリスト。names があれば商品名を埋める"""
    data = [int(v) for v in data]
    if not data or len(data) % 2 != 0:
        raise CodecError(f"形式が不正です（[商品ID, 個数, ...] の偶数個が必要）: {data}")
    names = names or {}
    items = [OrderItem(pid, names.get(pid, ""), qty) for pid, qty in zip(data[0::2], data[1::2])]
    if any(it.quantity <= 0 for it in items):
        raise CodecError(f"個数は1以上が必要です: {data}")
    return items
