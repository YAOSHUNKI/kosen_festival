"""ロボットとやり取りするトピック（形式は core/codec.py）"""
from rclpy.qos import DurabilityPolicy, HistoryPolicy, QoSProfile, ReliabilityPolicy

# 取りこぼさない。volatile にしておかないとノード再起動時に古い依頼が再送される
RELIABLE_QOS = QoSProfile(
    history=HistoryPolicy.KEEP_LAST, depth=10,
    reliability=ReliabilityPolicy.RELIABLE, durability=DurabilityPolicy.VOLATILE,
)

TOPIC_PREPARE = "shop/prepare_order"   # std_msgs/Int32MultiArray [商品ID, 個数, ...]  システム → ロボット
TOPIC_DONE = "shop/prepare_done"       # std_msgs/Bool  true=成功 false=失敗         ロボット → システム
