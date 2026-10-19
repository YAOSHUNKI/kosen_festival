"""注文受付 Web サーバ（ROS 2 ノード）

  pub  /shop/orders         shop_interfaces/Order       Web 画面で登録した注文を送信
  sub  /shop/queue_status   shop_interfaces/QueueStatus 画面表示用
  cli  /shop/queue_control  shop_interfaces/QueueControl スタッフ操作

ブラウザから http://<このPCのIP>:<port>/ を開く（店頭タブレット・スマホでOK）。
"""
import threading
import time

import rclpy
import uvicorn
from rclpy.executors import MultiThreadedExecutor
from rclpy.node import Node
from shop_interfaces.msg import Order as OrderMsg
from shop_interfaces.msg import QueueStatus
from shop_interfaces.srv import QueueControl

from ..core.models import Order
from ..core.products import ProductStore
from ..web.app import create_app
from .qos import ORDERS_QOS, SRV_CONTROL, STATUS_QOS, TOPIC_ORDERS, TOPIC_STATUS
from .ros_convert import order_to_msg, status_msg_to_snapshot

COMMAND_IDS = {
    "cancel": QueueControl.Request.CANCEL,
    "ready": QueueControl.Request.MARK_READY,
    "unready": QueueControl.Request.UNMARK_READY,
    "force_start": QueueControl.Request.FORCE_START,
    "inject_marker": QueueControl.Request.INJECT_MARKER,
}


class RosBridge(Node):
    mode = "ros"

    def __init__(self):
        super().__init__("order_web_server")
        self.host = self.declare_parameter("host", "0.0.0.0").value
        self.port = self.declare_parameter("port", 8000).value
        self.products_file = self.declare_parameter("products_file", "~/.shop_robot/products.json").value
        self.dict_name = self.declare_parameter("aruco_dictionary", "DICT_4X4_1000").value
        self.status_timeout = self.declare_parameter("status_timeout", 3.0).value
        self.confirm_timeout = self.declare_parameter("confirm_timeout", 2.0).value

        self._cond = threading.Condition()
        self._status = None
        self._status_time = 0.0

        self.pub = self.create_publisher(OrderMsg, TOPIC_ORDERS, ORDERS_QOS)
        self.create_subscription(QueueStatus, TOPIC_STATUS, self._on_status, STATUS_QOS)
        self.cli = self.create_client(QueueControl, SRV_CONTROL)

    def _on_status(self, msg: QueueStatus):
        snap = status_msg_to_snapshot(msg)
        with self._cond:
            self._status, self._status_time = snap, time.monotonic()
            self._cond.notify_all()

    # --- Bridge インタフェース（Web スレッドから呼ばれる） --------------------------------
    def status(self):
        with self._cond:
            if self._status is None or time.monotonic() - self._status_time > self.status_timeout:
                return None
            return self._status

    def submit_order(self, order: Order) -> bool:
        """publish 後、キューノードの状態に注文が現れるまで待って受付を確認する"""
        n = order.order_number
        self.pub.publish(order_to_msg(order))
        self.get_logger().info(f"送信: {n}番 {order.summary()}")

        def arrived():
            s = self._status
            if not s:
                return False
            return (any(o["order_number"] == n for o in s["pending"] + s["ready"])
                    or (s["active"] and s["active"]["order_number"] == n))

        with self._cond:
            return self._cond.wait_for(arrived, timeout=self.confirm_timeout)

    def control(self, command: str, order_number: int):
        if not self.cli.service_is_ready():
            return False, "キューノードに接続できません"
        req = QueueControl.Request(command=COMMAND_IDS[command], order_number=order_number)
        done = threading.Event()
        fut = self.cli.call_async(req)
        fut.add_done_callback(lambda _: done.set())
        if not done.wait(3.0):
            return False, "キューノードから応答がありません"
        res = fut.result()
        return res.success, res.message


def main(args=None):
    rclpy.init(args=args)
    node = RosBridge()
    executor = MultiThreadedExecutor()
    executor.add_node(node)
    threading.Thread(target=executor.spin, daemon=True).start()

    app = create_app(node, ProductStore(node.products_file), node.dict_name)
    node.get_logger().info(f"注文画面: http://{node.host}:{node.port}/  呼び出し表示: /display")
    try:
        uvicorn.run(app, host=node.host, port=node.port, log_level="warning")
    finally:
        executor.shutdown()
        node.destroy_node()
        if rclpy.ok():
            rclpy.shutdown()


if __name__ == "__main__":
    main()
