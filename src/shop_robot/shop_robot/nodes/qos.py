from rclpy.qos import DurabilityPolicy, HistoryPolicy, QoSProfile, ReliabilityPolicy

# 注文: 取りこぼさない。volatile にしておかないとキューノード再起動時に古い注文が再送される
ORDERS_QOS = QoSProfile(
    history=HistoryPolicy.KEEP_LAST, depth=50,
    reliability=ReliabilityPolicy.RELIABLE, durability=DurabilityPolicy.VOLATILE,
)

# キュー状態: 後から起動した Web サーバもすぐ最新状態を受け取れるよう transient_local
STATUS_QOS = QoSProfile(
    history=HistoryPolicy.KEEP_LAST, depth=1,
    reliability=ReliabilityPolicy.RELIABLE, durability=DurabilityPolicy.TRANSIENT_LOCAL,
)

TOPIC_ORDERS = "shop/orders"
TOPIC_STATUS = "shop/queue_status"
TOPIC_MARKER = "shop/marker_id"
SRV_CONTROL = "shop/queue_control"
ACTION_PREPARE = "shop/prepare_order"
