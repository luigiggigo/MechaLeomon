#!/usr/bin/env python3
"""DigiWorld explorer with adaptive two/three-action screenshot batches."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import auto_digiworld as strategy
import auto_pull
import digiworld_bot as bot
import numpy as np
from PIL import Image, ImageDraw, ImageFont

DIR_DELTA = {"right": (0, 1), "left": (0, -1), "down": (1, 0), "up": (-1, 0)}
VERSION = Path(__file__).with_name("VERSION").read_text(encoding="utf-8").strip()
STATUS_PREFIX = "@@DIGIWORLD@@"
DEFAULT_INTERVAL = .70
PICKUP_SETTLE_SECONDS = .90
SPECIAL_ACTION_SETTLE_SECONDS = 1.20
ATTACK_RECHECK_SECONDS = .80
DIABOROMON_POLL_SECONDS = .05
DIABOROMON_ACTION_SETTLE_SECONDS = .45
MAX_UNCHANGED_MOVE_ACTIONS = 5
ADB_RECOVERY_EXIT = 11
ADB_RECOVERY_MESSAGE = (
    "Connessione ADB a BlueStacks interrotta. Riconnessione non riuscita. "
    "Riavvia BlueStacks o premi Riavvia."
)


def emit_status(state, message, done=0, total=0, **extra):
    """Emit one machine-readable line for the compact Windows controller."""
    payload = {"state": state, "message": message, "done": done, "total": total}
    payload.update(extra)
    encoded = json.dumps(payload, ensure_ascii=False)
    if sys.stdout is not None:
        print(STATUS_PREFIX + encoded, flush=True)
    status_file = os.environ.get("DIGIWORLD_STATUS_FILE")
    if status_file:
        path = Path(status_file)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as stream:
            stream.write(encoded + "\n")


def controlled_wait(pause_file=None, stop_file=None, seconds=0.0):
    """Wait without sending input while paused; return False after a stop request.

    File controls also work across a frozen/no-console PyInstaller process and
    keep the automation independent from Tk's UI thread. Paused time is not
    counted toward the requested animation delay.
    """
    pause_file = Path(pause_file) if pause_file else None
    stop_file = Path(stop_file) if stop_file else None
    if pause_file is None and stop_file is None:
        time.sleep(max(0.0, seconds))
        return True

    remaining = max(0.0, seconds)
    previous = time.monotonic()
    reported_pause = False
    pause_ack = Path(str(pause_file) + ".ack") if pause_file else None
    while True:
        if stop_file is not None and stop_file.exists():
            if pause_ack is not None:
                pause_ack.unlink(missing_ok=True)
            return False
        paused = pause_file is not None and pause_file.exists()
        now = time.monotonic()
        if paused:
            pause_ack.touch()
            if not reported_pause:
                emit_status("paused", "Automazione in pausa")
                reported_pause = True
            previous = now
            time.sleep(0.08)
            continue
        if pause_ack is not None:
            pause_ack.unlink(missing_ok=True)
        if reported_pause:
            emit_status("running", "Automazione ripresa")
            reported_pause = False
        remaining -= now - previous
        previous = now
        if remaining <= 0:
            return True
        time.sleep(min(0.08, remaining))


def dispatch_worker_tap(args, log, x, y, action, recovery_settle_seconds):
    """Send every worker input through the same pause-aware recovery path."""
    def log_recovery(payload):
        bot.log_event(log, {
            "time_utc": datetime.now(timezone.utc).isoformat(),
            "adb_recovery": payload,
        })

    result = bot.dispatch_tap_with_recovery(
        args.adb, args.serial, x, y,
        action=action,
        requested_serial=args.requested_serial,
        should_continue=lambda seconds: controlled_wait(
            args.pause_file, args.stop_file, seconds),
        logger=log_recovery,
        recovery_settle_seconds=recovery_settle_seconds,
    )
    args.serial = result.serial
    return result


def finish_failed_adb_recovery(log, event, result, done, total):
    """Log and expose one controlled terminal ADB failure to the GUI."""
    event["action"] = "STOP: ADB recovery failed; no input confirmed"
    event["adb_recovery"] = list(result.recovery_events)
    bot.log_event(log, event)
    emit_status("error", ADB_RECOVERY_MESSAGE, done, total,
                serial=result.serial)
    return ADB_RECOVERY_EXIT


def append_confirmed_action(sent, dispatch, action):
    """Only a check=True ADB success is allowed to contribute to ``done``."""
    if dispatch.outcome != "sent":
        return False
    sent.append(action)
    return True


def player_cell(info):
    """Use the strategy's player selection for observation and planning."""
    return strategy.player_cell(info)


def adaptive_batch_limit(requested, item_goals):
    """Re-scan after every step while a routed pickup remains visible."""
    if item_goals:
        return 1
    return 3 if requested == 2 else requested


def should_wait_for_pickup_animation(pickup_pending, visible_item_count,
                                     waits_done):
    """Allow one settling scan only after a move that collected an item.

    Several genuine pickups can be visible at once, so their count alone is
    not evidence of an animation.  The old count-only check added two idle
    seconds after almost every move on item-rich boards.
    """
    return pickup_pending and visible_item_count >= 3 and waits_done < 1


def action_settle_seconds(kind, picked_up, interval):
    """Use longer delays only for actions that visibly animate."""
    if kind in ("dash", "attack"):
        return max(interval, SPECIAL_ACTION_SETTLE_SECONDS)
    if picked_up:
        return max(interval, PICKUP_SETTLE_SECONDS)
    return interval


def attack_verification_status(target_values, checks_done):
    """Require a second settled frame before declaring an attack ineffective."""
    if not strategy.is_obstacle(target_values):
        return "confirmed"
    return "wait" if checks_done < 1 else "failed"


def progress(current, total, message, color="36"):
    """Print one compact, colored status line for interactive debug runs."""
    if sys.stdout is not None:
        print(f"\033[{color}m{current}/{total}: {message}\033[0m", flush=True)


def format_duration(seconds):
    total = max(0, int(round(seconds)))
    hours, remainder = divmod(total, 3600)
    minutes, secs = divmod(remainder, 60)
    return f"{hours:d}:{minutes:02d}:{secs:02d}" if hours else f"{minutes:02d}:{secs:02d}"


def progress_summary(current, total, elapsed_seconds):
    percent = round(current * 100 / total) if total else 100
    remaining = (elapsed_seconds / current * (total - current)) if current else 0
    return (f"{current}/{total} ({percent}%) | vergangen {format_duration(elapsed_seconds)} "
            f"| ca. {format_duration(remaining)} verbleibend")

_DIGIT_TEMPLATES = None


def _normalize_glyph(mask, size=(20, 28)):
    ys, xs = np.where(mask)
    if not len(xs):
        return np.zeros(size[::-1], dtype=bool)
    crop = (mask[ys.min():ys.max()+1, xs.min():xs.max()+1] * 255).astype("uint8")
    resized = Image.fromarray(crop).resize(size, Image.Resampling.BILINEAR)
    return np.asarray(resized) > 100


def _digit_templates():
    global _DIGIT_TEMPLATES
    if _DIGIT_TEMPLATES is not None:
        return _DIGIT_TEMPLATES
    result = {str(digit): [] for digit in range(10)}
    fonts_dir = Path(os.environ.get("WINDIR", r"C:\Windows")) / "Fonts"
    try:
        for font_path in (fonts_dir / "arialbd.ttf", fonts_dir / "segoeuib.ttf"):
            if not font_path.exists():
                continue
            for size in range(18, 25):
                font = ImageFont.truetype(str(font_path), size)
                for digit in result:
                    canvas = Image.new("L", (32, 36))
                    ImageDraw.Draw(canvas).text((2, -2), digit, font=font, fill=255)
                    result[digit].append(_normalize_glyph(np.asarray(canvas) > 100))
    except OSError:
        return None
    if not all(result.values()):
        return None
    _DIGIT_TEMPLATES = result
    return result


def read_energy_counter(image):
    """Read the orange HUD counter conservatively; return None if uncertain."""
    templates = _digit_templates()
    if templates is None:
        return None
    rgb = np.asarray(image.convert("RGB"))
    top = rgb[:max(1, int(rgb.shape[0] * .15))]
    orange = ((top[:, :, 0] > 200) & (top[:, :, 1] > 55) &
              (top[:, :, 1] < 180) & (top[:, :, 2] < 100))
    ys, xs = np.where(orange)
    if not len(xs):
        return None
    x1, y0, y1 = xs.max(), ys.min(), ys.max()
    if y1 - y0 < 15:
        return None
    roi = top[y0+5:y1-5, x1+4:min(top.shape[1], x1+120)]
    white = (roi[:, :, 0] > 215) & (roi[:, :, 1] > 215) & (roi[:, :, 2] > 215)
    columns = white.sum(axis=0)
    runs, start = [], None
    for column, count in enumerate(columns):
        if count >= 2 and start is None:
            start = column
        elif count < 2 and start is not None:
            runs.append((start, column)); start = None
    if start is not None:
        runs.append((start, len(columns)))

    digits, previous_end = [], None
    for left, right in runs:
        if previous_end is not None and left - previous_end > 6 and digits:
            break
        previous_end = right
        glyph = white[:, left:right]
        glyph_y, glyph_x = np.where(glyph)
        if not len(glyph_x) or glyph_y.max() - glyph_y.min() + 1 < 9:
            continue
        if right - left <= 8:
            digits.append("1"); continue
        normalized = _normalize_glyph(glyph)
        ranked = sorted((min(float(np.mean(normalized != item)) for item in candidates), digit)
                        for digit, candidates in templates.items())
        if ranked[0][0] > .16 or ranked[1][0] - ranked[0][0] < .04:
            return None
        digits.append(ranked[0][1])
    if not 1 <= len(digits) <= 6:
        return None
    return int("".join(digits))


def format_counter(value):
    return f"{value:,}".replace(",", ".")

def item_category(values):
    """Return the strongest visible pickup color, or None below threshold."""
    return strategy.pickup_category(values)


def format_rate(value):
    return f"{value:,.1f}".replace(",", "_").replace(".", ",").replace("_", ".")


def run_summary(elapsed_seconds, collected, energy_start=None, energy_end=None):
    total = sum(collected.values())
    detected = (f"erkannt angefahren: {total} "
                f"(Energie {collected['orange']}, Lila {collected['pink']}, Gruen {collected['green']})")
    if energy_start is not None and energy_end is not None:
        difference = energy_end - energy_start
        hud = (f"Energie {format_counter(energy_start)} -> {format_counter(energy_end)} "
               f"({difference:+d})")
        if elapsed_seconds > 0:
            per_minute = difference * 60 / elapsed_seconds
            per_hour = difference * 3600 / elapsed_seconds
            hud += f" | {format_rate(per_minute)}/Min | {format_rate(per_hour)}/Std"
    else:
        hud = "Energie-Zähler nicht sicher lesbar"
    return f"FERTIG | Gesamtzeit {format_duration(elapsed_seconds)} | {hud} | {detected}"


def show_run_summary(current, total, started_at, collected, energy_start=None,
                     energy_end=None, color="32"):
    message = run_summary(time.monotonic() - started_at, collected, energy_start, energy_end)
    progress(current, total, message, color)

def plan_status(kind, direction, reason, item_count):
    if "orange priority" in reason:
        return f"Chip arancione prioritario - Dijkstra ricalcolato ({item_count} pickup)"
    if "green dash pickup" in reason:
        return "Pickup Dash prioritario - seguo la rotta pesata"
    if "purple value route" in reason:
        return "Passi viola convenienti - deviazione coperta dal valore di 4 passi"
    if kind == "dash":
        return "Dash di 3 caselle - percorso in avanti realmente bloccato"
    if kind == "attack":
        return "Attacco necessario - nessuna deviazione meno costosa"
    labels = {"right": "rechts", "left": "links", "up": "oben", "down": "unten"}
    return f"Erkunde nach {labels.get(direction, direction)} - {reason}"


def consecutive_right_obstacles(info, player):
    count = 0
    for col in range(player[1] + 1, 5):
        if strategy.is_obstacle(info[(player[0], col)]):
            count += 1
        else:
            break
    return count


def dash_scene_signature(info, player):
    """Stable evidence used to decide whether a Dash changed the board."""
    obstacles = tuple(sorted(
        cell for cell, values in info.items() if strategy.is_obstacle(values)
    ))
    pickups = tuple(sorted(
        (cell, category) for cell, values in info.items()
        if (category := item_category(values)) is not None and cell != player
    ))
    return player, obstacles, pickups


def expected_player_after_moves(player, screen_targets, direction):
    """Predict the visible player cell after dispatched taps.

    A right move into column 2 scrolls the world and leaves the Digimon
    visibly anchored in column 1. Other moves should end on the tapped cell.
    """
    expected = player
    for row, col in screen_targets:
        expected = (row, col - 1) if direction == "right" and col >= 2 else (row, col)
    return expected


def player_matches_pending_move(previous, expected, current, direction):
    """Accept only positions along the dispatched move's visible corridor.

    A batch may be observed between steps. Rightward scrolling can also leave
    the sprite briefly one column to the right of its final screen position.
    Neither can move it to an unrelated row or column.
    """
    row, col = current
    return (min(previous[0], expected[0]) <= row <= max(previous[0], expected[0])
            and min(previous[1], expected[1]) <= col <=
            max(previous[1], expected[1]) + int(direction == "right"))


def move_left_player_unchanged(previous_player, expected_player, current_player):
    """Return true only when a move had a verifiable cell transition and failed."""
    return expected_player != previous_player and current_player == previous_player


def player_reanchor_evidence(info, player):
    """Require a unique sprite on highlighted floor, never a pickup or blocker.

    A stale motion prediction is not visual evidence. Repeated settled scenes
    can supersede it, but an ambiguous sprite must keep the worker waiting.
    """
    candidates = [cell for cell, values in info.items()
                  if values["player"] >= .08
                  and values["item"] <= strategy.ITEM_THRESHOLD]
    values = info[player]
    if (candidates != [player] or values["item"] > strategy.ITEM_THRESHOLD
            or strategy.is_obstacle(values) or values.get("highlight", 0) < .20):
        return None
    return dash_scene_signature(info, player)


def oscillation_return_cell(recent_states):
    """Return the cell that would continue a proven A-B-A-B loop."""
    if len(recent_states) < 4:
        return None
    first, second, third, fourth = recent_states[-4:]
    if first == third and second == fourth and first != second:
        return first[0]
    return None


def safe_followup_moves(info, player, first_target, direction, count, goals=None):
    """Return safe (screen_target, source_cell_checked) follow-up moves.

    Entering zero-based column 2 scrolls the world left. The Digimon returns
    visually to column 1, while the next right neighbor corresponds to the
    old column 3 that is already visible in the current screenshot.
    """
    if count <= 0 or direction != "right":
        return []
    dr, dc = DIR_DELTA[direction]
    results = []
    goals = set(goals or ())
    previous_distance = (min(abs(first_target[0]-g[0]) + abs(first_target[1]-g[1])
                             for g in goals) if goals else None)
    if direction == "right" and first_target[1] >= 2:
        offset = 1
        screen_player = (first_target[0], first_target[1] - 1)
    else:
        offset = 0
        screen_player = first_target
    for _ in range(count):
        screen_target = (screen_player[0] + dr, screen_player[1] + dc)
        checked_cell = (screen_target[0], screen_target[1] + offset) if direction == "right" else screen_target
        if not (0 <= checked_cell[0] < 5 and 0 <= checked_cell[1] < 5):
            break
        cell = info[checked_cell]
        if strategy.is_obstacle(cell):
            break
        if goals:
            distance = min(abs(checked_cell[0]-g[0]) + abs(checked_cell[1]-g[1])
                           for g in goals)
            if distance > previous_distance:
                break
            previous_distance = distance
        results.append((screen_target, checked_cell))
        # Never plan beyond an item because its pickup animation changes the frame.
        if cell["item"] > .06:
            break
        if direction == "right" and screen_target[1] >= 2:
            offset += 1
            screen_player = (screen_target[0], screen_target[1] - 1)
        else:
            screen_player = screen_target
    return results


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--version", action="version", version=f"%(prog)s {VERSION}")
    p.add_argument("--steps", type=int, default=10)
    p.add_argument("--interval", type=float, default=DEFAULT_INTERVAL)
    p.add_argument("--batch-size", type=int, default=1, choices=(1, 2, 3))
    p.add_argument("--debug-screenshots", action="store_true")
    p.add_argument("--verbose", action="store_true", help="human-readable status for every scan")
    p.add_argument("--progress-percent", type=int, default=0, help="compact update interval in percent")
    p.add_argument("--min-confidence", type=float, default=.80)
    p.add_argument("--adb", default=bot.ADB_DEFAULT)
    p.add_argument("--serial", default=bot.SERIAL_DEFAULT)
    p.add_argument("--out", type=Path, default=Path("outputs"))
    p.add_argument("--pause-file", type=Path)
    p.add_argument("--stop-file", type=Path)
    auto_pull_group = p.add_mutually_exclusive_group()
    auto_pull_group.add_argument(
        "--auto-pull-10x", action="store_true",
        help="run the isolated 10x Summon workflow")
    auto_pull_group.add_argument(
        "--auto-pull-35x", action="store_true",
        help="run the isolated repeatable 35x Summon workflow")
    args = p.parse_args()
    if args.steps <= 0:
        raise SystemExit("--steps must be positive")

    try:
        args.requested_serial = bot.requested_serial_value(args.serial)
        args.adb = bot.resolve_adb(args.adb)
        args.serial = bot.resolve_serial(args.adb, args.requested_serial)
    except (OSError, RuntimeError, subprocess.SubprocessError) as exc:
        if sys.stderr is not None:
            print(f"FEHLER: {exc}", file=sys.stderr)
        emit_status("error", str(exc), 0, args.steps)
        return 10

    emit_status("connected", f"BlueStacks collegato: {args.serial}", 0, args.steps,
                serial=args.serial)

    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S_%fZ")
    run_dir = args.out / run_id
    run_dir.mkdir(parents=True, exist_ok=False)
    log = run_dir / "events.jsonl"
    pull_kind = "10x" if args.auto_pull_10x else "35x" if args.auto_pull_35x else None
    if pull_kind is not None:
        return auto_pull.run(
            args, log, pull_kind=pull_kind,
            emit_status=emit_status,
            controlled_wait=controlled_wait,
            dispatch_worker_tap=dispatch_worker_tap,
            finish_failed_adb_recovery=finish_failed_adb_recovery,
        )
    if args.verbose:
        progress(0, args.steps, "Debuglauf gestartet - erster Scan", "32")
    done = 0
    started_at = time.monotonic()
    progress_step = max(1, (args.steps * args.progress_percent + 99) // 100) if args.progress_percent else 0
    next_progress = progress_step
    collected = {"orange": 0, "pink": 0, "green": 0}
    energy_start = None
    attacks_enabled = True
    dashes_enabled = True
    previous_action = None
    previous_attack_target = None
    previous_attack_checks = 0
    previous_dash_player = None
    previous_dash_obstacles = 0
    previous_dash_signature = None
    previous_direction = None
    previous_move_player = None
    previous_move_expected = None
    previous_move_actions = 0
    unchanged_move_actions = 0
    stable_board = None
    unreliable = 0
    player_unreliable = 0
    item_burst_waits = 0
    pickup_animation_pending = False
    recent_states = []
    reanchor_evidence = None
    reanchor_scans = 0

    while done < args.steps:
        if not controlled_wait(args.pause_file, args.stop_file):
            emit_status("stopped", "Automazione fermata", done, args.steps)
            return 0
        emit_status("running", "Analisi del DigiWorld in corso…", done, args.steps)
        image = bot.screenshot(args.adb, args.serial)
        last_reanchor_evidence, last_reanchor_scans = reanchor_evidence, reanchor_scans
        # Any intervening overlay, unreliable board or missing sprite breaks
        # consecutiveness, including branches that skip cell classification.
        reanchor_evidence, reanchor_scans = None, 0
        stamp = datetime.now(timezone.utc).isoformat()
        attempt = bot.diaboromon_attempt_button(image)
        find_party = bot.diaboromon_find_party_button(image)
        party_count = bot.diaboromon_party_count(image)
        battle_banner = bot.diaboromon_battle_banner_visible(image)
        lobby_visible = bot.diaboromon_lobby_visible(image)

        if (attempt is not None or find_party is not None or
                party_count is not None or battle_banner or
                lobby_visible):
            # Transition frames between lobby, VS banner and battle are often
            # visually blank for one capture. Any recognized Diaboromon state
            # proves the observer is healthy and resets generic retry history.
            unreliable = 0
            player_unreliable = 0

        # Diaboromon is not a DigiWorld grid. Handle its two safe, visually
        # anchored controls before the generic board classifier can reject it.
        if attempt is not None:
            event = {"time_utc": stamp, "next_index": done,
                     "screen": "diaboromon_party_ready"}
            emit_status("running", "Diaboromon: party pronto, avvio…",
                        done, args.steps)
            dispatch = dispatch_worker_tap(
                args, log, attempt[0], attempt[1], "attempt",
                DIABOROMON_ACTION_SETTLE_SECONDS)
            if dispatch.outcome == "stopped":
                emit_status("stopped", "Automazione fermata", done, args.steps)
                return 0
            if dispatch.outcome == "failed":
                return finish_failed_adb_recovery(log, event, dispatch, done, args.steps)
            if dispatch.outcome == "rescan":
                event["action"] = "WAIT: ADB recovered; Diaboromon rescan required"
                event["adb_recovery"] = list(dispatch.recovery_events)
            else:
                event["action"] = {"status": "sent", "type": "attempt",
                                   "adb_xy": list(attempt)}
                done += 1
            bot.log_event(log, event)
            if not controlled_wait(
                    args.pause_file, args.stop_file,
                    DIABOROMON_ACTION_SETTLE_SECONDS):
                emit_status("stopped", "Automazione fermata", done, args.steps)
                return 0
            continue

        if find_party is not None:
            event = {"time_utc": stamp, "next_index": done,
                     "screen": "diaboromon_lobby"}
            emit_status("running", "Diaboromon: ricerca party…", done, args.steps)
            dispatch = dispatch_worker_tap(
                args, log, find_party[0], find_party[1], "find_party",
                DIABOROMON_ACTION_SETTLE_SECONDS)
            if dispatch.outcome == "stopped":
                emit_status("stopped", "Automazione fermata", done, args.steps)
                return 0
            if dispatch.outcome == "failed":
                return finish_failed_adb_recovery(log, event, dispatch, done, args.steps)
            if dispatch.outcome == "rescan":
                event["action"] = "WAIT: ADB recovered; Diaboromon rescan required"
                event["adb_recovery"] = list(dispatch.recovery_events)
            else:
                event["action"] = {"status": "sent", "type": "find_party",
                                   "adb_xy": list(find_party)}
                done += 1
            bot.log_event(log, event)
            if not controlled_wait(
                    args.pause_file, args.stop_file,
                    DIABOROMON_ACTION_SETTLE_SECONDS):
                emit_status("stopped", "Automazione fermata", done, args.steps)
                return 0
            continue

        if lobby_visible:
            event = {"time_utc": stamp, "next_index": done,
                     "screen": "diaboromon_party_forming",
                     "action": "WAIT: party forming"}
            emit_status("running", "Diaboromon: formazione party…",
                        done, args.steps)
            bot.log_event(log, event)
            if not controlled_wait(
                    args.pause_file, args.stop_file, DIABOROMON_POLL_SECONDS):
                emit_status("stopped", "Automazione fermata", done, args.steps)
                return 0
            continue

        if party_count is not None or battle_banner:
            party_ready = party_count == 5 or battle_banner
            event = {"time_utc": stamp, "next_index": done,
                     "screen": "diaboromon_battle",
                     "party_full": party_count == 5,
                     "battle_banner": battle_banner}
            if party_ready:
                give_up = bot.diaboromon_give_up_button(image)
                if give_up is not None:
                    trigger = "5/5" if party_count == 5 else "banner VS"
                    emit_status("running", f"Diaboromon {trigger}: Give Up…",
                                done, args.steps)
                    dispatch = dispatch_worker_tap(
                        args, log, give_up[0], give_up[1], "give_up",
                        DIABOROMON_ACTION_SETTLE_SECONDS)
                    if dispatch.outcome == "stopped":
                        emit_status("stopped", "Automazione fermata", done, args.steps)
                        return 0
                    if dispatch.outcome == "failed":
                        return finish_failed_adb_recovery(
                            log, event, dispatch, done, args.steps)
                    if dispatch.outcome == "rescan":
                        event["action"] = "WAIT: ADB recovered; Diaboromon rescan required"
                        event["adb_recovery"] = list(dispatch.recovery_events)
                    else:
                        event["action"] = {"status": "sent", "type": "give_up",
                                           "adb_xy": list(give_up)}
                        done += 1
                    wait_seconds = DIABOROMON_ACTION_SETTLE_SECONDS
                else:
                    event["action"] = (
                        "WAIT: Diaboromon ready; Give Up button not yet active")
                    wait_seconds = DIABOROMON_POLL_SECONDS
            else:
                emit_status("running", "Diaboromon: scansione party rapida…",
                            done, args.steps)
                event["action"] = "WAIT: party not full"
                wait_seconds = DIABOROMON_POLL_SECONDS
            bot.log_event(log, event)
            if not controlled_wait(
                    args.pause_file, args.stop_file, wait_seconds):
                emit_status("stopped", "Automazione fermata", done, args.steps)
                return 0
            continue

        det = bot.classify(image)
        event = {"time_utc": stamp, "next_index": done, "detection": bot.asdict(det)}
        if args.verbose:
            progress(done, args.steps, "Scanne Spielfeld und berechne neu ...", "90")

        if bot.tutorial_overlay_center(image) is not None:
            # Transient pickup/attack banners can resemble the first-use card.
            # Preserve the pending action and verify the settled board after
            # the overlay disappears instead of disabling a control here.
            event["action"] = "WAIT: overlay visible"
            bot.log_event(log, event)
            if args.verbose: progress(done, args.steps, str(event["action"]), "33")
            if not controlled_wait(args.pause_file, args.stop_file, args.interval):
                emit_status("stopped", "Automazione fermata", done, args.steps)
                return 0
            continue

        if det.state != "digiworld" or not det.board or det.confidence < args.min_confidence:
            unreliable += 1
            event["action"] = f"WAIT: unrecognized frame ({unreliable}); retrying"
            bot.log_event(log, event)
            emit_status("running", "Schermata non riconosciuta, continuo a cercare…",
                        done, args.steps)
            if args.verbose:
                progress(done, args.steps, "Spielfeld unsicher - neuer Scan", "33")
            if not controlled_wait(args.pause_file, args.stop_file, args.interval):
                emit_status("stopped", "Automazione fermata", done, args.steps)
                return 0
            continue
        unreliable = 0
        if energy_start is None:
            energy_start = read_energy_counter(image)
            if args.verbose and energy_start is not None:
                progress(done, args.steps, f"Energie-Startwert: {format_counter(energy_start)}", "93")

        if stable_board is None:
            stable_board = det.board
        elif max(abs(a - b) for a, b in zip(det.board, stable_board)) > 18:
            event["board_correction"] = {
                "detected": list(det.board),
                "used": list(stable_board),
                "reason": "rejected sudden grid jump",
            }
            det = bot.Detection(det.state, det.confidence, stable_board,
                                det.reason + "; stable board retained")

        info = strategy.cells(image, det.board)
        visible_items = [cell for cell, values in info.items() if values["item"] > .06]
        if should_wait_for_pickup_animation(
                pickup_animation_pending, len(visible_items), item_burst_waits):
            item_burst_waits += 1
            event["action"] = (f"WAIT: possible pickup animation; {len(visible_items)} "
                               f"item cells ({item_burst_waits}/1)")
            if args.debug_screenshots:
                safe_stamp = stamp.replace(":", "").replace("+", "_")
                wait_path = run_dir / f"animation_wait_{done:04d}_{safe_stamp}.png"
                bot.diagnostic(image, det).save(wait_path)
                event["debug"] = str(wait_path)
            bot.log_event(log, event)
            if not controlled_wait(
                    args.pause_file, args.stop_file,
                    max(args.interval, PICKUP_SETTLE_SECONDS)):
                emit_status("stopped", "Automazione fermata", done, args.steps)
                return 0
            continue
        item_burst_waits = 0
        pickup_animation_pending = False
        player, player_score = player_cell(info)
        inconsistent_move = (
            previous_action == "move" and previous_move_player is not None and
            previous_move_expected is not None and
            not player_matches_pending_move(
                previous_move_player, previous_move_expected, player,
                previous_direction)
        )
        if inconsistent_move and player_score >= .08:
            reanchor_evidence = player_reanchor_evidence(info, player)
            if reanchor_evidence is not None:
                reanchor_scans = (last_reanchor_scans + 1
                                  if reanchor_evidence == last_reanchor_evidence else 1)
            if reanchor_scans >= 3:
                event["player_reanchor"] = {
                    "previous": list(previous_move_player),
                    "expected": list(previous_move_expected),
                    "observed": list(player), "settled_scans": reanchor_scans,
                }
                # Discard the stale prediction; no extra input is counted and
                # the normal planner below uses this frame's observed cell.
                previous_action = None
                previous_move_player = previous_move_expected = None
                previous_move_actions = unchanged_move_actions = 0
                previous_direction = None
                recent_states.clear()
                inconsistent_move = False
        if player_score < .08 or inconsistent_move:
            player_unreliable += 1
            event["action"] = (f"WAIT: player score {player_score:.3f} "
                               f"({player_unreliable}); retrying")
            if inconsistent_move:
                event["action"] = "WAIT: player position inconsistent with pending move"
                event["player_check"] = {
                    "previous": list(previous_move_player),
                    "expected": list(previous_move_expected),
                    "detected": list(player),
                }
            bot.log_event(log, event)
            message = ("Verifica posizione del personaggio in corso…" if inconsistent_move
                       else "Personaggio non visibile, continuo a cercare…")
            emit_status("running", message,
                        done, args.steps)
            if args.verbose:
                progress(done, args.steps, "Spielerposition unsicher - neuer Scan", "33")
            if not controlled_wait(
                    args.pause_file, args.stop_file,
                    max(args.interval, PICKUP_SETTLE_SECONDS)):
                emit_status("stopped", "Automazione fermata", done, args.steps)
                return 0
            continue
        player_unreliable = 0
        event["player"] = {"cell": list(player), "score": round(player_score, 5)}
        if (previous_action == "move" and previous_move_player is not None and
                previous_move_expected is not None):
            failed = move_left_player_unchanged(
                previous_move_player, previous_move_expected, player)
            unchanged_move_actions = (
                unchanged_move_actions + previous_move_actions if failed else 0)
            event["move_state"] = {
                "status": "unchanged" if failed else "confirmed",
                "previous_player": list(previous_move_player),
                "expected_player": list(previous_move_expected),
                "current_player": list(player),
                "unchanged_actions": unchanged_move_actions,
                "limit": MAX_UNCHANGED_MOVE_ACTIONS,
            }
            previous_action = None
            previous_move_player = None
            previous_move_expected = None
            previous_move_actions = 0
            if unchanged_move_actions >= MAX_UNCHANGED_MOVE_ACTIONS:
                event["action"] = (
                    "STOP: five dispatched moves left the player unchanged"
                )
                bot.log_event(log, event)
                message = (
                    "Movimento non ricevuto da BlueStacks per 5 tentativi. "
                    "Automazione fermata in sicurezza."
                )
                emit_status("error", message, done, args.steps)
                if args.verbose:
                    progress(done, args.steps, message, "31")
                return 5
        item_goals = {cell for cell, values in info.items()
                      if values["item"] > .06 and cell != player}
        # Pickup routing is recalculated after every step. Only an item-free,
        # straight forward corridor may use the faster three-click batch.
        effective_batch_size = adaptive_batch_limit(args.batch_size, item_goals)
        if previous_action == "attack" and previous_attack_target is not None:
            attack_status = attack_verification_status(
                info.get(previous_attack_target, {
                    "pyramid": 0, "item": 0, "highlight": 0
                }),
                previous_attack_checks,
            )
            if attack_status == "wait":
                previous_attack_checks += 1
                event["attack_state"] = {
                    "status": "waiting for attack animation",
                    "target_cell": list(previous_attack_target),
                    "verification": f"{previous_attack_checks}/1",
                }
                event["action"] = "WAIT: attack animation verification (1/1)"
                bot.log_event(log, event)
                if not controlled_wait(
                        args.pause_file, args.stop_file,
                        max(args.interval, ATTACK_RECHECK_SECONDS)):
                    emit_status("stopped", "Automazione fermata", done, args.steps)
                    return 0
                continue
            if attack_status == "failed":
                attacks_enabled = False
                event["attack_state"] = {
                    "status": "disabled: attack unchanged in two settled frames",
                    "target_cell": list(previous_attack_target),
                }
            else:
                event["attack_state"] = {
                    "status": "confirmed: obstacle removed",
                    "target_cell": list(previous_attack_target),
                }
            previous_action = None
            previous_attack_target = None
            previous_attack_checks = 0
        if previous_action == "dash" and previous_dash_player is not None:
            current_right_obstacles = consecutive_right_obstacles(info, player)
            current_dash_signature = dash_scene_signature(info, player)
            if (previous_dash_signature is not None and
                    current_dash_signature == previous_dash_signature):
                dashes_enabled = False
                event["dash_state"] = {
                    "status": "disabled: previous dash left the scene unchanged",
                    "player_cell": list(player),
                    "right_obstacles_before": previous_dash_obstacles,
                    "right_obstacles_after": current_right_obstacles,
                }
            previous_action = None
            previous_dash_player = None
            previous_dash_obstacles = 0
            previous_dash_signature = None
        recent_states.append((player, tuple(sorted(item_goals))))
        recent_states = recent_states[-4:]
        avoid_first_step = oscillation_return_cell(recent_states)
        loop_guard = avoid_first_step is not None
        # The route is not physically Dash-blocked: one clear step is being
        # suppressed only to break a proven control loop. Do not spend the
        # rare Dash resource as a side effect of that temporary constraint.
        planner_dashes_enabled = dashes_enabled and not loop_guard
        action, reason = strategy.choose(
            info, previous_direction, attacks_enabled, planner_dashes_enabled,
            avoid_first_step=avoid_first_step,
        )
        if loop_guard:
            event["loop_guard"] = {
                "status": "A-B-A-B oscillation detected",
                "avoid_first_step": list(avoid_first_step),
                "dash_suppressed": True,
            }
            reason = f"{reason}; loop guard avoids={avoid_first_step}"
        if action is None:
            if loop_guard:
                event["reason"] = reason
                event["action"] = (
                    "WAIT: oscillation detected; no alternative safe first step"
                )
                bot.log_event(log, event)
                emit_status(
                    "running",
                    "Percorso in oscillazione, attendo una via alternativa…",
                    done, args.steps,
                )
                # Preserve A-B-A so the same unchanged frame reconstructs the
                # guard on the next scan instead of permitting the return tap.
                recent_states.pop()
                if not controlled_wait(
                        args.pause_file, args.stop_file, args.interval):
                    emit_status("stopped", "Automazione fermata",
                                done, args.steps)
                    return 0
                continue
            event["action"] = "STOP: no safe action"
            event["reason"] = reason
            image.save(run_dir / "no_safe_action.png")
            bot.log_event(log, event)
            if args.verbose: progress(done, args.steps, "STOPP - keine sichere Aktion", "31")
            show_run_summary(done, args.steps, started_at, collected, energy_start, read_energy_counter(image), "33")
            return 4
        kind, target, direction = action
        if args.verbose:
            color = "93" if item_goals else "36"
            progress(done, args.steps, plan_status(kind, direction, reason, len(item_goals)), color)
        event["reason"] = reason
        event["batch_limit"] = effective_batch_size
        if effective_batch_size == 3 and args.batch_size == 2:
            event["batch_mode"] = "adaptive-3: no visible items"
        sent = []
        picked_up = False
        rescan_required = False

        # Precompute and visualize the batch before sending any input.
        planned = [target]
        if kind == "move" and info[target]["item"] <= .06:
            remaining = (0 if loop_guard else
                         min(effective_batch_size - 1, args.steps - done - 1))
            planned.extend(p[0] for p in safe_followup_moves(
                info, player, target, direction, remaining, item_goals))
        if args.debug_screenshots:
            debug = bot.diagnostic(image, det)
            draw = ImageDraw.Draw(debug)
            x0, y0, x1, y1 = det.board
            # Bind the current board via default arguments: this closure is
            # used only within the same iteration (ruff B023).
            def box(cell, color, width=4, x0=x0, x1=x1, y0=y0, y1=y1, draw=draw):
                row, col = cell
                xa = round(x0 + col*(x1-x0)/5); xb = round(x0 + (col+1)*(x1-x0)/5)
                ya = round(y0 + row*(y1-y0)/5); yb = round(y0 + (row+1)*(y1-y0)/5)
                draw.rectangle((xa+3, ya+3, xb-3, yb-3), outline=color, width=width)
            box(player, (255, 255, 0), 5)
            for cell, values in info.items():
                if values["item"] > .06 and cell != player:
                    box(cell, (255, 0, 255), 3)
            for number, cell in enumerate(planned, 1):
                box(cell, (255, 80, 0), 5)
                cx, cy = bot.cell_center(det.board, *cell)
                draw.text((cx-5, cy-8), str(number), fill=(255,255,255))
            safe_stamp = stamp.replace(":", "").replace("+", "_")
            debug_path = run_dir / f"debug_{done:04d}_{safe_stamp}.png"
            debug.save(debug_path)
            event["debug"] = str(debug_path)

        if kind == "dash":
            control = bot.dash_button(image)
            if control is None:
                dashes_enabled = False
                event["action"] = "WAIT: dash button missing"
                bot.log_event(log, event)
                if args.verbose: progress(done, args.steps, "Dash nicht verfuegbar - plane neu", "33")
                continue
            if not controlled_wait(args.pause_file, args.stop_file):
                emit_status("stopped", "Automazione fermata", done, args.steps)
                return 0
            dispatch = dispatch_worker_tap(
                args, log, control[0], control[1], "dash",
                action_settle_seconds("dash", False, args.interval))
            if dispatch.outcome == "stopped":
                emit_status("stopped", "Automazione fermata", done, args.steps)
                return 0
            if dispatch.outcome == "failed":
                return finish_failed_adb_recovery(
                    log, event, dispatch, done, args.steps)
            if dispatch.outcome == "rescan":
                event["action"] = "WAIT: ADB recovered; fresh planning required"
                event["adb_recovery"] = list(dispatch.recovery_events)
                bot.log_event(log, event)
                continue
            append_confirmed_action(sent, dispatch, {
                "type": "dash", "target_cell": list(target),
                "distance": strategy.DASH_DISTANCE, "adb_xy": list(control),
            })
        else:
            x, y = bot.cell_center(det.board, *target)
            if not controlled_wait(args.pause_file, args.stop_file):
                emit_status("stopped", "Automazione fermata", done, args.steps)
                return 0
            possible_pickup = (
                kind == "move" and info[target]["item"] > .06
            )
            dispatch = dispatch_worker_tap(
                args, log, x, y, kind,
                action_settle_seconds(kind, possible_pickup, args.interval))
            if dispatch.outcome == "stopped":
                emit_status("stopped", "Automazione fermata", done, args.steps)
                return 0
            if dispatch.outcome == "failed":
                return finish_failed_adb_recovery(
                    log, event, dispatch, done, args.steps)
            if dispatch.outcome == "rescan":
                event["action"] = "WAIT: ADB recovered; fresh planning required"
                event["adb_recovery"] = list(dispatch.recovery_events)
                bot.log_event(log, event)
                continue
            append_confirmed_action(sent, dispatch, {
                "type": kind, "target_cell": list(target), "adb_xy": [x, y],
            })
            pickup = item_category(info[target]) if kind == "move" else None
            if pickup:
                collected[pickup] += 1
                picked_up = True

            # Never batch through an attack or an orange pickup animation.
            first_has_item = info[target]["item"] > .06
            if kind == "move" and not first_has_item and done + 1 < args.steps:
                remaining = min(effective_batch_size - 1, args.steps - done - 1)
                if loop_guard:
                    remaining = 0
                followups = safe_followup_moves(
                    info, player, target, direction, remaining, item_goals)
                for screen_target, checked in followups:
                    if not controlled_wait(args.pause_file, args.stop_file, args.interval):
                        emit_status("stopped", "Automazione fermata", done + len(sent), args.steps)
                        return 0
                    x2, y2 = bot.cell_center(det.board, *screen_target)
                    dispatch = dispatch_worker_tap(
                        args, log, x2, y2, "followup_move",
                        action_settle_seconds(
                            "move", info[checked]["item"] > .06,
                            args.interval))
                    if dispatch.outcome == "stopped":
                        event["action"] = sent
                        event["batch_interrupted"] = "stop requested during ADB recovery"
                        bot.log_event(log, event)
                        emit_status("stopped", "Automazione fermata",
                                    done + len(sent), args.steps)
                        return 0
                    if dispatch.outcome == "failed":
                        event["confirmed_actions"] = sent
                        return finish_failed_adb_recovery(
                            log, event, dispatch, done + len(sent), args.steps)
                    if dispatch.outcome == "rescan":
                        event["adb_recovery"] = list(dispatch.recovery_events)
                        event["batch_interrupted"] = (
                            "failed follow-up not counted; fresh planning required"
                        )
                        rescan_required = True
                        break
                    append_confirmed_action(sent, dispatch, {
                        "type": "move", "target_cell": list(screen_target),
                        "validated_from_cell": list(checked), "adb_xy": [x2, y2],
                    })
                    pickup = item_category(info[checked])
                    if pickup:
                        collected[pickup] += 1
                        picked_up = True

        event["action"] = sent
        event["collected_detected"] = dict(collected)
        bot.log_event(log, event)
        done += len(sent)
        emit_status("running", f"{len(sent)} azione/i eseguite", done, args.steps,
                    action=kind)
        if args.verbose:
            progress(done, args.steps, f"{len(sent)} Aktion(en) ausgefuehrt - neuer Scan", "32")
        elif progress_step and (done >= next_progress or done >= args.steps):
            elapsed = time.monotonic() - started_at
            progress(done, args.steps, progress_summary(done, args.steps, elapsed), "32")
            while next_progress <= done:
                next_progress += progress_step
        previous_action = kind
        if kind == "move":
            previous_move_player = player
            previous_move_expected = expected_player_after_moves(
                player,
                [tuple(action["target_cell"]) for action in sent
                 if action["type"] == "move"],
                direction,
            )
            previous_move_actions = sum(
                action["type"] == "move" for action in sent)
        pickup_animation_pending = picked_up
        previous_attack_target = target if kind == "attack" else None
        if kind == "attack":
            previous_attack_checks = 0
        if kind == "dash":
            previous_dash_player = player
            previous_dash_obstacles = consecutive_right_obstacles(info, player)
            previous_dash_signature = dash_scene_signature(info, player)
        previous_direction = direction
        if rescan_required:
            continue
        if not controlled_wait(
                args.pause_file, args.stop_file,
                action_settle_seconds(kind, picked_up, args.interval)):
            emit_status("stopped", "Automazione fermata", done, args.steps)
            return 0

    final = bot.screenshot(args.adb, args.serial)
    final_det = bot.classify(final)
    final.save(run_dir / "final.png")
    bot.diagnostic(final, final_det).save(
        run_dir / "final_diagnostic.png")
    event = {"time_utc": datetime.now(timezone.utc).isoformat(), "status": "complete",
             "steps": done, "run_dir": str(run_dir),
             "detection": bot.asdict(final_det)}
    energy_end = read_energy_counter(final)
    event["collected_detected"] = dict(collected)
    event["energy_hud"] = {
        "start": energy_start,
        "end": energy_end,
        "difference": (energy_end - energy_start
                       if energy_start is not None and energy_end is not None else None),
    }
    event["elapsed_seconds"] = round(time.monotonic() - started_at, 3)
    bot.log_event(log, event)
    show_run_summary(done, args.steps, started_at, collected, energy_start, energy_end)
    emit_status("complete", "Automazione completata", done, args.steps,
                run_dir=str(run_dir))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
