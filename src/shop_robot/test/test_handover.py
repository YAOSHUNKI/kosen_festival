"""受取動作コア（core/handover.py）のテスト。ROS なしで偽の Bus とロボットを使う"""
import threading
import time
from pathlib import Path

import pytest
import yaml

from shop_robot.core.handover import (
    STATUS_CANCELED, STATUS_FAILED, STATUS_SUCCESS, HandoverConfigError, HandoverRunner, Watcher,
    build_plan, expand, load_config, matches, mock_reply, parse_config,
)
from shop_robot.core.models import Order, OrderItem

CONFIG = Path(__file__).resolve().parents[1] / "config" / "handover.yaml"


def order(n=7, *items):
    return Order(n, list(items) or [OrderItem(1, "たこ焼き", 2)])


class FakeBus:
    """publish を記録し、pub トピックごとの応答関数でロボットを模擬する"""

    def __init__(self, cfg, delay=0.01):
        self.cfg = cfg
        self.delay = delay
        self.watchers = {k: Watcher() for k in cfg.sub_topics()}
        self.sent = []
        self.responders = {}   # pub トピック -> fields -> [(done トピック, 返すメッセージ)]

    def watcher(self, key):
        return self.watchers[key]

    def publish(self, key, fields):
        self.sent.append((key, fields))
        for done, reply in self.responders.get(key, lambda f: [])(fields):
            threading.Timer(self.delay, self.watchers[done].push, args=(reply,)).start()

    def push_later(self, key, msg, delay):
        threading.Timer(delay, self.watchers[key].push, args=(msg,)).start()


def robot(bus, taken_after=0.05, grasp_ok=True):
    """handover.yaml の想定どおりに応答する偽ロボット"""
    bus.responders = {
        "arm_cmd": lambda f: [("arm_state", {"data": f["data"]})],
        "grip_cmd": lambda f: [("holding", {"data": f["data"] > 0.5})] if (grasp_ok or f["data"] < 0.5) else [],
    }
    orig = bus.publish

    def publish(key, fields):
        orig(key, fields)
        if key == "say" and "受け取り" in fields["data"] and taken_after is not None:
            bus.push_later("taken", {"data": True}, taken_after)
    bus.publish = publish
    return bus


def short_timeouts(cfg, sec=0.3):
    for m in cfg.motions.values():
        m.timeout = sec
    return cfg


# --- 設定 ---------------------------------------------------------------------
def test_default_config_loads():
    cfg = load_config(CONFIG)
    assert cfg.repeat == "order"
    assert cfg.sequence[0] == "to_counter"
    assert cfg.topics["arm_cmd"].type == "std_msgs/msg/String"
    assert {"arm_state", "holding", "taken", "estop"} <= cfg.sub_topics()
    assert {"arm_cmd", "grip_cmd", "say"} == cfg.pub_topics()


def base_cfg(**over):
    d = {
        "topics": {"cmd": {"topic": "/c", "type": "std_msgs/String"}, "st": {"topic": "/s", "type": "std_msgs/msg/String"}},
        "motions": {"a": {"pub": "cmd", "msg": {"data": "{order_number}"}, "done": "st", "match": {"data": "a"}}},
        "sequence": ["a"],
    }
    d.update(over)
    return d


def test_type_normalized():
    assert parse_config(base_cfg()).topics["cmd"].type == "std_msgs/msg/String"


@pytest.mark.parametrize("over, words", [
    ({"sequence": ["nope"]}, "nope"),
    ({"motions": {"a": {"pub": "zzz"}}}, "zzz"),
    ({"motions": {"a": {"pub": "cmd", "tiemout": 3}}}, "tiemout"),
    ({"motions": {"a": {"label": "x"}}}, "pub / done / duration"),
    ({"motions": {"a": {"pub": "cmd", "match": {"data": 1}}}}, "done"),
    ({"motions": {"a": {"done": "st", "match": {"data": {"approx": 1}}}}}, "比較"),
    ({"motions": {"a": {"pub": "cmd", "msg": {"data": "{product_id}"}}}}, "product_id"),
    ({"repeat": "each"}, "repeat"),
    ({"estop": {"topic": "st"}}, "estop"),
    ({"topics": {"cmd": {"topic": "/c", "type": "String"}}}, "type"),
])
def test_config_errors(over, words):
    with pytest.raises(HandoverConfigError, match=words):
        parse_config(base_cfg(**over))


def test_item_placeholders_allowed_with_repeat_item():
    cfg = parse_config(base_cfg(repeat="item", motions={"a": {"pub": "cmd", "msg": {"data": "{product_id}"}}}))
    assert cfg.repeat == "item"


def test_load_missing_file(tmp_path):
    with pytest.raises(HandoverConfigError, match="ありません"):
        load_config(tmp_path / "none.yaml")


# --- 置換・判定 ----------------------------------------------------------------
def test_expand():
    ctx = {"order_number": 12, "summary": "たこ焼き×2"}
    assert expand("{order_number}", ctx) == 12            # 単独なら数値のまま
    assert expand("{order_number}番 {summary}", ctx) == "12番 たこ焼き×2"
    assert expand({"a": ["{order_number}", 1.5]}, ctx) == {"a": [12, 1.5]}
    assert expand('{{"json": {order_number}}}', ctx) == '{"json": 12}'


class Obj:
    def __init__(self, **kw):
        self.__dict__.update(kw)


def test_matches():
    msg = Obj(data=True, wrench=Obj(force=Obj(z=1.5)), position=[0.1, 0.2])
    assert matches(msg, {"data": True})
    assert not matches(msg, {"data": False})
    assert matches(msg, {"wrench.force.z": {"lt": 2.0, "gt": 1.0}})
    assert not matches(msg, {"wrench.force.z": {"ge": 2.0}})
    assert matches(msg, {"position.1": 0.2})
    assert matches({"data": "3"}, {"data": 3})                   # 文字列と数値
    assert not matches(msg, {"missing": 1})
    assert not matches(None, {"data": True})


def test_mock_reply():
    cfg = parse_config(base_cfg(motions={
        "a": {"done": "st", "match": {"x.y": 1, "z": {"ge": 2}}},
        "b": {"done": "st", "match": {"z": {"gt": 2}}},
        "c": {"done": "st", "match": {"z": {"gt": 2}}, "mock": {"z": 5}},
    }, sequence=["a", "b", "c"]))
    assert mock_reply(cfg.motions["a"]) == {"x": {"y": 1}, "z": 2}
    assert mock_reply(cfg.motions["b"]) is None
    assert mock_reply(cfg.motions["c"]) == {"z": 5}


# --- 計画 ---------------------------------------------------------------------
def test_plan_repeat_modes():
    d = base_cfg(motions={"a": {"pub": "cmd", "msg": {"data": "{order_number}"}},
                          "u": {"pub": "cmd", "msg": {"data": "{product_name}-{unit_index}"}}},
                 sequence=["u"], before=["a"], after=["a"], repeat="unit")
    o = order(5, OrderItem(1, "A", 2), OrderItem(2, "B", 1))
    plan = build_plan(parse_config(d), o)
    assert [s.msg["data"] for s in plan] == [5, "A-1", "A-2", "B-1", 5]
    d["repeat"] = "item"
    d["motions"]["u"]["msg"]["data"] = "{product_name}x{quantity}"
    assert [s.msg["data"] for s in build_plan(parse_config(d), o)] == [5, "Ax2", "Bx1", 5]


def test_plan_default_config():
    plan = build_plan(load_config(CONFIG), order(9))
    assert [s.motion.name for s in plan][-2:] == ["thanks", "home"]
    call = next(s for s in plan if s.motion.name == "call")
    assert call.msg == {"data": "お待たせしました。どうぞお受け取りください"}  # 注文番号はロボットに届かないので使わない


# --- Watcher ------------------------------------------------------------------
def test_watcher_fresh_and_latest():
    w = Watcher()
    w.push({"data": True})
    never = lambda: None
    # fresh: 受信済みの値は使わない
    ok, _ = w.wait(lambda m: m["data"], since=w.count, timeout=0.1, interrupt=never)
    assert not ok
    # 直前の値でもよい
    ok, _ = w.wait(lambda m: m["data"], since=None, timeout=0.1, interrupt=never)
    assert ok
    # 間に別の値が来ても、条件を満たしたメッセージを見逃さない
    since = w.count
    w.push({"data": True})
    w.push({"data": False})
    ok, _ = w.wait(lambda m: m["data"], since=since, timeout=0.1, interrupt=never)
    assert ok


def test_watcher_interrupt():
    w = Watcher()
    t0 = time.monotonic()
    ok, reason = w.wait(lambda m: True, since=0, timeout=5.0, interrupt=lambda: "stop")
    assert (ok, reason) == (False, "stop") and time.monotonic() - t0 < 0.5


# --- 実行 ---------------------------------------------------------------------
def test_run_success():
    cfg = short_timeouts(load_config(CONFIG))
    bus = robot(FakeBus(cfg))
    steps = []
    res = HandoverRunner(cfg, bus, poll=0.01).run(order(3), on_step=lambda i, n, s: steps.append((i, n, s.label)))
    assert res.status == STATUS_SUCCESS, res.message
    assert [k for k, _ in bus.sent] == ["arm_cmd", "grip_cmd", "arm_cmd", "say", "grip_cmd", "say", "arm_cmd"]
    assert [f["data"] for k, f in bus.sent if k == "arm_cmd"] == ["counter", "present", "home"]
    assert steps[0] == (0, 8, "受渡し台へ") and steps[-1][0] == 7


def test_run_grasp_fails_then_abort():
    cfg = short_timeouts(load_config(CONFIG), 0.1)
    bus = robot(FakeBus(cfg), grasp_ok=False)
    aborted = []
    res = HandoverRunner(cfg, bus, poll=0.01).run(order(3), on_abort_step=lambda s: aborted.append(s.motion.name))
    assert res.status == STATUS_FAILED and "商品をつかむ" in res.message
    assert aborted == ["to_counter", "release", "home"]
    # 後始末: 台へ戻る → 開く → 初期姿勢
    after = bus.sent[2:]
    assert [(k, f["data"]) for k, f in after] == [("arm_cmd", "counter"), ("grip_cmd", 0.0), ("arm_cmd", "home")]


def test_run_not_taken_timeout():
    cfg = short_timeouts(load_config(CONFIG), 0.1)
    bus = robot(FakeBus(cfg), taken_after=None)
    res = HandoverRunner(cfg, bus, poll=0.01).run(order(3))
    assert res.status == STATUS_FAILED and "お受け取りください" in res.message


def test_on_timeout_continue():
    cfg = short_timeouts(load_config(CONFIG), 0.1)
    cfg.motions["wait_taken"].on_timeout = "continue"
    bus = robot(FakeBus(cfg), taken_after=None)
    assert HandoverRunner(cfg, bus, poll=0.01).run(order(3)).success


def test_run_cancel_runs_abort():
    cfg = load_config(CONFIG)
    bus = robot(FakeBus(cfg), taken_after=None)
    cancel = threading.Event()
    threading.Timer(0.2, cancel.set).start()
    t0 = time.monotonic()
    res = HandoverRunner(cfg, bus, poll=0.01).run(order(3), should_cancel=cancel.is_set)
    assert res.status == STATUS_CANCELED
    assert time.monotonic() - t0 < 2.0
    assert [f["data"] for k, f in bus.sent if k == "arm_cmd"][-2:] == ["counter", "home"]


def test_estop_stops_without_abort():
    cfg = load_config(CONFIG)
    bus = robot(FakeBus(cfg), taken_after=None)
    bus.push_later("estop", {"data": True}, 0.2)
    res = HandoverRunner(cfg, bus, poll=0.01).run(order(3))
    assert res.status == STATUS_FAILED and "非常停止" in res.message
    assert [f["data"] for k, f in bus.sent if k == "arm_cmd"] == ["counter", "present"]   # 後始末で動かさない


def test_can_accept():
    cfg = parse_config(yaml.safe_load(CONFIG.read_text(encoding="utf-8")) | {
        "ready": {"topic": "estop", "match": {"data": False}}})   # テスト用に estop を ready 代わりに使う
    bus = FakeBus(cfg)
    r = HandoverRunner(cfg, bus)
    assert r.can_accept()[0] is False          # ready 未受信
    bus.watchers["estop"].push({"data": False})
    assert r.can_accept() == (True, "")
    bus.watchers["estop"].push({"data": True})
    ok, reason = r.can_accept()
    assert not ok and "非常停止" in reason
