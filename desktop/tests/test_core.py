import json
import random
import subprocess
import sys
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import auto_digiworld as strategy
import auto_digiworld_batch2 as runner
import auto_pull
import digiworld_bot as bot
import numpy as np
from PIL import Image, ImageDraw


def open_test_image(path):
    """Le catture reali non sono distribuite: possono contenere dati dei giocatori."""
    path = Path(path)
    if path.parent == Path(__file__).parent / "fixtures" and not path.is_file():
        raise unittest.SkipTest("Fixture visiva privata non distribuita")
    return Image.open(path)


def empty_grid():
    return {
        (row, col): {
            "player": 0.0,
            "orange": 0.0,
            "pink": 0.0,
            "green": 0.0,
            "item": 0.0,
            "pyramid": 0.0,
            "highlight": 0.0,
        }
        for row in range(5)
        for col in range(5)
    }


def place_item(info, cell, category):
    info[cell].update(item=.10, **{category: .10})


def synthetic_diaboromon_battle(full):
    image = Image.new("RGB", (500, 900), "white")
    draw = ImageDraw.Draw(image)
    draw.rectangle((200, 55, 300, 68), fill=(20, 220, 220))

    def five(x, y):
        color = (8, 25, 70)
        draw.rectangle((x, y, x + 10, y + 3), fill=color)
        draw.rectangle((x, y, x + 3, y + 12), fill=color)
        draw.rectangle((x, y + 10, x + 10, y + 13), fill=color)
        draw.rectangle((x + 7, y + 10, x + 10, y + 23), fill=color)
        draw.rectangle((x, y + 20, x + 10, y + 23), fill=color)

    if full:
        five(222, 80)
    else:
        draw.rectangle((225, 80, 229, 103), fill=(8, 25, 70))
        draw.rectangle((222, 100, 229, 103), fill=(8, 25, 70))
    five(244, 80)
    draw.rectangle((25, 700, 475, 899), fill=(2, 25, 57))
    draw.rectangle((190, 840, 310, 882), fill=(5, 145, 215))
    return image


class CoordinateTests(unittest.TestCase):
    def test_cell_center_is_relative(self):
        board = (100, 200, 600, 700)
        self.assertEqual(bot.cell_center(board, 0, 0), (150, 250))
        self.assertEqual(bot.cell_center(board, 4, 4), (550, 650))


class AutoPullDetectorTests(unittest.TestCase):
    @staticmethod
    def summon_screen(active=True, aligned=True):
        image = Image.new("RGB", (500, 900), "white")
        draw = ImageDraw.Draw(image)
        draw.rounded_rectangle((110, 770, 246, 814), radius=9,
                               fill=(0, 155, 224))
        y = 770 if aligned else 650
        yellow = (255, 190, 28) if active else (145, 135, 105)
        draw.rounded_rectangle((260, y, 397, y + 44), radius=9, fill=yellow)
        return image

    def test_aligned_active_summon_pair_returns_yellow_button_center(self):
        self.assertEqual(bot.summon_10x_button(self.summon_screen()),
                         (329, 792))

    def test_yellow_control_requires_aligned_blue_summon_sibling(self):
        self.assertIsNone(bot.summon_10x_button(
            self.summon_screen(aligned=False)))

    def test_dimmed_ten_pull_control_is_not_actionable(self):
        self.assertIsNone(bot.summon_10x_button(
            self.summon_screen(active=False)))

    def test_only_bottom_shelf_summon_control_is_repeatable_35x(self):
        image = Image.new("RGB", (500, 900), "white")
        draw = ImageDraw.Draw(image)
        draw.rounded_rectangle((300, 780, 330, 810), radius=4,
                               fill=(30, 205, 70))
        self.assertFalse(bot.summon_button_is_repeatable_35x(
            image, (329, 792)))
        self.assertTrue(bot.summon_button_is_repeatable_35x(
            image, (329, 840)))

    def test_small_frame_never_authorizes_repeatable_control(self):
        image = Image.new("RGB", (50, 50), "white")
        self.assertFalse(bot.summon_button_is_repeatable_35x(
            image, (25, 49)))

    def test_full_frame_crest_reward_button_is_not_repeatable_35x(self):
        image = open_test_image(
            Path(__file__).parent / "fixtures" / "crest_10x_reward_live.png"
        )
        button = bot.summon_10x_button(image)
        self.assertEqual(button, (574, 1535))
        self.assertFalse(bot.summon_button_is_repeatable_35x(image, button))

    def test_full_frame_support_reward_button_is_repeatable_35x(self):
        image = open_test_image(
            Path(__file__).parent / "fixtures" / "support_35x_reward_live.png"
        )
        button = bot.summon_10x_button(image)
        self.assertEqual(button, (574, 1535))
        self.assertTrue(bot.summon_button_is_repeatable_35x(image, button))

    @staticmethod
    def confirmation_modal(title=True, panel=True):
        image = Image.new("RGB", (500, 900), (190, 205, 220))
        draw = ImageDraw.Draw(image)
        draw.rounded_rectangle((70, 305, 425, 620), radius=18,
                               fill=(10, 82, 145), outline=(30, 195, 235),
                               width=4)
        if title:
            draw.rounded_rectangle((120, 285, 380, 330), radius=14,
                                   fill=(30, 195, 235))
        if panel:
            draw.rounded_rectangle((90, 382, 408, 492), radius=15,
                                   fill=(12, 48, 92))
        draw.rounded_rectangle((185, 562, 310, 605), radius=9,
                               fill=(255, 190, 28))
        return image

    def test_complete_confirmation_modal_returns_centered_yellow_button(self):
        self.assertEqual(bot.summon_10x_confirmation_button(
            self.confirmation_modal()), (248, 584))

    def test_confirmation_requires_title_and_explanatory_panel(self):
        self.assertIsNone(bot.summon_10x_confirmation_button(
            self.confirmation_modal(title=False)))
        self.assertIsNone(bot.summon_10x_confirmation_button(
            self.confirmation_modal(panel=False)))

    def test_main_summon_screen_is_not_a_confirmation_modal(self):
        self.assertIsNone(bot.summon_10x_confirmation_button(
            self.summon_screen()))

class AutoPullWorkerTests(unittest.TestCase):
    @staticmethod
    def args(steps=1):
        return SimpleNamespace(
            steps=steps, adb="adb", serial="emulator-5554",
            requested_serial="auto", pause_file=None, stop_file=None,
        )

    def test_only_confirmed_tap_counts(self):
        args = self.args()
        dispatch = bot.TapDispatchResult("sent", "emulator-5554")
        with tempfile.TemporaryDirectory() as temp, \
                patch.object(bot, "screenshot", return_value=Image.new("RGB", (500, 900))), \
                patch.object(bot, "summon_10x_button", return_value=(328, 792)), \
                patch.object(bot, "log_event"), \
                patch.object(auto_pull, "AUTO_PULL_ACTION_SETTLE_SECONDS", 0):
            emit = Mock()
            send = Mock(return_value=dispatch)
            result = auto_pull.run(
                args, Path(temp) / "events.jsonl", pull_kind="10x",
                emit_status=emit,
                controlled_wait=lambda *_args: True,
                dispatch_worker_tap=send,
                finish_failed_adb_recovery=Mock(),
            )
        self.assertEqual(result, 0)
        send.assert_called_once_with(
            args, Path(temp) / "events.jsonl", 328, 792,
            "auto_pull_10x_summon", 0,
        )
        self.assertEqual(emit.call_args.args[:4],
                         ("complete", "Auto Pull 10x completato", 1, 1))

    def test_same_visible_button_is_not_repeated_until_it_disappears(self):
        args = self.args(steps=2)
        sent = bot.TapDispatchResult("sent", "emulator-5554")
        detections = iter(((328, 792), (328, 792), None, (328, 792)))
        with tempfile.TemporaryDirectory() as temp, \
                patch.object(bot, "screenshot", return_value=Image.new("RGB", (500, 900))), \
                patch.object(bot, "summon_10x_button",
                             side_effect=lambda _image: next(detections)), \
                patch.object(bot, "log_event"), \
                patch.object(auto_pull, "AUTO_PULL_ACTION_SETTLE_SECONDS", 0), \
                patch.object(auto_pull, "AUTO_PULL_POLL_SECONDS", 0):
            send = Mock(return_value=sent)
            result = auto_pull.run(
                args, Path(temp) / "events.jsonl", pull_kind="10x",
                emit_status=Mock(),
                controlled_wait=lambda *_args: True,
                dispatch_worker_tap=send,
                finish_failed_adb_recovery=Mock(),
            )
        self.assertEqual(result, 0)
        self.assertEqual(send.call_count, 2)

    def test_bottom_shelf_35x_is_repeated_with_human_delay(self):
        self.assertEqual(auto_pull.AUTO_PULL_35X_DELAY_RANGE, (.25, .45))
        args = self.args(steps=2)
        sent = bot.TapDispatchResult("sent", "emulator-5554")
        waits = []

        def controlled_wait(*call_args):
            if len(call_args) == 3:
                waits.append(call_args[2])
            return True

        with tempfile.TemporaryDirectory() as temp, \
                patch.object(bot, "screenshot", return_value=Image.new("RGB", (500, 900))), \
                patch.object(bot, "summon_10x_button", return_value=(329, 840)), \
                patch.object(bot, "summon_button_is_repeatable_35x",
                             side_effect=AssertionError(
                                 "the selected mode must replace classification")) as classifier, \
                patch.object(bot, "summon_10x_confirmation_button",
                             side_effect=AssertionError(
                                 "35x mode must not inspect the 10x modal")) as confirmation, \
                patch.object(bot, "log_event"), \
                patch.object(auto_pull.random, "uniform", return_value=.35):
            send = Mock(return_value=sent)
            result = auto_pull.run(
                args, Path(temp) / "events.jsonl", pull_kind="35x",
                emit_status=Mock(),
                controlled_wait=controlled_wait,
                dispatch_worker_tap=send,
                finish_failed_adb_recovery=Mock(),
            )
        self.assertEqual(result, 0)
        self.assertEqual(send.call_count, 2)
        send.assert_called_with(
            args, Path(temp) / "events.jsonl", 329, 840,
            "auto_pull_35x_repeat", .35,
        )
        self.assertEqual(waits, [.35, .35])
        classifier.assert_not_called()
        confirmation.assert_not_called()

    def test_35x_keeps_tapping_latched_point_while_button_is_hidden(self):
        args = self.args(steps=3)
        sent = bot.TapDispatchResult("sent", "emulator-5554")
        logged = []
        with tempfile.TemporaryDirectory() as temp, \
                patch.object(bot, "screenshot",
                             return_value=Image.new("RGB", (500, 900))) as screenshot, \
                patch.object(bot, "summon_10x_button",
                             return_value=(329, 840)) as detector, \
                patch.object(bot, "summon_button_is_repeatable_35x",
                             side_effect=AssertionError(
                                 "the selected mode must replace classification")), \
                patch.object(bot, "log_event",
                             side_effect=lambda _log, event: logged.append(event)), \
                patch.object(auto_pull.random, "uniform", return_value=.3):
            send = Mock(return_value=sent)
            result = auto_pull.run(
                args, Path(temp) / "events.jsonl", pull_kind="35x",
                emit_status=Mock(),
                controlled_wait=lambda *_args: True,
                dispatch_worker_tap=send,
                finish_failed_adb_recovery=Mock(),
            )
        self.assertEqual(result, 0)
        self.assertEqual(send.call_count, 3)
        screenshot.assert_called_once_with("adb", "emulator-5554")
        self.assertEqual(detector.call_count, 1)
        self.assertEqual(
            [(call.args[2], call.args[3]) for call in send.call_args_list],
            [(329, 840), (329, 840), (329, 840)],
        )
        self.assertEqual(
            [event["visual_anchor"] for event in logged],
            ["current_frame", "latched_35x", "latched_35x"],
        )

    def test_ambiguous_dispatch_rescans_without_counting_the_action(self):
        args = self.args()
        rescan = bot.TapDispatchResult("rescan", "emulator-5554")
        sent = bot.TapDispatchResult("sent", "emulator-5554")
        with tempfile.TemporaryDirectory() as temp, \
                patch.object(bot, "screenshot", return_value=Image.new("RGB", (500, 900))), \
                patch.object(bot, "summon_10x_button", return_value=(328, 792)), \
                patch.object(bot, "log_event"), \
                patch.object(auto_pull, "AUTO_PULL_ACTION_SETTLE_SECONDS", 0):
            emit = Mock()
            send = Mock(side_effect=(rescan, sent))
            result = auto_pull.run(
                args, Path(temp) / "events.jsonl", pull_kind="10x",
                emit_status=emit,
                controlled_wait=lambda *_args: True,
                dispatch_worker_tap=send,
                finish_failed_adb_recovery=Mock(),
            )
        self.assertEqual(result, 0)
        self.assertEqual(send.call_count, 2)
        self.assertEqual(emit.call_args.args[:4],
                         ("complete", "Auto Pull 10x completato", 1, 1))

    def test_confirmation_can_follow_main_button_without_empty_frame(self):
        args = self.args(steps=2)
        sent = bot.TapDispatchResult("sent", "emulator-5554")
        confirmations = iter((None, (248, 584)))
        with tempfile.TemporaryDirectory() as temp, \
                patch.object(bot, "screenshot", return_value=Image.new("RGB", (500, 900))), \
                patch.object(bot, "summon_10x_confirmation_button",
                             side_effect=lambda _image: next(confirmations)), \
                patch.object(bot, "summon_10x_button", return_value=(328, 792)), \
                patch.object(bot, "log_event"), \
                patch.object(auto_pull, "AUTO_PULL_ACTION_SETTLE_SECONDS", 0):
            send = Mock(return_value=sent)
            result = auto_pull.run(
                args, Path(temp) / "events.jsonl", pull_kind="10x",
                emit_status=Mock(),
                controlled_wait=lambda *_args: True,
                dispatch_worker_tap=send,
                finish_failed_adb_recovery=Mock(),
            )
        self.assertEqual(result, 0)
        self.assertEqual(send.call_args_list[0].args[4],
                         "auto_pull_10x_summon")
        self.assertEqual(send.call_args_list[1].args[4],
                         "auto_pull_10x_confirm")

    def test_35x_rescan_discards_latch_and_requires_a_new_frame(self):
        args = self.args(steps=1)
        rescan = bot.TapDispatchResult("rescan", "emulator-5554")
        sent = bot.TapDispatchResult("sent", "emulator-5554")
        with tempfile.TemporaryDirectory() as temp, \
                patch.object(bot, "screenshot",
                             return_value=Image.new("RGB", (500, 900))) as screenshot, \
                patch.object(bot, "summon_10x_button",
                             return_value=(329, 840)), \
                patch.object(bot, "log_event"), \
                patch.object(auto_pull.random, "uniform", return_value=.3):
            send = Mock(side_effect=(rescan, sent))
            result = auto_pull.run(
                args, Path(temp) / "events.jsonl", pull_kind="35x",
                emit_status=Mock(), controlled_wait=lambda *_args: True,
                dispatch_worker_tap=send,
                finish_failed_adb_recovery=Mock(),
            )
        self.assertEqual(result, 0)
        self.assertEqual(send.call_count, 2)
        self.assertEqual(screenshot.call_count, 2)

    def test_unknown_pull_kind_is_rejected_before_any_input(self):
        with self.assertRaisesRegex(ValueError, "unsupported Auto Pull kind"):
            auto_pull.run(
                self.args(), Path("unused.jsonl"), pull_kind="auto",
                emit_status=Mock(), controlled_wait=Mock(),
                dispatch_worker_tap=Mock(),
                finish_failed_adb_recovery=Mock(),
            )


class DiaboromonTests(unittest.TestCase):
    def test_find_party_requires_complete_lobby_signature(self):
        image = Image.new("RGB", (500, 825), (5, 15, 30))
        draw = ImageDraw.Draw(image)
        draw.rectangle((70, 65, 430, 135), fill=(30, 205, 225))
        draw.rectangle((55, 130, 445, 595), fill=(20, 70, 145))
        draw.rectangle((175, 610, 325, 660), fill=(5, 160, 220))
        self.assertIsNotNone(bot.diaboromon_find_party_button(image))

        draw.rectangle((175, 610, 325, 660), fill=(20, 70, 145))
        self.assertIsNone(bot.diaboromon_find_party_button(image))

    def test_party_counter_matches_numerator_against_denominator_five(self):
        waiting = synthetic_diaboromon_battle(full=False)
        full = synthetic_diaboromon_battle(full=True)
        self.assertEqual(bot.diaboromon_party_count(waiting), 0)
        self.assertEqual(bot.diaboromon_party_count(full), 5)
        self.assertIsNotNone(bot.diaboromon_give_up_button(full))

    def test_normal_frame_has_no_diaboromon_actions(self):
        image = Image.new("RGB", (500, 900), "white")
        self.assertIsNone(bot.diaboromon_find_party_button(image))
        self.assertIsNone(bot.diaboromon_party_count(image))
        self.assertIsNone(bot.diaboromon_give_up_button(image))

    def test_real_digiworld_grid_is_not_a_diaboromon_lobby(self):
        fixture = (Path(__file__).parent / "fixtures" /
                   "digiworld_diaboromon_lobby_false_positive.png")
        image = open_test_image(fixture).convert("RGB")
        self.assertFalse(bot.diaboromon_lobby_visible(image))
        self.assertIsNone(bot.diaboromon_find_party_button(image))
        self.assertIsNone(bot.diaboromon_attempt_button(image))
        self.assertIsNone(bot.diaboromon_party_count(image))
        self.assertFalse(bot.diaboromon_battle_banner_visible(image))
        self.assertIsNone(bot.diaboromon_give_up_button(image))
        self.assertEqual(bot.classify(image).state, "digiworld")

    def test_party_poll_is_intentionally_faster_than_normal_worker_interval(self):
        self.assertLess(runner.DIABOROMON_POLL_SECONDS,
                        runner.DEFAULT_INTERVAL / 5)

    def test_real_full_frame_lobby_is_recognized(self):
        fixture = Path(__file__).parent / "fixtures" / "diaboromon_live.png"
        image = open_test_image(fixture).convert("RGB")
        button = bot.diaboromon_find_party_button(image)
        self.assertIsNotNone(button)
        self.assertAlmostEqual(button[0] / image.width, .50, delta=.04)
        self.assertAlmostEqual(button[1] / image.height, .79, delta=.04)
        self.assertIsNone(bot.diaboromon_attempt_button(image))

    def test_real_lobby_with_partners_uses_attempt_not_dim_find_party(self):
        fixture = (Path(__file__).parent / "fixtures" /
                   "diaboromon_party_lobby_live.png")
        image = open_test_image(fixture).convert("RGB")
        self.assertTrue(bot.diaboromon_lobby_visible(image))
        self.assertIsNone(bot.diaboromon_find_party_button(image))
        attempt = bot.diaboromon_attempt_button(image)
        self.assertIsNotNone(attempt)
        self.assertAlmostEqual(attempt[0] / image.width, .65, delta=.04)
        self.assertAlmostEqual(attempt[1] / image.height, .57, delta=.04)

    def test_real_dimmed_battle_still_reads_five_of_five(self):
        fixture = Path(__file__).parent / "fixtures" / "diaboromon_battle_live.png"
        image = open_test_image(fixture).convert("RGB")
        self.assertEqual(bot.diaboromon_party_count(image), 5)
        self.assertFalse(bot.diaboromon_battle_banner_visible(image))

    def test_red_black_middle_banner_is_a_battle_trigger(self):
        image = synthetic_diaboromon_battle(full=False)
        draw = ImageDraw.Draw(image)
        draw.rectangle((0, 300, 499, 500), fill=(15, 15, 20))
        draw.polygon(((0, 330), (500, 300), (500, 430), (0, 500)),
                     fill=(180, 20, 25))
        draw.rectangle((0, 430, 499, 500), fill=(10, 10, 15))
        self.assertTrue(bot.diaboromon_battle_banner_visible(image))


class GridSearchTests(unittest.TestCase):
    @staticmethod
    def reference_best_six_lines(score, start_range, step_range):
        best = None
        for step in step_range:
            for start in start_range:
                if start + 5 * step >= len(score):
                    continue
                positions, values = [], []
                for index in range(6):
                    expected = start + index * step
                    lo = max(0, expected - 2)
                    hi = min(len(score), expected + 3)
                    offset = int(np.argmax(score[lo:hi]))
                    positions.append(lo + offset)
                    values.append(float(score[lo + offset]))
                quality = sorted(values)[1] + .10 * float(np.mean(values))
                if best is None or quality > best[0]:
                    best = quality, positions, values
        return best

    def test_vectorized_grid_search_matches_reference(self):
        rng = np.random.default_rng(90210)
        for length, starts, steps in (
                (80, range(2, 18), range(5, 13)),
                (137, range(8, 31), range(9, 24)),
                (53, range(1, 12), range(6, 15))):
            score = rng.random(length) * 60
            expected = self.reference_best_six_lines(score, starts, steps)
            actual = bot._best_six_lines(score, starts, steps)
            self.assertAlmostEqual(actual[0], expected[0], places=12)
            self.assertEqual(actual[1], expected[1])
            np.testing.assert_allclose(actual[2], expected[2])


class ScreenshotRegressionTests(unittest.TestCase):
    def test_stopped_orange_route_remains_available_with_attacks(self):
        with open_test_image(Path(__file__).with_name("fixtures") /
                        "digiworld_stopped_unreachable_orange.png") as image:
            detection = bot.classify(image)
            self.assertEqual(detection.state, "digiworld")
            info = strategy.cells(image, detection.board)
        self.assertEqual(strategy.player_cell(info)[0], (4, 1))
        self.assertFalse(strategy.is_obstacle(info[(4, 2)]))
        action, reason = strategy.choose(info, attacks_enabled=True, dashes_enabled=False)
        self.assertEqual(action, ("move", (4, 2), "right"), reason)
        action, reason = strategy.choose(info, attacks_enabled=False, dashes_enabled=False)
        self.assertIsNone(action)
        self.assertIn("orange targets unreachable", reason)

    def test_real_purple_ticket_is_not_selected_as_the_player(self):
        fixture = (Path(__file__).with_name("fixtures") /
                   "digiworld_player_vs_purple_ticket.png")
        with open_test_image(fixture) as image:
            rgb = image.convert("RGB")

        detection = bot.classify(rgb)
        self.assertEqual(detection.state, "digiworld")
        info = strategy.cells(rgb, detection.board)

        # The ticket's white artwork slightly outscores the dark Digimon under
        # the raw player mask, but it is independently a visible pink pickup.
        self.assertGreater(info[(4, 2)]["player"], info[(1, 1)]["player"])
        self.assertGreater(info[(4, 2)]["item"], strategy.ITEM_THRESHOLD)
        self.assertLessEqual(info[(1, 1)]["item"], strategy.ITEM_THRESHOLD)
        self.assertEqual(strategy.player_cell(info)[0], (1, 1))
        self.assertEqual(runner.player_cell(info)[0], (1, 1))

        action, reason = strategy.choose(info, dashes_enabled=False)
        self.assertIsNotNone(action, reason)
        self.assertEqual(
            abs(action[1][0] - 1) + abs(action[1][1] - 1), 1, reason
        )

    def test_shifted_bottom_wall_is_not_a_pyramid_or_attack_target(self):
        fixture = (Path(__file__).with_name("fixtures") /
                   "bottom_row_shifted_wall_false_pyramid.png")
        with open_test_image(fixture) as image:
            rgb = image.convert("RGB")

        detection = bot.classify(rgb)
        self.assertEqual(detection.state, "digiworld")
        info = strategy.cells(rgb, detection.board)
        player = max(info, key=lambda cell: info[cell]["player"])
        target = info[(4, 2)]

        self.assertEqual(player, (4, 1))
        # The full cell genuinely crosses the old pyramid threshold because
        # the scrolling foreground wall has shifted into this column.
        self.assertGreater(target["pyramid"], strategy.PYRAMID_THRESHOLD)
        self.assertGreater(target["foreground_wall"], .20)
        self.assertGreater(target["visible_floor"], .90)
        self.assertFalse(strategy.is_obstacle(target))
        for pyramid in ((0, 4), (1, 0), (3, 0), (3, 1), (3, 3)):
            self.assertTrue(strategy.is_obstacle(info[pyramid]), pyramid)

        action, reason = strategy.choose(
            info, previous_direction="right", dashes_enabled=False
        )
        self.assertEqual(action, ("move", (4, 2), "right"), reason)

    def test_bottom_wall_filter_preserves_real_bottom_row_pyramids(self):
        fixture = (Path(__file__).with_name("fixtures") /
                   "digiworld_heavy_pyramid_occlusion.png")
        with open_test_image(fixture) as image:
            rgb = image.convert("RGB")

        detection = bot.classify(rgb)
        self.assertEqual(detection.state, "digiworld")
        info = strategy.cells(rgb, detection.board)

        for pyramid in ((4, 0), (4, 1)):
            with self.subTest(pyramid=pyramid):
                self.assertGreater(
                    info[pyramid]["pyramid_body"],
                    strategy.PYRAMID_THRESHOLD,
                )
                self.assertTrue(strategy.is_obstacle(info[pyramid]))
        for clear in ((4, 3), (4, 4)):
            with self.subTest(clear=clear):
                self.assertFalse(strategy.is_obstacle(info[clear]))

    def test_real_vertical_oscillation_has_an_alternative_route(self):
        fixture = (Path(__file__).with_name("fixtures") /
                   "digiworld_vertical_oscillation.png")
        with open_test_image(fixture) as image:
            rgb = image.convert("RGB")
        detection = bot.classify(rgb)
        self.assertEqual(detection.state, "digiworld")
        info = strategy.cells(rgb, detection.board)
        player = max(info, key=lambda cell: info[cell]["player"])
        self.assertEqual(player, (3, 1))

        normal, _ = strategy.choose(
            info, previous_direction="up", dashes_enabled=False
        )
        guarded, _ = strategy.choose(
            info, previous_direction="up", dashes_enabled=False,
            avoid_first_step=(4, 1),
        )

        self.assertEqual(normal, ("move", (4, 1), "down"))
        self.assertEqual(guarded, ("attack", (3, 2), "right"))

    def test_real_grid_takes_priority_over_lower_cyan_menu_region(self):
        fixtures = Path(__file__).with_name("fixtures")
        for name, expected_player in (
            ("bottom_row_player_dense_pyramids_after_attack.png", (4, 1)),
            ("bottom_row_player_dense_pyramids_cyan_tiles.png", (4, 1)),
            ("digiworld_dense_pyramids_weak_horizontal_edges.png", (3, 1)),
            ("digiworld_heavy_pyramid_occlusion.png", (2, 1)),
            ("digiworld_top_row_pyramids_weakened_lattice.png", (4, 1)),
            ("digiworld_two_hidden_horizontal_edges.png", (4, 1)),
        ):
            with self.subTest(name=name), open_test_image(fixtures / name) as image:
                rgb = image.convert("RGB")

                # This is the ambiguous signal which used to short-circuit
                # classify() as title_or_menu before the geometric grid
                # evidence was considered.
                self.assertTrue(bot._has_prominent_lower_cyan(rgb))

                detection = bot.classify(rgb)
                self.assertEqual(detection.state, "digiworld")
                self.assertIsNotNone(detection.board)
                self.assertGreaterEqual(detection.confidence, .80)

                info = strategy.cells(rgb, detection.board)
                player_score, player = max(
                    (values["player"], cell) for cell, values in info.items()
                )
                self.assertEqual(player, expected_player)
                self.assertGreaterEqual(player_score, .08)

    def test_lower_cyan_control_without_grid_remains_a_menu(self):
        image = Image.new("RGB", (900, 1600), (4, 9, 18))
        ImageDraw.Draw(image).rectangle(
            (250, 1270, 650, 1420), fill=(18, 105, 185)
        )
        detection = bot.classify(image)
        self.assertEqual(detection.state, "title_or_menu")
        self.assertIsNone(detection.board)

    def test_dense_pyramids_accept_a_thirteen_point_horizontal_edge(self):
        image = Image.new("RGB", (900, 1600), (4, 9, 18))
        x_candidate = (
            24.6,
            [91, 233, 370, 506, 643, 781],
            [61.1, 55.0, 33.3, 25.9, 21.0, 20.5],
        )
        y_candidate = (
            16.2,
            [532, 643, 755, 868, 979, 1086],
            [13.34, 27.3, 31.9, 38.1, 55.4, 6.6],
        )
        with patch.object(
                bot, "_best_six_lines", side_effect=[x_candidate, y_candidate]):
            detection = bot.classify(image)
        self.assertEqual(detection.state, "digiworld")
        self.assertEqual(detection.board, (93, 533, 781, 1088))
        self.assertGreaterEqual(detection.confidence, .80)

    def test_pyramid_exception_still_requires_strong_aggregate_vertical_edges(self):
        image = Image.new("RGB", (900, 1600), (4, 9, 18))
        weak_x_candidate = (
            21.9,
            [91, 233, 370, 506, 643, 781],
            [61.1, 32.0, 26.0, 22.0, 19.5, 18.5],
        )
        occluded_y_candidate = (
            16.2,
            [532, 643, 755, 868, 979, 1086],
            [13.34, 27.3, 31.9, 38.1, 55.4, 6.6],
        )
        with patch.object(
                bot, "_best_six_lines",
                side_effect=[weak_x_candidate, occluded_y_candidate]):
            detection = bot.classify(image)
        self.assertEqual(detection.state, "unknown")
        self.assertIsNone(detection.board)

    def test_three_line_exception_requires_distributed_board_content(self):
        image = Image.new("RGB", (900, 1600), (4, 9, 18))
        strong_x_candidate = (
            27.1,
            [91, 233, 369, 506, 644, 781],
            [61.1, 47.7, 38.9, 27.9, 23.4, 21.6],
        )
        incoherent_y_candidate = (
            14.4,
            [532, 644, 755, 867, 974, 1085],
            [11.2, 28.9, 51.8, 37.7, 13.7, 12.2],
        )
        with patch.object(
                bot, "_best_six_lines",
                side_effect=[strong_x_candidate, incoherent_y_candidate]):
            detection = bot.classify(image)
        # Strong synthetic lattice scores alone are insufficient: a blank
        # menu/panel has no texture distributed through the 25 cell interiors.
        self.assertEqual(detection.state, "unknown")
        self.assertIsNone(detection.board)

    def test_two_hidden_edges_still_require_four_strong_horizontal_lines(self):
        image = Image.new("RGB", (900, 1600), (4, 9, 18))
        strong_x_candidate = (
            25.0,
            [91, 233, 369, 506, 644, 781],
            [61.1, 41.4, 27.5, 25.6, 21.7, 21.3],
        )
        only_three_strong_y_lines = (
            13.3,
            [532, 641, 755, 867, 981, 1092],
            [10.6, 32.5, 33.8, 24.9, 47.5, 5.7],
        )
        with patch.object(
                bot, "_best_six_lines",
                side_effect=[strong_x_candidate, only_three_strong_y_lines]):
            detection = bot.classify(image)
        self.assertEqual(detection.state, "unknown")
        self.assertIsNone(detection.board)

    def test_dark_player_partially_hidden_on_bottom_row(self):
        fixtures = Path(__file__).with_name("fixtures")
        for name in (
            "bottom_row_player.png",
            "bottom_row_player_occluded.png",
            "bottom_row_player_dense_pyramids.png",
            "bottom_row_player_dense_pyramids_after_attack.png",
        ):
            with self.subTest(name=name), open_test_image(fixtures / name) as image:
                rgb = image.convert("RGB")
                detection = bot.classify(rgb)
                self.assertEqual(detection.state, "digiworld")
                self.assertGreaterEqual(detection.confidence, .80)
                info = strategy.cells(rgb, detection.board)
                ranked = sorted(
                    ((values["player"], cell) for cell, values in info.items()),
                    reverse=True,
                )
                self.assertEqual(ranked[0][1], (4, 1), ranked[:5])
                self.assertGreaterEqual(ranked[0][0], .08, ranked[:5])
                for col in (0, 2, 3, 4):
                    self.assertLess(info[(4, col)]["player"], .08)


class DeviceSelectionTests(unittest.TestCase):
    def tearDown(self):
        bot.close_tap_session()

    def test_adb_process_uses_no_window_flag_on_windows(self):
        completed = Mock(stdout="List of devices attached\nemulator-5554\tdevice\n")
        with (patch.object(bot.subprocess, "CREATE_NO_WINDOW", 0x08000000, create=True),
              patch.object(bot.subprocess, "run", return_value=completed) as run):
            self.assertEqual(bot._device_rows("HD-Adb.exe"), [("emulator-5554", "device")])
        self.assertEqual(run.call_args.kwargs["creationflags"], 0x08000000)

    def test_prefers_bluestacks_tcp_serial(self):
        rows = [("emulator-5554", "device"), ("127.0.0.1:5555", "device")]
        with patch.object(bot, "_device_rows", return_value=rows):
            self.assertEqual(bot.resolve_serial("adb.exe"), "127.0.0.1:5555")

    def test_auto_discovery_probes_tcp_even_when_emulator_is_already_listed(self):
        before = [("emulator-5554", "device")]
        after = before + [("127.0.0.1:5555", "device")]
        with (patch.object(bot, "_device_rows", side_effect=[before, after]),
              patch.object(bot, "_run_hidden") as run):
            serial = bot.resolve_serial("HD-Adb.exe")
        self.assertEqual(serial, "127.0.0.1:5555")
        self.assertEqual(
            run.call_args.args[0],
            ["HD-Adb.exe", "connect", "127.0.0.1:5555"],
        )

    def test_prefers_single_bluestacks_tcp_port_over_emulator(self):
        rows = [("emulator-5554", "device"), ("127.0.0.1:5565", "device")]
        with (patch.object(bot, "_device_rows", return_value=rows),
              patch.object(bot, "_run_hidden")):
            self.assertEqual(bot.resolve_serial("adb.exe"), "127.0.0.1:5565")

    def test_requires_explicit_choice_for_ambiguous_devices(self):
        rows = [("127.0.0.1:5565", "device"), ("127.0.0.1:5575", "device")]
        with (patch.object(bot, "_device_rows", return_value=rows),
              self.assertRaises(RuntimeError)):
            bot.resolve_serial("adb.exe")

    def test_taps_use_checked_one_shot_adb_commands(self):
        completed = Mock(stdout="")
        with patch.object(bot.subprocess, "run", return_value=completed) as run:
            bot.tap("HD-Adb.exe", "127.0.0.1:5555", 123, 456)
            bot.tap("HD-Adb.exe", "127.0.0.1:5555", 234, 567)
        self.assertEqual(
            [call.args[0] for call in run.call_args_list],
            [["HD-Adb.exe", "-s", "127.0.0.1:5555", "shell", "input", "tap", "123", "456"],
             ["HD-Adb.exe", "-s", "127.0.0.1:5555", "shell", "input", "tap", "234", "567"]],
        )

    def test_swipes_use_checked_one_shot_adb_commands(self):
        completed = Mock(stdout="")
        with patch.object(bot.subprocess, "run", return_value=completed) as run:
            bot.swipe("HD-Adb.exe", "127.0.0.1:5555", 700, 1300, 700, 500, 250)
        self.assertEqual(
            run.call_args.args[0],
            ["HD-Adb.exe", "-s", "127.0.0.1:5555", "shell", "input",
             "swipe", "700", "1300", "700", "500", "250"],
        )

    def test_failed_adb_tap_is_not_silently_accepted(self):
        failure = subprocess.CalledProcessError(1, "adb")
        with (patch.object(bot.subprocess, "run", side_effect=failure),
              self.assertRaises(bot.AdbInputError) as raised):
            bot.tap("HD-Adb.exe", "127.0.0.1:5555", 123, 456)
        self.assertEqual(raised.exception.returncode, 1)

    def test_timeout_and_os_errors_are_wrapped_as_input_errors(self):
        errors = (
            subprocess.TimeoutExpired("adb", 15),
            BrokenPipeError("pipe closed"),
            OSError("device transport lost"),
        )
        for failure in errors:
            with (self.subTest(error=type(failure).__name__),
                  patch.object(bot.subprocess, "run", side_effect=failure),
                  self.assertRaises(bot.AdbInputError)):
                bot.tap("HD-Adb.exe", "emulator-5554", 123, 456)


class AdbRecoveryTests(unittest.TestCase):
    image = Image.new("RGB", (20, 20), (0, 0, 0))

    def test_failed_tap_rescans_before_next_tap_succeeds(self):
        order = []
        failure = subprocess.CalledProcessError(
            1, "adb", output="daemon restarted", stderr="device offline")

        def tapped(*_args, **_kwargs):
            order.append("tap")
            if order.count("tap") == 1:
                raise failure

        with (patch.object(bot, "tap", side_effect=tapped),
              patch.object(bot, "resolve_serial",
                           side_effect=lambda *_: order.append("resolve") or "127.0.0.1:5555"),
              patch.object(bot, "screenshot",
                           side_effect=lambda *_: order.append("screenshot") or self.image)):
            first = bot.dispatch_tap_with_recovery(
                "adb", "emulator-5554", 10, 20,
                action="move", reconnect_backoff=0,
                recovery_settle_seconds=0)
            second = bot.dispatch_tap_with_recovery(
                "adb", first.serial, 30, 40,
                action="move", reconnect_backoff=0,
                recovery_settle_seconds=0)

        self.assertEqual(first.outcome, "rescan")
        self.assertEqual(second.outcome, "sent")
        self.assertEqual(
            order, ["tap", "resolve", "screenshot", "tap"])
        self.assertEqual(first.recovery_events[0]["return_code"], 1)
        self.assertEqual(first.recovery_events[0]["stderr"], "device offline")

    def test_failed_swipe_is_never_repeated_during_recovery(self):
        failure = subprocess.CalledProcessError(
            1, "adb", output="", stderr="device offline")
        with (patch.object(bot, "swipe", side_effect=failure) as swipe,
              patch.object(bot, "resolve_serial", return_value="127.0.0.1:5555"),
              patch.object(bot, "screenshot", return_value=self.image)):
            result = bot.dispatch_swipe_with_recovery(
                "adb", "emulator-5554", 700, 1300, 700, 500, 250,
                requested_serial="auto", reconnect_backoff=0,
                recovery_settle_seconds=0)
        self.assertEqual(result.outcome, "rescan")
        swipe.assert_called_once_with(
            "adb", "emulator-5554", 700, 1300, 700, 500, 250)
        self.assertEqual(result.recovery_events[0]["adb_swipe"],
                         [700, 1300, 700, 500, 250])

    def test_stop_before_swipe_sends_no_input(self):
        with patch.object(bot, "swipe") as swipe:
            result = bot.dispatch_swipe_with_recovery(
                "adb", "emulator-5554", 1, 2, 3, 4,
                should_continue=lambda _seconds: False)
        self.assertEqual(result.outcome, "stopped")
        swipe.assert_not_called()

    def test_all_recovery_attempts_fail(self):
        failure = subprocess.CalledProcessError(1, "adb")
        with (patch.object(bot, "tap", side_effect=failure),
              patch.object(bot, "resolve_serial", side_effect=OSError("offline")) as resolve):
            result = bot.dispatch_tap_with_recovery(
                "adb", "emulator-5554", 10, 20,
                reconnect_attempts=3, reconnect_backoff=0)
        self.assertEqual(result.outcome, "failed")
        self.assertEqual(resolve.call_count, 3)
        self.assertEqual(result.recovery_events[-1]["status"], "recovery_exhausted")

    def test_missing_device_is_recovered_with_a_new_serial(self):
        with (patch.object(bot, "tap", side_effect=OSError("transport lost")),
              patch.object(bot, "resolve_serial", return_value="127.0.0.1:5565"),
              patch.object(bot, "screenshot", return_value=self.image)):
            result = bot.dispatch_tap_with_recovery(
                "adb", "emulator-5554", 10, 20,
                requested_serial="auto", reconnect_backoff=0,
                recovery_settle_seconds=0)
        self.assertEqual(result.outcome, "rescan")
        self.assertEqual(result.serial, "127.0.0.1:5565")
        self.assertEqual(
            result.recovery_events[-1]["new_serial"], "127.0.0.1:5565")

    def test_failed_tap_is_not_counted_as_a_confirmed_action(self):
        dispatch = bot.TapDispatchResult("rescan", "emulator-5554")
        sent = []
        self.assertFalse(runner.append_confirmed_action(
            sent, dispatch, {"type": "move"}))
        self.assertEqual(sent, [])

    def test_partial_batch_counts_only_confirmed_actions(self):
        sent = []
        self.assertTrue(runner.append_confirmed_action(
            sent, bot.TapDispatchResult("sent", "emulator-5554"),
            {"type": "move", "target_cell": [2, 2]}))
        self.assertFalse(runner.append_confirmed_action(
            sent, bot.TapDispatchResult("rescan", "emulator-5554"),
            {"type": "move", "target_cell": [2, 3]}))
        self.assertEqual(
            sent, [{"type": "move", "target_cell": [2, 2]}])

    def test_recovery_waits_for_action_settle_before_observation(self):
        order = []

        def wait(seconds):
            order.append(("wait", seconds))
            return True

        def failed_tap(*_args):
            order.append("tap")
            raise OSError("offline")

        with (patch.object(bot, "tap", side_effect=failed_tap),
              patch.object(bot, "resolve_serial",
                           side_effect=lambda *_: order.append("resolve") or "127.0.0.1:5555"),
              patch.object(bot, "screenshot",
                           side_effect=lambda *_: order.append("screenshot") or self.image),
              patch.object(bot, "classify") as classify):
            result = bot.dispatch_tap_with_recovery(
                "adb", "emulator-5554", 10, 20,
                should_continue=wait,
                reconnect_backoff=.25,
                recovery_settle_seconds=.90)

        self.assertEqual(result.outcome, "rescan")
        self.assertEqual(order, [
            ("wait", 0.0), "tap", ("wait", .25), "resolve",
            ("wait", .90), "screenshot",
        ])
        classify.assert_not_called()

    def test_stop_during_recovery_settle_prevents_screenshot(self):
        waits = iter((True, True, False))
        with (patch.object(bot, "tap", side_effect=OSError("offline")),
              patch.object(bot, "resolve_serial", return_value="127.0.0.1:5555"),
              patch.object(bot, "screenshot") as screenshot,
              patch.object(bot, "classify") as classify):
            result = bot.dispatch_tap_with_recovery(
                "adb", "emulator-5554", 10, 20,
                should_continue=lambda _seconds: next(waits),
                reconnect_backoff=.25,
                recovery_settle_seconds=1.20)
        self.assertEqual(result.outcome, "stopped")
        self.assertEqual(result.recovery_events[-1]["phase"], "action_settle")
        screenshot.assert_not_called()
        classify.assert_not_called()

    def test_explicit_serial_is_preserved_during_recovery(self):
        with (patch.object(bot, "tap", side_effect=OSError("offline")),
              patch.object(bot, "resolve_serial", return_value="127.0.0.1:5565") as resolve,
              patch.object(bot, "screenshot", return_value=self.image)):
            result = bot.dispatch_tap_with_recovery(
                "adb", "127.0.0.1:5565", 10, 20,
                requested_serial="127.0.0.1:5565",
                reconnect_backoff=0, recovery_settle_seconds=0)
        self.assertEqual(result.outcome, "rescan")
        resolve.assert_called_once_with("adb", "127.0.0.1:5565")
        self.assertEqual(
            result.recovery_events[-1]["requested_serial"],
            "127.0.0.1:5565")

    def test_worker_keeps_requested_serial_separate_from_active_serial(self):
        args = SimpleNamespace(
            adb="adb", requested_serial="127.0.0.1:5565",
            serial="127.0.0.1:5565", pause_file=None, stop_file=None,
        )
        dispatch = bot.TapDispatchResult("sent", "127.0.0.1:5565")
        with tempfile.TemporaryDirectory() as temp, \
                patch.object(bot, "dispatch_tap_with_recovery",
                             return_value=dispatch) as send:
            result = runner.dispatch_worker_tap(
                args, Path(temp) / "events.jsonl", 10, 20, "attack", 1.20)
        self.assertIs(result, dispatch)
        self.assertEqual(args.serial, "127.0.0.1:5565")
        self.assertEqual(
            send.call_args.kwargs["requested_serial"], "127.0.0.1:5565")
        self.assertEqual(send.call_args.kwargs["recovery_settle_seconds"], 1.20)

    def test_explicit_serial_never_switches_to_other_bluestacks_instance(self):
        rows = [("127.0.0.1:5575", "device")]
        with (patch.object(bot, "tap", side_effect=OSError("offline")) as tap,
              patch.object(bot, "_run_hidden"),
              patch.object(bot, "_device_rows", return_value=rows),
              patch.object(bot, "screenshot") as screenshot):
            result = bot.dispatch_tap_with_recovery(
                "adb", "127.0.0.1:5565", 10, 20,
                requested_serial="127.0.0.1:5565",
                reconnect_attempts=3, reconnect_backoff=0,
                recovery_settle_seconds=0)
        self.assertEqual(result.outcome, "failed")
        self.assertEqual(result.serial, "127.0.0.1:5565")
        tap.assert_called_once()
        screenshot.assert_not_called()

    def test_auto_serial_can_rediscover_new_bluestacks_serial(self):
        with (patch.object(bot, "tap", side_effect=OSError("offline")),
              patch.object(bot, "resolve_serial", return_value="127.0.0.1:5555") as resolve,
              patch.object(bot, "screenshot", return_value=self.image)):
            result = bot.dispatch_tap_with_recovery(
                "adb", "emulator-5554", 10, 20,
                requested_serial="auto", reconnect_backoff=0,
                recovery_settle_seconds=0)
        self.assertEqual(result.outcome, "rescan")
        self.assertEqual(result.serial, "127.0.0.1:5555")
        resolve.assert_called_once_with("adb", "auto")

    def test_ambiguous_failed_tap_is_never_repeated_inside_dispatch(self):
        with (patch.object(bot, "tap", side_effect=OSError("offline")) as tap,
              patch.object(bot, "resolve_serial", side_effect=OSError("offline"))):
            result = bot.dispatch_tap_with_recovery(
                "adb", "emulator-5554", 10, 20,
                reconnect_attempts=3, reconnect_backoff=0)
        self.assertEqual(result.outcome, "failed")
        tap.assert_called_once()

    def test_exhausted_recovery_emits_controlled_error_without_changing_done(self):
        recovery = ({"status": "recovery_exhausted", "attempts": 3},)
        result = bot.TapDispatchResult(
            "failed", "emulator-5554", recovery)
        event = {"action": "move"}
        with tempfile.TemporaryDirectory() as temp, \
                patch.object(runner, "emit_status") as emit:
            exit_code = runner.finish_failed_adb_recovery(
                Path(temp) / "events.jsonl", event, result, 7, 100)
        self.assertEqual(exit_code, runner.ADB_RECOVERY_EXIT)
        emit.assert_called_once_with(
            "error", runner.ADB_RECOVERY_MESSAGE, 7, 100,
            serial="emulator-5554")

    def test_stop_during_recovery_prevents_rediscovery_and_retry(self):
        waits = iter((True, False))
        with (patch.object(bot, "tap", side_effect=OSError("offline")),
              patch.object(bot, "resolve_serial") as resolve):
            result = bot.dispatch_tap_with_recovery(
                "adb", "emulator-5554", 10, 20,
                should_continue=lambda _seconds: next(waits),
                reconnect_backoff=0)
        self.assertEqual(result.outcome, "stopped")
        resolve.assert_not_called()


class StrategyTests(unittest.TestCase):
    def test_item_art_overrides_pyramid_color(self):
        self.assertFalse(strategy.is_obstacle({"pyramid": 0.60, "item": 0.09}))

    def test_clipped_pyramid_is_blocked(self):
        self.assertTrue(strategy.is_obstacle({"pyramid": 0.22, "item": 0.02}))

    def test_bright_walkable_tile_is_not_a_pyramid(self):
        self.assertFalse(strategy.is_obstacle({
            "pyramid": .2253,
            "item": 0.0,
            "highlight": .97806,
        }))

    def test_highlighted_lower_corridor_routes_around_pyramids(self):
        info = empty_grid()
        info[(2, 1)]["player"] = .2
        for cell in ((0, 3), (1, 3), (2, 2), (2, 3),
                     (3, 3), (3, 4), (4, 0), (4, 1)):
            info[cell].update(pyramid=.80, highlight=.45)
        # These are the two bright empty cells from the reported frame. Their
        # loose pyramid color score must not close the lower entrance.
        info[(3, 0)].update(pyramid=.23352, highlight=.98964)
        info[(3, 1)].update(pyramid=.22530, highlight=.97806)
        action, reason = strategy.choose(
            info, attacks_enabled=False, dashes_enabled=False
        )
        self.assertEqual(action, ("move", (3, 1), "down"), reason)

    def test_orange_is_absolute_priority_even_when_behind(self):
        info = empty_grid()
        info[(2, 2)]["player"] = .2
        place_item(info, (2, 0), "orange")
        place_item(info, (2, 3), "green")
        place_item(info, (1, 2), "pink")
        action, reason = strategy.choose(info)
        self.assertEqual(action, ("move", (2, 1), "left"))
        self.assertIn("orange", reason)

    def test_green_dash_pickup_beats_unprofitable_purple_detour(self):
        info = empty_grid()
        info[(0, 0)]["player"] = .2
        place_item(info, (0, 4), "green")
        place_item(info, (4, 0), "pink")
        action, reason = strategy.choose(info)
        self.assertEqual(action, ("move", (0, 1), "right"))
        self.assertIn("green dash", reason)

    def test_green_dash_pickup_stays_ahead_of_profitable_purple_detour(self):
        info = empty_grid()
        info[(2, 0)]["player"] = .2
        place_item(info, (2, 4), "green")
        place_item(info, (1, 0), "pink")
        action, reason = strategy.choose(info)
        self.assertEqual(action, ("move", (2, 1), "right"))
        self.assertIn("green dash", reason)

    def test_green_and_purple_never_cause_backtracking(self):
        info = empty_grid()
        info[(2, 2)]["player"] = .2
        place_item(info, (2, 0), "green")
        place_item(info, (1, 0), "pink")
        action, reason = strategy.choose(info)
        self.assertEqual(action, ("move", (2, 3), "right"))
        self.assertIn("forward", reason)

    def test_purple_detour_is_taken_when_four_step_value_covers_it(self):
        info = empty_grid()
        info[(2, 0)]["player"] = .2
        place_item(info, (1, 0), "pink")
        action, reason = strategy.choose(info)
        self.assertEqual(action, ("move", (1, 0), "up"))
        self.assertIn("purple", reason)

    def test_purple_detour_over_four_steps_is_ignored(self):
        info = empty_grid()
        info[(0, 0)]["player"] = .2
        place_item(info, (4, 0), "pink")
        action, reason = strategy.choose(info)
        self.assertEqual(action, ("move", (0, 1), "right"))
        self.assertIn("forward", reason)

    def test_blocked_right_route_uses_vertical_detour(self):
        info = empty_grid()
        info[(2, 1)]["player"] = 0.2
        info[(2, 2)]["pyramid"] = 0.25
        action, _ = strategy.choose(info, attacks_enabled=False,
                                    dashes_enabled=False)
        self.assertIn(action[2], ("up", "down"))

    def test_attack_cost_prefers_longer_clear_route_to_orange(self):
        info = empty_grid()
        info[(2, 1)]["player"] = .2
        info[(2, 2)]["pyramid"] = .25
        place_item(info, (2, 3), "orange")
        action, reason = strategy.choose(info, dashes_enabled=False)
        self.assertEqual(action[0], "move")
        self.assertIn(action[2], ("up", "down"))
        self.assertIn("orange", reason)

    def test_attack_is_used_when_orange_dash_landing_is_blocked(self):
        info = empty_grid()
        info[(2, 1)]["player"] = .2
        for cell in ((2, 0), (1, 1), (2, 2), (3, 1), (2, 4)):
            info[cell]["pyramid"] = .25
        place_item(info, (2, 3), "orange")
        action, reason = strategy.choose(info, dashes_enabled=True)
        self.assertEqual(action, ("attack", (2, 2), "right"))
        self.assertIn("orange", reason)

    def test_dash_is_not_used_on_a_clear_route(self):
        info = empty_grid()
        info[(2, 1)]["player"] = .2
        action, _ = strategy.choose(info, dashes_enabled=True)
        self.assertEqual(action, ("move", (2, 2), "right"))

    def test_dash_is_not_used_when_clear_detour_exists(self):
        info = empty_grid()
        info[(2, 1)]["player"] = .2
        info[(2, 2)]["pyramid"] = .25
        action, _ = strategy.choose(info, dashes_enabled=True)
        self.assertEqual(action[0], "move")
        self.assertIn(action[2], ("up", "down"))

    def test_dash_crosses_exactly_three_cells_only_when_route_is_blocked(self):
        info = empty_grid()
        info[(2, 1)]["player"] = .2
        for cell in ((2, 0), (1, 1), (2, 2), (3, 1)):
            info[cell]["pyramid"] = .25
        action, reason = strategy.choose(info, dashes_enabled=True)
        self.assertEqual(action, ("dash", (2, 4), "right"))
        self.assertIn("blocked", reason)

    def test_dash_is_not_used_when_exact_landing_is_blocked(self):
        info = empty_grid()
        info[(2, 1)]["player"] = .2
        for cell in ((2, 0), (1, 1), (2, 2), (3, 1), (2, 4)):
            info[cell]["pyramid"] = .25
        action, _ = strategy.choose(info, dashes_enabled=True)
        self.assertEqual(action, ("attack", (2, 2), "right"))

    def test_visible_orange_uses_safe_dash_when_boxed(self):
        info = empty_grid()
        info[(2, 1)]["player"] = .2
        for cell in ((2, 0), (1, 1), (2, 2), (3, 1)):
            info[cell]["pyramid"] = .25
        place_item(info, (4, 4), "orange")
        action, reason = strategy.choose(info, dashes_enabled=True)
        self.assertEqual(action, ("dash", (2, 4), "right"))
        self.assertIn("orange", reason)

    def test_observed_orange_blocker_scene_uses_dash(self):
        info = empty_grid()
        info[(3, 1)]["player"] = .2
        for cell in (
                (0, 1), (0, 2), (0, 4), (1, 3),
                (2, 3), (3, 2), (4, 1), (4, 2)):
            info[cell]["pyramid"] = .25
        place_item(info, (4, 3), "orange")

        action, reason = strategy.choose(info, dashes_enabled=True)

        self.assertEqual(action, ("dash", (3, 4), "right"))
        self.assertIn("orange priority", reason)
        self.assertIn("blocked route", reason)

    def test_random_non_orange_routes_never_step_left(self):
        rng = random.Random(3417)
        for case in range(250):
            info = empty_grid()
            player = (rng.randrange(5), rng.randrange(5))
            info[player]["player"] = .2
            for cell in info:
                if cell != player and rng.random() < .28:
                    info[cell]["pyramid"] = .25
            for category in ("green", "pink"):
                if rng.random() < .7:
                    target = (rng.randrange(5), rng.randrange(5))
                    if target != player:
                        place_item(info, target, category)
            action, reason = strategy.choose(
                info, dashes_enabled=bool(rng.randrange(2))
            )
            if action is None:
                continue
            self.assertNotEqual(action[2], "left", (case, action, reason))
            if action[0] == "dash":
                self.assertEqual(action[1][1] - player[1], 3)
                self.assertTrue(strategy.is_obstacle(
                    info[(player[0], player[1] + 1)]
                ))

    def test_random_orange_routes_keep_orange_as_objective(self):
        rng = random.Random(90210)
        for case in range(150):
            info = empty_grid()
            player = (rng.randrange(5), rng.randrange(5))
            info[player]["player"] = .2
            for cell in info:
                if cell != player and rng.random() < .32:
                    info[cell]["pyramid"] = .25
            orange = player
            while orange == player:
                orange = (rng.randrange(5), rng.randrange(5))
            place_item(info, orange, "orange")
            for category in ("green", "pink"):
                target = (rng.randrange(5), rng.randrange(5))
                if target not in (player, orange):
                    place_item(info, target, category)
            action, reason = strategy.choose(info, dashes_enabled=True)
            self.assertIsNotNone(action, (case, orange, reason))
            self.assertIn("orange priority", reason, (case, action, reason))
            if action[0] == "dash":
                landing = action[1]
                self.assertEqual(landing[1] - player[1], 3)
                self.assertFalse(strategy.is_obstacle(info[landing]))
                self.assertTrue(strategy.is_obstacle(
                    info[(player[0], player[1] + 1)]
                ))

    def test_oscillation_guard_uses_an_alternative_first_step(self):
        info = empty_grid()
        player = (3, 1)
        info[player]["player"] = .2
        info[(2, 1)]["pyramid"] = .25
        info[(3, 2)]["pyramid"] = .25

        normal, _ = strategy.choose(info, dashes_enabled=False)
        guarded, _ = strategy.choose(
            info, dashes_enabled=False, avoid_first_step=(4, 1)
        )

        self.assertEqual(normal, ("move", (4, 1), "down"))
        self.assertEqual(guarded, ("attack", (3, 2), "right"))

    def test_oscillation_guard_preserves_orange_as_the_objective(self):
        info = empty_grid()
        player = (3, 1)
        info[player]["player"] = .2
        info[(2, 1)]["pyramid"] = .25
        info[(3, 2)]["pyramid"] = .25
        place_item(info, (3, 4), "orange")

        action, reason = strategy.choose(
            info, dashes_enabled=False, avoid_first_step=(4, 1)
        )

        self.assertIsNotNone(action)
        self.assertNotEqual(action[1], (4, 1))
        self.assertIn("orange priority", reason)


class BatchTests(unittest.TestCase):
    def test_pending_move_rejects_unrelated_player_but_accepts_scroll_and_batch(self):
        for previous, expected, current, direction, valid in (
            ((3, 1), (3, 1), (0, 2), "right", False),
            ((3, 1), (3, 1), (3, 1), "right", True),
            ((3, 1), (3, 1), (3, 2), "right", True),
            ((3, 1), (4, 1), (3, 2), "down", False),
            ((3, 1), (4, 1), (4, 1), "down", True),
            ((2, 4), (2, 3), (2, 4), "left", True),
            ((2, 4), (2, 3), (2, 2), "left", False),
            ((3, 1), (3, 3), (3, 2), "right", True),
        ):
            with self.subTest(current=current, direction=direction):
                self.assertEqual(runner.player_matches_pending_move(
                    previous, expected, current, direction), valid)

    def test_worker_rescans_false_player_without_attacking_or_consuming_move(self):
        # Replay the logged 712 -> 713 failure: one right move at (3, 1),
        # then a spurious player at (0, 2), then the real player reappears.
        good = empty_grid()
        good[(3, 1)]["player"] = .11
        bad = empty_grid()
        bad[(0, 2)]["player"] = .08946
        bad[(0, 3)]["pyramid"] = .9
        bad[(1, 2)]["pyramid"] = .9
        self.assertEqual(strategy.choose(bad)[0], ("attack", (0, 3), "right"))
        frame = Image.new("RGB", (900, 1600))
        with tempfile.TemporaryDirectory() as temp, ExitStack() as stack:
            stack.enter_context(patch.object(sys, "argv", [
                "worker", "--steps", "2", "--out", temp]))
            for name, value in (
                ("resolve_adb", "adb"), ("resolve_serial", "serial"),
                ("screenshot", frame), ("tutorial_overlay_center", None),
                ("diaboromon_attempt_button", None),
                ("diaboromon_find_party_button", None),
                ("diaboromon_party_count", None),
                ("diaboromon_battle_banner_visible", False),
                ("diaboromon_lobby_visible", False),
                ("classify", bot.Detection("digiworld", .9, (90, 530, 780, 1090), "test")),
            ):
                stack.enter_context(patch.object(bot, name, return_value=value))
            stack.enter_context(patch.object(strategy, "cells", side_effect=[good, bad, good]))
            stack.enter_context(patch.object(runner, "controlled_wait", return_value=True))
            stack.enter_context(patch.object(runner, "emit_status"))
            dispatch = stack.enter_context(patch.object(
                runner, "dispatch_worker_tap", return_value=SimpleNamespace(outcome="sent")))
            self.assertEqual(runner.main(), 0)
            self.assertEqual(dispatch.call_count, 2)
            self.assertTrue(all(call.args[4] == "move" for call in dispatch.call_args_list))
            events = [json.loads(line) for line in next(Path(temp).glob("*/events.jsonl")).read_text().splitlines()]
            rejected = next(event for event in events if "player_check" in event)
            self.assertEqual(rejected["next_index"], 1)
            self.assertEqual(rejected["player_check"]["detected"], [0, 2])
            self.assertEqual(events[-2]["move_state"]["status"], "confirmed")

    def test_worker_reanchors_real_player_after_stale_pickup_move(self):
        self._assert_real_player_reanchor(interrupted=False)

    def test_worker_reanchors_real_player_beside_green_pickup(self):
        self._assert_real_player_reanchor(
            interrupted=False, fixture="player_stale_move_adjacent_green.png",
            target=[3, 1])

    def test_worker_reanchor_restarts_after_missing_sprite(self):
        self._assert_real_player_reanchor(interrupted=True)

    def _assert_real_player_reanchor(
            self, interrupted, fixture="player_stale_move_after_pickup.png",
            target=None):
        good = empty_grid()
        good[(3, 1)]["player"] = .12
        frame = open_test_image(Path(__file__).parent / "fixtures" /
                           fixture).convert("RGB")
        detection = bot.classify(frame)
        self.assertEqual(detection.state, "digiworld")
        observed = strategy.cells(frame, detection.board)
        self.assertEqual(strategy.player_cell(observed)[0], (4, 1))
        if fixture == "player_stale_move_adjacent_green.png":
            self.assertGreaterEqual(observed[(4, 2)]["player"], .08)
            self.assertEqual(strategy.pickup_category(observed[(4, 2)]), "green")
            self.assertLessEqual(observed[(4, 1)]["item"], strategy.ITEM_THRESHOLD)
        self.assertIsNotNone(runner.player_reanchor_evidence(observed, (4, 1)))
        with tempfile.TemporaryDirectory() as temp, ExitStack() as stack:
            stack.enter_context(patch.object(sys, "argv", [
                "worker", "--steps", "2", "--out", temp]))
            for name, value in (
                ("resolve_adb", "adb"), ("resolve_serial", "serial"),
                ("screenshot", frame), ("tutorial_overlay_center", None),
                ("diaboromon_attempt_button", None),
                ("diaboromon_find_party_button", None),
                ("diaboromon_party_count", None),
                ("diaboromon_battle_banner_visible", False),
                ("diaboromon_lobby_visible", False),
                ("classify", bot.Detection("digiworld", .9, (90, 530, 780, 1090), "test")),
            ):
                stack.enter_context(patch.object(bot, name, return_value=value))
            stack.enter_context(patch.object(strategy, "cells", side_effect=([good, observed, empty_grid(), observed, observed, observed]
                             if interrupted else [good, observed, observed, observed])))
            stack.enter_context(patch.object(runner, "controlled_wait", return_value=True))
            stack.enter_context(patch.object(runner, "emit_status"))
            dispatch = stack.enter_context(patch.object(
                runner, "dispatch_worker_tap", return_value=SimpleNamespace(outcome="sent")))
            self.assertEqual(runner.main(), 0)
            self.assertEqual(dispatch.call_count, 2)
            self.assertTrue(all(call.args[4] == "move" for call in dispatch.call_args_list))
            events = [json.loads(line) for line in next(Path(temp).glob("*/events.jsonl")).read_text().splitlines()]
            rejected = [event for event in events if "player_check" in event]
            self.assertEqual(len(rejected), 4 if interrupted else 2)
            self.assertTrue(all(event["next_index"] == 1 for event in rejected))
            recovered = next(event for event in events if "player_reanchor" in event)
            self.assertEqual(recovered["player_reanchor"]["settled_scans"], 3)
            self.assertEqual(recovered["player"]["cell"], [4, 1])
            self.assertEqual(recovered["action"][0]["target_cell"],
                             [4, 2] if target is None else target)

    def test_reanchor_rejects_missing_ambiguous_pickup_and_obstacle_sprites(self):
        for changes in ({"player": .07}, {"item": .2},
                        {"pyramid": .8}, {"highlight": .1}):
            info = empty_grid()
            info[(4, 1)].update(player=.2, highlight=.5)
            info[(4, 1)].update(changes)
            self.assertIsNone(runner.player_reanchor_evidence(info, (4, 1)))
        info = empty_grid()
        info[(4, 1)].update(player=.2, highlight=.5)
        info[(0, 2)]["player"] = .09
        self.assertIsNone(runner.player_reanchor_evidence(info, (4, 1)))

    def test_oscillation_guard_returns_the_cell_just_left(self):
        goals = ((2, 4),)
        states = [
            ((3, 1), goals), ((4, 1), goals),
            ((3, 1), goals), ((4, 1), goals),
        ]
        self.assertEqual(runner.oscillation_return_cell(states), (3, 1))
        self.assertIsNone(runner.oscillation_return_cell(states[:3]))
        self.assertIsNone(runner.oscillation_return_cell([
            ((3, 1), goals), ((4, 1), goals),
            ((3, 1), goals), ((2, 1), goals),
        ]))

    def test_batch_three_only_without_items(self):
        self.assertEqual(runner.adaptive_batch_limit(2, set()), 3)
        self.assertEqual(runner.adaptive_batch_limit(2, {(1, 4)}), 1)
        self.assertEqual(runner.adaptive_batch_limit(1, set()), 1)

    def test_pickup_animation_wait_only_follows_a_real_pickup(self):
        self.assertFalse(runner.should_wait_for_pickup_animation(False, 7, 0))
        self.assertTrue(runner.should_wait_for_pickup_animation(True, 3, 0))
        self.assertFalse(runner.should_wait_for_pickup_animation(True, 3, 1))
        self.assertFalse(runner.should_wait_for_pickup_animation(True, 2, 0))

    def test_action_delay_is_long_only_when_animation_needs_it(self):
        self.assertEqual(runner.action_settle_seconds("move", False, .35), .35)
        self.assertEqual(
            runner.action_settle_seconds("move", True, .35),
            runner.PICKUP_SETTLE_SECONDS,
        )
        self.assertEqual(
            runner.action_settle_seconds("dash", False, .35),
            runner.SPECIAL_ACTION_SETTLE_SECONDS,
        )

    def test_attack_requires_two_settled_frames_before_rejection(self):
        obstacle = {"pyramid": .8, "item": 0.0, "highlight": .4}
        cleared = {"pyramid": 0.0, "item": .1, "highlight": .9}
        self.assertEqual(
            runner.attack_verification_status(obstacle, 0), "wait"
        )
        self.assertEqual(
            runner.attack_verification_status(obstacle, 1), "failed"
        )
        self.assertEqual(
            runner.attack_verification_status(cleared, 0), "confirmed"
        )

    def test_item_category_uses_strongest_visible_color(self):
        self.assertEqual(runner.item_category({"orange": .09, "pink": .07, "green": .01}), "orange")
        self.assertIsNone(runner.item_category({"orange": .02, "pink": .03, "green": .01}))

    def test_run_summary_contains_time_and_pickup_totals(self):
        text = runner.run_summary(125, {"orange": 3, "pink": 2, "green": 1})
        self.assertIn("Gesamtzeit 02:05", text)
        self.assertIn("erkannt angefahren: 6", text)
        self.assertIn("Energie 3", text)

    def test_run_summary_reports_exact_orange_hud_difference(self):
        text = runner.run_summary(125, {"orange": 3, "pink": 2, "green": 1}, 7180, 7525)
        self.assertIn("Energie 7.180 -> 7.525 (+345)", text)
        self.assertIn("165,6/Min", text)
        self.assertIn("9.936,0/Std", text)
    def test_compact_progress_includes_percent_and_eta(self):
        text = runner.progress_summary(20, 200, 100)
        self.assertIn("20/200 (10%)", text)
        self.assertIn("01:40", text)
        self.assertIn("15:00", text)
    def test_debug_status_prioritizes_visible_energy(self):
        text = runner.plan_status(
            "move", "right", "orange priority targets=[(1, 4)]", 2
        )
        self.assertIn("Chip arancione prioritario", text)
        self.assertIn("2 pickup", text)

    def test_debug_status_describes_exploration_direction(self):
        text = runner.plan_status("move", "right", "explore right", 0)
        self.assertIn("Erkunde nach rechts", text)

    def test_followup_stops_before_pyramid(self):
        info = empty_grid()
        info[(2, 4)]["pyramid"] = 0.25
        moves = runner.safe_followup_moves(
            info, (2, 1), (2, 2), "right", 2, set()
        )
        self.assertEqual(moves, [((2, 2), (2, 3))])

    def test_followup_never_extends_a_vertical_dijkstra_detour(self):
        info = empty_grid()
        moves = runner.safe_followup_moves(
            info, (2, 1), (1, 1), "up", 2, set()
        )
        self.assertEqual(moves, [])

    def test_dash_scene_signature_detects_even_one_unchanged_blocker(self):
        info = empty_grid()
        info[(2, 2)]["pyramid"] = .25
        before = runner.dash_scene_signature(info, (2, 1))
        self.assertEqual(before, runner.dash_scene_signature(info, (2, 1)))
        info[(2, 2)]["pyramid"] = 0
        self.assertNotEqual(before, runner.dash_scene_signature(info, (2, 1)))

    def test_vertical_move_has_a_verifiable_expected_player_cell(self):
        expected = runner.expected_player_after_moves(
            (0, 1), [(1, 1)], "down"
        )
        self.assertEqual(expected, (1, 1))
        self.assertTrue(runner.move_left_player_unchanged(
            (0, 1), expected, (0, 1)
        ))
        self.assertFalse(runner.move_left_player_unchanged(
            (0, 1), expected, (1, 1)
        ))

    def test_scrolling_right_move_is_not_mistaken_for_a_stall(self):
        expected = runner.expected_player_after_moves(
            (2, 1), [(2, 2)], "right"
        )
        self.assertEqual(expected, (2, 1))
        self.assertFalse(runner.move_left_player_unchanged(
            (2, 1), expected, (2, 1)
        ))


if __name__ == "__main__":
    unittest.main()
