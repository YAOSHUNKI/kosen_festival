"""受取（受渡し台 → 手渡し）動作の定義と実行（ROS 非依存）

動作は「トピックごとの動作オブジェクト」(Motion) として handover.yaml に書く。

    Motion = 指令トピックに msg を出す（pub） → 完了トピックに match を満たすメッセージが来るまで待つ（done）

HandoverRunner が注文ごとに sequence の Motion を順に実行する。ROS とのやりとりは Bus 経由で、
nodes/handover_node.py が rclpy で Bus を実装する（テストでは偽物の Bus を使う）。

    plan = build_plan(cfg, order)                       # 注文番号などを埋め込んだ Step の列
    result = HandoverRunner(cfg, bus).run(order, should_cancel, on_step)
"""
from __future__ import annotations

import re
import string
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Protocol

import yaml

from .models import Order

STATUS_SUCCESS = "success"
STATUS_FAILED = "failed"
STATUS_CANCELED = "canceled"

REPEAT_MODES = ("order", "item", "unit")
MATCH_OPS = {
    "eq": lambda a, b: _eq(a, b),
    "ne": lambda a, b: not _eq(a, b),
    "gt": lambda a, b: a > b,
    "ge": lambda a, b: a >= b,
    "lt": lambda a, b: a < b,
    "le": lambda a, b: a <= b,
}
QOS_KINDS = ("reliable", "best_effort", "latched")
MOTION_KEYS = {"label", "pub", "msg", "done", "match", "fresh", "timeout", "on_timeout", "duration", "mock", "mock_delay"}
DEFAULT_TIMEOUT = 10.0
_TYPE_RE = re.compile(r"^[A-Za-z0-9_]+/(msg/)?[A-Za-z0-9_]+$")
_SINGLE_PLACEHOLDER = re.compile(r"^\{(\w+)\}$")

# 置換できるプレースホルダ（repeat の単位ごとに使えるものが増える）
CTX_ORDER = ("order_number", "unit_count", "summary")
CTX_ITEM = CTX_ORDER + ("product_id", "product_name", "quantity", "item_index")
CTX_UNIT = CTX_ITEM + ("unit_index",)


class HandoverConfigError(ValueError):
    pass


# --- 設定 -------------------------------------------------------------------------
@dataclass
class TopicSpec:
    key: str
    topic: str
    type: str          # 例 "std_msgs/msg/Bool"
    qos: str = "reliable"


@dataclass
class Motion:
    """1つの動作オブジェクト。pub のトピックに msg を出し、done のトピックで完了を待つ"""
    name: str
    label: str
    pub: str | None = None             # 指令を出すトピック（topics のキー）
    msg: dict = field(default_factory=dict)
    done: str | None = None            # 完了を待つトピック（topics のキー）
    match: dict = field(default_factory=dict)   # 完了とみなす条件 {フィールド: 値 or {gt: 値, ...}}
    fresh: bool = True                 # True: この動作を始めた後に届いたメッセージだけで判定
    timeout: float = DEFAULT_TIMEOUT
    on_timeout: str = "fail"           # fail / continue
    duration: float = 0.0              # pub の後、done を待つ前に待つ秒数（done なしなら単なる待ち）
    mock: dict | None = None           # mock_robot が done に返すメッセージ（省略時は match から作る）
    mock_delay: float | None = None    # mock_robot が応答するまでの秒数


@dataclass
class Condition:
    topic: str
    match: dict


@dataclass
class HandoverConfig:
    topics: dict[str, TopicSpec]
    motions: dict[str, Motion]
    sequence: list[str]
    before: list[str] = field(default_factory=list)
    after: list[str] = field(default_factory=list)
    on_abort: list[str] = field(default_factory=list)
    repeat: str = "order"
    estop: Condition | None = None
    ready: Condition | None = None
    path: str = ""

    def pub_topics(self) -> set[str]:
        return {m.pub for m in self.motions.values() if m.pub}

    def sub_topics(self) -> set[str]:
        keys = {m.done for m in self.motions.values() if m.done}
        for c in (self.estop, self.ready):
            if c:
                keys.add(c.topic)
        return keys


def load_config(path: str | Path) -> HandoverConfig:
    path = Path(path).expanduser()
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except FileNotFoundError:
        raise HandoverConfigError(f"動作定義ファイルがありません: {path}") from None
    except yaml.YAMLError as e:
        raise HandoverConfigError(f"{path}: YAML の書式エラー: {e}") from None
    cfg = parse_config(data)
    cfg.path = str(path)
    return cfg


def parse_config(data: dict) -> HandoverConfig:
    if not isinstance(data, dict):
        raise HandoverConfigError("動作定義のトップレベルは辞書にしてください")

    topics: dict[str, TopicSpec] = {}
    for key, t in (data.get("topics") or {}).items():
        if not isinstance(t, dict) or "topic" not in t or "type" not in t:
            raise HandoverConfigError(f"topics.{key}: topic と type が必要です")
        typ = str(t["type"])
        if not _TYPE_RE.match(typ):
            raise HandoverConfigError(f"topics.{key}: type は 'std_msgs/msg/Bool' の形で書いてください（{typ}）")
        if "/msg/" not in typ:
            typ = typ.replace("/", "/msg/", 1)
        qos = t.get("qos", "reliable")
        if qos not in QOS_KINDS:
            raise HandoverConfigError(f"topics.{key}: qos は {QOS_KINDS} のどれか（{qos}）")
        topics[key] = TopicSpec(key, str(t["topic"]), typ, qos)

    repeat = data.get("repeat", "order")
    if repeat not in REPEAT_MODES:
        raise HandoverConfigError(f"repeat は {REPEAT_MODES} のどれか（{repeat}）")
    ctx_keys = {"order": CTX_ORDER, "item": CTX_ITEM, "unit": CTX_UNIT}[repeat]

    motions: dict[str, Motion] = {}
    for name, m in (data.get("motions") or {}).items():
        motions[name] = _parse_motion(name, m or {}, topics)

    def names(key: str) -> list[str]:
        lst = data.get(key) or []
        if not isinstance(lst, list):
            raise HandoverConfigError(f"{key} は動作名のリストにしてください")
        for n in lst:
            if n not in motions:
                raise HandoverConfigError(f"{key}: 動作 '{n}' が motions にありません")
        return list(lst)

    cfg = HandoverConfig(
        topics=topics, motions=motions, sequence=names("sequence"),
        before=names("before"), after=names("after"), on_abort=names("on_abort"), repeat=repeat,
        estop=_parse_condition("estop", data.get("estop"), topics),
        ready=_parse_condition("ready", data.get("ready"), topics),
    )
    if not (cfg.before or cfg.sequence or cfg.after):
        raise HandoverConfigError("sequence が空です")

    # プレースホルダの書き間違いを起動時に見つける
    dummy = {k: 1 for k in CTX_UNIT}
    for n in cfg.sequence:
        _check_placeholders(motions[n], ctx_keys, dummy)
    for n in cfg.before + cfg.after + cfg.on_abort:
        _check_placeholders(motions[n], CTX_ORDER, dummy)
    return cfg


def _parse_motion(name: str, m: dict, topics: dict[str, TopicSpec]) -> Motion:
    if not isinstance(m, dict):
        raise HandoverConfigError(f"motions.{name}: 辞書で書いてください")
    unknown = set(m) - MOTION_KEYS
    if unknown:
        raise HandoverConfigError(f"motions.{name}: 不明なキー {sorted(unknown)}（使えるのは {sorted(MOTION_KEYS)}）")
    for k in ("pub", "done"):
        if m.get(k) is not None and m[k] not in topics:
            raise HandoverConfigError(f"motions.{name}.{k}: トピック '{m[k]}' が topics にありません")
    if m.get("match") and not m.get("done"):
        raise HandoverConfigError(f"motions.{name}: match を書くときは done も必要です")
    if m.get("msg") and not m.get("pub"):
        raise HandoverConfigError(f"motions.{name}: msg を書くときは pub も必要です")
    if not (m.get("pub") or m.get("done") or m.get("duration")):
        raise HandoverConfigError(f"motions.{name}: pub / done / duration のどれかが必要です")
    on_timeout = m.get("on_timeout", "fail")
    if on_timeout not in ("fail", "continue"):
        raise HandoverConfigError(f"motions.{name}.on_timeout は fail か continue（{on_timeout}）")
    timeout = float(m.get("timeout", DEFAULT_TIMEOUT))
    if timeout <= 0:
        raise HandoverConfigError(f"motions.{name}.timeout は正の値にしてください")
    match = m.get("match") or {}
    _check_match(f"motions.{name}.match", match)
    return Motion(
        name=name, label=str(m.get("label", name)),
        pub=m.get("pub"), msg=dict(m.get("msg") or {}),
        done=m.get("done"), match=dict(match), fresh=bool(m.get("fresh", True)),
        timeout=timeout, on_timeout=on_timeout, duration=float(m.get("duration", 0.0)),
        mock=m.get("mock"), mock_delay=None if m.get("mock_delay") is None else float(m["mock_delay"]),
    )


def _parse_condition(key: str, c: Any, topics: dict[str, TopicSpec]) -> Condition | None:
    if not c:
        return None
    if not isinstance(c, dict) or c.get("topic") not in topics or not c.get("match"):
        raise HandoverConfigError(f"{key}: topic（topics のキー）と match が必要です")
    _check_match(f"{key}.match", c["match"])
    return Condition(c["topic"], dict(c["match"]))


def _check_match(where: str, match: Any) -> None:
    if not isinstance(match, dict):
        raise HandoverConfigError(f"{where}: {{フィールド: 値}} の辞書で書いてください")
    for f, v in match.items():
        if isinstance(v, dict):
            bad = set(v) - set(MATCH_OPS)
            if bad or not v:
                raise HandoverConfigError(f"{where}.{f}: 比較は {sorted(MATCH_OPS)} で書いてください")


def _check_placeholders(m: Motion, allowed: tuple[str, ...], dummy: dict) -> None:
    for text in _strings(m.label) + _strings(m.msg) + _strings(m.match) + _strings(m.mock):
        for _, fname, _, _ in string.Formatter().parse(text):
            if fname is not None and fname not in allowed:
                raise HandoverConfigError(
                    f"motions.{m.name}: '{{{fname}}}' は使えません（使えるのは {', '.join(allowed)}）")
    for v in (m.label, m.msg, m.match, m.mock):
        expand(v, dummy)  # 書式エラー（閉じ括弧忘れなど）の検出


def _strings(v: Any) -> list[str]:
    if isinstance(v, str):
        return [v]
    if isinstance(v, dict):
        return [s for x in v.values() for s in _strings(x)]
    if isinstance(v, list):
        return [s for x in v for s in _strings(x)]
    return []


# --- プレースホルダ ----------------------------------------------------------------
def expand(v: Any, ctx: dict) -> Any:
    """文字列中の {order_number} などを置き換える。"{order_number}" だけの文字列は数値のまま入れる"""
    if isinstance(v, str):
        m = _SINGLE_PLACEHOLDER.match(v)
        if m and m.group(1) in ctx:
            return ctx[m.group(1)]
        try:
            return v.format_map(ctx)
        except (KeyError, ValueError, IndexError) as e:
            raise HandoverConfigError(f"'{v}' の置換に失敗: {e}") from None
    if isinstance(v, dict):
        return {k: expand(x, ctx) for k, x in v.items()}
    if isinstance(v, list):
        return [expand(x, ctx) for x in v]
    return v


# --- 条件判定 ----------------------------------------------------------------------
def get_field(msg: Any, path: str) -> Any:
    """'data' / 'wrench.force.z' / 'position.0' のようなパスで値を取り出す（ROS msg・dict 両対応）"""
    obj = msg
    for part in path.split("."):
        if isinstance(obj, dict):
            obj = obj[part]
        elif part.isdigit():
            obj = obj[int(part)]
        else:
            obj = getattr(obj, part)
    return obj


def matches(msg: Any, match: dict) -> bool:
    if msg is None:
        return False
    try:
        for path, expected in match.items():
            actual = get_field(msg, path)
            ops = expected if isinstance(expected, dict) else {"eq": expected}
            for op, val in ops.items():
                if not MATCH_OPS[op](actual, val):
                    return False
        return True
    except (AttributeError, KeyError, IndexError, TypeError):
        return False


def _eq(a: Any, b: Any) -> bool:
    if isinstance(a, float) or isinstance(b, float):
        try:
            return abs(float(a) - float(b)) <= 1e-6
        except (TypeError, ValueError):
            return False
    if isinstance(a, str) or isinstance(b, str):
        return str(a) == str(b)
    return a == b


def mock_reply(m: Motion) -> dict | None:
    """mock_robot が done トピックに返すメッセージ（フィールド辞書）。作れなければ None"""
    if m.mock is not None:
        return dict(m.mock)
    out: dict = {}
    for path, expected in m.match.items():
        if isinstance(expected, dict):
            if "eq" in expected:
                expected = expected["eq"]
            elif "ge" in expected or "le" in expected:
                expected = expected.get("ge", expected.get("le"))
            else:
                return None  # gt / lt / ne だけでは値を決められない → motion に mock を書く
        d = out
        parts = path.split(".")
        for p in parts[:-1]:
            d = d.setdefault(p, {})
        d[parts[-1]] = expected
    return out


# --- 計画 --------------------------------------------------------------------------
@dataclass
class Step:
    motion: Motion
    ctx: dict
    label: str
    msg: dict
    match: dict
    mock: dict | None


def order_context(order: Order) -> dict:
    return {
        "order_number": order.order_number,
        "unit_count": sum(i.quantity for i in order.items),
        "summary": order.summary(),
    }


def make_step(m: Motion, ctx: dict) -> Step:
    reply = mock_reply(m)
    return Step(m, ctx, str(expand(m.label, ctx)), expand(m.msg, ctx), expand(m.match, ctx),
                None if reply is None else expand(reply, ctx))


def build_plan(cfg: HandoverConfig, order: Order) -> list[Step]:
    base = order_context(order)
    units: list[dict] = []
    if cfg.repeat == "order":
        units = [base]
    else:
        for i, it in enumerate(order.items, start=1):
            item_ctx = {**base, "product_id": it.product_id, "product_name": it.product_name or str(it.product_id),
                        "quantity": it.quantity, "item_index": i}
            if cfg.repeat == "item":
                units.append(item_ctx)
            else:
                units += [{**item_ctx, "unit_index": k} for k in range(1, it.quantity + 1)]
    steps = [make_step(cfg.motions[n], base) for n in cfg.before]
    for ctx in units:
        steps += [make_step(cfg.motions[n], ctx) for n in cfg.sequence]
    steps += [make_step(cfg.motions[n], base) for n in cfg.after]
    return steps


# --- 実行 --------------------------------------------------------------------------
class Watcher:
    """1つの購読トピックの受信履歴。ROS のコールバックから push() を呼ぶ"""

    def __init__(self, history: int = 64):
        self._cond = threading.Condition()
        self._recent: deque[tuple[int, Any]] = deque(maxlen=history)
        self.count = 0

    def push(self, msg: Any) -> None:
        with self._cond:
            self.count += 1
            self._recent.append((self.count, msg))
            self._cond.notify_all()

    @property
    def latest(self) -> Any:
        with self._cond:
            return self._recent[-1][1] if self._recent else None

    def wait(self, pred: Callable[[Any], bool], since: int | None, timeout: float,
             interrupt: Callable[[], str | None], poll: float = 0.05) -> tuple[bool, str | None]:
        """pred を満たすメッセージを待つ。since=None なら最新の受信済みメッセージも対象。
        戻り値 (満たした, 中断理由)。タイムアウトは (False, None)"""
        deadline = time.monotonic() + timeout
        seen = since
        with self._cond:
            while True:
                if seen is None:
                    if self._recent and pred(self._recent[-1][1]):
                        return True, None
                    seen = self.count
                else:
                    for n, msg in self._recent:
                        if n > seen and pred(msg):
                            return True, None
                    seen = self.count
                reason = interrupt()
                if reason:
                    return False, reason
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False, None
                self._cond.wait(min(poll, remaining))


class Bus(Protocol):
    def publish(self, topic_key: str, fields: dict) -> None: ...
    def watcher(self, topic_key: str) -> Watcher: ...


@dataclass
class RunResult:
    status: str
    message: str = ""

    @property
    def success(self) -> bool:
        return self.status == STATUS_SUCCESS


class _Interrupted(Exception):
    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


class _PrintLog:
    def info(self, s): print(s)
    def warn(self, s): print(s)
    def error(self, s): print(s)


REASON_CANCEL = "取消"
REASON_ESTOP = "非常停止"


class HandoverRunner:
    def __init__(self, cfg: HandoverConfig, bus: Bus, log=None, poll: float = 0.05):
        self.cfg = cfg
        self.bus = bus
        self.log = log or _PrintLog()
        self.poll = poll

    # --- 状態 ---
    def estop_active(self) -> bool:
        c = self.cfg.estop
        return bool(c) and matches(self.bus.watcher(c.topic).latest, c.match)

    def can_accept(self) -> tuple[bool, str]:
        if self.estop_active():
            return False, "非常停止中です"
        c = self.cfg.ready
        if c and not matches(self.bus.watcher(c.topic).latest, c.match):
            return False, "ロボットの準備ができていません（ready が来ていません）"
        return True, ""

    # --- 実行 ---
    def run(self, order: Order, should_cancel: Callable[[], bool] = lambda: False,
            on_step: Callable[[int, int, Step], None] | None = None,
            on_abort_step: Callable[[Step], None] | None = None) -> RunResult:
        """注文1件分の受取動作を実行する。on_step(i, 全体数, step) は各動作の開始時、
        on_abort_step(step) は後始末の各動作の開始時に呼ばれる"""
        steps = build_plan(self.cfg, order)

        def interrupt() -> str | None:
            if self.estop_active():
                return REASON_ESTOP
            if should_cancel():
                return REASON_CANCEL
            return None

        for i, step in enumerate(steps):
            try:
                self.run_step(step, interrupt, lambda: on_step and on_step(i, len(steps), step))
            except _Interrupted as e:
                if e.reason == REASON_ESTOP:
                    # 非常停止中はロボットを動かさない（後始末もしない）
                    self.log.error(f"{order.order_number}番: 非常停止（{step.label}）")
                    return RunResult(STATUS_FAILED, f"非常停止（{step.label}）")
                self.log.warn(f"{order.order_number}番: 取消（{step.label}）→ 後始末")
                self.abort(order, on_abort_step)
                return RunResult(STATUS_CANCELED, REASON_CANCEL)
            except _StepFailed as e:
                self.log.error(f"{order.order_number}番: {e} → 後始末")
                self.abort(order, on_abort_step)
                return RunResult(STATUS_FAILED, str(e))
        return RunResult(STATUS_SUCCESS, "")

    def abort(self, order: Order, on_step: Callable[[Step], None] | None = None) -> None:
        """失敗・取消後の後始末（on_abort）。取消は無視し、失敗しても最後まで試す。非常停止なら中止"""
        ctx = order_context(order)

        def interrupt() -> str | None:
            return REASON_ESTOP if self.estop_active() else None

        for name in self.cfg.on_abort:
            step = make_step(self.cfg.motions[name], ctx)
            try:
                self.run_step(step, interrupt, lambda: on_step and on_step(step))
            except _Interrupted:
                self.log.error("後始末中に非常停止")
                return
            except _StepFailed as e:
                self.log.error(f"後始末: {e}（続行）")

    def run_step(self, step: Step, interrupt: Callable[[], str | None],
                 started: Callable[[], Any] | None = None) -> None:
        m = step.motion
        w = self.bus.watcher(m.done) if m.done else None
        since = (w.count if m.fresh else None) if w else None   # 指令を出す前に受信位置を記録
        reason = interrupt()
        if reason:
            raise _Interrupted(reason)
        if started:
            started()
        if m.pub:
            self.bus.publish(m.pub, step.msg)
        if m.duration > 0:
            self._sleep(m.duration, interrupt)
        if w is None:
            return
        ok, reason = w.wait(lambda msg: matches(msg, step.match), since, m.timeout, interrupt, self.poll)
        if reason:
            raise _Interrupted(reason)
        if not ok:
            if m.on_timeout == "continue":
                self.log.warn(f"{step.label}: {m.timeout:g}秒で完了しなかったが続行")
                return
            raise _StepFailed(f"{step.label}が{m.timeout:g}秒で完了しませんでした")

    def _sleep(self, sec: float, interrupt: Callable[[], str | None]) -> None:
        end = time.monotonic() + sec
        while True:
            reason = interrupt()
            if reason:
                raise _Interrupted(reason)
            remaining = end - time.monotonic()
            if remaining <= 0:
                return
            time.sleep(min(self.poll, remaining))


class _StepFailed(Exception):
    pass
