---
name: dart-dev-orchestrator
description: "DART 공시 요약 서비스의 개발 에이전트 팀(analyst·backend·ai-prompt·frontend·qa)을 조율하는 오케스트레이터. 로드맵 단계 구현('수집 파이프라인 만들어줘', '요약 파이프라인 구현', '웹 화면 만들어줘'), 기능 추가, 여러 영역에 걸친 개발 작업 요청 시 반드시 이 스킬을 사용. 후속 작업 — 다시 실행, 재실행, 업데이트, 수정, 보완, 특정 부분만 다시, 이전 결과 개선, 결함 수정 요청 시에도 반드시 이 스킬을 사용. 단일 파일 수정이나 단순 질문은 직접 처리 가능."
---

# DART-Dev Orchestrator

DART 공시 쉬운 요약 웹서비스의 개발 에이전트 팀을 조율하여 로드맵(PLAN.md 9장) 단계를 구현하는 통합 스킬.

## 실행 모드: 에이전트 팀

파이프라인 + 병렬 구간 복합 패턴. 분석 → (백엔드 ∥ AI프롬프트 ∥ 프론트엔드) → 점진적 QA.
팀 도구(TeamCreate 등)를 쓸 수 없는 환경이면 서브 에이전트 모드(Agent 도구, `model: "opus"`, 산출물은 동일하게 `_workspace/`)로 대체한다.

## 에이전트 구성

| 팀원 | 에이전트 타입 | 역할 | 참조 스킬 | 출력 |
|------|-------------|------|----------|------|
| analyst | analyst (커스텀) | DART API 스펙·공시 구조 분석 | dart-api-know-how | `_workspace/{phase}_analyst_*.md` |
| backend | backend (커스텀) | Django 모델·수집·API 구현 | dart-api-know-how | 코드 + `_workspace/{phase}_backend_*.md` |
| ai-prompt | ai-prompt (커스텀) | 요약 프롬프트·출력 스키마 | summary-standards | 코드 + `_workspace/{phase}_ai-prompt_*.md` |
| frontend | frontend (커스텀) | 템플릿·카드 UI | — | 코드 + `_workspace/{phase}_frontend_*.md` |
| qa | qa (커스텀) | 정확성·정합성 검증 | summary-standards | `_workspace/{phase}_qa_*.md` |

모든 Agent 호출에 `model: "opus"`를 명시한다.

## 워크플로우

### Phase 0: 컨텍스트 확인 (후속 작업 지원)
1. `_workspace/` 존재 여부 확인
2. 실행 모드 결정:
   - 미존재 → 초기 실행, Phase 1로
   - 존재 + 부분 수정 요청 → **부분 재실행**: 해당 에이전트만 재호출, 이전 산출물 경로를 프롬프트에 포함
   - 존재 + 새 로드맵 단계 착수 → 기존 `_workspace/`를 `_workspace_{YYYYMMDD_HHMMSS}/`로 이동 후 초기 실행
3. CLAUDE.md 변경 이력과 현재 요청의 관련성 확인 (이전 피드백 재발 방지)

### Phase 1: 준비
1. 요청을 로드맵 단계(PLAN.md 9장)에 매핑하고 작업 범위 확정
2. `_workspace/` 생성, 요청 요약을 `_workspace/00_input.md`에 저장
3. 팀 규모 결정 — 이번 요청에 불필요한 에이전트는 팀에서 제외한다 (예: 수집 파이프라인 작업에 frontend 불필요)

### Phase 2: 팀 구성
1. `TeamCreate(team_name: "dart-dev", members: [필요 에이전트만, 각 model: "opus"])`
2. `TaskCreate`로 작업 등록 — 의존 관계 명시:
   - 분석 작업(analyst) → 구현 작업(backend/ai-prompt/frontend, `depends_on` 분석) → 검증 작업(qa, 모듈별)
   - 팀원당 3~6개 작업 유지

### Phase 3: 분석 → 구현 (팀원 자체 조율)
- analyst가 명세를 확정하면 backend·ai-prompt·frontend가 병렬 구현
- 통신 규칙은 각 에이전트 정의의 "팀 통신 프로토콜"을 따른다. 핵심: 인터페이스(스키마·컨텍스트 shape) 변경은 소비자에게 즉시 SendMessage
- 리더는 TaskGet으로 진행 모니터링, 막힌 팀원에게 개입

### Phase 4: 점진적 QA
- 각 구현 에이전트는 모듈 완성 즉시 qa에게 검증을 요청한다 (전체 완성 대기 금지)
- qa 결함 보고 → 담당 에이전트 수정 → qa 재검증. 동일 결함 2회 재발 시 리더가 설계 재검토
- 최종적으로 qa가 `manage.py check`·`manage.py test` 통과를 확인

### Phase 5: 통합 및 정리
1. 산출물 수집, 변경 요약 작성
2. 팀 정리 (TeamDelete), `_workspace/` 보존
3. CLAUDE.md의 하네스 변경 이력 갱신 (에이전트/스킬이 수정된 경우)
4. 사용자에게 결과 보고 + 개선 피드백 요청 (하네스 진화 입력)

## 데이터 흐름

```
[리더] → analyst ──명세──▶ _workspace/*_analyst_*.md
              │                      │ (Read)
              ▼                      ▼
        backend ∥ ai-prompt ∥ frontend  ←── SendMessage로 인터페이스 협의
              │ (모듈 완성 즉시)
              ▼
             qa ──결함──▶ 담당 에이전트 (직접 통보) ──수정──▶ qa 재검증
              │
              ▼
        [리더: 통합·보고]
```

## 에러 핸들링

| 상황 | 전략 |
|------|------|
| 팀원 1명 실패/중지 | 리더가 SendMessage로 상태 확인 → 재시작, 재실패 시 작업을 다른 팀원 또는 리더가 흡수 |
| DART API 키/한도 문제 | analyst·backend는 실호출 없이 문서 기반으로 진행하고 "실행 미검증" 명시 |
| 팀원 간 인터페이스 충돌 | 소비자(사용하는 쪽) 요구를 우선, 충돌 내용을 `_workspace/`에 기록 |
| 동일 결함 2회 재발 | 개별 수정 중단, 리더가 설계 재검토 후 작업 재분배 |
| 팀 도구 사용 불가 | 서브 에이전트 모드로 폴백 (analyst → 병렬 구현 → qa 순차 호출) |

## 테스트 시나리오

### 정상 흐름 (로드맵 2단계 예시)
1. 사용자: "LLM 요약 파이프라인 구현해줘"
2. Phase 1: 로드맵 2단계로 매핑, analyst·backend·ai-prompt·qa 4명 팀 결정 (frontend 제외)
3. Phase 2~3: analyst가 document.xml 구조 분석 → ai-prompt가 프롬프트·스키마 설계, backend가 요약 저장 로직 구현
4. Phase 4: qa가 샘플 공시로 숫자 대조 검증
5. 예상 결과: 요약 생성 코드 + `_workspace/` 산출물 + 검증 보고서

### 에러 흐름
1. Phase 3에서 ai-prompt의 스키마와 backend의 모델 필드가 불일치
2. qa가 경계면 교차 비교로 감지, 양쪽에 SendMessage
3. 소비자 우선 원칙으로 backend 모델 기준 스키마 수정 → qa 재검증 통과
4. 최종 보고에 충돌·해결 기록
