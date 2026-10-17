"""注文キューの状態機械（ROS 非依存）。

    IDLE ──add──▶ WAITING_MARKER ──先頭のマーカー検出 / 強制開始──▶ PREPARING
      ▲                 ▲                                              │
      └── 待ちが空 ─────┴──────────── 完了 / 失敗(先頭に戻す) / 取消 ───┘

・先頭の注文番号と一致するマーカーを見たときだけ準備を開始する
・準備中は次のマーカーを受け付けない（完了後に次の番号を呼び出す）
・状態は JSON に保存し、ノード再起動後も待ち行列を復元する
"""
from __future__ import annotations

import json
import os
import tempfile
import threading
from collections import deque
from pathlib import Path

from .models import Order, STATE_IDLE, STATE_PREPARING, STATE_WAITING_MARKER

RESULT_SUCCESS = "success"
RESULT_FAILED = "failed"
RESULT_CANCELED = "canceled"


class QueueError(ValueError):
    pass


class OrderQueue:
    def __init__(self, state_path: str | os.PathLike | None = None, max_completed: int = 10):
        self._lock = threading.RLock()
        self._waiting: list[Order] = []
        self._active: Order | None = None
        self._completed: deque[int] = deque(maxlen=max_completed)
        self.progress = 0.0
        self.progress_text = ""
        self.last_event = ""
        self._path = Path(state_path).expanduser() if state_path else None
        if self._path and self._path.exists():
            self._load()

    # --- 参照 ---------------------------------------------------------------
    @property
    def state(self) -> int:
        with self._lock:
            if self._active is not None:
                return STATE_PREPARING
            return STATE_WAITING_MARKER if self._waiting else STATE_IDLE

    @property
    def waiting(self) -> list[Order]:
        with self._lock:
            return list(self._waiting)

    @property
    def active(self) -> Order | None:
        return self._active

    @property
    def head(self) -> Order | None:
        with self._lock:
            return self._waiting[0] if self._waiting else None

    @property
    def completed(self) -> list[int]:
        """最近完了した注文番号（新しい順）"""
        with self._lock:
            return list(reversed(self._completed))

    def contains(self, order_number: int) -> bool:
        with self._lock:
            return (self._active is not None and self._active.order_number == order_number) or any(
                o.order_number == order_number for o in self._waiting
            )

    def snapshot(self) -> dict:
        with self._lock:
            return {
                "state": self.state,
                "active": self._active.to_dict() if self._active else None,
                "progress": self.progress,
                "progress_text": self.progress_text,
                "waiting": [o.to_dict() for o in self._waiting],
                "recently_completed": self.completed,
                "last_event": self.last_event,
            }

    # --- 注文の追加・スタッフ操作 ---------------------------------------------
    def add(self, order: Order) -> None:
        if not order.items:
            raise QueueError("商品が1つも入っていません")
        if any(i.quantity <= 0 for i in order.items):
            raise QueueError("個数は1以上にしてください")
        with self._lock:
            if self.contains(order.order_number):
                raise QueueError(f"注文番号 {order.order_number} は既に受付中です")
            self._waiting.append(order)
            self._event(f"{order.order_number}番の注文を受け付けました（{order.summary()}）")

    def cancel(self, order_number: int) -> str:
        """待ち注文は即削除して 'waiting' を返す。準備中なら 'active' を返す
        （呼び出し側がロボット動作をキャンセルし、終了時に finish_active(CANCELED) を呼ぶ）。"""
        with self._lock:
            if self._active is not None and self._active.order_number == order_number:
                return "active"
            for i, o in enumerate(self._waiting):
                if o.order_number == order_number:
                    del self._waiting[i]
                    self._event(f"{order_number}番の注文を取り消しました")
                    return "waiting"
            raise QueueError(f"注文番号 {order_number} は受付中にありません")

    def postpone(self, order_number: int) -> None:
        """待ち注文を最後尾に回す（呼び出してもお客様が来ないとき用）"""
        with self._lock:
            for i, o in enumerate(self._waiting):
                if o.order_number == order_number:
                    self._waiting.append(self._waiting.pop(i))
                    self._event(f"{order_number}番を後回しにしました")
                    return
            raise QueueError(f"注文番号 {order_number} は待ち行列にありません")

    # --- マーカーと準備の進行 -------------------------------------------------
    def check_marker(self, marker_id: int) -> tuple[bool, str]:
        """マーカーIDで準備を開始してよいか判定する（状態は変えない）。"""
        with self._lock:
            if self._active is not None:
                if self._active.order_number == marker_id:
                    return False, f"{marker_id}番は準備中です"
                if self.contains(marker_id):
                    return False, f"{marker_id}番はまだ順番ではありません（現在 {self._active.order_number}番を準備中）"
                return False, f"{marker_id}番の注文は受付中にありません"
            if not self._waiting:
                return False, f"{marker_id}番の注文は受付中にありません"
            head = self._waiting[0].order_number
            if marker_id == head:
                return True, ""
            if self.contains(marker_id):
                return False, f"{marker_id}番はまだ順番ではありません（現在 {head}番）"
            return False, f"{marker_id}番の注文は受付中にありません"

    def start_head(self, expected_number: int | None = None) -> Order:
        with self._lock:
            if self._active is not None:
                raise QueueError(f"{self._active.order_number}番を準備中です")
            if not self._waiting:
                raise QueueError("待ち注文がありません")
            if expected_number is not None and self._waiting[0].order_number != expected_number:
                raise QueueError(f"先頭は {self._waiting[0].order_number}番です")
            self._active = self._waiting.pop(0)
            self.progress, self.progress_text = 0.0, "準備開始"
            self._event(f"{self._active.order_number}番の準備を開始しました")
            return self._active

    def set_progress(self, progress: float, text: str = "") -> None:
        with self._lock:
            self.progress = max(0.0, min(1.0, float(progress)))
            self.progress_text = text
        # 進捗は頻繁に来るので保存しない

    def finish_active(self, result: str, message: str = "") -> Order:
        with self._lock:
            if self._active is None:
                raise QueueError("準備中の注文がありません")
            order, self._active = self._active, None
            self.progress, self.progress_text = 0.0, ""
            n = order.order_number
            if result == RESULT_SUCCESS:
                self._completed.append(n)
                self._event(f"{n}番のご用意ができました")
            elif result == RESULT_CANCELED:
                self._event(f"{n}番の準備を取り消しました")
            else:  # 失敗: 先頭に戻してスタッフが再開できるようにする
                self._waiting.insert(0, order)
                self._event(f"{n}番の準備に失敗しました（{message or '原因不明'}）。先頭に戻しました")
            return order

    def requeue_active(self, reason: str) -> None:
        """準備開始できなかった（ロボット応答なし等）注文を先頭に戻す"""
        with self._lock:
            if self._active is None:
                return
            self._waiting.insert(0, self._active)
            n = self._active.order_number
            self._active = None
            self.progress, self.progress_text = 0.0, ""
            self._event(f"{n}番を開始できませんでした（{reason}）")

    def note(self, text: str) -> None:
        """画面に出すイベントだけ更新する（マーカーが順番違いのとき等）"""
        with self._lock:
            self.last_event = text

    # --- 永続化 -------------------------------------------------------------
    def _event(self, text: str) -> None:
        self.last_event = text
        self._save()

    def _save(self) -> None:
        if not self._path:
            return
        with self._lock:
            data = {
                "waiting": [o.to_dict() for o in self._waiting],
                "active": self._active.to_dict() if self._active else None,
                "completed": list(self._completed),
            }
        self._path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=self._path.parent, suffix=".tmp")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, self._path)

    def _load(self) -> None:
        data = json.loads(self._path.read_text(encoding="utf-8"))
        self._waiting = [Order.from_dict(d) for d in data.get("waiting", [])]
        # 準備途中で落ちた注文は先頭に戻す（ロボットの状態が不明なのでスタッフ判断で再開）
        if data.get("active"):
            self._waiting.insert(0, Order.from_dict(data["active"]))
        self._completed.extend(int(n) for n in data.get("completed", []))
        if self._waiting:
            self.last_event = f"前回の待ち注文 {len(self._waiting)}件を復元しました"
