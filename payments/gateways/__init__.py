from .zarinpal import ZarinpalGateway
from .cod import CashOnDeliveryGateway


class PaymentGatewayFactory:
    _gateways = {
        'zarinpal': ZarinpalGateway,
        'cash_on_delivery': CashOnDeliveryGateway,
    }

    @classmethod
    def register(cls, name, gateway_class):
        cls._gateways[name] = gateway_class

    @classmethod
    def get(cls, gateway_name):
        gateway_class = cls._gateways.get(gateway_name)
        if not gateway_class:
            raise ValueError(f"Gateway '{gateway_name}' is not supported.")
        return gateway_class()