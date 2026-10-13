"""Capture a bounded, input-free sequence for player-detector diagnostics."""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import auto_digiworld as strategy
import digiworld_bot as bot


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--samples", type=int, default=20)
    parser.add_argument("--interval", type=float, default=.12)
    args = parser.parse_args()
    args.out.mkdir(parents=True, exist_ok=True)

    adb_path = bot.resolve_adb()
    serial = bot.resolve_serial(adb_path)
    records = []
    for index in range(args.samples):
        started = time.perf_counter()
        image = bot.screenshot(adb_path, serial)
        captured = time.perf_counter()
        detection = bot.classify(image)
        classified = time.perf_counter()
        record = {"index": index, "detection": bot.asdict(detection)}
        if detection.board:
            info = strategy.cells(image, detection.board)
            ranked = sorted(((values["player"], cell) for cell, values in info.items()),
                            reverse=True)
            record["player"] = {
                "cell": list(ranked[0][1]),
                "score": ranked[0][0],
                "top5": [{"cell": list(cell), "score": score}
                         for score, cell in ranked[:5]],
            }
            action, reason = strategy.choose(info)
            record["routing"] = {
                "action": list(action) if action is not None else None,
                "reason": reason,
            }
        analyzed = time.perf_counter()
        record["timing_ms"] = {
            "capture": round((captured - started) * 1000, 1),
            "classify": round((classified - captured) * 1000, 1),
            "cells_and_route": round((analyzed - classified) * 1000, 1),
            "total": round((analyzed - started) * 1000, 1),
        }
        image_path = args.out / f"frame_{index:03d}.png"
        image.save(image_path)
        record["image"] = str(image_path)
        records.append(record)
        print(json.dumps(record, ensure_ascii=False), flush=True)
        time.sleep(max(0, args.interval))
    (args.out / "samples.json").write_text(
        json.dumps(records, ensure_ascii=False, indent=2), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
