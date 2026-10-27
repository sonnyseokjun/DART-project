# 21 backend — 10단계 운영 모니터링 (이슈 #56)

## 변경 파일

| 파일 | 내용 |
|---|---|
| `disclosures/models.py` | `JobStatus`(+`mark()` · `created_at`) · `PipelineRun` · `AlertState` · `record_job_safely()` · `Disclosure.raw_fetched_at` |
| `disclosures/migrations/0013_monitoring.py` | 위 셋 생성 + `raw_fetched_at` 추가 |
| `disclosures/health.py` (신규) | 판정 단일 출처. `Check` · `run_checks()` · `overall_level()` · `request_backlog()` · `stuck_counts()` · `detect_deadline()` · `memory_available_mb()` · `disk_usage()` · 임계값 상수 |
| `disclosures/monitoring.py` (신규) | `dashboard` 뷰 + `build_context()`(섹션·일별 비용·최근 실행·작업 상태) |
| `disclosures/admin.py` | 세 모델 읽기 전용 등록(추가·수정 불가, 삭제는 허용) · `admin.site.index_template = 'admin/dart_index.html'`(frontend 템플릿 연결) |
| `config/urls.py` | `admin/monitoring/` (`admin_monitoring`) — `admin.site.urls`보다 **앞에** 둔다 |
| `config/settings.py` | `EMAIL_*` · `ALERT_EMAIL_TO` · `SITE_DOMAIN` (env, 빈 값 안전) |
| `.env.example` | 10단계 메일 항목 빈 값으로 추가 |
| `management/commands/record_job.py` (신규) | `record_job <kind> [--failed] [--message]` |
| `management/commands/record_run.py` (신규) | `record_run --mode --started-at --exit-code [--mem-min-mb] [--message]` |
| `management/commands/check_health.py` (신규) | `--dry-run` · `--test`. 전환 판정 · 메일 묶음 · 상태 저장 · 하루 1회 90일 정리 |
| `management/commands/poll_dart.py` | `handle` → `_poll` 감싸기. `--detect`=detect, 구간 옵션 없음=full_poll, `--bgn/--end`=기록 안 함. 종료 9도 성공 |
| `management/commands/sync_listed_corps.py` | 성공 시 `sync_listed` 기록 |
| `management/commands/fetch_documents.py` | 성공 시 `raw_fetched_at` 저장 (record_run의 "원문 확보" 건수용) |
| `deploy/pipeline.sh` | `on_exit` trap 하나로 통합(표본기 정리 + 실행 기록). `RUN_MODE`가 정해진 실행만 기록 |
| `deploy/backup.sh` | 끝에 `flock -w 600 … record_job backup --message s3://…`. 실패해도 종료 코드 불변 |
| `deploy/crontab` | `3-59/10 * * * *  sleep 30 && … flock -w 120 … check_health >> /var/log/dart/health.log` |

계약(00_input의 context) **변경 없음.** 추가로만 넣은 것: `JobStatus.created_at`, `Disclosure.raw_fetched_at`,
`PipelineRun.duration_seconds`(property, 정수 초, `finished_at`이 없으면 None — frontend 요청).
`jobs`에는 기록이 없는 종류도 저장하지 않은 빈 `JobStatus(kind=…)`로 넣는다 → 템플릿의 "기록 없음"이 그대로 보인다.

## 실행한 검증

- `manage.py check` — 문제 없음
- `manage.py makemigrations --check --dry-run` — No changes detected
- `manage.py test` 전체 — 결과는 아래 "기존 테스트" 절
- 스크래치 스모크 테스트(저장소 밖, 테스트 DB): 감지 기한 7사례 · 메일 전환 1통/중복 0/복구 1통 · 미설정 시 상태 미저장 · 요청 판정 · record_run 건수 · 대시보드 200/비로그인 302 · admin 첫 화면 링크 · poll_dart 종료 9 → detect 성공 기록 — 모두 통과
- `pipeline.sh`를 stub(docker·flock·free)으로 13경로 실행 — 종료 코드 전부 기존과 동일, `record_run`은 일이 있는 경로에서만 호출:

| 경로 | 종료 | record_run |
|---|---|---|
| 감지 신규 없음 + 대기 없음 | 0 | 없음 |
| 감지 신규 → 전 단계 성공 | 0 | detect, exit 0, mem |
| 감지 신규 → 요약 실패 | 1 | detect, exit 1, mem |
| 감지 신규 → record_run 실패 | **0** (로그 한 줄) | 시도함 |
| 감지 신규 없음 + 대기 있음 | 0 | resume |
| --requests 할 일 없음 | 0 | 없음 |
| --requests 할 일 있음 | 0 | requests |
| --full 수집 실패 | 1 | full, exit 1 (mem 없음) |
| --full 성공 | 0 | full |
| 감지 실패 | 1 | 없음 (JobStatus 실패로만) |
| 잠금 실패 -n / -w | 0 / 1 | 없음 |
| pending_work 오류 | 2 | 없음 |
| flock 미설치 | 127 | 없음 |

## 판단이 갈린 지점과 이유

### 1. `requests` 판정 — "처리할 수 있는가"를 retry_policy에 과거 시각으로 묻는다

요청됐고 요약이 없는 공시를 `request_backlog()`가 나눈다.

- 원문 미확보(`unfetched_targets`): 상한 → stuck / `is_retry_due(…, now − 5분)`이 거짓 → **사다리 대기(정상)** / 참이고 요청 15분 경과 → **overdue**
- 원문 확보·요약 없음(`unsummarized_targets`): 상한 → stuck / 월 상한 소진 → 보류(정상, ai_budget이 잡는다) / 요약 재시도 대기 → 정상 / 그 밖 + 15분 → **overdue**

`now − REQUEST_OVERDUE_GRACE(5분)`로 묻는 이유: "5분 전에 이미 만기"인 것만 처리 가능으로 친다. 막 만기가 된 건은 다음 분 실행이
처리하므로, 점검이 그보다 먼저 잠금을 잡았다고 오보가 나면 안 된다. 시도 0회는 `is_retry_due`가 항상 참 → 15분 넘게 한 번도 시도되지
않은 요청은 이상(파이프라인이 안 돈다는 뜻).

알려진 틈: 월 상한 직전(`can_afford` 실패, `is_exhausted` 아님)에는 요약이 건너뛰어지는데 overdue로 잡힌다. 그때는 ai_budget도 거의
확실히 주의(80%↑)라 메일에 둘이 같이 뜬다 — 오보라기보다 "요청이 실제로 처리되지 않고 있다"는 사실이라 그대로 뒀다.

### 2. `stuck` — 최근 7일 안에 마지막으로 시도한 것만 센다

상한에 걸린 공시는 사람이 되살리기 전까지 영원히 남는다. 기간 없이 세면 한 번 걸린 뒤로 "주의"가 풀리지 않고, 그 다음에 새로 걸린 건은
전환이 아니라 알림을 받지 못한다. `STUCK_WINDOW = 7일`. 또 **원문은 받았는데 전처리 결과가 빈 요청**도 센다 — `unsummarized_targets`가
`raw_content=''`를 빼므로 재시도도 상한도 없이 화면에 "정리 중"으로 영원히 남는 구멍이었다.

### 3. `detect` 기한 — 마지막 성공이 아니라 "다음 예정 실행"에서 잰다

`detect_deadline(L) = min(첫 예정 실행 + 그 시간대 여유, 첫 매분 실행 + 10분)`. 여유는 매분 시간대 10분, 그 외 1시간 10분
→ 평소엔 00_input의 10분 / 2시간 10분과 같다(매분 쪽은 최대 11분). 시간대 경계:

| 마지막 성공 | 기한 |
|---|---|
| 월 08:00:30 | 월 09:10 (09:00~09:10 오탐 없음) |
| 월 10:00:20 | 10:11 |
| 월 20:00:40 | 22:10 |
| 금 18:59:20 | 금 20:10 (매분 → 정각 경계. 처음 버전은 19:10으로 잘못 나왔다 — 스모크에서 잡아 고침) |
| 토 11:00:10 | 13:10 |
| 일 23:00:10 | 월 01:10 |

cron 일정은 `ACTIVE_WEEKDAYS`·`ACTIVE_HOURS`·`next_detect_slot`에 옮겨 적었다. crontab을 바꾸면 함께 고쳐야 한다(상수 주석에 적음).
공휴일은 cron도 구분하지 않으므로 여기서도 구분하지 않는다.

### 4. 기록 없음 처리 — `JobStatus.created_at`을 더했다

성공 기록이 없을 때 "판정 불가"로만 두면 **배포 후 한 번도 돈 적 없는 백업이 영원히 알림 없이 넘어간다**(이 프로젝트의 사고 유형 그대로).
`check_health`가 첫 실행에서 모든 종류의 행을 만들고(`ensure_job_rows`), 성공이 없으면 행이 생긴 때부터 잰다. `--dry-run`은 행을
만들지 않으므로 그때만 `unknown`이 보인다.

### 5. 메일은 "나간 뒤에만" 상태 저장

- 발송 실패: 상태를 저장하지 않고(다음 점검에서 다시 시도) `health_check` **실패**로 기록, 종료 1. 성공 시각이 멈추므로 대시보드
  "마지막 점검"이 25분 뒤 주의가 된다.
- 메일 미설정: 경고 + 보낼 메일 본문을 로그에 찍고 **상태 저장 안 함**, 종료 0. 나중에 설정하면 그때의 이상이 첫 점검에서 한 번 나간다.
  대가: 미설정 서버에서는 health.log에 같은 본문이 10분마다 찍힌다.
- `unknown`은 전환으로 치지 않고 지난 상태도 건드리지 않는다(측정 한 번 실패로 "복구"를 보내지 않게).
- 주의 ↔ 이상 변화도 "이상" 메일로 보낸다(심해졌거나 누그러짐).

### 6. pipeline.sh 종료 코드 보존

- trap을 `on_exit` 하나로 합쳤다. bash trap은 신호마다 하나라, 표본기 줄에서 다시 걸면 기록 trap이 지워진다.
- `on_exit`은 첫 줄에서 `rc=$?`를 잡고 `set +e`로 돈 뒤 `exit "$rc"`로 끝난다. 기록 실패는 `|| log …`.
- `RUN_MODE`는 "할 일이 있다"가 확정되는 지점에서만 채운다: `--requests`/재개는 pending_work 0 뒤, 감지는 poll_dart 0 뒤,
  `--full`은 수집 **전**(하루 1회라 실패도 남긴다).
- **감지 실패(poll_dart ≠ 0·9)는 PipelineRun에 남기지 않았다.** DART 장애 중엔 매분 실패라 헛도는 실행과 같은 누적 문제가 된다.
  poll_dart가 `JobStatus.detect.last_failure_at`을 적고, 판정은 freshness가 한다.
- `RUN_STARTED`는 잠금을 잡은 **직후**에 찍는다(qa 결함 보고로 수정). 잠금 전에 찍으면 `--wait`/`--full`이 최대 600초 기다리는 동안 앞 실행의 공시·요약을 이중 집계하고 소요 시간에 대기가 들어간다.
- `mem_min=""` 초기화를 두지 않았다 — 기존 테스트가 `mem_min=`의 첫 등장을 본문 집계 줄로 찾는다. trap에서는 `${mem_min:-}`.

### 7. check_health cron에 `sleep 30`

`3-59/10`은 정각·06:50·07:05와 안 겹치지만 **평일 낮 매분 감지·밤 요청 줄과는 늘 같은 분**이다. 그 둘은 `flock -n`이라 점검이
먼저 잠금을 잡으면 그 분의 감지가 "정상"을 찍고 건너뛰어진다(주의 3의 모양 그대로, 10분에 한 번). 30초 늦게 띄워 파이프라인이
먼저 잡게 하고 점검은 `-w 120`으로 기다린다.

### 8. 대시보드 세부

- 회원 = `is_staff=False, is_superuser=False`. 최근 7일 접속은 `last_login`.
- "마지막 점검" 항목은 25분(`HEALTH_CHECK_MAX_AGE`) 넘으면 주의. 메일 판정(checks)에는 넣지 않았다 — 점검이 멈췄으면 그 메일을
  보낼 주체도 멈춰 있다. 외부 감시(UptimeRobot)의 몫.
- 요청→완료 중앙값은 `summary.created_at − summary_requested_at`, 요청보다 먼저 만든 요약(8단계 전 자동 요약)은 뺀다.
- 일별 비용은 30일 전부(0원인 날 포함), `ratio`는 기간 최대값 대비.
- 개인정보: 숫자만. 공시 접수번호는 요청 지연 detail에 최대 5개 나온다(공개 정보).

## 기존 테스트 — 깨진 것 (리더 판단 필요)

`disclosures/tests.py` `PipelineScriptTest.test_sampler_cleanup_cannot_change_the_exit_code`가 **`trap `으로 시작하는 줄 자체**에
`MEM_SAMPLER` · `|| true` · `rm -f`가 있는지 본다. 지금 trap 줄은 `trap on_exit EXIT`이고 그 정리는 `on_exit` 함수 안(`set +e` +
`exit "$rc"`)에 있다. 테스트의 의도(뒤처리가 종료 코드를 오염시키지 않는다)는 지켜지고 stub 실행으로도 확인했지만 문자열 검사라 실패한다.
tests.py는 내 소유가 아니라 고치지 않았다. 제안: 그 테스트를 `on_exit() {` 본문에서 `local rc=$?`가 첫 명령 · `set +e` · `exit "$rc"`를
보는 것으로 바꾼다(qa의 test_monitoring.py로 옮겨도 된다).

## qa가 볼 경계 사례

1. `detect_deadline` 위 표 6건 + 월 08:30(정각 아님) → 09:10 · 금 18:59 경계 · 일→월 경계
2. `requests`: 시도 0회 + 요청 16분 전 → bad / 사다리 대기 → ok / 만기 3분 전 지남 → ok(유예) · 6분 → bad / 요약 상한·원문 상한 → ok(stuck으로) /
   월 상한 소진 + 원문 있음 → ok · 원문 없음은 여전히 판정 대상 / 요청 14분 전 → ok
3. `stuck`: 상한 + 마지막 시도 8일 전 → 안 셈 / 빈 원문 요청 → 셈
4. 메모리 `/proc/meminfo` 없음 → unknown, 전환 없음, AlertState 그대로 / 149·150·249·250MB 경계(`<` 기준)
5. 디스크 80%·90% 정확히 → 그 아래 단계(`>` 기준) / `disk_usage` OSError → unknown
6. ai_budget: 80% 정확히 → warn(`>=`) / 예산 0 → bad
7. 전환: ok→bad 1통 · bad 유지 0통 · bad→ok 1통 · warn→bad 1통 · 여러 항목 → 1통(제목에 이상/복구 함께) · unknown 끼어도 상태 유지
8. 메일 미설정 → 종료 0, AlertState 미변경, health_check 성공 기록 / SMTP 예외 → 종료 1(CommandError), AlertState 미변경, health_check 실패 기록
9. `--dry-run` → JobStatus·AlertState·PipelineRun 모두 쓰기 0, outbox 0
10. `--test` 미설정 → CommandError / 설정 → 1통
11. `poll_dart --detect` 종료 9 → detect 성공 / DartApiError → detect 실패 기록 + 예외 그대로 / `--bgn` 백필 → 기록 없음 / `--days` → full_poll
12. 기록 자체가 예외 → poll_dart 결과 불변(`record_job_safely`)
13. `record_run` 건수: started 이후 생성 공시 · `raw_fetched_at` · 요약 생성. naive 시각 · 잘못된 형식 → CommandError
14. PipelineRun 90일 정리는 그날 첫 점검에서만
15. `/admin/monitoring/`: 비로그인 302 · 일반 회원(staff 아님) 302 · staff 200 · `dart.*`/`summarizer.*` 미호출(patch로 확인)
16. pipeline.sh: 위 13경로 표 (stub 방식은 `bin/docker`가 `shift 6` 후 명령 이름별 `RC_<명령>`으로 종료)

## 남은 일

- 리더: RUNBOOK에 모니터링 절 추가 시 `check_health.RUNBOOK_REFS`의 절 이름을 맞출 것(지금은 기존 5장 제목을 가리킨다).
  서버 반영 순서: `.env`에 메일 5항목 → 재빌드(0013 적용) → `check_health --test` → cron 교체. logrotate는 `*.log` 와일드카드라 health.log도 자동 포함.
- 위 깨진 테스트 1건 처리.
