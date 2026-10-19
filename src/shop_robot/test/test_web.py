import time

import cv2
import numpy as np
from fastapi.testclient import TestClient

from shop_robot.core.aruco import MarkerDetector
from shop_robot.core.products import ProductStore
from shop_robot.web.app import create_app
from shop_robot.web.bridge import LocalBridge


def make(tmp_path, sec=0.05):
    products = ProductStore(tmp_path / "p.json")
    products.add(1, "たこ焼き", 300)
    products.add(2, "ラムネ", 150)
    bridge = LocalBridge(seconds_per_item=sec)
    return TestClient(create_app(bridge, products, "DICT_4X4_250")), bridge


def wait_state(c, state, timeout=3.0):
    t0 = time.time()
    while time.time() - t0 < timeout:
        q = c.get("/api/status").json()["queue"]
        if q["state"] == state:
            return q
        time.sleep(0.02)
    raise AssertionError(f"state {state} not reached: {q}")


def test_pages(tmp_path):
    c, _ = make(tmp_path)
    assert c.get("/").status_code == 200
    assert c.get("/display").status_code == 200
    assert c.get("/static/app.js").status_code == 200
    assert c.get("/api/config").json() == {"mode": "demo", "dict_name": "DICT_4X4_250", "marker_max": 249}


def test_order_flow(tmp_path):
    c, _ = make(tmp_path)
    assert c.get("/api/status").json()["next_number"] == 1
    r = c.post("/api/orders", json={"order_number": 12, "items": [
        {"product_id": 1, "quantity": 2}, {"product_id": 2, "quantity": 1}, {"product_id": 1, "quantity": 1}]})
    assert r.status_code == 200 and r.json()["delivered"]
    st = c.get("/api/status").json()
    assert st["next_number"] == 13
    w = st["queue"]["pending"][0]
    assert w["order_number"] == 12
    assert {(i["product_id"], i["product_name"], i["quantity"]) for i in w["items"]} == {(1, "たこ焼き", 3), (2, "ラムネ", 1)}

    # 重複番号・範囲外・未登録商品
    assert c.post("/api/orders", json={"order_number": 12, "items": [{"product_id": 1, "quantity": 1}]}).status_code == 409
    assert c.post("/api/orders", json={"order_number": 250, "items": [{"product_id": 1, "quantity": 1}]}).status_code == 400
    assert c.post("/api/orders", json={"order_number": 13, "items": [{"product_id": 9, "quantity": 1}]}).status_code == 400

    c.post("/api/orders", json={"order_number": 13, "items": [{"product_id": 2, "quantity": 1}]})
    # 完成前のマーカーは拒否され、お客様向けメッセージが出る
    r = c.post("/api/queue/inject_marker", json={"order_number": 12})
    assert r.status_code == 400 and "まだ準備中" in r.json()["detail"]
    q = c.get("/api/status").json()["queue"]
    assert "まだ準備中" in q["public_message"]

    # 13 を先に完成 → 呼び出し中 → マーカーでロボット動作 → 完了
    assert c.post("/api/queue/ready", json={"order_number": 13}).status_code == 200
    q = c.get("/api/status").json()["queue"]
    assert q["state"] == 2 and [o["order_number"] for o in q["ready"]] == [13]
    assert c.post("/api/queue/inject_marker", json={"order_number": 13}).status_code == 200
    q = wait_state(c, 1)  # 13 完了後、12 は準備中のまま
    assert q["recently_completed"] == [13] and q["pending"][0]["order_number"] == 12 and q["ready"] == []
    # 呼び出し中の番号も next_number で飛ばす
    c.post("/api/queue/ready", json={"order_number": 12})
    c.post("/api/orders", json={"order_number": 14, "items": [{"product_id": 2, "quantity": 1}]})
    assert c.get("/api/status").json()["next_number"] == 15


def test_cancel_active_and_unready(tmp_path):
    c, _ = make(tmp_path, sec=0.5)
    for n in (1, 2, 3):
        c.post("/api/orders", json={"order_number": n, "items": [{"product_id": 1, "quantity": 3}]})
    c.post("/api/queue/ready", json={"order_number": 1})
    c.post("/api/queue/unready", json={"order_number": 1})
    assert c.post("/api/queue/unready", json={"order_number": 1}).status_code == 400
    c.post("/api/queue/ready", json={"order_number": 2})
    c.post("/api/queue/force_start", json={"order_number": 2})
    assert c.get("/api/status").json()["queue"]["state"] == 3
    assert c.post("/api/queue/force_start", json={"order_number": 1}).status_code == 400  # 呼び出し中でない
    c.post("/api/queue/cancel", json={"order_number": 2})
    q = wait_state(c, 1)
    assert [o["order_number"] for o in q["pending"]] == [1, 3] and q["recently_completed"] == []
    assert c.post("/api/queue/postpone", json={"order_number": 1}).status_code == 404


def test_products_api(tmp_path):
    c, _ = make(tmp_path)
    assert c.post("/api/products", json={"id": 5, "name": "かき氷", "price": 200}).status_code == 200
    assert c.post("/api/products", json={"id": 5, "name": "x"}).status_code == 400
    assert c.put("/api/products/5", json={"id": 5, "name": "かき氷(大)", "price": 300}).json()["name"] == "かき氷(大)"
    assert c.delete("/api/products/5").status_code == 200
    assert [p["id"] for p in c.get("/api/products").json()] == [1, 2]


def test_marker_png_is_detectable(tmp_path):
    c, _ = make(tmp_path)
    png = c.get("/api/marker/77.png").content
    img = cv2.imdecode(np.frombuffer(png, np.uint8), cv2.IMREAD_GRAYSCALE)
    assert MarkerDetector("DICT_4X4_250").detect(img)[0] == [77]


def test_numbering_reset_and_clear_all(tmp_path):
    c, _ = make(tmp_path, sec=0.5)
    for n in (5, 6):
        c.post("/api/orders", json={"order_number": n, "items": [{"product_id": 1, "quantity": 2}]})
    assert c.get("/api/status").json()["next_number"] == 7
    # 1番から振り直し（受付中の番号は飛ばす）
    assert c.post("/api/numbering", json={"next_number": 5}).json()["next_number"] == 7
    assert c.post("/api/numbering", json={"next_number": 1}).json()["next_number"] == 1
    assert c.post("/api/numbering", json={"next_number": 250}).status_code == 400
    # 再起動しても続きから
    c2, _ = make_again(tmp_path)
    assert c2.get("/api/status").json()["next_number"] == 1

    # 全注文クリア（動作中も含む）
    c.post("/api/queue/ready", json={"order_number": 5})
    c.post("/api/queue/force_start", json={"order_number": 5})
    assert c.post("/api/queue/clear_all", json={}).status_code == 200
    q = wait_state(c, 0)
    assert q["pending"] == [] and q["ready"] == [] and q["recently_completed"] == []


def make_again(tmp_path):
    products = ProductStore(tmp_path / "p.json")
    return TestClient(create_app(LocalBridge(), products, "DICT_4X4_250")), None


def test_no_cache_headers(tmp_path):
    c, _ = make(tmp_path)
    for path in ("/", "/display", "/static/app.js", "/api/status"):
        assert c.get(path).headers["cache-control"] == "no-cache"
    assert "app.js?v=" in c.get("/").text
