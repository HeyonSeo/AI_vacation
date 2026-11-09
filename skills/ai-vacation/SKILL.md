---
name: ai-vacation
description: AI Vacation 플러그인 설정·상태 확인. 사용자가 "AI 휴가", "주간 한도 휴가", "캘린더 휴가 연결/설정/상태", "한도 다 쓰면 휴가" 등을 말하거나 Google 캘린더 연결, 기준(%) 변경, 휴가 일정 확인을 원할 때 사용.
---

# AI Vacation

주간 사용 한도(Claude Code / Codex)가 기준(기본 95%) 이상이 되면, 한도가 리셋되는 시각까지
사용자의 Google 캘린더에 휴가(부재중) 일정을 자동으로 만든다. 훅(SessionStart, Stop)이 백그라운드로
확인하므로 평소에는 아무것도 할 필요가 없다. 이 스킬은 **처음 연결**과 **확인/조정**에만 쓴다.

스크립트 위치: 이 스킬 폴더 기준 `../../scripts/ai_vacation.py` (플러그인 루트의 `scripts/ai_vacation.py`).
아래에서 `$S`는 그 절대 경로다.

## 1. 상태 확인 — 항상 이것부터

```bash
python3 "$S" status
```

결과를 사용자에게 한국어로 짧게 정리한다: 캘린더 연결 여부, 도구별 주간 사용량과 리셋 시각, 휴가 일정 존재 여부.

## 2. 처음 연결 (캘린더 연결: 아니오 일 때)

사용자가 직접 해야 하는 일은 Google OAuth 클라이언트 파일 하나 받기뿐이다. 순서대로 안내한다.

1. https://console.cloud.google.com/ 에서 프로젝트 선택(없으면 생성)
2. "API 및 서비스 → 라이브러리"에서 **Google Calendar API** 사용 설정
3. "OAuth 동의 화면" 구성(내부/외부 아무거나, 외부면 본인 계정을 테스트 사용자로 추가)
4. "사용자 인증 정보 → OAuth 클라이언트 ID 만들기 → 애플리케이션 유형: **데스크톱 앱**" → JSON 다운로드

파일 경로를 받으면 실행한다(브라우저가 열리고 사용자가 승인하면 끝난다):

```bash
python3 "$S" setup --client-secret "<다운로드한 JSON 경로>"
```

브라우저를 열 수 없는 환경이면 출력된 URL을 사용자에게 그대로 전달한다. 같은 컴퓨터의 브라우저에서 열어야 한다.

## 3. 지금 바로 반영

```bash
python3 "$S" check --force --verbose
```

## 4. 설정 변경

```bash
python3 "$S" config                          # 현재 설정 보기
python3 "$S" config threshold_percent 90     # 기준 변경
python3 "$S" config event_type default       # 부재중 대신 일반 일정
python3 "$S" config auto_decline declineOnlyNewConversationInvitations  # 휴가 중 새 회의 초대 자동 거절
python3 "$S" config providers claude         # 특정 도구만 감시
```

## 주의

- 사용자가 캘린더에서 휴가 일정을 직접 지우면, 같은 주간 창에서는 다시 만들지 않는다(사용자 결정 존중).
- 한도가 예정보다 일찍 리셋되면 남은 휴가 일정은 그 시각에 끝나도록 줄어든다.
- 토큰·상태 파일은 `~/.config/ai-vacation/`에 있다. 내용을 화면에 출력하지 말 것.
