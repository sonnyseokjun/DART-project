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
    list_display = ('rcept_no', 'company', 'report_name', 'filed_at', 'raw_fetched')
    list_filter = ('company', 'filed_at', 'raw_fetched')
    search_fields = ('rcept_no', 'report_name')
    date_hierarchy = 'filed_at'
    inlines = [DisclosureSummaryInline]


@admin.register(DisclosureSummary)
class DisclosureSummaryAdmin(admin.ModelAdmin):
    list_display = ('disclosure', 'importance', 'is_reviewed', 'model_name', 'created_at')
    list_filter = ('importance', 'is_reviewed')
    search_fields = ('disclosure__report_name', 'one_line')
