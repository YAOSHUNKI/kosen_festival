"""カメラ確認用のプレビュー画像（検出結果の重ね描き + JPEG 化）。ROS 非依存。"""
from __future__ import annotations

import cv2
import numpy as np

# マーカー枠の色（BGR）。キューの状態で色を変えて、かざした番号が受け付けられるか一目でわかるようにする
COLOR_READY = (80, 200, 60)      # 緑: 呼び出し中 → 受け付ける
COLOR_WAIT = (0, 160, 255)       # 橙: 準備中 / ロボット動作中
COLOR_UNKNOWN = (60, 60, 230)    # 赤: 受付中にない番号


def marker_color(marker_id: int, ready: set[int], waiting: set[int]) -> tuple[int, int, int]:
    if marker_id in ready:
        return COLOR_READY
    if marker_id in waiting:
        return COLOR_WAIT
    return COLOR_UNKNOWN


def annotate(frame: np.ndarray, ids: list[int], corners: list[np.ndarray],
             ready: set[int] = frozenset(), waiting: set[int] = frozenset(),
             mirror: bool = False) -> np.ndarray:
    """検出したマーカーに枠と番号を描く。mirror=True なら鏡像（お客様向け表示で自然な向き）にする。
    検出は必ず元画像で行い（鏡像だとマーカーが読めない）、描画だけ反転後の座標で行う。"""
    out = cv2.flip(frame, 1) if mirror else frame.copy()
    w = out.shape[1]
    thick = max(2, w // 200)
    for marker_id, c in zip(ids, corners):
        pts = c.reshape(-1, 2).astype(np.float32).copy()
        if mirror:
            pts[:, 0] = w - 1 - pts[:, 0]
        color = marker_color(marker_id, ready, waiting)
        cv2.polylines(out, [pts.astype(np.int32)], True, color, thick, cv2.LINE_AA)
        label = f"#{marker_id}"
        scale = max(0.8, w / 640 * 1.1)
        (tw, th), _ = cv2.getTextSize(label, cv2.FONT_HERSHEY_SIMPLEX, scale, thick)
        x = int(pts[:, 0].min())
        y = int(pts[:, 1].min()) - 10
        if y - th < 0:
            y = int(pts[:, 1].max()) + th + 10
        cv2.rectangle(out, (x - 4, y - th - 6), (x + tw + 4, y + 6), color, -1)
        cv2.putText(out, label, (x, y), cv2.FONT_HERSHEY_SIMPLEX, scale, (255, 255, 255), thick, cv2.LINE_AA)
    return out


def encode_jpeg(frame: np.ndarray, max_width: int = 640, quality: int = 70) -> bytes:
    if frame.shape[1] > max_width:
        h = int(frame.shape[0] * max_width / frame.shape[1])
        frame = cv2.resize(frame, (max_width, h), interpolation=cv2.INTER_AREA)
    ok, buf = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, int(quality)])
    if not ok:
        raise RuntimeError("JPEG エンコードに失敗しました")
    return buf.tobytes()


def queue_sets(snapshot: dict | None) -> tuple[set[int], set[int]]:
    """キュー状態から (呼び出し中の番号, 準備中・動作中の番号) を取り出す"""
    if not snapshot:
        return set(), set()
    ready = {o["order_number"] for o in snapshot["ready"]}
    waiting = {o["order_number"] for o in snapshot["pending"]}
    if snapshot.get("active"):
        waiting.add(snapshot["active"]["order_number"])
    return ready, waiting
