"""템플릿 메모가 화면에 새지 않는지 (이슈 #46).

Django의 `{# ... #}` 주석은 **한 줄 안에서만** 동작한다. 여러 줄에 걸치면 주석으로
인식되지 않고 본문으로 출력된다. 운영 사이트의 섹터 화면 한 곳에서만 개발자 메모가
23곳 보이고 있었다(2026-09-22). 기존 화면 테스트는 "보여야 할 문구가 있는가"만 보고
"보이면 안 되는 것이 없는가"는 보지 않아 잡지 못했다.

여러 줄 메모는 `{% comment %}...{% endcomment %}`로 쓴다.
"""
import re
from pathlib import Path

from django.test import TestCase
from django.urls import reverse

from .tests import ReviewWorkflowTestBase, WebViewTestBase

ROOT = Path(__file__).resolve().parent.parent
TEMPLATE_DIRS = [ROOT / 'disclosures' / 'templates', ROOT / 'accounts' / 'templates']
MULTILINE_COMMENT = re.compile(r'\{#(?:(?!#\}).)*\n', re.S)


class NoMultilineCommentTest(TestCase):
    """원인을 막는다 — 여러 줄 `{# #}`가 템플릿에 들어오면 실패한다."""

    def test_templates_have_no_multiline_hash_comments(self):
        offenders = []
        for directory in TEMPLATE_DIRS:
            for path in directory.rglob('*.html'):
                text = path.read_text(encoding='utf-8')
                for match in MULTILINE_COMMENT.finditer(text):
                    line = text.count('\n', 0, match.start()) + 1
                    offenders.append(f'{path.relative_to(ROOT)}:{line}')
        self.assertEqual(
            offenders, [],
            '여러 줄 {# #}는 화면에 그대로 출력된다. {% comment %}로 바꿀 것')

    def test_the_detector_catches_the_bad_form(self):
        """검사 자체가 틀리면 위 테스트는 늘 통과한다. 잡아야 할 모양을 직접 넣어 본다."""
        self.assertTrue(MULTILINE_COMMENT.search('{# 첫 줄\n   둘째 줄 #}'))
        self.assertFalse(MULTILINE_COMMENT.search('{# 한 줄 #}\n<p>본문</p>'))
        self.assertFalse(MULTILINE_COMMENT.search('{# 가 #}\n{# 나 #}'))


def _leaks(html):
    return [marker for marker in ('{#', '#}') if marker in html]


class PublicPagesShowNoCommentsTest(WebViewTestBase):
    """결과를 본다 — 사용자 화면에 메모 표식이 없다."""

    def test_member_pages(self):
        urls = [
            reverse('disclosures:sector_list'),
            reverse('disclosures:sector_detail', args=['semiconductor']),
            reverse('disclosures:company_detail', args=['005930']),
            reverse('disclosures:disclosure_detail', args=['20260701000001']),
            reverse('accounts:privacy'),
            reverse('accounts:withdraw'),
        ]
        for url in urls:
            with self.subTest(url=url):
                self.assertEqual(_leaks(self.client.get(url).content.decode()), [])

    def test_anonymous_pages(self):
        self.client.logout()
        for url in (reverse('disclosures:sector_list'),
                    reverse('disclosures:disclosure_detail', args=['20260701000001']),
                    reverse('accounts:privacy')):
            with self.subTest(url=url):
                self.assertEqual(_leaks(self.client.get(url).content.decode()), [])


class ReviewScreenShowsNoCommentsTest(ReviewWorkflowTestBase):
    """검수 화면(admin)도 본다. 검수 패널 템플릿에 여러 줄 메모가 4개 있었다."""

    def test_review_change_form(self):
        """메모 4개는 전부 조건부 구역 안에 있었다. 그 구역들이 그려지는 요약을 만든다.

        평범한 요약 하나로는 이 테스트가 고치기 전에도 통과했다 — 자동 미게시·유형 게이트·
        재생성 이력 구역이 그려지지 않아 메모가 출력될 기회가 없었다.
        """
        from .models import DisclosureSummary
        from .review_policy import ReviewCategory

        summary = self.make_summary(
            1, review_category=ReviewCategory.CAPITAL,
            is_published=False, hidden_by=DisclosureSummary.HiddenBy.AUTO,
            hidden_reason='검증 실패', regeneration_count=1,
            regeneration_history=[{'attempt': 1, 'reason': '수치 불일치'}],
        )
        url = reverse('admin:disclosures_disclosuresummary_change', args=[summary.pk])
        self.assertEqual(_leaks(self.client.get(url).content.decode()), [])
