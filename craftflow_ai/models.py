"""
مدل‌های ممیزی لایهٔ AI.

در فاز ۱ فقط فراخوانی ابزارها ثبت می‌شود (نه متن کامل گفتگو)، چون ثبت محتوای
گفتگو می‌تواند اطلاعات تجاری حساس را ماندگار کند.
"""
from django.conf import settings
from django.db import models


class AIConversation(models.Model):
    """یک نشست گفتگو. مالک آن کاربر واردشده به سامانه است."""
    conversation_id = models.CharField(max_length=64, unique=True, db_index=True)
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='ai_conversations',
        verbose_name='کاربر',
    )
    created_at = models.DateTimeField(auto_now_add=True, verbose_name='زمان ایجاد')
    last_activity_at = models.DateTimeField(auto_now=True, verbose_name='آخرین فعالیت')
    provider = models.CharField(max_length=32, blank=True, verbose_name='ارائه‌دهنده')
    model = models.CharField(max_length=64, blank=True, verbose_name='مدل')

    class Meta:
        verbose_name = 'گفتگوی دستیار هوشمند'
        verbose_name_plural = 'گفتگوهای دستیار هوشمند'
        ordering = ['-last_activity_at']

    def __str__(self):
        return self.conversation_id

    def touch(self, provider=None, model=None):
        self.provider = provider or self.provider
        self.model = model or self.model
        self.save(update_fields=['last_activity_at', 'provider', 'model'])


class AIConversationMessage(models.Model):
    """پیام‌های گفتگو برای حفظ زمینهٔ چند-turn."""
    ROLE_CHOICES = [
        ('user', 'کاربر'),
        ('assistant', 'دستیار'),
    ]

    conversation = models.ForeignKey(
        AIConversation, on_delete=models.CASCADE, related_name='messages',
        verbose_name='گفتگو',
    )
    role = models.CharField(max_length=16, choices=ROLE_CHOICES, verbose_name='نقش')
    content = models.TextField(verbose_name='متن')
    created_at = models.DateTimeField(auto_now_add=True, verbose_name='زمان')

    class Meta:
        verbose_name = 'پیام دستیار هوشمند'
        verbose_name_plural = 'پیام‌های دستیار هوشمند'
        ordering = ['created_at']

    def __str__(self):
        return f'{self.get_role_display()}: {self.content[:40]}'


class AIAuditLog(models.Model):
    """رد ممیزی فراخوانی ابزار توسط AI."""

    STATUS_CHOICES = [
        ('completed', 'موفق'),
        ('failed', 'ناموفق'),
        ('denied', 'رد شد (دسترسی)'),
    ]

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name='ai_audit_logs',
        verbose_name='کاربر',
    )
    session_id = models.CharField(
        max_length=64, blank=True, db_index=True, verbose_name='شناسهٔ گفتگو',
    )
    tool_name = models.CharField(max_length=64, db_index=True, verbose_name='ابزار')
    arguments = models.JSONField(default=dict, blank=True, verbose_name='آرگومان‌ها')
    result = models.JSONField(default=dict, blank=True, verbose_name='نتیجه')
    status = models.CharField(
        max_length=16, choices=STATUS_CHOICES, default='completed', verbose_name='وضعیت',
    )
    error_code = models.CharField(max_length=48, blank=True, verbose_name='کد خطا')
    duration_ms = models.PositiveIntegerField(default=0, verbose_name='مدت (میلی‌ثانیه)')
    created_at = models.DateTimeField(auto_now_add=True, db_index=True, verbose_name='زمان')

    class Meta:
        verbose_name = 'رد ممیزی هوش مصنوعی'
        verbose_name_plural = 'ردهای ممیزی هوش مصنوعی'
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['tool_name', 'created_at']),
            models.Index(fields=['user', 'created_at']),
        ]

    def __str__(self):
        return f'{self.tool_name} ({self.status}) - {self.created_at:%Y-%m-%d %H:%M}'

    @property
    def succeeded(self):
        return self.status == 'completed'