#!/usr/bin/env python3
"""Conservative ADB observer/controller for Digimon UP DigiWorld.

Input is disabled unless --allow-input is supplied. Even then, taps are only
accepted after a playfield has been detected with sufficient confidence.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFont

ADB_DEFAULT = "auto"
SERIAL_DEFAULT = "auto"
ADB_COMMAND_TIMEOUT = 15.0
ADB_RECONNECT_ATTEMPTS = 3
ADB_RECONNECT_BACKOFF = 0.25
ADB_DEFAULT_ACTION_SETTLE = 1.20
ADB_RECOVERABLE_ERRORS = (subprocess.SubprocessError, OSError)


def _run_hidden(command, **kwargs):
    """Run a command without allocating a flashing Windows console window."""
    no_window = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    if no_window:
        kwargs.setdefault("creationflags", no_window)
    # Wrapper generico: il call site decide check (tap usa check=True);
    # il default resta quello di subprocess.run, solo dichiarato esplicito.
    check = kwargs.pop("check", False)
    return subprocess.run(command, check=check, **kwargs)


def resolve_adb(requested: str = ADB_DEFAULT) -> str:
    """Find BlueStacks' bundled ADB or a compatible ADB on PATH."""
    explicit = requested if requested and requested.lower() != "auto" else None
    candidates = []
    if explicit:
        candidates.append(explicit)
    if os.environ.get("DIGIWORLD_ADB"):
        candidates.append(os.environ["DIGIWORLD_ADB"])

    for env_name in ("ProgramFiles", "ProgramFiles(x86)"):
        base = os.environ.get(env_name)
        if base:
            candidates.extend([
                str(Path(base) / "BlueStacks_nxt" / "HD-Adb.exe"),
                str(Path(base) / "BlueStacks" / "HD-Adb.exe"),
            ])
    for command in ("HD-Adb.exe", "HD-Adb", "adb.exe", "adb"):
        found = shutil.which(command)
        if found:
            candidates.append(found)

    seen = set()
    for candidate in candidates:
        normalized = os.path.normcase(os.path.abspath(os.path.expandvars(candidate)))
        if normalized in seen:
            continue
        seen.add(normalized)
        if Path(candidate).is_file():
            return str(Path(candidate).resolve())

    if explicit:
        raise RuntimeError(f"ADB wurde nicht gefunden: {explicit}")
    raise RuntimeError(
        "ADB wurde nicht gefunden. Installiere BlueStacks 5 oder setze "
        "DIGIWORLD_ADB auf den vollstaendigen Pfad zu HD-Adb.exe."
    )


def _device_rows(adb_path: str):
    result = _run_hidden([adb_path, "devices"], check=True,
                         capture_output=True, text=True,
                         timeout=ADB_COMMAND_TIMEOUT)
    rows = []
    for line in result.stdout.splitlines()[1:]:
        parts = line.strip().split()
        if len(parts) >= 2:
            rows.append((parts[0], parts[1]))
    return rows


def requested_serial_value(requested: str = SERIAL_DEFAULT) -> str:
    """Preserve the user's serial intent, including an environment override."""
    if requested and requested.lower() != SERIAL_DEFAULT:
        return requested
    return os.environ.get("DIGIWORLD_SERIAL") or SERIAL_DEFAULT


def resolve_serial(adb_path: str, requested: str = SERIAL_DEFAULT) -> str:
    """Select a connected BlueStacks device without assuming a fixed port."""
    requested = requested_serial_value(requested)
    explicit = requested if requested.lower() != SERIAL_DEFAULT else None

    if explicit and ":" in explicit:
        _run_hidden([adb_path, "connect", explicit], capture_output=True,
                    text=True, timeout=ADB_COMMAND_TIMEOUT)

    rows = _device_rows(adb_path)
    if not explicit and not any(
            serial == "127.0.0.1:5555" and state == "device"
            for serial, state in rows):
        # HD-Adb can expose the same BlueStacks instance as emulator-5554
        # before its stable TCP endpoint has been connected. Probe TCP even
        # when another device is already listed, then prefer it below.
        try:
            _run_hidden([adb_path, "connect", "127.0.0.1:5555"],
                        capture_output=True, text=True,
                        timeout=ADB_COMMAND_TIMEOUT)
            rows = _device_rows(adb_path)
        except ADB_RECOVERABLE_ERRORS:
            # The already-listed device remains a valid conservative fallback.
            pass
    devices = [serial for serial, state in rows if state == "device"]
    if explicit:
        if explicit in devices:
            return explicit
        raise RuntimeError(
            f"ADB-Geraet {explicit!r} ist nicht bereit. Geraete: {rows or 'keine'}"
        )

    if not devices:
        raise RuntimeError(
            "Kein ADB-Geraet gefunden. Starte BlueStacks und aktiviere unter "
            "Einstellungen > Erweitert den Android Debug Bridge-Schalter."
        )

    if "127.0.0.1:5555" in devices:
        return "127.0.0.1:5555"
    tcp_devices = [serial for serial in devices
                   if serial.startswith("127.0.0.1:")]
    if len(tcp_devices) == 1:
        return tcp_devices[0]
    if not tcp_devices and "emulator-5554" in devices:
        return "emulator-5554"
    if len(devices) == 1:
        return devices[0]
    raise RuntimeError(
        "Mehrere ADB-Geraete gefunden. Setze DIGIWORLD_SERIAL oder verwende "
        f"--serial. Geraete: {devices}"
    )


@dataclass
class Detection:
    state: str
    confidence: float
    board: tuple[int, int, int, int] | None
    reason: str


def adb(adb_path: str, serial: str, *args: str, binary: bool = False):
    cmd = [adb_path, "-s", serial, *args]
    return _run_hidden(cmd, check=True, capture_output=True,
                       text=not binary, timeout=ADB_COMMAND_TIMEOUT).stdout


def close_tap_session() -> None:
    """Backward-compatible no-op after removing the persistent ADB shell."""


class AdbInputError(RuntimeError):
    """A checked ADB input failed and must not be treated as dispatched."""

    def __init__(self, command: list[str], serial: str, cause: BaseException):
        super().__init__(f"ADB input failed on {serial}")
        self.command = command
        self.serial = serial
        self.cause = cause
        self.returncode = getattr(cause, "returncode", None)
        self.stdout = getattr(cause, "stdout", None)
        self.stderr = getattr(cause, "stderr", None)


@dataclass(frozen=True)
class TapDispatchResult:
    outcome: str
    serial: str
    recovery_events: tuple[dict, ...] = ()


def _error_text(value) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace").strip()
    return str(value).strip()


def _adb_error_details(exc: BaseException) -> dict:
    cause = exc.cause if isinstance(exc, AdbInputError) else exc
    return {
        "error_type": type(cause).__name__,
        "return_code": getattr(cause, "returncode", None),
        "stdout": _error_text(getattr(cause, "stdout", None)),
        "stderr": _error_text(getattr(cause, "stderr", None)),
        "error": str(cause),
    }


def tap(adb_path: str, serial: str, x: int, y: int) -> None:
    """Send one checked ADB command, wrapping every recoverable pipe failure."""
    command = [adb_path, "-s", serial, "shell", "input", "tap",
               str(int(x)), str(int(y))]
    try:
        adb(adb_path, serial, "shell", "input", "tap",
            str(int(x)), str(int(y)))
    except ADB_RECOVERABLE_ERRORS as exc:
        raise AdbInputError(command, serial, exc) from None


def swipe(adb_path: str, serial: str, x1: int, y1: int, x2: int, y2: int,
          duration_ms: int = 250) -> None:
    """Send one checked ADB swipe, wrapping every recoverable pipe failure."""
    values = tuple(str(int(value)) for value in (x1, y1, x2, y2, duration_ms))
    command = [adb_path, "-s", serial, "shell", "input", "swipe", *values]
    try:
        adb(adb_path, serial, "shell", "input", "swipe", *values)
    except ADB_RECOVERABLE_ERRORS as exc:
        raise AdbInputError(command, serial, exc) from None


def _dispatch_input_with_recovery(
        adb_path: str, serial: str, *, send_input, command: list[str],
        input_coordinates: dict, action: str,
        requested_serial: str = SERIAL_DEFAULT,
        should_continue=None, logger=None,
        reconnect_attempts: int = ADB_RECONNECT_ATTEMPTS,
        reconnect_backoff: float = ADB_RECONNECT_BACKOFF,
        recovery_settle_seconds: float = ADB_DEFAULT_ACTION_SETTLE,
) -> TapDispatchResult:
    """Dispatch one input or recover safely before returning for a fresh plan.

    The input command is attempted exactly once. Recovery attempts only
    restore the ADB transport and observe the resulting state. A failed input
    is ambiguous because Android may have received it despite ADB's non-zero
    exit, so this function never repeats the input itself. After reconnecting it
    waits for the possibly-applied action to settle, verifies screenshots work,
    and returns ``rescan`` so the caller replans from its normal next frame.
    """
    requested_serial = requested_serial_value(requested_serial)

    def default_continue(seconds):
        time.sleep(max(0.0, seconds))
        return True

    wait = should_continue or default_continue
    events = []

    def record(payload):
        events.append(payload)
        if logger is not None:
            logger(payload)

    if not wait(0.0):
        return TapDispatchResult("stopped", serial)
    try:
        send_input()
        return TapDispatchResult("sent", serial)
    except (AdbInputError,) + ADB_RECOVERABLE_ERRORS as exc:
        details = _adb_error_details(exc)
        failed_command = exc.command if isinstance(exc, AdbInputError) else command
        record({
            "status": "input_failed",
            "action": action,
            **input_coordinates,
            "command": failed_command,
            "requested_serial": requested_serial,
            "active_serial": serial,
            "attempt": 1,
            **details,
        })

    current_serial = serial
    for attempt in range(1, max(1, reconnect_attempts) + 1):
        if not wait(reconnect_backoff):
            record({
                "status": "stopped",
                "action": action,
                "requested_serial": requested_serial,
                "active_serial": current_serial,
                "reconnect_attempt": attempt,
                "phase": "reconnect_backoff",
            })
            return TapDispatchResult("stopped", current_serial, tuple(events))
        try:
            recovered_serial = resolve_serial(adb_path, requested_serial)
        except ADB_RECOVERABLE_ERRORS + (RuntimeError,) as exc:
            record({
                "status": "recovery_failed",
                "action": action,
                "requested_serial": requested_serial,
                "active_serial": current_serial,
                "reconnect_attempt": attempt,
                "phase": "rediscovery",
                **_adb_error_details(exc),
            })
            continue
        current_serial = recovered_serial
        if not wait(recovery_settle_seconds):
            record({
                "status": "stopped",
                "action": action,
                "requested_serial": requested_serial,
                "active_serial": current_serial,
                "new_serial": recovered_serial,
                "reconnect_attempt": attempt,
                "phase": "action_settle",
                "settle_seconds": recovery_settle_seconds,
            })
            return TapDispatchResult("stopped", current_serial, tuple(events))
        try:
            screenshot(adb_path, recovered_serial)
        except ADB_RECOVERABLE_ERRORS as exc:
            record({
                "status": "recovery_failed",
                "action": action,
                "requested_serial": requested_serial,
                "active_serial": current_serial,
                "reconnect_attempt": attempt,
                "phase": "observation",
                "settle_seconds": recovery_settle_seconds,
                **_adb_error_details(exc),
            })
            continue
        record({
            "status": "recovered_rescan_required",
            "action": action,
            "requested_serial": requested_serial,
            "active_serial": serial,
            "new_serial": recovered_serial,
            "reconnect_attempt": attempt,
            "settle_seconds": recovery_settle_seconds,
        })
        return TapDispatchResult("rescan", recovered_serial, tuple(events))

    record({
        "status": "recovery_exhausted",
        "action": action,
        "requested_serial": requested_serial,
        "active_serial": current_serial,
        "reconnect_attempts": max(1, reconnect_attempts),
    })
    return TapDispatchResult("failed", current_serial, tuple(events))


def dispatch_tap_with_recovery(
        adb_path: str, serial: str, x: int, y: int, *, action: str = "tap",
        requested_serial: str = SERIAL_DEFAULT,
        should_continue=None, logger=None,
        reconnect_attempts: int = ADB_RECONNECT_ATTEMPTS,
        reconnect_backoff: float = ADB_RECONNECT_BACKOFF,
        recovery_settle_seconds: float = ADB_DEFAULT_ACTION_SETTLE,
) -> TapDispatchResult:
    command = [adb_path, "-s", serial, "shell", "input", "tap",
               str(int(x)), str(int(y))]
    return _dispatch_input_with_recovery(
        adb_path, serial,
        send_input=lambda: tap(adb_path, serial, x, y),
        command=command, input_coordinates={"adb_xy": [int(x), int(y)]},
        action=action, requested_serial=requested_serial,
        should_continue=should_continue, logger=logger,
        reconnect_attempts=reconnect_attempts,
        reconnect_backoff=reconnect_backoff,
        recovery_settle_seconds=recovery_settle_seconds,
    )


def dispatch_swipe_with_recovery(
        adb_path: str, serial: str, x1: int, y1: int, x2: int, y2: int,
        duration_ms: int = 250, *, action: str = "swipe",
        requested_serial: str = SERIAL_DEFAULT,
        should_continue=None, logger=None,
        reconnect_attempts: int = ADB_RECONNECT_ATTEMPTS,
        reconnect_backoff: float = ADB_RECONNECT_BACKOFF,
        recovery_settle_seconds: float = ADB_DEFAULT_ACTION_SETTLE,
) -> TapDispatchResult:
    values = [int(value) for value in (x1, y1, x2, y2, duration_ms)]
    command = [adb_path, "-s", serial, "shell", "input", "swipe",
               *(str(value) for value in values)]
    return _dispatch_input_with_recovery(
        adb_path, serial,
        send_input=lambda: swipe(adb_path, serial, *values),
        command=command, input_coordinates={"adb_swipe": values},
        action=action, requested_serial=requested_serial,
        should_continue=should_continue, logger=logger,
        reconnect_attempts=reconnect_attempts,
        reconnect_backoff=reconnect_backoff,
        recovery_settle_seconds=recovery_settle_seconds,
    )


def screenshot(adb_path: str, serial: str) -> Image.Image:
    if not adb_path or adb_path.lower() == "auto":
        adb_path = resolve_adb(adb_path)
    if not serial or serial.lower() == "auto":
        serial = resolve_serial(adb_path, serial)
    raw = adb(adb_path, serial, "exec-out", "screencap", "-p", binary=True)
    from io import BytesIO
    return Image.open(BytesIO(raw)).convert("RGB")


def _components(mask: np.ndarray):
    """Connected components on a small boolean mask, without OpenCV/SciPy."""
    seen = np.zeros(mask.shape, dtype=bool)
    result = []
    height, width = mask.shape
    for sy, sx in zip(*np.nonzero(mask & ~seen)):
        if seen[sy, sx]:
            continue
        stack, seen[sy, sx] = [(int(sy), int(sx))], True
        x0 = x1 = int(sx); y0 = y1 = int(sy); area = 0
        while stack:
            y, x = stack.pop(); area += 1
            x0, x1, y0, y1 = min(x0, x), max(x1, x), min(y0, y), max(y1, y)
            for ny, nx in ((y-1,x),(y+1,x),(y,x-1),(y,x+1)):
                if 0 <= ny < height and 0 <= nx < width and mask[ny,nx] and not seen[ny,nx]:
                    seen[ny,nx] = True; stack.append((ny,nx))
        result.append((x0, y0, x1 + 1, y1 + 1, area))
    return result


def _best_six_lines(score: np.ndarray, start_range: range, step_range: range):
    """Find six near-equidistant edges; tolerate one edge hidden by a sprite."""
    starts = np.asarray(list(start_range), dtype=np.int32)
    steps = np.asarray(list(step_range), dtype=np.int32)
    if not len(score) or not starts.size or not steps.size:
        return None

    # Evaluate every (step, start, line, +/-2 px) candidate in one NumPy
    # operation. This is equivalent to the former nested Python loops, but it
    # removes roughly 400 ms from every 900x1600 frame on the Windows build.
    expected = (
        steps[:, None, None] * np.arange(6, dtype=np.int32)[None, None, :]
        + starts[None, :, None]
    )
    offsets = np.arange(-2, 3, dtype=np.int32)
    sample_indices = expected[..., None] + offsets
    samples_valid = ((sample_indices >= 0) &
                     (sample_indices < len(score)))
    clipped = np.clip(sample_indices, 0, len(score) - 1)
    samples = np.where(samples_valid, score[clipped], -np.inf)
    local_offsets = np.argmax(samples, axis=-1)
    positions = np.take_along_axis(
        sample_indices, local_offsets[..., None], axis=-1
    )[..., 0]
    values = np.take_along_axis(
        samples, local_offsets[..., None], axis=-1
    )[..., 0]

    # The second weakest line dominates the score. This prevents two strong
    # panel borders from masquerading as a five-cell grid.
    qualities = np.sort(values, axis=-1)[..., 1] + .10 * np.mean(values, axis=-1)
    candidates_valid = ((expected[..., 0] >= 0) &
                        (expected[..., -1] < len(score)))
    qualities = np.where(candidates_valid, qualities, -np.inf)
    flat_index = int(np.argmax(qualities))
    step_index, start_index = np.unravel_index(flat_index, qualities.shape)
    quality = float(qualities[step_index, start_index])
    if not np.isfinite(quality):
        return None
    return (
        quality,
        positions[step_index, start_index].astype(int).tolist(),
        values[step_index, start_index].astype(float).tolist(),
    )


def _has_distributed_cell_texture(
        gray: np.ndarray, board: tuple[int, int, int, int]) -> bool:
    """Require non-uniform content across a candidate's 5x5 cell interiors.

    Dense pyramids can hide three horizontal borders while leaving the full
    vertical lattice intact.  Grayscale texture distributed across the cell
    interiors is additional structural evidence for that narrow case; unlike
    a color threshold, it does not calibrate the detector to one tile palette.
    """
    height, width = gray.shape
    x0, y0, x1, y1 = board
    x0, y0 = max(0, x0), max(0, y0)
    x1, y1 = min(width, x1), min(height, y1)
    if x1 - x0 < 50 or y1 - y0 < 50:
        return False

    textured_cells = 0
    for row in range(5):
        for column in range(5):
            cell_x0 = round(x0 + (column + .12) * (x1 - x0) / 5)
            cell_x1 = round(x0 + (column + .88) * (x1 - x0) / 5)
            cell_y0 = round(y0 + (row + .12) * (y1 - y0) / 5)
            cell_y1 = round(y0 + (row + .88) * (y1 - y0) / 5)
            interior = gray[cell_y0:cell_y1, cell_x0:cell_x1]
            if interior.size and float(np.std(interior)) >= 10.0:
                textured_cells += 1
    return textured_cells >= 12


def _detect_grid(image: Image.Image) -> Detection | None:
    """Return a DigiWorld detection only for a reliable 5x5 grid candidate."""
    w, h = image.size
    gray = np.asarray(image, dtype=float).mean(axis=2)
    # Vertical lines are evaluated through the upper play area, horizontal
    # lines through the centered content area. Coordinates remain framebuffer-
    # relative and are independent of the Windows window position.
    x_edges = np.mean(np.abs(np.diff(gray[int(.30*h):int(.70*h)], axis=1)), axis=0)
    y_edges = np.mean(np.abs(np.diff(gray[:, int(.10*w):int(.90*w)], axis=0)), axis=1)
    xb = _best_six_lines(x_edges,
                         range(int(.05*w), int(.25*w)),
                         range(int(.10*w), int(.22*w)))
    yb = _best_six_lines(y_edges,
                         # The board begins in the upper two fifths. Allowing
                         # later starts can mistake rows 1-5 plus the progress
                         # panel for a shifted six-line grid when row 0 is full
                         # of pyramids.
                         range(int(.20*h), int(.39*h)),
                         range(int(.045*h), int(.10*h)))
    if xb and yb:
        x_quality, xp, xv = xb
        y_quality, yp, yv = yb
        x_second = sorted(xv)[1]
        y_second = sorted(yv)[1]
        xi = np.polyfit(np.arange(6), np.asarray(xp), 1)
        yi = np.polyfit(np.arange(6), np.asarray(yp), 1)
        board = (round(xi[1]), round(yi[1]),
                 round(xi[1] + 5*xi[0]), round(yi[1] + 5*yi[0]))
        bw, bh = board[2] - board[0], board[3] - board[1]
        geometry_reliable = (
            .85 <= bw / max(bh, 1) <= 1.55 and
            .20 <= bw*bh/(w*h) <= .45
        )
        # A row containing several pyramids can almost erase the upper two
        # horizontal borders.  Accept that narrowly observed case only when
        # the six vertical grid lines remain exceptionally strong.
        # Dense pyramids plus highlighted cyan cells can slightly weaken a
        # second vertical border as well.  Keep this exception structural:
        # the aggregate six-line evidence must still be strong, rather than
        # accepting the frame from the weak horizontal score alone.
        pyramid_occlusion = (
            x_second >= 19.0 and x_quality >= 22.0 and
            13.0 <= y_second < 15.0
        )
        # Several pyramids spread across different rows can obscure more than
        # the upper pair of horizontal borders.  Accept that heavier pattern
        # only with exceptional vertical evidence and a still-coherent
        # aggregate horizontal score.
        heavy_pyramid_occlusion = (
            x_second >= 20.0 and x_quality >= 25.0 and
            y_second >= 12.0 and y_quality >= 14.5
        )
        # A sprite plus pyramids on several rows can erase two horizontal
        # intersections entirely.  In that case do not score by the second
        # weakest line: require all but those two horizontal lines to remain
        # strong, together with an exceptionally reliable vertical lattice.
        multi_row_pyramid_occlusion = (
            x_second >= 20.0 and x_quality >= 24.0 and
            sorted(yv)[2] >= 25.0
        )
        # In the heaviest observed layout only three horizontal borders remain
        # strong.  Accept it only when all six vertical borders remain
        # consistently strong and the fitted 5x5 area has non-uniform content
        # distributed through many cell interiors.  This extra evidence keeps
        # a flat menu or panel from passing merely because a few of its edges
        # happen to be equidistant.
        textured_three_line_occlusion = (
            geometry_reliable and
            x_second >= 19.5 and x_quality >= 22.0 and
            y_second >= 11.5 and y_quality >= 14.0 and
            sum(value >= 25.0 for value in yv) >= 3 and
            _has_distributed_cell_texture(gray, board)
        )
        edges_reliable = (
            (x_second >= 15.0 and y_second >= 15.0) or
            pyramid_occlusion or heavy_pyramid_occlusion or
            multi_row_pyramid_occlusion or textured_three_line_occlusion
        )
        if edges_reliable and geometry_reliable:
            # Passing both edge thresholds plus aspect/coverage is already
            # the conservative acceptance gate.  Calibrate its confidence
            # floor to the controller's 0.80 requirement; otherwise a frame
            # can be labelled DigiWorld and then rejected by the worker using
            # the same detector.
            confidence = min(.98, .72 + .005 * min(x_quality, y_quality))
            # Reaching this branch already requires six very strong vertical
            # borders plus valid board aspect and coverage.  The raw minimum-
            # quality formula undervalues that evidence only because pyramids
            # cover the first horizontal line.
            if (pyramid_occlusion or heavy_pyramid_occlusion or
                    multi_row_pyramid_occlusion or
                    textured_three_line_occlusion):
                confidence = max(.805, confidence)
            return Detection("digiworld", confidence, board,
                             "six equidistant vertical and horizontal grid edges")
    return None


def _has_prominent_lower_cyan(image: Image.Image) -> bool:
    """Return whether the lower-center region resembles the menu control."""
    w, h = image.size
    lower = np.asarray(image.crop((int(.15*w), int(.78*h), int(.85*w), int(.90*h))))
    lr, lg, lb = lower[:, :, 0], lower[:, :, 1], lower[:, :, 2]
    start_blue = (lb > 100) & (lg > 80) & (lb.astype(int) > lr.astype(int) + 20)
    return np.count_nonzero(start_blue) / start_blue.size > 0.08


def summon_10x_button(image: Image.Image) -> tuple[int, int] | None:
    """Return the active 10x Summon button center from the summon screen.

    The detector anchors the yellow control to the similarly sized blue 1x
    Summon button beside it.  Requiring the aligned pair avoids treating
    unrelated yellow cards, counters or notification badges as an input.
    """
    a = np.asarray(image.convert("RGB"))
    h, w = a.shape[:2]
    if h < 100 or w < 100:
        return None
    r, g, b = (a[:, :, index].astype(np.int16) for index in range(3))
    yellow = ((r > 185) & (g > 105) & (g < 235) & (b < 105) &
              (r > g + 18) & (g > b + 45))
    blue = ((g > 80) & (b > 115) & (g > r + 25) & (b > r + 40))

    def lower_buttons(mask):
        result = []
        for component in _components(mask[int(.72*h):]):
            x0, y0, x1, y1, area = component
            y0 += int(.72*h)
            y1 += int(.72*h)
            width, height = x1 - x0, y1 - y0
            fill = area / max(1, width * height)
            if (int(.18*w) <= width <= int(.38*w) and
                    int(.025*h) <= height <= int(.09*h) and
                    2.2 <= width / max(1, height) <= 4.8 and
                    fill >= .55):
                result.append((x0, y0, x1, y1, area))
        return result

    yellow_buttons = lower_buttons(yellow)
    blue_buttons = lower_buttons(blue)
    pairs = []
    for yellow_button in yellow_buttons:
        yx0, yy0, yx1, yy1, yarea = yellow_button
        yw, yh = yx1 - yx0, yy1 - yy0
        yc = (yy0 + yy1) / 2
        for blue_button in blue_buttons:
            bx0, by0, bx1, by1, _ = blue_button
            bw, bh = bx1 - bx0, by1 - by0
            bc = (by0 + by1) / 2
            gap = yx0 - bx1
            if (bx1 < yx0 and .005*w <= gap <= .14*w and
                    abs(yc - bc) <= .025*h and
                    .72 <= yw / max(1, bw) <= 1.38 and
                    .70 <= yh / max(1, bh) <= 1.42):
                pairs.append((yarea, yellow_button))
    if not pairs:
        return None
    _, (x0, y0, x1, y1, _) = max(pairs)
    return (round((x0 + x1) / 2), round((y0 + y1) / 2))


def summon_button_is_repeatable_35x(
        image: Image.Image, button: tuple[int, int]) -> bool:
    """Identify the lower 35x action row used by Skill/Support rewards.

    The normal paid Summon control sits above this bottom shelf.  Keeping the
    distinction relative to the framebuffer preserves the anti-double-tap
    gate for 10x while supporting both cropped and full-height captures.
    ``button`` has already passed :func:`summon_10x_button`'s aligned-pair
    structural checks; vertical position alone never authorizes an input.
    """
    width, height = image.size
    if (width < 100 or height < 100 or
            button[1] < round(.92 * height)):
        return False

    # Both full-height reward screens put their yellow control on the same
    # bottom shelf.  The reliable distinction is the currency marker directly
    # above it: Skill/Support 35x uses a saturated green ticket, while Crest
    # 10x uses a pale violet ticket.  Position alone misclassifies one flow or
    # the other, and generic dark/light price glyphs occur in both screens.
    x, y = button
    x0, x1 = (max(0, round(x - .080*width)),
              min(width, round(x + .020*width)))
    y0, y1 = (max(0, round(y - .075*height)),
              max(0, round(y - .020*height)))
    price_band = np.asarray(image.convert("RGB"))[y0:y1, x0:x1]
    if not price_band.size:
        return False
    pr, pg, pb = (price_band[:, :, index].astype(np.int16)
                  for index in range(3))
    green_ticket = ((pg > 140) & (pg > pr + 40) &
                    (pg > pb + 20))
    return bool(np.mean(green_ticket) >= .025)


def summon_10x_confirmation_button(image: Image.Image) -> tuple[int, int] | None:
    """Return the 10x button only inside the complete Crest confirmation modal.

    The confirmation control has no 1x sibling.  Anchor it instead to the
    modal's broad blue card, cyan title plaque and dark explanatory panel so a
    lone yellow game control cannot authorize a paid action.
    """
    a = np.asarray(image.convert("RGB"))
    h, w = a.shape[:2]
    if h < 100 or w < 100:
        return None
    r, g, b = (a[:, :, index].astype(np.int16) for index in range(3))
    yellow = ((r > 185) & (g > 105) & (g < 235) & (b < 105) &
              (r > g + 18) & (g > b + 45))
    cyan = ((g > 125) & (b > 140) & (g > r + 35) & (b > r + 50))
    modal_blue = ((r < 80) & (g > 45) & (g < 155) & (b > 90) &
                  (g > r + 20) & (b > g + 15))
    dark_panel = ((r < 55) & (g < 105) & (b < 130) &
                  (b < g + 55))

    def candidates(mask, width_range, height_range, center_y_range,
                   crop_y_range, fill_min=.35):
        result = []
        crop_x0, crop_x1 = int(.08*w), int(.92*w)
        crop_y0, crop_y1 = (int(crop_y_range[0]*h),
                            int(crop_y_range[1]*h))
        for local in _components(mask[crop_y0:crop_y1, crop_x0:crop_x1]):
            lx0, ly0, lx1, ly1, area = local
            component = (lx0 + crop_x0, ly0 + crop_y0,
                         lx1 + crop_x0, ly1 + crop_y0, area)
            x0, y0, x1, y1, area = component
            width, height = x1 - x0, y1 - y0
            center_x = (x0 + x1) / (2*w)
            center_y = (y0 + y1) / (2*h)
            fill = area / max(1, width * height)
            if (width_range[0]*w <= width <= width_range[1]*w and
                    height_range[0]*h <= height <= height_range[1]*h and
                    abs(center_x - .5) <= .16 and
                    center_y_range[0] <= center_y <= center_y_range[1] and
                    fill >= fill_min):
                result.append(component)
        return result

    title_band = cyan[int(.30*h):int(.40*h), int(.20*w):int(.80*w)]
    title_evidence = bool(
        title_band.size and np.mean(title_band) >= .18 and
        np.max(np.mean(title_band, axis=1)) >= .55
    )
    dialogs = candidates(
        modal_blue, (.55, .85), (.22, .48), (.43, .62), (.20, .85), .28)
    panels = candidates(
        dark_panel, (.45, .75), (.08, .20), (.40, .60), (.28, .70), .55)
    buttons = candidates(
        yellow, (.18, .36), (.03, .09), (.56, .76), (.48, .82), .55)
    matches = []
    for button in buttons:
        bx0, by0, bx1, by1, area = button
        for dialog in dialogs:
            dx0, dy0, dx1, dy1, _ = dialog
            if not (dx0 < bx0 < bx1 < dx1 and dy0 < by0 < by1 < dy1):
                continue
            panel_ok = any(
                dx0 < px0 < px1 < dx1 and dy0 < py0 < py1 < by0
                for px0, py0, px1, py1, _ in panels
            )
            if title_evidence and panel_ok:
                matches.append((area, button))
    if not matches:
        return None
    _, (x0, y0, x1, y1, _) = max(matches)
    return (round((x0 + x1) / 2), round((y0 + y1) / 2))


def _diaboromon_lobby_evidence(image: Image.Image):
    """Return lobby masks and whether its title/body structure is present."""
    a = np.asarray(image)
    h, w = a.shape[:2]
    r, g, b = (a[:, :, index].astype(np.int16) for index in range(3))
    cyan = (g > 125) & (b > 135) & (g > r + 35) & (b > r + 45)
    blue = (b > 90) & (b > r + 35) & (b > g + 15)

    # Raw ADB frames are 900x1600 and include the game's upper HUD.  Cropped
    # screenshots shared in chat omit part of that area, so anchor this band
    # to the full framebuffer geometry observed from BlueStacks.
    full_frame_title = cyan[int(.155*h):int(.24*h), int(.14*w):int(.86*w)]
    cropped_title = cyan[int(.075*h):int(.17*h), int(.14*w):int(.86*w)]
    body = blue[int(.20*h):int(.72*h), int(.11*w):int(.89*w)]
    title_score = max(float(np.mean(full_frame_title)),
                      float(np.mean(cropped_title)))
    # A DigiWorld grid can put cyan scenery/lettering in this broad title band
    # while its blue cells strongly satisfy the body mask.  A real Defense Ops
    # title spans much more of the band, so require that complete structural
    # heading instead of accepting isolated HUD accents.
    visible = bool(body.size and title_score >= .20 and np.mean(body) >= .28)
    return a, cyan, blue, visible


def diaboromon_lobby_visible(image: Image.Image) -> bool:
    return _diaboromon_lobby_evidence(image)[3]


def diaboromon_find_party_button(image: Image.Image) -> tuple[int, int] | None:
    """Recognize an active Find a Party button in the Defense Ops lobby."""
    a, cyan, _, visible = _diaboromon_lobby_evidence(image)
    h, w = a.shape[:2]
    button = cyan[int(.72*h):int(.82*h), int(.30*w):int(.70*w)]
    # Once players join, this control becomes visibly dim. Do not keep tapping
    # it: the next valid action in that state is Attempt.
    if not visible or button.size == 0 or np.mean(button) < .08:
        return None

    ys, xs = np.nonzero(button)
    # The cyan lettering/border is centred on the control; using its median
    # also tolerates small aspect-ratio changes and Android display scaling.
    return (int(round(.30*w + np.median(xs))),
            int(round(.72*h + np.median(ys))))


def diaboromon_attempt_button(image: Image.Image) -> tuple[int, int] | None:
    """Return Attempt after both partner slots in the lobby are occupied."""
    a, cyan, _, visible = _diaboromon_lobby_evidence(image)
    if not visible:
        return None
    h, w = a.shape[:2]
    bright = np.max(a, axis=2) > 105
    left = bright[int(.39*h):int(.51*h), int(.15*w):int(.38*w)]
    right = bright[int(.39*h):int(.51*h), int(.62*w):int(.85*w)]
    if (not left.size or not right.size or
            np.mean(left) < .065 or np.mean(right) < .065):
        return None
    button = cyan[int(.53*h):int(.61*h), int(.49*w):int(.81*w)]
    if not button.size or np.mean(button) < .08:
        return None
    ys, xs = np.nonzero(button)
    return (int(round(.49*w + np.median(xs))),
            int(round(.53*h + np.median(ys))))


def _diaboromon_timer_bar(image: Image.Image) -> tuple[int, int, int, int] | None:
    """Locate the cyan battle timer used as the party-counter anchor."""
    a = np.asarray(image)
    h, w = a.shape[:2]
    upper = a[:int(.16*h)]
    r, g, b = (upper[:, :, index].astype(np.int16) for index in range(3))
    mask = (g > 145) & (b > 135) & (g > r + 35) & (b > r + 35)
    candidates = []
    for x0, y0, x1, y1, area in _components(mask):
        cw, ch = x1 - x0, y1 - y0
        if (int(.12*w) <= cw <= int(.38*w) and
                int(.006*h) <= ch <= int(.04*h) and
                .30*w <= (x0 + x1) / 2 <= .70*w and area >= .30*cw*ch):
            candidates.append((x0, y0, x1, y1, area))
    if not candidates:
        return None
    return max(candidates, key=lambda component: component[4])[:4]


def _normalized_component(mask: np.ndarray, component, size=(24, 32)) -> np.ndarray:
    x0, y0, x1, y1, _ = component
    glyph = Image.fromarray((mask[y0:y1, x0:x1] * 255).astype(np.uint8))
    return np.asarray(glyph.resize(size, Image.Resampling.NEAREST)) > 0


def diaboromon_party_count(image: Image.Image) -> int | None:
    """Return 5 only for a visually matched ``5/5`` battle counter.

    For every other readable ``n/5`` value this returns 0: the worker only
    needs to distinguish full from still-searching.  The numerator is compared
    with the denominator already rendered in the same font and frame, avoiding
    an OCR dependency and making the check fast enough for tight polling.
    """
    a = np.asarray(image)
    h, w = a.shape[:2]

    # The red encounter icon and counter occupy a stable HUD position in raw
    # 900x1600 ADB frames.  Unlike the cyan timer, they remain readable while
    # the VS/reward overlays dim the whole scene.
    r, g, b = (a[:, :, index].astype(np.int16) for index in range(3))
    red = (r > 65) & (r > g + 20) & (r > b + 10)
    icon = red[int(.07*h):int(.14*h), int(.27*w):int(.39*w)]
    if icon.size and np.mean(icon) >= .035:
        x0, x1 = int(.36*w), int(.47*w)
        y0, y1 = int(.08*h), int(.13*h)
        crop = a[y0:y1, x0:x1]
        cr, cg, cb = (crop[:, :, index].astype(np.int16)
                      for index in range(3))
        # Neutral light glyph fills survive both normal and dimmed frames.
        glyph_mask = ((cr > 65) & (cg > 65) & (cb > 65) &
                      (np.abs(cr - cg) < 38) & (np.abs(cg - cb) < 55))
        components = [component for component in _components(glyph_mask)
                      if int(.008*h) <= component[3] - component[1] <= int(.03*h)
                      and component[2] - component[0] <= int(.04*w)
                      and component[4] >= max(20, int(.00004*w*h))]

        def fixed_glyph(expected_ratio):
            expected_x = expected_ratio*w - x0
            usable = [component for component in components
                      if abs((component[0] + component[2]) / 2 - expected_x)
                      <= .018*w]
            return (min(usable, key=lambda component:
                        abs((component[0] + component[2]) / 2 - expected_x))
                    if usable else None)

        numerator = fixed_glyph(.391)
        denominator = fixed_glyph(.428)
        if (numerator is not None and denominator is not None and
                numerator != denominator):
            first = _normalized_component(glyph_mask, numerator)
            last = _normalized_component(glyph_mask, denominator)
            overlap = np.count_nonzero(first & last)
            similarity = (2.0 * overlap /
                          max(1, np.count_nonzero(first) + np.count_nonzero(last)))
            return 5 if similarity >= .90 else 0

    # Compatibility fallback for cropped captures without the full HUD.
    timer = _diaboromon_timer_bar(image)
    if timer is None:
        return None
    tx0, _, tx1, ty1 = timer
    h, w = image.height, image.width
    tw = tx1 - tx0
    x0 = max(0, round(tx0 + .08*tw))
    x1 = min(w, round(tx0 + .66*tw))
    y0 = max(0, round(ty1 + .007*h))
    y1 = min(h, round(ty1 + .050*h))
    crop = np.asarray(image)[y0:y1, x0:x1]
    if not crop.size:
        return None
    r, g, b = (crop[:, :, index].astype(np.int16) for index in range(3))
    dark = (r < 95) & (g < 110) & (b < 155)
    components = [component for component in _components(dark)
                  if component[3] - component[1] >= max(7, int(.010*h))
                  and component[4] >= max(12, int(.000025*w*h))]
    if not components:
        return None

    numerator_x = tx0 + .27*tw - x0
    denominator_x = tx0 + .49*tw - x0

    def closest(expected_x):
        usable = [component for component in components
                  if component[2] - component[0] <= .20*tw]
        if not usable:
            return None
        result = min(usable, key=lambda component:
                     abs((component[0] + component[2]) / 2 - expected_x))
        if abs((result[0] + result[2]) / 2 - expected_x) > .11*tw:
            return None
        return result

    numerator = closest(numerator_x)
    denominator = closest(denominator_x)
    if numerator is None or denominator is None or numerator == denominator:
        return None
    first = _normalized_component(dark, numerator)
    last = _normalized_component(dark, denominator)
    overlap = np.count_nonzero(first & last)
    similarity = 2.0 * overlap / max(1, np.count_nonzero(first) + np.count_nonzero(last))
    # Identical rendered fives normally score almost 1.0.  Keep the threshold
    # deliberately high because 2/5 and 3/5 share several strokes with 5/5.
    return 5 if similarity >= .90 else 0


def diaboromon_battle_banner_visible(image: Image.Image) -> bool:
    """Recognize the broad red/black Diaboromon VS encounter banner."""
    a = np.asarray(image)
    h, _ = a.shape[:2]
    band = a[int(.31*h):int(.58*h)]
    if not band.size:
        return False
    r, g, b = (band[:, :, index].astype(np.int16) for index in range(3))
    vivid_red = (r > 105) & (r > g + 45) & (r > b + 30)
    near_black = (r < 45) & (g < 45) & (b < 55)
    # Both colors must cover a meaningful part of the wide middle strip. This
    # excludes isolated red sprites and the blue Reward overlay.
    return bool(np.mean(vivid_red) >= .025 and np.mean(near_black) >= .09)


def diaboromon_give_up_button(image: Image.Image) -> tuple[int, int] | None:
    """Return the Give Up control only when its bottom confirmation card exists."""
    a = np.asarray(image)
    h, w = a.shape[:2]
    r, g, b = (a[:, :, index].astype(np.int16) for index in range(3))
    dark_card = (b < 85) & (r < 35) & (g < 75)
    bright_blue = (b > 130) & (g > 90) & (b > r + 55)
    card = dark_card[int(.77*h):int(.98*h), int(.05*w):int(.95*w)]
    button = bright_blue[int(.91*h):h, int(.30*w):int(.70*w)]
    if (card.size == 0 or button.size == 0 or
            np.mean(card) < .30 or np.mean(button) < .08):
        return None
    ys, xs = np.nonzero(button)
    return (int(round(.30*w + np.median(xs))),
            int(round(.91*h + np.median(ys))))


def classify(image: Image.Image) -> Detection:
    """Classify a frame, giving a reliable 5x5 grid priority over menu color.

    A cyan lower control is ambiguous because the live DigiWorld HUD can have
    the same signature.  Six reliable, equidistant grid edges plus the board
    aspect/coverage checks are stronger structural evidence and are therefore
    evaluated first.  The component fallback remains after menu rejection so
    a menu cannot pass merely because it contains several cyan regions.
    """
    w, h = image.size
    grid = _detect_grid(image)
    if grid is not None:
        return grid

    # Reject the known start/menu layout only after the stronger grid test.
    if _has_prominent_lower_cyan(image):
        return Detection("title_or_menu", 0.92, None,
                         "prominent lower-center cyan start/menu region; no board")

    small = image.resize((180, 320), Image.Resampling.BILINEAR)
    rgb = np.asarray(small)
    r, g, b = rgb[:,:,0], rgb[:,:,1], rgb[:,:,2]
    cyan = (b > 105) & (g > 85) & (b.astype(int) > r.astype(int) + 18) & (g.astype(int) > r.astype(int) + 8)
    components = []
    sx_scale, sy_scale = w / 180, h / 320
    for x0s, y0s, x1s, y1s, areas in _components(cyan):
        x, y = round(x0s*sx_scale), round(y0s*sy_scale)
        cw, ch = round((x1s-x0s)*sx_scale), round((y1s-y0s)*sy_scale)
        area = round(areas*sx_scale*sy_scale)
        if (0.025 * w <= cw <= 0.30 * w and 0.018 * h <= ch <= 0.18 * h
                and area >= 0.20 * cw * ch):
            components.append((int(x), int(y), int(cw), int(ch), int(area)))

    if len(components) >= 6:
        xs = [c[0] for c in components] + [c[0] + c[2] for c in components]
        ys = [c[1] for c in components] + [c[1] + c[3] for c in components]
        x0, x1 = max(0, min(xs)), min(w, max(xs))
        y0, y1 = max(0, min(ys)), min(h, max(ys))
        bw, bh = x1 - x0, y1 - y0
        aspect = bw / max(bh, 1)
        coverage = bw * bh / (w * h)
        confidence = min(0.98, 0.45 + 0.04 * len(components))
        if 0.55 <= aspect <= 2.2 and 0.12 <= coverage <= 0.80:
            return Detection("digiworld", confidence, (x0, y0, x1, y1),
                             f"{len(components)} cyan tile-like regions")

    return Detection("unknown", 0.0, None,
                     f"only {len(components)} tile-like cyan regions")


def cell_center(board: tuple[int, int, int, int], row: int, col: int):
    x0, y0, x1, y1 = board
    return (round(x0 + (col + 0.5) * (x1 - x0) / 5),
            round(y0 + (row + 0.5) * (y1 - y0) / 5))


def attack_button(image: Image.Image) -> tuple[int, int] | None:
    """Detect the yellow claw control in the lower-left HUD."""
    a = np.asarray(image)
    h, w = a.shape[:2]
    r, g, b = a[:,:,0], a[:,:,1], a[:,:,2]
    mask = (r > 150) & (g > 100) & (b < 130)
    mask[:int(.78*h), :] = False
    mask[:, :int(.10*w)] = False
    mask[:, int(.55*w):] = False
    candidates = [c for c in _components(mask) if c[4] >= 80]
    if not candidates:
        return None
    x0, y0, x1, y1, _ = max(candidates, key=lambda c: c[4])
    return ((x0 + x1)//2, (y0 + y1)//2)


def dash_button(image: Image.Image) -> tuple[int, int] | None:
    """Detect the large lime/cyan dash control in the lower HUD."""
    a = np.asarray(image)
    h, w = a.shape[:2]
    r, g, b = a[:,:,0], a[:,:,1], a[:,:,2]
    mask = (g > 175) & (r < 190) & (b < 190)
    mask[:int(.78*h), :] = False
    mask[:, :int(.45*w)] = False
    mask[:, int(.80*w):] = False
    candidates = [c for c in _components(mask) if c[4] >= 250]
    if not candidates:
        return None
    x0, y0, x1, y1, _ = max(candidates, key=lambda c: c[4])
    return ((x0 + x1)//2, (y0 + y1)//2)


def tutorial_overlay_center(image: Image.Image) -> tuple[int, int] | None:
    """Recognize the large centered blue first-use tutorial card."""
    a = np.asarray(image)
    h, w = a.shape[:2]
    center = a[int(.35*h):int(.70*h), int(.12*w):int(.88*w)]
    # Normal boards also contain a large connected blue area. Overlays add a
    # broad near-white title/body text line. Bright sprite fragments and item
    # icons occupy too few columns to qualify.
    white = np.all(center > 225, axis=2)
    if int(np.count_nonzero(np.sum(white, axis=0) >= 2)) < 200:
        return None
    r, g, b = a[:,:,0], a[:,:,1], a[:,:,2]
    mask = (b > 70) & (b.astype(int) > r.astype(int) + 20) & (g > 35)
    for x0, y0, x1, y1, area in _components(mask):
        if (area > .08*w*h and x0 < .30*w and x1 > .70*w and
                y0 < .55*h and y1 > .55*h):
            return ((x0+x1)//2, (y0+y1)//2)
    return None


def diagnostic(image: Image.Image, det: Detection) -> Image.Image:
    out = image.copy(); draw = ImageDraw.Draw(out)
    color = (40, 220, 40) if det.board else (255, 180, 0)
    if det.board:
        x0, y0, x1, y1 = det.board
        for i in range(6):
            x = round(x0 + i * (x1 - x0) / 5)
            y = round(y0 + i * (y1 - y0) / 5)
            draw.line((x, y0, x, y1), fill=color, width=2)
            draw.line((x0, y, x1, y), fill=color, width=2)
        draw.rectangle((x0, y0, x1, y1), outline=color, width=3)
    label = f"STATE={det.state} CONF={det.confidence:.2f}"
    draw.rectangle((8, 8, min(out.width-8, 570), 78), fill=(0, 0, 0))
    font = ImageFont.load_default(size=20)
    small_font = ImageFont.load_default(size=13)
    draw.text((18, 17), label, fill=color, font=font)
    draw.text((18, 50), det.reason[:70], fill=(255,255,255), font=small_font)
    return out


def log_event(path: Path, event: dict):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(event, ensure_ascii=False) + "\n")


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--adb", default=ADB_DEFAULT)
    p.add_argument("--serial", default=SERIAL_DEFAULT)
    p.add_argument("--out", type=Path, default=Path("outputs"))
    p.add_argument("--allow-input", action="store_true")
    p.add_argument("--tap-cell", nargs=2, type=int, metavar=("ROW", "COL"))
    p.add_argument("--attack-cell", nargs=2, type=int, metavar=("ROW", "COL"))
    p.add_argument("--dash", action="store_true")
    p.add_argument("--dismiss-tutorial", action="store_true")
    p.add_argument("--min-confidence", type=float, default=0.85)
    p.add_argument("--animation-wait", type=float, default=2.0)
    args = p.parse_args()

    try:
        args.requested_serial = requested_serial_value(args.serial)
        args.adb = resolve_adb(args.adb)
        args.serial = resolve_serial(args.adb, args.requested_serial)
    except (OSError, RuntimeError, subprocess.SubprocessError) as exc:
        print(f"FEHLER: {exc}", file=sys.stderr)
        return 10
    print(json.dumps({"adb": args.adb, "serial": args.serial,
                      "mode": "input" if args.allow_input else "observe"},
                     ensure_ascii=False))

    args.out.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    image = screenshot(args.adb, args.serial)
    det = classify(image)
    raw_path = args.out / f"digiworld_raw_{stamp}.png"
    diag_path = args.out / f"digiworld_diagnostic_{stamp}.png"
    image.save(raw_path)
    diagnostic(image, det).save(diag_path)
    event = {"time_utc": stamp, "mode": "input" if args.allow_input else "observe",
             "detection": asdict(det), "raw": str(raw_path), "diagnostic": str(diag_path)}
    input_unconfirmed = False

    def dispatch_control(x, y, action):
        result = dispatch_tap_with_recovery(
            args.adb, args.serial, x, y, action=action,
            requested_serial=args.requested_serial)
        args.serial = result.serial
        if result.recovery_events:
            event["adb_recovery"] = list(result.recovery_events)
        return result

    requested_cell = args.attack_cell or args.tap_cell
    if args.dismiss_tutorial:
        control = tutorial_overlay_center(image)
        if not args.allow_input:
            event["action"] = "STOP: tutorial dismissal requested but input is disabled"
        elif control is None:
            event["action"] = "STOP: tutorial overlay was not detected"
        else:
            dispatch = dispatch_control(*control, "dismiss_tutorial")
            if dispatch.outcome != "sent":
                input_unconfirmed = True
                event["action"] = "STOP: ADB input unconfirmed; fresh frame captured"
            else:
                event["action"] = {"status": "sent", "type": "dismiss_tutorial",
                                   "control_xy": list(control)}
                time.sleep(1.5)
                post = screenshot(args.adb, args.serial)
                post_det = classify(post)
                post_raw = args.out / f"digiworld_post_raw_{stamp}.png"
                post_diag = args.out / f"digiworld_post_diagnostic_{stamp}.png"
                post.save(post_raw); diagnostic(post, post_det).save(post_diag)
                event["post_action"] = {"detection": asdict(post_det),
                                        "raw": str(post_raw), "diagnostic": str(post_diag)}
    elif args.dash:
        if not args.allow_input:
            event["action"] = "STOP: dash requested but input is disabled"
        elif det.state != "digiworld" or det.board is None or det.confidence < args.min_confidence:
            event["action"] = "STOP: board recognition is not reliable"
        else:
            control = dash_button(image)
            if control is None:
                event["action"] = "STOP: large dash control was not detected"
            else:
                dispatch = dispatch_control(*control, "dash")
                if dispatch.outcome == "sent":
                    event["action"] = {"status": "sent", "type": "dash",
                                       "control_xy": list(control)}
                    time.sleep(3.0)
                    post = screenshot(args.adb, args.serial)
                    post_det = classify(post)
                    post_raw = args.out / f"digiworld_post_raw_{stamp}.png"
                    post_diag = args.out / f"digiworld_post_diagnostic_{stamp}.png"
                    post.save(post_raw)
                    diagnostic(post, post_det).save(post_diag)
                    event["post_action"] = {"detection": asdict(post_det),
                                            "raw": str(post_raw),
                                            "diagnostic": str(post_diag)}
                else:
                    input_unconfirmed = True
                    event["action"] = "STOP: ADB dash unconfirmed; fresh frame captured"
    elif args.attack_cell and args.tap_cell:
        event["action"] = "STOP: request only one action at a time"
    elif requested_cell:
        row, col = requested_cell
        if not args.allow_input:
            event["action"] = "STOP: tap requested but input is disabled"
        elif det.state != "digiworld" or det.board is None or det.confidence < args.min_confidence:
            event["action"] = "STOP: board recognition is not reliable"
        elif not (0 <= row < 5 and 0 <= col < 5):
            event["action"] = "STOP: cell must be within row/col 0..4"
        else:
            x, y = cell_center(det.board, row, col)
            control = None
            dispatch = dispatch_control(
                x, y, "attack" if args.attack_cell else "move")
            if dispatch.outcome == "sent":
                event["action"] = {"status": "sent",
                                   "type": "attack" if args.attack_cell else "move",
                                   "target_cell": [row, col], "adb_xy": [x, y]}
                if control:
                    event["action"]["control_xy"] = list(control)
                time.sleep(max(.5, args.animation_wait))
                post = screenshot(args.adb, args.serial)
                post_det = classify(post)
                post_raw = args.out / f"digiworld_post_raw_{stamp}.png"
                post_diag = args.out / f"digiworld_post_diagnostic_{stamp}.png"
                post.save(post_raw)
                diagnostic(post, post_det).save(post_diag)
                event["post_action"] = {
                    "detection": asdict(post_det),
                    "raw": str(post_raw),
                    "diagnostic": str(post_diag),
                }
            else:
                input_unconfirmed = True
                event["action"] = "STOP: ADB tap unconfirmed; fresh frame captured"
    else:
        event["action"] = "none"

    log_event(args.out / "digiworld_steps.jsonl", event)
    print(json.dumps(event, ensure_ascii=False, indent=2))
    if input_unconfirmed:
        return 11
    return 0 if det.state == "digiworld" else 2


if __name__ == "__main__":
    sys.exit(main())
