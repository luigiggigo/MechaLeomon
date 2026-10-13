"""Print geometric detector candidates for one captured DigiWorld frame."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import numpy as np
from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import digiworld_bot as bot


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("image", type=Path)
    args = parser.parse_args()
    with Image.open(args.image) as source:
        image = source.convert("RGB")

    width, height = image.size
    gray = np.asarray(image, dtype=float).mean(axis=2)
    x_edges = np.mean(
        np.abs(np.diff(gray[int(.30 * height):int(.70 * height)], axis=1)),
        axis=0,
    )
    y_edges = np.mean(
        np.abs(np.diff(gray[:, int(.10 * width):int(.90 * width)], axis=0)),
        axis=1,
    )
    xb = bot._best_six_lines(
        x_edges,
        range(int(.05 * width), int(.25 * width)),
        range(int(.10 * width), int(.22 * width)),
    )
    yb = bot._best_six_lines(
        y_edges,
        range(int(.20 * height), int(.39 * height)),
        range(int(.045 * height), int(.10 * height)),
    )
    print("detection=", bot.classify(image))
    for name, candidate in (("x", xb), ("y", yb)):
        quality, positions, values = candidate
        print(
            name,
            "quality=", round(quality, 4),
            "positions=", positions,
            "values=", [round(value, 4) for value in values],
            "second_weakest=", round(sorted(values)[1], 4),
        )
    xi = np.polyfit(np.arange(6), np.asarray(xb[1]), 1)
    yi = np.polyfit(np.arange(6), np.asarray(yb[1]), 1)
    board = (
        round(xi[1]), round(yi[1]),
        round(xi[1] + 5 * xi[0]), round(yi[1] + 5 * yi[0]),
    )
    board_width = board[2] - board[0]
    board_height = board[3] - board[1]
    print(
        "candidate_board=", board,
        "aspect=", round(board_width / max(board_height, 1), 4),
        "coverage=", round(board_width * board_height / (width * height), 4),
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
