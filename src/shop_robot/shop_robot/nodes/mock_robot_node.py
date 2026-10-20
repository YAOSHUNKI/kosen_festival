"""偽ロボット（受取ノードの動作確認用）

  sub  /shop/handover/state   受取ノードが今どの動作をしているか
  pub  handover.yaml の done トピック   その動作の完了メッセージを delay 秒後に返す
  pub  ready トピック（設定時）  1Hz で準備完了を出す

handover.yaml だけを読んで応答を作るので、実機が無くても注文 → マーカー → 受取 → 完了の流れを試せる。

  ros2 run shop_robot mock_robot --ros-args -p fail_motion:=grasp   # つかみ失敗を再現
"""
import json

import rclpy
from rclpy.node import Node
from std_msgs.msg import String

from ..core.handover import HandoverConfigError, load_config, make_step
from .handover_node import TOPIC_HANDOVER_STATE, RosBus, default_motion_file


class MockRobot(Node):
    def __init__(self):
        super().__init__("mock_robot")
        path = self.declare_parameter("motion_file", "").value or default_motion_file()
        self.delay = float(self.declare_parameter("delay", 1.0).value)
        self.fail_motion = self.declare_parameter("fail_motion", "").value  # この動作には応答しない
        self.cfg = load_config(path)
        done_keys = {m.done for m in self.cfg.motions.values() if m.done}
        if self.cfg.ready:
            done_keys.add(self.cfg.ready.topic)
        self.bus = RosBus(self, self.cfg, None, publish_keys=done_keys, subscribe_keys=())
        self.create_subscription(String, TOPIC_HANDOVER_STATE, self.on_state, 10)
        if self.cfg.ready:
            reply = {k: v for k, v in self.cfg.ready.match.items() if not isinstance(v, dict)}
            self.create_timer(1.0, lambda: self.bus.publish(self.cfg.ready.topic, reply))
        self._timers = []
        self.get_logger().info(f"偽ロボット起動（応答 {self.delay:g} 秒後, 応答しない動作: {self.fail_motion or 'なし'}）")

    def on_state(self, msg: String):
        d = json.loads(msg.data)
        if d.get("status") not in ("running", "aborting"):
            self.get_logger().info(f"{d['order_number']}番: {d['status']} {d.get('message', '')}")
            return
        m = self.cfg.motions.get(d.get("motion"))
        if m is None or not m.done:
            return
        if m.name == self.fail_motion:
            self.get_logger().warn(f"  {m.name}: 応答しません（fail_motion）")
            return
        step = make_step(m, d.get("ctx", {}))
        if step.mock is None:
            self.get_logger().warn(f"  {m.name}: match から応答を作れません。handover.yaml に mock を書いてください")
            return
        delay = self.delay if m.mock_delay is None else m.mock_delay
        self.get_logger().info(f"  {step.label} → {delay:g}秒後に {self.cfg.topics[m.done].topic} {step.mock}")
        self.later(delay, lambda: self.bus.publish(m.done, step.mock))

    def later(self, sec, fn):
        timer = None

        def fire():
            timer.cancel()
            self.destroy_timer(timer)
            self._timers.remove(timer)
            fn()

        timer = self.create_timer(max(sec, 0.01), fire)
        self._timers.append(timer)


def main(args=None):
    rclpy.init(args=args)
    try:
        node = MockRobot()
    except HandoverConfigError as e:
        print(f"[mock_robot] 動作定義のエラー: {e}")
        rclpy.shutdown()
        raise SystemExit(1)
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
