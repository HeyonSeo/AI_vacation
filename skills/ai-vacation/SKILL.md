---
name: ai-vacation
description: AI Vacation 플러그인 상태 확인·설정·즉시 반영. 사용자가 "AI 휴가", "주간 한도 휴가", "휴가 일정 확인/반영", "한도 기준 바꿔줘" 등을 말할 때, 또는 [AI Vacation] 지시를 받아 Google 캘린더에 휴가를 기록해야 할 때 사용.
---

# AI Vacation

주간 사용 한도(Claude Code / Codex)가 기준(기본 95%) 이상이 되면, 한도가 리셋되는 시각까지
Google 캘린더에 휴가 일정을 둔다. 캘린더 쓰기는 **이미 연결된 Google Calendar MCP 도구**로 한다.

평소에는 Stop 훅이 알아서 판단해 `[AI Vacation]` 지시를 보낸다. 그 지시를 받으면 적힌 대로
MCP로 처리하고 `ack` 명령으로 결과를 기록한다. 처리 후 사용자에게는 한 줄로만 알린다.

스크립트: 이 스킬 폴더 기준 `../../scripts/ai_vacation.py` (아래 `$S`는 그 절대 경로).

## 상태 확인

```bash
python3 "$S" status
```

도구별 주간 사용량·리셋 시각, 기록된 휴가 일정, 처리 대기 작업을 한국어로 짧게 정리해 전한다.

## 지금 바로 반영

```bash
python3 "$S" plan --force --text
```

"할 일 없음"이면 끝. 지시가 나오면 그대로 MCP로 처리하고 지시에 적힌 `ack` 명령을 실행한다.

## MCP 처리 원칙

- 만들기 전에 반드시 지시에 있는 `[ai-vacation:...]` 표식으로 일정을 검색해 중복을 막는다.
- 일정은 바쁨(busy)으로, 참석자·알림은 넣지 않는다. 표식은 설명(description)에 넣는다.
- 시작·종료는 지시에 적힌 시각을 시간대 그대로 쓴다.
- 지정한 일정이 이미 없으면(사용자가 지움) 다시 만들지 말고 `ack --dismissed`.
- Google Calendar MCP 도구가 없으면: 사용자에게 연결 방법을 한 줄로 알리고 `ack --skip` (24시간 보류).

## 설정 변경

```bash
python3 "$S" config                          # 현재 설정
python3 "$S" config threshold_percent 90     # 기준 변경
python3 "$S" config calendar_id <캘린더ID>    # 기본 캘린더 대신 다른 캘린더
python3 "$S" config providers claude         # 특정 도구만 감시
```
