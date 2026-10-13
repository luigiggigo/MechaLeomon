import inspect
import json
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import app
import auto_digiworld_batch2 as runner
from control import ControlFiles, parse_status_line


class ControlFileTests(unittest.TestCase):
    def test_pause_resume_and_stop_are_explicit(self):
        with tempfile.TemporaryDirectory() as temp:
            files = ControlFiles(Path(temp))
            files.reset()
            files.pause()
            self.assertTrue(files.pause_file.exists())
            files.resume()
            self.assertFalse(files.pause_file.exists())
            files.stop()
            self.assertTrue(files.stop_file.exists())
            self.assertFalse(files.pause_file.exists())

    def test_controlled_wait_stops_without_waiting(self):
        with tempfile.TemporaryDirectory() as temp:
            files = ControlFiles(Path(temp))
            files.stop()
            self.assertFalse(runner.controlled_wait(files.pause_file, files.stop_file, 5))

    def test_controlled_wait_blocks_until_resume(self):
        with tempfile.TemporaryDirectory() as temp:
            files = ControlFiles(Path(temp))
            files.pause()
            result = []
            worker = threading.Thread(
                target=lambda: result.append(runner.controlled_wait(
                    files.pause_file, files.stop_file, 0)), daemon=True)
            worker.start()
            time.sleep(0.12)
            self.assertTrue(worker.is_alive())
            files.resume()
            worker.join(1)
            self.assertFalse(worker.is_alive())
            self.assertEqual(result, [True])


class StatusTests(unittest.TestCase):
    def test_visual_recognition_retries_do_not_terminate_worker(self):
        source = inspect.getsource(runner.main)
        self.assertNotIn("if unreliable >=", source)
        self.assertNotIn("if player_unreliable >=", source)

    def test_status_parser_rejects_invalid_lines(self):
        self.assertIsNone(parse_status_line("not-json"))
        self.assertIsNone(parse_status_line("[]"))
        self.assertEqual(parse_status_line('{"state":"paused"}')["state"], "paused")

    def test_emit_status_writes_gui_status_file(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "status.jsonl"
            with patch.dict("os.environ", {"DIGIWORLD_STATUS_FILE": str(path)}):
                runner.emit_status("running", "ok", 3, 10)
            payload = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(payload["done"], 3)


class LauncherTests(unittest.TestCase):
    def test_visible_brand_and_runtime_location_are_mechaleomon(self):
        self.assertEqual(app.APP_NAME, "MechaLeomon")
        with patch.dict("os.environ", {"LOCALAPPDATA": "C:\\Users\\test\\AppData\\Local"}):
            self.assertEqual(app.data_root().name, "MechaLeomon")

    def test_visible_app_version_comes_from_bundled_version_file(self):
        self.assertEqual(app.APP_VERSION,
                         Path(app.__file__).with_name("VERSION").read_text(
                             encoding="utf-8").strip())

    def test_worker_boundary_converts_called_process_error_to_status(self):
        failure = subprocess.CalledProcessError(1, ["adb", "shell", "input"])
        with (patch.object(sys, "argv", ["app.py", "--worker"]),
              patch.object(runner, "main", side_effect=failure),
              patch.object(runner, "emit_status") as emit):
            exit_code = app.main()
        self.assertEqual(exit_code, runner.ADB_RECOVERY_EXIT)
        emit.assert_called_once_with(
            "error", runner.ADB_RECOVERY_MESSAGE)

    def test_source_worker_command_contains_safety_controls(self):
        with tempfile.TemporaryDirectory() as temp:
            files = ControlFiles(Path(temp) / "control")
            command = app.worker_command(files, Path(temp) / "runs")
            self.assertIn("--worker", command)
            self.assertIn("--pause-file", command)
            self.assertIn("--stop-file", command)
            self.assertIn("--min-confidence", command)
            interval = float(command[command.index("--interval") + 1])
            self.assertEqual(interval, .70)
            batch_size = int(command[command.index("--batch-size") + 1])
            self.assertEqual(batch_size, 1)
            self.assertNotIn("--auto-pull-10x", command)
            self.assertNotIn("--auto-pull-35x", command)

            pull_10x_command = app.worker_command(
                files, Path(temp) / "runs", "auto_pull_10x")
            self.assertIn("--auto-pull-10x", pull_10x_command)
            self.assertNotIn("--auto-pull-35x", pull_10x_command)

            pull_35x_command = app.worker_command(
                files, Path(temp) / "runs", "auto_pull_35x")
            self.assertIn("--auto-pull-35x", pull_35x_command)
            self.assertNotIn("--auto-pull-10x", pull_35x_command)

    def test_restart_requests_safe_stop_then_forces_a_wedged_worker(self):
        window = object.__new__(app.ExplorerWindow)
        window.closing = False
        window.restart_requested = False
        window.process = Mock()
        window.process.poll.return_value = None
        window.controls = Mock()
        window.root = Mock()
        window.set_state = Mock()

        window.restart()

        self.assertTrue(window.restart_requested)
        window.controls.stop.assert_called_once_with()
        window.set_state.assert_called_once_with(
            "stopping", "Riavvio dell’automazione in corso…"
        )
        self.assertEqual(window.root.after.call_args.args[0], 3000)
        window._force_restart()
        window.process.terminate.assert_called_once_with()

    def test_restart_starts_immediately_when_worker_is_already_stopped(self):
        window = object.__new__(app.ExplorerWindow)
        window.closing = False
        window.restart_requested = False
        window.process = None
        window.start = Mock()

        window.restart()

        window.start.assert_called_once_with()

    def test_global_stop_cancels_pvp_scan_and_worker(self):
        window = object.__new__(app.ExplorerWindow)
        window.pvp_window = Mock()
        window.process = Mock()
        window.process.poll.return_value = None
        window.controls = Mock()
        window.set_state = Mock()

        window.stop()

        window.pvp_window.cancel_scan.assert_called_once_with()
        window.controls.stop.assert_called_once_with()
        window.set_state.assert_called_once_with(
            "stopping", "Arresto sicuro in corso…")

    def test_switch_mode_requests_safe_worker_replacement(self):
        window = object.__new__(app.ExplorerWindow)
        window.closing = False
        window.restart_requested = False
        window.mode = "digiworld"
        window.process = Mock()
        window.process.poll.return_value = None
        window.controls = Mock()
        window.root = Mock()
        window.set_state = Mock()

        window.switch_mode("auto_pull_10x")

        self.assertEqual(window.mode, "auto_pull_10x")
        self.assertTrue(window.restart_requested)
        window.controls.stop.assert_called_once_with()
        window.set_state.assert_called_once_with(
            "stopping", "Passaggio a Auto Pull 10x in corso…")
        self.assertEqual(window.root.after.call_args.args[0], 3000)

    def test_selecting_active_pull_button_returns_to_digiworld(self):
        window = object.__new__(app.ExplorerWindow)
        window.closing = False
        window.restart_requested = False
        window.mode = "auto_pull_35x"
        window.process = None
        window.start = Mock()

        window.switch_mode("auto_pull_35x")

        self.assertEqual(window.mode, "digiworld")
        window.start.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
