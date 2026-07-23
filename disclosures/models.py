from django.db import models


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
    raw_fetched = models.BooleanField('원문 확보 여부', default=False)
    raw_content = models.TextField('원문 본문(전처리)', blank=True)
    created_at = models.DateTimeField('수집 시각', auto_now_add=True)

    class Meta:
        verbose_name = '공시'
        verbose_name_plural = '공시'
        ordering = ['-filed_at', '-rcept_no']
        indexes = [
            models.Index(fields=['company', '-filed_at']),
        ]

    def __str__(self):
        return f'[{self.company.name}] {self.report_name}'


class DisclosureSummary(models.Model):
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
    is_reviewed = models.BooleanField('검수 여부', default=False)
    created_at = models.DateTimeField('생성 시각', auto_now_add=True)

    class Meta:
        verbose_name = 'AI 요약'
        verbose_name_plural = 'AI 요약'

    def __str__(self):
        return f'요약: {self.disclosure}'
