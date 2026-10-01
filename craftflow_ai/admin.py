from django.contrib import admin

from craftflow_ai.models import AIAuditLog, AIConversation, AIConversationMessage


@admin.register(AIAuditLog)
class AIAuditLogAdmin(admin.ModelAdmin):
    list_display = ('created_at', 'tool_name', 'status', 'user', 'duration_ms', 'error_code')
    list_filter = ('tool_name', 'status', 'created_at')
    search_fields = ('tool_name', 'session_id', 'user__username')
    readonly_fields = ('created_at', 'arguments', 'result', 'tool_name',
                       'status', 'duration_ms', 'error_code', 'session_id', 'user')
    date_hierarchy = 'created_at'

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False


@admin.register(AIConversation)
class AIConversationAdmin(admin.ModelAdmin):
    list_display = ('conversation_id', 'user', 'provider', 'model', 'created_at',
                    'last_activity_at')
    list_filter = ('provider', 'created_at')
    search_fields = ('conversation_id', 'user__username')
    readonly_fields = ('conversation_id', 'user', 'created_at', 'last_activity_at',
                       'provider', 'model')

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False


@admin.register(AIConversationMessage)
class AIConversationMessageAdmin(admin.ModelAdmin):
    list_display = ('created_at', 'conversation', 'role')
    list_filter = ('role', 'created_at')
    search_fields = ('conversation__conversation_id', 'content')
    readonly_fields = ('conversation', 'role', 'content', 'created_at')

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False