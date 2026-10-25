"""店舗システム本体（ROS 2 ノード）: 注文 Web サーバ + 待ち行列 + AR マーカー読み取り + カメラ映像

外とやり取りする ROS トピックはロボットとの2本だけ（形式は core/codec.py）:

  pub  /shop/prepare_order  std_msgs/Int32MultiArray  [商品ID, 個数, 商品ID, 個数, ...]
                            呼び出し中の番号札（AR マーカー）が読まれたときだけ publish。
                            登録されている全商品を ID の小さい順に入れる（注文されていない商品は個数 0）
  sub  /shop/prepare_done   std_msgs/Bool             ロボットの完了通知（true=成功 / false=失敗）

  sub  <image_topic>        sensor_msgs/Image         camera_device < 0 のときだけ（ロボットのカメラを使う場合）

ブラウザから http://<このPCのIP>:<port>/ を開く（店頭タブレット・スマホでOK）。呼び出し表示は /display。
"""
import threading

import rclpy
import uvicorn
from rclpy.executors import MultiThreadedExecutor
from rclpy.node import Node
from rclpy.qos import qos_profile_sensor_data
from std_msgs.msg import Bool, Int32MultiArray

from ..core import codec
from ..core.models import Order
from ..core.products import ProductStore
from ..web.app import create_app
from ..web.bridge import ShopBridge
from .qos import RELIABLE_QOS, TOPIC_DONE, TOPIC_PREPARE


class RosRobot:
    """ShopBridge の Robot を ROS トピックで実装する"""

    def __init__(self, node: Node, products: ProductStore):
        self.node = node
        self.products = products
        self.pub = node.create_publisher(Int32MultiArray, TOPIC_PREPARE, RELIABLE_QOS)

    def available(self) -> tuple[bool, str]:
        if self.node.count_subscribers(TOPIC_PREPARE) == 0:
            return False, f"ロボット側（/{TOPIC_PREPARE} の購読者）が起動していません"
        return True, ""

    def send(self, order: Order) -> None:
        # 登録されている全商品を入れる（注文されていない商品は個数 0）
        data = codec.encode_items(order, {p.send_id for p in self.products.list()})
        self.pub.publish(Int32MultiArray(data=data))
        self.node.get_logger().info(f"{order.order_number}番 → /{TOPIC_PREPARE} {data}")

    def cancel(self) -> None:
        self.node.get_logger().warn("取消: ロボットに中止を伝えるトピックはないため、システム側だけ取消扱いにします")


class ShopNode(Node):
    def __init__(self):
        super().__init__("shop_server")
        p = lambda name, default: self.declare_parameter(name, default).value  # noqa: E731
        self.host = p("host", "0.0.0.0")
        self.port = p("port", 8000)
        self.products_file = p("products_file", "~/.shop_robot/products.json")
        self.dict_name = p("aruco_dictionary", "DICT_4X4_1000")
        state_file = p("state_file", "~/.shop_robot/queue_state.json")
        device = p("camera_device", 0)
        image_topic = p("image_topic", "/image_raw")
        fps = p("frame_rate", 15.0)
        width, height = p("camera_width", 640), p("camera_height", 480)

        self.products = ProductStore(self.products_file)   # Web 画面と共有（画面で追加した商品もすぐ反映）
        self.bridge = ShopBridge(
            RosRobot(self, self.products), state_file=state_file, dict_name=self.dict_name,
            mirror=p("preview_mirror", True), min_frames=p("min_frames", 5), cooldown_s=p("cooldown_sec", 3.0),
            log=lambda s: self.get_logger().info(s))
        self.create_subscription(Bool, TOPIC_DONE, lambda m: self.bridge.robot_done(bool(m.data)), RELIABLE_QOS)

        if device >= 0:
            try:
                self.bridge.start_camera(device, fps, width, height)
                self.get_logger().info(f"カメラ {device} から AR マーカーを読み取ります（{self.dict_name}）")
            except RuntimeError as e:
                self.get_logger().error(f"{e}。カメラなしで続けます（画面の「動作確認」で読み取りを再現できます）")
        else:
            from cv_bridge import CvBridge
            from sensor_msgs.msg import Image
            cvb = CvBridge()
            self.create_subscription(
                Image, image_topic, lambda m: self.bridge.feed_frame(cvb.imgmsg_to_cv2(m, desired_encoding="bgr8")),
                qos_profile_sensor_data)
            self.get_logger().info(f"{image_topic} から AR マーカーを読み取ります（{self.dict_name}）")


def main(args=None):
    rclpy.init(args=args)
    node = ShopNode()
    executor = MultiThreadedExecutor()
    executor.add_node(node)
    threading.Thread(target=executor.spin, daemon=True).start()

    app = create_app(node.bridge, node.products, node.dict_name)
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
