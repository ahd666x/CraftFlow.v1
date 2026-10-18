"""
ویوهای پرداخت.

نکته‌ی کلیدی: در این پروژه `Order` مالکیت تولید است و `generate_tasks()`
روی آن فراخوانی می‌شود. بنابراین پرداخت باید *بعد* از ایجاد تسک‌ها
اندام بگیرد — وگرنه سفارش در حالت `draft` می‌ماند و هیچ کارگاهی نمی‌داند
چه چیزی باید بسازد.
"""
from django.shortcuts import get_object_or_404, redirect, render
from django.contrib.auth.decorators import login_required
from django.conf import settings
from django.utils import timezone
from django.contrib import messages

from product.models import Order
from .models import Payment, Transaction
from .gateways import PaymentGatewayFactory


def _finish_payment(request, payment, result):
    """پس از verify موفق، پرداخت، تراکنش و سفارش را تکمیل می‌کند."""
    order = payment.order

    payment.status = 'success'
    payment.transaction_id = result.get('ref_id', '')
    payment.paid_at = timezone.now()
    payment.save()

    Transaction.objects.create(
        payment=payment,
        amount=payment.amount,
        ref_id=result.get('ref_id', ''),
        card_pan=result.get('card_pan', ''),
    )

    order.status = 'paid'
    order.paid_at = payment.paid_at
    order.save(update_fields=['status', 'paid_at'])

    # Generate production tasks after successful payment
    result = order.generate_tasks()
    if not result.get('success'):
        messages.warning(
            request,
            f"پرداخت موفق، اما صدور دستور تولید با مشکل مواجه شد: {result.get('error')}",
        )

    return redirect('storefront:order_detail', order_id=order.id)


@login_required
def payment_create(request, order_id):
    order = get_object_or_404(Order, id=order_id, user=request.user)
    if order.status in ('paid', 'shipped', 'delivered'):
        messages.error(request, 'این سفارش قبلاً پرداخت شده است.')
        return redirect('storefront:order_detail', order_id=order.id)

    payment, created = Payment.objects.get_or_create(
        order=order,
        defaults={
            'amount': order.final_amount,
            'gateway': getattr(settings, 'DEFAULT_PAYMENT_GATEWAY', 'zarinpal'),
        },
    )
    if not created and payment.status == 'success':
        return redirect('storefront:order_detail', order_id=order.id)

    if request.method == 'POST':
        gateway_name = request.POST.get('gateway', payment.gateway)
        payment.gateway = gateway_name
        payment.save(update_fields=['gateway'])

        gateway = PaymentGatewayFactory.get(payment.gateway)
        result = gateway.pay(request, payment)
        if result.get('success'):
            return redirect(result['url'])

        return render(request, 'payments/payment_error.html', {'error': result.get('error')})

    # GET request - show payment method selection
    return render(request, 'payments/payment_create.html', {'order': order, 'payment': payment})


@login_required
def payment_verify(request):
    authority = request.GET.get('Authority')
    gateway_name = request.GET.get('gateway')
    payment = None

    if gateway_name == 'cash_on_delivery':
        payment_id = request.GET.get('payment_id')
        if payment_id:
            payment = get_object_or_404(Payment, id=payment_id, order__user=request.user)
        else:
            return redirect('cart:cart_detail')
    else:
        payment = get_object_or_404(
            Payment, authority=authority, order__user=request.user,
        )

    order = payment.order

    expected_amount = order.final_amount
    if payment.amount != expected_amount:
        payment.status = 'failed'
        payment.save()
        return render(request, 'payments/payment_error.html', {
            'error': f'مبلغ پرداخت ({payment.amount}) با مبلغ سفارش ({expected_amount}) مطابقت ندارد.'
        })

    # پرداخت درب منزل: بدون پرداخت واقعی، سفارش paid نمی‌شود.
    # Payment در حالت `pending` می‌ماند تا مدیر هنگام تحویل دستی تغییر دهد.
    if payment.gateway == 'cash_on_delivery':
        messages.success(
            request,
            'سفارش شما ثبت شد. مبلغ آن هنگام تحویل پرداخت خواهد شد.',
        )
        return redirect('storefront:order_detail', order_id=order.id)

    # Idempotency check - if already successfully paid, don't verify again
    if payment.status == 'success':
        return redirect('storefront:order_detail', order_id=order.id)

    gateway = PaymentGatewayFactory.get(payment.gateway)
    result = gateway.verify(request, payment)
    if result.get('success'):
        return _finish_payment(request, payment, result)

    payment.status = 'failed'
    payment.save()
    return render(request, 'payments/payment_error.html', {'error': result.get('error')})