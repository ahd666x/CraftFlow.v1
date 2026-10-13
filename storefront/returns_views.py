"""
ویوهای مرجوعی.
"""
from django.contrib.auth.mixins import LoginRequiredMixin
from django.shortcuts import get_object_or_404
from django.views.generic import DetailView, ListView, CreateView
from django.urls import reverse_lazy
from django.contrib import messages

from product.models import OrderItem
from storefront.models import ReturnRequest


class ReturnRequestListView(LoginRequiredMixin, ListView):
    model = ReturnRequest
    template_name = 'returns/return_request_list.html'
    context_object_name = 'requests'

    def get_queryset(self):
        return ReturnRequest.objects.filter(user=self.request.user).order_by('-created_at')


class ReturnRequestCreateView(LoginRequiredMixin, CreateView):
    model = ReturnRequest
    template_name = 'returns/return_request_form.html'
    fields = ['reason']
    success_url = reverse_lazy('storefront:return_list')

    def form_valid(self, form):
        order_item = get_object_or_404(
            OrderItem, pk=self.kwargs['order_item_id'], order__user=self.request.user,
        )
        
        # Validate return rules
        if order_item.order.status != 'delivered':
            form.add_error(None, 'فقط سفارش‌های تحویل‌شده قابل مرجوعی هستند.')
            return self.form_invalid(form)
        
        # Check if item has custom dimensions (non-returnable)
        if order_item.length or order_item.width or order_item.height:
            form.add_error(None, 'محصولات با ابعاد سفارشی قابل مرجوعی نیستند.')
            return self.form_invalid(form)
        
        # Check 7-day return window
        from django.utils import timezone
        from datetime import timedelta
        if order_item.order.delivered_at:
            if timezone.now() > order_item.order.delivered_at + timedelta(days=7):
                form.add_error(None, 'مهلت ۷ روزه مرجوعی از بین رفته است.')
                return self.form_invalid(form)
        
        form.instance.user = self.request.user
        form.instance.order_item = order_item
        messages.success(self.request, 'درخواست مرجوعی ثبت شد.')
        return super().form_valid(form)


class ReturnRequestDetailView(LoginRequiredMixin, DetailView):
    model = ReturnRequest
    template_name = 'returns/return_request_detail.html'
    context_object_name = 'request_obj'

    def get_object(self):
        return get_object_or_404(
            ReturnRequest, pk=self.kwargs['pk'], user=self.request.user,
        )