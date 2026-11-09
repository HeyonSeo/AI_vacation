# AI Vacation 🏖️

Claude Code·Codex의 **주간 사용 한도를 95% 이상 쓰면, 한도가 리셋되는 시각까지 Google 캘린더에 휴가를 표시**하는 플러그인입니다.
캘린더는 이미 연결해 둔 **Google Calendar MCP**로 씁니다. 따로 OAuth 설정을 할 필요가 없습니다.

## 동작

```
응답 종료(Stop 훅) ─▶ ai_vacation.py: 사용량 확인 → 캘린더에 할 일 판단
                         │ 할 일 있음
                         ▼
                   에이전트에게 [AI Vacation] 지시 (한 번 더 이어서 작업)
                         │
                         ▼
          에이전트: Google Calendar MCP로 일정 생성/수정 → ack로 결과 기록
```

- 주간 사용량이 기준(기본 95%) 이상이 되면 지금부터 리셋 시각까지 '바쁨' 일정을 만듭니다.
- 95%에서 움직이는 이유: 일정을 기록하려면 에이전트가 한 번 더 움직여야 하므로, 한도가 조금 남아 있을 때 처리합니다.
- 같은 주간 창에는 **일정이 하나만** 생깁니다.
  - Claude·Codex 훅이 같은 상태 파일(`~/.config/ai-vacation/`)을 함께 씁니다.
  - 일정 설명의 `[ai-vacation:...]` 표식으로 먼저 검색한 뒤에 만듭니다.
- 리셋 시각이 바뀌면 종료 시각을 고칩니다.
- 한도가 **일찍 리셋되면** 남은 휴가를 그 시각에 끝냅니다.
- 사용자가 일정을 **직접 지우면** 그 주에는 다시 만들지 않습니다.
- 지시를 보낸 뒤 30분 안에는 같은 지시를 반복하지 않습니다. 훅이 이어서 실행한 작업 안에서는 다시 막지 않습니다(무한 루프 방지).
- Google Calendar MCP가 없으면 연결하라고 한 줄 안내하고 24시간 동안 다시 묻지 않습니다.
- 한 도구에만 설치해도 Claude·Codex 사용량을 **둘 다** 감시합니다(도구별로 각각 일정).

| 도구 | 사용량 출처 |
|---|---|
| Claude Code | ① statusLine `rate_limits.seven_day` (tap 사용 시, 공식) ② `api.anthropic.com/api/oauth/usage` (Claude Code 로그인 토큰 사용) |
| Codex | `~/.codex/sessions/**/rollout-*.jsonl`의 `token_count` 이벤트 `rate_limits` 중 7일(10080분) 창 |

## 설치

필요: `python3` (표준 라이브러리만 사용), 각 도구에 연결된 Google Calendar MCP.

**Claude Code**
```
/plugin marketplace add heyonseo/AI_vacation
/plugin install ai-vacation@ai-vacation
```
Google 캘린더 커넥터(MCP)가 연결되어 있어야 합니다.

**Codex**: 저장소를 마켓플레이스로 추가하고 `ai-vacation`을 설치합니다.
```
codex plugin marketplace add heyonseo/AI_vacation
```
- 처음 실행할 때 나오는 **훅 신뢰(trust) 확인을 승인**해야 동작합니다.
- Codex에도 Google Calendar MCP 서버를 따로 연결해야 합니다.

## 명령

```bash
python3 scripts/ai_vacation.py status                  # 도구별 사용량, 기록된 휴가 일정, 대기 작업
python3 scripts/ai_vacation.py plan --force --text     # 지금 필요한 캘린더 작업(에이전트용 지시)
python3 scripts/ai_vacation.py config threshold_percent 90
python3 scripts/ai_vacation.py config calendar_id <캘린더ID>
python3 scripts/ai_vacation.py config providers claude
```

에이전트에게 "AI 휴가 상태 알려줘", "AI 휴가 지금 반영해줘"라고 해도 됩니다(`ai-vacation` 스킬).

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

- Claude 사용량 API(`/api/oauth/usage`)와 Codex rollout 형식은 공식 문서에 없는 인터페이스라 바뀔 수 있습니다. 읽지 못하면 조용히 건너뛰고 `~/.config/ai-vacation/ai-vacation.log`에 남깁니다.
- 한도를 100% 다 쓴 뒤에는 에이전트가 응답할 수 없어 일정을 기록하지 못합니다. 기준을 너무 높게 잡지 마세요.
