"""注文受付 Web アプリ（FastAPI）。

  /         スタッフ用（注文登録・待ち行列操作・商品管理）
  /display  お客様向けの呼び出し表示（別モニタやタブレットで全画面表示）
"""
from __future__ import annotations

import asyncio
import copy
import json
import threading
from pathlib import Path

import cv2
from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from ..core.aruco import dictionary_size, generate_marker
from ..core.codec import COMMANDS
from ..core.models import Order, OrderItem
from ..core.order_queue import QueueError
from ..core.products import ProductError, ProductStore
from .bridge import Bridge

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
    order_number: int = Field(default=0, ge=0)


class NumberingIn(BaseModel):
    next_number: int = Field(ge=0)


class NumberCounter:
    """次の番号札の候補。products.json と同じフォルダに保存し、再起動しても続きから振る"""

    def __init__(self, path: Path):
        self.path = path
        self.base = 1
        try:
            self.base = int(json.loads(path.read_text(encoding="utf-8"))["next"])
        except (OSError, ValueError, KeyError, TypeError):
            pass

    def set(self, n: int) -> None:
        self.base = n
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(json.dumps({"next": n}), encoding="utf-8")
        except OSError:
            pass  # 保存できなくても動作は続ける


class NoCacheMiddleware:
    """更新後に古い画面・スクリプトが残らないよう、毎回サーバに確認させる（変化なしなら 304 で軽い）。
    カメラのストリーミングを妨げないよう、素の ASGI ミドルウェアとして実装している。"""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)

        async def send_wrapper(message):
            if message["type"] == "http.response.start":
                headers = [(k, v) for k, v in message.get("headers", []) if k.lower() != b"cache-control"]
                headers.append((b"cache-control", b"no-cache"))
                message["headers"] = headers
            await send(message)

        await self.app(scope, receive, send_wrapper)


def create_app(bridge: Bridge, products: ProductStore, dict_name: str = "DICT_4X4_1000") -> FastAPI:
    marker_count = dictionary_size(dict_name)
    app = FastAPI(title="店舗注文システム")
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

    app.add_middleware(NoCacheMiddleware)
    counter = NumberCounter(products.path.parent / "numbering.json")
    submit_lock = threading.Lock()

    def in_queue(status: dict | None) -> set[int]:
        if not status:
            return set()
        nums = {o["order_number"] for o in status["pending"] + status["ready"]}
        if status.get("active"):
            nums.add(status["active"]["order_number"])
        return nums

    def next_number(status: dict | None) -> int:
        """次に使う番号の候補（直前の番号+1、受付中の番号は飛ばす）"""
        used = in_queue(status)
        n = counter.base
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

    def with_names(st: dict | None) -> dict | None:
        """ROS 経由の注文は商品IDしか持たないので、商品名を商品マスタから埋める"""
        if not st:
            return st
        st = copy.deepcopy(st)
        orders = st["pending"] + st["ready"] + ([st["active"]] if st.get("active") else [])
        for o in orders:
            for it in o["items"]:
                if not it.get("product_name"):
                    p = products.get(it["product_id"])
                    it["product_name"] = p.name if p else f"ID{it['product_id']}"
        return st

    @app.get("/api/status")
    def status():
        st = with_names(bridge.status())
        return {"connected": st is not None, "queue": st, "next_number": next_number(st),
                "camera": bridge.camera.get() is not None}

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
            counter.set((body.order_number + 1) % marker_count)
        return {"ok": True, "delivered": delivered, "order_number": body.order_number}

    @app.post("/api/numbering")
    def set_numbering(body: NumberingIn):
        """番号札の振り直し（次の候補を next_number にする）"""
        if body.next_number >= marker_count:
            raise HTTPException(400, f"番号は 0〜{marker_count - 1} にしてください")
        counter.set(body.next_number)
        return {"ok": True, "next_number": next_number(bridge.status())}

    @app.post("/api/queue/{command}")
    def queue_control(command: str, body: ControlIn):
        if command not in COMMANDS:
            raise HTTPException(404, f"不明な操作: {command}")
        ok, msg = bridge.control(command, body.order_number)
        if not ok:
            raise HTTPException(400, msg)
        return {"ok": True, "message": msg}

    # --- カメラ映像（検出結果入りのプレビュー） ------------------------------------------
    @app.get("/api/camera.jpg")
    def camera_snapshot():
        f = bridge.camera.get()
        if f is None:
            raise HTTPException(404, "カメラ映像が届いていません")
        return Response(f[0], media_type="image/jpeg")

    @app.get("/api/camera.mjpg")
    async def camera_stream(request: Request, max_frames: int = 0):
        """MJPEG ストリーム（<img src> でそのまま動画として表示できる）。max_frames はテスト用"""
        async def frames():
            last, sent = None, 0
            while not await request.is_disconnected():
                f = bridge.camera.get()
                if f is not None and f[1] != last:
                    last = f[1]
                    yield (b"--frame\r\nContent-Type: image/jpeg\r\nContent-Length: "
                           + str(len(f[0])).encode() + b"\r\n\r\n" + f[0] + b"\r\n")
                    sent += 1
                    if max_frames and sent >= max_frames:
                        return
                await asyncio.sleep(0.03)

        return StreamingResponse(frames(), media_type="multipart/x-mixed-replace; boundary=frame")

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
