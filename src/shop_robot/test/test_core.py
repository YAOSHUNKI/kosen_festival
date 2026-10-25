import cv2
import numpy as np
import pytest

from shop_robot.core.aruco import MarkerDebouncer, MarkerDetector, generate_marker
from shop_robot.core.models import Order, OrderItem, STATE_CALLING, STATE_IDLE, STATE_PENDING, STATE_ROBOT_BUSY
from shop_robot.core.order_queue import (
    OrderQueue, QueueError, RESULT_CANCELED, RESULT_FAILED, RESULT_SUCCESS,
)
from shop_robot.core.products import ProductError, ProductStore


def order(n, pid=1, qty=1):
    return Order(n, [OrderItem(pid, f"p{pid}", qty)])


# --- 商品 -------------------------------------------------------------------
def test_products_persist(tmp_path):
    path = tmp_path / "products.json"
    s = ProductStore(path)
    s.add(10, "たこ焼き", 300)
    s.add(2, "フランクフルト")
    with pytest.raises(ProductError):
        s.add(10, "重複")
    s.update(2, "フランク", 200)
    s2 = ProductStore(path)
    assert [(p.id, p.name, p.price) for p in s2.list()] == [(2, "フランク", 200), (10, "たこ焼き", 300)]
    s2.delete(10)
    assert [p.id for p in ProductStore(path).list()] == [2]


# --- キュー -----------------------------------------------------------------
def test_queue_flow():
    q = OrderQueue()
    assert q.state == STATE_IDLE
    q.add(order(5))
    q.add(order(3))
    with pytest.raises(QueueError):
        q.add(order(5))
    assert q.state == STATE_PENDING

    # 完成前のマーカーは受け付けない
    ok, msg = q.check_marker(5)
    assert not ok and "まだ準備中" in msg
    ok, _ = q.check_marker(99)
    assert not ok

    # 完成は順不同。完成した番号だけ呼び出し中になる
    q.mark_ready(3)
    assert q.state == STATE_CALLING
    assert [o.order_number for o in q.ready] == [3] and [o.order_number for o in q.pending] == [5]
    assert q.public_message.startswith("3番") and q.public_seq == 1
    with pytest.raises(QueueError):
        q.mark_ready(3)  # 既に呼び出し中

    ok, _ = q.check_marker(3)
    assert ok
    assert q.start(3).order_number == 3
    assert q.state == STATE_ROBOT_BUSY
    q.mark_ready(5)
    ok, msg = q.check_marker(5)  # ロボット動作中は次を受け付けない
    assert not ok and "前のお客様" in msg
    with pytest.raises(QueueError):
        q.add(order(3))  # 動作中の番号も重複扱い

    q.finish_active(RESULT_SUCCESS)
    assert q.completed == [3]
    assert q.state == STATE_CALLING and q.check_marker(5)[0]


def test_queue_unready_failure_cancel():
    q = OrderQueue()
    for n in (1, 2, 3):
        q.add(order(n))
    q.mark_ready(2)
    q.unmark_ready(2)  # 押し間違い → 受付順に戻る
    assert [o.order_number for o in q.pending] == [1, 2, 3] and q.ready == []

    q.mark_ready(1)
    q.mark_ready(2)
    q.start(2)
    q.finish_active(RESULT_FAILED, "把持失敗")
    assert [o.order_number for o in q.ready] == [2, 1]  # 失敗は呼び出し中の先頭に戻る
    assert "把持失敗" in q.last_event

    assert q.cancel(3) == "waiting"
    assert q.cancel(1) == "waiting"
    q.start(2)
    assert q.cancel(2) == "active"  # 動作中は呼び出し側でロボットを止める
    q.finish_active(RESULT_CANCELED)
    assert q.state == STATE_IDLE and q.completed == []
    with pytest.raises(QueueError):
        q.start(2)


def test_queue_validation():
    q = OrderQueue()
    with pytest.raises(QueueError):
        q.add(Order(1, []))
    with pytest.raises(QueueError):
        q.add(order(1, qty=0))


def test_queue_persistence(tmp_path):
    path = tmp_path / "queue.json"
    q = OrderQueue(path)
    for n in (7, 8, 9):
        q.add(order(n))
    q.mark_ready(7)
    q.mark_ready(8)
    q.start(8)  # 8 を動作中のまま「クラッシュ」
    q2 = OrderQueue(path)
    assert [o.order_number for o in q2.ready] == [8, 7]
    assert [o.order_number for o in q2.pending] == [9]
    assert q2.active is None


def test_queue_loads_old_format(tmp_path):
    path = tmp_path / "queue.json"
    path.write_text('{"waiting": [{"order_number": 4, "items": [{"product_id": 1, "quantity": 1}]}], "active": null}')
    assert [o.order_number for o in OrderQueue(path).pending] == [4]


# --- ArUco ------------------------------------------------------------------
def _scene(marker_id, dict_name="DICT_4X4_1000"):
    m = generate_marker(dict_name, marker_id, 200)
    img = np.full((480, 640), 255, np.uint8)
    img[140:340, 220:420] = m
    # 手でかざした状態を想定して少し回転・ぼかし
    rot = cv2.getRotationMatrix2D((320, 240), 17, 0.8)
    img = cv2.warpAffine(img, rot, (640, 480), borderValue=255)
    return cv2.cvtColor(cv2.GaussianBlur(img, (3, 3), 0), cv2.COLOR_GRAY2BGR)


@pytest.mark.parametrize("marker_id", [0, 1, 42, 999])
def test_marker_detect(marker_id):
    ids, corners = MarkerDetector("DICT_4X4_1000").detect(_scene(marker_id))
    assert ids == [marker_id] and len(corners) == 1


def test_debouncer():
    d = MarkerDebouncer(min_frames=3, cooldown_s=2.0)
    assert d.update([4], 0.0) == []
    assert d.update([4], 0.1) == []
    assert d.update([4], 0.2) == [4]
    assert d.update([4], 0.3) == []          # クールダウン中
    assert d.update([], 0.4) == []           # 途切れたらリセット
    assert d.update([4], 0.5) == [] and d.update([4], 0.6) == []
    assert d.update([4], 2.3) == [4]         # 3フレーム連続 & クールダウン明け


def test_queue_clear_all(tmp_path):
    path = tmp_path / "queue.json"
    q = OrderQueue(path)
    for n in (1, 2, 3):
        q.add(order(n))
    q.mark_ready(1)
    q.mark_ready(2)
    q.start(2)
    q.finish_active(RESULT_SUCCESS)
    assert q.clear_all() is False
    assert q.state == STATE_IDLE and q.completed == [] and q.pending == []
    assert OrderQueue(path).pending == []  # 保存にも反映
    # ロボット動作中は True（呼び出し側がキャンセル）
    q.add(order(4)); q.mark_ready(4); q.start(4)
    assert q.clear_all() is True
    q.finish_active(RESULT_CANCELED)
    assert q.state == STATE_IDLE


def test_products_robot_id(tmp_path):
    path = tmp_path / "products.json"
    s = ProductStore(path)
    s.add(1, "たこ焼き", 300)
    s.add(11, "たこ焼き(割引)", 250, robot_id=1)
    s.add(12, "そのまま", 100, robot_id=12)        # 商品IDと同じなら持たない
    s2 = ProductStore(path)
    assert [(p.id, p.robot_id, p.send_id) for p in s2.list()] == [(1, None, 1), (11, 1, 1), (12, None, 12)]
    s2.update(11, "たこ焼き(割引)", 250, robot_id=None)  # 空にすると商品IDで送る
    assert ProductStore(path).get(11).send_id == 11
    with pytest.raises(ProductError):
        s2.add(13, "x", 0, robot_id=-1)
