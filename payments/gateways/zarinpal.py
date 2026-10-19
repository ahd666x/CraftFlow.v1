import requests
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
        # `requests` اکنون وابستگی اعلام‌شدهٔ requirements.txt است؛ import در
        # سطح ماژول یعنی نبود آن به‌جای پیام مبهم «خطا در ارتباط»، در استارتاپ
        # پروژه آشکار می‌شود. اینجا فقط خطاهای شبکه/پارس پاسخ نرم گرفته می‌شود
        # تا خروجی همیشه دیکشنری قابل پردازش باشد.
        try:
            response = requests.post(url, json=data, timeout=(5, 30))
            response.raise_for_status()
            return response.json()
        except (requests.RequestException, ValueError) as e:
            return {'error': 'request', 'message': f'خطا در ارتباط: {str(e)}'}

    def pay(self, request, payment):
        callback_url = request.build_absolute_uri(reverse('payments:payment_verify')) + f'?gateway=zarinpal'
        
        if self.sandbox:
            # Old sandbox API format
            data = {
                'MerchantID': self.merchant_id,
                'Amount': int(payment.amount),
                'Description': f'پرداخت سفارش #{payment.order.id}',
                'CallbackURL': callback_url,
            }
        else:
            # New v4 API format for production
            data = {
                'merchant_id': self.merchant_id,
                'amount': int(payment.amount),
                'description': f'پرداخت سفارش #{payment.order.id}',
                'callback_url': callback_url,
                'metadata': {'order_id': payment.order.id},
            }
        
        result = self._post_json(self.request_url, data)
        
        if self.sandbox:
            # Old sandbox API response format
            if result.get('Status') == 100:
                payment.authority = result.get('Authority', '')
                payment.save()
                return {'success': True, 'url': f'{self.start_pay_url}{payment.authority}'}
            error_msg = result.get('Status', 'خطا در اتصال به درگاه')
        else:
            # New v4 API response format
            if result.get('data', {}).get('code') == 100:
                payment.authority = result['data'].get('authority', '')
                payment.save()
                return {'success': True, 'url': f'{self.start_pay_url}{payment.authority}'}
            error_msg = result.get('errors', {}).get('message', 'خطا در اتصال به درگاه')
            if result.get('error'):
                error_msg = result.get('message', error_msg)
        
        return {'success': False, 'error': str(error_msg)}

    def verify(self, request, payment):
        # Check for idempotency - if already paid, don't verify again
        if payment.status == 'success':
            return {'success': True, 'ref_id': payment.transaction_id, 'card_pan': ''}
        
        # Check Status=OK from callback for v4 API
        if request.GET.get('Status') != 'OK':
            return {'success': False, 'error': 'پرداخت توسط کاربر لغو شد یا ناموفق بود'}
        
        if self.sandbox:
            # Old sandbox API format
            data = {
                'MerchantID': self.merchant_id,
                'Amount': int(payment.amount),
                'Authority': payment.authority,
            }
        else:
            # New v4 API format
            data = {
                'merchant_id': self.merchant_id,
                'amount': int(payment.amount),
                'authority': payment.authority,
            }
        
        result = self._post_json(self.verify_url, data)
        
        if self.sandbox:
            # Old sandbox API response format
            if result.get('Status') in (100, 101):
                return {
                    'success': True,
                    'ref_id': str(result.get('RefID', '')),
                    'card_pan': result.get('CardPan', ''),
                }
            error_msg = result.get('Status', 'پرداخت ناموفق بود')
        else:
            # New v4 API response format
            if result.get('data', {}).get('code') in (100, 101):
                return {
                    'success': True,
                    'ref_id': str(result['data'].get('ref_id', '')),
                    'card_pan': result['data'].get('card_pan', ''),
                }
            error_msg = result.get('errors', {}).get('message', 'پرداخت ناموفق بود')
            if result.get('error'):
                error_msg = result.get('message', error_msg)
        
        return {'success': False, 'error': str(error_msg)}