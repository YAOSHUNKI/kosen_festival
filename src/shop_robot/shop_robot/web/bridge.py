"""Web アプリの裏側（待ち行列・マーカー読み取り・カメラ映像・ロボットへの依頼）。

待ち行列とマーカー検出は Web サーバと同じプロセスで動かし、外とやり取りするのはロボットだけ。
ロボットとのやり取りは Robot として差し替えられる:

  ・RosRobot（nodes/shop_node.py）… 実運用。/shop/prepare_order に配列を publish、/shop/prepare_done で完了を受け取る
  ・SimRobot（このファイル）       … ROS なしのデモ。一定時間待って完了を返す
"""
from __future__ import annotations

import threading
import time
from typing import Callable, Protocol

from ..core.aruco import MarkerDebouncer, MarkerDetector
from ..core.camera_preview import annotate, encode_jpeg, queue_sets
from ..core.models import Order
from ..core.order_queue import (
    OrderQueue, QueueError, RESULT_CANCELED, RESULT_FAILED, RESULT_SUCCESS,
)


COMMANDS = ("cancel", "ready", "unready", "force_start", "inject_marker", "clear_all")  # Web API で受け付ける操作


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


class Robot(Protocol):
    def available(self) -> tuple[bool, str]:
        """依頼を受け取れる状態か（ROS ならロボット側の購読者がいるか）"""

    def send(self, order: Order) -> None:
        """動作を依頼する。終わったら ShopBridge.robot_done(success) を呼んでもらう"""

    def cancel(self) -> None:
        """取消（ROS 版はロボットに中止を伝える手段がないので何もしない）"""


class ShopBridge:
    """待ち行列 + マーカー読み取り + カメラ映像 + ロボットへの依頼"""

    mode = "ros"

    def __init__(self, robot: Robot | None = None, state_file: str | None = None,
                 dict_name: str = "DICT_4X4_1000", mirror: bool = True,
                 min_frames: int = 5, cooldown_s: float = 3.0, log: Callable[[str], None] | None = None):
        self.queue = OrderQueue(state_file)
        self.robot = robot
        self.camera = FrameBuffer()
        self._detector = MarkerDetector(dict_name)
        self._debouncer = MarkerDebouncer(min_frames, cooldown_s)
        self._mirror = mirror
        self._lock = threading.RLock()   # Web・カメラ・ROS の各スレッドから呼ばれる
        self._log = log or (lambda s: None)

    # --- Web アプリから -----------------------------------------------------------
    def submit_order(self, order: Order) -> bool:
        self.queue.add(order)
        return True

    def status(self) -> dict | None:
        return self.queue.snapshot()

    def control(self, command: str, order_number: int) -> tuple[bool, str]:
        q = self.queue
        with self._lock:
            try:
                if command == "cancel":
                    if q.cancel(order_number) == "active":
                        self._cancel_active()
                        return True, f"{order_number}番のロボット動作を取り消し扱いにしました"
                elif command == "ready":
                    q.mark_ready(order_number)
                elif command == "unready":
                    q.unmark_ready(order_number)
                elif command == "clear_all":
                    if q.clear_all():
                        self._cancel_active()
                elif command == "force_start":
                    self._start(order_number)
                elif command == "inject_marker":
                    return self.on_marker(order_number)
                else:
                    return False, f"不明な操作: {command}"
            except QueueError as e:
                return False, str(e)
            return True, q.last_event

    # --- マーカー -----------------------------------------------------------------
    def on_marker(self, marker_id: int) -> tuple[bool, str]:
        """マーカーを読んだ。呼び出し中の番号ならロボットに依頼する"""
        with self._lock:
            ok, reason = self.queue.check_marker(marker_id)
            if not ok:
                self.queue.note(reason, public=True)
                self._log(f"マーカー {marker_id}: {reason}")
                return False, reason
            try:
                self._start(marker_id)
            except QueueError as e:
                return False, str(e)
            return True, self.queue.last_event

    def feed_frame(self, frame) -> list[int]:
        """カメラ画像1枚を処理する（マーカー検出 → 確定したら on_marker、プレビュー更新）"""
        ids, corners = self._detector.detect(frame)
        fired = self._debouncer.update(ids)
        for marker_id in fired:
            self._log(f"マーカー {marker_id} を確定")
            self.on_marker(marker_id)
        ready, waiting = queue_sets(self.queue.snapshot())
        self.camera.put(encode_jpeg(annotate(frame, ids, corners, ready, waiting, mirror=self._mirror)))
        return fired

    def start_camera(self, device, fps: float = 15.0, width: int = 640, height: int = 480) -> None:
        """USB カメラ（番号）または動画ファイルを別スレッドで読み続ける"""
        import cv2
        cap = cv2.VideoCapture(device)
        cap.set(cv2.CAP_PROP_FRAME_WIDTH, width)
        cap.set(cv2.CAP_PROP_FRAME_HEIGHT, height)
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
                try:
                    self.feed_frame(frame)
                except Exception as e:  # noqa: BLE001  1フレームの失敗でカメラを止めない
                    self._log(f"カメラ画像の処理に失敗: {e}")
                time.sleep(1.0 / fps)

        threading.Thread(target=loop, daemon=True).start()

    # --- ロボット -----------------------------------------------------------------
    def _start(self, order_number: int) -> None:
        if self.robot is None:
            raise QueueError("ロボットが設定されていません")
        ok, reason = self.robot.available()
        if not ok:
            self.queue.note("ただいまロボットを準備しています。スタッフにお声がけください", public=True)
            self.queue.note(reason)
            raise QueueError(reason)
        order = self.queue.start(order_number)
        self.queue.set_progress(0.0, "ロボット動作中（完了待ち）")
        self.robot.send(order)
        self._log(f"{order_number}番をロボットに依頼しました")

    def _cancel_active(self) -> None:
        self.robot.cancel()
        self.queue.finish_active(RESULT_CANCELED)

    def robot_done(self, success: bool) -> None:
        """ロボットから完了が届いた"""
        with self._lock:
            if self.queue.active is None:
                self._log("動作中の注文がないのに完了が届いたので無視しました（取消済みの動作など）")
                return
            n = self.queue.active.order_number
            if success:
                self.queue.finish_active(RESULT_SUCCESS)
            else:
                self.queue.finish_active(RESULT_FAILED, "ロボットが失敗を報告")
            self._log(f"{n}番: {self.queue.last_event}")


class SimRobot:
    """デモ用のロボット（1個あたり seconds_per_item 秒で完了を返す）"""

    def __init__(self, seconds_per_item: float = 2.0):
        self.seconds_per_item = seconds_per_item
        self.bridge: ShopBridge | None = None
        self._token = 0

    def available(self) -> tuple[bool, str]:
        return True, ""

    def send(self, order: Order) -> None:
        self._token += 1
        token = self._token
        units = [(it, k) for it in order.items for k in range(1, it.quantity + 1)]

        def run():
            for i, (it, k) in enumerate(units):
                if token != self._token:
                    return
                self.bridge.queue.set_progress(i / len(units), f"{it.product_name} {k}/{it.quantity} 個目")
                time.sleep(self.seconds_per_item)
            if token == self._token:
                self.bridge.robot_done(True)

        threading.Thread(target=run, daemon=True).start()

    def cancel(self) -> None:
        self._token += 1   # 動作中のスレッドは完了を返さずに終わる


class LocalBridge(ShopBridge):
    """ROS なしで画面と流れを確認するためのデモ用（SimRobot つき）"""

    mode = "demo"

    def __init__(self, seconds_per_item: float = 2.0, dict_name: str = "DICT_4X4_1000",
                 mirror: bool = True):
        robot = SimRobot(seconds_per_item)
        super().__init__(robot, dict_name=dict_name, mirror=mirror)
        robot.bridge = self
