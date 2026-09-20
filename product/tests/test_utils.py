# tests for product/utils.py
from django.test import TestCase
from django.contrib.auth.models import User, Group
from django.db import transaction
from decimal import Decimal
from datetime import datetime, time, timedelta
from unittest.mock import patch, MagicMock
from django.utils import timezone
import jdatetime

from product.utils import (
    is_working_day,
    consume_material_for_task,
    consume_material_for_paint_task,
    get_painting_material_requirements_for_item_colorpart,
    auto_create_material_issues,
    get_color_code_choices,
    invalidate_caches,
    get_color_hex_map,
    get_color_material_map,
    get_material_for_color,
    parse_size_string,
    apply_size_adjustment,
    update_barcode_size,
    _parse_default_colors,
    get_unique_color_codes_for_item,
    get_item_color_assignments,
    get_painting_process_for_color,
    parse_jalali_date,
    _safe_eval,
    _get_process_cache,
    _get_worker_cache,
    _worker_day_bounds,
    _task_matches_rule,
    PaintingScheduler,
    schedule_paint_tasks_for_items,
    schedule_paint_items_auto,
    auto_assign_paint_tasks,
    create_and_schedule_items_for_date,
    repaint_item_ids_for_date,
    assign_task_to_worker,
    log_production_event,
    _get_initial_item_cursors,
    _get_active_assignment_rules,
    _get_item_ready_time,
    _get_item_next_task,
    _run_cascade_schedule,
    _insert_and_cascade_worker_day,
    _maybe_enqueue_successor,
    get_painting_ready_items_queryset,
    get_unscheduled_ready_items,
    get_item_paint_preview,
    painting_nav_context,
)
from product.models import (
    Product, ProductCategory, ProductBOM, Part, Material,
    Order, OrderItem, Color, ProductionTask, Customer, WorkerProfile,
    PaintingProcess, PaintingStage, PaintingMaterialRequirement,
    PaintingProcessMaterial, ProductionDefect, PackagingUnit,
    PaintingColorMaterialVariant, STATION_CHOICES, ProductionLog,
    PaintingAssignmentRule, ColorCode,
)
from inventory.models import (
    RawMaterial, RawMaterialCategory, StockMovement, MaterialIssue,
    MaterialLeftover, MaterialHandover, MaterialHandoverLine,
)
from inventory.services import (
    HandoverError, q2, _load_issues, build_plans, plan_to_dict,
    preview_handover, execute_handover, _create_issue_movement,
)
from product.fields import PersianDateField
from product.decorators import (
    admin_or_manager_required,
    staff_or_representative_required,
    warehouse_required,
    warehouse_or_manager_required,
    is_warehouse_user,
)
from product.signals import generate_qr_code, generate_packaging_qr_codes
from product.templatetags.barcode_tags import load_barcode, barcode_css
from product.templatetags.product_filters import (
    format_colors, split, get_item, zip_lists, intcomma, multiply, persian_date, task_color_code
)
from product.templatetags.selvi_tags import status_badge, status_color_class
from product.templatetags.worker_filters import format_costs, div, mul, sub
from product.forms import (
    OrderEditForm, ColorSelectionForm, OrderItemForm, CustomerForm,
    OrderCustomerForm, PartForm, CustomerInfoForm, EditOrderItemForm,
    ProductCreateForm, PaintingProcessForm, PaintingStageForm,
    PaintingMaterialRequirementForm, PaintingColorMaterialVariantForm, WorkerProfileForm,
)
from product.serializers import OrderSerializer
from inventory.forms import (
    SupplierForm, RawMaterialCategoryForm, RawMaterialForm,
    StockMovementForm, PurchaseOrderForm, PurchaseOrderItemForm,
)
