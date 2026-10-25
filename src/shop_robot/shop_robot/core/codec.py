"""ロボットとやり取りするトピックの形式（標準メッセージ型）。ROS 非依存。

/shop/prepare_order  std_msgs/Int32MultiArray   システム → ロボット
    data = [商品ID, 個数, 商品ID, 個数, ...]
    お客様が呼び出し中の番号札（AR マーカー）をかざしたときだけ、その注文の分を1回 publish する。
    ・登録されている商品はすべて入れる（注文されていない商品は個数 0）。並びは商品IDの小さい順で固定
    ・商品ID は商品マスタの「ロボット用ID」（未設定なら商品ID）。同じIDは個数を合計して1つにまとめる
    例: 登録が ID 1, 2, 3, 4 と 11（ロボット用ID 1）、注文が ID 1 ×2 と ID 11 ×1 → [1, 3, 2, 0, 3, 0, 4, 0]

/shop/prepare_done   std_msgs/Bool              ロボット → システム
    data = true（成功） / false（失敗）
    受け取るまで次の注文は依頼しない（次のマーカーは「前のお客様の対応中です」になる）。
"""
from __future__ import annotations

from .models import Order, OrderItem


class CodecError(ValueError):
    pass


def encode_items(order: Order, all_ids=()) -> list[int]:
    """all_ids: 登録されている全商品の送るID。注文になくても個数 0 で入れる"""
    totals: dict[int, int] = {int(i): 0 for i in all_ids}
    for it in order.items:
        totals[it.send_id] = totals.get(it.send_id, 0) + int(it.quantity)
    data: list[int] = []
    for sid in sorted(totals):   # 商品IDの小さい順（毎回同じ並び）
        data += [int(sid), totals[sid]]
    return data


def decode_items(data, names: dict[int, str] | None = None) -> list[OrderItem]:
    """Int32MultiArray.data → 作る商品のリスト（個数 0 の商品は含めない）。names があれば商品名を埋める"""
    data = [int(v) for v in data]
    if not data or len(data) % 2 != 0:
        raise CodecError(f"形式が不正です（[商品ID, 個数, ...] の偶数個が必要）: {data}")
    if any(q < 0 for q in data[1::2]):
        raise CodecError(f"個数は0以上が必要です: {data}")
    names = names or {}
    items = [OrderItem(pid, names.get(pid, ""), qty) for pid, qty in zip(data[0::2], data[1::2]) if qty > 0]
    if not items:
        raise CodecError(f"個数がすべて0です: {data}")
    return items
