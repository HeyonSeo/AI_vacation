#!/usr/bin/env python3
"""AI Vacation — 주간 한도를 다 쓰면 리셋 시각까지 Google 캘린더에 휴가를 표시한다.

캘린더 쓰기는 에이전트가 이미 연결된 Google Calendar MCP로 한다. 이 스크립트는
사용량을 읽고 "캘린더에 지금 무엇을 해야 하는지"를 판단해 에이전트에게 지시하고,
에이전트가 처리한 결과(ack)를 기록한다. 표준 라이브러리만 사용한다.

명령:
  hook                 Stop 훅 진입점. 할 일이 있으면 에이전트에게 처리 지시(block)를 낸다.
  plan [--force]       지금 필요한 캘린더 작업을 JSON으로 출력(없으면 null).
  ack ...              에이전트가 MCP로 처리한 결과를 기록.
                         --key K --event-id ID --end ISO   생성/수정 완료
                         --key K --ended                    조기 종료 완료
                         --key K --dismissed                사용자가 일정을 지워 둠(이번 주 재생성 안 함)
                         --key K --skip                     캘린더 MCP 없음 등(24시간 보류)
  status               사용량, 기록된 휴가 일정, 대기 중 작업.
  tap -- CMD...        Claude Code statusLine 래퍼. rate_limits를 캐시하고 원래 CMD로 넘긴다.
  config [KEY VALUE]   설정 보기/변경.
"""

import datetime as dt
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request

APP_DIR = os.path.expanduser(os.environ.get("AI_VACATION_HOME", "~/.config/ai-vacation"))
CONFIG_PATH = os.path.join(APP_DIR, "config.json")
STATE_PATH = os.path.join(APP_DIR, "state.json")
TAP_CACHE_PATH = os.path.join(APP_DIR, "claude_statusline_cache.json")
LOCK_PATH = os.path.join(APP_DIR, "state.lock")
LOG_PATH = os.path.join(APP_DIR, "ai-vacation.log")
SCRIPT = os.path.abspath(__file__)

WEEK_MINUTES = 7 * 24 * 60
PROVIDER_LABEL = {"claude": "Claude", "codex": "Codex"}
END_TOLERANCE_S = 120          # 이 정도 차이는 같은 종료 시각으로 본다
SAME_WINDOW_S = 6 * 3600       # 리셋 시각이 이만큼 흔들려도 같은 주간 창
REREQUEST_AFTER_S = 30 * 60    # 지시했는데 처리 기록이 없으면 이 뒤에 다시 지시
SKIP_SNOOZE_S = 24 * 3600

DEFAULT_CONFIG = {
    "threshold_percent": 95,
    "calendar_id": "primary",
    "title": "🏖️ AI 휴가 ({provider} 주간 한도 소진)",
    "providers": ["claude", "codex"],
    "min_check_interval_seconds": 300,
}


# ---------------------------------------------------------------- utils

def now_utc():
    return dt.datetime.now(dt.timezone.utc)


def iso(t):
    return t.astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def local_iso(t):
    return t.astimezone().isoformat(timespec="seconds")


def parse_time(v):
    """epoch(초/밀리초) 또는 ISO 문자열 → aware datetime."""
    if v is None or v == "":
        return None
    if isinstance(v, (int, float)):
        x = float(v)
        if x > 1e12:
            x /= 1000.0
        return dt.datetime.fromtimestamp(x, dt.timezone.utc)
    if isinstance(v, str):
        s = v.strip()
        try:
            return parse_time(float(s))
        except ValueError:
            pass
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        try:
            t = dt.datetime.fromisoformat(s)
        except ValueError:
            return None
        return t if t.tzinfo else t.replace(tzinfo=dt.timezone.utc)
    return None


def ensure_dir():
    os.makedirs(APP_DIR, mode=0o700, exist_ok=True)


def read_json(path, default=None):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def write_json(path, data):
    ensure_dir()
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def log(msg):
    try:
        ensure_dir()
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write("%s %s\n" % (iso(now_utc()), msg))
        if os.path.getsize(LOG_PATH) > 512 * 1024:
            os.replace(LOG_PATH, LOG_PATH + ".1")
    except OSError:
        pass


def load_config():
    cfg = dict(DEFAULT_CONFIG)
    cfg.update(read_json(CONFIG_PATH, {}) or {})
    return cfg


class Lock:
    """Claude와 Codex 훅이 동시에 돌아도 같은 지시를 두 번 내지 않게 한다."""

    def __init__(self, path=None, wait=5.0, stale=60):
        self.path, self.wait, self.stale, self.ok = path or LOCK_PATH, wait, stale, False

    def __enter__(self):
        ensure_dir()
        deadline = time.time() + self.wait
        while True:
            try:
                fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
                os.close(fd)
                self.ok = True
                return self
            except FileExistsError:
                try:
                    if time.time() - os.path.getmtime(self.path) > self.stale:
                        os.remove(self.path)
                        continue
                except OSError:
                    continue
                if time.time() > deadline:
                    return self
                time.sleep(0.1)

    def __exit__(self, *a):
        if self.ok:
            try:
                os.remove(self.path)
            except OSError:
                pass


# ---------------------------------------------------------------- usage readers
# 반환: {"provider", "used_percent", "resets_at"(datetime), "source", "observed_at"(datetime)}

def _claude_access_token():
    tok = os.environ.get("CLAUDE_CODE_OAUTH_TOKEN")
    if tok:
        return tok
    cfg_dir = os.path.expanduser(os.environ.get("CLAUDE_CONFIG_DIR", "~/.claude"))
    data = read_json(os.path.join(cfg_dir, ".credentials.json"))
    if not data and sys.platform == "darwin":
        try:
            out = subprocess.run(
                ["security", "find-generic-password", "-s", "Claude Code-credentials", "-w"],
                capture_output=True, text=True, timeout=5,
            )
            data = json.loads(out.stdout) if out.returncode == 0 else None
        except (OSError, ValueError, subprocess.SubprocessError):
            data = None
    return ((data or {}).get("claudeAiOauth") or {}).get("accessToken")


def read_claude_from_tap(max_age=900):
    """statusLine 래퍼(tap)가 남긴 공식 rate_limits 캐시."""
    c = read_json(TAP_CACHE_PATH) or {}
    observed = parse_time(c.get("observed_at"))
    sd = (c.get("rate_limits") or {}).get("seven_day") or {}
    if not observed or (now_utc() - observed).total_seconds() > max_age:
        return None
    pct, reset = sd.get("used_percentage"), parse_time(sd.get("resets_at"))
    if pct is None or reset is None:
        return None
    return {"provider": "claude", "used_percent": float(pct), "resets_at": reset,
            "source": "statusline", "observed_at": observed}


def read_claude_from_api():
    token = _claude_access_token()
    if not token:
        return None
    req = urllib.request.Request("https://api.anthropic.com/api/oauth/usage", headers={
        "Authorization": "Bearer " + token,
        "anthropic-beta": "oauth-2025-04-20",
        "User-Agent": "ai-vacation",
    })
    try:
        with urllib.request.urlopen(req, timeout=6) as r:
            data = json.loads(r.read() or b"{}")
    except (urllib.error.URLError, OSError, ValueError) as e:
        log("claude usage api error: %s" % e)
        return None
    sd = data.get("seven_day") or {}
    pct = sd.get("utilization", sd.get("used_percentage"))
    reset = parse_time(sd.get("resets_at"))
    if pct is None or reset is None:
        return None
    return {"provider": "claude", "used_percent": float(pct), "resets_at": reset,
            "source": "oauth-usage-api", "observed_at": now_utc()}


def read_claude():
    return read_claude_from_tap() or read_claude_from_api()


def _find_rate_limits(obj):
    if isinstance(obj, dict):
        if isinstance(obj.get("rate_limits"), dict):
            return obj["rate_limits"]
        for v in obj.values():
            r = _find_rate_limits(v)
            if r:
                return r
    return None


def parse_codex_line(line):
    """Codex rollout JSONL 한 줄에서 주간(7일) 창을 뽑는다."""
    if "rate_limits" not in line:
        return None
    try:
        ev = json.loads(line)
    except ValueError:
        return None
    rl = _find_rate_limits(ev)
    if not rl:
        return None
    observed = parse_time(ev.get("timestamp")) or now_utc()
    weekly = None
    for w in (rl.get("secondary"), rl.get("primary")):
        if isinstance(w, dict) and int(w.get("window_minutes") or 0) == WEEK_MINUTES:
            weekly = w
            break
    if weekly is None and isinstance(rl.get("secondary"), dict) and not rl["secondary"].get("window_minutes"):
        weekly = rl["secondary"]  # 구버전: window_minutes 없이 secondary가 주간
    if weekly is None or weekly.get("used_percent") is None:
        return None
    reset = parse_time(weekly.get("resets_at"))
    if reset is None and weekly.get("resets_in_seconds") is not None:
        reset = observed + dt.timedelta(seconds=float(weekly["resets_in_seconds"]))
    if reset is None:
        return None
    return {"provider": "codex", "used_percent": float(weekly["used_percent"]),
            "resets_at": reset, "source": "codex-rollout", "observed_at": observed}


def read_codex(max_files=8):
    root = os.path.join(os.path.expanduser(os.environ.get("CODEX_HOME", "~/.codex")), "sessions")
    if not os.path.isdir(root):
        return None
    files = []
    for dp, _, fns in os.walk(root):
        for fn in fns:
            if fn.endswith(".jsonl"):
                p = os.path.join(dp, fn)
                try:
                    files.append((os.path.getmtime(p), p))
                except OSError:
                    pass
    best = None
    for _, p in sorted(files, reverse=True)[:max_files]:
        try:
            with open(p, encoding="utf-8", errors="replace") as f:
                lines = f.readlines()
        except OSError:
            continue
        for line in reversed(lines):
            r = parse_codex_line(line)
            if r:
                if best is None or r["observed_at"] > best["observed_at"]:
                    best = r
                break
    return best


READERS = {"claude": read_claude, "codex": read_codex}


# ---------------------------------------------------------------- planning (순수 함수)

def window_key(reading, pstate=None):
    """주간 창 식별자. 직전 기록과 리셋 시각이 가까우면 같은 창으로 본다."""
    prev_end = parse_time((pstate or {}).get("end"))
    if (pstate or {}).get("key") and prev_end and \
            abs((prev_end - reading["resets_at"]).total_seconds()) < SAME_WINDOW_S:
        return pstate["key"]
    r = reading["resets_at"].astimezone(dt.timezone.utc)
    return "%s-%s" % (reading["provider"], r.strftime("%Y%m%dT%H"))


def plan(reading, pstate, cfg, now):
    """지금 캘린더에 필요한 작업 하나(또는 None)."""
    pstate = pstate or {}
    key = window_key(reading, pstate)
    snooze = parse_time(pstate.get("snooze_until"))
    if snooze and snooze > now and pstate.get("snooze_key") == key:
        return None
    exhausted = reading["used_percent"] >= float(cfg["threshold_percent"]) and reading["resets_at"] > now
    same = pstate.get("key") == key
    base = {"provider": reading["provider"], "key": key,
            "used_percent": reading["used_percent"], "resets_at": iso(reading["resets_at"])}

    if exhausted:
        if same and pstate.get("dismissed"):
            return None
        if same and pstate.get("event_id"):
            end = parse_time(pstate.get("end"))
            if end and abs((end - reading["resets_at"]).total_seconds()) <= END_TOLERANCE_S:
                return None
            return dict(base, action="update_end", event_id=pstate["event_id"], end=iso(reading["resets_at"]))
        return dict(base, action="create", start=iso(now), end=iso(reading["resets_at"]))

    # 한도 미만: 새 주간 창인데(조기 리셋 등) 이전 휴가가 아직 끝나지 않았으면 지금 끝낸다
    end = parse_time(pstate.get("end"))
    if pstate.get("event_id") and not same and end and end > now:
        return dict(base, action="end_now", key=pstate["key"], event_id=pstate["event_id"], end=iso(now))
    return None


def should_request(task, pstate, now):
    """같은 지시를 반복해서 내지 않는다(다른 도구의 훅 포함)."""
    p = (pstate or {}).get("pending") or {}
    at = parse_time(p.get("requested_at"))
    sig = "%s|%s|%s" % (task["action"], task["key"], task.get("end"))
    if p.get("sig") == sig and at and (now - at).total_seconds() < REREQUEST_AFTER_S:
        return False
    return True


def mark_requested(task, pstate, now):
    pstate = dict(pstate or {})
    pstate["pending"] = {"sig": "%s|%s|%s" % (task["action"], task["key"], task.get("end")),
                         "requested_at": iso(now)}
    return pstate


def apply_ack(pstate, key, now, event_id=None, end=None, ended=False, dismissed=False, skip=False):
    pstate = dict(pstate or {})
    pstate.pop("pending", None)
    if skip:
        pstate.update(snooze_key=key, snooze_until=iso(now + dt.timedelta(seconds=SKIP_SNOOZE_S)))
    elif dismissed:
        pstate.update(key=key, dismissed=True)
        pstate.pop("event_id", None)
    elif ended:
        for k in ("key", "event_id", "end", "dismissed"):
            pstate.pop(k, None)
    else:
        pstate.update(key=key, event_id=event_id, end=end, dismissed=False)
    return pstate


# ---------------------------------------------------------------- instructions for the agent

def instruction(task, cfg):
    label = PROVIDER_LABEL.get(task["provider"], task["provider"])
    title = cfg["title"].format(provider=label)
    marker = "[ai-vacation:%s]" % task["key"]
    ack = 'python3 "%s" ack --key %s' % (SCRIPT, task["key"])
    reset_local = parse_time(task["resets_at"]).astimezone().strftime("%m/%d %H:%M")
    head = ("[AI Vacation] %s 주간 사용량 %.0f%% (리셋 %s). 사용자 요청과 별개인 자동 작업이다. "
            "연결된 Google Calendar MCP 도구로 아래를 처리하고, 끝나면 사용자에게 한 줄로만 알려라.\n"
            % (label, task["used_percent"], reset_local))
    no_mcp = ("\nGoogle Calendar MCP 도구가 없으면 일정을 만들지 말고, 사용자에게 'Google Calendar MCP를 연결하면 "
              "주간 한도 소진 시 휴가가 자동 표시된다'고 한 줄 알린 뒤 `%s --skip` 을 실행하라." % ack)
    if task["action"] == "create":
        return head + (
            "1) 캘린더 `%s`에서 `%s` 문자열이 들어간 일정을 검색한다(중복 방지).\n"
            "   - 있으면 그 일정의 종료 시각만 %s 로 맞춘다.\n"
            "   - 없으면 새 일정을 만든다:\n"
            "     제목: %s\n"
            "     시작: %s\n"
            "     종료: %s\n"
            "     표시: 바쁨(busy). 참석자·알림 추가 금지.\n"
            "     설명: \"%s 주간 사용량이 %.0f%%에 도달해 리셋 시각까지 휴가로 표시합니다. %s\"\n"
            "2) 완료 후 실행: `%s --event-id <일정ID> --end %s`"
            % (cfg["calendar_id"], marker, local_iso(parse_time(task["end"])), title,
               local_iso(parse_time(task["start"])), local_iso(parse_time(task["end"])),
               label, task["used_percent"], marker, ack, task["end"])
        ) + no_mcp
    if task["action"] == "update_end":
        return head + (
            "1) 캘린더 `%s`의 일정 ID `%s`(설명에 `%s`)의 종료 시각을 %s 로 바꾼다.\n"
            "2) 완료 후 실행: `%s --event-id %s --end %s`\n"
            "   일정이 없으면(사용자가 지움) 다시 만들지 말고 `%s --dismissed` 실행."
            % (cfg["calendar_id"], task["event_id"], marker, local_iso(parse_time(task["end"])),
               ack, task["event_id"], task["end"], ack)
        ) + no_mcp
    return head + (
        "한도가 예정보다 일찍 리셋됐다.\n"
        "1) 캘린더 `%s`의 일정 ID `%s`(설명에 `%s`)의 종료 시각을 지금(%s)으로 바꿔 휴가를 끝낸다.\n"
        "2) 완료 후(또는 일정이 이미 없으면) 실행: `%s --ended`"
        % (cfg["calendar_id"], task["event_id"], marker, local_iso(parse_time(task["end"])), ack)
    ) + no_mcp


# ---------------------------------------------------------------- orchestration

def collect_tasks(force=False, mark=False, now=None):
    """설정된 도구들의 사용량을 확인하고 필요한 작업 목록을 만든다."""
    cfg = load_config()
    now = now or now_utc()
    tasks = []
    with Lock() as lk:
        state = read_json(STATE_PATH, {}) or {}
        for prov in cfg["providers"]:
            ps = state.get(prov) or {}
            last = parse_time(ps.get("last_check"))
            reading = None
            if force or not last or (now - last).total_seconds() >= cfg["min_check_interval_seconds"]:
                reader = READERS.get(prov)
                reading = reader() if reader else None
                ps["last_check"] = iso(now)
                if reading:
                    ps["last_reading"] = {"used_percent": reading["used_percent"],
                                          "resets_at": iso(reading["resets_at"]), "source": reading["source"]}
            if reading is None and ps.get("last_reading"):
                lr = ps["last_reading"]  # 확인 주기 사이에는 직전 값으로 판단
                reading = {"provider": prov, "used_percent": lr["used_percent"],
                           "resets_at": parse_time(lr["resets_at"]), "source": lr.get("source")}
            task = plan(reading, ps, cfg, now) if reading and reading.get("resets_at") else None
            if task and (not mark or should_request(task, ps, now)):
                tasks.append(task)
                if mark:
                    ps = mark_requested(task, ps, now)
            state[prov] = ps
        if lk.ok:
            write_json(STATE_PATH, state)
    return tasks, cfg


def cmd_hook():
    raw = ""
    try:
        if not sys.stdin.isatty():
            raw = sys.stdin.read()
    except OSError:
        pass
    try:
        payload = json.loads(raw) if raw.strip() else {}
    except ValueError:
        payload = {}
    out = {}
    # 훅이 만든 이어하기 안에서는 다시 막지 않는다(무한 루프 방지)
    if not payload.get("stop_hook_active"):
        try:
            tasks, cfg = collect_tasks(mark=True)
        except Exception as e:  # 훅은 어떤 경우에도 작업을 망치지 않는다
            log("hook error: %r" % e)
            tasks = []
        if tasks:
            out = {"decision": "block", "reason": "\n\n".join(instruction(t, cfg) for t in tasks)}
            for t in tasks:
                log("requested %s %s" % (t["action"], t["key"]))
    sys.stdout.write(json.dumps(out, ensure_ascii=False) + "\n")
    return 0


def cmd_plan(args):
    tasks, cfg = collect_tasks(force="--force" in args)
    if "--text" in args:
        print("\n\n".join(instruction(t, cfg) for t in tasks) if tasks else "할 일 없음")
    else:
        print(json.dumps(tasks or None, ensure_ascii=False, indent=2))
    return 0


def _opt(args, name):
    if name in args:
        i = args.index(name)
        return args[i + 1] if i + 1 < len(args) else None
    return None


def cmd_ack(args):
    key = _opt(args, "--key")
    if not key or "-" not in key:
        print("--key 가 필요합니다.")
        return 2
    prov = key.split("-", 1)[0]
    event_id, end = _opt(args, "--event-id"), _opt(args, "--end")
    ended, dismissed, skip = "--ended" in args, "--dismissed" in args, "--skip" in args
    if not (ended or dismissed or skip) and not (event_id and parse_time(end)):
        print("--event-id 와 --end 가 필요합니다(또는 --ended / --dismissed / --skip).")
        return 2
    with Lock() as lk:
        state = read_json(STATE_PATH, {}) or {}
        state[prov] = apply_ack(state.get(prov), key, now_utc(), event_id=event_id,
                                end=iso(parse_time(end)) if end else None,
                                ended=ended, dismissed=dismissed, skip=skip)
        write_json(STATE_PATH, state)
    log("ack %s %s" % (key, "ended" if ended else "dismissed" if dismissed else "skip" if skip else event_id))
    print("기록했습니다.")
    return 0


def cmd_tap(argv):
    raw = sys.stdin.read() if not sys.stdin.isatty() else ""
    try:
        data = json.loads(raw) if raw else {}
        if isinstance(data.get("rate_limits"), dict):
            write_json(TAP_CACHE_PATH, {"observed_at": iso(now_utc()), "rate_limits": data["rate_limits"]})
    except (ValueError, OSError):
        pass
    if argv:
        return subprocess.run(argv, input=raw, text=True).returncode
    return 0


def cmd_status():
    cfg = load_config()
    state = read_json(STATE_PATH, {}) or {}
    print("휴가 기준: 주간 사용량 %s%% 이상 · 캘린더: %s (Google Calendar MCP로 기록)"
          % (cfg["threshold_percent"], cfg["calendar_id"]))
    for prov in cfg["providers"]:
        r = READERS[prov]()
        name = PROVIDER_LABEL.get(prov, prov)
        if r is None:
            print("- %s: 사용량을 읽을 수 없음" % name)
        else:
            on = r["used_percent"] >= float(cfg["threshold_percent"]) and r["resets_at"] > now_utc()
            print("- %s: %.1f%% (리셋 %s, 출처 %s)%s" % (
                name, r["used_percent"], r["resets_at"].astimezone().strftime("%m/%d %H:%M"),
                r["source"], "  ← 휴가 대상" if on else ""))
        ps = state.get(prov) or {}
        if ps.get("event_id"):
            print("    기록된 휴가 일정: %s (종료 %s)" % (ps["event_id"], ps.get("end")))
        if ps.get("dismissed"):
            print("    이번 주 휴가 일정은 사용자가 삭제함 → 다시 만들지 않음")
        if ps.get("pending"):
            print("    처리 대기: %s" % ps["pending"]["sig"])
    return 0


def cmd_config(args):
    cfg = read_json(CONFIG_PATH, {}) or {}
    if len(args) >= 2:
        k, v = args[0], " ".join(args[1:])
        if k not in DEFAULT_CONFIG:
            print("알 수 없는 키: %s (가능: %s)" % (k, ", ".join(DEFAULT_CONFIG)))
            return 2
        d = DEFAULT_CONFIG[k]
        if isinstance(d, list):
            v = [x.strip() for x in v.split(",") if x.strip()]
        elif isinstance(d, int):
            v = int(float(v))
        cfg[k] = v
        write_json(CONFIG_PATH, cfg)
    merged = dict(DEFAULT_CONFIG)
    merged.update(cfg)
    print(json.dumps(merged, ensure_ascii=False, indent=2))
    return 0


def main(argv):
    cmd, rest = (argv[0], argv[1:]) if argv else ("status", [])
    if cmd == "hook":
        return cmd_hook()
    if cmd == "plan":
        return cmd_plan(rest)
    if cmd == "ack":
        return cmd_ack(rest)
    if cmd == "status":
        return cmd_status()
    if cmd == "config":
        return cmd_config(rest)
    if cmd == "tap":
        return cmd_tap(rest[1:] if rest[:1] == ["--"] else rest)
    print(__doc__)
    return 2


if __name__ == "__main__":
    try:
        sys.exit(main(sys.argv[1:]))
    except KeyboardInterrupt:
        sys.exit(130)
