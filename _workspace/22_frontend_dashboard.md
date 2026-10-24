# 22 frontend — 운영 모니터링 대시보드 (이슈 #56)

## 변경 파일

| 파일 | 내용 |
|---|---|
| `disclosures/templates/admin/monitoring.html` (신규) | `/admin/monitoring/` 화면. `admin/base_site.html` 확장. 계약 context만 사용 |
| `disclosures/templates/admin/dart_index.html` (신규) | admin 첫 화면 위에 "운영 모니터링 ›" 링크 한 줄. `admin/index.html` 확장 |

**backend에 요청한 한 줄(admin.py 소유):** `admin.site.index_template = 'admin/dart_index.html'`.
INSTALLED_APPS에서 `disclosures`가 `django.contrib.admin` 뒤라, `disclosures/templates/admin/index.html`
같은 이름으로 덮어쓰면 admin 기본 템플릿이 이긴다(앱 디렉터리 로더는 앞 앱 우선). 설정 순서를 바꾸는 것보다
`index_template` 지정이 영향 범위가 작다.

## 화면 구성 (위에서 아래)

1. **전체 상태 한 줄** — 큰 글자 `정상/주의/이상` + 12px 네모. 이상·주의면 **해당 항목 이름까지** 같은 줄에 나열
   (아래 목록을 훑지 않아도 무엇이 깨졌는지 보인다). 오른쪽 끝에 판정 시각. `bad`면 위아래 선이 1px→2px로 굵어진다.
2. **점검 항목(`checks`)** — 표가 아니라 줄 목록: 상태 · 제목 · 요약(+ detail은 회색 작은 글자, 줄바꿈 보존).
   `bad` 줄은 요약을 굵게.
3. **숫자 패널(`sections`) 다섯** — `auto-fill, minmax(260px, 1fr)` 그리드. 위 2px 먹 선 + 항목은 dt(라벨)/dd(값 18px 굵게 + level 표시 + hint).
4. **일별 AI 비용** — `ratio`로 높이를 잡는 CSS 막대 30개(`widthratio d.ratio 1 100`). 최댓값과 그 날짜를 머리에 적고
   (`dictsortreversed:"cost_usd"|first`), 0원인 날은 1px 회색, 아주 작은 값은 최소 2px. 막대는 `aria-hidden`이고
   같은 값을 `<details>` 표로 접어 둔다(JS 없음).
5. **최근 실행(`recent_runs`)** · 6. **작업별(`jobs`)** — 표. 좁은 화면에서는 표만 `overflow-x: auto`로 가로 이동, 페이지는 넘치지 않는다.
   실행의 결과 칸은 `성공`/`실패` 글자 + 네모 + `종료 N`. 미완료(`finished_at` 없음)·메모리 미측정은 `—`.
   작업의 마지막 성공이 없으면 점선 네모 `기록 없음`, 있으면 시각 + `(N 전)`(`timesince:checked_at`).
7. 데이터가 없으면 각 블록이 빈 상태 문장을 보인다(실행 기록 없음은 "헛도는 실행은 기록하지 않으므로 정상일 수 있다"고 적음).

## 디자인 판단과 이유

- **상태는 색 + 글자 + 모양.** DESIGN.md "Gauge and status"대로 색은 8px 네모에만 두고 글자는 먹. 네모 옆에 반드시
  `정상·주의·이상·판정 불가` 글자를 붙인다 — 9.5에서 색 배지를 버린 이유(색만으로는 구분이 안 됨)를 이 화면에서 되살리지 않기 위해.
  `unknown`(판정 불가)은 점선 빈 네모 + 회색 글자 — DESIGN.md 적용 규칙 2의 "없음의 신호는 면을 칠하지 않는다"와 같은 규칙.
  전체 상태 `bad`는 색 외에 선 굵기(2px)로 한 번 더 말한다.
- **중요도 게이지(칸 수)는 쓰지 않았다.** 칸 수는 이 서비스에서 "공시 중요도"의 뜻으로 묶여 있어, 심각도에 빌려 쓰면 뜻이 둘이 된다(적용 규칙 5 "모양만 빌려 다른 뜻으로 쓰지 않는다").
- **파랑을 쓰지 않았다.** 파랑의 자리는 1차 버튼·현재 메뉴·초점뿐(적용 규칙 4). 이 화면에는 셋 다 없다. 막대도 먹색.
- **admin 안이므로 style.css를 싣지 않고 admin CSS 변수를 우선 쓴다**(`--body-fg`·`--body-quiet-color`·`--hairline-color`·`--border-color`).
  기존 검수 패널(review-panel.css)과 같은 방침이다 — admin 다크 모드에서 글자가 읽힌다. 의미 색 셋만 DESIGN.md 값으로 고정.
  스타일은 이 화면 전용이라 별도 정적 파일 없이 템플릿의 `extrastyle`에 두고 `mon-` 접두어를 붙였다(파일 소유권도 템플릿뿐).
- **반경 0, 그림자 없음, 선으로 구획**(DESIGN.md). 패널은 카드가 아니라 위 2px 먹 선.
- **한글 자간**: h2를 처음에 1.5px로 줬다가 "점 검 항 목"처럼 흩어져 0.5px로 줄였다(적용 규칙 6 — 1.5px는 두 어절 이내 라벨에만).

## 브라우저 확인 (claude-in-chrome)

뷰가 아직 없을 때 계약 그대로의 가짜 context로 렌더링했다 — scratchpad의 `render_mon.py`가
`admin.site.each_context(request)` + `render_to_string('admin/monitoring.html', …)`로 세 변형(bad/ok/empty)을
HTML로 만들고 admin 정적 파일을 복사해 `127.0.0.1:8765`로 띄웠다. 실 DB에는 쓰지 않았다.

| 폭 | 변형 | 테마 | 본 것 |
|---|---|---|---|
| 1536px(창 전체) | bad | 다크 | 전체 상태 줄·점검 8항목·패널 4+1열·막대·실행 20행·작업 5행 |
| 1536px | bad | 라이트(`data-theme=light`) | 같은 화면. 막대·선·글자 대비 |
| 368px(iframe 375) | bad, ok | 다크 | 점검 줄 접힘, 패널 1열, 막대 30개 폭, 표 내부 가로 스크롤 |
| 754px(iframe 768) | empty | 다크 | 빈 상태 문장 3곳, 패널 2열 |

**실제 뷰로도 렌더링했다** — backend의 `monitoring.dashboard`·URL `admin_monitoring`·`index_template`가 생긴 뒤,
scratchpad의 `real_view.py`가 메모리 테스트 DB(`create_test_db`)에 마이그레이션을 적용하고 staff로 `force_login`해
`/admin/`·`/admin/monitoring/`을 GET했다(둘 다 200, 비로그인은 302 → `/admin/login/`, `{# #}` 누출 없음).
계약 불일치는 없었다 — `Check`(dataclass)·`Item` dict·`daily_cost`·`PipelineRun.get_mode_display`·
`JobStatus.get_kind_display`(저장되지 않은 빈 행 포함)가 모두 템플릿 기대와 맞는다.

| 폭 | 대상 | 본 것 |
|---|---|---|
| 1536px | 실제 `/admin/monitoring/` | 감지 이상·디스크 이상(개발 PC 97%)·메모리 판정 불가(Windows라 `/proc/meminfo` 없음)·비용 전부 0 |
| 1536px · 368px | 실제 `/admin/` | "운영 모니터링 ›" 링크 줄이 앱 목록 위에 붙음 |
| 368px · 754px | 실제 `/admin/monitoring/` | 실행 3행·작업 5행(기록 없음 행 포함) 표 내부 가로 스크롤 |

좁은 폭에서 문서 `scrollWidth`(353) ≤ `innerWidth`(368) — 페이지 가로 넘침 없음을 JS로 확인했다.
`resize_window`가 창 폭을 줄이지 못해(innerWidth가 1536 그대로) 좁은 폭은 iframe으로 봤다.

## 발견해 고친 결함

1. h2 자간 1.5px로 한글 제목이 낱자로 흩어짐 → 0.5px.
2. 좁은 폭에서 점검 줄이 상태/제목/요약 세 줄로 쪼개져 8항목이 화면 세 장을 차지 → 상태+제목 한 줄, 요약은 아래로.
3. 전체 상태 줄의 이상·주의 항목이 줄바꿈 중간에서 "이상"과 제목이 떨어짐 → 항목 단위 `nowrap` 묶음.
4. 비용 막대 머리의 "최댓값 대비"가 최댓값을 말하지 않아 높이가 무엇인지 알 수 없음 → 최댓값 금액과 날짜 표시.
5. (실제 뷰에서 발견) 30일 모두 $0이면 머리에 "최댓값 $0.0000 (09-25)"가 떠서 첫날에 무슨 일이 있었던 것처럼 읽힘
   → 전부 0이면 "이 기간에 AI 사용이 없다 — 모든 날이 $0"으로 바꾼다.
6. 0이 아닌데 매우 작은 날은 `widthratio`가 0%로 반올림돼 0원과 같아 보임 → 최소 2px(0원은 1px 회색으로 구분).

## backend에 전달할 제안 (계약 변경 아님)

- 서버 패널 "마지막 백업" — 가짜 데이터로 값에 S3 경로를 넣어 보니 18px 굵은 글자로 375px에서 세 줄로 접혔다.
  실제 뷰(`_job_item`)는 값을 "N분 전"으로 짧게 하고 시각을 hint로 두고 있어 문제없다. 값은 짧게, 긴 문자열은 hint로 — 이 원칙을 유지해 달라.
- 실행 표에 소요 시간이 없다. `timesince`는 분 단위라 21초 실행이 "0분"으로 나와 쓰지 않고 시작·종료 시각을 따로 보였다.
  필요하면 `PipelineRun`에 초 단위 property(예: `duration_seconds`)를 추가해 주면 칸을 바꾸겠다.

## 테스트

`manage.py test disclosures.test_templates` — 5개 통과. (처음 돌렸을 때는 backend가 모델을 고치고 마이그레이션을
아직 만들기 전이라 `raw_fetched_at` 컬럼 없음으로 3개가 에러였다. `0013_monitoring.py`가 생긴 뒤 다시 돌려 통과.)
새 템플릿 둘의 여러 줄 메모는 모두 `{% comment %}`다.

## 후속 — 소요 시간 칸

backend가 `PipelineRun.duration_seconds`(정수 초, 미완료면 None)를 추가해, 실행 표에 "소요" 칸(`N초` / `—`)을 넣었다.
위 "backend에 전달할 제안"의 둘째 항목은 이것으로 해소됐다.
