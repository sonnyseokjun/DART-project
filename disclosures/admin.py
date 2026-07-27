from django.contrib import admin

from .models import Company, Disclosure, DisclosureSummary, Sector


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
    model = DisclosureSummary
    extra = 0


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


@admin.register(DisclosureSummary)
class DisclosureSummaryAdmin(admin.ModelAdmin):
    list_display = (
        'disclosure', 'importance', 'warning_count', 'needs_review',
        'is_reviewed', 'model_name', 'created_at',
    )
    list_filter = ('importance', 'is_reviewed', HasWarningsFilter)
    search_fields = ('disclosure__report_name', 'one_line')
    readonly_fields = ('evidence', 'review_warnings', 'created_at')

    @admin.display(description='경고 수', ordering='review_warnings')
    def warning_count(self, obj):
        count = len(obj.review_warnings or [])
        return f'⚠ {count}' if count else '-'

    @admin.display(description='검수 필요', boolean=True)
    def needs_review(self, obj):
        return obj.needs_review
