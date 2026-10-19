"""Подписи общего каталога поставщика для Telegram, без данных магазинов и покупок."""
from dataclasses import replace
import logging
import time

from domains.supplier_hub_client import SupplierHubClient, load_supplier_hub_settings


def catalog_labels(services):
    result = {}
    for service in services:
        service_id = str(service.get('service_id') or '')
        if not service_id:
            continue
        nominals = {}
        for field in service.get('fields') or []:
            if str(field.get('name') or '').lower() != 'nominal':
                continue
            for value in field.get('value_list') or []:
                if isinstance(value, dict):
                    identity = next((value[k] for k in ('id', 'value', 'title') if value.get(k) is not None), '')
                    title = next((value[k] for k in ('title', 'value') if value.get(k) is not None), identity)
                else:
                    identity = title = value
                if identity is not None and str(identity):
                    nominals[str(identity)] = str(title or identity)
        result[service_id] = (str(service.get('title') or ''), nominals)
    return result


class SupplierCatalogLabels:
    def __init__(self, client_factory=None, clock=time.monotonic):
        self.client_factory = client_factory or self.client
        self.clock = clock
        self.labels = {}
        self.next_refresh = 0

    @staticmethod
    def client():
        settings = load_supplier_hub_settings()
        return SupplierHubClient(replace(settings, timeout_seconds=min(settings.timeout_seconds, 3)))

    def enrich(self, event_type, payload):
        # Только новые товарные события содержат точные ID сохранённой связки.
        service_id = str(payload.get('service_id') or '')
        if event_type != 'supplier_stock' or not service_id:
            return payload
        if self.clock() >= self.next_refresh:
            try:
                labels = catalog_labels(self.client_factory().services())
            except Exception:
                # Подписи не блокируют доставку; сохраняем последний успешный каталог.
                logging.getLogger(__name__).warning('Supplier catalog labels unavailable')
                self.next_refresh = self.clock() + 300
            else:
                self.labels = labels
                self.next_refresh = self.clock() + 3600
        title, nominals = self.labels.get(service_id, ('', {}))
        nominal_id = str(payload.get('nominal_id') or '')
        return {**payload, 'service_title': title, 'nominal_title': nominals.get(nominal_id, '')}
