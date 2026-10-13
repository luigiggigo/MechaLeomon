"""Explain cell classification and routing decisions for one saved frame."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import auto_digiworld as strategy
import digiworld_bot as bot


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("image", type=Path)
    args = parser.parse_args()
    with Image.open(args.image) as source:
        image = source.convert("RGB")
    detection = bot.classify(image)
    print("detection=", detection)
    if detection.board is None:
        return 1
    info = strategy.cells(image, detection.board)
    player = max(info, key=lambda cell: info[cell]["player"])
    print("player=", player, "score=", round(info[player]["player"], 5))
    for row in range(5):
        labels = []
        for col in range(5):
            cell = (row, col)
            values = info[cell]
            label = "P" if cell == player else (
                "X" if strategy.is_obstacle(values) else
                (strategy.pickup_category(values) or ".")[0].upper()
            )
            labels.append(label)
        print("row", row, " ".join(labels))
    for cell in sorted(info):
        values = info[cell]
        print(
            cell,
            "obstacle=", strategy.is_obstacle(values),
            "pyramid=", round(values["pyramid"], 5),
            "item=", round(values["item"], 5),
            "highlight=", round(values["highlight"], 5),
            "player=", round(values["player"], 5),
        )
    for attacks_enabled in (True, False):
        action, reason = strategy.choose(
            info, attacks_enabled=attacks_enabled, dashes_enabled=False
        )
        print("attacks_enabled=", attacks_enabled,
              "action=", action, "reason=", reason)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
