"""작업 하나의 성공·실패를 JobStatus에 적는다 (10단계, 이슈 #56).

파이프라인 밖의 셸 스크립트가 쓴다 — 지금은 `deploy/backup.sh`뿐이다. Django 명령
(poll_dart·sync_listed_corps)은 이 명령을 거치지 않고 끝에서 직접 적는다.

**같은 잠금 안에서 부를 것.** DB에 쓰는 프로세스를 하나로 유지하는 것이 SQLite를 쓰는
근거다(PLAN.md 9.3). backup.sh는 `flock -w 600 /tmp/dart-pipeline.lock`으로 감싼다.

사용법:
  python manage.py record_job backup --message s3://…/db_20261024_040000.sqlite3.gz
  python manage.py record_job backup --failed --message "S3 업로드 실패"
"""
from django.core.management.base import BaseCommand

from disclosures.models import JobStatus


class Command(BaseCommand):
    help = '작업의 마지막 성공(또는 --failed면 실패) 시각을 기록한다'

    def add_arguments(self, parser):
        parser.add_argument('kind', choices=JobStatus.Kind.values, help='작업 종류')
        parser.add_argument('--failed', action='store_true', help='실패로 기록한다')
        parser.add_argument('--message', default='', help='남길 메시지 (300자에서 자른다)')

    def handle(self, *args, **options):
        status = JobStatus.mark(options['kind'], ok=not options['failed'],
                                message=options['message'])
        self.stdout.write(
            f'{status.get_kind_display()} {"실패" if options["failed"] else "성공"} 기록')
