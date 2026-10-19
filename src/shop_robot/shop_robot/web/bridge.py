"""Web アプリとロボット側をつなぐ層。

・RosBridge（nodes/web_server_node.py）… 実運用。ROS Topic/Service でキューノードとやり取り
・LocalBridge（このファイル）… ROS なしのデモ。キューと「ロボット動作」を同じプロセス内で模擬
"""
from __future__ import annotations

import threading
from typing import Protocol

from ..core.models import Order
from ..core.order_queue import (
    OrderQueue, QueueError, RESULT_CANCELED, RESULT_SUCCESS,
)

COMMANDS = ("cancel", "ready", "unready", "force_start", "inject_marker", "clear_all")


class Bridge(Protocol):
    mode: str

    def submit_order(self, order: Order) -> bool:
        """注文を送信する。ロボット側のキューに入ったことを確認できたら True。"""

    def status(self) -> dict | None:
        """キュー状態（OrderQueue.snapshot() と同じ形）。ロボット側と未接続なら None。"""

    def control(self, command: str, order_number: int) -> tuple[bool, str]:
        """スタッフ操作。command は COMMANDS のいずれか。"""


class LocalBridge:
    """ROS なしで画面と流れを確認するためのデモ用ブリッジ。"""

    mode = "demo"

    def __init__(self, seconds_per_item: float = 2.0):
        self.queue = OrderQueue()
        self.seconds_per_item = seconds_per_item
        self._cancel = threading.Event()

    def submit_order(self, order: Order) -> bool:
        self.queue.add(order)
        return True

    def status(self) -> dict | None:
        return self.queue.snapshot()

    def control(self, command: str, order_number: int) -> tuple[bool, str]:
        q = self.queue
        try:
            if command == "cancel":
                if q.cancel(order_number) == "active":
                    self._cancel.set()
                    return True, f"{order_number}番のロボット動作を中止しています"
            elif command == "ready":
                q.mark_ready(order_number)
            elif command == "unready":
                q.unmark_ready(order_number)
            elif command == "clear_all":
                if q.clear_all():
                    self._cancel.set()
            elif command == "force_start":
                self._start(order_number)
            elif command == "inject_marker":
                ok, msg = q.check_marker(order_number)
                if not ok:
                    q.note(msg, public=True)
                    return False, msg
                self._start(order_number)
            else:
                return False, f"不明な操作: {command}"
        except QueueError as e:
            return False, str(e)
        return True, q.last_event

    def _start(self, order_number: int) -> None:
        order = self.queue.start(order_number)
        self._cancel.clear()
        threading.Thread(target=self._run_robot, args=(order,), daemon=True).start()

    def _run_robot(self, order: Order) -> None:
        """ロボット動作の代わり（1個あたり seconds_per_item 秒）"""
        units = [(it, k) for it in order.items for k in range(1, it.quantity + 1)]
        for i, (it, k) in enumerate(units):
            self.queue.set_progress(i / len(units), f"{it.product_name} {k}/{it.quantity} 個目")
            if self._cancel.wait(self.seconds_per_item):
                self.queue.finish_active(RESULT_CANCELED)
                return
        self.queue.finish_active(RESULT_SUCCESS)
