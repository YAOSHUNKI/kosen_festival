"""core のデータモデル <-> ROS メッセージ の変換"""
from builtin_interfaces.msg import Time
from shop_interfaces.msg import Order as OrderMsg
from shop_interfaces.msg import OrderItem as OrderItemMsg
from shop_interfaces.msg import QueueStatus

from ..core.models import Order, OrderItem


def to_time(t: float) -> Time:
    sec = int(t)
    return Time(sec=sec, nanosec=int((t - sec) * 1e9))


def from_time(t: Time) -> float:
    return t.sec + t.nanosec * 1e-9


def order_to_msg(o: Order) -> OrderMsg:
    return OrderMsg(
        stamp=to_time(o.stamp),
        order_number=o.order_number,
        items=[OrderItemMsg(product_id=i.product_id, product_name=i.product_name, quantity=i.quantity) for i in o.items],
    )


def msg_to_order(m: OrderMsg) -> Order:
    return Order(
        order_number=m.order_number,
        items=[OrderItem(i.product_id, i.product_name, i.quantity) for i in m.items],
        stamp=from_time(m.stamp),
    )


def snapshot_to_msg(snap: dict, now: Time) -> QueueStatus:
    msg = QueueStatus(stamp=now, state=snap["state"])
    if snap["active"]:
        msg.has_active = True
        msg.active = order_to_msg(Order.from_dict(snap["active"]))
    msg.progress = float(snap["progress"])
    msg.progress_text = snap["progress_text"]
    msg.pending = [order_to_msg(Order.from_dict(d)) for d in snap["pending"]]
    msg.ready = [order_to_msg(Order.from_dict(d)) for d in snap["ready"]]
    msg.recently_completed = list(snap["recently_completed"])
    msg.last_event = snap["last_event"]
    msg.public_message = snap["public_message"]
    msg.public_seq = snap["public_seq"]
    return msg


def status_msg_to_snapshot(m: QueueStatus) -> dict:
    """Web アプリ用に OrderQueue.snapshot() と同じ形の dict に戻す"""
    return {
        "state": m.state,
        "active": msg_to_order(m.active).to_dict() if m.has_active else None,
        "progress": m.progress,
        "progress_text": m.progress_text,
        "pending": [msg_to_order(o).to_dict() for o in m.pending],
        "ready": [msg_to_order(o).to_dict() for o in m.ready],
        "recently_completed": list(m.recently_completed),
        "last_event": m.last_event,
        "public_message": m.public_message,
        "public_seq": m.public_seq,
    }
