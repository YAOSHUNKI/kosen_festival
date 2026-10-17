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
    w = st["queue"]["waiting"][0]
    assert w["order_number"] == 12
    assert {(i["product_id"], i["product_name"], i["quantity"]) for i in w["items"]} == {(1, "たこ焼き", 3), (2, "ラムネ", 1)}

    # 重複番号・範囲外・未登録商品
    assert c.post("/api/orders", json={"order_number": 12, "items": [{"product_id": 1, "quantity": 1}]}).status_code == 409
    assert c.post("/api/orders", json={"order_number": 250, "items": [{"product_id": 1, "quantity": 1}]}).status_code == 400
    assert c.post("/api/orders", json={"order_number": 13, "items": [{"product_id": 9, "quantity": 1}]}).status_code == 400

    c.post("/api/orders", json={"order_number": 13, "items": [{"product_id": 2, "quantity": 1}]})
    # 順番違いのマーカーは拒否
    r = c.post("/api/queue/inject_marker", json={"order_number": 13})
    assert r.status_code == 400 and "まだ順番" in r.json()["detail"]
    # 先頭のマーカーで準備開始 → 完了
    assert c.post("/api/queue/inject_marker", json={"order_number": 12}).status_code == 200
    q = wait_state(c, 1)  # 12 完了後、13 のマーカー待ち
    assert q["recently_completed"] == [12] and q["waiting"][0]["order_number"] == 13


def test_cancel_active_and_postpone(tmp_path):
    c, _ = make(tmp_path, sec=0.5)
    for n in (1, 2, 3):
        c.post("/api/orders", json={"order_number": n, "items": [{"product_id": 1, "quantity": 3}]})
    c.post("/api/queue/postpone", json={"order_number": 1})
    q = c.get("/api/status").json()["queue"]
    assert [o["order_number"] for o in q["waiting"]] == [2, 3, 1]
    c.post("/api/queue/force_start", json={"order_number": 2})
    assert c.get("/api/status").json()["queue"]["state"] == 2
    c.post("/api/queue/cancel", json={"order_number": 2})
    q = wait_state(c, 1)
    assert [o["order_number"] for o in q["waiting"]] == [3, 1] and q["recently_completed"] == []


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
