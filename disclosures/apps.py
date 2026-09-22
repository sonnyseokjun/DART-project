from django.apps import AppConfig


class DisclosuresConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'disclosures'

    def ready(self):
        # 관심 기업 삭제 신호를 등록한다. 탈퇴(User CASCADE)로 지워질 때도 추적이
        # 정리되어야 한다(watchlist._stop_tracking_after_unwatch).
        from . import watchlist  # noqa: F401
