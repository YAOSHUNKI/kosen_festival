"""ロボット側ノードの共通部分とスタブ（形式は core/codec.py）

  sub  /shop/prepare_order  std_msgs/Int32MultiArray  [商品ID, 個数, 商品ID, 個数, ...]  動作開始
  pub  /shop/prepare_done   std_msgs/Bool             終わったら true（成功）/ false（失敗）

実機に置き換えるときは PrepareNodeBase を継承して、
  ・1個ずつ用意する動作なら prepare_unit() を、
  ・注文全体をまとめて扱うなら execute_order() を
書けばよい。受取ノード（handover_node.py）は execute_order() 側の例。

    class MyRobot(PrepareNodeBase):
        def __init__(self):
            super().__init__("prepare_robot")

        def prepare_unit(self, item, k) -> bool:
            if item.product_id == 1:
                ...  # たこ焼きを1個つかんで渡す
            return True   # 失敗したら False（注文は呼び出し中に戻り、スタッフが再開できる）

PrepareNodeBase を使わず自前で書く場合（ROS1 側・C++ など）も、上の2トピックだけ守ればよい。
"""
import threading
import time

import rclpy
from rclpy.executors import MultiThreadedExecutor
from rclpy.node import Node
from std_msgs.msg import Bool, Int32MultiArray

from ..core import codec
from ..core.models import Order
from .qos import RELIABLE_QOS, TOPIC_DONE, TOPIC_PREPARE


class PrepareNodeBase(Node):
    def __init__(self, node_name: str):
        super().__init__(node_name)
        self._busy_lock = threading.Lock()
        self._busy = False
        self._count = 0   # 受け取った依頼の通し番号（ログ用。注文番号はトピックに含まれない）
        self.done_pub = self.create_publisher(Bool, TOPIC_DONE, RELIABLE_QOS)
        self.create_subscription(Int32MultiArray, TOPIC_PREPARE, self._on_order, RELIABLE_QOS)

    def _on_order(self, msg: Int32MultiArray):
        try:
            items = codec.decode_items(msg.data)
        except codec.CodecError as e:
            self.get_logger().error(str(e))
            self.publish_done(False)
            return
        accepted = False
        with self._busy_lock:
            if self._busy:
                reason = "動作中"
            else:
                accepted, reason = self.can_accept()
                if accepted:
                    self._busy = True
        if not accepted:
            # 受け付けられない → すぐ失敗を返す（システム側で注文は呼び出し中に戻る）
            self.get_logger().error(f"依頼 {list(msg.data)} を受け付けられません: {reason}")
            self.publish_done(False)
            return
        self._count += 1
        order = Order(self._count, items)
        threading.Thread(target=self._run, args=(order,), daemon=True).start()

    def _run(self, order: Order):
        try:
            success = bool(self.execute_order(order))
        except Exception as e:  # noqa: BLE001  動作コードの例外でシステムが止まらないようにする
            self.get_logger().error(f"動作中に例外: {e}")
            success = False
        with self._busy_lock:
            self._busy = False
        self.publish_done(success)
        self.get_logger().info(f"動作終了（依頼 #{order.order_number}）: {'成功' if success else '失敗'}")

    def publish_done(self, success: bool):
        self.done_pub.publish(Bool(data=success))

    # --- 実機ではここを実装する ---------------------------------------------------
    def can_accept(self) -> tuple[bool, str]:
        """新しい依頼を受け付けられるか（非常停止中など）。受け付けられるときは (True, "")"""
        return True, ""

    def execute_order(self, order: Order) -> bool:
        """依頼1件の動作。成功なら True。既定では商品を1個ずつ prepare_unit() に渡す。
        order.items の各要素は product_id と quantity を持つ（order.order_number はログ用の通し番号）。"""
        units = [(it, k) for it in order.items for k in range(1, it.quantity + 1)]
        self.get_logger().info(f"動作開始（依頼 #{order.order_number}）: {codec.encode_items(order)}（{len(units)}個）")
        return all(self.prepare_unit(item, k) for item, k in units)

    def prepare_unit(self, item, k: int) -> bool:
        """item（product_id, quantity を持つ）を1個用意する。k は何個目か（1始まり）。成功なら True。"""
        raise NotImplementedError


class PrepareStub(PrepareNodeBase):
    """動作の代わりに待つだけのスタブ"""

    def __init__(self):
        super().__init__("prepare_stub")
        self.sec = self.declare_parameter("seconds_per_item", 3.0).value
        self.fail_product_id = self.declare_parameter("fail_product_id", -1).value  # 失敗時の動作確認用

    def prepare_unit(self, item, k) -> bool:
        self.get_logger().info(f"  [stub] 商品ID {item.product_id} {k}/{item.quantity}")
        time.sleep(self.sec)
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
