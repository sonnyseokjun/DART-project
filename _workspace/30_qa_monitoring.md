# 30 qa — 10단계 운영 모니터링 검증 (이슈 #56)

## 결과

- `manage.py check` — 문제 없음
- `manage.py makemigrations --check --dry-run` — No changes detected
- `manage.py test` 전체 — **Ran 638 tests, OK** (새 `disclosures/test_monitoring.py` 67개 포함)
- 기존 실패 `PipelineScriptTest.test_sampler_cleanup_cannot_change_the_exit_code` — 의도(표본기 정리 실패가 종료 코드를 바꾸지 않음 · 표본 파일 삭제)가
  **실제 실행으로 지켜지는 것을 확인한 뒤** 새 구조(`trap on_exit EXIT` 하나 · `local rc=$?` 첫 명령 · `set +e` · `kill`/`rm -f` · `exit "$rc"`)를 보도록 고쳤다.
  실행 확인은 `test_monitoring.PipelineRunRecordingTest`(stub 실행, 표본 파일 잔존 0 · 종료 코드 보존)가 한다.

## 검증 항목 (test_monitoring.py)

| 영역 | 클래스 | 본 것 |
|---|---|---|
| 감지 판정 | `DetectDeadlineTest` | 기한 표 7건 · 월 09:00~09:10 매분 오탐 없음 · 매분/그 외 임계 바로 위·아래(1초) · 금 18:59 이후 · 토요일 09시대 · 일→월 · 행 없음=unknown · 성공 없음은 행 생성 시각부터 · 실패는 detail만 |
| 하루 1회 | `DailyJobCheckTest` | 전체 폴링·백업 26h 정확히=ok, +1초=bad, 기록 없음=unknown |
| 요청 판정 | `RequestCheckTest` | 시도 0회 14분 ok/16분 bad · 사다리 대기(30분 칸) ok · 만기 3분 지남 ok(유예)/6분 bad · 원문·요약 상한 → stuck · 요약 재시도 대기 ok · 원문 있고 손 안 댐 bad · 월 상한 소진 시 요약 보류 ok·원문 미확보는 여전히 bad |
| 재시도 포기 | 같은 클래스 | 8일 전 마지막 시도 안 셈/1일 전 warn · 빈 원문 요청 셈 |
| 비용·서버 | `AiBudgetCheckTest` · `ServerCheckTest` | 80% 정확히 warn · 상한 bad · 예산 0 bad · 메모리 149/150/249/250/None · meminfo 파싱(없음·형식 오류=None) · 디스크 80/81/90/91% · OSError=unknown · overall이 unknown 무시 |
| 알림 | `CheckHealthAlertTest` · `CheckHealthWithoutMailTest` | 정상 0통 · 이상 1통 → 유지 3회 0통 → 복구 1통 → 0통 · 여러 전환 1통(이상·복구 혼합 제목, 유지 중 항목 제외) · 주의→이상 알림 · unknown은 알림·상태 변경 없음 · SMTP 실패 시 상태 미저장·health_check 실패·다음 점검에서 재발송 · --dry-run 쓰기 0(JobStatus·AlertState·PipelineRun)·메일 0 · --test 1통/미설정 CommandError · 미설정 시 정상 종료·상태 미저장·설정 후 첫 점검에서 1통 · 첫 점검이 행을 만들어 한 번도 안 돈 백업을 26h 뒤 잡음 · 90일 정리는 하루 첫 점검만 |
| 기록 지점 | `RecordCommandsTest` · `PollDartJobStatusTest` · `SyncListedJobStatusTest` | record_job 성공/실패(성공 시각 보존·300자) · record_run 건수(시작 이후 생성·원문·요약만) · naive 시각=한국시간 · 형식 오류 CommandError · poll_dart 종료 9=감지 성공 · DartApiError=실패 기록+예외 유지 · `--days`=전체 폴링 · 백필 미기록 · 기록 예외가 poll 결과 불변 · 명단 거절 시 미기록/성공 시 기록 |
| 대시보드 | `DashboardTest` | 비로그인·일반 회원 302 → 로그인 · staff 200 · 계약 키·순서·Item 키·값 문자열·daily_cost 30·recent_runs 20 최신순·jobs 5종 · 닉네임·username 미노출 · 회원 수에서 staff 제외 · 뷰가 DB에 쓰지 않음 · `_call_openai`·`dart.requests.get` 미호출 · health/monitoring/check_health가 dart·summarizer 미import · admin 첫 화면 링크 · 세 모델 add 403 |
| cron | `CronScheduleTest` | check_health 줄 하나 · 10분마다 · 매분 줄 외 모든 줄과 같은 분 없음 · `sleep` 뒤 `flock -w`(‑n 아님) · 같은 잠금 · health.log · backup.sh가 같은 잠금으로 record_job |
| 셸 실행 | `PipelineRunRecordingTest` · `BackupRecordingTest` | docker·flock·free·aws stub으로 **실제 실행**. 헛도는 6경로(종료 0/0/0/1/1/2) record_run 0회 · 일하는 6경로 mode·exit-code 정확 · record_run 실패 시 종료 0 유지, 요약 실패+기록 실패(3)면 1 유지 · 표본 파일 잔존 0 · backup 성공 시 flock 경유 record_job 1회 · 기록 실패에도 0 · 업로드 실패 시 기록 없음 |

셸 실행 테스트는 `bash`가 없으면 건너뛴다(`skipUnless`). 이 PC는 Git Bash에서 돌았다.

## 결함

| 심각도 | 위치 | 증상 | 재현 | 기대/실제 |
|---|---|---|---|---|
| 낮음 (수치 오류, 대시보드 한정) | `deploy/pipeline.sh:87` (소유 backend) | `RUN_STARTED`를 잠금(`flock`, 125~137행)보다 **앞에서** 잡는다. `--wait`(매시 정각)·`--full`(07:05)은 잠금을 최대 600초 기다리므로, 그동안 앞 실행(예: 06:59에 뜬 `--requests`나 07:04의 요청 처리)이 만든 공시·원문·요약이 `record_run`의 "started_at 이후 변화"에 **두 실행 모두에 잡혀 이중 집계**된다. `duration_seconds`에도 대기 시간이 들어가 "소요"가 부풀려진다. | ① 07:04:00 `--requests`가 잠금을 잡고 요약 1건 생성(07:04:40) → 기록 `summarized=1` ② 07:05:00 `--full`이 `RUN_STARTED=07:05:00`을 잡고 잠금 대기 → 07:05:00~07:05:20 사이 앞 실행이 만든 행이 있으면 ③ `--full`의 record_run도 그것을 센다. 같은 원리로 앞 실행이 07:04:59 이후 쓴 행 전부 | 기대: 각 실행은 잠금을 잡은 뒤의 변화만 센다(쓰기 주체가 하나라는 전제가 성립하는 구간). 실제: 잠금 대기 구간의 다른 실행 변화까지 센다. 수정 제안: `RUN_STARTED="$(date -Is)"`를 잠금 분기 뒤(`fi` 다음, `# --- 수집` 앞)로 옮긴다. on_exit은 잠금 전 종료 경로에서 RUN_MODE가 비어 있어 기록하지 않으므로 `RUN_STARTED=""` 초기화만 앞에 두면 `set -u`도 안전하다 |

**재검증(2026-10-24): 수정 확인.** `RUN_STARTED=""`(89행) · 시각 기록은 잠금 분기 뒤(158행). 회귀 테스트 `CronScheduleTest.test_run_start_is_taken_after_the_lock` 추가. 전체 639개 OK.

판정·알림·종료 코드에 영향을 주는 결함은 찾지 못했다.

## 참고 (결함 아님)

- `check_health --test`에서 SMTP 예외는 CommandError로 감싸지 않아 traceback이 나온다(종료 1은 같음). 수동 확인용 명령이라 그대로 둬도 된다.
- `poll_dart --detect --bgn …`(잘못된 조합, CommandError)도 감지 **실패**로 기록된다. 사람이 손으로 잘못 친 경우뿐이고 판정은 freshness라 영향 없음.
- backend가 적어 둔 알려진 틈(월 상한 직전 `can_afford` 실패 시 requests bad)은 재현 테스트를 넣지 않았다 — 설계 판단으로 유지된 것.

## 실행한 명령

```
./venv/Scripts/python.exe manage.py test disclosures.test_monitoring disclosures.tests.PipelineScriptTest   # 87개 OK
./venv/Scripts/python.exe manage.py check                                  # no issues
./venv/Scripts/python.exe manage.py makemigrations --check --dry-run       # No changes detected
./venv/Scripts/python.exe manage.py test                                   # Ran 638 tests, OK (129.8s)
```
