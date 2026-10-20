"""Web アプリとロボット側をつなぐ層。

・RosBridge（nodes/web_server_node.py）… 実運用。ROS Topic/Service でキューノードとやり取り
・LocalBridge（このファイル）… ROS なしのデモ。キューと「ロボット動作」を同じプロセス内で模擬
"""
from __future__ import annotations

import threading
import time
from typing import Protocol

from ..core.aruco import MarkerDebouncer, MarkerDetector
from ..core.camera_preview import annotate, encode_jpeg, queue_sets
from ..core.models import Order
from ..core.order_queue import (
    OrderQueue, QueueError, RESULT_CANCELED, RESULT_SUCCESS,
)



class FrameBuffer:
    """カメラのプレビュー（JPEG）の最新1枚を保持する"""

    def __init__(self, timeout: float = 3.0):
        self.timeout = timeout
        self._lock = threading.Lock()
        self._jpeg: bytes | None = None
        self._seq = 0
        self._time = 0.0

    def put(self, jpeg: bytes) -> None:
        with self._lock:
            self._jpeg, self._seq, self._time = jpeg, self._seq + 1, time.monotonic()

    def get(self) -> tuple[bytes, int] | None:
        """(JPEG, 通し番号)。timeout 秒以上届いていなければ None"""
        with self._lock:
            if self._jpeg is None or time.monotonic() - self._time > self.timeout:
                return None
            return self._jpeg, self._seq


class Bridge(Protocol):
    mode: str
    camera: FrameBuffer

    def submit_order(self, order: Order) -> bool:
        """注文を送信する。ロボット側のキューに入ったことを確認できたら True。"""

    def status(self) -> dict | None:
        """キュー状態（OrderQueue.snapshot() と同じ形）。ロボット側と未接続なら None。"""

    def control(self, command: str, order_number: int) -> tuple[bool, str]:
        """スタッフ操作。command は COMMANDS のいずれか。"""


class LocalBridge:
    """ROS なしで画面と流れを確認するためのデモ用ブリッジ。"""

    mode = "demo"

    def __init__(self, seconds_per_item: float = 2.0, dict_name: str = "DICT_4X4_1000",
                 mirror: bool = True):
        self.queue = OrderQueue()
        self.seconds_per_item = seconds_per_item
        self._cancel = threading.Event()
        self.camera = FrameBuffer()
        self._detector = MarkerDetector(dict_name)
        self._debouncer = MarkerDebouncer()
        self._mirror = mirror

    # --- カメラ（デモ: PC の Web カメラでマーカー読み取りまで試せる） -------------------
    def feed_frame(self, frame) -> list[int]:
        """カメラ画像1枚を処理する。確定したマーカーIDを返す（ROS 版の aruco_detector と同じ動き）"""
        ids, corners = self._detector.detect(frame)
        fired = self._debouncer.update(ids)
        for marker_id in fired:
            self.control("inject_marker", marker_id)
        ready, waiting = queue_sets(self.queue.snapshot())
        self.camera.put(encode_jpeg(annotate(frame, ids, corners, ready, waiting, mirror=self._mirror)))
        return fired

    def start_camera(self, device, fps: float = 15.0) -> None:
        import cv2
        cap = cv2.VideoCapture(device)
        if not cap.isOpened():
            raise RuntimeError(f"カメラ {device} を開けません")

        def loop():
            while True:
                ok, frame = cap.read()
                if not ok:
                    if isinstance(device, str):  # 動画ファイルなら先頭に戻してループ
                        cap.set(cv2.CAP_PROP_POS_FRAMES, 0)
                    time.sleep(0.5)
                    continue
                self.feed_frame(frame)
                time.sleep(1.0 / fps)

        threading.Thread(target=loop, daemon=True).start()

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
