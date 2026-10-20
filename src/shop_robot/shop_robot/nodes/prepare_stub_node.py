"""ロボット動作ノードの共通部分とスタブ。すべて標準メッセージ型（形式は core/codec.py）

  sub  /shop/prepare_order     std_msgs/Int32MultiArray   [注文番号, 商品ID, 個数, ...]  動作開始
  sub  /shop/prepare_cancel    std_msgs/Int32             注文番号  動作中止
  pub  /shop/prepare_result    std_msgs/Int32MultiArray   [注文番号, 結果]  1=成功 0=失敗 2=取消
  pub  /shop/prepare_progress  std_msgs/Float32MultiArray [注文番号, 進捗0〜1]
  pub  /shop/prepare_step      std_msgs/String            "<注文番号> <ステップ名>"（スタッフ画面の表示用）

実機に置き換えるときは PrepareNodeBase を継承して、
  ・1個ずつ用意する動作なら prepare_unit() を、
  ・注文全体をまとめて扱うなら execute_order() を
書けばよい（キュー側は変更不要）。受取ノード（handover_node.py）は execute_order() 側の例。

    class MyRobot(PrepareNodeBase):
        def __init__(self):
            super().__init__("prepare_robot")

        def prepare_unit(self, item, k) -> bool:
            if item.product_id == 1:
                ...  # たこ焼きを1個つかんで渡す
            return True   # 失敗したら False（注文は呼び出し中に戻り、スタッフが再開できる）

PrepareNodeBase を使わず自前で書く場合（ROS1 側など）も、上のトピックだけ守ればよい。
"""
import threading
import time

import rclpy
from rclpy.executors import MultiThreadedExecutor
from rclpy.node import Node
from std_msgs.msg import Float32MultiArray, Int32, Int32MultiArray, String

from ..core import codec
from .qos import (
    RELIABLE_QOS, TOPIC_PREPARE, TOPIC_PREPARE_CANCEL, TOPIC_PREPARE_PROGRESS, TOPIC_PREPARE_RESULT,
    TOPIC_PREPARE_STEP,
)


class PrepareNodeBase(Node):
    def __init__(self, node_name: str):
        super().__init__(node_name)
        self._lock = threading.Lock()
        self._current = None        # 動作中の注文番号
        self._cancel = threading.Event()
        self.result_pub = self.create_publisher(Int32MultiArray, TOPIC_PREPARE_RESULT, RELIABLE_QOS)
        self.progress_pub = self.create_publisher(Float32MultiArray, TOPIC_PREPARE_PROGRESS, 10)
        self.step_pub = self.create_publisher(String, TOPIC_PREPARE_STEP, 10)
        self.create_subscription(Int32MultiArray, TOPIC_PREPARE, self._on_order, RELIABLE_QOS)
        self.create_subscription(Int32, TOPIC_PREPARE_CANCEL, self._on_cancel, RELIABLE_QOS)

    # --- 受信 ---------------------------------------------------------------
    def _on_order(self, msg: Int32MultiArray):
        try:
            order = codec.decode_order(msg.data)
        except codec.CodecError as e:
            self.get_logger().error(str(e))
            return
        n = order.order_number
        accepted = False
        with self._lock:
            if self._current == n:
                return  # 同じ依頼の重複
            if self._current is not None:
                reason = f"{self._current}番の動作中"
            else:
                accepted, reason = self.can_accept()
                if accepted:
                    self._current = n
                    self._cancel.clear()
        if not accepted:
            # 受け付けられない → すぐ失敗を返す（キュー側で注文は呼び出し中に戻る）
            self.get_logger().error(f"{n}番を受け付けられません: {reason}")
            self.publish_result(n, codec.RESULT_FAILED)
            return
        threading.Thread(target=self._run, args=(order,), daemon=True).start()

    def _on_cancel(self, msg: Int32):
        if msg.data == self._current:
            self._cancel.set()

    # --- 実行 ---------------------------------------------------------------
    @property
    def is_cancel_requested(self) -> bool:
        """長い動作の途中で見て、早めに切り上げると良い"""
        return self._cancel.is_set()

    def _run(self, order):
        n = order.order_number
        try:
            result = self.execute_order(order)
        except Exception as e:  # noqa: BLE001  動作コードの例外でキューが止まらないようにする
            self.get_logger().error(f"{n}番の動作中に例外: {e}")
            result = codec.RESULT_FAILED
        if result == codec.RESULT_SUCCESS:
            self.publish_progress(n, 1.0)
        with self._lock:
            self._current = None
        self.publish_result(n, result)
        self.get_logger().info(f"動作終了: {n}番 結果={result}")

    def publish_result(self, n: int, result: int):
        self.result_pub.publish(Int32MultiArray(data=codec.encode_result(n, result)))

    def publish_progress(self, n: int, p: float):
        self.progress_pub.publish(Float32MultiArray(data=[float(n), float(p)]))

    def publish_step(self, n: int, label: str):
        self.step_pub.publish(String(data=codec.encode_step(n, label)))

    # --- 実機ではここを実装する ---------------------------------------------------
    def can_accept(self) -> tuple[bool, str]:
        """新しい注文を受け付けられるか（非常停止中など）"""
        return True, ""

    def execute_order(self, order) -> int:
        """注文1件の動作。codec.RESULT_SUCCESS / RESULT_FAILED / RESULT_CANCELED を返す。
        既定では商品を1個ずつ prepare_unit() に渡す。"""
        n = order.order_number
        units = [(it, k) for it in order.items for k in range(1, it.quantity + 1)]
        self.get_logger().info(f"動作開始: {n}番（{len(units)}個）")
        for i, (item, k) in enumerate(units):
            if self.is_cancel_requested:
                break
            self.publish_progress(n, i / len(units))
            self.publish_step(n, f"商品ID {item.product_id} {k}/{item.quantity} 個目")
            if not self.prepare_unit(item, k):
                return codec.RESULT_FAILED
        if self.is_cancel_requested:
            self.on_canceled(order)
            return codec.RESULT_CANCELED
        return codec.RESULT_SUCCESS

    def prepare_unit(self, item, k: int) -> bool:
        """item（product_id, quantity を持つ）を1個用意する。k は何個目か（1始まり）。成功なら True。"""
        raise NotImplementedError

    def on_canceled(self, order):
        """取消時の後始末（アームを初期姿勢に戻す等）"""
        self.get_logger().info(f"取消: {order.order_number}番")


class PrepareStub(PrepareNodeBase):
    """動作の代わりに待つだけのスタブ"""

    def __init__(self):
        super().__init__("prepare_stub")
        self.sec = self.declare_parameter("seconds_per_item", 3.0).value
        self.fail_product_id = self.declare_parameter("fail_product_id", -1).value  # 失敗時の動作確認用

    def prepare_unit(self, item, k) -> bool:
        self.get_logger().info(f"  [stub] 商品ID {item.product_id} {k}/{item.quantity}")
        t_end = time.monotonic() + self.sec
        while time.monotonic() < t_end:
            if self.is_cancel_requested:
                return True  # 呼び出し元で取消処理
            time.sleep(0.05)
        return item.product_id != self.fail_product_id


def run_node(node):
    executor = MultiThreadedExecutor()
    executor.add_node(node)
    try:
        executor.spin()
    except KeyboardInterrupt:
        pass
    finally:
        node.destroy_node()
        if rclpy.ok():
            rclpy.shutdown()


def main(args=None):
    rclpy.init(args=args)
    run_node(PrepareStub())


if __name__ == "__main__":
    main()
