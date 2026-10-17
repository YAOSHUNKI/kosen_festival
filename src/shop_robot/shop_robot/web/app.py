"""注文受付 Web アプリ（FastAPI）。

  /         スタッフ用（注文登録・待ち行列操作・商品管理）
  /display  お客様向けの呼び出し表示（別モニタやタブレットで全画面表示）
"""
from __future__ import annotations

import threading
from pathlib import Path

import cv2
from fastapi import FastAPI, HTTPException, Response
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from ..core.aruco import dictionary_size, generate_marker
from ..core.models import Order, OrderItem
from ..core.order_queue import QueueError
from ..core.products import ProductError, ProductStore
from .bridge import COMMANDS, Bridge

STATIC_DIR = Path(__file__).parent / "static"


class ProductIn(BaseModel):
    id: int = Field(ge=0)
    name: str
    price: int = Field(default=0, ge=0)


class OrderItemIn(BaseModel):
    product_id: int
    quantity: int = Field(ge=1, le=99)


class OrderIn(BaseModel):
    order_number: int = Field(ge=0)
    items: list[OrderItemIn]


class ControlIn(BaseModel):
    order_number: int = Field(ge=0)


def create_app(bridge: Bridge, products: ProductStore, dict_name: str = "DICT_4X4_1000") -> FastAPI:
    marker_count = dictionary_size(dict_name)
    app = FastAPI(title="店舗注文システム")
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
    last_number = {"n": None}
    submit_lock = threading.Lock()

    def in_queue(status: dict | None) -> set[int]:
        if not status:
            return set()
        nums = {o["order_number"] for o in status["waiting"]}
        if status.get("active"):
            nums.add(status["active"]["order_number"])
        return nums

    def next_number(status: dict | None) -> int:
        """次に使う番号の候補（直前の番号+1、受付中の番号は飛ばす）"""
        used = in_queue(status)
        n = 1 if last_number["n"] is None else last_number["n"] + 1
        for _ in range(marker_count):
            n %= marker_count
            if n not in used:
                return n
            n += 1
        return n % marker_count

    # --- 画面 -----------------------------------------------------------------
    @app.get("/", include_in_schema=False)
    def staff_page():
        return FileResponse(STATIC_DIR / "index.html")

    @app.get("/display", include_in_schema=False)
    def display_page():
        return FileResponse(STATIC_DIR / "display.html")

    # --- 設定・状態 -------------------------------------------------------------
    @app.get("/api/config")
    def config():
        return {"mode": bridge.mode, "dict_name": dict_name, "marker_max": marker_count - 1}

    @app.get("/api/status")
    def status():
        st = bridge.status()
        return {"connected": st is not None, "queue": st, "next_number": next_number(st)}

    # --- 商品 -----------------------------------------------------------------
    @app.get("/api/products")
    def list_products():
        return [p.__dict__ for p in products.list()]

    @app.post("/api/products")
    def add_product(p: ProductIn):
        try:
            return products.add(p.id, p.name, p.price).__dict__
        except ProductError as e:
            raise HTTPException(400, str(e))

    @app.put("/api/products/{product_id}")
    def update_product(product_id: int, p: ProductIn):
        try:
            return products.update(product_id, p.name, p.price).__dict__
        except ProductError as e:
            raise HTTPException(400, str(e))

    @app.delete("/api/products/{product_id}")
    def delete_product(product_id: int):
        try:
            products.delete(product_id)
        except ProductError as e:
            raise HTTPException(404, str(e))
        return {"ok": True}

    # --- 注文 -----------------------------------------------------------------
    @app.post("/api/orders")
    def submit_order(body: OrderIn):
        if body.order_number >= marker_count:
            raise HTTPException(400, f"注文番号は 0〜{marker_count - 1} にしてください（{dict_name}）")
        if not body.items:
            raise HTTPException(400, "商品を選んでください")
        merged: dict[int, int] = {}
        for it in body.items:
            merged[it.product_id] = merged.get(it.product_id, 0) + it.quantity
        items = []
        for pid, qty in merged.items():
            p = products.get(pid)
            if p is None:
                raise HTTPException(400, f"商品ID {pid} は登録されていません")
            items.append(OrderItem(pid, p.name, qty))

        with submit_lock:  # 2台のタブレットから同じ番号を同時送信したときの重複防止
            st = bridge.status()
            if body.order_number in in_queue(st):
                raise HTTPException(409, f"注文番号 {body.order_number} は既に受付中です")
            try:
                delivered = bridge.submit_order(Order(body.order_number, items))
            except QueueError as e:
                raise HTTPException(409, str(e))
            last_number["n"] = body.order_number
        return {"ok": True, "delivered": delivered, "order_number": body.order_number}

    @app.post("/api/queue/{command}")
    def queue_control(command: str, body: ControlIn):
        if command not in COMMANDS:
            raise HTTPException(404, f"不明な操作: {command}")
        ok, msg = bridge.control(command, body.order_number)
        if not ok:
            raise HTTPException(400, msg)
        return {"ok": True, "message": msg}

    # --- マーカー画像（番号札の確認・個別印刷用） ------------------------------------
    @app.get("/api/marker/{marker_id}.png")
    def marker_png(marker_id: int, size: int = 400):
        if not 0 <= marker_id < marker_count:
            raise HTTPException(404, "範囲外の番号です")
        size = max(100, min(size, 1200))
        img = generate_marker(dict_name, marker_id, size)
        border = size // 8  # 認識には白い余白（クワイエットゾーン）が必要
        img = cv2.copyMakeBorder(img, border, border, border, border, cv2.BORDER_CONSTANT, value=255)
        ok, png = cv2.imencode(".png", img)
        return Response(png.tobytes(), media_type="image/png")

    return app
