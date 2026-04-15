"""
幻灯片/静态画面关键帧抽取（思路对齐 extract-video-ppt / evp：每秒采样 + 帧间相似度）。

输出带 timestamp_sec 的帧图与 keyframes.json；时间戳在 VFR 视频上为近似值。
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import cv2
import numpy as np


@dataclass
class KeyframeRecord:
    index: int
    timestamp_sec: float
    frame_relpath: str
    similarity_to_previous: float | None


def _compare_img_bgr_hist_correl(img1: np.ndarray, img2: np.ndarray, size=(256, 256)) -> float:
    """返回 [0,1]，越高越相似（与 OpenCV compareHist CORREL 一致方向）。"""
    a = cv2.resize(img1, size)
    b = cv2.resize(img2, size)
    scores: list[float] = []
    for c in range(3):
        h1 = cv2.calcHist([a], [c], None, [256], [0, 256])
        h2 = cv2.calcHist([b], [c], None, [256], [0, 256])
        cv2.normalize(h1, h1)
        cv2.normalize(h2, h2)
        corr = float(cv2.compareHist(h1, h2, cv2.HISTCMP_CORREL))
        scores.append(corr)
    mean_c = float(np.mean(scores))
    # CORREL ∈ [-1,1]；映射到 [0,1] 便于与默认阈值 0.6 对齐
    return float(np.clip((mean_c + 1.0) / 2.0, 0.0, 1.0))


def extract_keyframes(
    video: Path,
    frames_dir: Path,
    *,
    similarity_threshold: float = 0.6,
    max_frames: int | None = None,
    fps_hint: float | None = None,
    duration_sec: float | None = None,
) -> tuple[list[KeyframeRecord], dict[str, Any]]:
    """
    顺序读视频，按约 1 秒间隔采样帧；与上一保留帧相似度低于阈值则保留为新关键帧。

    :param fps_hint: 来自 ffprobe，用于 timestamp_sec = frame_index / fps
    """
    frames_dir.mkdir(parents=True, exist_ok=True)
    cap = cv2.VideoCapture(str(video))
    if not cap.isOpened():
        raise RuntimeError(f"无法打开视频: {video}")

    fps = float(fps_hint or cap.get(cv2.CAP_PROP_FPS) or 25.0)
    if fps <= 0:
        fps = 25.0
    sample_interval = max(1, int(round(fps)))

    records: list[KeyframeRecord] = []
    last_kept: np.ndarray | None = None
    frame_idx = 0
    saved = 0

    meta: dict[str, Any] = {
        "fps_used": fps,
        "sample_interval_frames": sample_interval,
        "similarity_threshold": similarity_threshold,
    }

    while True:
        ret, frame = cap.read()
        if not ret:
            break
        if frame_idx % sample_interval != 0:
            frame_idx += 1
            continue

        timestamp_sec = frame_idx / fps
        if duration_sec is not None and timestamp_sec > duration_sec + 1.0:
            break

        should_save = False
        sim: float | None = None
        if last_kept is None:
            should_save = True
        else:
            sim = _compare_img_bgr_hist_correl(frame, last_kept)
            if sim < similarity_threshold:
                should_save = True

        if should_save:
            if max_frames is not None and saved >= max_frames:
                break
            fname = f"frame_{saved:04d}_{timestamp_sec:.2f}s.jpg"
            fpath = frames_dir / fname
            if not cv2.imwrite(str(fpath), frame):
                cap.release()
                raise RuntimeError(f"写入帧失败: {fpath}")
            # 相对课程输出根目录：slides/frames/...
            rel = f"slides/frames/{fname}"
            rec = KeyframeRecord(
                index=saved,
                timestamp_sec=float(round(timestamp_sec, 4)),
                frame_relpath=rel,
                similarity_to_previous=sim,
            )
            records.append(rec)
            last_kept = frame.copy()
            saved += 1

        frame_idx += 1

    cap.release()

    keyframes_path = frames_dir.parent / "keyframes.json"
    payload = {
        "video": str(video),
        "keyframes": [asdict(r) for r in records],
        "meta": meta,
    }
    keyframes_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return records, meta
