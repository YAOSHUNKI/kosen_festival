"""ArUco マーカー検出ノード

  pub  /shop/marker_id            std_msgs/Int32    確定したマーカーID（= 注文番号）
  pub  /shop/aruco/debug_image    sensor_msgs/Image 検出結果を描いた画像（publish_debug_image=true のとき）
  sub  <image_topic>              sensor_msgs/Image camera_device < 0 のときのみ

camera_device >= 0 なら OpenCV で USB カメラを直接開く（受付に置いた Web カメラ向け）。
ロボットのカメラなど既存の画像トピックを使う場合は camera_device:=-1 と image_topic を指定する。
"""
import rclpy
from rclpy.node import Node
from rclpy.qos import qos_profile_sensor_data
from sensor_msgs.msg import Image
from std_msgs.msg import Int32

from ..core.aruco import MarkerDebouncer, MarkerDetector
from .qos import TOPIC_MARKER


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
        self.publish_debug = self.declare_parameter("publish_debug_image", False).value
        self.show_window = self.declare_parameter("show_window", False).value

        self.detector = MarkerDetector(dict_name)
        self.debouncer = MarkerDebouncer(min_frames, cooldown)
        self.pub = self.create_publisher(Int32, TOPIC_MARKER, 10)
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
        if self.debug_pub is not None or self.show_window:
            vis = MarkerDetector.draw(frame, ids, corners)
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
