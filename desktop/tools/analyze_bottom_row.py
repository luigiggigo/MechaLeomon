"""Print low-level color-mask metrics for saved bottom-row regression frames."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import numpy as np
from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import auto_digiworld as strategy
import digiworld_bot as bot


def metrics(image, board, row=4, col=1):
    array = np.asarray(image)
    x0, y0, x1, y1 = board
    xa = round(x0 + col * (x1 - x0) / 5) + 8
    xb = round(x0 + (col + 1) * (x1 - x0) / 5) - 8
    ya = round(y0 + row * (y1 - y0) / 5) + 8
    yb = round(y0 + (row + 1) * (y1 - y0) / 5) - 12
    crop = array[ya:yb, xa:xb]
    red, green, blue = crop[:, :, 0], crop[:, :, 1], crop[:, :, 2]
    yellow = (red > 190) & (green > 140) & (blue < 80)
    dark = (red < 65) & (green < 65) & (blue < 75)
    highlight = ((blue > 120) & (green > 90) &
                 (blue.astype(int) > red.astype(int) + 25))
    # The sprite occupies the lower half when row 4 is clipped by the frame.
    lower = dark[crop.shape[0] // 3:]
    return {
        "shape": crop.shape[:2],
        "yellow_count": int(yellow.sum()),
        "yellow_mean": float(yellow.mean()),
        "dark_mean": float(dark.mean()),
        "lower_dark_mean": float(lower.mean()),
        "highlight_mean": float(highlight.mean()),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("directory", type=Path)
    parser.add_argument("--pattern", default="frame_*.png")
    parser.add_argument("--expected-cell", default="4,1")
    parser.add_argument("--min-score", type=float, default=.08)
    parser.add_argument("--min-board-confidence", type=float, default=.80)
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()
    expected = tuple(int(part) for part in args.expected_cell.split(","))
    failures = []
    minimum = 1.0
    minimum_board_confidence = 1.0
    count = 0
    for path in sorted(args.directory.glob(args.pattern)):
        with Image.open(path) as source:
            image = source.convert("RGB")
        detection = bot.classify(image)
        count += 1
        minimum_board_confidence = min(
            minimum_board_confidence, detection.confidence
        )
        if (
            detection.state != "digiworld"
            or detection.board is None
            or detection.confidence < args.min_board_confidence
        ):
            failures.append(path.name)
            print(path.name, "detection=", detection)
            continue
        info = strategy.cells(image, detection.board)
        best = max(((value["player"], cell) for cell, value in info.items()))
        bottom = {col: metrics(image, detection.board, 4, col) for col in range(5)}
        minimum = min(minimum, best[0])
        if best[1] != expected or best[0] < args.min_score:
            failures.append(path.name)
        if args.verbose or path.name in failures:
            print(path.name, "best=", best, "bottom=", bottom)
    print(
        f"verified={count} expected={expected} "
        f"min_board_confidence={minimum_board_confidence:.6f} "
        f"min_score={minimum:.6f} "
        f"failures={failures}"
    )
    return 1 if not count or failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
