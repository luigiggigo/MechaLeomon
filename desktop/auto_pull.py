"""Isolated Summon automation loop for the desktop controller."""

from __future__ import annotations

import random
from datetime import datetime, timezone

import digiworld_bot as bot

AUTO_PULL_POLL_SECONDS = .25
AUTO_PULL_ACTION_SETTLE_SECONDS = 1.20
AUTO_PULL_35X_DELAY_RANGE = (.25, .45)


def run(args, log, *, pull_kind, emit_status, controlled_wait,
        dispatch_worker_tap, finish_failed_adb_recovery):
    """Run the explicitly selected 10x or 35x Summon workflow."""
    if pull_kind not in {"10x", "35x"}:
        raise ValueError(f"unsupported Auto Pull kind: {pull_kind}")

    mode = f"auto_pull_{pull_kind}"
    label = f"{pull_kind} Summon"
    repeatable_35x = pull_kind == "35x"
    done = 0
    blocked_kind = None
    latched_35x_button = None
    last_status = None

    def report(message):
        nonlocal last_status
        status = (message, done)
        if status != last_status:
            emit_status("running", message, done, args.steps,
                        mode=mode)
            last_status = status

    while done < args.steps:
        if not controlled_wait(args.pause_file, args.stop_file):
            emit_status("stopped", f"Auto Pull {pull_kind} fermato",
                        done, args.steps, mode=mode)
            return 0

        if repeatable_35x and latched_35x_button is not None:
            # Skill/Support hides the 35x control between consecutive taps.
            # Once its lower action row has been visually anchored, keep the
            # exact framebuffer-relative point without paying the screenshot
            # latency again. Pause/Stop is still checked before every input.
            button = latched_35x_button
            visual_anchor = "latched_35x"
            confirmation = None
        else:
            image = bot.screenshot(args.adb, args.serial)
            confirmation = (
                None if repeatable_35x else
                bot.summon_10x_confirmation_button(image)
            )
            button = confirmation or bot.summon_10x_button(image)
            visual_anchor = "current_frame"
            if repeatable_35x and button is not None:
                latched_35x_button = button
        action_kind = (
            "confirm" if confirmation is not None else
            "summon_35x" if repeatable_35x else
            "summon"
        )
        stamp = datetime.now(timezone.utc).isoformat()

        if button is None:
            # A confirmed disappearance rearms the detector. This prevents a
            # slow transition from receiving the same paid action twice.
            blocked_kind = None
            report(f"Auto Pull: cerco {label}…")
            if not controlled_wait(args.pause_file, args.stop_file,
                                   AUTO_PULL_POLL_SECONDS):
                emit_status("stopped", f"Auto Pull {pull_kind} fermato",
                            done, args.steps, mode=mode)
                return 0
            continue

        if action_kind == blocked_kind and not repeatable_35x:
            report("Auto Pull: attendo il cambio schermata…")
            if not controlled_wait(args.pause_file, args.stop_file,
                                   AUTO_PULL_POLL_SECONDS):
                emit_status("stopped", f"Auto Pull {pull_kind} fermato",
                            done, args.steps, mode=mode)
                return 0
            continue

        event = {
            "time_utc": stamp,
            "mode": mode,
            "next_index": done,
            "button": list(button),
            "control": action_kind,
            "visual_anchor": visual_anchor,
        }
        action_delay = (
            random.uniform(*AUTO_PULL_35X_DELAY_RANGE)
            if repeatable_35x else AUTO_PULL_ACTION_SETTLE_SECONDS
        )
        report(
            "Auto Pull: continuo 35x Summon…"
            if visual_anchor == "latched_35x" else
            f"Auto Pull: {label} rilevato…"
        )
        dispatch = dispatch_worker_tap(
            args, log, button[0], button[1],
            f"auto_pull_{'35x_repeat' if repeatable_35x else '10x_' + action_kind}",
            action_delay,
        )
        if dispatch.outcome == "stopped":
            emit_status("stopped", f"Auto Pull {pull_kind} fermato",
                        done, args.steps, mode=mode)
            return 0
        if dispatch.outcome == "failed":
            return finish_failed_adb_recovery(
                log, event, dispatch, done, args.steps)
        if dispatch.outcome == "rescan":
            event["action"] = f"RESCAN: ambiguous {label} input"
            event["adb_recovery"] = list(dispatch.recovery_events)
            bot.log_event(log, event)
            # An ambiguous input must always return to visual planning. In
            # particular, do not keep using a latched 35x coordinate across
            # transport recovery.
            latched_35x_button = None
            continue

        done += 1
        blocked_kind = action_kind
        event["action"] = f"sent: {label} {action_kind}"
        event["settle_seconds"] = action_delay
        event["serial"] = dispatch.serial
        bot.log_event(log, event)
        report(f"Auto Pull: {label} premuto")
        if not controlled_wait(args.pause_file, args.stop_file,
                               action_delay):
            emit_status("stopped", f"Auto Pull {pull_kind} fermato",
                        done, args.steps, mode=mode)
            return 0

    emit_status("complete", f"Auto Pull {pull_kind} completato",
                done, args.steps, mode=mode)
    return 0
