from django.contrib import admin
from django.db.models import Case, IntegerField, Q, When
from django.utils import timezone
from django.utils.html import format_html

from .models import Company, Disclosure, DisclosureSummary, Sector

#: 일괄 숨김 액션이 남기는 기본 사유. 개별 사유는 변경 화면에서 덧쓴다.
DEFAULT_HIDDEN_REASON = '검수자 일괄 숨김 처리'


@admin.register(Sector)
class SectorAdmin(admin.ModelAdmin):
    list_display = ('name', 'slug')
    prepopulated_fields = {'slug': ('name',)}


@admin.register(Company)
class CompanyAdmin(admin.ModelAdmin):
    list_display = ('name', 'stock_code', 'corp_code', 'sector', 'sub_category', 'is_active')
    list_filter = ('sector', 'is_active')
    search_fields = ('name', 'stock_code', 'corp_code')


class DisclosureSummaryInline(admin.StackedInline):
    """공시 화면에서 요약을 **읽기 전용**으로 곁들여 보여준다.

    수정은 'AI 요약' 화면에서만 한다. 인라인 저장은 DisclosureSummaryAdmin.save_model을
    타지 않으므로, 여기서 본문을 고칠 수 있게 두면 사람 수정 감지(edited_by_human ·
    llm_original)가 통째로 우회되어 검수 이력이 비게 된다.
    """

    model = DisclosureSummary
    extra = 0
    can_delete = False
    readonly_fields = (
        'one_line', 'easy_explanation', 'why_important', 'importance',
        'model_name', 'evidence', 'review_warnings',
        'is_reviewed', 'is_published', 'hidden_reason',
        'edited_by_human', 'llm_original', 'reviewed_by', 'reviewed_at', 'created_at',
    )

    def has_add_permission(self, request, obj=None):
        # 요약은 요약 파이프라인만 만든다(공시당 1회 원칙, PLAN.md 11).
        return False


@admin.register(Disclosure)
class DisclosureAdmin(admin.ModelAdmin):
    list_display = (
        'rcept_no', 'company', 'disclosure_type', 'report_name', 'filed_at',
        'selection_state', 'exclusion_reason', 'raw_fetched',
    )
    # 선별 상태·제외 사유로 걸러 봐야 "왜 이 공시는 요약이 없나"를 화면에서 확인할 수 있다.
    list_filter = (
        'company', 'disclosure_type', 'filed_at',
        'selection_state', 'exclusion_reason', 'raw_fetched',
    )
    search_fields = ('rcept_no', 'report_name')
    date_hierarchy = 'filed_at'
    inlines = [DisclosureSummaryInline]


class HasWarningsFilter(admin.SimpleListFilter):
    """자동 검증 경고가 붙은 요약만 걸러 본다.

    JSONField는 기본 list_filter로 못 거는데, 검수 담당자가 가장 먼저 찾아야 하는 것이
    "경고가 달린 요약"이므로 전용 필터를 둔다.
    """

    title = '자동 검증 경고'
    parameter_name = 'has_warnings'

    def lookups(self, request, model_admin):
        return (('yes', '경고 있음'), ('no', '경고 없음'))

    def queryset(self, request, queryset):
        if self.value() == 'yes':
            return queryset.exclude(review_warnings=[])
        if self.value() == 'no':
            return queryset.filter(review_warnings=[])
        return queryset


class NeedsReviewFilter(admin.SimpleListFilter):
    """검수 대기 큐를 그대로 뽑는 필터.

    `DisclosureSummary.needs_review`는 property라 list_filter에 못 올린다. 조건을 SQL로
    한 번 더 적되 의미는 모델과 동일하게 유지한다(미검수 && (중요도 높음 || 경고 있음)).
    """

    title = '검수 필요'
    parameter_name = 'needs_review'

    def lookups(self, request, model_admin):
        return (('yes', '검수 필요'), ('no', '검수 불필요'))

    def queryset(self, request, queryset):
        condition = Q(is_reviewed=False) & (
            Q(importance=DisclosureSummary.Importance.HIGH) | ~Q(review_warnings=[])
        )
        if self.value() == 'yes':
            return queryset.filter(condition)
        if self.value() == 'no':
            return queryset.exclude(condition)
        return queryset


@admin.register(DisclosureSummary)
class DisclosureSummaryAdmin(admin.ModelAdmin):
    list_display = (
        'disclosure', 'importance', 'warning_count', 'needs_review',
        'is_reviewed', 'is_published', 'edited_by_human', 'reviewed_by',
        'dart_link', 'created_at',
    )
    list_filter = (
        'importance', 'is_reviewed', 'is_published', 'edited_by_human',
        NeedsReviewFilter, HasWarningsFilter,
    )
    search_fields = ('disclosure__report_name', 'one_line')
    # 본문 3필드는 검수자가 직접 고친다(4단계의 핵심). 자동 검증 산출물과 감사 기록은
    # 손으로 못 고치게 막는다 — 경고를 지워 문제를 덮거나 LLM 원본을 잃으면 안 된다.
    readonly_fields = (
        'disclosure', 'dart_link', 'evidence', 'review_warnings',
        'edited_by_human', 'llm_original', 'reviewed_by', 'reviewed_at', 'created_at',
    )
    fieldsets = (
        ('공시', {'fields': ('disclosure', 'dart_link')}),
        ('요약 본문 (검수자가 직접 수정)', {
            'fields': ('one_line', 'easy_explanation', 'why_important', 'importance'),
        }),
        ('자동 검증', {'fields': ('review_warnings', 'evidence')}),
        ('검수', {'fields': ('is_reviewed', 'is_published', 'hidden_reason')}),
        ('기록', {
            'fields': ('edited_by_human', 'llm_original', 'reviewed_by', 'reviewed_at',
                       'model_name', 'created_at'),
        }),
    )
    actions = ('mark_reviewed', 'hide_summaries', 'restore_summaries')

    def has_add_permission(self, request):
        """요약은 손으로 만들지 않는다 — 요약 파이프라인만 만든다(PLAN.md 11).

        인라인(`DisclosureSummaryInline`)이 이미 같은 이유로 추가를 막고 있어 두 화면의
        정책을 맞춘다. 겸해서 `disclosure`가 읽기 전용이라 추가 폼에서 빠지는 탓에
        저장 시 disclosure_id가 NULL로 터지던 문제(qa 결함 1)도 없어진다.
        """
        return False

    def get_queryset(self, request):
        # 목록에서 공시·기업·검수자를 매 행 참조하므로 N+1을 막는다.
        return super().get_queryset(request).select_related(
            'disclosure', 'disclosure__company', 'reviewed_by'
        )

    def get_ordering(self, request):
        """검수 대기 우선순위: 중요도 높음 → 경고 있음 → 접수일 최신.

        검수 큐가 56건 쌓여 있는 상태에서 기본 정렬이 생성 시각이면 담당자가 매번
        "무엇부터 볼지"를 스스로 고르게 된다. 위험이 큰 것부터 위로 올린다.

        `ordering` 속성이나 주석(annotate) 대신 정렬식을 직접 돌려주는 이유:
        ModelAdmin.get_queryset()이 **주석을 붙일 틈 없이** 먼저 order_by를 적용하므로
        별칭 이름을 쓰면 FieldError가 난다. 열 머리글 클릭 정렬은 그대로 동작한다.
        """
        return (
            Case(
                When(importance=DisclosureSummary.Importance.HIGH, then=0),
                default=1, output_field=IntegerField(),
            ).asc(),
            Case(
                When(review_warnings=[], then=1),
                default=0, output_field=IntegerField(),
            ).asc(),
            '-disclosure__filed_at',
        )

    @admin.display(description='경고 수', ordering='review_warnings')
    def warning_count(self, obj):
        count = len(obj.review_warnings or [])
        return f'⚠ {count}' if count else '-'

    @admin.display(description='검수 필요', boolean=True)
    def needs_review(self, obj):
        return obj.needs_review

    @admin.display(description='DART 원문')
    def dart_link(self, obj):
        """DART 원문을 새 탭으로 연다.

        검수는 요약과 원문을 나란히 놓고 보는 작업이라 같은 탭에서 이동하면 목록 위치와
        필터를 잃는다. 저장된 dart_url만 쓰고 DART API는 부르지 않는다(PLAN.md 12.1).
        """
        if not obj.disclosure_id or not obj.disclosure.dart_url:
            return '-'
        return format_html(
            '<a href="{}" target="_blank" rel="noopener noreferrer">원문 보기 ↗</a>',
            obj.disclosure.dart_url,
        )

    def save_model(self, request, obj, form, change):
        """사람의 손이 닿았는지를 저장 직전에 판정한다.

        판정 기준은 **DB에 있는 값과 저장하려는 값의 차이**다. 폼의 changed_data가 아니라
        DB 값을 보는 이유는, 어떤 경로로 저장하든(폼·스크립트) 같은 규칙이 적용되고
        "값을 눌렀다 되돌린" 경우를 수정으로 세지 않기 때문이다.

        llm_original은 **직전 값이 아직 LLM 것일 때 한 번만** 기록한다. 두 번째 수정에서
        덮어쓰면 사람이 고친 문장을 'LLM 원본'으로 오인하게 되어 원본 추적이 무의미해진다.
        """
        previous = None
        if change and obj.pk:
            previous = DisclosureSummary.objects.filter(pk=obj.pk).first()

        if previous is not None:
            body_changed = any(
                getattr(previous, field) != getattr(obj, field)
                for field in DisclosureSummary.BODY_FIELDS
            )
            if body_changed:
                if not previous.edited_by_human and not previous.llm_original:
                    obj.llm_original = previous.body_snapshot()
                obj.edited_by_human = True

            # 이미 기록된 LLM 원본은 어떤 저장에서도 지워지지 않는다.
            # 이 줄이 body_changed 분기 **밖**에 있는 이유: 본문을 안 고친 저장(예: 검수
            # 체크만)에서도 빈 llm_original이 실려 오면 기존 스냅샷이 날아간다. 지금은
            # llm_original이 읽기 전용이라 admin 폼으로는 그런 값이 못 들어오지만,
            # 감사 기록의 보존이 화면 설정 하나에만 걸려 있으면 안 된다.
            obj.llm_original = obj.llm_original or previous.llm_original

            # 검수 여부를 폼에서 직접 켜고 끈 경우에도 책임 소재를 일괄 액션과 똑같이 남긴다.
            if obj.is_reviewed and not previous.is_reviewed:
                obj.reviewed_by = request.user
                obj.reviewed_at = timezone.now()
            elif not obj.is_reviewed and previous.is_reviewed:
                # 검수를 되돌렸으면 검수 기록도 지운다. 남겨두면 "누가 검수했다"는
                # 잘못된 이력이 된다.
                obj.reviewed_by = None
                obj.reviewed_at = None

        super().save_model(request, obj, form, change)

    @admin.action(description='선택한 요약을 검수 완료 처리')
    def mark_reviewed(self, request, queryset):
        """검수 완료 도장. 완료 즉시 '미검수' 배지와 정확성 경고 배너가 웹에서 걷힌다
        (`accuracy_warnings`가 is_reviewed면 빈 리스트를 돌려주는 기존 규칙).

        이미 검수된 건이 섞여 있어도 검수자·시각을 최신으로 덮는다 — 다시 도장을 찍는
        행위는 "지금 이 사람이 다시 확인했다"는 뜻이기 때문이다.
        """
        updated = queryset.update(
            is_reviewed=True, reviewed_by=request.user, reviewed_at=timezone.now()
        )
        self.message_user(request, f'{updated}건을 검수 완료 처리했습니다.')

    @admin.action(description='선택한 요약을 웹에서 숨김')
    def hide_summaries(self, request, queryset):
        """웹 노출만 끊는다. 요약 자체는 지우지 않는다 — 삭제하면 재수집 때 LLM을 다시
        불러 비용이 발생하고, 왜 내렸는지 기록도 사라진다.

        사유는 이미 적혀 있으면 보존하고 비어 있을 때만 기본 문구를 채운다.
        """
        updated = queryset.update(is_published=False)
        blank_filled = queryset.filter(hidden_reason='').update(
            hidden_reason=DEFAULT_HIDDEN_REASON
        )
        self.message_user(
            request,
            f'{updated}건을 웹에서 숨겼습니다(사유 자동 기입 {blank_filled}건).',
        )

    @admin.action(description='선택한 요약의 노출 복구')
    def restore_summaries(self, request, queryset):
        """숨김 해제. 사유도 함께 지운다 — 노출 중인 요약에 숨김 사유가 남아 있으면
        다음 검수자가 현재 상태를 오해한다.
        """
        updated = queryset.update(is_published=True, hidden_reason='')
        self.message_user(request, f'{updated}건의 노출을 복구했습니다.')
