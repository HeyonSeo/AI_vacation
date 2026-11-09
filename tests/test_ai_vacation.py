import datetime as dt
import json
import os
import sys
import tempfile
import unittest
import urllib.error

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))
import ai_vacation as av  # noqa: E402

UTC = dt.timezone.utc
NOW = dt.datetime(2026, 11, 9, 12, 0, tzinfo=UTC)
RESET = dt.datetime(2026, 11, 12, 9, 0, tzinfo=UTC)
CFG = dict(av.DEFAULT_CONFIG)


class FakeCal:
    def __init__(self, ooo_allowed=True):
        self.events, self.n, self.ooo_allowed, self.created_bodies = {}, 0, ooo_allowed, []

    def find(self, key):
        for e in self.events.values():
            if e["key"] == key:
                return e
        return None

    def get(self, eid):
        return self.events.get(eid)

    def create(self, reading, key, start):
        self.n += 1
        e = {"id": "e%d" % self.n, "key": key, "start": start,
             "end": {"dateTime": av.iso(reading["resets_at"])}}
        self.events[e["id"]] = e
        return e

    def set_end(self, eid, end):
        self.events[eid]["end"] = {"dateTime": av.iso(end)}


def reading(pct, reset=RESET, prov="claude"):
    return {"provider": prov, "used_percent": pct, "resets_at": reset, "source": "t", "observed_at": NOW}


class ReconcileTest(unittest.TestCase):
    def test_below_threshold_does_nothing(self):
        cal = FakeCal()
        st, act = av.reconcile(reading(94.9), {}, cal, CFG, NOW)
        self.assertEqual(act, "none")
        self.assertEqual(cal.events, {})

    def test_creates_once_until_reset(self):
        cal = FakeCal()
        st, act = av.reconcile(reading(95), {}, cal, CFG, NOW)
        self.assertEqual(act, "created")
        e = cal.events[st["event_id"]]
        self.assertEqual(e["start"], NOW)
        self.assertEqual(e["end"]["dateTime"], "2026-11-12T09:00:00Z")
        # 이후 훅이 여러 번 돌아도 중복 생성 없음
        for _ in range(3):
            st, act = av.reconcile(reading(99), st, cal, CFG, NOW)
            self.assertEqual(act, "unchanged")
        self.assertEqual(len(cal.events), 1)

    def test_no_duplicate_when_state_lost(self):
        cal = FakeCal()
        av.reconcile(reading(96), {}, cal, CFG, NOW)
        st, act = av.reconcile(reading(97), {}, cal, CFG, NOW)  # 다른 도구/상태 유실
        self.assertEqual(act, "unchanged")
        self.assertEqual(len(cal.events), 1)

    def test_small_reset_jitter_same_window(self):
        cal = FakeCal()
        st, _ = av.reconcile(reading(96), {}, cal, CFG, NOW)
        st, act = av.reconcile(reading(96, RESET + dt.timedelta(minutes=10)), st, cal, CFG, NOW)
        self.assertEqual(act, "extended")
        self.assertEqual(len(cal.events), 1)

    def test_jitter_across_hour_boundary(self):
        cal = FakeCal()
        st, _ = av.reconcile(reading(96, RESET - dt.timedelta(minutes=3)), {}, cal, CFG, NOW)
        st, act = av.reconcile(reading(96, RESET + dt.timedelta(minutes=3)), st, cal, CFG, NOW)
        self.assertEqual(act, "extended")
        self.assertEqual(len(cal.events), 1)

    def test_user_deleted_event_is_respected(self):
        cal = FakeCal()
        st, _ = av.reconcile(reading(96), {}, cal, CFG, NOW)
        cal.events.clear()
        st, act = av.reconcile(reading(98), st, cal, CFG, NOW)
        self.assertEqual(act, "dismissed")
        st, act = av.reconcile(reading(99), st, cal, CFG, NOW)
        self.assertEqual(act, "dismissed")
        self.assertEqual(cal.events, {})

    def test_early_reset_shortens_event(self):
        cal = FakeCal()
        st, _ = av.reconcile(reading(96), {}, cal, CFG, NOW)
        later = NOW + dt.timedelta(hours=5)
        new_window = reading(3, RESET + dt.timedelta(days=7))
        st, act = av.reconcile(new_window, st, cal, CFG, later)
        self.assertEqual(act, "ended-early")
        self.assertEqual(list(cal.events.values())[0]["end"]["dateTime"], av.iso(later))

    def test_already_reset_reading_ignored(self):
        cal = FakeCal()
        st, act = av.reconcile(reading(100, NOW - dt.timedelta(minutes=1)), {}, cal, CFG, NOW)
        self.assertEqual(cal.events, {})


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

    def test_weekly_found_in_primary(self):
        r = av.parse_codex_line(self.line({
            "primary": {"used_percent": 50, "window_minutes": 10080, "resets_at": 1762950000},
        }))
        self.assertEqual(r["used_percent"], 50)

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
        a = av.parse_time("2026-11-12T09:00:00Z")
        self.assertEqual(a, RESET)
        self.assertEqual(av.parse_time("2026-11-12T09:00:00.123456+00:00").replace(microsecond=0), RESET)
        self.assertEqual(av.parse_time(RESET.timestamp()), RESET)
        self.assertEqual(av.parse_time(RESET.timestamp() * 1000), RESET)
        self.assertIsNone(av.parse_time("garbage"))


if __name__ == "__main__":
    unittest.main()
