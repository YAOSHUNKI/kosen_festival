"""ArUco マーカー検出ノード

  pub  /shop/marker_id                  std_msgs/Int32             確定したマーカーID（= 注文番号）
  pub  /shop/camera/image/compressed    sensor_msgs/CompressedImage 確認用プレビュー（JPEG・検出結果入り）
  pub  /shop/aruco/debug_image          sensor_msgs/Image          検出結果入りの生画像（publish_debug_image=true のとき）
  sub  /shop/queue_status               shop_interfaces/QueueStatus プレビューの枠の色分け用
  sub  <image_topic>                    sensor_msgs/Image          camera_device < 0 のときのみ

camera_device >= 0 なら OpenCV で USB カメラを直接開く（受付に置いた Web カメラ向け）。
ロボットのカメラなど既存の画像トピックを使う場合は camera_device:=-1 と image_topic を指定する。

プレビューの枠の色: 緑 = 呼び出し中（受け付ける） / 橙 = 準備中・動作中 / 赤 = 受付中にない番号
"""
import time

import rclpy
from rclpy.node import Node
from rclpy.qos import qos_profile_sensor_data
from sensor_msgs.msg import CompressedImage, Image
from shop_interfaces.msg import QueueStatus
from std_msgs.msg import Int32

from ..core.aruco import MarkerDebouncer, MarkerDetector
from ..core.camera_preview import annotate, encode_jpeg, queue_sets
from .qos import STATUS_QOS, TOPIC_CAMERA, TOPIC_MARKER, TOPIC_STATUS
from .ros_convert import status_msg_to_snapshot


class ArucoDetectorNode(Node):
    def __init__(self):
        super().__init__("aruco_detector")
        dict_name = self.declare_parameter("aruco_dictionary", "DICT_4X4_1000").value
        device = self.declare_parameter("camera_device", 0).value
        image_topic = self.declare_parameter("image_topic", "/image_raw").value
        fps = self.declare_parameter("frame_rate", 15.0).value
        width = self.declare_parameter("camera_width", 640).value
        height = self.declare_parameter("camera_height", 480).value
        min_frames = self.declare_parameter("min_frames", 5).value
        cooldown = self.declare_parameter("cooldown_sec", 3.0).value
        self.publish_preview = self.declare_parameter("publish_preview", True).value
        preview_rate = self.declare_parameter("preview_rate", 10.0).value
        self.preview_width = self.declare_parameter("preview_width", 640).value
        self.preview_quality = self.declare_parameter("preview_quality", 70).value
        self.preview_mirror = self.declare_parameter("preview_mirror", True).value
        self.publish_debug = self.declare_parameter("publish_debug_image", False).value
        self.show_window = self.declare_parameter("show_window", False).value

        self.detector = MarkerDetector(dict_name)
        self.debouncer = MarkerDebouncer(min_frames, cooldown)
        self.pub = self.create_publisher(Int32, TOPIC_MARKER, 10)
        self.preview_pub = self.create_publisher(CompressedImage, TOPIC_CAMERA, qos_profile_sensor_data)
        self.preview_period = 1.0 / max(preview_rate, 0.1)
        self._last_preview = 0.0
        self.ready, self.waiting = set(), set()
        self.create_subscription(QueueStatus, TOPIC_STATUS, self.on_status, STATUS_QOS)

        self.debug_pub = self.create_publisher(Image, "shop/aruco/debug_image", 1) if self.publish_debug else None
        self.bridge = None
        if self.publish_debug or device < 0:
            from cv_bridge import CvBridge
            self.bridge = CvBridge()

        self.cap = None
        if device >= 0:
            import cv2
            self.cap = cv2.VideoCapture(device)
            self.cap.set(cv2.CAP_PROP_FRAME_WIDTH, width)
            self.cap.set(cv2.CAP_PROP_FRAME_HEIGHT, height)
            if not self.cap.isOpened():
                raise RuntimeError(f"カメラ /dev/video{device} を開けません")
            self.create_timer(1.0 / fps, self.grab)
            self.get_logger().info(f"カメラ {device} からマーカーを検出します（{dict_name}）")
        else:
            self.create_subscription(Image, image_topic, self.on_image, qos_profile_sensor_data)
            self.get_logger().info(f"{image_topic} からマーカーを検出します（{dict_name}）")

    def on_status(self, msg: QueueStatus):
        self.ready, self.waiting = queue_sets(status_msg_to_snapshot(msg))

    def grab(self):
        ok, frame = self.cap.read()
        if not ok:
            self.get_logger().warn("カメラ画像を取得できません", throttle_duration_sec=5.0)
            return
        self.process(frame)

    def on_image(self, msg: Image):
        self.process(self.bridge.imgmsg_to_cv2(msg, desired_encoding="bgr8"))

    def process(self, frame):
        ids, corners = self.detector.detect(frame)
        for marker_id in self.debouncer.update(ids):
            self.get_logger().info(f"マーカー {marker_id} を確定")
            self.pub.publish(Int32(data=marker_id))

        now = time.monotonic()
        if self.publish_preview and now - self._last_preview >= self.preview_period:
            self._last_preview = now
            vis = annotate(frame, ids, corners, self.ready, self.waiting, mirror=self.preview_mirror)
            msg = CompressedImage(format="jpeg", data=encode_jpeg(vis, self.preview_width, self.preview_quality))
            msg.header.stamp = self.get_clock().now().to_msg()
            self.preview_pub.publish(msg)

        if self.debug_pub is not None or self.show_window:
            vis = annotate(frame, ids, corners, self.ready, self.waiting)
            if self.debug_pub is not None:
                self.debug_pub.publish(self.bridge.cv2_to_imgmsg(vis, encoding="bgr8"))
            if self.show_window:
                import cv2
                cv2.imshow("aruco_detector", vis)
                cv2.waitKey(1)

    def destroy_node(self):
        if self.cap is not None:
            self.cap.release()
        super().destroy_node()


def main(args=None):
    rclpy.init(args=args)
    node = ArucoDetectorNode()
    try:
        rclpy.spin(node)
    except KeyboardInterrupt:
        pass
    finally:
        node.destroy_node()
        if rclpy.ok():
            rclpy.shutdown()


if __name__ == "__main__":
    main()
