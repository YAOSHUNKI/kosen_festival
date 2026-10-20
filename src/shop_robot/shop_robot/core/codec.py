"""標準メッセージ型（std_msgs）に載せるためのエンコード／デコード。ROS 非依存。

注文（/shop/orders, /shop/prepare_order）: std_msgs/Int32MultiArray
    data = [注文番号, 商品ID, 個数, 商品ID, 個数, ...]

ロボット動作の結果（/shop/prepare_result）: std_msgs/Int32MultiArray
    data = [注文番号, 結果]   結果: 1 = 成功, 0 = 失敗, 2 = 取消

ロボット動作の進捗（/shop/prepare_progress）: std_msgs/Float32MultiArray
    data = [注文番号, 進捗 0.0〜1.0]

ロボット動作の現在ステップ（/shop/prepare_step, 任意）: std_msgs/String
    "<注文番号> <ステップ名>"   例: "12 受渡し台へ"

スタッフ操作（/shop/queue_command → /shop/queue_command_result）: std_msgs/String
    要求 "<要求ID> <コマンド> <注文番号>"   例: "17 ready 12"
    応答 "<要求ID> ok <メッセージ>" / "<要求ID> ng <メッセージ>"

キュー状態（/shop/queue_status）: std_msgs/String（JSON。Web 画面用の内部データ）
"""
from __future__ import annotations

import json

from .models import Order, OrderItem

RESULT_FAILED = 0
RESULT_SUCCESS = 1
RESULT_CANCELED = 2

COMMANDS = ("cancel", "ready", "unready", "force_start", "inject_marker", "clear_all")


class CodecError(ValueError):
    pass


# --- 注文 -------------------------------------------------------------------
def encode_order(order: Order) -> list[int]:
    data = [int(order.order_number)]
    for it in order.items:
        data += [int(it.product_id), int(it.quantity)]
    return data


def decode_order(data, names: dict[int, str] | None = None) -> Order:
    """Int32MultiArray.data から Order を作る。names があれば商品名を埋める"""
    data = [int(v) for v in data]
    if len(data) < 3 or len(data) % 2 == 0:
        raise CodecError(f"注文の形式が不正です（[注文番号, 商品ID, 個数, ...] の奇数個が必要）: {data}")
    if data[0] < 0:
        raise CodecError(f"注文番号が不正です: {data[0]}")
    names = names or {}
    items = [OrderItem(pid, names.get(pid, ""), qty) for pid, qty in zip(data[1::2], data[2::2])]
    return Order(data[0], items)


# --- ロボット動作の結果・進捗 ---------------------------------------------------
def encode_result(order_number: int, result: int) -> list[int]:
    return [int(order_number), int(result)]


def decode_result(data) -> tuple[int, int]:
    data = [int(v) for v in data]
    if len(data) < 2:
        raise CodecError(f"結果の形式が不正です（[注文番号, 結果] が必要）: {data}")
    return data[0], data[1]


def decode_progress(data) -> tuple[int, float]:
    if len(data) < 2:
        raise CodecError(f"進捗の形式が不正です（[注文番号, 進捗] が必要）: {list(data)}")
    return int(round(data[0])), float(data[1])


def encode_step(order_number: int, label: str) -> str:
    return f"{int(order_number)} {label}"


def decode_step(text: str) -> tuple[int, str]:
    parts = text.split(" ", 1)
    try:
        return int(parts[0]), parts[1] if len(parts) > 1 else ""
    except ValueError:
        raise CodecError(f"ステップの形式が不正です: {text!r}") from None


# --- スタッフ操作 ---------------------------------------------------------------
def encode_command(request_id: int, command: str, order_number: int) -> str:
    return f"{int(request_id)} {command} {int(order_number)}"


def decode_command(text: str) -> tuple[int, str, int]:
    parts = text.split()
    if len(parts) != 3 or parts[1] not in COMMANDS:
        raise CodecError(f"コマンドの形式が不正です: {text!r}")
    try:
        return int(parts[0]), parts[1], int(parts[2])
    except ValueError:
        raise CodecError(f"コマンドの形式が不正です: {text!r}") from None


def encode_command_result(request_id: int, ok: bool, message: str) -> str:
    return f"{int(request_id)} {'ok' if ok else 'ng'} {message}"


def decode_command_result(text: str) -> tuple[int, bool, str]:
    parts = text.split(" ", 2)
    if len(parts) < 2 or parts[1] not in ("ok", "ng"):
        raise CodecError(f"応答の形式が不正です: {text!r}")
    return int(parts[0]), parts[1] == "ok", parts[2] if len(parts) > 2 else ""


# --- キュー状態 ----------------------------------------------------------------
def encode_status(snapshot: dict) -> str:
    return json.dumps(snapshot, ensure_ascii=False, separators=(",", ":"))


def decode_status(text: str) -> dict:
    return json.loads(text)
