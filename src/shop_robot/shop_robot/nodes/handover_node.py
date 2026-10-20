"""受取ノード（ロボット側）: 受渡し台の商品をつかんでお客様に手渡す

  システムとのやり取り（標準メッセージ型。PrepareNodeBase / core/codec.py を参照）
  sub  /shop/prepare_order   std_msgs/Int32MultiArray  [商品ID, 個数, 商品ID, 個数, ...]  受取開始
  pub  /shop/prepare_done    std_msgs/Bool             true=成功 / false=失敗（後始末を終えてから返す）

  pub  /shop/handover/state  std_msgs/String               実行中の動作（JSON。status: running / aborting / success / failed / canceled）
  pub/sub  handover.yaml の topics                          ロボットへの指令・完了/状態の受信

動作の中身は config/handover.yaml（motion_file パラメータ）に「トピックごとの動作オブジェクト」として書く。
このノードは YAML の型名からメッセージクラスを読み込み、publisher / subscription を起動時に全部作る。
"""
import json
import os

import rclpy
from ament_index_python.packages import get_package_share_directory
from rclpy.callback_groups import ReentrantCallbackGroup
from rclpy.node import Node
from rclpy.qos import DurabilityPolicy, HistoryPolicy, QoSProfile, ReliabilityPolicy
from rosidl_runtime_py.set_message import set_message_fields
from rosidl_runtime_py.utilities import get_message
from std_msgs.msg import String

from ..core.handover import (
    STATUS_SUCCESS, HandoverConfig, HandoverConfigError, HandoverRunner, Watcher, load_config,
    make_step, order_context,
)
from ..core.models import Order, OrderItem
from .prepare_stub_node import PrepareNodeBase, run_node

TOPIC_HANDOVER_STATE = "shop/handover/state"


def default_motion_file() -> str:
    return os.path.join(get_package_share_directory("shop_robot"), "config", "handover.yaml")


def topic_qos(kind: str) -> QoSProfile:
    return QoSProfile(
        history=HistoryPolicy.KEEP_LAST, depth=10,
        reliability=ReliabilityPolicy.BEST_EFFORT if kind == "best_effort" else ReliabilityPolicy.RELIABLE,
        durability=DurabilityPolicy.TRANSIENT_LOCAL if kind == "latched" else DurabilityPolicy.VOLATILE,
    )


class RosBus:
    """HandoverRunner の Bus を rclpy で実装する（指令の publish・完了トピックの受信）"""

    def __init__(self, node: Node, cfg: HandoverConfig, cb_group, publish_keys=None, subscribe_keys=None):
        self.node = node
        self.cfg = cfg
        self._types = {}
        self._pubs = {}
        self._watchers = {}
        self._subs = []
        for key in (cfg.pub_topics() if publish_keys is None else publish_keys):
            t = cfg.topics[key]
            self._pubs[key] = node.create_publisher(self.msg_type(key), t.topic, topic_qos(t.qos))
        for key in (cfg.sub_topics() if subscribe_keys is None else subscribe_keys):
            t = cfg.topics[key]
            w = self._watchers[key] = Watcher()
            self._subs.append(node.create_subscription(
                self.msg_type(key), t.topic, w.push, topic_qos(t.qos), callback_group=cb_group))

    def msg_type(self, key: str):
        if key not in self._types:
            t = self.cfg.topics[key]
            try:
                self._types[key] = get_message(t.type)
            except (AttributeError, ModuleNotFoundError, ValueError) as e:
                raise HandoverConfigError(f"topics.{key}: メッセージ型 {t.type} を読み込めません（{e}）") from None
        return self._types[key]

    def make_msg(self, key: str, fields: dict):
        msg = self.msg_type(key)()
        set_message_fields(msg, fields)
        return msg

    def publish(self, key: str, fields: dict) -> None:
        self._pubs[key].publish(self.make_msg(key, fields))

    def watcher(self, key: str) -> Watcher:
        return self._watchers[key]


class HandoverNode(PrepareNodeBase):
    def __init__(self):
        super().__init__("handover")
        path = self.declare_parameter("motion_file", "").value or default_motion_file()
        self.cfg = load_config(path)
        cb = ReentrantCallbackGroup()
        self.bus = RosBus(self, self.cfg, cb)
        self._check_messages()
        self.runner = HandoverRunner(self.cfg, self.bus, log=self.get_logger())
        self.state_pub = self.create_publisher(String, TOPIC_HANDOVER_STATE, 10)
        self.get_logger().info(
            f"受取ノード起動: {path}（{len(self.cfg.sequence)}動作 × repeat={self.cfg.repeat}, "
            f"後始末 {self.cfg.on_abort or 'なし'}）")
        for key in sorted(self.cfg.pub_topics()):
            self.get_logger().info(f"  指令 → {self.cfg.topics[key].topic} [{self.cfg.topics[key].type}]")
        for key in sorted(self.cfg.sub_topics()):
            self.get_logger().info(f"  受信 ← {self.cfg.topics[key].topic} [{self.cfg.topics[key].type}]")

    def _check_messages(self):
        """msg のフィールド名・型の書き間違いを起動時に見つける（ダミーの注文で組み立ててみる）"""
        ctx = {**order_context(Order(1, [OrderItem(1, "test", 1)])),
               "product_id": 1, "product_name": "test", "quantity": 1, "item_index": 1, "unit_index": 1}
        for m in self.cfg.motions.values():
            if not m.pub:
                continue
            try:
                self.bus.make_msg(m.pub, make_step(m, ctx).msg)
            except Exception as e:  # noqa: BLE001  set_message_fields はいろいろな例外を出す
                raise HandoverConfigError(f"motions.{m.name}.msg を {self.cfg.topics[m.pub].type} に入れられません: {e}") from None

    # --- PrepareNodeBase の実装 -----------------------------------------------------
    def can_accept(self) -> tuple[bool, str]:
        return self.runner.can_accept()   # 非常停止中・ロボット未準備なら受け付けない

    def execute_order(self, order) -> bool:
        # トピックに注文番号は含まれないので、order.order_number はこのノードでの通し番号（ログ・状態表示用）
        n = order.order_number
        self.get_logger().info(f"受取開始（依頼 #{n}）: {order.summary()}")

        def on_step(i, total, step):
            self.publish_state(n, "running", step=step, index=i, total=total)
            self.get_logger().info(f"  [{i + 1}/{total}] {step.label}")

        def on_abort_step(step):
            self.publish_state(n, "aborting", step=step)
            self.get_logger().info(f"  [後始末] {step.label}")

        res = self.runner.run(order, on_step=on_step, on_abort_step=on_abort_step)
        self.publish_state(n, res.status, message=res.message)
        if res.status == STATUS_SUCCESS:
            self.get_logger().info(f"受取完了（依頼 #{n}）")
            return True
        self.get_logger().warn(f"受取失敗（依頼 #{n}）: {res.message}")
        return False

    def publish_state(self, order_number, status, step=None, index=0, total=0, message=""):
        d = {"order_number": order_number, "status": status, "message": message}
        if step is not None:
            d.update(motion=step.motion.name, label=step.label, index=index, total=total, ctx=step.ctx)
        self.state_pub.publish(String(data=json.dumps(d, ensure_ascii=False)))


def main(args=None):
    rclpy.init(args=args)
    try:
        node = HandoverNode()
    except HandoverConfigError as e:
        print(f"[handover] 動作定義のエラー: {e}")
        rclpy.shutdown()
        raise SystemExit(1)
    run_node(node)


if __name__ == "__main__":
    main()
