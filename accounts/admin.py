"""사용자 어드민 — 관리자가 스스로를 잠그지 못하게 막는다.

## 왜 필요한가

운영 서버의 관리자 계정은 하나뿐이다. 어드민의 사용자 화면에서 그 계정을 지우면
검수·모니터링 화면에 아무도 들어갈 수 없고, 서버 셸에서 `createsuperuser`로 되살릴
때까지 운영자가 상황을 볼 수 없다. 2026-11-25에 실제로 두 번 이렇게 됐다.

사이트의 "회원 탈퇴" 쪽은 `accounts/views.py`의 `withdraw`가 따로 막는다.

## 무엇을 막나

- **지금 로그인한 자기 자신**은 지울 수 없다.
- **마지막 남은 활성 superuser**는 지울 수 없다.

둘 다 Django 어드민의 객체 단위 삭제 권한(`has_delete_permission(request, obj)`)으로
막는다. 그래서 변경 화면의 "삭제" 버튼이 사라지고, 목록에서 고른 뒤 "선택된 사용자
삭제"를 실행해도 확인 화면이 권한 부족으로 거절한다 — Django가 지울 객체마다 이
함수를 묻기 때문이다. 다만 여러 superuser를 한꺼번에 고르면 하나하나는 "마지막"이
아니어서 통과하므로, 실제로 지우는 `delete_queryset`에서 한 번 더 센다.

관리자가 둘 이상이면 **다른** 관리자를 지우는 것은 지금처럼 된다.
"""
from django.contrib import admin
from django.contrib.auth import get_user_model
from django.contrib.auth.admin import UserAdmin
from django.core.exceptions import PermissionDenied

User = get_user_model()


def _remaining_superusers(excluding_ids):
    """`excluding_ids`를 지우고 나면 남는 활성 superuser 수."""
    return (User.objects.filter(is_superuser=True, is_active=True)
            .exclude(pk__in=excluding_ids).count())


class GuardedUserAdmin(UserAdmin):

    def has_delete_permission(self, request, obj=None):
        if not super().has_delete_permission(request, obj):
            return False
        if obj is None:
            # 목록 화면의 일괄 삭제 동작 자체는 보여 준다. 객체마다 다시 묻는다.
            return True
        if obj.pk == request.user.pk:
            return False
        if obj.is_superuser and obj.is_active and _remaining_superusers([obj.pk]) == 0:
            return False
        return True

    def delete_queryset(self, request, queryset):
        ids = list(queryset.values_list('pk', flat=True))
        targets_superuser = queryset.filter(is_superuser=True, is_active=True).exists()
        if request.user.pk in ids or (targets_superuser and _remaining_superusers(ids) == 0):
            # 확인 화면의 권한 검사를 지나온 경우의 마지막 방어선이다. 메시지를 남기고
            # 돌아가면 Django가 뒤이어 "N명 삭제했습니다"를 띄우므로 403으로 끊는다.
            raise PermissionDenied(
                '자기 자신이나 마지막 남은 관리자 계정은 지울 수 없습니다.')
        super().delete_queryset(request, queryset)


admin.site.unregister(User)
admin.site.register(User, GuardedUserAdmin)
