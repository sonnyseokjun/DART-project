# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## 프로젝트 개요

DART 전자공시를 AI가 일반인 친화적으로 요약해 보여주는 Django 웹서비스. **`PLAN.md`가 전체 기획의 단일 출처**이며, 아키텍처 결정(스키마, 폴링 전략, 확장 설계, 로드맵)은 반드시 PLAN.md와 일치시켜야 한다.

**진행 상황 (PLAN.md 9장 기준):** 0단계 셋업 · 1단계 수집 · 2단계 AI 요약 · 3단계 웹 조회 · 4단계 검수 워크플로우 · 5단계 요약 자동 교정 · 6단계 배포까지 **완료**(PR #21까지 머지, 요약 146건 전량 v4). 로드맵이 한 번 재편되었다 — 5단계가 신설되면서 배포가 6단계로 밀렸다(PLAN.md 9.1). **서비스는 https://dartaisite.duckdns.org 에서 상시 운영 중이다**(이슈 #16 → PR #17): Lightsail 1GB + Ubuntu 24.04 + Docker Compose + SQLite(WAL) + Caddy + 호스트 cron. 구성 근거와 기각한 대안은 **PLAN.md 9.2**에 있으니 배포 관련 판단은 거기서 확인할 것. 배포 문서는 셋으로 나뉜다 — **PLAN.md 9.2**(결정 기록) · **DEPLOY.md**(무엇을 왜 이렇게 만들었나, 소개용) · **docs/RUNBOOK.md**(구축·장애 대응·복원 절차). **현재는 7단계 준실시간화 진행 중이다**(이슈 #22): 공시 노출 지연을 24시간 → 3분으로 줄인다. **구성 판단은 반드시 PLAN.md 9.3을 먼저 볼 것** — 4.5의 원안(Celery·Redis·PostgreSQL·ASGI)은 측정 이전 설계라 **채택하지 않았다**. 실측(요약 피크 590MB · 쓰기 주체 1 · 데이터 10.7MB · 월 45건)에 근거해 **인프라를 그대로 두고 cron 주기 단축 + `flock` + 클라이언트 폴링으로 푼다**. Celery/PostgreSQL 전환 조건은 9.3에 명시돼 있으니, 그 선을 넘지 않는 한 도입하지 말 것. **주의 1:** 서버는 UTC였다 — 파이프라인이 한국시간 16:00에 돌고 있었고 2026-09-01에 `Asia/Seoul`로 고쳤다. **주의 2:** 재구축 리허설은 6단계에서 범위 밖으로 뺐다 — 복원 절차는 문서화됐을 뿐 실행 검증된 적이 없다(PLAN.md 9.1-(4)).

## 명령어

가상환경은 `venv/`에 있고, Windows에서는 `.\venv\Scripts\python.exe`로 직접 실행한다.

```powershell
.\venv\Scripts\python.exe manage.py runserver        # 개발 서버
.\venv\Scripts\python.exe manage.py makemigrations   # 마이그레이션 생성
.\venv\Scripts\python.exe manage.py migrate
.\venv\Scripts\python.exe manage.py test disclosures # 테스트 (단일: test disclosures.tests.TestClass.test_method)
.\venv\Scripts\python.exe manage.py seed_companies   # 반도체 10개 기업 시드 + corpCode.xml로 corp_code 매핑
.\venv\Scripts\python.exe manage.py poll_dart --days 3  # DART 공시 폴링·적재 (멱등)
.\venv\Scripts\python.exe manage.py poll_dart --bgn 20260101 --end 20260630  # 임의 구간 백필 (89일 창으로 자동 분할)
.\venv\Scripts\python.exe manage.py apply_selection --dry-run   # 요약 대상 선별 정책 적용
.\venv\Scripts\python.exe manage.py fetch_documents --limit 5   # 선별 대상 원문 확보·전처리 (원문은 크다, --limit 먼저)
.\venv\Scripts\python.exe manage.py revalidate_summaries        # 저장된 요약 재검증 (LLM 미호출, 멱등)
```

파이프라인 순서: `poll_dart` → `apply_selection` → `fetch_documents` → `summarize_disclosures` → (`revalidate_summaries`).

**⚠ `summarize_disclosures`는 비용이 발생하는 유일한 명령이며, 에이전트 실행이 훅으로 차단된다** (`.claude/hooks/block_llm_calls.py`). 실행은 사용자가 직접 판단해 수행한다. 요약 품질·검증기 수정 효과는 LLM을 부르지 않는 `revalidate_summaries`로 측정한다.

`DART_API_KEY`는 `.env`에서 로드된다(`settings.DART_API_KEY`). 키가 없으면 DART 호출 명령은 `DartApiError`로 실패한다.

## 아키텍처

### 핵심 설계 원칙 (PLAN.md 전반을 관통)

**사용자 요청과 DART API 호출의 완전 분리.** 웹 요청은 항상 로컬 DB에서만 읽고, DART API는 수집 파이프라인(`poll_dart`)만 호출한다. DART 호출 수는 사용자 트래픽과 무관해야 하며, 이 원칙을 깨는 코드(뷰에서 DART 직접 호출 등)를 넣지 말 것.

### 데이터 흐름

```
poll_dart (관리 명령, 추후 Celery 태스크로 이전 예정)
  → dart.py: list.json을 corp_code 없이 날짜 범위로 전체 조회 (페이지네이션)
  → 로컬에서 추적 기업(Company.is_active)만 필터링   ← 기업 수가 늘어도 호출 수 불변 (PLAN.md 12.2)
  → Disclosure.get_or_create(rcept_no=...)          ← rcept_no unique가 멱등성·중복 요약 방지의 근간
apply_selection  → 요약 대상 선별 (selection.py)
fetch_documents  → document.xml 원문 확보·전처리 (대형 서식은 KEY_SECTIONS_BY_TYPE로 섹션 추출)
summarize_disclosures → LLM 요약 → DisclosureSummary   ← 비용 발생 지점, 공시당 1회
revalidate_summaries  → 원문 대조 재검증 (LLM 미호출)
```

### 모델 체인 (disclosures/models.py)

`Sector → Company → Disclosure → DisclosureSummary(OneToOne)`. 요약은 공시당 정확히 1회 생성 후 영구 재사용하는 것이 LLM 비용 통제의 핵심(PLAN.md 11). 요약을 **삭제하지 않고 감추는**(`is_published`/`hidden_reason`) 이유도 같다 — 지우면 재수집 시 LLM을 다시 부른다.

검수는 Django admin에서 수행하며, `templates/admin/disclosures/disclosuresummary/`의 `change_form.html`·`_review_panel.html`이 검수 패널을 얹는다. `is_reviewed`가 게이트이고, `review_warnings`(자동 검증 경고)·`llm_original`(사람 수정 전 LLM 원본 스냅샷)이 검수 판단 근거다.

**숫자 표기 주의:** 한국어 수 단위(만·억·조)는 4자리로 끊고 콤마는 3자리로 끊는다. 이 어긋남이 실제 요약 오류의 원인이었으므로, **단위 환산은 LLM이 아니라 코드가 한다**.

### DART 클라이언트 (disclosures/dart.py)

Django 독립적인 얇은 함수 모음. 오류는 `DartApiError(status, message)`로 통일한다. DART 응답 특성: corpCode.xml·document.xml은 ZIP으로 오고, 오류 시에만 XML 본문이 온다(코드가 이를 분기 처리함). `list.json`의 status `000`=정상, `013`=결과 없음(정상 취급).

## 하네스: DART 공시 요약 서비스 개발

**목표:** 에이전트 팀(analyst·backend·ai-prompt·frontend·qa)으로 로드맵(PLAN.md 9장) 단계를 구현한다.

**트리거:** 기능 구현·로드맵 단계 착수 등 여러 영역에 걸친 개발 작업 요청 시 `dart-dev-orchestrator` 스킬을 사용하라. 단일 파일 수정이나 단순 질문은 직접 응답 가능.

**변경 이력:**
| 날짜 | 변경 내용 | 대상 | 사유 |
|------|----------|------|------|
| 2026-07-23 | 초기 구성 (에이전트 5 + 스킬 3) | 전체 | PLAN.md 8장 파이프라인 패턴 |
| 2026-07-27 | DART 실측 지식 축적 (인코딩·토큰량·89일 제한) | skills/dart-api-know-how | 2단계 실행 중 문서만으로는 모르는 함정 발견 |
| 2026-07-27 | 권한 정책 추가 (`rm`·`git push`·`.env` deny) | settings.json | 에이전트 사고 방지 |
| 2026-08-14 | **하네스 감사 및 4개 항목 보완** | settings.json · hooks · agents 3 · skills 2 · CLAUDE.md | 아래 참조 |

2026-08-14 감사에서 확인된 것 — 3·4단계 실행 중에는 하네스가 한 번도 갱신되지 않았고, 그 사이 절대 제약·파일 소유권·Phase 구조가 매 실행 `_workspace/00_input.md`에 수작업으로 재작성되고 있었다. 반복되는 규칙을 하네스로 승격하고, 비용 발생 경로(`summarize_disclosures`)에 실행 훅 가드를 추가했다.

## 컨벤션

- 모델 필드·admin에는 한국어 `verbose_name`을 붙인다. 주석·docstring·커밋 메시지도 한국어.
- 서비스 요약 출력에는 항상 DART 원문 링크(`dart_viewer_url`)를 병기하고, 투자 자문이 아니라는 면책 문구를 UI에 상시 노출한다(PLAN.md 1.4, 5.3).
