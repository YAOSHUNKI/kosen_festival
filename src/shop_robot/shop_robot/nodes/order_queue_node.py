"""注文キューノード（ロボット側）

  sub  /shop/orders         shop_interfaces/Order       Web から届く注文を「準備中」に積む
  sub  /shop/marker_id      std_msgs/Int32              ArUco 検出結果。呼び出し中の番号ならロボット動作開始
  pub  /shop/queue_status   shop_interfaces/QueueStatus 状態（変化時 + 1Hz）
  srv  /shop/queue_control  shop_interfaces/QueueControl 完成（呼び出し）・呼び出し取消・注文取消・強制開始
  act  /shop/prepare_order  shop_interfaces/PrepareOrder ロボット動作ノードへの依頼（クライアント）
"""
import rclpy
from action_msgs.msg import GoalStatus
from rclpy.action import ActionClient
from rclpy.node import Node
from shop_interfaces.action import PrepareOrder
from shop_interfaces.msg import Order as OrderMsg
from shop_interfaces.msg import QueueStatus
from shop_interfaces.srv import QueueControl
from std_msgs.msg import Int32

from ..core.order_queue import (
    OrderQueue, QueueError, RESULT_CANCELED, RESULT_FAILED, RESULT_SUCCESS,
)
from .qos import ACTION_PREPARE, ORDERS_QOS, SRV_CONTROL, STATUS_QOS, TOPIC_MARKER, TOPIC_ORDERS, TOPIC_STATUS
from .ros_convert import msg_to_order, order_to_msg, snapshot_to_msg


class OrderQueueNode(Node):
    def __init__(self):
        super().__init__("order_queue")
        state_file = self.declare_parameter("state_file", "~/.shop_robot/queue_state.json").value
        rate = self.declare_parameter("status_rate", 1.0).value
        self.declare_parameter("cancel_timeout", 5.0)

        self.q = OrderQueue(state_file)
        self._goal_handle = None
        self._cancel_requested = False
        self._seq = 0

        self.status_pub = self.create_publisher(QueueStatus, TOPIC_STATUS, STATUS_QOS)
        self.create_subscription(OrderMsg, TOPIC_ORDERS, self.on_order, ORDERS_QOS)
        self.create_subscription(Int32, TOPIC_MARKER, self.on_marker, 10)
        self.create_service(QueueControl, SRV_CONTROL, self.on_control)
        self.prepare = ActionClient(self, PrepareOrder, ACTION_PREPARE)
        self.create_timer(1.0 / rate, self.publish_status)

        self.get_logger().info(f"注文キュー起動（準備中 {len(self.q.pending)}件・呼び出し中 {len(self.q.ready)}件, 状態ファイル {state_file}）")
        self.publish_status()

    def publish_status(self):
        self.status_pub.publish(snapshot_to_msg(self.q.snapshot(), self.get_clock().now().to_msg()))

    # --- 入力 -----------------------------------------------------------------
    def on_order(self, msg: OrderMsg):
        try:
            order = msg_to_order(msg)
            self.q.add(order)
            self.get_logger().info(f"受付: {order.order_number}番 {order.summary()}")
        except QueueError as e:
            self.q.note(str(e))
            self.get_logger().warn(f"注文を受け付けられません: {e}")
        self.publish_status()

    def on_marker(self, msg: Int32):
        n = msg.data
        ok, reason = self.q.check_marker(n)
        if ok:
            self.get_logger().info(f"マーカー {n} を検出 → ロボット動作開始")
            self.start(n)
        else:
            self.q.note(reason, public=True)
            self.get_logger().info(f"マーカー {n}: {reason}")
        self.publish_status()

    def on_control(self, req: QueueControl.Request, res: QueueControl.Response):
        n = req.order_number
        try:
            if req.command == QueueControl.Request.CANCEL:
                if self.q.cancel(n) == "active":
                    self.cancel_active()
                    res.message = f"{n}番のロボット動作を中止しています"
            elif req.command == QueueControl.Request.MARK_READY:
                self.q.mark_ready(n)
            elif req.command == QueueControl.Request.UNMARK_READY:
                self.q.unmark_ready(n)
            elif req.command == QueueControl.Request.CLEAR_ALL:
                if self.q.clear_all():
                    self.cancel_active()
            elif req.command == QueueControl.Request.FORCE_START:
                ok, reason = self.start(n)
                if not ok:
                    raise QueueError(reason)
            elif req.command == QueueControl.Request.INJECT_MARKER:
                self.on_marker(Int32(data=n))
                if self.q.active is None or self.q.active.order_number != n:
                    raise QueueError(self.q.last_event)
            else:
                raise QueueError(f"不明なコマンド {req.command}")
            res.success = True
            res.message = res.message or self.q.last_event
        except QueueError as e:
            res.success, res.message = False, str(e)
        self.publish_status()
        return res

    # --- ロボットへの動作依頼 -----------------------------------------------------
    # 1回の動作ごとに seq を振り、古いゴールの遅れて届いた結果・進捗で新しい注文を壊さないようにする
    def start(self, n: int) -> tuple[bool, str]:
        if not self.prepare.server_is_ready():
            reason = "ロボット動作ノード（prepare_order）が起動していません"
            self.q.note(reason)
            self.q.note("ただいまロボットを準備しています。スタッフにお声がけください", public=True)
            self.q.last_event = reason
            self.get_logger().error(reason)
            return False, reason
        try:
            order = self.q.start(n)
        except QueueError as e:
            return False, str(e)
        self._seq += 1
        seq = self._seq
        self._goal_handle = None
        self._cancel_requested = False
        goal = PrepareOrder.Goal(order=order_to_msg(order))
        fut = self.prepare.send_goal_async(goal, feedback_callback=lambda fb: self.on_feedback(seq, fb))
        fut.add_done_callback(lambda f: self.on_goal_response(seq, f))
        return True, ""

    def on_goal_response(self, seq, fut):
        if seq != self._seq:
            return
        gh = fut.result()
        if not gh.accepted:
            self.q.requeue_active("ロボットが受け付けませんでした")
            self.get_logger().error(self.q.last_event)
            self.publish_status()
            return
        self._goal_handle = gh
        gh.get_result_async().add_done_callback(lambda f: self.on_result(seq, f))
        if self._cancel_requested:  # 受理前に取消された
            gh.cancel_goal_async()

    def on_feedback(self, seq, fb):
        if seq != self._seq or self.q.active is None:
            return
        self.q.set_progress(fb.feedback.progress, fb.feedback.current_step)
        self.publish_status()

    def on_result(self, seq, fut):
        if seq != self._seq or self.q.active is None:
            return  # 強制取消済みのゴールの結果
        res = fut.result()
        self._seq += 1
        self._goal_handle = None
        if res.status == GoalStatus.STATUS_SUCCEEDED and res.result.success:
            order = self.q.finish_active(RESULT_SUCCESS)
        elif res.status == GoalStatus.STATUS_CANCELED:
            order = self.q.finish_active(RESULT_CANCELED)
        else:
            order = self.q.finish_active(RESULT_FAILED, res.result.message or f"status={res.status}")
        self.get_logger().info(f"{order.order_number}番: {self.q.last_event}")
        self.publish_status()

    def cancel_active(self):
        self._cancel_requested = True
        if self._goal_handle is not None:
            self._goal_handle.cancel_goal_async()
        # 動作ノードが落ちていて応答がない場合でもキューが止まらないよう、一定時間後に強制終了
        seq = self._seq
        timer = None

        def force():
            self.destroy_timer(timer)
            if seq == self._seq and self.q.active is not None:
                self._seq += 1
                self._goal_handle = None
                order = self.q.finish_active(RESULT_CANCELED)
                self.get_logger().warn(f"{order.order_number}番: 動作ノードから応答がないため取消扱いにしました")
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
