from django.conf import settings
from django.db import models

from .review_policy import ReviewCategory
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


#: 요약 생성을 이 횟수만큼 실패하면 더 시도하지 않는다.
#:
#: 상한이 **모델 쪽에 있는 이유**는 화면도 이 값을 봐야 하기 때문이다. 상한에 걸린
#: 공시는 views.published_disclosures()에서 "AI가 정리 중" 목록에서도 빠져야 한다 —
#: 오지 않을 요약을 계속 기다리게 하면 안 된다. 원문 확보 상한
#: (fetch_documents.MAX_FETCH_ATTEMPTS)이 명령 안에 있는 것과 다른 점이다.
#: 그쪽은 화면이 알 필요가 없다(원문을 못 받으면 애초에 노출 대상이 아니다).
MAX_SUMMARY_ATTEMPTS = 4


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
    # 사람 검수 게이트 대상 여부를 **공시 제목**으로 판정한 결과. 빈 문자열이면 비대상.
    # AI가 매긴 중요도로 게이트를 걸면 "AI를 못 믿어서 하는 검수"의 대상 선정을 AI에게
    # 맡기게 된다. 정책은 disclosures/review_policy.py가 단일 출처이며,
    # selection_state와 같은 이유로 판정 결과를 DB에 남긴다 — 제목 정규식을 SQL로 옮겨
    # 적으면 모델과 admin 필터가 조용히 어긋난다.
    review_category = models.CharField(
        '검수 필수 유형', max_length=20,
        choices=ReviewCategory.choices, blank=True, default='',
    )
    raw_fetched = models.BooleanField('원문 확보 여부', default=False)
    raw_content = models.TextField('원문 본문(전처리)', blank=True)
    # 원문 확보 재시도 제어. 목록에는 떴는데 원문이 아직 공개되지 않은 공시(DART [014])는
    # 확보에 실패하고, 미확보 상태로 남아 다음 실행에서 다시 시도된다. 하루 1회 폴링에서는
    # 무해했지만 7단계에서 파이프라인이 1분마다 도는 순간 같은 공시를 하루 1,440번 부르게
    # 된다. 시도 횟수와 마지막 시각을 남겨 간격을 벌리고 상한에서 멈춘다.
    #
    # 마지막 오류를 함께 남기는 이유: 상한에 걸려 멈춘 공시가 "아직 안 올라온 것"인지
    # "우리 코드가 깨진 것"인지 구분해야 한다. 둘은 대응이 정반대다.
    raw_fetch_attempts = models.PositiveSmallIntegerField('원문 확보 시도 횟수', default=0)
    raw_fetch_attempted_at = models.DateTimeField(
        '원문 확보 마지막 시도', null=True, blank=True
    )
    raw_fetch_error = models.CharField('원문 확보 마지막 오류', max_length=300, blank=True)
    # 요약 생성 재시도 제어. 요약에 실패하면 DisclosureSummary 행이 만들어지지 않아
    # 다음 실행에서 그대로 다시 대상이 된다. 하루 1회 폴링에서는 하루 1번이었지만,
    # 7단계에서 파이프라인이 1분마다 돌면 같은 공시로 **LLM을 매분 부른다**.
    # 원문 확보(raw_fetch_*)와 같은 구조지만 실패 비용이 다르다 — 그쪽은 DART 호출
    # 한도만 쓰고, 이쪽은 실제 돈이 나간다(PLAN.md 11).
    summary_attempts = models.PositiveSmallIntegerField('요약 시도 횟수', default=0)
    summary_attempted_at = models.DateTimeField('요약 마지막 시도', null=True, blank=True)
    summary_error = models.CharField('요약 마지막 오류', max_length=300, blank=True)
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

    @property
    def is_summary_stuck(self):
        """요약을 상한만큼 실패해 더 시도하지 않는 상태인지.

        화면에서 "AI가 정리 중"으로 내보내면 안 되는 조건이다. 만들어지지 않을 요약을
        계속 기다리게 하는 셈이기 때문이다. 노출에서 빼는 일은
        views.published_disclosures()가 한다 — 이 속성은 판정만 한다.
        """
        return self.summary_attempts >= MAX_SUMMARY_ATTEMPTS

    @property
    def is_summary_pending(self):
        """요약이 아직 없어 화면에 "정리 중"으로 보일 상태인지 (PLAN.md 9.3).

        노출 대상인지는 views.published_disclosures()가 이미 걸러서 온다. 여기서
        선별 상태나 원문 확보 여부를 다시 보지 않는 이유는, 판정이 두 곳에 있으면
        한쪽만 고쳤을 때 조용히 어긋나기 때문이다. 이 속성은 **"요약이 있는가"만**
        답한다.

        hasattr을 쓰는 것은 역방향 OneToOne이 없을 때 예외를 던지기 때문이다.
        목록은 select_related('summary')로 가져오므로 추가 쿼리가 발생하지 않는다.
        """
        return not hasattr(self, 'summary')


class DisclosureSummary(models.Model):
    #: 사람이 검수 화면에서 직접 고칠 수 있는 요약 본문 필드.
    #: admin의 변경 감지와 llm_original 스냅샷이 같은 목록을 봐야 하므로 여기가 단일 출처다.
    BODY_FIELDS = ('one_line', 'easy_explanation', 'why_important')

    class Importance(models.TextChoices):
        HIGH = 'high', '높음'
        MEDIUM = 'medium', '보통'
        LOW = 'low', '낮음'

    class HiddenBy(models.TextChoices):
        """미게시 상태를 **누가 만들었는지**. 게시 중이면 빈 문자열이다."""

        AUTO = 'auto', '자동 미게시(검증 실패)'
        HUMAN = 'human', '검수자가 내림'

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
    # 이 요약을 만든 시스템 프롬프트 버전(summarizer.PROMPT_VERSION).
    #
    # 모델명만으로는 부족하다. 같은 모델이라도 프롬프트를 고치면 출력 성격이 달라지는데,
    # 그 경계를 모르면 "프롬프트를 고쳐서 나아진 것"과 "원래 그랬던 것"을 구별할 수 없다.
    # 실제로 v2→v3 효과를 재려 할 때 어느 요약이 어느 버전인지 알 수 없어 측정이 막혔다.
    #
    # 빈 문자열은 **버전을 모른다**는 뜻이다(이 필드가 생기기 전에 만들어진 요약).
    # 모르는 것을 v2로 단정해 채우지 않는다 — 틀린 값이 들어가면 이 필드의 존재 이유인
    # "측정 가능성"이 오히려 무너진다.
    prompt_version = models.CharField(
        '프롬프트 버전', max_length=10, blank=True, default='',
    )
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
    # 내려간 요약을 **자동 검증이 막은 것**과 **사람이 판단해 내린 것**으로 가른다.
    # is_published=False 하나만으로는 둘이 구분되지 않는데, 검수자가 할 일이 정반대다:
    # 자동 미게시는 "고쳐서 올릴 것"이고 사람이 내린 것은 "이미 결론이 난 것"이다.
    # hidden_reason 문구로 구분하지 않는 이유는 그 필드가 사람이 자유롭게 고치는
    # 자유 문자열이라, 검수자가 한 번 고쳐 쓰면 기계 판정이 무너지기 때문이다.
    hidden_by = models.CharField(
        '미게시 주체', max_length=10,
        choices=HiddenBy.choices, blank=True, default='',
    )
    # 검증 실패로 다시 생성한 횟수. review_policy.MAX_REGENERATION_ATTEMPTS 가 상한이며,
    # 이 값이 상한에 닿으면 더 부르지 않고 사람 검수 큐로 넘긴다(무한 재시도 = 무한 비용).
    regeneration_count = models.PositiveSmallIntegerField('재생성 시도 횟수', default=0)
    # 시도별 이력. [{'attempt', 'at', 'reason', 'warnings', 'model', 'cost_usd'}, ...]
    # 횟수만 남기면 "무엇을 고치려다 실패했는지"를 잃어 프롬프트 개선의 근거가 사라진다.
    regeneration_history = models.JSONField('재생성 이력', default=list, blank=True)
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

        두 갈래다.
          - **공시 유형 게이트**: DART 제목으로 판정한 고위험 유형(`review_policy.py`).
            경고가 하나도 없어도 사람이 본다.
          - **자동 검증 경고**: 근거 대조에서 걸린 요약은 유형과 무관하게 본다.

        게이트를 AI의 `importance == 'high'` 에서 공시 유형으로 옮긴 이력이 있다.
        AI 요약을 믿지 못해 하는 검수인데 그 대상 선정을 같은 AI에게 맡기고 있었고,
        AI가 중요도를 낮게 잘못 매기면 가장 위험한 요약이 큐에 아예 안 떴다.

        admin의 `NeedsReviewFilter` 가 같은 조건을 SQL로 한 번 더 적는다. 한쪽만 고치면
        검수 큐가 조용히 어긋나므로 반드시 함께 고친다(테스트로 고정되어 있다).
        """
        return not self.is_reviewed and (
            bool(self.disclosure.review_category) or bool(self.review_warnings)
        )

    @property
    def auto_hidden(self):
        """자동 검증이 막아 내려간 요약인지. 검수 화면이 사람 판단과 구분해 표시한다."""
        return not self.is_published and self.hidden_by == self.HiddenBy.AUTO

    @property
    def regeneration_exhausted(self):
        """재생성 상한을 다 써서 더는 자동으로 고칠 수 없는 요약인지.

        사람 검수 큐에서 우선순위가 가장 높은 무리다 — 코드가 두 번 시도해 실패했다.
        """
        from .review_policy import MAX_REGENERATION_ATTEMPTS

        return self.regeneration_count >= MAX_REGENERATION_ATTEMPTS

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
        from .verification import ACCURACY_WARNING_PREFIXES

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
        from .verification import UNSUPPORTED_NUMBER_PREFIX

        return self._numbers_from_warnings(UNSUPPORTED_NUMBER_PREFIX)

    @property
    def uncited_numbers(self):
        """값은 원문에서 확인됐지만 인용 근거에는 없는 수치 목록.

        `unsupported_numbers` 와 **성격이 다르므로 따로 둔다.** 저쪽은 원문 어디에도 없는
        수치라 사실 오류일 수 있지만, 이쪽은 값이 원문과 일치한다. 같은 배너에 같은 문구로
        묶으면 "원문 근거를 찾지 못했습니다"라는 틀린 말을 하게 된다.

        검수자에게는 둘 다 필요하다 — 확인할 숫자를 짚어주는 목적은 같기 때문이다.
        """
        from .verification import UNCITED_NUMBER_PREFIX

        return self._numbers_from_warnings(UNCITED_NUMBER_PREFIX)

    @property
    def numbers_to_review(self):
        """검수자가 원문과 대조해야 할 수치 전체.

        게시 판정에서는 둘을 갈라야 하지만(하나는 사실 오류일 수 있고 하나는 인용 누락일 뿐),
        **검수 화면에서는 할 일이 같다** — 원문에서 찾아 맞는지 본다. 검수 패널의 칩이
        이미 '원문 N곳'과 '원문에 없음'으로 둘을 구분해 보여주므로 합쳐서 넘긴다.
        """
        return self.unsupported_numbers + self.uncited_numbers

    def _numbers_from_warnings(self, prefix):
        """정확성 경고 문구에서 접두어가 맞는 것들의 수치 표기를 뽑는다."""
        from .verification import WARNING_LIST_SEPARATOR

        numbers = []
        for warning in self.accuracy_warnings:
            if warning.startswith(prefix):
                body = warning[len(prefix):]
                numbers.extend(
                    part.strip()
                    for part in body.split(WARNING_LIST_SEPARATOR)
                    if part.strip()
                )
        return numbers
