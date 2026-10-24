"""일이 있었던 파이프라인 실행 1건을 PipelineRun에 남긴다 (10단계, 이슈 #56).

`deploy/pipeline.sh`가 끝날 때(EXIT trap) 부른다. 이미 파이프라인 잠금 안이다.
**헛도는 실행(신규 없음 · 대기 없음)에서는 부르지 않는다** — 밤·주말 매분 실행을 다 쌓으면
하루 1,440행이다(PLAN.md 9.6).

건수는 스크립트 출력을 파싱하지 않고 DB에서 `--started-at` 이후의 변화로 센다. 출력 문구를
고칠 때마다 기록이 조용히 0이 되는 일을 막기 위해서다. 쓰기 주체가 하나(잠금)라 그 사이의
변화는 전부 이 실행의 것이다.

사용법:
  python manage.py record_run --mode detect --started-at 2026-10-24T10:01:00+09:00 \\
      --exit-code 0 --mem-min-mb 312
"""
from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from disclosures.models import Disclosure, DisclosureSummary, PipelineRun


class Command(BaseCommand):
    help = '일이 있었던 파이프라인 실행 1건을 기록한다 (건수는 DB에서 센다)'

    def add_arguments(self, parser):
        parser.add_argument('--mode', required=True, choices=PipelineRun.Mode.values)
        parser.add_argument('--started-at', required=True,
                            help='실행 시작 시각 ISO 8601 (`date -Is`)')
        parser.add_argument('--exit-code', type=int, required=True)
        parser.add_argument('--mem-min-mb', type=int, default=None)
        parser.add_argument('--message', default='')

    def handle(self, *args, **options):
        started = parse_datetime(options['started_at'])
        if started is None:
            raise CommandError(f'--started-at 형식이 잘못되었습니다: {options["started_at"]!r}')
        if timezone.is_naive(started):
            started = timezone.make_aware(started)
        exit_code = options['exit_code']

        run = PipelineRun.objects.create(
            mode=options['mode'], started_at=started, finished_at=timezone.now(),
            ok=exit_code == 0, exit_code=exit_code,
            new_disclosures=Disclosure.objects.filter(created_at__gte=started).count(),
            fetched=Disclosure.objects.filter(raw_fetched_at__gte=started).count(),
            summarized=DisclosureSummary.objects.filter(created_at__gte=started).count(),
            mem_min_mb=options['mem_min_mb'],
            message=(options['message'] or ('' if exit_code == 0 else f'종료 코드 {exit_code}'))[:300],
        )
        self.stdout.write(
            f'실행 기록: {run.get_mode_display()} · {"성공" if run.ok else "실패"} · '
            f'신규 {run.new_disclosures} · 원문 {run.fetched} · 요약 {run.summarized}')
