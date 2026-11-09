import datetime as dt
import io
import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stdout

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))
import ai_vacation as av  # noqa: E402

UTC = dt.timezone.utc
NOW = dt.datetime(2026, 11, 9, 12, 0, tzinfo=UTC)
RESET = dt.datetime(2026, 11, 12, 9, 0, tzinfo=UTC)
CFG = dict(av.DEFAULT_CONFIG)


def reading(pct, reset=RESET, prov="claude"):
    return {"provider": prov, "used_percent": pct, "resets_at": reset, "source": "t", "observed_at": NOW}


def done(task):
    """에이전트가 MCP로 처리하고 ack 한 것처럼."""
    return av.apply_ack({}, task["key"], NOW, event_id="ev1", end=task["end"])


class PlanTest(unittest.TestCase):
    def test_below_threshold_nothing(self):
        self.assertIsNone(av.plan(reading(94.9), {}, CFG, NOW))

    def test_create_until_reset(self):
        t = av.plan(reading(95), {}, CFG, NOW)
        self.assertEqual(t["action"], "create")
        self.assertEqual((t["start"], t["end"]), ("2026-11-09T12:00:00Z", "2026-11-12T09:00:00Z"))

    def test_after_ack_nothing_more(self):
        st = done(av.plan(reading(96), {}, CFG, NOW))
        for pct in (97, 99, 100):
            self.assertIsNone(av.plan(reading(pct), st, CFG, NOW))

    def test_reset_moved_updates_end(self):
        st = done(av.plan(reading(96), {}, CFG, NOW))
        t = av.plan(reading(96, RESET + dt.timedelta(minutes=40)), st, CFG, NOW)
        self.assertEqual((t["action"], t["event_id"]), ("update_end", "ev1"))
        self.assertEqual(t["key"], st["key"])

    def test_small_jitter_ignored(self):
        st = done(av.plan(reading(96), {}, CFG, NOW))
        self.assertIsNone(av.plan(reading(96, RESET + dt.timedelta(seconds=90)), st, CFG, NOW))

    def test_dismissed_respected(self):
        t = av.plan(reading(96), {}, CFG, NOW)
        st = av.apply_ack(done(t), t["key"], NOW, dismissed=True)
        self.assertIsNone(av.plan(reading(99), st, CFG, NOW))

    def test_early_reset_ends_now(self):
        st = done(av.plan(reading(96), {}, CFG, NOW))
        later = NOW + dt.timedelta(hours=5)
        t = av.plan(reading(2, RESET + dt.timedelta(days=7)), st, CFG, later)
        self.assertEqual((t["action"], t["end"]), ("end_now", av.iso(later)))
        st = av.apply_ack(st, t["key"], later, ended=True)
        self.assertIsNone(av.plan(reading(2, RESET + dt.timedelta(days=7)), st, CFG, later))

    def test_next_week_exhausted_creates_new(self):
        st = done(av.plan(reading(96), {}, CFG, NOW))
        nxt = NOW + dt.timedelta(days=8)
        t = av.plan(reading(97, RESET + dt.timedelta(days=7)), st, CFG, nxt)
        self.assertEqual(t["action"], "create")
        self.assertNotEqual(t["key"], st["key"])

    def test_past_reset_ignored(self):
        self.assertIsNone(av.plan(reading(100, NOW - dt.timedelta(minutes=1)), {}, CFG, NOW))

    def test_skip_snoozes(self):
        t = av.plan(reading(96), {}, CFG, NOW)
        st = av.apply_ack({}, t["key"], NOW, skip=True)
        self.assertIsNone(av.plan(reading(96), st, CFG, NOW + dt.timedelta(hours=23)))
        self.assertIsNotNone(av.plan(reading(96), st, CFG, NOW + dt.timedelta(hours=25)))

    def test_no_repeat_request(self):
        t = av.plan(reading(96), {}, CFG, NOW)
        st = av.mark_requested(t, {}, NOW)
        self.assertFalse(av.should_request(t, st, NOW + dt.timedelta(minutes=10)))
        self.assertTrue(av.should_request(t, st, NOW + dt.timedelta(minutes=31)))

    def test_instruction_contains_ack_and_marker(self):
        for t in (av.plan(reading(96), {}, CFG, NOW),
                  dict(av.plan(reading(96), {}, CFG, NOW), action="end_now", event_id="ev1")):
            text = av.instruction(t, CFG)
            self.assertIn("[ai-vacation:%s]" % t["key"], text)
            self.assertIn("ack --key %s" % t["key"], text)
            self.assertIn("--skip", text)


class HookTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old = (av.STATE_PATH, av.LOCK_PATH, av.LOG_PATH, av.CONFIG_PATH, av.APP_DIR, dict(av.READERS))
        av.APP_DIR = self.tmp.name
        av.STATE_PATH = os.path.join(self.tmp.name, "state.json")
        av.LOCK_PATH = os.path.join(self.tmp.name, "lock")
        av.LOG_PATH = os.path.join(self.tmp.name, "log")
        av.CONFIG_PATH = os.path.join(self.tmp.name, "config.json")
        self.pct = 96.0
        reset = av.now_utc() + dt.timedelta(days=2)
        av.READERS["claude"] = lambda: reading(self.pct, reset)
        av.READERS["codex"] = lambda: None

    def tearDown(self):
        (av.STATE_PATH, av.LOCK_PATH, av.LOG_PATH, av.CONFIG_PATH, av.APP_DIR, readers) = self.old
        av.READERS.clear()
        av.READERS.update(readers)
        self.tmp.cleanup()

    def run_hook(self, payload):
        old_stdin = sys.stdin
        sys.stdin = io.StringIO(json.dumps(payload))
        buf = io.StringIO()
        try:
            with redirect_stdout(buf):
                av.cmd_hook()
        finally:
            sys.stdin = old_stdin
        return json.loads(buf.getvalue())

    def test_block_once_then_quiet(self):
        out = self.run_hook({"hook_event_name": "Stop"})
        self.assertEqual(out["decision"], "block")
        self.assertIn("Google Calendar MCP", out["reason"])
        # 다른 도구의 훅이 곧바로 돌아도 같은 지시를 반복하지 않는다
        self.assertEqual(self.run_hook({"hook_event_name": "Stop"}), {})

    def test_never_blocks_inside_continuation(self):
        self.assertEqual(self.run_hook({"stop_hook_active": True}), {})

    def test_below_threshold_quiet(self):
        self.pct = 50
        self.assertEqual(self.run_hook({}), {})

    def test_ack_cli_stops_requests(self):
        out = self.run_hook({})
        key = out["reason"].split("ack --key ")[1].split()[0]
        with redirect_stdout(io.StringIO()):
            av.cmd_ack(["--key", key, "--event-id", "ev9", "--end", "2030-01-01T00:00:00Z"])
        st = av.read_json(av.STATE_PATH)["claude"]
        self.assertEqual(st["event_id"], "ev9")
        self.assertNotIn("pending", st)


class CodexParseTest(unittest.TestCase):
    def line(self, rl, ts="2026-11-09T12:00:00.000Z"):
        return json.dumps({"timestamp": ts, "type": "event_msg",
                           "payload": {"type": "token_count", "info": None, "rate_limits": rl}})

    def test_resets_at_epoch(self):
        r = av.parse_codex_line(self.line({
            "primary": {"used_percent": 10, "window_minutes": 300, "resets_at": 1762700000},
            "secondary": {"used_percent": 96.5, "window_minutes": 10080, "resets_at": 1762950000},
        }))
        self.assertEqual(r["used_percent"], 96.5)
        self.assertEqual(r["resets_at"], dt.datetime.fromtimestamp(1762950000, UTC))

    def test_resets_in_seconds_relative_to_event(self):
        r = av.parse_codex_line(self.line({
            "primary": {"used_percent": 1, "window_minutes": 299, "resets_in_seconds": 100},
            "secondary": {"used_percent": 97, "window_minutes": 10080, "resets_in_seconds": 3600},
        }))
        self.assertEqual(r["resets_at"], NOW + dt.timedelta(hours=1))

    def test_irrelevant_line(self):
        self.assertIsNone(av.parse_codex_line('{"type":"response_item"}'))

    def test_read_codex_picks_latest(self):
        with tempfile.TemporaryDirectory() as d:
            sess = os.path.join(d, "sessions", "2026", "11", "09")
            os.makedirs(sess)
            with open(os.path.join(sess, "rollout-a.jsonl"), "w") as f:
                f.write(self.line({"secondary": {"used_percent": 40, "window_minutes": 10080,
                                                 "resets_at": 1762950000}}) + "\n")
                f.write(self.line({"secondary": {"used_percent": 95, "window_minutes": 10080,
                                                 "resets_at": 1762950000}}, ts="2026-11-09T13:00:00Z") + "\n")
            old = os.environ.get("CODEX_HOME")
            os.environ["CODEX_HOME"] = d
            try:
                r = av.read_codex()
            finally:
                if old is None:
                    os.environ.pop("CODEX_HOME")
                else:
                    os.environ["CODEX_HOME"] = old
            self.assertEqual(r["used_percent"], 95)


class TimeParseTest(unittest.TestCase):
    def test_formats(self):
        self.assertEqual(av.parse_time("2026-11-12T09:00:00Z"), RESET)
        self.assertEqual(av.parse_time(RESET.timestamp()), RESET)
        self.assertEqual(av.parse_time(RESET.timestamp() * 1000), RESET)
        self.assertIsNone(av.parse_time("garbage"))


if __name__ == "__main__":
    unittest.main()
