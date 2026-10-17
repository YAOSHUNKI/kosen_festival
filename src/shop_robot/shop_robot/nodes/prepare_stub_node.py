"""商品準備アクションサーバ（スタブ）

  act  /shop/prepare_order  shop_interfaces/PrepareOrder

実機に置き換えるときは PrepareServerBase を継承して prepare_unit() に
「商品 item を1個用意する」動作を書けばよい（キュー側は変更不要）。

    class MyRobotServer(PrepareServerBase):
        def prepare_unit(self, item, k, goal_handle) -> bool:
            if item.product_id == 1:
                ...  # たこ焼きを1個つかんで容器に入れる
            return True   # 失敗したら False（注文は先頭に戻り、スタッフが再開できる）
"""
import time

import rclpy
from rclpy.action import ActionServer, CancelResponse, GoalResponse
from rclpy.callback_groups import ReentrantCallbackGroup
from rclpy.executors import MultiThreadedExecutor
from rclpy.node import Node
from shop_interfaces.action import PrepareOrder

from .qos import ACTION_PREPARE


class PrepareServerBase(Node):
    def __init__(self, node_name: str):
        super().__init__(node_name)
        self._busy = False
        self._server = ActionServer(
            self, PrepareOrder, ACTION_PREPARE,
            execute_callback=self.execute,
            goal_callback=self.on_goal,
            cancel_callback=lambda _: CancelResponse.ACCEPT,
            callback_group=ReentrantCallbackGroup(),
        )

    def on_goal(self, goal: PrepareOrder.Goal):
        # ロボットは一度に1注文だけ（キュー側も1件ずつ送るが念のため）
        return GoalResponse.REJECT if self._busy else GoalResponse.ACCEPT

    def execute(self, goal_handle):
        self._busy = True
        try:
            return self._execute(goal_handle)
        finally:
            self._busy = False

    def _execute(self, goal_handle):
        order = goal_handle.request.order
        units = [(it, k) for it in order.items for k in range(1, it.quantity + 1)]
        self.get_logger().info(f"準備開始: {order.order_number}番（{len(units)}個）")
        fb = PrepareOrder.Feedback()
        for i, (item, k) in enumerate(units):
            if goal_handle.is_cancel_requested:
                goal_handle.canceled()
                self.on_canceled(order)
                return PrepareOrder.Result(success=False, message="取消")
            fb.progress = i / len(units)
            fb.current_step = f"{item.product_name or item.product_id} {k}/{item.quantity} 個目"
            goal_handle.publish_feedback(fb)
            if not self.prepare_unit(item, k, goal_handle):
                goal_handle.abort()
                return PrepareOrder.Result(success=False, message=f"{fb.current_step}で失敗")
        if goal_handle.is_cancel_requested:
            goal_handle.canceled()
            self.on_canceled(order)
            return PrepareOrder.Result(success=False, message="取消")
        fb.progress, fb.current_step = 1.0, "完了"
        goal_handle.publish_feedback(fb)
        goal_handle.succeed()
        self.get_logger().info(f"準備完了: {order.order_number}番")
        return PrepareOrder.Result(success=True, message="")

    # --- 実機ではここを実装する ---------------------------------------------------
    def prepare_unit(self, item, k: int, goal_handle) -> bool:
        """item（OrderItem msg）を1個用意する。k は何個目か（1始まり）。成功なら True。
        長い動作の途中では goal_handle.is_cancel_requested を見て早めに切り上げると良い。"""
        raise NotImplementedError

    def on_canceled(self, order):
        """取消時の後始末（アームを初期姿勢に戻す等）"""
        self.get_logger().info(f"取消: {order.order_number}番")


class PrepareStub(PrepareServerBase):
    """動作の代わりに待つだけのスタブ"""

    def __init__(self):
        super().__init__("prepare_stub")
        self.sec = self.declare_parameter("seconds_per_item", 3.0).value
        self.fail_product_id = self.declare_parameter("fail_product_id", -1).value  # 失敗時の動作確認用

    def prepare_unit(self, item, k, goal_handle) -> bool:
        self.get_logger().info(f"  [stub] 商品ID {item.product_id}（{item.product_name}）{k}/{item.quantity}")
        t_end = time.monotonic() + self.sec
        while time.monotonic() < t_end:
            if goal_handle.is_cancel_requested:
                return True  # 呼び出し元で取消処理
            time.sleep(0.05)
        return item.product_id != self.fail_product_id


def main(args=None):
    rclpy.init(args=args)
    node = PrepareStub()
    executor = MultiThreadedExecutor()
    executor.add_node(node)
    try:
        executor.spin()
    except KeyboardInterrupt:
        pass
    finally:
        node.destroy_node()
        if rclpy.ok():
            rclpy.shutdown()


if __name__ == "__main__":
    main()
