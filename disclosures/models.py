from django.conf import settings
from django.db import models

from .selection import ExclusionReason, SelectionState


class Sector(models.Model):
    name = models.CharField('섹터명', max_length=50, unique=True)
    slug = models.SlugField('슬러그', max_length=50, unique=True)
    description = models.TextField('설명', blank=True)

    class Meta:
        verbose_name = '섹터'
        verbose_name_plural = '섹터'

    def __str__(self):
        return self.name


class Company(models.Model):
    sector = models.ForeignKey(
        Sector, on_delete=models.PROTECT, related_name='companies', verbose_name='섹터'
    )
    corp_code = models.CharField('DART 고유번호', max_length=8, unique=True)
    stock_code = models.CharField('종목코드', max_length=6, unique=True)
    name = models.CharField('기업명', max_length=100)
    sub_category = models.CharField('서브 카테고리', max_length=50, blank=True)
    is_active = models.BooleanField('추적 여부', default=True)

    class Meta:
        verbose_name = '기업'
        verbose_name_plural = '기업'
        ordering = ['name']

    def __str__(self):
        return f'{self.name} ({self.stock_code})'


class Disclosure(models.Model):
    company = models.ForeignKey(
        Company, on_delete=models.CASCADE, related_name='disclosures', verbose_name='기업'
    )
    rcept_no = models.CharField('접수번호', max_length=14, unique=True)
    report_name = models.CharField('공시 제목', max_length=300)
    disclosure_type = models.CharField('공시 유형', max_length=50, blank=True)
    filed_at = models.DateField('접수일자')
    dart_url = models.URLField('원문 링크', max_length=300)
    # 선별 결과를 DB에 남겨야 "아직 안 본 공시"와 "의도적으로 제외한 공시"가 구분되고,
    # 매 실행마다 전건을 재평가하지 않는다. 정책은 disclosures/selection.py가 단일 출처.
    selection_state = models.CharField(
        '선별 상태', max_length=10,
        choices=SelectionState.choices, default=SelectionState.PENDING,
    )
    exclusion_reason = models.CharField(
        '제외 사유', max_length=20,
        choices=ExclusionReason.choices, blank=True, default='',
    )
    raw_fetched = models.BooleanField('원문 확보 여부', default=False)
    raw_content = models.TextField('원문 본문(전처리)', blank=True)
    created_at = models.DateTimeField('수집 시각', auto_now_add=True)

    class Meta:
        verbose_name = '공시'
        verbose_name_plural = '공시'
        ordering = ['-filed_at', '-rcept_no']
        indexes = [
            models.Index(fields=['company', '-filed_at']),
            # 요약 대상 중 원문 미확보분 조회(fetch_documents)를 위한 인덱스
            models.Index(fields=['selection_state', 'raw_fetched']),
        ]

    def __str__(self):
        return f'[{self.company.name}] {self.report_name}'

    @property
    def is_summary_target(self):
        return self.selection_state == SelectionState.TARGET


class DisclosureSummary(models.Model):
    #: 사람이 검수 화면에서 직접 고칠 수 있는 요약 본문 필드.
    #: admin의 변경 감지와 llm_original 스냅샷이 같은 목록을 봐야 하므로 여기가 단일 출처다.
    BODY_FIELDS = ('one_line', 'easy_explanation', 'why_important')

    class Importance(models.TextChoices):
        HIGH = 'high', '높음'
        MEDIUM = 'medium', '보통'
        LOW = 'low', '낮음'

    disclosure = models.OneToOneField(
        Disclosure, on_delete=models.CASCADE, related_name='summary', verbose_name='공시'
    )
    one_line = models.CharField('한 줄 요약', max_length=200)
    easy_explanation = models.TextField('쉬운 설명')
    why_important = models.TextField('왜 중요한가')
    importance = models.CharField(
        '중요도', max_length=10, choices=Importance.choices, default=Importance.MEDIUM
    )
    model_name = models.CharField('사용 LLM', max_length=50, blank=True)
    # summarizer.summarize_disclosure()가 돌려주는 원문 근거 배열.
    # [{'field','claim','quote','quote_found','numbers_ok','missing_numbers'}, ...]
    # 저장하지 않으면 QA가 숫자 대조를 재검증할 때마다 LLM을 다시 불러야 해서
    # "공시당 1회" 원칙이 깨지고, 사후에 요약 오류가 나와도 출처를 추적할 수 없다.
    evidence = models.JSONField('원문 근거', default=list, blank=True)
    # 자동 검증이 잡아낸 경고(근거 없는 수치, 인용 미발견, 문장 수 일탈 등).
    # is_reviewed 불리언만으로는 "아직 검수 안 함"과 "검수가 필요함"이 구분되지 않는다.
    review_warnings = models.JSONField('자동 검증 경고', default=list, blank=True)
    is_reviewed = models.BooleanField('검수 여부', default=False)
    # 검수자가 "이 요약은 내보내면 안 된다"고 판단했을 때 쓰는 스위치.
    # 요약을 지우면 재수집 시 LLM을 다시 부르게 되므로 삭제 대신 감춘다(PLAN.md 11).
    is_published = models.BooleanField('노출 여부', default=True)
    hidden_reason = models.CharField('숨김 사유', max_length=200, blank=True, default='')
    edited_by_human = models.BooleanField('사람 수정 여부', default=False)
    # 사람이 본문을 처음 고치는 순간의 LLM 출력 스냅샷.
    # {'one_line':…, 'easy_explanation':…, 'why_important':…} 형태이며 이후 덮어쓰지 않는다.
    # 덮어쓰면 "LLM이 원래 뭐라고 했는지"를 잃어 프롬프트 개선의 근거가 사라진다.
    llm_original = models.JSONField('LLM 원본', default=dict, blank=True)
    # 검수 책임 소재를 남긴다. 사용자가 지워져도 검수 사실 자체는 보존해야 하므로 SET_NULL.
    reviewed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL,
        null=True, blank=True, related_name='reviewed_summaries', verbose_name='검수자',
    )
    reviewed_at = models.DateTimeField('검수 시각', null=True, blank=True)
    created_at = models.DateTimeField('생성 시각', auto_now_add=True)

    class Meta:
        verbose_name = 'AI 요약'
        verbose_name_plural = 'AI 요약'

    def __str__(self):
        return f'요약: {self.disclosure}'

    def body_snapshot(self):
        """현재 요약 본문 3필드의 스냅샷. 변경 감지와 llm_original 기록이 함께 쓴다."""
        return {field: getattr(self, field) for field in self.BODY_FIELDS}

    @property
    def needs_review(self):
        """사람 검수가 필요한 요약인지.

        중요도 '높음'은 PLAN.md 11의 검수 게이트 대상이고, 자동 검증 경고가 붙은 요약은
        근거 대조에서 걸린 것이므로 게시 전에 사람이 봐야 한다.
        """
        return not self.is_reviewed and (
            self.importance == self.Importance.HIGH or bool(self.review_warnings)
        )

    @property
    def was_human_edited(self):
        """웹에 "사람이 검토·수정함"을 표시할지 여부.

        수정만으로는 부족하고 검수 완료(is_reviewed)까지 되어야 한다. 고치다 만 상태를
        "사람이 검토했다"고 내보내면 사용자에게 실제보다 강한 신뢰 신호를 주게 된다.
        """
        return self.edited_by_human and self.is_reviewed

    @property
    def accuracy_warnings(self):
        """사실 정확성과 직접 관련된 경고만 추린다(문체 경고 제외).

        화면의 '수치 확인 필요' 배너 조건이다. 문장 수 같은 문체 경고까지 배너를 띄우면
        경고가 흔해져 사용자가 무시하게 되고, 정작 수치가 틀린 요약을 놓친다.
        검수가 끝나면(is_reviewed=True) 배너를 걷는다 — 사람이 이미 확인했기 때문이다.
        """
        from .summarizer import ACCURACY_WARNING_PREFIXES

        if self.is_reviewed:
            return []
        return [
            warning for warning in (self.review_warnings or [])
            if warning.startswith(ACCURACY_WARNING_PREFIXES)
        ]

    @property
    def unsupported_numbers(self):
        """원문 근거로 뒷받침되지 않은 수치 표기 목록. 배너에 그대로 보여준다.

        "일부 수치가 확인되지 않았다"고만 하면 사용자가 어느 숫자를 의심해야 할지 모른다.
        실제로 SK하이닉스 유상증자 요약이 39조 8,905억을 3조 9,891억으로 10배 잘못 적은
        사례가 있었고, 그 값을 짚어주는 것과 아닌 것은 확인 난이도가 다르다.
        """
        from .summarizer import UNSUPPORTED_NUMBER_PREFIX, WARNING_LIST_SEPARATOR

        numbers = []
        for warning in self.accuracy_warnings:
            if warning.startswith(UNSUPPORTED_NUMBER_PREFIX):
                body = warning[len(UNSUPPORTED_NUMBER_PREFIX):]
                numbers.extend(
                    part.strip()
                    for part in body.split(WARNING_LIST_SEPARATOR)
                    if part.strip()
                )
        return numbers
