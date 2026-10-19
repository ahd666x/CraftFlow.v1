from django.conf import settings
from django.db import models


class CncControllerProfile(models.Model):
    """Auditable controller declaration; approval alone does not create an adapter."""
    key = models.SlugField(max_length=80, unique=True)
    manufacturer = models.CharField(max_length=120)
    model = models.CharField(max_length=120)
    firmware_version = models.CharField(max_length=120, blank=True)
    adapter_key = models.CharField(max_length=100, blank=True)
    supported_g_codes = models.JSONField(default=list, blank=True)
    supported_m_codes = models.JSONField(default=list, blank=True)
    validated_source_sha256 = models.JSONField(default=list, blank=True)
    validation_evidence = models.TextField(blank=True)
    active = models.BooleanField(default=False)
    approved = models.BooleanField(default=False)
    approved_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL,
                                    related_name="approved_cnc_controller_profiles")
    approved_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ("manufacturer", "model", "key")
        verbose_name = "پروفایل کنترلر CNC"
        verbose_name_plural = "پروفایل‌های کنترلر CNC"

    @property
    def governance_ready(self):
        return bool(
            self.active and self.approved and self.approved_by_id and self.approved_at
            and self.validation_evidence.strip()
            and isinstance(self.validated_source_sha256, list) and self.validated_source_sha256
        )

    @property
    def postprocessor_config(self):
        return {
            "approved": self.governance_ready,
            "adapter_key": self.adapter_key,
            "validated_source_sha256": self.validated_source_sha256,
            "supported_g_codes": self.supported_g_codes,
            "supported_m_codes": self.supported_m_codes,
            "supported_compact_tokens": [],
        }

    def __str__(self):
        return f"{self.manufacturer} {self.model} ({self.firmware_version or 'firmware unknown'})"


class CncFile(models.Model):
    KIND_BASE = "base"
    KIND_DERIVED = "derived"
    KIND_CHOICES = (
        (KIND_BASE, "فایل پایه"),
        (KIND_DERIVED, "فایل مشتق"),
    )

    barcode_key = models.CharField(max_length=200, db_index=True, verbose_name="کلید بارکد")
    path = models.CharField(max_length=500, verbose_name="مسیر فایل")
    sha256 = models.CharField(max_length=64, db_index=True, verbose_name="SHA-256")
    kind = models.CharField(max_length=20, choices=KIND_CHOICES, default=KIND_BASE)
    source_file = models.ForeignKey(
        "self",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="derived_files",
        verbose_name="فایل منبع",
    )
    params = models.JSONField(default=dict, blank=True, verbose_name="پارامترهای تولید")
    length_before = models.DecimalField(max_digits=10, decimal_places=3, null=True, blank=True)
    length_after = models.DecimalField(max_digits=10, decimal_places=3, null=True, blank=True)
    resizable = models.BooleanField(default=False, verbose_name="قابل تغییر اندازه")
    validation_error = models.TextField(blank=True, verbose_name="خطای اعتبارسنجی")
    controller_profile = models.ForeignKey(
        CncControllerProfile, null=True, blank=True, on_delete=models.PROTECT,
        related_name="cnc_files", verbose_name="پروفایل کنترلر تأییدشده",
    )
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="created_cnc_files",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "فایل CNC"
        verbose_name_plural = "فایل‌های CNC"
        constraints = [
            models.UniqueConstraint(
                fields=("barcode_key", "sha256", "kind"),
                name="unique_cnc_file_revision",
            ),
        ]
        ordering = ("-created_at", "-id")

    def __str__(self):
        return f"{self.barcode_key} ({self.get_kind_display()})"


class CncPartProfile(models.Model):
    part = models.OneToOneField(
        "product.Part", on_delete=models.CASCADE, related_name="cnc_profile",
        verbose_name="قطعه",
    )
    source_file = models.ForeignKey(
        CncFile, on_delete=models.PROTECT, related_name="part_profiles",
        verbose_name="فایل پایه",
    )
    params = models.JSONField(default=dict, verbose_name="الگوی تغییر اندازه")
    feature_profile = models.JSONField(default=dict, blank=True, verbose_name="پروفایل تحلیل ویژگی‌ها")
    feature_source_file = models.ForeignKey(
        CncFile, null=True, blank=True, on_delete=models.SET_NULL,
        related_name="feature_profiles", verbose_name="فایل منبع تحلیل ویژگی‌ها",
    )
    feature_profile_updated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL,
        related_name="updated_cnc_feature_profiles",
    )
    feature_profile_updated_at = models.DateTimeField(null=True, blank=True)
    approved = models.BooleanField(default=False, verbose_name="تأیید شده")
    approved_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL,
        related_name="approved_cnc_profiles",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "الگوی CNC قطعه"
        verbose_name_plural = "الگوهای CNC قطعات"

    def __str__(self):
        return f"الگوی CNC: {self.part.f3}"


class CncSheetPlan(models.Model):
    STATUS = (
        ("planned", "برنامه‌ریزی شده"),
        ("done", "اجرا شده"),
        ("cancelled", "لغو شده"),
    )
    material = models.ForeignKey("product.Material", on_delete=models.PROTECT, related_name="cnc_sheet_plans")
    source_stock = models.ForeignKey("cutting.SheetStock", on_delete=models.PROTECT, related_name="cnc_sheet_plans")
    sheet_index = models.PositiveIntegerField(default=0)
    width = models.DecimalField(max_digits=10, decimal_places=1)
    height = models.DecimalField(max_digits=10, decimal_places=1)
    planned_area = models.DecimalField(max_digits=20, decimal_places=2, default=0, editable=False)
    status = models.CharField(max_length=12, choices=STATUS, default="planned")
    RESULT_OUTCOMES = (("used", "مصرف شد"), ("untouched", "استفاده نشد"), ("scrapped", "اسقاط شد"))
    result_outcome = models.CharField(max_length=12, choices=RESULT_OUTCOMES, blank=True)
    actual_offcuts = models.JSONField(default=list, blank=True)
    result_note = models.CharField(max_length=255, blank=True)
    completed_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="completed_cnc_sheet_plans")
    completed_at = models.DateTimeField(null=True, blank=True)
    kerf = models.DecimalField(max_digits=6, decimal_places=1, default=4)
    layout = models.JSONField(default=dict)
    gcode = models.TextField()
    output_verified = models.BooleanField(default=False, editable=False)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL, related_name="cnc_sheet_plans")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ("-created_at", "material__name", "sheet_index")
        constraints = [models.UniqueConstraint(fields=("source_stock", "sheet_index", "created_by"), name="cnc_sheet_plan_stock_index_user_uniq")]
        verbose_name = "برنامه ورق CNC"
        verbose_name_plural = "برنامه‌های ورق CNC"

    def __str__(self):
        return f"CNC {self.material.name} — ورق {self.sheet_index + 1}"


class CncPlanningIssue(models.Model):
    MODES = (("cnc_sheets", "تفکیک CNC"), ("standard", "برش عادی"))
    STATUSES = (("open", "باز"), ("resolved", "رفع شد"), ("ignored", "نادیده گرفته شد"))
    task = models.ForeignKey("product.ProductionTask", null=True, blank=True, on_delete=models.SET_NULL, related_name="cnc_planning_issues")
    order = models.ForeignKey("product.Order", null=True, blank=True, on_delete=models.SET_NULL, related_name="cnc_planning_issues")
    part = models.ForeignKey("product.Part", null=True, blank=True, on_delete=models.SET_NULL, related_name="cnc_planning_issues")
    mode = models.CharField(max_length=20, choices=MODES, default="cnc_sheets")
    reason = models.TextField()
    details = models.JSONField(default=dict, blank=True)
    status = models.CharField(max_length=12, choices=STATUSES, default="open")
    resolution_note = models.CharField(max_length=255, blank=True)
    resolved_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="resolved_cnc_planning_issues")
    resolved_at = models.DateTimeField(null=True, blank=True)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL, related_name="cnc_planning_issues")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ("-created_at", "-id")
        verbose_name = "خطای برنامه‌ریزی CNC"
        verbose_name_plural = "خطاهای برنامه‌ریزی CNC"

    def __str__(self):
        return f"خطای CNC تسک {self.task_id or '-'}: {self.reason[:60]}"


class CncEngineeringProfile(models.Model):
    """Private, source-hash-bound persistent rules for the reverse-engineering workbench."""
    owner = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE,
                              related_name="cnc_engineering_profiles")
    source_sha256 = models.CharField(max_length=64, db_index=True)
    filename = models.CharField(max_length=255, blank=True)
    profile = models.JSONField(default=dict)
    updated_at = models.DateTimeField(auto_now=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ("-updated_at",)
        constraints = [models.UniqueConstraint(fields=("owner", "source_sha256"),
                                               name="cnc_eng_profile_owner_source_uniq")]
        verbose_name = "پروفایل مهندسی CNC"
        verbose_name_plural = "پروفایل‌های مهندسی CNC"

    def __str__(self):
        return f"CNC profile {self.source_sha256[:12]} — {self.owner_id}"
