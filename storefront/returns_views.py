"""
ویوهای مرجوعی.
"""
from django.contrib.auth.mixins import LoginRequiredMixin
from django.shortcuts import get_object_or_404
from django.views.generic import DetailView, ListView, CreateView
from django.urls import reverse_lazy
from django.contrib import messages

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
        form.instance.user = self.request.user
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