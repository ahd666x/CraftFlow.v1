from django.http import JsonResponse
from django.shortcuts import render
from django.urls import reverse
from django.contrib.auth.decorators import login_required
from django.views.decorators.http import require_GET, require_POST
from django.http import Http404

from product.decorators import admin_or_manager_required
from . import core
from .models import CncFile


@login_required
@admin_or_manager_required
@require_GET
def index(request):
    return render(request, "cnc_resizer/index.html", {
        "api_url": reverse("cnc_resizer:process"),
        "archive_api_url": reverse("cnc_resizer:archive_files"),
    })


@login_required
@admin_or_manager_required
@require_POST
def process(request):
    """JSON in  : {filename, content (the .cnc text), params}
       JSON out : see core.process().  CSRF is enforced as usual (the page sends the token)."""
    status, data = core.handle_request(request.body)
    return JsonResponse(data, status=status, json_dumps_params={"ensure_ascii": False})


@login_required
@admin_or_manager_required
@require_GET
def archive_files(request):
    record_id = request.GET.get("id")
    if record_id:
        record = CncFile.objects.filter(
            pk=record_id, kind=CncFile.KIND_BASE, resizable=True
        ).first()
        if not record or not record.path:
            raise Http404("فایل CNC آرشیو پیدا نشد")
        try:
            with open(record.path, "r", encoding="utf-8-sig") as stream:
                content = stream.read(core.MAX_BYTES + 1)
        except OSError as exc:
            raise Http404(f"خواندن فایل CNC ممکن نیست: {exc}")
        return JsonResponse({
            "id": record.id,
            "name": record.barcode_key,
            "content": content,
        }, json_dumps_params={"ensure_ascii": False})

    records = CncFile.objects.filter(
        kind=CncFile.KIND_BASE, resizable=True
    ).values("id", "barcode_key", "length_before", "created_at")
    return JsonResponse({
        "files": [
            {
                "id": row["id"],
                "name": row["barcode_key"],
                "length": row["length_before"],
            }
            for row in records
        ]
    }, json_dumps_params={"ensure_ascii": False})
