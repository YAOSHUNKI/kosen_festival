"""トピック名と QoS（すべて標準メッセージ型。形式は core/codec.py を参照）"""
from rclpy.qos import DurabilityPolicy, HistoryPolicy, QoSProfile, ReliabilityPolicy

# 取りこぼさない。volatile にしておかないとノード再起動時に古い注文・依頼が再送される
RELIABLE_QOS = QoSProfile(
    history=HistoryPolicy.KEEP_LAST, depth=50,
    reliability=ReliabilityPolicy.RELIABLE, durability=DurabilityPolicy.VOLATILE,
)
ORDERS_QOS = RELIABLE_QOS

# キュー状態: 後から起動した Web サーバもすぐ最新状態を受け取れるよう transient_local
STATUS_QOS = QoSProfile(
    history=HistoryPolicy.KEEP_LAST, depth=1,
    reliability=ReliabilityPolicy.RELIABLE, durability=DurabilityPolicy.TRANSIENT_LOCAL,
)

TOPIC_ORDERS = "shop/orders"                      # std_msgs/Int32MultiArray  Web → キュー
TOPIC_STATUS = "shop/queue_status"                # std_msgs/String (JSON)    キュー → Web
TOPIC_COMMAND = "shop/queue_command"              # std_msgs/String           Web → キュー
TOPIC_COMMAND_RESULT = "shop/queue_command_result"  # std_msgs/String         キュー → Web
TOPIC_MARKER = "shop/marker_id"                   # std_msgs/Int32            検出 → キュー
TOPIC_PREPARE = "shop/prepare_order"              # std_msgs/Int32MultiArray  キュー → ロボット
TOPIC_PREPARE_CANCEL = "shop/prepare_cancel"      # std_msgs/Int32            キュー → ロボット
TOPIC_PREPARE_RESULT = "shop/prepare_result"      # std_msgs/Int32MultiArray  ロボット → キュー
TOPIC_PREPARE_PROGRESS = "shop/prepare_progress"  # std_msgs/Float32MultiArray ロボット → キュー
TOPIC_PREPARE_STEP = "shop/prepare_step"          # std_msgs/String           ロボット → キュー（任意）
TOPIC_CAMERA = "shop/camera/image/compressed"     # sensor_msgs/CompressedImage 検出 → Web
