"""운영 상태를 판정하고, 상태가 **바뀐** 항목만 메일로 알린다 (10단계, 이슈 #56).

목표는 "운영자가 사용자보다 먼저 안다"이다(PLAN.md 9.6). 판정은 `disclosures/health.py`가
하고, 이 명령은 지난 상태(AlertState)와 비교해 전환만 골라 보낸다.

- **같은 이상은 한 번만 알린다.** 정상 → 이상(주의)으로 바뀔 때 1통, 복구될 때 1통.
  한 번 점검에서 여러 항목이 바뀌면 한 통으로 묶는다.
- **판정 불가(unknown)는 알리지 않고 지난 상태도 건드리지 않는다.** 측정이 한 번 실패했다고
  "복구"를 보내면 안 된다.
- **메일이 실제로 나간 뒤에만 상태를 저장한다.** 발송이 실패했는데 상태를 바꿔 두면 그 이상은
  영영 다시 알려지지 않는다. 메일을 설정하지 않은 서버에서도 같다 — 나중에 설정하면 그때의
  이상이 첫 점검에서 한 번 나간다.
- DART·LLM을 부르지 않는다.

서버에서는 cron이 10분마다 파이프라인과 **같은 잠금**을 잡고 부른다(deploy/crontab).

사용법:
  python manage.py check_health            # 판정 + 전환 알림 + 상태 저장
  python manage.py check_health --dry-run  # 판정과 보낼 메일만 출력 (메일·DB 쓰기 없음)
  python manage.py check_health --test     # 시험 메일 1통 (설정 확인용)

종료 코드: 0 정상(이상이 있어도 알렸으면 0) · 1 메일 발송 실패 또는 --test 설정 누락
"""
from django.conf import settings
from django.core.mail import send_mail
from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from disclosures import health
from disclosures.models import AlertState, JobStatus, PipelineRun

SUBJECT_PREFIX = '[DART 알림]'

LEVEL_LABELS = {'ok': '정상', 'warn': '주의', 'bad': '이상', 'unknown': '판정 불가'}

#: 항목별로 메일에 적을 대응 절차. docs/RUNBOOK.md의 절 이름이다.
RUNBOOK_REFS = {
    'detect': 'RUNBOOK 5장 "파이프라인이 안 돈다"',
    'full_poll': 'RUNBOOK 5장 "파이프라인이 안 돈다"',
    'backup': 'RUNBOOK 2.4·2.5 (백업) — 로그 /var/log/dart/backup.log',
    'requests': 'RUNBOOK 5장 "원문을 못 받는 공시가 있다" · "파이프라인이 안 돈다"',
    'stuck': 'RUNBOOK 5장 "원문을 못 받는 공시가 있다"',
    'ai_budget': 'RUNBOOK 4장 (월 AI 비용 상한)',
    'memory': 'RUNBOOK 5장 "컨테이너가 계속 재시작한다"',
    'disk': 'RUNBOOK 2.9 (로그 회전)',
}


def mail_configured():
    """메일을 보낼 수 있게 설정됐는가. 하나라도 비면 메일 없이 경고만 남긴다."""
    return bool(settings.EMAIL_HOST_USER and settings.EMAIL_HOST_PASSWORD
                and settings.ALERT_EMAIL_TO)


def dashboard_url():
    path = '/admin/monitoring/'
    return f'https://{settings.SITE_DOMAIN}{path}' if settings.SITE_DOMAIN else path


def find_transitions(checks, states):
    """[(check, 'alert'|'recover', 이전 level)] — 알릴 전환만.

    지난 상태가 없으면 정상이었다고 본다. 처음 도는 서버에서 정상 항목은 아무것도 보내지 않고,
    이미 이상인 항목은 한 번 보낸다.
    주의 ↔ 이상 사이의 변화도 알린다 — 심해졌거나 누그러진 것은 운영자가 알아야 할 변화다.
    """
    transitions = []
    for check in checks:
        if check.level == 'unknown':
            continue
        previous = states[check.key].level if check.key in states else 'ok'
        if check.level == previous:
            continue
        kind = 'recover' if check.level == 'ok' else 'alert'
        transitions.append((check, kind, previous))
    return transitions


def compose_mail(transitions, checks, now):
    """(제목, 본문). 이상과 복구가 섞이면 제목에 둘 다 적는다."""
    alerts = [check.title for check, kind, _ in transitions if kind == 'alert']
    recovers = [check.title for check, kind, _ in transitions if kind == 'recover']
    parts = []
    if alerts:
        parts.append(f'이상: {", ".join(alerts)}')
    if recovers:
        parts.append(f'복구: {", ".join(recovers)}')
    subject = f'{SUBJECT_PREFIX} {" / ".join(parts)}'

    lines = [f'운영 점검 {timezone.localtime(now):%Y-%m-%d %H:%M} 결과, 상태가 바뀐 항목입니다.', '']
    for check, kind, previous in transitions:
        lines.append(f'[{LEVEL_LABELS[previous]} → {LEVEL_LABELS[check.level]}] '
                     f'{check.title}: {check.summary}')
        if check.detail:
            lines.append(f'    {check.detail}')
        if kind == 'alert' and check.key in RUNBOOK_REFS:
            lines.append(f'    대응: {RUNBOOK_REFS[check.key]}')
    lines += ['', '전체 상태']
    for check in checks:
        lines.append(f'  {LEVEL_LABELS[check.level]:<5} {check.title}: {check.summary}')
    lines += [
        '',
        f'대시보드: {dashboard_url()}',
        '(/admin은 등록된 IP에서만 열린다 — RUNBOOK 5장 "admin에 못 들어간다")',
        '',
        '같은 이상은 다시 보내지 않는다. 복구되면 한 통 더 간다.',
    ]
    return subject, '\n'.join(lines)


def save_transitions(transitions, now):
    for check, _, _ in transitions:
        AlertState.objects.update_or_create(
            key=check.key,
            defaults={'level': check.level, 'since': now, 'last_sent_at': now,
                      'detail': f'{check.summary} {check.detail}'.strip()[:300]},
        )


def prune_runs(now):
    """보관 기간이 지난 실행 이력을 지운다. 지운 행 수."""
    deleted, _ = PipelineRun.objects.filter(
        started_at__lt=now - health.PIPELINE_RUN_RETENTION).delete()
    return deleted


class Command(BaseCommand):
    help = '운영 상태를 판정하고 바뀐 항목만 메일로 알린다 (DART·LLM 미호출)'

    def add_arguments(self, parser):
        parser.add_argument('--dry-run', action='store_true',
                            help='판정과 보낼 메일만 출력한다. 메일도 DB 쓰기도 없다')
        parser.add_argument('--test', action='store_true',
                            help='현재 상태를 담은 시험 메일 1통을 보낸다')

    def handle(self, *args, **options):
        now = timezone.now()
        if options['test']:
            return self._send_test(now)

        dry_run = options['dry_run']
        previous_check = JobStatus.objects.filter(kind=JobStatus.Kind.HEALTH_CHECK).first()
        if not dry_run:
            health.ensure_job_rows()

        checks = health.run_checks(now)
        for check in checks:
            self.stdout.write(f'{LEVEL_LABELS[check.level]:<5} {check.title}: {check.summary}'
                              + (f' — {check.detail}' if check.detail else ''))

        states = {state.key: state for state in AlertState.objects.all()}
        transitions = find_transitions(checks, states)
        if not transitions:
            self.stdout.write('바뀐 항목 없음 — 메일을 보내지 않습니다')
            note = '전환 없음'
        else:
            subject, body = compose_mail(transitions, checks, now)
            if dry_run:
                self.stdout.write(f'\n--dry-run: 아래 메일을 보냈을 것입니다\n{subject}\n\n{body}')
                return
            note = self._deliver(subject, body, transitions, now)

        if dry_run:
            return
        if previous_check is None or previous_check.last_success_at is None or (
                timezone.localdate(previous_check.last_success_at) != timezone.localdate(now)):
            # 하루 첫 점검에서만 정리한다. 매번 지워도 싸지만 할 이유가 없다.
            deleted = prune_runs(now)
            if deleted:
                self.stdout.write(f'보관 기간({health.PIPELINE_RUN_RETENTION.days}일) 지난 실행 기록 {deleted}건 정리')
        JobStatus.mark(JobStatus.Kind.HEALTH_CHECK, ok=True,
                       message=f'전체 {LEVEL_LABELS[health.overall_level(checks)]} · {note}')

    def _deliver(self, subject, body, transitions, now):
        """메일을 보내고 상태를 저장한다. 남길 메모를 돌려준다. 발송 실패는 CommandError(종료 1)."""
        if not mail_configured():
            # 상태를 저장하지 않는다 — 메일을 설정한 뒤 첫 점검에서 이 이상이 한 번 나가야 한다.
            self.stdout.write(self.style.WARNING(
                '메일 설정이 없어 알림을 보내지 않습니다 '
                '(.env의 EMAIL_HOST_USER · EMAIL_HOST_PASSWORD · ALERT_EMAIL_TO)'))
            self.stdout.write(f'{subject}\n\n{body}')
            return f'메일 미설정 — 전환 {len(transitions)}건 미발송'
        try:
            send_mail(subject, body, settings.DEFAULT_FROM_EMAIL, settings.ALERT_EMAIL_TO)
        except Exception as exc:  # noqa: BLE001 — SMTP·네트워크 오류 종류가 많다
            message = f'메일 발송 실패: {type(exc).__name__}: {exc}'
            self.stderr.write(message)
            # 성공 시각을 갱신하지 않는다 — 대시보드의 "마지막 점검"이 오래돼 드러난다.
            JobStatus.mark(JobStatus.Kind.HEALTH_CHECK, ok=False, message=message)
            raise CommandError(message)
        save_transitions(transitions, now)
        self.stdout.write(self.style.SUCCESS(f'알림 발송: {subject}'))
        return f'알림 {len(transitions)}건 발송'

    def _send_test(self, now):
        if not mail_configured():
            raise CommandError(
                '메일 설정이 없습니다. .env의 EMAIL_HOST_USER · EMAIL_HOST_PASSWORD · '
                'ALERT_EMAIL_TO를 채우고 컨테이너를 다시 띄우세요.')
        checks = health.run_checks(now)
        body = '\n'.join(
            ['DART 운영 알림 시험 메일입니다. 이 메일이 보이면 알림 설정이 끝난 것입니다.', '',
             '현재 상태']
            + [f'  {LEVEL_LABELS[c.level]:<5} {c.title}: {c.summary}' for c in checks]
            + ['', f'대시보드: {dashboard_url()}'])
        send_mail(f'{SUBJECT_PREFIX} 시험 메일', body,
                  settings.DEFAULT_FROM_EMAIL, settings.ALERT_EMAIL_TO)
        self.stdout.write(self.style.SUCCESS(
            f'시험 메일을 보냈습니다 → {", ".join(settings.ALERT_EMAIL_TO)}'))
