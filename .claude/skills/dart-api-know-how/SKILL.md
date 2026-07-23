---
name: dart-api-know-how
description: "DART OpenAPI 호출 절차와 함정 지식. DART API 연동 코드 작성/수정, corpCode·list.json·document.xml 처리, 공시 수집 파이프라인 작업, API 오류 디버깅 시 반드시 참조. analyst·backend 에이전트의 공용 스킬."
---

# DART OpenAPI Know-how

DART OpenAPI를 다루는 모든 작업에서 지켜야 할 절차와, 문서만 봐서는 모르는 함정 지식.

## 기본 규칙

- 모든 호출은 `disclosures/dart.py`의 함수를 통한다. 새 엔드포인트가 필요하면 dart.py에 추가하고, 뷰·태스크에서 requests를 직접 쓰지 않는다
- 인증키는 `settings.DART_API_KEY`(.env)에서만 읽는다 — 코드·산출물에 키 값을 남기지 않는다
- **뷰(웹 요청)에서 DART를 호출하지 않는다** — 수집 파이프라인만 호출한다 (프로젝트 핵심 설계 원칙)

## 응답 형태의 함정 (중요)

| 엔드포인트 | 정상 응답 | 오류 응답 |
|-----------|----------|----------|
| corpCode.xml | ZIP 바이너리 | XML (`<status>`, `<message>`) |
| document.xml | ZIP 바이너리 (내부에 XML) | XML |
| list.json | JSON | JSON (`status` 필드) |

- 같은 엔드포인트가 성공/실패에 따라 다른 Content-Type을 반환하므로, ZIP 시그니처(`PK`) 또는 status 필드로 분기한다 (dart.py에 구현됨)
- `list.json`의 status `000`=정상, **`013`=조회 결과 없음(오류 아님, 빈 목록으로 취급)**. 그 외는 `DartApiError`
- corpCode.xml에는 비상장 법인이 다수 포함 — `stock_code` 유무로 상장사를 필터링한다

## 호출량 규칙

- 일일 한도 약 20,000회(개인 키). 대량 조회는 `corp_code` 없이 날짜 범위로 시장 전체를 받아 로컬 필터링한다 (PLAN.md 12.2 — 기업 수와 호출 수의 분리)
- `page_count` 최대 100. 페이지네이션은 `iter_disclosures()` 사용
- 같은 `rcept_no`의 원문을 두 번 받지 않는다 — `Disclosure.raw_fetched` 확인 후 호출

## 멱등성 패턴

수집 코드는 반드시 다음 패턴을 따른다:

```python
d, created = Disclosure.objects.get_or_create(rcept_no=..., defaults={...})
if created:
    # 신규일 때만 후속 처리 (원문 확보, 요약 enqueue)
```

이유: 폴링 중복·재시도에도 중복 저장이 없고, 중복 LLM 호출(비용)을 원천 차단한다.

## 검증 절차

DART 연동 코드를 작성/수정한 후:
1. `manage.py check` 통과 확인
2. 키가 있으면 실호출 1건으로 검증 (`poll_dart --days 1` 등), 없으면 산출물에 "실호출 미검증" 명시
3. 새 응답 필드를 쓰게 되면 실제 응답 샘플을 `_workspace/`에 기록 (다음 작업자의 명세가 된다)
