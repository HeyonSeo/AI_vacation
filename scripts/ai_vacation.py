#!/usr/bin/env python3
"""AI Vacation — 주간 한도를 다 쓰면 리셋 시각까지 Google 캘린더에 휴가를 표시한다.

Claude Code와 Codex 양쪽 플러그인 훅에서 같은 스크립트를 호출한다.
표준 라이브러리만 사용한다(추가 설치 없음).

명령:
  hook                 훅 진입점. stdin을 비우고 백그라운드로 check를 띄운 뒤 즉시 종료.
  check [--force]      사용량을 읽고 캘린더를 실제 상태에 맞춘다.
  status               현재 사용량, 휴가 일정, 설정 상태를 보여준다.
  setup --client-secret FILE   Google OAuth 연결(최초 1회).
  tap -- CMD...        Claude Code statusLine 래퍼. rate_limits를 캐시하고 원래 CMD로 넘긴다.
  config [KEY VALUE]   설정 보기/변경.
"""

import base64
import datetime as dt
import hashlib
import json
import os
import secrets
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer

APP_DIR = os.path.expanduser(os.environ.get("AI_VACATION_HOME", "~/.config/ai-vacation"))
CONFIG_PATH = os.path.join(APP_DIR, "config.json")
TOKEN_PATH = os.path.join(APP_DIR, "google_token.json")
STATE_PATH = os.path.join(APP_DIR, "state.json")
TAP_CACHE_PATH = os.path.join(APP_DIR, "claude_statusline_cache.json")
LOCK_PATH = os.path.join(APP_DIR, "check.lock")
LOG_PATH = os.path.join(APP_DIR, "ai-vacation.log")

WEEK_MINUTES = 7 * 24 * 60
CAL_SCOPE = "https://www.googleapis.com/auth/calendar.events"
CAL_API = "https://www.googleapis.com/calendar/v3"
PROVIDER_LABEL = {"claude": "Claude", "codex": "Codex"}

DEFAULT_CONFIG = {
    "threshold_percent": 95,
    "calendar_id": "primary",
    # "outOfOffice": 캘린더의 '부재중' 일정(Workspace 계정). 실패하면 일반 일정으로 대체.
    # "default": 일반 '바쁨' 일정.
    "event_type": "outOfOffice",
    # outOfOffice일 때 회의 초대 처리: declineNone | declineOnlyNewConversationInvitations | declineAllConflictingInvitations
    "auto_decline": "declineNone",
    "title": "🏖️ AI 휴가 ({provider} 주간 한도 소진)",
    "providers": ["claude", "codex"],
    "min_check_interval_seconds": 300,
}


# ---------------------------------------------------------------- utils

def now_utc():
    return dt.datetime.now(dt.timezone.utc)


def iso(t):
    return t.astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_time(v, base=None):
    """epoch(초/밀리초), ISO 문자열을 aware datetime으로."""
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
        if t.tzinfo is None:
            t = t.replace(tzinfo=dt.timezone.utc)
        return t
    return None


def ensure_dir():
    os.makedirs(APP_DIR, mode=0o700, exist_ok=True)


def read_json(path, default=None):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def write_json(path, data, private=False):
    ensure_dir()
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    if private:
        os.chmod(tmp, 0o600)
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


def http_json(method, url, headers=None, body=None, form=None, timeout=10):
    data = None
    headers = dict(headers or {})
    if form is not None:
        data = urllib.parse.urlencode(form).encode()
        headers["Content-Type"] = "application/x-www-form-urlencoded"
    elif body is not None:
        data = json.dumps(body).encode()
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        raw = r.read()
    return json.loads(raw) if raw else {}


class Lock:
    """Claude와 Codex 훅이 동시에 돌아도 일정이 두 번 생기지 않게 한다."""

    def __init__(self, path, stale=120):
        self.path, self.stale, self.ok = path, stale, False

    def __enter__(self):
        ensure_dir()
        for _ in range(2):
            try:
                fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
                os.write(fd, str(os.getpid()).encode())
                os.close(fd)
                self.ok = True
                break
            except FileExistsError:
                try:
                    if time.time() - os.path.getmtime(self.path) > self.stale:
                        os.remove(self.path)
                        continue
                except OSError:
                    continue
                break
        return self

    def __exit__(self, *a):
        if self.ok:
            try:
                os.remove(self.path)
            except OSError:
                pass


# ---------------------------------------------------------------- usage readers
# 반환 형식: {"provider", "used_percent", "resets_at"(datetime), "source", "observed_at"(datetime)}

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
    if not data:
        return None
    oauth = data.get("claudeAiOauth") or {}
    return oauth.get("accessToken")


def read_claude_from_tap(max_age=900):
    """statusLine 래퍼(tap)가 남긴 공식 rate_limits 캐시."""
    c = read_json(TAP_CACHE_PATH)
    if not c:
        return None
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
    try:
        data = http_json("GET", "https://api.anthropic.com/api/oauth/usage", headers={
            "Authorization": "Bearer " + token,
            "anthropic-beta": "oauth-2025-04-20",
            "User-Agent": "ai-vacation",
        }, timeout=8)
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
    """rollout JSONL 한 줄에서 주간(7일) 창을 뽑는다."""
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
    windows = [w for w in (rl.get("secondary"), rl.get("primary")) if isinstance(w, dict)]
    weekly = None
    for w in windows:
        if int(w.get("window_minutes") or 0) == WEEK_MINUTES:
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


# ---------------------------------------------------------------- Google Calendar

class Calendar:
    def __init__(self, cfg):
        self.cfg = cfg
        self.cal = urllib.parse.quote(cfg["calendar_id"], safe="")
        self._access = None

    @staticmethod
    def configured():
        t = read_json(TOKEN_PATH) or {}
        return bool(t.get("refresh_token") and t.get("client_id"))

    def _token(self):
        if self._access:
            return self._access
        t = read_json(TOKEN_PATH) or {}
        r = http_json("POST", "https://oauth2.googleapis.com/token", form={
            "client_id": t["client_id"], "client_secret": t.get("client_secret", ""),
            "refresh_token": t["refresh_token"], "grant_type": "refresh_token",
        })
        self._access = r["access_token"]
        return self._access

    def _call(self, method, path, body=None, query=None):
        url = "%s/calendars/%s%s" % (CAL_API, self.cal, path)
        if query:
            url += "?" + urllib.parse.urlencode(query)
        return http_json(method, url, headers={"Authorization": "Bearer " + self._token()}, body=body)

    def find(self, key):
        r = self._call("GET", "/events", query={
            "privateExtendedProperty": "aiVacationKey=" + key,
            "showDeleted": "false", "singleEvents": "true", "maxResults": 5,
        })
        items = [e for e in r.get("items", []) if e.get("status") != "cancelled"]
        return items[0] if items else None

    def get(self, event_id):
        try:
            e = self._call("GET", "/events/" + urllib.parse.quote(event_id, safe=""))
        except urllib.error.HTTPError as err:
            if err.code in (404, 410):
                return None
            raise
        return None if e.get("status") == "cancelled" else e

    def create(self, reading, key, start):
        cfg = self.cfg
        label = PROVIDER_LABEL.get(reading["provider"], reading["provider"])
        body = {
            "summary": cfg["title"].format(provider=label),
            "description": (
                "%s 주간 사용량이 %.0f%%에 도달했습니다.\n"
                "한도가 리셋되는 %s까지 휴가로 표시합니다.\n\n"
                "— AI Vacation 플러그인이 자동으로 만든 일정입니다."
                % (label, reading["used_percent"], reading["resets_at"].astimezone().strftime("%m/%d %H:%M"))
            ),
            "start": {"dateTime": iso(start)},
            "end": {"dateTime": iso(reading["resets_at"])},
            "transparency": "opaque",
            "extendedProperties": {"private": {"aiVacationKey": key, "aiVacationProvider": reading["provider"]}},
        }
        if cfg.get("event_type") == "outOfOffice":
            ooo = dict(body, eventType="outOfOffice", outOfOfficeProperties={
                "autoDeclineMode": cfg.get("auto_decline", "declineNone"),
                "declineMessage": "AI 휴가 중입니다. 한도 리셋 후 복귀합니다.",
            })
            ooo.pop("description", None)  # 부재중 일정은 설명 필드를 받지 않는다
            try:
                return self._call("POST", "/events", body=ooo)
            except urllib.error.HTTPError as e:
                # 개인 Gmail 계정 등은 부재중 일정을 만들 수 없다 → 일반 일정으로 대체
                log("outOfOffice not allowed (%s); falling back to default event" % e.code)
        return self._call("POST", "/events", body=body)

    def set_end(self, event_id, end):
        return self._call("PATCH", "/events/" + urllib.parse.quote(event_id, safe=""),
                          body={"end": {"dateTime": iso(end)}})


# ---------------------------------------------------------------- reconcile

def window_key(reading):
    # 같은 주간 창이면 리셋 시각이 몇 분 흔들려도 같은 키가 되도록 시 단위로 자른다
    r = reading["resets_at"].astimezone(dt.timezone.utc).replace(minute=0, second=0, microsecond=0)
    return "%s:%s" % (reading["provider"], r.strftime("%Y%m%dT%H"))


def reconcile(reading, pstate, cal, cfg, now):
    """캘린더를 실제 한도 상태에 맞춘다. 갱신된 provider 상태와 수행한 동작을 반환."""
    pstate = dict(pstate or {})
    key = window_key(reading)
    prev_end = parse_time(pstate.get("end"))
    if pstate.get("key") and prev_end and abs((prev_end - reading["resets_at"]).total_seconds()) < 6 * 3600:
        key = pstate["key"]  # 리셋 시각이 정각 경계를 넘어 흔들려도 같은 주간 창으로 본다
    exhausted = reading["used_percent"] >= float(cfg["threshold_percent"]) and reading["resets_at"] > now

    if exhausted:
        ev = None
        if pstate.get("key") == key and pstate.get("event_id"):
            ev = cal.get(pstate["event_id"])
            if ev is None:
                # 사용자가 일정을 직접 지웠다 → 그 결정을 존중하고 다시 만들지 않는다
                pstate["dismissed"] = True
                return pstate, "dismissed"
        if pstate.get("key") == key and pstate.get("dismissed"):
            return pstate, "dismissed"
        if ev is None:
            ev = cal.find(key)
        if ev is None:
            ev = cal.create(reading, key, start=now)
            action = "created"
        else:
            end = parse_time((ev.get("end") or {}).get("dateTime"))
            if end and abs((end - reading["resets_at"]).total_seconds()) > 120:
                cal.set_end(ev["id"], reading["resets_at"])
                action = "extended"
            else:
                action = "unchanged"
        pstate.update(key=key, event_id=ev["id"], end=iso(reading["resets_at"]), dismissed=False)
        return pstate, action

    # 한도 미만: 새 주간 창이 열렸는데(조기 리셋 등) 이전 휴가가 남아 있으면 지금으로 끝낸다
    end = parse_time(pstate.get("end"))
    if pstate.get("event_id") and pstate.get("key") != key and end and end > now:
        if cal.get(pstate["event_id"]) is not None:
            cal.set_end(pstate["event_id"], now)
            return {}, "ended-early"
        return {}, "cleared"
    return pstate, "none"


def run_check(force=False, quiet=True):
    cfg = load_config()
    if not Calendar.configured():
        if not quiet:
            print("Google 캘린더가 연결되지 않았습니다. `setup`을 먼저 실행하세요.")
        return 1
    with Lock(LOCK_PATH) as lk:
        if not lk.ok:
            return 0  # 다른 훅이 이미 처리 중
        state = read_json(STATE_PATH, {}) or {}
        cal = Calendar(cfg)
        now = now_utc()
        for prov in cfg["providers"]:
            ps = state.get(prov) or {}
            last = parse_time(ps.get("last_check"))
            if not force and last and (now - last).total_seconds() < cfg["min_check_interval_seconds"]:
                continue
            reader = READERS.get(prov)
            reading = reader() if reader else None
            ps["last_check"] = iso(now)
            if reading is None:
                state[prov] = ps
                continue
            ps["last_used_percent"] = reading["used_percent"]
            ps["last_resets_at"] = iso(reading["resets_at"])
            ps["last_source"] = reading["source"]
            try:
                new_ps, action = reconcile(reading, ps, cal, cfg, now)
            except (urllib.error.URLError, OSError, ValueError, KeyError) as e:
                log("%s reconcile error: %s" % (prov, e))
                state[prov] = ps
                continue
            for k in ("last_check", "last_used_percent", "last_resets_at", "last_source"):
                new_ps[k] = ps[k]
            state[prov] = new_ps
            if action not in ("none", "unchanged"):
                log("%s %.1f%% resets %s -> %s" % (prov, reading["used_percent"], iso(reading["resets_at"]), action))
            if not quiet:
                print("%s: %.1f%% (리셋 %s) → %s" % (
                    PROVIDER_LABEL.get(prov, prov), reading["used_percent"],
                    reading["resets_at"].astimezone().strftime("%m/%d %H:%M"), action))
        write_json(STATE_PATH, state)
    return 0


# ---------------------------------------------------------------- commands

def cmd_hook():
    # 훅은 사용자 작업을 절대 막지 않는다: stdin을 비우고 백그라운드로 넘긴 뒤 바로 끝낸다.
    try:
        if not sys.stdin.isatty():
            sys.stdin.read()
    except OSError:
        pass
    if Calendar.configured():
        kwargs = dict(stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        if os.name == "nt":
            kwargs["creationflags"] = 0x00000008  # DETACHED_PROCESS
        else:
            kwargs["start_new_session"] = True
        try:
            subprocess.Popen([sys.executable, os.path.abspath(__file__), "check"], **kwargs)
        except OSError as e:
            log("hook spawn error: %s" % e)
    sys.stdout.write("{}\n")
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
    print("Google 캘린더 연결: %s" % ("예" if Calendar.configured() else "아니오 (setup 필요)"))
    print("휴가 기준: 주간 사용량 %s%% 이상, 캘린더: %s" % (cfg["threshold_percent"], cfg["calendar_id"]))
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
            print("    캘린더 일정: %s (종료 %s)%s" % (ps["event_id"], ps.get("end"),
                                               " [사용자가 삭제함]" if ps.get("dismissed") else ""))
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


def cmd_setup(args):
    path = None
    if "--client-secret" in args:
        i = args.index("--client-secret")
        path = args[i + 1] if i + 1 < len(args) else None
    if not path:
        print(__doc__)
        print("사용법: ai_vacation.py setup --client-secret ~/Downloads/client_secret_XXX.json")
        return 2
    sec = read_json(os.path.expanduser(path)) or {}
    sec = sec.get("installed") or sec.get("web") or {}
    if not sec.get("client_id"):
        print("client secret 파일에서 client_id를 찾지 못했습니다(데스크톱 앱 유형 OAuth 클라이언트 필요).")
        return 2

    verifier = base64.urlsafe_b64encode(secrets.token_bytes(48)).rstrip(b"=").decode()
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    state = secrets.token_urlsafe(16)
    result = {}

    class H(BaseHTTPRequestHandler):
        def do_GET(self):
            q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            if q.get("state", [""])[0] == state:
                result["code"] = q.get("code", [None])[0]
                result["error"] = q.get("error", [None])[0]
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write("<h2>AI Vacation 연결 완료. 이 창을 닫아도 됩니다.</h2>".encode())

        def log_message(self, *a):
            pass

    srv = HTTPServer(("127.0.0.1", 0), H)
    redirect = "http://127.0.0.1:%d" % srv.server_port
    url = "https://accounts.google.com/o/oauth2/v2/auth?" + urllib.parse.urlencode({
        "client_id": sec["client_id"], "redirect_uri": redirect, "response_type": "code",
        "scope": CAL_SCOPE, "access_type": "offline", "prompt": "consent",
        "code_challenge": challenge, "code_challenge_method": "S256", "state": state,
    })
    print("브라우저에서 Google 계정을 연결하세요:\n%s\n" % url)
    try:
        import webbrowser
        webbrowser.open(url)
    except Exception:
        pass
    t = threading.Thread(target=lambda: [srv.handle_request() for _ in range(5) if "code" not in result])
    t.daemon = True
    t.start()
    t.join(300)
    srv.server_close()
    if not result.get("code"):
        print("인증이 완료되지 않았습니다: %s" % (result.get("error") or "시간 초과"))
        return 1
    tok = http_json("POST", "https://oauth2.googleapis.com/token", form={
        "client_id": sec["client_id"], "client_secret": sec.get("client_secret", ""),
        "code": result["code"], "code_verifier": verifier,
        "grant_type": "authorization_code", "redirect_uri": redirect,
    })
    if not tok.get("refresh_token"):
        print("refresh token을 받지 못했습니다. Google 계정의 앱 권한에서 이 앱을 제거한 뒤 다시 시도하세요.")
        return 1
    write_json(TOKEN_PATH, {"client_id": sec["client_id"], "client_secret": sec.get("client_secret", ""),
                            "refresh_token": tok["refresh_token"], "scope": CAL_SCOPE}, private=True)
    print("Google 캘린더 연결 완료.\n")
    cmd_status()
    return 0


def main(argv):
    cmd = argv[0] if argv else "status"
    rest = argv[1:]
    if cmd == "hook":
        return cmd_hook()
    if cmd == "check":
        return run_check(force="--force" in rest, quiet="--verbose" not in rest and "-v" not in rest)
    if cmd == "status":
        return cmd_status()
    if cmd == "setup":
        return cmd_setup(rest)
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
