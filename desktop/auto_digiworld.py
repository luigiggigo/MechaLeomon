#!/usr/bin/env python3
"""Fast, bounded DigiWorld explorer using the conservative detector."""

from __future__ import annotations

import argparse
import heapq
import itertools
import json
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import digiworld_bot as bot
import numpy as np

# Right is preferred for exploration, but left remains available for real
# targets behind the Digimon and as a last safe route around blockers.
DIRS = ((0, 1, "right"), (1, 0, "down"), (-1, 0, "up"), (0, -1, "left"))
PYRAMID_THRESHOLD = .18
ITEM_THRESHOLD = .06

# Integer costs keep Dijkstra deterministic and make the priorities explicit.
# A pyramid is more expensive than any sensible clear detour on a 5x5 board,
# so attacks remain a last resort instead of a shortcut.
DIRECTION_COST = {"right": 10, "down": 12, "up": 12, "left": 24}
ATTACK_PENALTY = 100
REVERSE_PENALTY = 18
PURPLE_STEP_VALUE = 4
PURPLE_VALUE_COST = PURPLE_STEP_VALUE * DIRECTION_COST["right"]
DASH_DISTANCE = 3
OPPOSITE = {"right": "left", "left": "right", "up": "down", "down": "up"}


@dataclass(frozen=True)
class Route:
    cost: int
    path: tuple[tuple[tuple[int, int], bool, str], ...]
    target: tuple[int, int]
    attacks: int = 0
    backtracks: int = 0


def is_obstacle(values):
    """Detect clipped pyramids without mistaking foreground wall art for one."""
    # A fully highlighted walkable tile contains enough blue/purple pixels to
    # cross the loose clipped-pyramid threshold. Real pyramids cover the tile
    # highlight and stay far below this near-solid cyan ratio.
    walkable_highlight = max(
        values.get("highlight", 0.0), values.get("visible_floor", 0.0)
    ) >= .80
    pyramid_body = values.get("pyramid_body", values["pyramid"])
    return (pyramid_body > PYRAMID_THRESHOLD and
            values["item"] <= .06 and not walkable_highlight)


def cells(image, board):
    """Return per-cell visual scores, including bottom-wall occlusion.

    The decorative foreground wall overlaps the bottom row and scrolls at a
    different horizontal phase from the 5x5 grid.  Consequently a fixed crop
    or a per-column exception eventually treats one of its blue segments as a
    pyramid.  Score the upper, still-visible body of every bottom-row cell
    separately and use the disagreement with its lower half as wall evidence.
    All regions are derived from the detected board and cell height.
    """
    a = np.asarray(image)
    x0, y0, x1, y1 = board
    result = {}
    for row in range(5):
        for col in range(5):
            xa = round(x0 + col*(x1-x0)/5) + 8
            xb = round(x0 + (col+1)*(x1-x0)/5) - 8
            ya = round(y0 + row*(y1-y0)/5) + 8
            yb = round(y0 + (row+1)*(y1-y0)/5) - 12
            z = a[ya:yb, xa:xb]
            r, g, b = z[:,:,0], z[:,:,1], z[:,:,2]
            orange_mask = (r > 180) & (g > 55) & (g < 190) & (b < 100)
            pink_mask = ((r > 170) & (b > 140) & (g < 170) &
                         (r.astype(int) > g.astype(int)+40))
            green_mask = ((g > 130) & (r < 150) &
                          (g.astype(int) > r.astype(int)+35) &
                          (g.astype(int) > b.astype(int)+25))
            red_player = ((r > 110) & (r.astype(int) > g.astype(int)+45) &
                          (r.astype(int) > b.astype(int)+25) & ~orange_mask)
            # Some Digimon forms are predominantly silver/white instead of red.
            # A compact bright-neutral sprite score keeps those forms detectable;
            # static pyramid highlights stay well below the player threshold.
            rgb = z.astype(int)
            bright_neutral_player = ((rgb.min(axis=2) > 165) &
                                     ((rgb.max(axis=2) - rgb.min(axis=2)) < 65))
            yellow_accent = ((r > 190) & (g > 140) & (b < 80))
            dark_sprite = ((r < 65) & (g < 65) & (b < 75))
            highlight_mask = ((b > 120) & (g > 90) &
                              (b.astype(int) > r.astype(int)+25))
            pyramid_mask = ((b > 70) & (r > 45) &
                            (b.astype(int) > g.astype(int)+10))
            pyramid_score = float(pyramid_mask.mean())
            highlight_score = float(highlight_mask.mean())
            pyramid_body_score = pyramid_score
            visible_floor_score = highlight_score
            foreground_wall_score = 0.0
            if row == 4:
                # The wall is bottom-anchored but its crenellations shift
                # across columns.  The upper half remains the stable evidence
                # for the cell body; the lower/upper disagreement identifies
                # wall contamination independently in every cell.
                body_end = max(1, pyramid_mask.shape[0] // 2)
                upper_pyramid = float(pyramid_mask[:body_end].mean())
                lower_pyramid = float(pyramid_mask[body_end:].mean())
                upper_highlight = float(highlight_mask[:body_end].mean())
                lower_highlight = float(highlight_mask[body_end:].mean())
                pyramid_body_score = upper_pyramid
                visible_floor_score = upper_highlight
                foreground_wall_score = max(
                    0.0,
                    lower_pyramid - upper_pyramid,
                    upper_highlight - lower_highlight,
                )
            shadow_player_score = 0.0
            yellow_ratio = float(yellow_accent.mean())
            # On the last row the foreground frame hides the lower animation
            # pose, reducing the visible yellow eyes from about 1.16% to 0.67%
            # of the crop.  Keep this relaxation local to row 4: the dark-body
            # and highlighted-tile checks still reject every neighboring cell.
            min_yellow_ratio = .0055 if row == 4 else .008
            yellow_score_scale = 13 if row == 4 else 8
            if (dark_sprite.mean() > .20 and yellow_ratio > min_yellow_ratio and
                    highlight_mask.mean() > .20 and orange_mask.mean() < .04):
                # The small dark Diri form is nearly black; its two bright
                # yellow eyes are the stable discriminator from dark tiles.
                shadow_player_score = yellow_ratio * yellow_score_scale
            result[(row, col)] = {
                "player": float(max(red_player.mean(), bright_neutral_player.mean(),
                                    shadow_player_score)),
                "orange": float(orange_mask.mean()),
                "pink": float(pink_mask.mean()),
                "green": float(green_mask.mean()),
                "item": float(max(orange_mask.mean(), pink_mask.mean(), green_mask.mean())),
                "pyramid": pyramid_score,
                "pyramid_body": pyramid_body_score,
                "highlight": highlight_score,
                "visible_floor": visible_floor_score,
                "foreground_wall": foreground_wall_score,
            }
    return result


def pickup_category(values):
    """Classify one pickup using the strongest mutually exclusive color."""
    ranked = ((values.get(name, 0.0), name)
              for name in ("orange", "green", "pink"))
    score, category = max(ranked, key=lambda pair: pair[0])
    return category if score > ITEM_THRESHOLD else None


def player_cell(info):
    """Return the most credible player without promoting a visible pickup.

    Bright artwork inside a purple ticket can resemble a silver/white Digimon.
    Prefer any independently credible player candidate outside cells that also
    cross the pickup threshold.  Keep the unrestricted maximum as a fallback
    for an unusual player pose that overlaps a pickup.
    """
    ranked = [(cell, values["player"]) for cell, values in info.items()]
    non_item = [candidate for candidate in ranked
                if candidate[1] >= .08 and
                info[candidate[0]]["item"] <= ITEM_THRESHOLD]
    return max(non_item or ranked, key=lambda candidate: candidate[1])


def dijkstra_route(info, player, targets, *, allow_left=True,
                   allow_obstacles=True, previous_direction=None,
                   allow_empty=False, avoid_first_step=None):
    """Return the least-cost route to any target using the weighted grid.

    Cost is the primary key. Equal routes prefer fewer attacks, fewer backward
    steps and fewer total actions. Non-orange routing sets ``allow_left=False``
    so a secondary pickup can never make the bot reverse direction.
    """
    targets = set(targets)
    if not targets:
        return None
    if allow_empty and player in targets:
        return Route(0, (), player)

    serial = itertools.count()
    start_metrics = (0, 0, 0, 0)
    queue = [(start_metrics, next(serial), player, ())]
    best = {player: start_metrics}
    while queue:
        metrics, _, pos, path = heapq.heappop(queue)
        cost, attacks, backtracks, steps = metrics
        if metrics != best.get(pos):
            continue
        if pos in targets and path:
            return Route(cost, path, pos, attacks, backtracks)
        for dr, dc, name in DIRS:
            if name == "left" and not allow_left:
                continue
            nxt = (pos[0]+dr, pos[1]+dc)
            if not (0 <= nxt[0] < 5 and 0 <= nxt[1] < 5):
                continue
            if not path and nxt == avoid_first_step:
                continue
            obstacle = is_obstacle(info[nxt])
            if obstacle and not allow_obstacles:
                continue
            edge_cost = DIRECTION_COST[name]
            if not path and previous_direction == OPPOSITE[name]:
                edge_cost += REVERSE_PENALTY
            if obstacle:
                edge_cost += ATTACK_PENALTY
            candidate = (
                cost + edge_cost,
                attacks + int(obstacle),
                backtracks + int(name == "left"),
                steps + 1,
            )
            if candidate < best.get(nxt, (10**9, 10**9, 10**9, 10**9)):
                best[nxt] = candidate
                next_path = path + ((nxt, obstacle, name),)
                heapq.heappush(queue, (candidate, next(serial), nxt, next_path))
    return None


def shortest_action(info, player, targets, allow_obstacles=True):
    """Compatibility wrapper returning the first action of a Dijkstra route."""
    route = dijkstra_route(info, player, targets,
                           allow_obstacles=allow_obstacles)
    return route.path[0] if route and route.path else None


def _combined_route(first, second):
    return Route(
        first.cost + second.cost,
        first.path + second.path,
        second.target,
        first.attacks + second.attacks,
        first.backtracks + second.backtracks,
    )


def _purple_detour(info, player, purple_targets, next_targets, direct_route,
                   *, allow_obstacles, previous_direction,
                   avoid_first_step=None):
    """Choose purple steps only when their four-step value covers the detour."""
    best = None
    for purple in sorted(purple_targets):
        to_purple = dijkstra_route(
            info, player, {purple}, allow_left=False,
            allow_obstacles=allow_obstacles,
            previous_direction=previous_direction,
            avoid_first_step=avoid_first_step,
        )
        if to_purple is None:
            continue
        onward = dijkstra_route(
            info, purple, next_targets, allow_left=False,
            allow_obstacles=allow_obstacles, allow_empty=True,
        )
        if onward is None:
            continue
        via = _combined_route(to_purple, onward)
        extra = via.cost - direct_route.cost
        if extra > PURPLE_VALUE_COST:
            continue
        rank = (via.cost - PURPLE_VALUE_COST, extra, via.attacks,
                via.backtracks, len(via.path), purple)
        if best is None or rank < best[0]:
            best = (rank, via, purple, extra)
    return None if best is None else best[1:]


def _forward_targets(info, player):
    """Use the right edge as the exploration objective without going left."""
    if player[1] < 4:
        return {(row, 4) for row in range(5) if (row, 4) in info}
    # At the visible edge, keep moving vertically until scrolling exposes a
    # new forward cell. Backtracking remains forbidden without orange energy.
    return {(row, 4) for row in range(5)
            if row != player[0] and (row, 4) in info}


def _first_action(route, reason):
    if route is None or not route.path:
        return None, reason
    target, obstacle, direction = route.path[0]
    return (("attack" if obstacle else "move", target, direction),
            f"{reason}; cost={route.cost}")


def _dash_landing(info, player, route, dashes_enabled):
    """Return a safe three-cell landing for an actually blocked forward path."""
    landing_col = player[1] + DASH_DISTANCE
    if (not dashes_enabled or landing_col > 4 or route is None or
            not route.path or route.path[0][2] != "right" or
            not route.path[0][1]):
        return None
    landing = (player[0], landing_col)
    if is_obstacle(info[landing]):
        return None
    crossed = [(player[0], col) for col in range(player[1] + 1, landing_col)]
    if not any(is_obstacle(info[cell]) for cell in crossed):
        return None
    return landing


def choose(info, previous_direction=None, attacks_enabled=True,
           dashes_enabled=True, avoid_first_step=None):
    player, pscore = player_cell(info)
    if pscore < .08:
        return None, f"player confidence too low ({pscore:.3f})"

    categories = {position: pickup_category(values)
                  for position, values in info.items() if position != player}
    orange_items = {position for position, category in categories.items()
                    if category == "orange"}

    # Orange energy is the only objective allowed to reverse direction. It is
    # selected before every other pickup, regardless of cost. Dash may be the
    # first action toward it only when the chosen route is blocked to the
    # right, the exact three-cell landing is safe, and orange remains reachable
    # from that landing.
    if orange_items:
        route = dijkstra_route(
            info, player, orange_items, allow_left=True,
            allow_obstacles=attacks_enabled,
            previous_direction=previous_direction,
            avoid_first_step=avoid_first_step,
        )
        blocked_route = route
        if blocked_route is None:
            blocked_route = dijkstra_route(
                info, player, orange_items, allow_left=True,
                allow_obstacles=True,
                previous_direction=previous_direction,
                avoid_first_step=avoid_first_step,
            )
        landing = _dash_landing(info, player, blocked_route, dashes_enabled)
        if landing is not None:
            onward = dijkstra_route(
                info, landing, orange_items, allow_left=True,
                allow_obstacles=attacks_enabled, allow_empty=True,
            )
            if onward is not None:
                return (
                    ("dash", landing, "right"),
                    (f"orange priority targets={sorted(orange_items)}; "
                     f"blocked route; dash {DASH_DISTANCE} cells"),
                )
        if route is None:
            return None, f"orange targets unreachable={sorted(orange_items)}"
        return _first_action(route, f"orange priority targets={sorted(orange_items)}")

    # Green pickups replenish the rare Dash resource. They are the next real
    # objective, but never justify a backward step.
    green_items = {position for position, category in categories.items()
                   if category == "green" and position[1] >= player[1]}
    purple_items = {position for position, category in categories.items()
                    if category == "pink" and position[1] >= player[1]}
    next_targets = green_items or _forward_targets(info, player)
    next_kind = "green dash pickup" if green_items else "forward frontier"
    route = dijkstra_route(
        info, player, next_targets, allow_left=False,
        allow_obstacles=attacks_enabled,
        previous_direction=previous_direction,
        avoid_first_step=avoid_first_step,
    )

    # If attacks were disabled after a rejected tap, still inspect the weighted
    # blocked route so a real three-cell Dash can remain available.
    blocked_route = route
    if blocked_route is None and not green_items:
        blocked_route = dijkstra_route(
            info, player, next_targets, allow_left=False,
            allow_obstacles=True, previous_direction=previous_direction,
            avoid_first_step=avoid_first_step,
        )
    if not green_items:
        landing = _dash_landing(info, player, blocked_route, dashes_enabled)
        if landing is not None:
            return (("dash", landing, "right"),
                    f"blocked forward route; dash {DASH_DISTANCE} cells")

    if route is None:
        return None, f"no safe {next_kind} route"

    if green_items:
        return _first_action(route, f"{next_kind} dijkstra")

    # Purple footsteps are worth four normal forward steps. With no higher
    # priority orange/green pickup visible, they become a waypoint only when
    # that value fully covers the extra route cost to the forward frontier.
    detour = _purple_detour(
        info, player, purple_items, next_targets, route,
        allow_obstacles=attacks_enabled,
        previous_direction=previous_direction,
        avoid_first_step=avoid_first_step,
    )
    if detour is not None:
        purple_route, purple, extra = detour
        return _first_action(
            purple_route,
            f"purple value route via={purple}; extra={extra} "
            f"<= value={PURPLE_VALUE_COST}; next={next_kind}",
        )

    return _first_action(route, f"{next_kind} dijkstra")


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--steps", type=int, default=20)
    p.add_argument("--interval", type=float, default=1.0)
    p.add_argument("--min-confidence", type=float, default=.82)
    p.add_argument("--adb", default=bot.ADB_DEFAULT)
    p.add_argument("--serial", default=bot.SERIAL_DEFAULT)
    p.add_argument("--out", type=Path, default=Path("outputs"))
    args = p.parse_args()
    try:
        args.requested_serial = bot.requested_serial_value(args.serial)
        args.adb = bot.resolve_adb(args.adb)
        args.serial = bot.resolve_serial(args.adb, args.requested_serial)
    except (OSError, RuntimeError, subprocess.SubprocessError) as exc:
        print(f"FEHLER: {exc}", file=sys.stderr)
        return 10
    args.out.mkdir(parents=True, exist_ok=True)
    log_path = args.out / "digiworld_auto_steps.jsonl"
    previous_direction = None
    previous_action = None
    attacks_enabled = True
    dashes_enabled = True
    actions_done = 0
    uncertain_frames = 0

    while actions_done < args.steps:
        started = time.monotonic()
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
        image = bot.screenshot(args.adb, args.serial)
        det = bot.classify(image)
        event = {"time_utc": stamp, "index": actions_done, "detection": bot.asdict(det)}
        if bot.tutorial_overlay_center(image) is not None:
            if previous_action == "attack":
                attacks_enabled = False
                previous_action = None
                event["action"] = "WAIT: attack rejected; attacks disabled, rerouting"
                bot.log_event(log_path, event); print(json.dumps(event))
                time.sleep(max(.5, args.interval)); continue
            if previous_action == "dash":
                dashes_enabled = False
                previous_action = None
                event["action"] = "WAIT: dash rejected; dashes disabled, rerouting"
                bot.log_event(log_path, event); print(json.dumps(event))
                time.sleep(max(.5, args.interval)); continue
            event["action"] = "WAIT: centered overlay/message visible"
            bot.log_event(log_path, event); print(json.dumps(event))
            time.sleep(max(.5, args.interval)); continue
        if det.state != "digiworld" or not det.board or det.confidence < args.min_confidence:
            uncertain_frames += 1
            event["action"] = f"WAIT: unreliable board ({uncertain_frames}/5)"
            bot.log_event(log_path, event); print(json.dumps(event))
            if uncertain_frames >= 5:
                event["action"] = "STOP: five consecutive unreliable frames"
                image.save(args.out / f"auto_stop_{stamp.replace(':','')}.png")
                bot.log_event(log_path, event); return 2
            time.sleep(max(.5, args.interval)); continue
        uncertain_frames = 0

        info = cells(image, det.board)
        action, reason = choose(info, previous_direction, attacks_enabled, dashes_enabled)
        event["reason"] = reason
        if action is None:
            event["action"] = "STOP: no safe action"
            bot.log_event(log_path, event); print(json.dumps(event)); return 3
        kind, target, direction = action
        if kind == "dash":
            control = bot.dash_button(image)
            if control is None:
                dashes_enabled = False
                event["action"] = "WAIT: dash button not detected; dashes disabled"
                bot.log_event(log_path, event); print(json.dumps(event)); continue
            x, y = control
        else:
            x, y = bot.cell_center(det.board, *target)
        dispatch = bot.dispatch_tap_with_recovery(
            args.adb, args.serial, x, y, action=kind,
            requested_serial=args.requested_serial,
            logger=lambda payload: bot.log_event(log_path, {
                "time_utc": datetime.now(timezone.utc).isoformat(),
                "adb_recovery": payload,
            }),
            recovery_settle_seconds=(
                max(args.interval, 1.20) if kind in ("dash", "attack") else
                max(args.interval, .90) if info[target]["item"] > .06 else
                args.interval
            ),
        )
        args.serial = dispatch.serial
        if dispatch.outcome == "rescan":
            event["action"] = "WAIT: ADB recovered; fresh planning required"
            event["adb_recovery"] = list(dispatch.recovery_events)
            bot.log_event(log_path, event)
            print(json.dumps(event, ensure_ascii=False))
            continue
        if dispatch.outcome != "sent":
            event["action"] = "STOP: ADB recovery failed; no input confirmed"
            event["adb_recovery"] = list(dispatch.recovery_events)
            bot.log_event(log_path, event)
            print(json.dumps(event, ensure_ascii=False))
            return 11
        event["action"] = {"type": kind, "target_cell": list(target),
                           "direction": direction, "adb_xy": [x, y]}
        event["scores"] = info[target]
        bot.log_event(log_path, event)
        print(json.dumps(event, ensure_ascii=False))
        previous_direction = direction
        previous_action = kind
        actions_done += 1
        time.sleep(max(0, args.interval - (time.monotonic() - started)))

    # Always leave a final verified screenshot after the bounded run.
    time.sleep(max(.5, args.interval))
    final = bot.screenshot(args.adb, args.serial)
    final_det = bot.classify(final)
    final_path = args.out / "digiworld_auto_final.png"
    final.save(final_path)
    bot.diagnostic(final, final_det).save(args.out / "digiworld_auto_final_diagnostic.png")
    event = {"time_utc": datetime.now(timezone.utc).isoformat(), "status": "complete",
             "steps": args.steps, "detection": bot.asdict(final_det), "final": str(final_path)}
    bot.log_event(log_path, event); print(json.dumps(event, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
