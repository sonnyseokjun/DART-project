"""10단계 운영 모니터링 테스트 (이슈 #56, PLAN.md 9.6).

이 기능의 목표는 "운영자가 사용자보다 먼저 안다"이다. 이 프로젝트의 큰 사고 셋은 모두
**로그가 정상으로 보이는 채** 며칠씩 갔다(PLAN.md 9.3). 감시 장치가 같은 방식으로 조용히
틀리면 — 시간대 경계마다 오보를 내거나, 같은 이상을 10분마다 보내거나, 기록 실패가
파이프라인을 실패로 만들면 — 운영자는 메일을 읽지 않게 되고 결국 아무것도 모르게 된다.
그래서 판정 경계 · 알림 전환 · 기록 지점 · 셸 스크립트의 종료 코드를 함께 고정한다.

DART·LLM·SMTP를 부르지 않는다. 메일은 locmem 백엔드로만 받는다.
"""
import io
import os
import re
import shutil
import smtplib
import subprocess
import tempfile
from datetime import datetime, timedelta
from pathlib import Path
from unittest import skipUnless
from unittest.mock import patch

from allauth.socialaccount.models import SocialAccount
from django.contrib.auth import get_user_model
from django.core import mail
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import SimpleTestCase, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from disclosures import health, monitoring, retry_policy, summarizer
from disclosures.dart import DartApiError, dart_viewer_url
from disclosures.management.commands import check_health as check_health_command
from disclosures.management.commands import poll_dart as poll_dart_command
from disclosures.models import (
    MAX_SUMMARY_ATTEMPTS, AiUsage, AlertState, Company, Disclosure, DisclosureSummary,
    JobStatus, PipelineRun, Sector, Watch,
)
from disclosures.selection import SelectionState
from disclosures.test_watchlist import _corp_zip

REPO_DIR = Path(__file__).resolve().parent.parent
DEPLOY_DIR = REPO_DIR / 'deploy'

#: 메일이 나갈 수 있게 설정된 상태. 백엔드는 locmem이라 실제 SMTP로 나가지 않는다.
MAIL_ON = dict(
    EMAIL_BACKEND='django.core.mail.backends.locmem.EmailBackend',
    EMAIL_HOST_USER='ops@example.com', EMAIL_HOST_PASSWORD='app-password',
    ALERT_EMAIL_TO=['ops@example.com'], DEFAULT_FROM_EMAIL='ops@example.com',
)
MAIL_OFF = dict(MAIL_ON, EMAIL_HOST_USER='', EMAIL_HOST_PASSWORD='', ALERT_EMAIL_TO=[])


def _at(day, hour, minute, second=0):
    """2026년 10월 `day`일의 한국 시각. 19일이 월요일, 23일이 금요일, 24·25일이 주말이다."""
    return timezone.make_aware(datetime(2026, 10, day, hour, minute, second))


def _detect_status(last_success, created=None):
    """저장하지 않은 감지 상태 행 — 판정 함수는 dict로 받으므로 DB가 필요 없다."""
    return {JobStatus.Kind.DETECT: JobStatus(
        kind=JobStatus.Kind.DETECT, last_success_at=last_success,
        created_at=created or last_success)}


# --- 판정: 감지 -----------------------------------------------------------------

class DetectDeadlineTest(SimpleTestCase):
    """감지 판정은 시간대 경계에서 오보를 내면 안 된다.

    월요일 08:00에 감지가 돌고 09:05에 점검하면 마지막 성공이 65분 전이다. 매분 시간대의
    기준(10분)을 그대로 대면 멀쩡한 파이프라인을 이상으로 알린다 — 매주 월요일 아침마다.
    그런 메일이 몇 번 오면 운영자는 진짜 이상 메일도 읽지 않는다.
    """

    def test_calendar_assumption(self):
        # 아래 사례들이 요일을 전제로 한다. 날짜를 바꾸면 여기가 먼저 깨진다.
        self.assertEqual([_at(d, 0, 0).weekday() for d in (19, 23, 24, 25, 26)],
                         [0, 4, 5, 6, 0])

    def test_deadline_table(self):
        cases = [
            # (마지막 성공, 기한, 이유)
            (_at(19, 8, 0, 30), _at(19, 9, 10), '월 아침 — 매분 시간대가 막 시작했다'),
            (_at(19, 8, 30), _at(19, 9, 10), '정각이 아닌 성공(수동 실행)도 같은 기한'),
            (_at(19, 10, 0, 20), _at(19, 10, 11), '매분 시간대 — 다음 분 + 10분'),
            (_at(19, 20, 0, 40), _at(19, 22, 10), '평일 밤 — 다음 정각 + 1시간 10분'),
            (_at(23, 18, 59, 20), _at(23, 20, 10), '금 18:59 — 다음 실행은 19:00 정각'),
            (_at(24, 11, 0, 10), _at(24, 13, 10), '토요일은 종일 정각만'),
            (_at(25, 23, 0, 10), _at(26, 1, 10), '일 → 월 경계'),
        ]
        for last, deadline, why in cases:
            with self.subTest(why=why, last=last):
                self.assertEqual(health.detect_deadline(last), deadline)

    def _level(self, last, now, created=None):
        return health.check_detect(_detect_status(last, created), now).level

    def test_no_false_alarm_right_after_the_morning_boundary(self):
        last = _at(19, 8, 0, 30)
        for minute in range(0, 11):
            with self.subTest(now=f'09:{minute:02d}'):
                self.assertEqual(self._level(last, _at(19, 9, minute)), 'ok')
        self.assertEqual(self._level(last, _at(19, 9, 10, 1)), 'bad')

    def test_active_hours_threshold_just_below_and_above(self):
        last = _at(20, 10, 0, 20)
        self.assertEqual(self._level(last, _at(20, 10, 11)), 'ok')
        self.assertEqual(self._level(last, _at(20, 10, 11, 1)), 'bad')

    def test_idle_hours_threshold_just_below_and_above(self):
        last = _at(20, 20, 0, 40)
        self.assertEqual(self._level(last, _at(20, 22, 10)), 'ok')
        self.assertEqual(self._level(last, _at(20, 22, 10, 1)), 'bad')

    def test_friday_evening_is_not_judged_by_the_active_threshold(self):
        """금 18:59 뒤로는 정각만 돈다. 19:30에 "30분째 감지 없음"은 정상이다."""
        last = _at(23, 18, 59, 20)
        for now in (_at(23, 19, 30), _at(23, 20, 0), _at(23, 20, 10)):
            with self.subTest(now=now):
                self.assertEqual(self._level(last, now), 'ok')
        self.assertEqual(self._level(last, _at(23, 20, 10, 1)), 'bad')

    def test_weekend_morning_is_not_an_active_period(self):
        """토요일 09시대는 매분 시간대가 아니다 — 08:00 성공을 09:15에 보고 울리면 오보다."""
        self.assertEqual(self._level(_at(24, 8, 0, 10), _at(24, 9, 15)), 'ok')
        self.assertEqual(self._level(_at(24, 8, 0, 10), _at(24, 10, 10, 1)), 'bad')

    def test_no_row_is_unknown(self):
        """점검이 한 번도 돌지 않은 첫 배포 직후 — 재료가 없으니 알리지 않는다."""
        check = health.check_detect({}, _at(19, 10, 0))
        self.assertEqual(check.level, 'unknown')

    def test_never_succeeded_is_measured_from_the_row_creation(self):
        """성공이 한 번도 없다고 "판정 불가"로 두면 영영 안 도는 작업이 알림 없이 넘어간다."""
        created = _at(19, 10, 0)
        self.assertEqual(self._level(None, _at(19, 10, 5), created=created), 'ok')
        self.assertEqual(self._level(None, _at(19, 11, 0), created=created), 'bad')

    def test_failure_after_the_last_success_is_shown_but_not_judged(self):
        """실패 기록은 보조다 — 판정은 freshness, 실패는 내용으로만 덧붙인다."""
        statuses = _detect_status(_at(19, 10, 0))
        statuses[JobStatus.Kind.DETECT].last_failure_at = _at(19, 10, 1)
        statuses[JobStatus.Kind.DETECT].last_message = 'DartApiError: 020'
        check = health.check_detect(statuses, _at(19, 10, 3))
        self.assertEqual(check.level, 'ok')
        self.assertIn('DartApiError', check.detail)


class DailyJobCheckTest(SimpleTestCase):
    """하루 1회 작업은 26시간 — 정확히 26시간은 아직 정상(`>` 기준)이다."""

    def test_full_poll_and_backup_threshold(self):
        now = _at(20, 12, 0)
        for check_fn, kind in ((health.check_full_poll, JobStatus.Kind.FULL_POLL),
                               (health.check_backup, JobStatus.Kind.BACKUP)):
            for age, expected in ((timedelta(hours=26), 'ok'),
                                  (timedelta(hours=26, seconds=1), 'bad')):
                with self.subTest(kind=kind, age=age):
                    last = now - age
                    statuses = {kind: JobStatus(kind=kind, last_success_at=last, created_at=last)}
                    self.assertEqual(check_fn(statuses, now).level, expected)
            with self.subTest(kind=kind, case='기록 없음'):
                self.assertEqual(check_fn({}, now).level, 'unknown')


# --- 판정: 요청 처리 · 재시도 상한 ----------------------------------------------

class RequestCheckTest(TestCase):
    """"처리할 수 있는데 15분 넘게 그대로인 요청"만 이상이다.

    원문이 DART에 아직 안 올라와 사다리를 기다리는 건은 우리가 할 수 있는 일이 없다.
    사다리가 24시간이라 그걸 이상으로 치면 원문이 늦는 공시마다 메일이 온다.
    """

    def setUp(self):
        # 만기 판정이 분 단위라(is_retry_due가 초를 버린다) 기준 시각도 분에 맞춘다.
        self.now = timezone.now().replace(second=0, microsecond=0)
        sector = Sector.objects.create(name='반도체', slug='semiconductor')
        self.company = Company.objects.create(
            sector=sector, corp_code='00126380', stock_code='005930', name='삼성전자')
        self._seq = 0

    def _request(self, requested_ago, *, fetched=False, **fields):
        self._seq += 1
        rcept_no = f'2026102400{self._seq:04d}'
        return Disclosure.objects.create(
            company=self.company, rcept_no=rcept_no, report_name='단일판매ㆍ공급계약체결',
            disclosure_type='거래소공시', filed_at=self.now.date(),
            dart_url=dart_viewer_url(rcept_no), selection_state=SelectionState.TARGET,
            raw_fetched=fetched, raw_content='원문' if fetched else '',
            summary_requested_at=self.now - requested_ago, **fields)

    def _level(self):
        return health.check_requests(self.now).level

    def test_no_requests_is_ok(self):
        self.assertEqual(self._level(), 'ok')

    def test_never_attempted_request_overdue_after_15_minutes(self):
        """시도 0회로 15분 — 파이프라인이 안 돈다는 뜻이다."""
        self._request(timedelta(minutes=14))
        self.assertEqual(self._level(), 'ok')
        self._request(timedelta(minutes=16))
        check = health.check_requests(self.now)
        self.assertEqual(check.level, 'bad')
        self.assertIn('1건', check.summary)

    def test_waiting_on_the_dart_ladder_is_normal(self):
        """요청은 3시간 전이지만 30분 칸 사다리를 10분째 기다리는 중 — 정상(대기)."""
        attempts = 50
        self.assertEqual(retry_policy.RETRY_BACKOFF_MINUTES[attempts - 1], 30)
        self._request(timedelta(hours=3), raw_fetch_attempts=attempts,
                      raw_fetch_attempted_at=self.now - timedelta(minutes=10))
        backlog = health.request_backlog(self.now)
        self.assertEqual((backlog.fetch_waiting, backlog.overdue), (1, []))
        self.assertEqual(self._level(), 'ok')

    def test_just_due_retry_gets_the_grace_period(self):
        """만기가 막 지난 건은 다음 분 실행이 처리한다. 3분 지남은 정상, 6분 지남은 이상."""
        # 1분 칸: 4분 전 시도 → 3분 전 만기.
        self._request(timedelta(hours=1), raw_fetch_attempts=1,
                      raw_fetch_attempted_at=self.now - timedelta(minutes=4))
        self.assertEqual(self._level(), 'ok')
        # 7분 전 시도 → 6분 전 만기. 5분 넘게 아무도 다시 시도하지 않았다.
        self._request(timedelta(hours=1), raw_fetch_attempts=1,
                      raw_fetch_attempted_at=self.now - timedelta(minutes=7))
        self.assertEqual(self._level(), 'bad')

    def test_retry_limit_is_left_to_the_stuck_check(self):
        """상한에 걸린 건은 다시 시도하지 않는다 — 요청 판정이 아니라 `stuck`이 잡는다."""
        self._request(timedelta(days=1), raw_fetch_attempts=retry_policy.MAX_FETCH_ATTEMPTS,
                      raw_fetch_attempted_at=self.now - timedelta(hours=1))
        self._request(timedelta(days=1), fetched=True, summary_attempts=MAX_SUMMARY_ATTEMPTS,
                      summary_attempted_at=self.now - timedelta(hours=1))
        backlog = health.request_backlog(self.now)
        self.assertEqual(backlog.stuck, 2)
        self.assertEqual(self._level(), 'ok')

    def test_summary_retry_waiting_is_normal(self):
        # 요약 첫 실패 후 10분 대기 중 — 5분째.
        self._request(timedelta(hours=1), fetched=True, summary_attempts=1,
                      summary_attempted_at=self.now - timedelta(minutes=5))
        self.assertEqual(self._level(), 'ok')

    def test_summary_ready_but_untouched_is_overdue(self):
        self._request(timedelta(minutes=20), fetched=True)
        self.assertEqual(self._level(), 'bad')

    @override_settings(AI_MONTHLY_BUDGET_USD=1.0)
    def test_exhausted_budget_holds_summaries_but_not_fetches(self):
        """월 상한 소진은 ai_budget이 따로 알린다. 요청 판정이 같이 울리면 메일이 둘이 된다.

        다만 원문 확보는 비용과 무관하게 계속 도므로, 원문이 처리 안 되는 것은 여전히 이상이다.
        """
        AiUsage.objects.create(model_name='m', cost_usd=1.0)
        self._request(timedelta(hours=2), fetched=True)
        backlog = health.request_backlog(self.now)
        self.assertEqual((backlog.budget_held, backlog.overdue), (1, []))
        self.assertEqual(self._level(), 'ok')

        self._request(timedelta(hours=2))
        self.assertEqual(self._level(), 'bad')

    def test_stuck_counts_only_recent_attempts(self):
        """상한 건은 사람이 되살리기 전까지 남는다. 기간 없이 세면 "주의"가 영영 안 풀린다."""
        self._request(timedelta(days=9), raw_fetch_attempts=retry_policy.MAX_FETCH_ATTEMPTS,
                      raw_fetch_attempted_at=self.now - timedelta(days=8))
        self.assertEqual(health.check_stuck(self.now).level, 'ok')
        self._request(timedelta(days=2), raw_fetch_attempts=retry_policy.MAX_FETCH_ATTEMPTS,
                      raw_fetch_attempted_at=self.now - timedelta(days=1))
        self.assertEqual(health.check_stuck(self.now).level, 'warn')

    def test_empty_preprocessed_request_is_counted_as_stuck(self):
        """원문은 받았는데 전처리 결과가 빈 요청 — 재시도도 상한도 없이 "정리 중"으로 남는다."""
        disclosure = self._request(timedelta(hours=1), fetched=True)
        disclosure.raw_content = ''
        disclosure.save(update_fields=['raw_content'])
        self.assertEqual(health.stuck_counts(self.now), (0, 0, 1))
        self.assertEqual(health.check_stuck(self.now).level, 'warn')


# --- 판정: 비용 · 서버 ------------------------------------------------------------

@override_settings(AI_MONTHLY_BUDGET_USD=10.0)
class AiBudgetCheckTest(TestCase):
    def _spend(self, amount):
        AiUsage.objects.create(model_name='m', cost_usd=amount)

    def test_thresholds(self):
        self.assertEqual(health.check_ai_budget(timezone.now()).level, 'ok')
        self._spend(7.99)
        self.assertEqual(health.check_ai_budget(timezone.now()).level, 'ok')
        self._spend(0.01)   # 정확히 80% — 주의(>=)
        self.assertEqual(health.check_ai_budget(timezone.now()).level, 'warn')
        self._spend(2.0)
        self.assertEqual(health.check_ai_budget(timezone.now()).level, 'bad')

    @override_settings(AI_MONTHLY_BUDGET_USD=0.0)
    def test_zero_budget_is_exhausted(self):
        self.assertEqual(health.check_ai_budget(timezone.now()).level, 'bad')


class ServerCheckTest(SimpleTestCase):
    """메모리·디스크. 측정이 안 되면 판정 불가(unknown)이고 알리지 않는다."""

    def test_memory_thresholds(self):
        for available, expected in ((149, 'bad'), (150, 'warn'), (249, 'warn'),
                                    (250, 'ok'), (None, 'unknown')):
            with self.subTest(available=available), \
                    patch.object(health, 'memory_available_mb', return_value=available):
                self.assertEqual(health.check_memory().level, expected)

    def test_memory_reads_mem_available_from_meminfo(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'meminfo'
            path.write_text('MemTotal:  971920 kB\nMemFree: 100 kB\n'
                            'MemAvailable:   307200 kB\n', encoding='ascii')
            self.assertEqual(health.memory_available_mb(str(path)), 300)
            path.write_text('MemTotal:  971920 kB\n', encoding='ascii')
            self.assertIsNone(health.memory_available_mb(str(path)))
            path.write_text('MemAvailable: 많음 kB\n', encoding='utf-8')
            self.assertIsNone(health.memory_available_mb(str(path)))
            self.assertIsNone(health.memory_available_mb(str(Path(tmp) / '없음')))

    def test_disk_thresholds(self):
        for used, expected in ((80, 'ok'), (81, 'warn'), (90, 'warn'), (91, 'bad')):
            with self.subTest(used=used), patch.object(
                    health.shutil, 'disk_usage',
                    return_value=shutil._ntuple_diskusage(100, used, 100 - used)):
                self.assertEqual(health.check_disk().level, expected)

    def test_disk_unreadable_is_unknown(self):
        with patch.object(health.shutil, 'disk_usage', side_effect=OSError):
            self.assertEqual(health.check_disk().level, 'unknown')

    def test_overall_ignores_unknown(self):
        def check(level):
            return health.Check('k', 't', level, '')
        self.assertEqual(health.overall_level([check('unknown'), check('ok')]), 'ok')
        self.assertEqual(health.overall_level([check('unknown')]), 'ok')
        self.assertEqual(health.overall_level([check('ok'), check('warn'), check('bad')]), 'bad')


# --- 알림: check_health --------------------------------------------------------

class _HealthyServerMixin:
    """개발 PC의 메모리(/proc 없음)·디스크(97%) 상태가 판정에 끼지 않게 서버 값을 고정한다."""

    def setUp(self):
        super().setUp()
        for target, value in (('memory_available_mb', 900), ('disk_usage', (10, 100))):
            patcher = patch.object(health, target, return_value=value)
            patcher.start()
            self.addCleanup(patcher.stop)
        now = timezone.now()
        for kind in (JobStatus.Kind.DETECT, JobStatus.Kind.FULL_POLL, JobStatus.Kind.BACKUP):
            JobStatus.mark(kind, now=now)

    def _make_stale(self, kind):
        JobStatus.objects.filter(kind=kind).update(
            last_success_at=timezone.now() - timedelta(days=2))

    def _make_fresh(self, kind):
        JobStatus.objects.filter(kind=kind).update(last_success_at=timezone.now())

    def _run(self, *args):
        out = io.StringIO()
        call_command('check_health', *args, stdout=out, stderr=io.StringIO())
        return out.getvalue()


@override_settings(**MAIL_ON)
class CheckHealthAlertTest(_HealthyServerMixin, TestCase):
    """같은 이상은 한 번만 — 정상→이상 1통, 유지 중 0통, 복구 1통.

    10분마다 같은 메일이 오면 소음이 되고 결국 아무도 읽지 않는다(PLAN.md 9.6).
    반대로 상태를 너무 일찍 저장하면 이상이 영영 알려지지 않는다.
    """

    def test_healthy_server_sends_nothing(self):
        self._run()
        self.assertEqual(len(mail.outbox), 0)
        self.assertTrue(JobStatus.objects.get(kind=JobStatus.Kind.HEALTH_CHECK).last_success_at)

    def test_alert_once_then_silence_then_recovery_once(self):
        self._make_stale(JobStatus.Kind.DETECT)
        self._run()
        self.assertEqual(len(mail.outbox), 1)
        self.assertTrue(mail.outbox[0].subject.startswith('[DART 알림] 이상: 공시 감지'))
        self.assertIn('/admin/monitoring/', mail.outbox[0].body)
        self.assertIn('RUNBOOK', mail.outbox[0].body)
        self.assertEqual(AlertState.objects.get(key='detect').level, 'bad')

        for _ in range(3):
            self._run()
        self.assertEqual(len(mail.outbox), 1, '이상이 유지되는 동안 추가 메일이 나갔다')

        self._make_fresh(JobStatus.Kind.DETECT)
        self._run()
        self.assertEqual(len(mail.outbox), 2)
        self.assertTrue(mail.outbox[1].subject.startswith('[DART 알림] 복구: 공시 감지'))
        self.assertEqual(AlertState.objects.get(key='detect').level, 'ok')

        self._run()
        self.assertEqual(len(mail.outbox), 2)

    def test_several_transitions_go_out_as_one_mail(self):
        self._make_stale(JobStatus.Kind.FULL_POLL)
        self._make_stale(JobStatus.Kind.BACKUP)
        self._run()
        self.assertEqual(len(mail.outbox), 1)
        self.assertIn('전체 폴링', mail.outbox[0].subject)
        self.assertIn('백업', mail.outbox[0].subject)

        # 하나는 복구, 하나는 새 이상 — 역시 한 통이고 제목에 둘 다 적힌다.
        self._make_fresh(JobStatus.Kind.FULL_POLL)
        self._make_stale(JobStatus.Kind.DETECT)
        self._run()
        self.assertEqual(len(mail.outbox), 2)
        subject = mail.outbox[1].subject
        self.assertIn('이상: 공시 감지', subject)
        self.assertIn('복구: 전체 폴링', subject)
        self.assertNotIn('백업', subject, '유지 중인 이상을 다시 알렸다')

    def test_warn_to_bad_is_reported(self):
        with patch.object(health, 'memory_available_mb', return_value=200):
            self._run()
        with patch.object(health, 'memory_available_mb', return_value=100):
            self._run()
        self.assertEqual(len(mail.outbox), 2)
        self.assertIn('[주의 → 이상] 메모리', mail.outbox[1].body)

    def test_unknown_neither_alerts_nor_resets_the_state(self):
        """측정이 한 번 실패했다고 "복구"를 보내면 다음 측정에서 같은 이상을 또 보낸다."""
        with patch.object(health, 'memory_available_mb', return_value=100):
            self._run()
        self.assertEqual(len(mail.outbox), 1)
        with patch.object(health, 'memory_available_mb', return_value=None):
            self._run()
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(AlertState.objects.get(key='memory').level, 'bad')
        with patch.object(health, 'memory_available_mb', return_value=100):
            self._run()
        self.assertEqual(len(mail.outbox), 1)

    def test_smtp_failure_keeps_the_state_for_the_next_check(self):
        """발송이 실패했는데 상태를 바꿔 두면 그 이상은 영영 다시 알려지지 않는다."""
        self._make_stale(JobStatus.Kind.DETECT)
        with patch.object(check_health_command, 'send_mail',
                          side_effect=smtplib.SMTPException('연결 거부')), \
                self.assertRaises(CommandError):
            self._run()
        self.assertFalse(AlertState.objects.filter(key='detect').exists())
        status = JobStatus.objects.get(kind=JobStatus.Kind.HEALTH_CHECK)
        self.assertIsNone(status.last_success_at)
        self.assertIsNotNone(status.last_failure_at)

        self._run()   # 다음 점검에서 다시 시도해 나간다
        self.assertEqual(len(mail.outbox), 1)

    def test_dry_run_writes_nothing_and_sends_nothing(self):
        JobStatus.objects.all().delete()
        old = PipelineRun.objects.create(
            mode='detect', started_at=timezone.now() - timedelta(days=200), ok=True)
        output = self._run('--dry-run')
        self.assertEqual(len(mail.outbox), 0)
        self.assertEqual(JobStatus.objects.count(), 0)
        self.assertEqual(AlertState.objects.count(), 0)
        self.assertTrue(PipelineRun.objects.filter(pk=old.pk).exists())
        self.assertIn('판정 불가', output)

    def test_dry_run_prints_the_mail_it_would_send(self):
        self._make_stale(JobStatus.Kind.DETECT)
        output = self._run('--dry-run')
        self.assertEqual(len(mail.outbox), 0)
        self.assertFalse(AlertState.objects.exists())
        self.assertIn('[DART 알림] 이상: 공시 감지', output)

    def test_test_mail(self):
        self._run('--test')
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(mail.outbox[0].subject, '[DART 알림] 시험 메일')
        self.assertEqual(mail.outbox[0].to, ['ops@example.com'])

    def test_first_check_creates_rows_so_a_never_run_job_is_eventually_caught(self):
        """배포 후 한 번도 돈 적 없는 백업이 "기록 없음"으로 영원히 넘어가면 안 된다."""
        JobStatus.objects.filter(kind=JobStatus.Kind.BACKUP).delete()
        self._run()
        row = JobStatus.objects.get(kind=JobStatus.Kind.BACKUP)
        self.assertIsNone(row.last_success_at)
        self.assertEqual(len(mail.outbox), 0)
        JobStatus.objects.filter(pk=row.pk).update(created_at=timezone.now() - timedelta(days=2))
        self._run()
        self.assertEqual(len(mail.outbox), 1)
        self.assertIn('백업', mail.outbox[0].subject)

    def test_old_runs_are_pruned_once_a_day(self):
        now = timezone.now()
        old = PipelineRun.objects.create(mode='detect', started_at=now - timedelta(days=91))
        recent = PipelineRun.objects.create(mode='detect', started_at=now - timedelta(days=89))
        self._run()
        self.assertFalse(PipelineRun.objects.filter(pk=old.pk).exists())
        self.assertTrue(PipelineRun.objects.filter(pk=recent.pk).exists())

        # 같은 날 두 번째 점검은 지우지 않는다(하루 첫 점검에서만).
        again = PipelineRun.objects.create(mode='detect', started_at=now - timedelta(days=91))
        self._run()
        self.assertTrue(PipelineRun.objects.filter(pk=again.pk).exists())


@override_settings(**MAIL_OFF)
class CheckHealthWithoutMailTest(_HealthyServerMixin, TestCase):
    """메일을 설정하지 않은 서버(로컬·재구축 직후)에서 점검 cron이 매번 실패로 끝나면 안 된다."""

    def test_unconfigured_mail_exits_normally_without_saving_state(self):
        self._make_stale(JobStatus.Kind.DETECT)
        output = self._run()   # CommandError 없이 끝난다
        self.assertEqual(len(mail.outbox), 0)
        self.assertIn('메일 설정이 없어', output)
        # 상태를 저장하지 않는다 — 나중에 설정하면 그때의 이상이 첫 점검에서 나가야 한다.
        self.assertFalse(AlertState.objects.exists())
        self.assertIsNotNone(
            JobStatus.objects.get(kind=JobStatus.Kind.HEALTH_CHECK).last_success_at)

        with override_settings(**MAIL_ON):
            self._run()
        self.assertEqual(len(mail.outbox), 1)

    def test_test_mail_requires_configuration(self):
        with self.assertRaises(CommandError):
            self._run('--test')
        self.assertEqual(len(mail.outbox), 0)


# --- 기록 지점 -----------------------------------------------------------------

class _DisclosureFixture:
    def setUp(self):
        super().setUp()
        self.sector = Sector.objects.create(name='반도체', slug='semiconductor')
        self.company = Company.objects.create(
            sector=self.sector, corp_code='00126380', stock_code='005930',
            name='삼성전자', is_active=True)

    def _disclosure(self, rcept_no, **fields):
        return Disclosure.objects.create(
            company=self.company, rcept_no=rcept_no, report_name='공시',
            disclosure_type='거래소공시', filed_at=timezone.localdate(),
            dart_url=dart_viewer_url(rcept_no), selection_state=SelectionState.TARGET,
            **fields)


class RecordCommandsTest(_DisclosureFixture, TestCase):
    """record_job · record_run — 셸 스크립트가 DB에 남기는 유일한 통로."""

    def test_record_job_success_and_failure(self):
        call_command('record_job', 'backup', '--message', 's3://b/db/x.gz', stdout=io.StringIO())
        row = JobStatus.objects.get(kind='backup')
        success = row.last_success_at
        self.assertIsNotNone(success)
        self.assertEqual(row.last_message, 's3://b/db/x.gz')

        call_command('record_job', 'backup', '--failed', '--message', '가' * 400,
                     stdout=io.StringIO())
        row.refresh_from_db()
        self.assertEqual(row.last_success_at, success, '실패 기록이 마지막 성공을 지웠다')
        self.assertIsNotNone(row.last_failure_at)
        self.assertEqual(len(row.last_message), 300)
        self.assertEqual(JobStatus.objects.count(), 1, '종류별 한 행이 아니다')

    def test_record_run_counts_changes_since_the_start_from_the_db(self):
        """건수는 출력 파싱이 아니라 DB에서 — 문구를 고쳐도 기록이 조용히 0이 되지 않는다."""
        started = timezone.now() - timedelta(minutes=1)
        before = timezone.now() - timedelta(hours=1)
        old = self._disclosure('20261024000001', raw_fetched=True, raw_content='원문',
                               raw_fetched_at=before)
        Disclosure.objects.filter(pk=old.pk).update(created_at=before)
        new_fetched = self._disclosure('20261024000002', raw_fetched=True, raw_content='원문',
                                       raw_fetched_at=timezone.now())
        self._disclosure('20261024000003')
        DisclosureSummary.objects.create(disclosure=new_fetched, one_line='요약',
                                         easy_explanation='설명', why_important='이유',
                                         importance='medium')
        old_summary = DisclosureSummary.objects.create(
            disclosure=old, one_line='요약', easy_explanation='설명', why_important='이유',
            importance='low')
        DisclosureSummary.objects.filter(pk=old_summary.pk).update(created_at=before)

        call_command('record_run', '--mode', 'detect', '--started-at', started.isoformat(),
                     '--exit-code', '0', '--mem-min-mb', '312', stdout=io.StringIO())
        run = PipelineRun.objects.get()
        self.assertEqual((run.new_disclosures, run.fetched, run.summarized), (2, 1, 1))
        self.assertTrue(run.ok)
        self.assertEqual((run.exit_code, run.mem_min_mb, run.message), (0, 312, ''))
        self.assertIsNotNone(run.duration_seconds)

    def test_record_run_failure_and_time_parsing(self):
        call_command('record_run', '--mode', 'full', '--started-at', '2026-10-24T07:05:00',
                     '--exit-code', '1', stdout=io.StringIO())
        run = PipelineRun.objects.get()
        self.assertFalse(run.ok)
        self.assertEqual(run.message, '종료 코드 1')
        self.assertTrue(timezone.is_aware(run.started_at))
        self.assertEqual(timezone.localtime(run.started_at).hour, 7, 'naive 시각을 한국 시간으로 읽지 않았다')

        with self.assertRaises(CommandError):
            call_command('record_run', '--mode', 'full', '--started-at', '어제',
                         '--exit-code', '0', stdout=io.StringIO())
        self.assertEqual(PipelineRun.objects.count(), 1)


class PollDartJobStatusTest(_DisclosureFixture, TestCase):
    """감지·전체 폴링의 마지막 성공. "신규 없음"(종료 9)도 성공이다.

    성공으로 적지 않으면 공시가 뜸한 시간마다 "감지가 멈췄다"는 오보가 난다.
    """

    LATEST = 'disclosures.management.commands.poll_dart.latest_disclosures'
    ITER = 'disclosures.management.commands.poll_dart.iter_disclosures'

    def _status(self, kind):
        return JobStatus.objects.filter(kind=kind).first()

    def test_nothing_new_is_a_detect_success(self):
        with patch(self.LATEST, return_value=[]), patch(self.ITER, return_value=iter([])), \
                self.assertRaises(SystemExit) as ctx:
            call_command('poll_dart', days=2, detect=True, stdout=io.StringIO())
        self.assertEqual(ctx.exception.code, poll_dart_command.NOTHING_NEW_EXIT_CODE)
        status = self._status(JobStatus.Kind.DETECT)
        self.assertIsNotNone(status.last_success_at)
        self.assertEqual(status.last_message, '신규 없음')
        self.assertIsNone(self._status(JobStatus.Kind.FULL_POLL))

    def test_dart_error_is_recorded_as_failure_and_still_raised(self):
        with patch(self.LATEST, side_effect=DartApiError('020', '요청 제한 초과')), \
                self.assertRaises(DartApiError):
            call_command('poll_dart', days=2, detect=True, stdout=io.StringIO())
        status = self._status(JobStatus.Kind.DETECT)
        self.assertIsNone(status.last_success_at)
        self.assertIsNotNone(status.last_failure_at)
        self.assertIn('DartApiError', status.last_message)

    def test_days_is_a_full_poll_and_backfill_is_not_recorded(self):
        with patch(self.ITER, side_effect=lambda *a, **k: iter([])):
            call_command('poll_dart', days=2, stdout=io.StringIO())
            self.assertIsNotNone(self._status(JobStatus.Kind.FULL_POLL).last_success_at)
            JobStatus.objects.all().delete()
            call_command('poll_dart', bgn='20260901', end='20260930', stdout=io.StringIO())
        self.assertFalse(JobStatus.objects.exists(), '백필이 정기 작업 성공으로 기록됐다')

    def test_recording_failure_does_not_change_the_poll_result(self):
        """감시 장치가 감시 대상을 멈추면 안 된다 — 기록이 깨져도 종료 코드는 그대로."""
        with patch.object(JobStatus, 'mark', side_effect=RuntimeError('database is locked')):
            with patch(self.LATEST, return_value=[]), self.assertRaises(SystemExit) as ctx:
                call_command('poll_dart', days=2, detect=True, stdout=io.StringIO(),
                             stderr=io.StringIO())
            self.assertEqual(ctx.exception.code, poll_dart_command.NOTHING_NEW_EXIT_CODE)
            with patch(self.ITER, side_effect=lambda *a, **k: iter([])):
                call_command('poll_dart', days=2, stdout=io.StringIO(), stderr=io.StringIO())


class SyncListedJobStatusTest(TestCase):
    def _sync(self, entries):
        response = type('R', (), {})()
        response.content = _corp_zip(entries)
        response.headers = {'Content-Type': 'application/x-msdownload'}
        response.raise_for_status = lambda: None
        with patch('disclosures.dart.requests.get', return_value=response), \
                patch('disclosures.management.commands.sync_listed_corps.MIN_EXPECTED_CORPS', 2):
            call_command('sync_listed_corps', stdout=io.StringIO())

    def test_success_is_recorded_and_a_rejected_list_is_not(self):
        with self.assertRaises(CommandError):
            self._sync([('00000001', '가', '000001')])
        self.assertFalse(JobStatus.objects.exists())

        self._sync([('00000001', '가', '000001'), ('00000002', '나', '000002')])
        status = JobStatus.objects.get(kind=JobStatus.Kind.SYNC_LISTED)
        self.assertIsNotNone(status.last_success_at)
        self.assertIn('전체 2곳', status.last_message)


# --- 대시보드 -------------------------------------------------------------------

class DashboardTest(_DisclosureFixture, TestCase):
    """`/admin/monitoring/` — staff 전용, DART·LLM 미호출, 개인정보는 숫자로만."""

    NICKNAME = '비밀닉네임하늘'
    USERNAME = 'kakao_4815162342'

    def setUp(self):
        super().setUp()
        User = get_user_model()
        self.staff = User.objects.create_user(username='opsreviewer', is_staff=True)
        self.member = User.objects.create_user(username=self.USERNAME, first_name=self.NICKNAME)
        SocialAccount.objects.create(
            user=self.member, provider='kakao', uid='4815162342',
            extra_data={'kakao_account': {'profile': {'nickname': self.NICKNAME}}})
        Watch.objects.create(user=self.member, company=self.company)
        self.url = reverse('admin_monitoring')

    def test_url(self):
        self.assertEqual(self.url, '/admin/monitoring/')

    def test_anonymous_and_members_are_turned_away(self):
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 302)
        self.assertIn('/admin/login/', response['Location'])

        self.client.force_login(self.member)
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 302)
        self.assertIn('/admin/login/', response['Location'])

    def test_staff_sees_the_contract(self):
        now = timezone.now()
        for i in range(25):
            PipelineRun.objects.create(mode='detect', started_at=now - timedelta(minutes=i),
                                       finished_at=now, ok=True, exit_code=0)
        self.client.force_login(self.staff)
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200)
        ctx = response.context

        self.assertEqual(ctx['title'], '운영 모니터링')
        self.assertTrue(timezone.is_aware(ctx['checked_at']))
        self.assertIn(ctx['overall'], ('ok', 'warn', 'bad'))
        self.assertEqual([c.key for c in ctx['checks']],
                         ['detect', 'full_poll', 'backup', 'requests', 'stuck',
                          'ai_budget', 'memory', 'disk'])
        self.assertEqual([s['key'] for s in ctx['sections']],
                         ['service', 'pipeline', 'summary', 'cost', 'server'])
        for section in ctx['sections']:
            for item in section['items']:
                with self.subTest(section=section['key'], item=item.get('label')):
                    self.assertEqual(set(item), {'label', 'value', 'level', 'hint'})
                    self.assertIsInstance(item['value'], str)
                    self.assertIn(item['level'], (None, 'ok', 'warn', 'bad', 'unknown'))
        self.assertEqual(len(ctx['daily_cost']), monitoring.DAILY_COST_DAYS)
        self.assertEqual(set(ctx['daily_cost'][0]), {'date', 'cost_usd', 'ratio'})
        runs = ctx['recent_runs']
        self.assertEqual(len(runs), monitoring.RECENT_RUNS)
        self.assertEqual(runs, sorted(runs, key=lambda r: r.started_at, reverse=True))
        self.assertEqual([j.kind for j in ctx['jobs']], JobStatus.Kind.values)

    def test_member_count_only_no_personal_data(self):
        self.client.force_login(self.staff)
        html = self.client.get(self.url).content.decode()
        self.assertNotIn(self.NICKNAME, html)
        self.assertNotIn(self.USERNAME, html)
        service = self.client.get(self.url).context['sections'][0]['items']
        self.assertEqual(service[0]['value'], '1명', '검수자(staff)를 회원으로 셌거나 회원을 놓쳤다')

    def test_view_only_reads(self):
        """대시보드를 연다고 기록이 생기면 "점검이 살아 있다"를 화면이 거짓으로 만든다."""
        self.client.force_login(self.staff)
        self.client.get(self.url)
        self.assertFalse(JobStatus.objects.exists())
        self.assertFalse(AlertState.objects.exists())

    def test_rendering_never_calls_openai_or_dart(self):
        self.client.force_login(self.staff)
        with patch.object(summarizer, '_call_openai') as mock_llm, \
                patch('disclosures.dart.requests.get') as mock_dart:
            self.assertEqual(self.client.get(self.url).status_code, 200)
        mock_llm.assert_not_called()
        mock_dart.assert_not_called()

    def test_modules_do_not_import_dart_or_summarizer(self):
        import inspect

        for module in (health, monitoring, check_health_command):
            source = inspect.getsource(module)
            for forbidden in ('dart', 'summarizer'):
                with self.subTest(module=module.__name__, forbidden=forbidden):
                    self.assertNotIn(f'from .{forbidden} import', source)
                    self.assertNotIn(f'from disclosures.{forbidden} import', source)
                    self.assertNotRegex(source, rf'(?m)^from (\.|disclosures) import .*\b{forbidden}\b')

    def test_admin_index_links_to_the_dashboard(self):
        self.client.force_login(self.staff)
        self.assertContains(self.client.get('/admin/'), f'href="{self.url}"')

    def test_monitoring_tables_are_read_only_in_admin(self):
        """기록을 손으로 고치면 판정의 근거가 거짓이 된다."""
        admin_user = get_user_model().objects.create_superuser(username='root', password='x')
        self.client.force_login(admin_user)
        for model in ('jobstatus', 'pipelinerun', 'alertstate'):
            with self.subTest(model=model):
                response = self.client.get(f'/admin/disclosures/{model}/add/')
                self.assertEqual(response.status_code, 403)
                self.assertEqual(
                    self.client.get(f'/admin/disclosures/{model}/').status_code, 200)


# --- 셸 스크립트 · cron ----------------------------------------------------------

def _read(name):
    return (DEPLOY_DIR / name).read_text(encoding='utf-8')


class CronScheduleTest(SimpleTestCase):
    """점검 줄이 다른 줄과 같은 분에 뜨면 `flock`에 밀린 쪽이 조용히 죽는다(PLAN.md 9.3 주의 3)."""

    CRON_LINE = re.compile(
        r'^([\d*/,-]+)\s+([\d*/,-]+)\s+(\S+)\s+(\S+)\s+([\d*/,-]+)\s+(.*)$', re.MULTILINE)

    @staticmethod
    def _expand(field, lo, hi):
        values = set()
        for part in field.split(','):
            step = 1
            if '/' in part:
                part, raw_step = part.split('/')
                step = int(raw_step)
            if part == '*':
                start, end = lo, hi
            elif '-' in part:
                start, end = (int(x) for x in part.split('-'))
            else:
                start = end = int(part)
            values.update(range(start, end + 1, step))
        return values

    def _lines(self):
        return [m for m in self.CRON_LINE.findall(_read('crontab'))]

    def _health_line(self):
        lines = [m for m in self._lines() if 'check_health' in m[5]]
        self.assertEqual(len(lines), 1, 'crontab에 check_health 줄이 하나가 아니다')
        return lines[0]

    def _firings(self, line):
        minute, hour, _dom, _mon, dow, _command = line
        return {(d % 7, h, m)
                for d in self._expand(dow, 0, 7)
                for h in self._expand(hour, 0, 23)
                for m in self._expand(minute, 0, 59)}

    def test_health_check_never_shares_a_minute_with_scheduled_lines(self):
        """정각 감지 · 06:50 명단 · 07:05 전체 폴링 · 03:00 재검증 · 04:00 백업 · 30분 메모리 표본."""
        health_line = self._health_line()
        health_firings = self._firings(health_line)
        self.assertEqual(len({m for _, _, m in health_firings}), 6, '10분마다가 아니다')
        for line in self._lines():
            if line == health_line or len(self._expand(line[0], 0, 59)) >= 59:
                continue   # 매분 줄은 아래 테스트가 따로 본다
            with self.subTest(line=' '.join(line[:5]) + ' ' + line[5][:50]):
                overlap = sorted(health_firings & self._firings(line))
                self.assertFalse(bool(overlap), f"같은 분에 뜬다: {overlap[:3]}")

    def test_health_check_waits_behind_the_every_minute_pipeline(self):
        """매분 감지·요청 줄과는 늘 같은 분이다. 그 둘은 `-n`이라 점검이 먼저 잠금을 잡으면
        그 분의 감지가 "정상"을 찍고 건너뛰어진다. 늦게 뜨고 잠금을 기다려야 한다."""
        command = self._health_line()[5]
        self.assertIn('flock -w', command)
        self.assertNotIn('flock -n', command)
        self.assertIn('/tmp/dart-pipeline.lock', command)
        self.assertRegex(command, r'^sleep \d+ && ')
        self.assertLess(command.index('sleep'), command.index('flock'))
        self.assertIn('/var/log/dart/health.log', command)

    def test_run_start_is_taken_after_the_lock(self):
        """시작 시각을 잠금보다 먼저 찍으면 --wait·--full이 기다리는 동안 앞 실행이 만든
        공시·원문·요약까지 record_run이 이 실행의 것으로 센다(이중 집계, QA 2026-10-24)."""
        script = _read('pipeline.sh')
        stamps = [m.start() for m in re.finditer(r'^RUN_STARTED="\$\(date -Is\)"$', script, re.MULTILINE)]
        self.assertEqual(len(stamps), 1, 'RUN_STARTED에 시각을 넣는 줄이 하나가 아니다')
        self.assertGreater(stamps[0], script.index('flock -w "$LOCK_WAIT_SECONDS" 9'))
        self.assertGreater(stamps[0], script.index('flock -n 9'))
        self.assertLess(stamps[0], script.index('# --- 수집'))

    def test_backup_records_under_the_pipeline_lock(self):
        """DB에 쓰는 프로세스는 하나 — 파이프라인 밖의 기록도 같은 잠금을 잡는다(PLAN.md 9.3)."""
        script = _read('backup.sh')
        self.assertRegex(script, r'flock -w \d+ /tmp/dart-pipeline\.lock \\\n\s+docker compose '
                                 r'exec -T web python manage\.py record_job backup')


BASH = shutil.which('bash')

_STUBS = {
    # docker compose exec -T web python manage.py <명령> … — 명령 이름별 RC_<명령>으로 끝난다.
    # backup.sh의 스냅샷(`python -c`)은 컨테이너 경로를 호스트 data/로 옮겨 빈 파일을 만든다.
    'docker': r'''#!/bin/bash
if [ "$6" = "-c" ]; then
    path=$(printf '%s' "$7" | grep -o '/app/data/backup_[0-9_]*\.sqlite3' | head -1)
    : > "./data/${path##*/}"
    exit 0
fi
shift 6
echo "$*" >> "$CALLS"
var="RC_$1"
exit "${!var:-0}"
''',
    # 명령이 붙으면(backup.sh·crontab 방식) 기록하고 그 명령을 실행한다. 아니면 RC_FLOCK.
    'flock': r'''#!/bin/bash
echo "flock $*" >> "$CALLS"
if [ $# -gt 3 ]; then shift 3; exec "$@"; fi
exit "${RC_FLOCK:-0}"
''',
    'free': r'''#!/bin/bash
echo "              total        used        free      shared  buff/cache   available"
echo "Mem:            949         600         100          10         249         ${FREE_AVAIL:-300}"
''',
    'aws': '#!/bin/bash\nexit "${RC_AWS:-0}"\n',
}


@skipUnless(BASH, 'bash가 없어 셸 스크립트를 실행할 수 없다')
class _ScriptRunner(SimpleTestCase):
    """deploy/*.sh를 docker·flock·free·aws stub으로 실제 실행한다.

    줄 단위 구조 검사는 "그렇게 쓰여 있다"만 본다. 종료 코드는 trap·set -e·pipefail이
    얽혀 정해지므로 실제로 돌려 봐야 안다.
    """

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        stub_dir = self.tmp / 'bin'
        stub_dir.mkdir()
        for name, body in _STUBS.items():
            path = stub_dir / name
            path.write_text(body, encoding='utf-8', newline='\n')
            path.chmod(0o755)
        self.proj = self.tmp / 'proj'
        (self.proj / 'data').mkdir(parents=True)
        (self.proj / 'deploy').mkdir()
        shutil.copy(DEPLOY_DIR / '_load_env.sh', self.proj / 'deploy' / '_load_env.sh')
        (self.proj / '.env').write_text('S3_BACKUP_BUCKET=test-bucket\n', encoding='utf-8')
        self.tmpdir = self.tmp / 't'
        self.tmpdir.mkdir()
        self.calls_file = self.tmp / 'calls.txt'
        self.env = dict(
            os.environ,
            PATH=str(stub_dir) + os.pathsep + os.environ.get('PATH', ''),
            PROJECT_DIR=self.proj.as_posix(), LOCK_FILE=(self.tmp / 'lock').as_posix(),
            TMPDIR=self.tmpdir.as_posix(), CALLS=self.calls_file.as_posix(),
        )

    def _run(self, script, *args, **rcs):
        self.calls_file.write_text('', encoding='utf-8')
        env = dict(self.env, **{k: str(v) for k, v in rcs.items()})
        result = subprocess.run(
            [BASH, (DEPLOY_DIR / script).as_posix(), *args], env=env,
            capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=120)
        calls = self.calls_file.read_text(encoding='utf-8').splitlines()
        return result.returncode, calls, result.stdout + result.stderr


class PipelineRunRecordingTest(_ScriptRunner):
    """pipeline.sh — 일이 있었던 실행만 기록하고, 기록이 종료 코드를 바꾸지 않는다.

    헛도는 실행까지 기록하면 밤·주말 하루 1,440행이다. 기록 실패가 파이프라인 실패가 되면
    감시 장치가 감시 대상을 멈춘다. 둘 다 기존 종료 코드의 의미(PLAN.md 9.3)를 지켜야 한다.
    """

    def _records(self, calls):
        return [c for c in calls if c.startswith('record_run')]

    def _assert_no_sample_left(self):
        self.assertEqual(list(self.tmpdir.iterdir()), [], '메모리 표본 파일이 남았다')

    def test_idle_runs_record_nothing(self):
        cases = [
            ('감지 신규 없음 + 대기 없음', (), dict(RC_poll_dart=9, RC_pending_work=9), 0),
            ('요청 처리 할 일 없음', ('--requests',), dict(RC_pending_work=9), 0),
            ('앞 실행이 잠금을 쥠', (), dict(RC_FLOCK=1), 0),
            ('--wait 잠금 대기 실패', ('--wait',), dict(RC_FLOCK=1), 1),
            ('감지 실패', (), dict(RC_poll_dart=1), 1),
            ('대기 확인 실패', (), dict(RC_poll_dart=9, RC_pending_work=2), 2),
        ]
        for why, args, rcs, expected_rc in cases:
            with self.subTest(why=why):
                rc, calls, output = self._run('pipeline.sh', *args, **rcs)
                self.assertEqual(rc, expected_rc, output)
                self.assertEqual(self._records(calls), [])
                self.assertFalse(any(c.startswith('fetch_documents') for c in calls))
                self._assert_no_sample_left()

    def test_working_runs_record_their_mode_and_exit_code(self):
        cases = [
            ('감지 신규 → 성공', (), {}, 0, 'detect'),
            ('감지 신규 → 요약 실패', (), dict(RC_summarize_disclosures=1), 1, 'detect'),
            ('재개', (), dict(RC_poll_dart=9), 0, 'resume'),
            ('요청 처리', ('--requests',), {}, 0, 'requests'),
            ('전체 폴링 성공', ('--full',), {}, 0, 'full'),
            ('전체 폴링 수집 실패', ('--full',), dict(RC_poll_dart=1), 1, 'full'),
        ]
        for why, args, rcs, expected_rc, mode in cases:
            with self.subTest(why=why):
                rc, calls, output = self._run('pipeline.sh', *args, **rcs)
                self.assertEqual(rc, expected_rc, output)
                records = self._records(calls)
                self.assertEqual(len(records), 1, calls)
                self.assertIn(f'--mode {mode} ', records[0])
                self.assertIn(f'--exit-code {expected_rc}', records[0])
                self.assertRegex(records[0], r'--started-at \S+T\S+')
                mem = re.search(r'--mem-min-mb (\d+)', records[0])
                if mem:   # 표본기가 첫 표본을 뜨기 전에 끝났으면 빠질 수 있다
                    self.assertEqual(mem.group(1), '300')
                self._assert_no_sample_left()

    def test_record_failure_does_not_change_the_exit_code(self):
        rc, calls, output = self._run('pipeline.sh', RC_record_run=1)
        self.assertEqual(rc, 0, output)
        self.assertEqual(len(self._records(calls)), 1)
        self.assertIn('실행 기록 실패', output)

        rc, _calls, output = self._run('pipeline.sh', RC_record_run=3,
                                       RC_summarize_disclosures=1)
        self.assertEqual(rc, 1, '기록 실패의 종료 코드가 파이프라인 결과를 덮었다')
        self._assert_no_sample_left()


class BackupRecordingTest(_ScriptRunner):
    """backup.sh — 성공만 기록하고, 기록 실패가 백업을 실패로 만들지 않는다."""

    def test_success_is_recorded_under_the_pipeline_lock(self):
        rc, calls, output = self._run('backup.sh')
        self.assertEqual(rc, 0, output)
        flock = [c for c in calls if c.startswith('flock ')]
        self.assertEqual(len(flock), 1, calls)
        self.assertRegex(flock[0], r'^flock -w \d+ /tmp/dart-pipeline\.lock docker compose ')
        record = [c for c in calls if c.startswith('record_job')]
        self.assertEqual(len(record), 1, calls)
        self.assertRegex(record[0],
                         r'^record_job backup --message s3://test-bucket/db/db_\d{8}_\d{6}\.sqlite3\.gz$')

    def test_record_failure_keeps_the_backup_successful(self):
        rc, calls, output = self._run('backup.sh', RC_record_job=1)
        self.assertEqual(rc, 0, output)
        self.assertIn('경고', output)

    def test_failed_backup_records_nothing(self):
        """실패를 따로 적지 않아도 성공 시각이 멈추므로 26시간 판정에 걸린다."""
        rc, calls, output = self._run('backup.sh', RC_AWS=1)
        self.assertNotEqual(rc, 0, output)
        self.assertFalse([c for c in calls if c.startswith('record_job')])
