"""ArUco マーカーの検出・生成（OpenCV 4.5 系の旧 API と 4.7 以降の新 API の両対応）。

Ubuntu 22.04 の apt 版 python3-opencv は 4.5.4（旧 API）、pip の opencv-contrib-python は新 API。
"""
from __future__ import annotations

import time

import cv2
import numpy as np

# よく使う辞書。マーカーIDの上限 = 辞書サイズ - 1
DICTIONARIES = {
    "DICT_4X4_50": 50, "DICT_4X4_100": 100, "DICT_4X4_250": 250, "DICT_4X4_1000": 1000,
    "DICT_5X5_50": 50, "DICT_5X5_100": 100, "DICT_5X5_250": 250, "DICT_5X5_1000": 1000,
    "DICT_6X6_50": 50, "DICT_6X6_100": 100, "DICT_6X6_250": 250, "DICT_6X6_1000": 1000,
}

_NEW_API = hasattr(cv2.aruco, "ArucoDetector")


def dictionary_size(name: str) -> int:
    if name not in DICTIONARIES:
        raise ValueError(f"未対応の辞書: {name}（{', '.join(DICTIONARIES)}）")
    return DICTIONARIES[name]


def get_dictionary(name: str):
    dictionary_size(name)
    dict_id = getattr(cv2.aruco, name)
    if hasattr(cv2.aruco, "getPredefinedDictionary"):
        return cv2.aruco.getPredefinedDictionary(dict_id)
    return cv2.aruco.Dictionary_get(dict_id)  # OpenCV < 4.7


def generate_marker(dict_name: str, marker_id: int, size_px: int) -> np.ndarray:
    """白黒のマーカー画像（周囲の白余白なし）を返す。"""
    d = get_dictionary(dict_name)
    if hasattr(cv2.aruco, "generateImageMarker"):
        return cv2.aruco.generateImageMarker(d, marker_id, size_px)
    return cv2.aruco.drawMarker(d, marker_id, size_px)  # OpenCV < 4.7


class MarkerDetector:
    def __init__(self, dict_name: str = "DICT_4X4_1000"):
        self.dict_name = dict_name
        self._dict = get_dictionary(dict_name)
        if _NEW_API:
            params = cv2.aruco.DetectorParameters()
            self._detector = cv2.aruco.ArucoDetector(self._dict, params)
        else:
            self._params = cv2.aruco.DetectorParameters_create()

    def detect(self, image: np.ndarray) -> tuple[list[int], list[np.ndarray]]:
        """画像中のマーカーID一覧と四隅座標を返す。"""
        gray = image if image.ndim == 2 else cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
        if _NEW_API:
            corners, ids, _ = self._detector.detectMarkers(gray)
        else:
            corners, ids, _ = cv2.aruco.detectMarkers(gray, self._dict, parameters=self._params)
        if ids is None:
            return [], []
        return [int(i) for i in ids.flatten()], list(corners)

    @staticmethod
    def draw(image: np.ndarray, ids: list[int], corners: list[np.ndarray]) -> np.ndarray:
        out = image.copy()
        if ids:
            cv2.aruco.drawDetectedMarkers(out, corners, np.array(ids, dtype=np.int32).reshape(-1, 1))
        return out


class MarkerDebouncer:
    """誤検出・連続送信を防ぐ。

    同じIDが `min_frames` フレーム連続で見えたら1回だけ確定し、
    同じIDは `cooldown_s` 秒間再確定しない。
    """

    def __init__(self, min_frames: int = 5, cooldown_s: float = 3.0):
        self.min_frames = max(1, min_frames)
        self.cooldown_s = cooldown_s
        self._streak: dict[int, int] = {}
        self._last_fired: dict[int, float] = {}

    def update(self, ids: list[int], now: float | None = None) -> list[int]:
        now = time.monotonic() if now is None else now
        seen = set(ids)
        self._streak = {i: self._streak.get(i, 0) + 1 for i in seen}  # 見えなかったIDはリセット
        fired = []
        for i in seen:
            if self._streak[i] >= self.min_frames and now - self._last_fired.get(i, -1e9) >= self.cooldown_s:
                self._last_fired[i] = now
                fired.append(i)
        return sorted(fired)
