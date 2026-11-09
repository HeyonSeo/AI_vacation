# AI Vacation 🏖️

Claude Code·Codex의 **주간 사용 한도를 95% 이상 쓰면, 한도가 리셋되는 시각까지 Google 캘린더에 휴가를 표시**하는 플러그인입니다.
한 번 연결해 두면 신경 쓸 일이 없습니다. 팀은 캘린더만 보고 "지금은 AI 없이 쉬는 중"임을 알 수 있습니다.

## 동작

- 세션 시작·응답 종료 시점(SessionStart, Stop 훅)에 **백그라운드로** 사용량을 확인합니다. 작업을 막거나 느리게 하지 않습니다(5분에 한 번만 실제 확인).
- 주간 사용량 ≥ 기준(기본 95%) → 지금부터 리셋 시각까지 휴가 일정 생성
  - Workspace 계정: 캘린더 고유의 **부재중(Out of office)** 일정
  - 개인 Gmail 등 부재중 일정이 불가한 계정: 일반 '바쁨' 일정으로 자동 대체
- 같은 주간 창에는 **일정이 한 번만** 생깁니다(Claude·Codex 훅이 동시에 돌아도, 상태 파일을 잃어도).
- 리셋 시각이 바뀌면 일정 종료 시각을 맞춰 고칩니다. 한도가 **일찍 리셋되면** 남은 휴가를 그 시각에 끝냅니다.
- 사용자가 캘린더에서 일정을 **직접 지우면** 그 주에는 다시 만들지 않습니다.
- 한 도구에만 설치해도 Claude·Codex 사용량을 **둘 다** 감시합니다(도구별로 각각 일정).

| 도구 | 사용량 출처 |
|---|---|
| Claude Code | ① statusLine `rate_limits.seven_day` (tap 사용 시, 공식) ② `api.anthropic.com/api/oauth/usage` (Claude Code 로그인 토큰 사용) |
| Codex | `~/.codex/sessions/**/rollout-*.jsonl`의 `token_count` 이벤트 `rate_limits` 중 7일(10080분) 창 |

## 설치

필요: `python3` (표준 라이브러리만 사용).

**Claude Code**
```
/plugin marketplace add heyonseo/AI_vacation
/plugin install ai-vacation@ai-vacation
```

**Codex** — 저장소를 마켓플레이스로 추가하고 `ai-vacation`을 설치한 뒤, 처음 실행 시 나오는 **훅 신뢰(trust) 확인을 승인**해야 동작합니다.
```
codex plugin marketplace add heyonseo/AI_vacation
```

## Google 캘린더 연결 (최초 1회)

에이전트에게 "AI 휴가 설정해줘"라고 하면 `ai-vacation` 스킬이 안내합니다. 직접 하려면:

1. [Google Cloud Console](https://console.cloud.google.com/)에서 **Google Calendar API** 사용 설정
2. OAuth 동의 화면 구성(외부라면 본인 계정을 테스트 사용자로 추가)
3. 사용자 인증 정보 → OAuth 클라이언트 ID → **데스크톱 앱** → JSON 다운로드
4. 실행:
   ```bash
   python3 <플러그인 경로>/scripts/ai_vacation.py setup --client-secret ~/Downloads/client_secret_XXX.json
   ```

권한은 `calendar.events`(일정 읽기/쓰기)만 요청합니다. 토큰은 `~/.config/ai-vacation/google_token.json`(권한 600)에 저장됩니다.

## 명령

```bash
python3 scripts/ai_vacation.py status                    # 연결 여부, 도구별 사용량, 휴가 일정
python3 scripts/ai_vacation.py check --force --verbose   # 지금 바로 반영
python3 scripts/ai_vacation.py config threshold_percent 90
python3 scripts/ai_vacation.py config event_type default # 부재중 대신 일반 일정
python3 scripts/ai_vacation.py config auto_decline declineOnlyNewConversationInvitations
python3 scripts/ai_vacation.py config providers claude   # 감시 대상 제한
```

### (선택) Claude Code 공식 사용량 값 쓰기

statusLine에 들어오는 `rate_limits`를 쓰면 비공개 API에 의존하지 않습니다. 기존 statusLine 명령 앞에 `tap --`를 붙이세요.

```json
{ "statusLine": { "type": "command", "command": "python3 /path/to/ai_vacation.py tap -- <기존 statusline 명령>" } }
```

## 테스트

```bash
python3 -m unittest discover -s tests -v
```

## 주의

- Claude 사용량 API(`/api/oauth/usage`)와 Codex rollout 형식은 공식 문서화된 인터페이스가 아니라 바뀔 수 있습니다. 읽지 못하면 조용히 건너뛰고 `~/.config/ai-vacation/ai-vacation.log`에 남깁니다.
