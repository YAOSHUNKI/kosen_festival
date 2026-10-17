import cv2
import numpy as np
import pytest

from shop_robot.core.aruco import MarkerDebouncer, MarkerDetector, generate_marker
from shop_robot.core.models import Order, OrderItem, STATE_IDLE, STATE_PREPARING, STATE_WAITING_MARKER
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
    assert q.state == STATE_WAITING_MARKER

    ok, msg = q.check_marker(3)
    assert not ok and "まだ順番ではありません" in msg
    ok, _ = q.check_marker(99)
    assert not ok
    ok, _ = q.check_marker(5)
    assert ok
    assert q.start_head(5).order_number == 5
    assert q.state == STATE_PREPARING
    ok, msg = q.check_marker(3)  # 準備中は次を受け付けない
    assert not ok and "準備中" in msg
    with pytest.raises(QueueError):
        q.add(order(5))  # 準備中の番号も重複扱い

    q.finish_active(RESULT_SUCCESS)
    assert q.completed == [5]
    assert q.head.order_number == 3


def test_queue_failure_returns_to_head_and_cancel():
    q = OrderQueue()
    for n in (1, 2, 3):
        q.add(order(n))
    q.start_head()
    q.finish_active(RESULT_FAILED, "把持失敗")
    assert [o.order_number for o in q.waiting] == [1, 2, 3]

    q.postpone(1)
    assert [o.order_number for o in q.waiting] == [2, 3, 1]
    assert q.cancel(3) == "waiting"
    q.start_head()
    assert q.cancel(2) == "active"  # 準備中は呼び出し側でロボットを止める
    q.finish_active(RESULT_CANCELED)
    assert [o.order_number for o in q.waiting] == [1] and q.completed == []


def test_queue_validation():
    q = OrderQueue()
    with pytest.raises(QueueError):
        q.add(Order(1, []))
    with pytest.raises(QueueError):
        q.add(order(1, qty=0))


def test_queue_persistence(tmp_path):
    path = tmp_path / "queue.json"
    q = OrderQueue(path)
    q.add(order(7))
    q.add(order(8))
    q.start_head()  # 7 を準備中のまま「クラッシュ」
    q2 = OrderQueue(path)
    assert [o.order_number for o in q2.waiting] == [7, 8]
    assert q2.active is None


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
