from django.conf import settings
from django.urls import reverse


class ZarinpalGateway:
    def __init__(self):
        self.merchant_id = getattr(settings, 'ZARINPAL_MERCHANT_ID', '')
        self.sandbox = getattr(settings, 'ZARINPAL_SANDBOX', True)
        if self.sandbox:
            self.request_url = 'https://sandbox.zarinpal.com/pg/rest/WebGate/PaymentRequest.json'
            self.verify_url = 'https://sandbox.zarinpal.com/pg/rest/WebGate/PaymentVerification.json'
            self.start_pay_url = 'https://sandbox.zarinpal.com/pg/StartPay/'
        else:
            self.request_url = 'https://api.zarinpal.com/pg/v4/payment/request.json'
            self.verify_url = 'https://api.zarinpal.com/pg/v4/payment/verify.json'
            self.start_pay_url = 'https://www.zarinpal.com/pg/StartPay/'

    def _post_json(self, url, data):
        try:
            import requests
            response = requests.post(url, json=data, timeout=(5, 30))
            response.raise_for_status()
            return response.json()
        except Exception as e:
            return {'error': 'request', 'message': f'خطا در ارتباط: {str(e)}'}

    def pay(self, request, payment):
        callback_url = request.build_absolute_uri(reverse('payments:payment_verify'))
        data = {
            'MerchantID': self.merchant_id,
            'Amount': int(payment.amount),
            'Description': f'پرداخت سفارش #{payment.order.id}',
            'CallbackURL': callback_url,
        }
        result = self._post_json(self.request_url, data)
        if result.get('data', {}).get('code') == 100:
            payment.authority = result['data']['authority']
            payment.save()
            return {'success': True, 'url': f'{self.start_pay_url}{result["data"]["authority"]}'}
        error_msg = result.get('errors', {}).get('message', 'خطا در اتصال به درگاه')
        if result.get('error'):
            error_msg = result.get('message', error_msg)
        return {'success': False, 'error': error_msg}

    def verify(self, request, payment):
        data = {
            'MerchantID': self.merchant_id,
            'Amount': int(payment.amount),
            'Authority': payment.authority,
        }
        result = self._post_json(self.verify_url, data)
        if result.get('data', {}).get('code') in (100, 101):
            return {
                'success': True,
                'ref_id': str(result['data'].get('ref_id', '')),
                'card_pan': result['data'].get('card_pan', ''),
            }
        error_msg = result.get('errors', {}).get('message', 'پرداخت ناموفق بود')
        if result.get('error'):
            error_msg = result.get('message', error_msg)
        return {'success': False, 'error': error_msg}