from django.http import JsonResponse
from django.shortcuts import render
from django.urls import reverse
from django.contrib.auth.decorators import login_required
from django.views.decorators.http import require_GET, require_POST

from product.decorators import admin_or_manager_required
from . import core


@login_required
@admin_or_manager_required
@require_GET
def index(request):
    return render(request, "cnc_resizer/index.html", {"api_url": reverse("cnc_resizer:process")})


@login_required
@admin_or_manager_required
@require_POST
def process(request):
    """JSON in  : {filename, content (the .cnc text), params}
       JSON out : see core.process().  CSRF is enforced as usual (the page sends the token)."""
    status, data = core.handle_request(request.body)
    return JsonResponse(data, status=status, json_dumps_params={"ensure_ascii": False})
