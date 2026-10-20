"""注文キューノード（ロボット側）。すべて標準メッセージ型（形式は core/codec.py）

  sub  /shop/orders               std_msgs/Int32MultiArray   [注文番号, 商品ID, 個数, ...] →「準備中」に積む
  sub  /shop/marker_id            std_msgs/Int32             ArUco 検出結果。呼び出し中の番号ならロボット動作開始
  sub  /shop/queue_command        std_msgs/String            スタッフ操作 "<要求ID> <コマンド> <注文番号>"
  pub  /shop/queue_command_result std_msgs/String            "<要求ID> ok|ng <メッセージ>"
  pub  /shop/queue_status         std_msgs/String (JSON)     状態（変化時 + 1Hz, transient_local）

  ロボット動作ノードとのやり取り
  pub  /shop/prepare_order        std_msgs/Int32MultiArray   [注文番号, 商品ID, 個数, ...] 動作開始の依頼
  pub  /shop/prepare_cancel       std_msgs/Int32             注文番号  動作中止の依頼
  sub  /shop/prepare_result       std_msgs/Int32MultiArray   [注文番号, 結果]  1=成功 0=失敗 2=取消
  sub  /shop/prepare_progress     std_msgs/Float32MultiArray [注文番号, 進捗0〜1]（任意）
  sub  /shop/prepare_step         std_msgs/String            "<注文番号> <ステップ名>"（任意。画面表示・失敗理由）
"""
import rclpy
from rclpy.node import Node
from std_msgs.msg import Float32MultiArray, Int32, Int32MultiArray, String

from ..core import codec
from ..core.order_queue import (
    OrderQueue, QueueError, RESULT_CANCELED, RESULT_FAILED, RESULT_SUCCESS,
)
from .qos import (
    ORDERS_QOS, RELIABLE_QOS, STATUS_QOS, TOPIC_COMMAND, TOPIC_COMMAND_RESULT, TOPIC_MARKER,
    TOPIC_ORDERS, TOPIC_PREPARE, TOPIC_PREPARE_CANCEL, TOPIC_PREPARE_PROGRESS, TOPIC_PREPARE_RESULT,
    TOPIC_PREPARE_STEP, TOPIC_STATUS,
)


class OrderQueueNode(Node):
    def __init__(self):
        super().__init__("order_queue")
        state_file = self.declare_parameter("state_file", "~/.shop_robot/queue_state.json").value
        rate = self.declare_parameter("status_rate", 1.0).value
        self.declare_parameter("cancel_timeout", 5.0)

        self.q = OrderQueue(state_file)
        self._step = ""   # ロボットから届いた現在ステップ名（動作中の注文のもの）

        self.status_pub = self.create_publisher(String, TOPIC_STATUS, STATUS_QOS)
        self.result_pub = self.create_publisher(String, TOPIC_COMMAND_RESULT, RELIABLE_QOS)
        self.prepare_pub = self.create_publisher(Int32MultiArray, TOPIC_PREPARE, RELIABLE_QOS)
        self.cancel_pub = self.create_publisher(Int32, TOPIC_PREPARE_CANCEL, RELIABLE_QOS)
        self.create_subscription(Int32MultiArray, TOPIC_ORDERS, self.on_order, ORDERS_QOS)
        self.create_subscription(String, TOPIC_COMMAND, self.on_command, RELIABLE_QOS)
        self.create_subscription(Int32, TOPIC_MARKER, lambda m: self.on_marker(m.data), 10)
        self.create_subscription(Int32MultiArray, TOPIC_PREPARE_RESULT, self.on_result, RELIABLE_QOS)
        self.create_subscription(Float32MultiArray, TOPIC_PREPARE_PROGRESS, self.on_progress, 10)
        self.create_subscription(String, TOPIC_PREPARE_STEP, self.on_step, 10)
        self.create_timer(1.0 / rate, self.publish_status)

        self.get_logger().info(
            f"注文キュー起動（準備中 {len(self.q.pending)}件・呼び出し中 {len(self.q.ready)}件, 状態ファイル {state_file}）")
        self.publish_status()

    def publish_status(self):
        self.status_pub.publish(String(data=codec.encode_status(self.q.snapshot())))

    # --- 入力 -----------------------------------------------------------------
    def on_order(self, msg: Int32MultiArray):
        try:
            order = codec.decode_order(msg.data)
            self.q.add(order)
            self.get_logger().info(f"受付: {order.order_number}番 {codec.encode_order(order)}")
        except (codec.CodecError, QueueError) as e:
            self.q.note(str(e))
            self.get_logger().warn(f"注文を受け付けられません: {e}")
        self.publish_status()

    def on_marker(self, n: int):
        ok, reason = self.q.check_marker(n)
        if ok:
            self.get_logger().info(f"マーカー {n} を検出 → ロボット動作開始")
            self.start(n)
        else:
            self.q.note(reason, public=True)
            self.get_logger().info(f"マーカー {n}: {reason}")
        self.publish_status()

    def on_command(self, msg: String):
        try:
            rid, cmd, n = codec.decode_command(msg.data)
        except codec.CodecError as e:
            self.get_logger().warn(str(e))
            return
        ok, text = self.handle_command(cmd, n)
        self.result_pub.publish(String(data=codec.encode_command_result(rid, ok, text)))
        self.publish_status()

    def handle_command(self, cmd: str, n: int) -> tuple[bool, str]:
        try:
            if cmd == "cancel":
                if self.q.cancel(n) == "active":
                    self.cancel_active()
                    return True, f"{n}番のロボット動作を中止しています"
            elif cmd == "ready":
                self.q.mark_ready(n)
            elif cmd == "unready":
                self.q.unmark_ready(n)
            elif cmd == "clear_all":
                if self.q.clear_all():
                    self.cancel_active()
            elif cmd == "force_start":
                ok, reason = self.start(n)
                if not ok:
                    raise QueueError(reason)
            elif cmd == "inject_marker":
                self.on_marker(n)
                if self.q.active is None or self.q.active.order_number != n:
                    raise QueueError(self.q.last_event)
        except QueueError as e:
            return False, str(e)
        return True, self.q.last_event

    # --- ロボットへの動作依頼 -----------------------------------------------------
    def start(self, n: int) -> tuple[bool, str]:
        if self.count_subscribers(TOPIC_PREPARE) == 0:
            reason = f"ロボット動作ノード（/{TOPIC_PREPARE} の購読者）が起動していません"
            self.q.note("ただいまロボットを準備しています。スタッフにお声がけください", public=True)
            self.q.last_event = reason
            self.get_logger().error(reason)
            return False, reason
        try:
            order = self.q.start(n)
        except QueueError as e:
            return False, str(e)
        self._step = ""
        self.prepare_pub.publish(Int32MultiArray(data=codec.encode_order(order)))
        return True, ""

    def _is_active(self, n: int) -> bool:
        return self.q.active is not None and self.q.active.order_number == n

    def on_progress(self, msg: Float32MultiArray):
        try:
            n, p = codec.decode_progress(msg.data)
        except codec.CodecError as e:
            self.get_logger().warn(str(e))
            return
        if self._is_active(n):
            self.q.set_progress(p, self._step or f"{round(p * 100)}%")
            self.publish_status()

    def on_step(self, msg: String):
        try:
            n, label = codec.decode_step(msg.data)
        except codec.CodecError as e:
            self.get_logger().warn(str(e))
            return
        if self._is_active(n):
            self._step = label
            self.q.set_progress(self.q.progress, label)
            self.publish_status()

    def on_result(self, msg: Int32MultiArray):
        try:
            n, code = codec.decode_result(msg.data)
        except codec.CodecError as e:
            self.get_logger().warn(str(e))
            return
        if not self._is_active(n):
            return  # 取消済み・強制終了済みの注文の結果が遅れて届いた
        if code == codec.RESULT_SUCCESS:
            self.q.finish_active(RESULT_SUCCESS)
        elif code == codec.RESULT_CANCELED:
            self.q.finish_active(RESULT_CANCELED)
        else:
            reason = self._step[len("失敗: "):] if self._step.startswith("失敗: ") else (
                f"ロボットが失敗を報告・{self._step}" if self._step else "ロボットが失敗を報告")
            self.q.finish_active(RESULT_FAILED, reason)
        self.get_logger().info(f"{n}番: {self.q.last_event}")
        self.publish_status()

    def cancel_active(self):
        if self.q.active is None:
            return
        n = self.q.active.order_number
        self.cancel_pub.publish(Int32(data=n))
        # 動作ノードが応答しない場合でもキューが止まらないよう、一定時間後に取消扱いにする
        timer = None

        def force():
            self.destroy_timer(timer)
            if self._is_active(n):
                self.q.finish_active(RESULT_CANCELED)
                self.get_logger().warn(f"{n}番: 動作ノードから応答がないため取消扱いにしました")
                self.publish_status()

        timer = self.create_timer(self.get_parameter("cancel_timeout").value, force)


def main(args=None):
    rclpy.init(args=args)
    node = OrderQueueNode()
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
