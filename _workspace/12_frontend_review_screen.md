# 4단계 frontend — 검수 화면(원문 대조 패널) · 웹 "사람이 검토·수정함" 표시

작업 범위: `00_input.md` 4장 frontend 완료 조건 전체.
소유 파일만 수정했다(`disclosures/templates/**`, `disclosures/static/**`, `disclosures/templatetags/**`).
`models.py`·`admin.py`·`views.py`·`tests.py`는 건드리지 않았다.

## 1. 만든 파일 · 고친 파일

| 파일 | 역할 |
|---|---|
| `disclosures/templates/admin/disclosures/disclosuresummary/change_form.html` | 신규. `admin/change_form.html`을 상속해 `extrahead`(CSS·JS)와 `content`(패널) 두 블록만 확장 |
| `disclosures/templates/admin/disclosures/disclosuresummary/_review_panel.html` | 신규. 원문 대조 패널 본체 |
| `disclosures/templatetags/__init__.py`, `review_panel.py` | 신규. `highlight_terms` 태그, `evidence_field_label`·`has_key` 필터 |
| `disclosures/static/disclosures/css/review-panel.css` | 신규. admin 전용 스타일(전 클래스 `rp-` 접두어) |
| `disclosures/static/disclosures/js/review-panel.js` | 신규. 지목 수치 칩 → 원문 위치 이동(유일한 JS 기능) |
| `disclosures/templates/disclosures/_disclosure_card.html` | 수정. `was_human_edited` 배지 |
| `disclosures/templates/disclosures/disclosure_detail.html` | 수정. `was_human_edited` 배지 + 푸터 한 줄 설명 |
| `disclosures/static/disclosures/css/style.css` | 수정. `--verified`/`--verified-bg` 변수, `.badge-human`, `.detail-foot .human-edited-note` |

## 2. 원문 대조 패널 구조

폼 **위**에 패널을 얹었다. 검수자가 먼저 "무엇이 의심스러운가"를 판단하고, 패널 안의
`수정하러 가기 ↓`(`#{{ opts.model_name }}_form`)로 아래 폼에 내려가 고치는 흐름이다.

```
[원문 대조]                    [DART 원문 열기 ↗] [수정하러 가기 ↓]
기업 · 공시명 · 접수일 · 접수번호
배지: 중요도 / 검수 필요·검수 완료 / 사람이 검토·수정함 / 웹에서 숨김(+사유) / 검수자·검수시각
자동 검증 경고 원문 목록(review_warnings)
┌ 먼저 확인할 수치 ─────────────────────────────────────────┐
│ [3조 9,891억 · 원문 2곳] [4,000억 · 원문에 없음] …          │  ← 클릭 시 원문으로 점프
└──────────────────────────────────────────────────────────┘
┌ 현재 요약 (스크롤) ──────┬ 공시 원문 (스크롤, 글자수·하이라이트 수) ─┐
│ 한 줄 요약 / 쉬운 설명 / │  … 지목 수치가 <mark>로 하이라이트 …      │
│ 왜 중요한가 (하이라이트) │                                            │
│ ▸ LLM 원본 보기(접힘)    │                                            │
└──────────────────────────┴────────────────────────────────────────────┘
원문 근거 N건 — [필드 라벨] [인용 확인됨|인용 미발견|인용 미검증] [수치 대조됨|수치 미대조: …]
                claim 문장 + quote 인용 블록
```

- 컨텍스트는 admin이 주는 `original` 하나만 쓴다. **뷰 쪽 추가 컨텍스트가 필요 없다.**
- 패널은 DB의 `disclosure.raw_content`만 읽는다 — DART·LLM 호출 없음(PLAN.md 12.1).
- `original.evidence`가 비었거나 `raw_content`가 없어도 안내 문구로 대체되어 깨지지 않는다.
- 추가(add) 화면에서는 `{% if not add and original %}`로 패널을 걸러 낸다.

## 3. 하이라이트 구현 (`templatetags/review_panel.py`)

`{% highlight_terms 텍스트 수치목록 '접두어' as hl %}` → `{'html', 'hits', 'total', 'missing'}`.
같은 태그를 원문과 요약 3필드에 각각 다른 접두어로 4번 호출해 **양쪽에 동일한 표시**를 준다.

**XSS 대응**: 위치 탐색은 이스케이프 **전** 원문에서 하고, 잘라낸 조각은 각각
`django.utils.html.escape()`, 마크업은 `format_html()`로만 만든 뒤 마지막에 `mark_safe`.
이스케이프된 문자열에서 검색하면 `&`·`<`가 엔티티로 바뀌어 위치가 어긋나므로 순서가 중요하다.
실측 확인: 원문 `<b>표기</b>`가 `&lt;b&gt;`로 렌더되고 `<mark>`만 삽입된다.

**표기 관용**: 쉼표·공백은 있어도 없어도 같은 값으로 본다(`[\s,]*`). 요약은 `3조 9,891억`,
원문(표 추출)은 `3조9891억`으로 적히는 일이 흔해서, 정확 일치만 보면 하이라이트가 거의 안 걸린다.
단위 문자(`조`·`억`·`%`)는 느슨하게 풀지 않는다 — 엉뚱한 곳이 걸린다.
긴 표기를 먼저 매칭해 `3조 9,891억`이 `891`에 먹히지 않게 했고, 구분자만으로 된 항목은
빈 매칭 무한 분할을 막기 위해 걸러 낸다.

**`원문에 없음` 칩이 핵심 신호다.** 요약의 수치가 원문에 문자열로 아예 없다는 뜻으로,
39조 8,905억 → 3조 9,891억 같은 사례가 여기서 드러난다. 다만 표기 차이일 수도 있으므로
화면 문구는 "오류 확정"이 아니라 "가장 먼저 눈으로 확인할 곳"으로 썼다.

현재 DB 실측(요약 140건): 지목 수치가 있는 요약 19건 · 원문에서 찾은 표기 19개 ·
못 찾은 표기 10개. 이 10개가 검수 우선순위 최상단이다.

## 4. 긴 원문 처리

- 두 단(요약·원문)을 `max-height: 60vh` + `overflow-y: auto`로 각자 스크롤시킨다.
  원문을 그대로 펼치면 아래 수정 폼이 화면 밖으로 밀려나 검수 흐름이 끊긴다.
- 원문 단 제목에 **글자 수와 하이라이트 적중 수**를 표시해 규모를 먼저 알린다.
- 스크롤 안에서 위치를 찾는 수단이 칩 버튼이다. 각 `<mark>`에 `raw-{수치}-{순번}` id를 주고,
  칩을 누를 때마다 다음 적중으로 `scrollIntoView({block:'center'})` + 활성 하이라이트 표시.
  이 순환 이동이 JS의 전부다(외부 라이브러리·빌드 단계 없음).
- 1024px 이하에서는 2단 → 1단, 스크롤 높이 44vh.

## 5. 웹 화면 표시

- 카드·상세 모두 `summary.was_human_edited`일 때 `✓ 사람이 검토·수정함` 배지(녹색 실선).
  미검수(회색 점선)·정확성 경고(붉은색)와 색·테두리 모두 구분된다. 새 변수 `--verified` 추가.
- 상세 푸터에는 "사람이 DART 원문과 대조해 검토하고 내용을 수정했습니다" 한 줄을 덧붙였다.
  검수자 개인은 노출하지 않는다(서비스 차원의 검수 사실만 알리면 된다).
- `was_human_edited = edited_by_human and is_reviewed`이므로 미검수 배지와 동시에 뜨지 않는다.
- **검수 완료 시 배지·배너 소멸 확인 완료**: 격리 테스트 DB 렌더에서
  `badge-unreviewed` 없음 / `accuracy-alert` 없음 / `badge-human` 있음 / 면책 문구 유지.

## 6. backend·qa가 알아야 할 것

- 소비하는 property: `unsupported_numbers`, `accuracy_warnings`, `needs_review`,
  `was_human_edited`. 필드: `is_published`, `hidden_reason`, `edited_by_human`,
  `llm_original`, `reviewed_by`, `reviewed_at`, `evidence`, `review_warnings`.
  **계약(3.1/3.2)대로 동작하며, 이름이 바뀌면 패널 표시가 조용히 빠진다**(Django 템플릿은
  없는 속성을 조용히 무시한다). 변경 시 알려 달라.
- `llm_original`은 `{'one_line','easy_explanation','why_important'}` 키를 그대로 읽는다.
- `hidden_reason`은 패널 배지에 그대로 노출된다 — 내부 메모 성격이면 그대로도 무방하나
  외부 노출은 없다(웹은 숨김 공시를 아예 제거하므로).
- qa 참고 테스트 포인트: `highlight_terms`의 XSS 이스케이프, `원문에 없음` 판정,
  `has_key` 필터가 `quote_found` 키 부재를 '인용 미검증'으로 처리하는 것.

## 7. 검증

- `manage.py check` → 0 issues.
- `manage.py test disclosures` → **Ran 130 tests, OK**(기존 테스트 무손상).
- 요약 140건 **전건**에 대해 `_review_panel.html` 렌더 성공(예외 0건, 원문 미확보 0건).
- 격리된 in-memory 테스트 DB에 임시 슈퍼유저를 만들어 실제 admin 변경 화면을 GET:
  `200`, 패널·CSS·JS·DART 링크·수정 폼·하이라이트(원문/요약)·evidence 배지 3종·
  숨김 배지·LLM 원본·이스케이프 모두 확인. 개발 DB(`db.sqlite3`)는 건드리지 않았다.
- **runserver 브라우저 확인은 하지 않았다** — 로그인 계정이 없고(auth_user 0건),
  지시대로 계정을 만들지 않았다. 위 서버사이드 렌더 검증으로 대체했다.

## 8. 미해결 · 주의점

1. **CSS·JS의 시각적 확인은 안 됐다.** HTML 구조·클래스는 검증했지만 실제 브라우저에서
   2단 레이아웃과 스크롤 점프가 어떻게 보이는지는 계정이 생긴 뒤 한 번 봐야 한다.
   admin 다크 모드 대비로 색은 admin CSS 변수(`--body-bg`·`--border-color` 등)에 얹었다.
2. **evidence의 인용문은 원문에서 하이라이트하지 않았다.** 인용문은 정규화(`_normalize`/
   `_content_only`)를 거친 문자열이라 원본 `raw_content`와 표 구분자·공백이 달라
   문자열 매칭이 자주 실패한다. 억지로 넣으면 "근거를 원문에서 못 찾았다"는 잘못된 인상을 준다.
   필요하다면 summarizer의 정규화 함수를 태그에서 재사용하는 방식으로 후속 처리 가능.
3. `원문에 없음` 표기 10개 중 일부는 단위 환산(억↔조) 때문일 수 있다. 지금은 문자열 매칭만
   하므로 환산 후보를 제안하지는 않는다.
4. 원문 전체를 항상 렌더한다(잘라내면 검수자가 봐야 할 대목이 잘릴 수 있다).
   현재 최대 원문 기준 문제 없으나, 수십만 자 공시가 들어오면 페이지 무게를 다시 봐야 한다.

## 9. 후속 수정 (2026-08-12) — qa 결함 1건

**`has_key` 필터가 문자열에 대해 부분 문자열 검사로 동작**
(`13_qa_review_verification.md` 1.3, 중요도 낮음).

`in` 연산자를 그대로 써서 dict가 아닌 값이 들어오면 `'quote_found 를 담은 문자열'`처럼
키 이름을 포함한 문자열이 True가 됐다. evidence 항목이 dict가 아닌 옛 형식일 때
검증 배지를 조용히 잘못 표시할 수 있다(예외는 안 나서 화면은 안 깨진다).

수정: `isinstance(value, dict) and key in value`.
"키가 있는가"에 답할 수 없는 입력이므로 dict가 아니면 False가 맞다.
`disclosures/templatetags/review_panel.py` 한 곳만 고쳤다.

확인:
- `manage.py check` → 0 issues.
- 필터 직접 호출 6종(문자열·dict 키 있음/없음·list·None·int) 기대값 일치.
- `manage.py test disclosures` → 208건 중 실패 1건 =
  qa가 **현재(잘못된) 동작을 고정해 둔** `test_has_key_on_a_string_degrades_to_substring_matching`.
  지시대로 `tests.py`는 건드리지 않았다 — qa가 기대값을 뒤집으면 해소된다.
- 같은 실행에서 `AdminReviewScreenTest.test_add_form_does_not_500`이
  **unexpected success**로 뜬다. `@unittest.expectedFailure`로 표시된 backend 쪽 알려진 결함
  ('AI 요약 추가' 저장 시 IntegrityError)이며, 이번 수정과 무관하다(admin.py는 backend 소유).
  이미 고쳐진 것으로 보이므로 데코레이터 제거 여부는 backend·qa가 판단할 몫이다.
