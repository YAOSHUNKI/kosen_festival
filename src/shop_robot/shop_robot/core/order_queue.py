"""注文キューの状態機械（ROS 非依存）。

    受付 ─▶ pending ──[完成ボタン]──▶ ready（呼び出し中）──[その番号のマーカー]──▶ active（ロボット動作）──▶ 完了
              ▲  └───[呼び出し取消]────┘      ▲                                          │
              │                               └──────────── 失敗 / 開始できない ─────────┘

・スタッフが「完成」を押した注文だけが呼び出し表示に出る（完成の順番は自由）
・呼び出し中の注文ならどの番号のマーカーでも受け付ける。ロボット動作中は次のマーカーを受け付けない
・状態は JSON に保存し、ノード再起動後も復元する
"""
from __future__ import annotations

import json
import os
import tempfile
import threading
from collections import deque
from pathlib import Path

from .models import Order, STATE_CALLING, STATE_IDLE, STATE_PENDING, STATE_ROBOT_BUSY

RESULT_SUCCESS = "success"
RESULT_FAILED = "failed"
RESULT_CANCELED = "canceled"


class QueueError(ValueError):
    pass


class OrderQueue:
    def __init__(self, state_path: str | os.PathLike | None = None, max_completed: int = 10):
        self._lock = threading.RLock()
        self._pending: list[Order] = []   # スタッフが準備中
        self._ready: list[Order] = []     # 呼び出し中
        self._active: Order | None = None  # ロボット動作中
        self._completed: deque[int] = deque(maxlen=max_completed)
        self.progress = 0.0
        self.progress_text = ""
        self.last_event = ""
        self.public_message = ""
        self.public_seq = 0
        self._path = Path(state_path).expanduser() if state_path else None
        if self._path and self._path.exists():
            self._load()

    # --- 参照 ---------------------------------------------------------------
    @property
    def state(self) -> int:
        with self._lock:
            if self._active is not None:
                return STATE_ROBOT_BUSY
            if self._ready:
                return STATE_CALLING
            return STATE_PENDING if self._pending else STATE_IDLE

    @property
    def pending(self) -> list[Order]:
        with self._lock:
            return list(self._pending)

    @property
    def ready(self) -> list[Order]:
        with self._lock:
            return list(self._ready)

    @property
    def active(self) -> Order | None:
        return self._active

    @property
    def completed(self) -> list[int]:
        """最近完了した注文番号（新しい順）"""
        with self._lock:
            return list(reversed(self._completed))

    def contains(self, order_number: int) -> bool:
        with self._lock:
            return (self._active is not None and self._active.order_number == order_number) or any(
                o.order_number == order_number for o in self._pending + self._ready
            )

    def snapshot(self) -> dict:
        with self._lock:
            return {
                "state": self.state,
                "active": self._active.to_dict() if self._active else None,
                "progress": self.progress,
                "progress_text": self.progress_text,
                "pending": [o.to_dict() for o in self._pending],
                "ready": [o.to_dict() for o in self._ready],
                "recently_completed": self.completed,
                "last_event": self.last_event,
                "public_message": self.public_message,
                "public_seq": self.public_seq,
            }

    # --- 受付・スタッフ操作 ----------------------------------------------------
    def add(self, order: Order) -> None:
        if not order.items:
            raise QueueError("商品が1つも入っていません")
        if any(i.quantity <= 0 for i in order.items):
            raise QueueError("個数は1以上にしてください")
        with self._lock:
            if self.contains(order.order_number):
                raise QueueError(f"注文番号 {order.order_number} は既に受付中です")
            self._pending.append(order)
            self._event(f"{order.order_number}番の注文を受け付けました（{order.summary()}）")

    def mark_ready(self, order_number: int) -> None:
        """準備完了 → 呼び出し中へ"""
        with self._lock:
            order = self._take(self._pending, order_number, "準備中")
            self._ready.append(order)
            self._public(f"{order_number}番のお客様、お待たせしました")
            self._event(f"{order_number}番をお呼び出ししています")

    def unmark_ready(self, order_number: int) -> None:
        """呼び出しを取り消して準備中に戻す（押し間違い用）"""
        with self._lock:
            order = self._take(self._ready, order_number, "呼び出し中")
            self._pending.append(order)
            self._pending.sort(key=lambda o: o.stamp)
            self._event(f"{order_number}番を準備中に戻しました")

    def cancel(self, order_number: int) -> str:
        """待ち（準備中・呼び出し中）の注文は即削除して 'waiting' を返す。ロボット動作中なら
        'active' を返す（呼び出し側がロボット動作をキャンセルし、終了時に finish_active(CANCELED) を呼ぶ）。"""
        with self._lock:
            if self._active is not None and self._active.order_number == order_number:
                return "active"
            for lst in (self._pending, self._ready):
                for i, o in enumerate(lst):
                    if o.order_number == order_number:
                        del lst[i]
                        self._event(f"{order_number}番の注文を取り消しました")
                        return "waiting"
            raise QueueError(f"注文番号 {order_number} は受付中にありません")

    # --- マーカーとロボット動作 -------------------------------------------------
    def check_marker(self, marker_id: int) -> tuple[bool, str]:
        """マーカーIDでロボット動作を開始してよいか判定する（状態は変えない）。"""
        with self._lock:
            n = marker_id
            if self._active is not None and self._active.order_number == n:
                return False, f"{n}番はロボットが対応中です"
            if any(o.order_number == n for o in self._ready):
                if self._active is not None:
                    return False, f"前のお客様の対応中です。終わったらもう一度{n}番の札をかざしてください"
                return True, ""
            if any(o.order_number == n for o in self._pending):
                return False, f"{n}番はまだ準備中です。番号が表示されるまでお待ちください"
            return False, f"{n}番の注文は受付中にありません"

    def start(self, order_number: int) -> Order:
        """呼び出し中の注文のロボット動作を開始する"""
        with self._lock:
            if self._active is not None:
                raise QueueError(f"{self._active.order_number}番をロボットが対応中です")
            self._active = self._take(self._ready, order_number, "呼び出し中")
            self.progress, self.progress_text = 0.0, "開始"
            self._public(f"{order_number}番のお客様、ロボットが対応します")
            self._event(f"{order_number}番のロボット動作を開始しました")
            return self._active

    def set_progress(self, progress: float, text: str = "") -> None:
        with self._lock:
            self.progress = max(0.0, min(1.0, float(progress)))
            self.progress_text = text
        # 進捗は頻繁に来るので保存しない

    def finish_active(self, result: str, message: str = "") -> Order:
        with self._lock:
            if self._active is None:
                raise QueueError("ロボット動作中の注文がありません")
            order, self._active = self._active, None
            self.progress, self.progress_text = 0.0, ""
            n = order.order_number
            if result == RESULT_SUCCESS:
                self._completed.append(n)
                self._public(f"{n}番のお客様、ありがとうございました")
                self._event(f"{n}番のお渡しが完了しました")
            elif result == RESULT_CANCELED:
                self._event(f"{n}番のロボット動作を取り消しました")
            else:  # 失敗: 呼び出し中に戻してスタッフが再開できるようにする
                self._ready.insert(0, order)
                self._event(f"{n}番のロボット動作に失敗しました（{message or '原因不明'}）。呼び出し中に戻しました")
            return order

    def requeue_active(self, reason: str) -> None:
        """ロボット動作を開始できなかった（応答なし等）注文を呼び出し中に戻す"""
        with self._lock:
            if self._active is None:
                return
            n = self._active.order_number
            self._ready.insert(0, self._active)
            self._active = None
            self.progress, self.progress_text = 0.0, ""
            self._event(f"{n}番を開始できませんでした（{reason}）")

    def note(self, text: str, public: bool = False) -> None:
        """イベント表示だけ更新する（順番違いのマーカー等）"""
        with self._lock:
            self.last_event = text
            if public:
                self._public(text)

    # --- 内部 ---------------------------------------------------------------
    @staticmethod
    def _take(lst: list[Order], order_number: int, where: str) -> Order:
        for i, o in enumerate(lst):
            if o.order_number == order_number:
                return lst.pop(i)
        raise QueueError(f"注文番号 {order_number} は{where}にありません")

    def _public(self, text: str) -> None:
        self.public_message = text
        self.public_seq += 1

    def _event(self, text: str) -> None:
        self.last_event = text
        self._save()

    def _save(self) -> None:
        if not self._path:
            return
        with self._lock:
            data = {
                "pending": [o.to_dict() for o in self._pending],
                "ready": [o.to_dict() for o in self._ready],
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
        # "waiting" は旧バージョンの保存形式
        self._pending = [Order.from_dict(d) for d in data.get("pending", data.get("waiting", []))]
        self._ready = [Order.from_dict(d) for d in data.get("ready", [])]
        # ロボット動作の途中で落ちた注文は呼び出し中に戻す（スタッフ判断で再開）
        if data.get("active"):
            self._ready.insert(0, Order.from_dict(data["active"]))
        self._completed.extend(int(n) for n in data.get("completed", []))
        if self._pending or self._ready:
            self.last_event = f"前回の注文（準備中 {len(self._pending)}件・呼び出し中 {len(self._ready)}件）を復元しました"
