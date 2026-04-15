"""命令行入口：run 子命令跑完整流水线（至结构合并 + 校验）。"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

from . import __version__
from .ffmpeg_util import (
    extract_wav_16k_mono,
    format_duration_sec,
    run_ffprobe,
    video_stream_fps,
    which_binary,
)
from .merge import build_merged, write_merged_json, write_merged_markdown
from .mineru_run import run_mineru_all_keyframes
from .paths import default_output_dir_for_video
from .slide_extract import extract_keyframes
from .validate import validate_merged, write_validation_report
from .whisperx_run import run_whisperx


def _probe_summary(probe: dict[str, Any]) -> dict[str, Any]:
    fmt = probe.get("format") or {}
    return {
        "format_name": fmt.get("format_name"),
        "duration_sec": format_duration_sec(probe),
        "size": fmt.get("size"),
    }


def _load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def cmd_run(args: argparse.Namespace) -> int:
    video = Path(args.video).resolve()
    if not video.is_file():
        print(f"找不到视频文件: {video}", file=sys.stderr)
        return 1

    out_dir = (
        Path(args.out_dir).resolve()
        if args.out_dir
        else default_output_dir_for_video(video)
    )
    out_dir.mkdir(parents=True, exist_ok=True)

    ffmpeg = which_binary("ffmpeg")
    if not ffmpeg:
        print("未找到 ffmpeg", file=sys.stderr)
        return 1

    probe = run_ffprobe(video)
    duration_sec = format_duration_sec(probe)
    fps = video_stream_fps(probe)
    summary = _probe_summary(probe)

    work = out_dir / "_work"
    wav = work / "audio_16k.wav"
    whisper_dir = out_dir / "whisperx"
    slides_dir = out_dir / "slides"
    frames_dir = slides_dir / "frames"

    # 1) 音频
    if not args.skip_audio:
        print(f"[1/5] 抽取 WAV -> {wav}", flush=True)
        extract_wav_16k_mono(video, wav, ffmpeg)
    elif not wav.is_file():
        print("缺少 _work/audio_16k.wav 且指定了 --skip-audio", file=sys.stderr)
        return 1

    # 2) WhisperX
    speech: dict[str, Any]
    if not args.skip_whisperx:
        print("[2/5] WhisperX 转写...", flush=True)
        lang = None if str(args.language).lower() == "auto" else args.language
        speech = run_whisperx(
            wav,
            whisper_dir,
            model_name=args.whisperx_model,
            device=args.device,
            batch_size=args.batch_size,
            language=lang,
        )
    else:
        seg_path = whisper_dir / "segments.json"
        if not seg_path.is_file():
            print("缺少 whisperx/segments.json 且指定了 --skip-whisperx", file=sys.stderr)
            return 1
        speech = _load_json(seg_path)

    # 3) 抽帧
    keyframes_raw: list[dict[str, Any]]
    if not args.skip_slides:
        print("[3/5] 抽取关键帧...", flush=True)
        _records, _meta = extract_keyframes(
            video,
            frames_dir,
            similarity_threshold=args.similarity,
            max_frames=args.max_frames,
            fps_hint=fps,
            duration_sec=duration_sec,
        )
        kpath = slides_dir / "keyframes.json"
        keyframes_data = _load_json(kpath)
        keyframes_raw = keyframes_data.get("keyframes") or []
    else:
        kpath = slides_dir / "keyframes.json"
        if not kpath.is_file():
            print("缺少 slides/keyframes.json 且指定了 --skip-slides", file=sys.stderr)
            return 1
        keyframes_data = _load_json(kpath)
        keyframes_raw = keyframes_data.get("keyframes") or []

    # 4) MinerU
    slides_json = slides_dir / "slides.json"
    slides: list[dict[str, Any]]
    if not args.skip_mineru:
        print("[4/5] MinerU 解析画面（可能较久）...", flush=True)
        backend = args.mineru_backend or (os.environ.get("MINERU_BACKEND") or None)
        extra = []
        if args.mineru_extra:
            extra = args.mineru_extra.split()
        slides = run_mineru_all_keyframes(
            out_dir,
            keyframes_raw,
            backend=backend,
            extra_args=extra or None,
            fail_fast=args.mineru_fail_fast,
        )
        slides_json.write_text(
            json.dumps(slides, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
    else:
        if not slides_json.is_file():
            print("缺少 slides/slides.json 且指定了 --skip-mineru", file=sys.stderr)
            return 1
        slides = json.loads(slides_json.read_text(encoding="utf-8"))

    # 5) 合并 + 校验
    print("[5/5] 结构合并与校验...", flush=True)
    merged = build_merged(
        video_path=video,
        duration_sec=duration_sec,
        probe_summary=summary,
        speech=speech,
        slides=slides,
    )
    if not args.skip_merge:
        write_merged_json(merged, out_dir / "lesson_merged.json")
        write_merged_markdown(merged, out_dir / "lesson_merged.md")

    report = validate_merged(
        merged,
        out_dir,
        require_speech=args.require_speech,
        require_visual_text=args.require_visual_text,
    )
    write_validation_report(report, out_dir / "validation_report.json")

    if report.get("status") != "ok":
        print("校验失败:", file=sys.stderr)
        for e in report.get("errors") or []:
            print(f"  - {e}", file=sys.stderr)
        return 2

    for w in report.get("warnings") or []:
        print(f"警告: {w}", file=sys.stderr)

    print(f"完成。输出目录: {out_dir}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="video-raw-ingest",
        description="课程视频原始内容获取（至结构合并，不含 data-juicer）",
    )
    p.add_argument("--version", action="version", version=f"%(prog)s {__version__}")

    sub = p.add_subparsers(dest="command", required=True)

    run_p = sub.add_parser("run", help="处理单个视频")
    run_p.add_argument("video", type=Path, help="输入视频路径")
    run_p.add_argument(
        "-o",
        "--out-dir",
        type=Path,
        default=None,
        help="输出目录；默认见 RAW_INGEST_OUTPUT_ROOT / autodl-tmp/raw-ingest",
    )
    run_p.add_argument("--skip-audio", action="store_true", help="跳过抽 WAV（需已存在）")
    run_p.add_argument("--skip-whisperx", action="store_true")
    run_p.add_argument("--skip-slides", action="store_true")
    run_p.add_argument("--skip-mineru", action="store_true")
    run_p.add_argument("--skip-merge", action="store_true", help="仍写校验报告但跳过写 lesson_merged.*")
    run_p.add_argument("--similarity", type=float, default=0.6, help="关键帧相似度阈值（越低越敏感）")
    run_p.add_argument("--max-frames", type=int, default=None, help="最多保留关键帧数")
    run_p.add_argument("--whisperx-model", default="large-v2", help="WhisperX 模型名")
    run_p.add_argument(
        "--device",
        default="cuda",
        choices=["cuda", "cpu"],
        help="WhisperX / 对齐所用设备",
    )
    run_p.add_argument("--batch-size", type=int, default=16)
    run_p.add_argument("--language", default="zh", help="WhisperX 语言提示，如 zh / en")
    run_p.add_argument(
        "--mineru-backend",
        default=None,
        help="MinerU -b，如 pipeline；也可设环境变量 MINERU_BACKEND",
    )
    run_p.add_argument(
        "--mineru-extra",
        default=None,
        help="MinerU 额外参数（空格分隔，原样追加）",
    )
    run_p.add_argument(
        "--mineru-fail-fast",
        action="store_true",
        help="任一帧 MinerU 失败则停止",
    )
    run_p.add_argument(
        "--require-speech",
        action="store_true",
        help="校验：口播片段不得为空",
    )
    run_p.add_argument(
        "--require-visual-text",
        action="store_true",
        help="校验：至少一帧 MinerU 有非空正文",
    )

    run_p.set_defaults(func=cmd_run)
    return p


def main(argv: list[str] | None = None) -> int:
    argv = argv if argv is not None else sys.argv[1:]
    parser = build_parser()
    args = parser.parse_args(argv)
    fn = getattr(args, "func", None)
    if fn is None:
        parser.print_help()
        return 1
    return int(fn(args))


if __name__ == "__main__":
    raise SystemExit(main())
