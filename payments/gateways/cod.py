from django.urls import reverse


class CashOnDeliveryGateway:
    def __init__(self):
        self.name = 'cash_on_delivery'

    def pay(self, request, payment):
        callback_url = request.build_absolute_uri(reverse('payments:payment_verify'))
        return {
            'success': True,
            'url': f'{callback_url}?gateway=cash_on_delivery&payment_id={payment.id}',
        }

    def verify(self, request, payment):
        return {
            'success': True,
            'ref_id': f'COD-{payment.id}',
            'card_pan': 'پرداخت درب منزل',
        }