# 1단계: 데이터 수집 — 작업 입력

- **로드맵 단계**: PLAN.md 9장 1단계 (데이터 수집)
- **브랜치**: feature/stage1-data-collection
- **기준 커밋**: 3a18e4f (0단계 셋업)
- **팀 구성**: 리더(직접 실행) + qa(회귀 테스트·독립 검증). frontend·ai-prompt 제외.
- **실행 모드**: 서브에이전트 모드 (팀 도구 미가용)

## 목표

0단계에서 작성한 수집 코드(dart.py, seed_companies, poll_dart)를 실제 DART API로 돌려
"공시가 DB에 실제로 쌓인다"를 검증하고, 실데이터에서 드러나는 문제를 잡아 파이프라인을 완성한다.

## 완료 조건

1. migrate 후 seed_companies로 반도체 10개 기업 corp_code 전부 매핑 (누락 0건)
2. poll_dart --days 7 실호출로 실제 공시 Disclosure 적재, 건수·기업별 분포 보고
3. 재실행 멱등성 (중복 적재 0건)
4. DART 응답 필드 → 모델 필드 정합성 확인, 실응답 샘플 _workspace/에 기록
5. disclosures/tests.py에 회귀 테스트 고정, manage.py test 통과

## 제약

- 사용자 요청 경로에서 DART 직접 호출 금지 (수집 파이프라인만 호출)
- corp_code 없이 날짜 범위 전체 조회 후 로컬 필터링 (호출 수와 기업 수 분리, PLAN.md 12.2)
- 실호출 중 문제 발생 시 사용자에게 즉시 보고·질문
