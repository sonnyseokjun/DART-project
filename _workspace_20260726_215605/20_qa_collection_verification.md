# 1단계 데이터 수집 — 검증 보고

- 검증자: 리더(팀 도구 미가용 → 리더가 QA 흡수)
- 기준: feature/stage1-data-collection, 조회 범위 20260719~20260726

## 완료 조건 체크리스트

| # | 조건 | 결과 | 근거 |
|---|------|------|------|
| 1 | corp_code 매핑 10/10 | ✅ | 상장사 3,979 로드, 10곳 전부 매핑, 누락 0 |
| 2 | 공시 적재 | ✅ | 3,843 스캔 → 755 저장 (지분공시 748·거래소공시 7) |
| 3 | 멱등성 | ✅ | 재실행 신규 0, rcept_no 중복 0 |
| 4 | 필드 정합성 | ✅ | 빈 유형 0, report_name strip·filed_at 파싱·dart_url 정상 |
| 5 | 회귀 테스트 | ✅ | disclosures/tests.py 7건 통과, `manage.py check` 무결 |

## 경계면 교차 비교 (dart.py ↔ poll_dart ↔ 모델)

- list.json 응답 필드(9개) → 모델 필드 매핑 전수 확인. 미사용 필드(corp_cls/flr_nm/rm) 명시.
- `disclosure_type`: 응답에 없음 → 유형별 조회로 태깅(해결). PBLNTF_TYPES 단일 출처.
- `report_nm` 뒤 공백 다수 → poll_dart가 `.strip()` (테스트 test_field_mapping로 고정).
- `rcept_no` unique → 멱등성 근간 (테스트 test_idempotent_rerun로 고정).

## 발견·해결 이슈

1. **[해결] disclosure_type 전건 빈값** — `pblntf_ty`는 응답 필드가 아니라 요청 필터. 유형별 분할 조회로 전환. 상세: `10_analyst_list_json_spec.md`.

## 실행 명령·결과

- `manage.py seed_companies` → 신규 10곳
- `manage.py poll_dart --days 7` → 755 저장 / 재실행 0 저장
- `manage.py test disclosures` → Ran 7 tests, OK
- `manage.py check` → no issues

## 불변식 확인

- rcept_no 중복 없음 ✅
- 뷰에서 DART 직접 호출 없음(수집은 관리 명령만) ✅
- 호출 수가 기업 수·트래픽에 비례하지 않음(유형 10종 고정) ✅
