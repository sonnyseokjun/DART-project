# 00 입력 명세 — 10단계 운영 모니터링 (이슈 #56)

브랜치: `feat/stage10-monitoring` · 설계: PLAN.md 9.6 · 절대 제약은 오케스트레이터 스킬 참조(여기 다시 적지 않는다).

## 왜 하는가

지인 베타테스트를 연다. 지금 운영 점검은 운영자가 SSH로 명령 열 개를 돌려 보는 방식이다.
이 프로젝트의 큰 사고 셋(전체 폴링 이틀 미실행 · 재시도 사다리 5일 지연 · 1분 칸이 2분마다)은
**모두 로그가 정상으로 보이는 채** 며칠씩 갔다(PLAN.md 9.3). 목표는 **운영자가 사용자보다 먼저 안다.**

## 확정된 설계 결정 (다시 논의하지 않는다)

1. **판정은 "실패"가 아니라 "마지막 성공이 너무 오래됨"(freshness)** 으로 한다. 실패 기록은 보조다.
2. **헛도는 실행은 행을 쌓지 않는다.** 작업 종류별 "마지막 성공"은 한 행을 덮어쓴다(`JobStatus`).
   일이 있었던 파이프라인 실행만 이력(`PipelineRun`)으로 쌓는다.
3. **`pending_work`는 계속 읽기 전용이다.** 밤 `--requests` 매분 실행에 쓰기를 붙이지 않는다.
   요청 처리의 정상 여부는 "처리 가능한데 15분 넘게 그대로인 요청"으로 판정한다.
4. **DB 쓰기 주체는 하나.** 파이프라인 밖에서 쓰는 기록(백업 · 점검 명령)은
   `flock -w … /tmp/dart-pipeline.lock`으로 같은 잠금을 잡는다(crontab의 sync_listed_corps 줄과 같은 방식).
5. **판정 규칙의 단일 출처는 새 모듈 `disclosures/health.py`.** 메일 알림과 대시보드가 같은 함수를 쓴다.
   임계값은 모듈 상단 상수로 둔다.
6. **같은 이상은 한 번만 알린다** — 정상→이상 전환 때 1통, 복구 때 1통(`AlertState`).
7. **메일은 Gmail SMTP(587, TLS, 앱 비밀번호).** 설정은 `.env`. 없으면 메일 없이 경고만 출력하고 정상 종료.
8. **대시보드는 `/admin/monitoring/`** — Caddy의 `/admin/*` IP 화이트리스트를 그대로 물려받는다.
   staff 전용. DART·LLM 미호출. JS 없음. 개인정보는 숫자로만.
9. 컨테이너에는 `data/`만 마운트돼 있어 `/var/log/dart`를 못 읽는다. 로그 파일을 읽는 설계를 하지 않는다.
   메모리는 `/proc/meminfo`의 `MemAvailable`(컨테이너 메모리 제한이 없어 호스트 값이 보인다),
   디스크는 `shutil.disk_usage`(DB가 있는 디렉터리)로 직접 읽는다.

## 점검 항목과 임계값 (health.py 상수)

| key | 이상(bad) 조건 | 주의(warn) | 비고 |
|---|---|---|---|
| `detect` | 마지막 감지 성공이 평일 09~18시면 10분, 그 외 2시간 10분 초과 | — | 시각 판정은 `timezone.localtime`. 시간대 경계 직후(09:00~09:10)에 오탐하지 않게 할 것 |
| `full_poll` | 마지막 전체 폴링 성공 26시간 초과 | — | 07:05 일 1회 |
| `backup` | 마지막 백업 성공 26시간 초과 | — | 04:00 일 1회 |
| `requests` | **처리 가능한데** 요약 요청 후 15분 넘게 요약이 없는 공시 ≥1 | — | DART 원문 미공개로 사다리 대기 중인 건은 정상(대기)이다. 월 상한 소진은 `ai_budget`에서 따로 잡는다 |
| `stuck` | — | 재시도 상한에 걸린 원문·요약 ≥1 | `retry_policy` 재사용 |
| `ai_budget` | 상한 소진 | 이번 달 사용 ≥ 80% | `ai_budget` 재사용 |
| `memory` | MemAvailable < 150MB | < 250MB | 측정 실패 시 판정 불가(None)로 두고 알리지 않는다 |
| `disk` | 사용률 > 90% | > 80% | |

판정 결과 타입(제안): `Check(key, title, level: 'ok'|'warn'|'bad'|'unknown', summary: str, detail: str)`.
메일은 level이 `bad`·`warn`인 항목의 **전환**에만 보낸다.

## 데이터 (models.py · 새 마이그레이션)

- `JobStatus` — 작업 종류별 한 행. `kind`(unique, choices: detect·full_poll·backup·sync_listed·health_check),
  `last_success_at`, `last_failure_at`, `last_message`, `updated_at`.
- `PipelineRun` — 일이 있었던 실행만. `mode`(detect·resume·full·requests), `started_at`, `finished_at`,
  `ok`, `exit_code`, `new_disclosures`, `fetched`, `summarized`, `mem_min_mb`(null), `message`.
  건수는 실행 끝에 DB에서 `started_at` 이후 변화로 계산한다(스크립트가 출력을 파싱하지 않는다).
  90일 지난 행은 `check_health`가 하루 한 번 정리한다.
- `AlertState` — `key`(unique), `level`, `since`, `last_sent_at`, `detail`.
- 모두 한국어 `verbose_name`. admin에는 읽기 전용으로 등록(추가·수정 불가).

## 기록 지점

| 지점 | 방식 |
|---|---|
| 감지·전체 폴링 성공 | `poll_dart`가 끝에서 `JobStatus` 갱신. `--detect` "신규 없음"(종료 9)도 **성공**이다 |
| 일이 있었던 파이프라인 실행 | `pipeline.sh`가 뒷단계 끝(또는 EXIT trap에서 실패 시)에 `record_run` 명령 호출. 이미 잠금 안이다. **기록 실패가 파이프라인을 실패로 만들면 안 된다**(`|| log …`) |
| 백업 성공 | `backup.sh` 끝에서 `flock -w 600` 잡고 `record_job backup --message <S3 경로>` |
| 상장사 명단 | `sync_listed_corps`가 끝에서 `JobStatus` 갱신 (이미 잠금 안에서 돈다) |
| 점검 자체 | `check_health`가 실행마다 `health_check` 갱신 → 대시보드가 "감시자가 살아 있나"를 보여 준다 |

## 알림 (`check_health` 명령)

- cron: `3-59/10 * * * *` + `flock -w 120` 같은 잠금. 로그는 `/var/log/dart/health.log`.
  (매시 00분 감지 · 06:50 · 07:05 줄과 분이 겹치지 않는다 — PLAN.md 9.3 주의 3)
- 옵션: `--dry-run`(판정만 출력, 메일·상태 저장 없음), `--test`(시험 메일 1통 발송).
- 메일 제목 `[DART 알림] 이상: …` / `[DART 알림] 복구: …`. 본문에 대시보드 URL(`https://{SITE_DOMAIN}/admin/monitoring/`)과
  RUNBOOK 대응 절을 적는다. 한 번 점검에서 여러 전환이 생기면 **한 통으로 묶는다.**
- `.env` 새 항목: `EMAIL_HOST`(기본 smtp.gmail.com) · `EMAIL_PORT`(587) · `EMAIL_HOST_USER` · `EMAIL_HOST_PASSWORD` ·
  `ALERT_EMAIL_TO`(쉼표 구분). `.env.example`에 빈 값으로 추가. **실제 `.env`는 건드리지 않는다.**

## 대시보드 — 뷰와 템플릿 사이 계약 (frontend·backend 병렬 작업의 경계)

URL `admin/monitoring/` (이름 `admin_monitoring`), `admin.site.admin_view`로 감싼다. admin 첫 화면에 링크.
템플릿 `disclosures/templates/admin/monitoring.html`, `admin/base_site.html` 확장.

```python
context = {
    **admin.site.each_context(request),
    'title': '운영 모니터링',
    'checked_at': datetime,                  # 판정 시각 (aware)
    'overall': 'ok' | 'warn' | 'bad',        # 가장 나쁜 level ('unknown'은 무시)
    'checks': [Check, ...],                  # health.py 판정 결과, 위 표 순서
    'sections': [                            # 숫자 패널
        {'key': 'service',  'title': '서비스',     'items': [Item, ...]},
        {'key': 'pipeline', 'title': '파이프라인', 'items': [...]},
        {'key': 'summary',  'title': '요약',       'items': [...]},
        {'key': 'cost',     'title': '비용',       'items': [...]},
        {'key': 'server',   'title': '서버',       'items': [...]},
    ],
    # Item = {'label': str, 'value': str(이미 포맷됨), 'level': 'ok'|'warn'|'bad'|None, 'hint': str}
    'daily_cost': [{'date': date, 'cost_usd': float, 'ratio': float(0~1, 기간 최대값 대비)}],  # 최근 30일
    'recent_runs': [PipelineRun, ...],       # 최근 20건, 최신 먼저
    'jobs': [JobStatus, ...],                # 종류별 마지막 성공·실패
}
```

섹션 항목(값은 backend가 문자열로 포맷한다): 서비스 — 회원 수(오늘 신규) · 최근 7일 접속 · 관심 기업 수 · 추적 기업 수 /
파이프라인 — 마지막 감지 · 마지막 전체 폴링 · 오늘 감지 공시 · 마지막 점검 / 요약 — 요청 대기 · 원문 대기(사다리) · 포기 ·
요청→완료 중앙값(최근 30일) · 경고 붙은 요약 · 미검수 / 비용 — 이번 달 / 상한 · 남은 금액 / 서버 — 메모리 가용 · 디스크 · 마지막 백업.

## 파일 소유권

| 에이전트 | 소유 |
|---|---|
| backend | `disclosures/models.py` · `migrations/**` · `admin.py` · 새 `health.py` · 새 `monitoring.py`(뷰) · `management/commands/**`(poll_dart·sync_listed_corps 수정, record_run·record_job·check_health 신설) · `config/settings.py` · `config/urls.py` · `deploy/pipeline.sh` · `deploy/backup.sh` · `deploy/crontab` · `.env.example` |
| frontend | `disclosures/templates/admin/monitoring.html` · admin 첫 화면 링크용 템플릿(`disclosures/templates/admin/index.html` 확장 등) |
| qa | 새 `disclosures/test_monitoring.py` (기존 `tests.py`는 건드리지 않는다. 기존 테스트가 깨지면 담당자에게 보고) |
| 리더 | `docs/RUNBOOK.md` · `PLAN.md` · `CLAUDE.md` · 커밋 |

## 완료 조건

- backend: 위 모델·기록 지점·명령·뷰가 동작. `manage.py check`·`makemigrations --check` 통과. 산출물 `_workspace/21_backend_monitoring.md`
  (판단이 갈린 지점과 이유 포함 — 특히 `requests` 판정과 pipeline.sh 종료 코드 보존).
- frontend: 계약대로 렌더링. 브라우저로 띄워 확인(CLAUDE.md: 화면은 반드시 눈으로). 산출물 `_workspace/22_frontend_dashboard.md`.
- qa: 판정 경계(시간대 경계 · 임계값 경계 · 측정 불가) · 알림 전환/중복 억제 · 메일 미설정 시 정상 종료 ·
  staff 외 접근 차단 · 대시보드가 DART/LLM을 부르지 않음 · pipeline.sh 기존 종료 코드 의미 유지. 전체 테스트 통과. 산출물 `_workspace/30_qa_monitoring.md`.

## 성공 지표

- 서버 반영 후 시험 메일 수신, 대시보드가 한 화면에 전 항목 표시.
- 의도적으로 `JobStatus.detect`를 오래된 값으로 만들면 다음 점검에서 메일 1통, 되돌리면 복구 메일 1통, 그 사이 추가 메일 0통.
