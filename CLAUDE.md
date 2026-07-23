# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## 프로젝트 개요

DART 전자공시를 AI가 일반인 친화적으로 요약해 보여주는 Django 웹서비스. **`PLAN.md`가 전체 기획의 단일 출처**이며, 아키텍처 결정(스키마, 폴링 전략, 확장 설계, 로드맵)은 반드시 PLAN.md와 일치시켜야 한다. 현재 로드맵 1단계(데이터 수집) 진행 중이며, LLM 요약(2단계)·웹 화면(3단계)·Celery 비동기 처리는 아직 미구현이다.

## 명령어

가상환경은 `venv/`에 있고, Windows에서는 `.\venv\Scripts\python.exe`로 직접 실행한다.

```powershell
.\venv\Scripts\python.exe manage.py runserver        # 개발 서버
.\venv\Scripts\python.exe manage.py makemigrations   # 마이그레이션 생성
.\venv\Scripts\python.exe manage.py migrate
.\venv\Scripts\python.exe manage.py test disclosures # 테스트 (단일: test disclosures.tests.TestClass.test_method)
.\venv\Scripts\python.exe manage.py seed_companies   # 반도체 10개 기업 시드 + corpCode.xml로 corp_code 매핑
.\venv\Scripts\python.exe manage.py poll_dart --days 3  # DART 공시 폴링·적재 (멱등)
```

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
  → (미구현) document.xml 원문 확보 → LLM 요약 → DisclosureSummary
```

### 모델 체인 (disclosures/models.py)

`Sector → Company → Disclosure → DisclosureSummary(OneToOne)`. 요약은 공시당 정확히 1회 생성 후 영구 재사용하는 것이 LLM 비용 통제의 핵심(PLAN.md 11). `DisclosureSummary.is_reviewed`는 중요도 '높음' 공시의 사람 검수 게이트(Django admin에서 수행)를 위한 필드다.

### DART 클라이언트 (disclosures/dart.py)

Django 독립적인 얇은 함수 모음. 오류는 `DartApiError(status, message)`로 통일한다. DART 응답 특성: corpCode.xml·document.xml은 ZIP으로 오고, 오류 시에만 XML 본문이 온다(코드가 이를 분기 처리함). `list.json`의 status `000`=정상, `013`=결과 없음(정상 취급).

## 하네스: DART 공시 요약 서비스 개발

**목표:** 에이전트 팀(analyst·backend·ai-prompt·frontend·qa)으로 로드맵(PLAN.md 9장) 단계를 구현한다.

**트리거:** 기능 구현·로드맵 단계 착수 등 여러 영역에 걸친 개발 작업 요청 시 `dart-dev-orchestrator` 스킬을 사용하라. 단일 파일 수정이나 단순 질문은 직접 응답 가능.

**변경 이력:**
| 날짜 | 변경 내용 | 대상 | 사유 |
|------|----------|------|------|
| 2026-07-23 | 초기 구성 (에이전트 5 + 스킬 3) | 전체 | PLAN.md 8장 파이프라인 패턴 |

## 컨벤션

- 모델 필드·admin에는 한국어 `verbose_name`을 붙인다. 주석·docstring·커밋 메시지도 한국어.
- 서비스 요약 출력에는 항상 DART 원문 링크(`dart_viewer_url`)를 병기하고, 투자 자문이 아니라는 면책 문구를 UI에 상시 노출한다(PLAN.md 1.4, 5.3).
