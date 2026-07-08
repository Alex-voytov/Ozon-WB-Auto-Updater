# -*- coding: utf-8 -*-
"""
ozon_auto_updater.py
Десктопное приложение для автоматического обновления товаров Ozon с Claude AI.
"""

import datetime
import json
import logging
import os
import queue
import re
import sys
import threading
import time
import tkinter as tk
from dataclasses import dataclass, field
from tkinter import messagebox, scrolledtext, ttk
from typing import Any, Dict, List, Optional

import anthropic
import requests
import schedule

CONFIG_PATH = "config.json"
OZON_API_BASE = "https://api-seller.ozon.ru"
OZON_PERF_BASE = "https://api-performance.ozon.ru"
WB_API_BASE = "https://content-api.wildberries.ru"

STOP_WORDS = {
    'и', 'в', 'на', 'с', 'для', 'по', 'от', 'из', 'до', 'при', 'без', 'через', 'над', 'под',
    'о', 'об', 'к', 'у', 'за', 'про', 'не', 'ни', 'что', 'это', 'как', 'так', 'все', 'всё',
    'этот', 'эта', 'это', 'эти', 'те', 'тот', 'та', 'то', 'или', 'а', 'но', 'да', 'же',
    'бы', 'ли', 'уж', 'вон', 'вот', 'только', 'еще', 'уже', 'даже', 'ведь', 'всего',
    'очень', 'слишком', 'также', 'зато', 'потому', 'поэтому', 'оттого', 'зачем', 'почему'
}


# ============================================================================
# ИСКЛЮЧЕНИЯ
# ============================================================================

class OzonApiError(RuntimeError):
    pass

class WbApiError(RuntimeError):
    pass

class AIGenerationError(RuntimeError):
    pass


# ============================================================================
# МОДЕЛИ ДАННЫХ
# ============================================================================

@dataclass
class OzonCredentials:
    client_id: str
    api_key: str


@dataclass
class ProductSnapshot:
    offer_id: str
    product_id: int
    sku: Optional[int]
    name: str
    description_category_id: int
    type_id: int
    current_attributes: List[Dict[str, Any]] = field(default_factory=list)
    images: List[str] = field(default_factory=list)
    barcode: Optional[str] = None
    weight: Optional[int] = None
    width: Optional[int] = None
    height: Optional[int] = None
    depth: Optional[int] = None
    dimension_unit: str = "mm"
    weight_unit: str = "g"
    vat: str = "0"
    currency_code: str = "RUB"
    price: Optional[str] = None
    old_price: Optional[str] = None


# ============================================================================
# КЛИЕНТ OZON
# ============================================================================

class OzonClient:
    def __init__(self, creds: OzonCredentials, timeout: int = 30, max_retries: int = 3):
        self.creds = creds
        self.timeout = timeout
        self.max_retries = max_retries
        self.session = requests.Session()
        self.session.headers.update({
            "Client-Id": creds.client_id,
            "Api-Key": creds.api_key,
            "Content-Type": "application/json",
        })
        self._category_attributes_cache: Dict[str, List[Dict[str, Any]]] = {}

    def _post(self, path: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        url = f"{OZON_API_BASE}{path}"
        last_exc: Optional[Exception] = None

        for attempt in range(1, self.max_retries + 1):
            try:
                resp = self.session.post(url, json=payload, timeout=self.timeout)
            except requests.RequestException as exc:
                last_exc = exc
                time.sleep(2 ** attempt)
                continue

            if resp.status_code == 429:
                time.sleep(2 ** attempt)
                continue

            if resp.status_code >= 500:
                time.sleep(2 ** attempt)
                continue

            if resp.status_code >= 400:
                raise OzonApiError(f"{path} -> {resp.status_code}: {resp.text[:500]}")

            try:
                return resp.json()
            except requests.exceptions.JSONDecodeError:
                raise OzonApiError(f"{path} -> невалидный JSON: {resp.text[:200]}")

        raise OzonApiError(f"Не удалось выполнить {path} после {self.max_retries} попыток: {last_exc}")

    def find_product_by_offer_id(self, offer_id: str) -> int:
        payload = {"offer_id": [offer_id], "limit": 1}
        data = self._post("/v3/product/info/list", payload)
        items = data.get("result", {}).get("items", []) or data.get("items", [])
        if not items:
            raise OzonApiError(f"Товар с offer_id={offer_id!r} не найден")
        item = items[0]
        product_id = item.get("product_id") or item.get("id") or item.get("sku")
        if product_id is None:
            raise OzonApiError(f"В ответе нет ID товара: {item}")
        return int(product_id)

    def list_all_offer_ids(self, visibility: str = "ALL", page_size: int = 100) -> List[str]:
        offer_ids: List[str] = []
        last_id = ""
        while True:
            payload = {"filter": {"visibility": visibility}, "last_id": last_id, "limit": page_size}
            data = self._post("/v3/product/list", payload)
            result = data.get("result", {})
            items = result.get("items", [])
            if not items:
                break
            offer_ids.extend(item["offer_id"] for item in items if item.get("offer_id"))
            last_id = result.get("last_id", "")
            if not last_id or len(items) < page_size:
                break
        return offer_ids

    def get_product_names(self, offer_ids: List[str]) -> Dict[str, str]:
        """Возвращает {offer_id: name} через /v3/product/info/list (пачками по 1000)."""
        result: Dict[str, str] = {}
        for i in range(0, len(offer_ids), 1000):
            batch = offer_ids[i:i + 1000]
            try:
                data = self._post("/v3/product/info/list", {"offer_id": batch})
                items = data.get("result", {}).get("items", []) or data.get("items", [])
                for item in items:
                    oid = item.get("offer_id", "")
                    name = item.get("name", "") or oid
                    if oid:
                        result[oid] = name
            except Exception:
                pass
        return result

    def get_categories(self, name: str = "") -> List[Dict[str, Any]]:
        """Поиск категорий Ozon по названию через /v1/description-category/tree."""
        try:
            payload: Dict[str, Any] = {"language": "RU"}
            if name:
                payload["name"] = name
            data = self._post("/v1/description-category/tree", payload)
            result = data.get("result", []) or []
            if result:
                logging.info(f"[get_categories] пример узла: {str(result[0])[:300]}")
            return result
        except Exception as e:
            logging.warning(f"[get_categories] ошибка: {e}")
            return []


    def get_product_info(self, offer_id: str) -> Dict[str, Any]:
        """Получает полные данные товара включая изображения."""
        try:
            data = self._post("/v3/product/info/list", {"offer_id": [offer_id]})
            items = data.get("result", {}).get("items", []) or data.get("items", [])
            return items[0] if items else {}
        except Exception:
            return {}

    def create_product(self, offer_id: str, name: str, description: str,
                       category_id: int, type_id: int, price: str,
                       images: Optional[List[str]] = None,
                       attributes: Optional[List[Dict]] = None,
                       weight: int = 100, depth: int = 100,
                       width: int = 100, height: int = 100) -> Dict[str, Any]:
        """Создаёт товар на Ozon. Возвращает финальный статус после опроса задачи."""
        import time
        item: Dict[str, Any] = {
            "offer_id": offer_id,
            "name": name[:500],
            "description": description[:4000],
            "description_category_id": category_id,
            "type_id": type_id,
            "price": str(price),
            "old_price": "0",
            "vat": "0",
            "currency_code": "RUB",
            "dimension_unit": "mm",
            "weight_unit": "g",
            "weight": max(1, weight),
            "depth": max(1, depth),
            "width": max(1, width),
            "height": max(1, height),
            "attributes": attributes or [],
        }
        if images:
            item["images"] = images[:15]
        logging.info(f"[Ozon create_product] payload: offer_id={offer_id!r}, cat={category_id}, type={type_id}, price={price}")
        resp = self._post("/v3/product/import", {"items": [item]})
        task_id = resp.get("result", {}).get("task_id")
        if not task_id:
            logging.warning(f"[Ozon create_product] no task_id in response: {resp}")
            return resp
        logging.info(f"[Ozon create_product] task_id={task_id}, polling status...")
        # Пробуем известные эндпоинты для проверки статуса задачи
        _TASK_ENDPOINTS = [
            ("/v1/product/import/task", {"task_id": task_id}),
            ("/v1/product/import/task/list", {"task_id": [task_id]}),
            ("/v1/product/list/by-task", {"task_id": task_id}),
        ]
        task_endpoint = None
        task_payload = None
        for ep, pl in _TASK_ENDPOINTS:
            try:
                test = self._post(ep, pl)
                if "result" in test or "items" in test:
                    task_endpoint = ep
                    task_payload = pl
                    break
            except OzonApiError as ex:
                if "404" in str(ex):
                    logging.warning(f"[Ozon create_product] эндпоинт {ep} недоступен (404)")
                    continue
                raise

        if task_endpoint is None:
            # Не удалось найти рабочий эндпоинт — товар отправлен, проверка недоступна
            logging.warning(f"[Ozon create_product] эндпоинт статуса недоступен, товар отправлен (task_id={task_id})")
            return {"result": {"task_id": task_id, "status": "submitted"}}

        # Опрашиваем статус до 30 секунд
        for attempt in range(15):
            time.sleep(2)
            try:
                task_resp = self._post(task_endpoint, task_payload)
                # Поддержка разных форматов ответа
                result_data = task_resp.get("result", task_resp)
                if isinstance(result_data, dict):
                    task_items = result_data.get("items", [])
                elif isinstance(result_data, list):
                    task_items = result_data
                else:
                    task_items = []
                if not task_items:
                    continue
                task_item = task_items[0]
                status = task_item.get("status", "")
                logging.info(f"[Ozon create_product] attempt={attempt+1}, status={status!r}, item={task_item}")
                if status in ("imported", "moderating"):
                    product_id = task_item.get("product_id")
                    return {"result": {"task_id": task_id, "product_id": product_id, "status": status}}
                if status == "failed":
                    errors = task_item.get("errors") or []
                    err_msgs = "; ".join(
                        e.get("message") or e.get("code") or str(e) for e in errors
                    ) if errors else "неизвестная ошибка"
                    raise OzonApiError(f"Товар не принят Ozon: {err_msgs}")
                # status == "processing" — ждём ещё
            except OzonApiError:
                raise
            except Exception as e:
                logging.warning(f"[Ozon create_product] task poll error: {e}")
        # Истёк таймаут, но товар был отправлен — не падаем, просто сообщаем
        logging.warning(f"[Ozon create_product] таймаут опроса статуса (task_id={task_id})")
        return {"result": {"task_id": task_id, "status": "processing"}}

    def get_fbo_skus_bulk(self, offer_ids: List[str]) -> Dict[str, int]:
        """Возвращает {offer_id: fbo_sku} для списка offer_id через /v3/product/info/list."""
        result: Dict[str, int] = {}
        chunk_size = 1000
        for i in range(0, len(offer_ids), chunk_size):
            chunk = offer_ids[i:i + chunk_size]
            try:
                data = self._post("/v3/product/info/list", {"offer_id": chunk})
                items = (data.get("result", {}).get("items", [])
                         or data.get("items", []))
                if i == 0 and items:
                    logging.info(f"[Ozon get_fbo_skus_bulk] первый item: {str(items[0])[:400]}")
                for item in items:
                    oid = item.get("offer_id", "")
                    sku = (item.get("fbo_sku") or item.get("fbs_sku")
                           or item.get("sku"))
                    if oid and sku:
                        result[oid] = int(sku)
            except Exception as exc:
                logging.info(f"[Ozon get_fbo_skus_bulk] err: {exc}")
        return result

    def get_prices(self, offer_ids: Optional[List[str]] = None) -> List[Dict[str, Any]]:
        """Возвращает список товаров с ценами. Ответ: {items:[...], cursor:{...}, total:N}"""
        items_all: List[Dict[str, Any]] = []
        last_id = ""
        while True:
            payload: Dict[str, Any] = {
                "filter": {"visibility": "ALL"},
                "last_id": last_id,
                "limit": 1000,
            }
            if offer_ids:
                payload["filter"]["offer_id"] = offer_ids
            data = self._post("/v5/product/info/prices", payload)
            # v5 возвращает items прямо в корне, не внутри result
            items = data.get("items") or data.get("result", {}).get("items", [])
            if not items:
                break
            items_all.extend(items)
            cursor = data.get("cursor", {})
            last_id = cursor.get("last_id", "") if isinstance(cursor, dict) else ""
            total = data.get("total", 0)
            if not last_id or len(items_all) >= total:
                break
        return items_all

    def update_prices(self, prices: List[Dict[str, Any]]) -> Dict[str, Any]:
        """prices: [{"offer_id": "x", "price": "1000", "min_price": "0", "old_price": "0"}]"""
        return self._post("/v1/product/import/prices", {"prices": prices})

    def get_product_attributes(self, product_id: int) -> Dict[str, Any]:
        payload = {"filter": {"product_id": [product_id], "visibility": "ALL"}, "limit": 1}
        data = self._post("/v4/product/info/attributes", payload)
        items = data.get("result", []) or data.get("items", [])
        if not items:
            raise OzonApiError(f"Не удалось получить атрибуты product_id={product_id}")
        return items[0]

    def build_snapshot(self, offer_id: str) -> ProductSnapshot:
        product_id = self.find_product_by_offer_id(offer_id)
        info = self.get_product_attributes(product_id)
        images = info.get("images", []) or []
        sku = info.get("sku") or info.get("fbo_sku") or info.get("fbs_sku")
        price = info.get("price")
        old_price = info.get("old_price")
        return ProductSnapshot(
            offer_id=offer_id,
            product_id=product_id,
            sku=sku,
            name=info.get("name", ""),
            description_category_id=info.get("description_category_id"),
            type_id=info.get("type_id"),
            current_attributes=info.get("attributes", []),
            images=[img if isinstance(img, str) else img.get("file_name", "") for img in images],
            barcode=(info.get("barcodes") or [None])[0] if info.get("barcodes") else None,
            weight=info.get("weight"),
            width=info.get("width"),
            height=info.get("height"),
            depth=info.get("depth"),
            dimension_unit=info.get("dimension_unit", "mm"),
            weight_unit=info.get("weight_unit", "g"),
            vat=info.get("vat", "0"),
            currency_code=info.get("currency_code", "RUB"),
            price=str(price) if price else None,
            old_price=str(old_price) if old_price else None,
        )

    def get_category_attributes(self, description_category_id: int, type_id: int) -> List[Dict[str, Any]]:
        cache_key = f"{description_category_id}_{type_id}"
        if cache_key in self._category_attributes_cache:
            return self._category_attributes_cache[cache_key]
        all_attrs: List[Dict[str, Any]] = []
        last_id = 0
        for _ in range(20):
            payload = {
                "description_category_id": description_category_id,
                "type_id": type_id,
                "language": "RU",
                "last_id": last_id,
                "limit": 200,
            }
            try:
                data = self._post("/v1/description-category/attribute", payload)
            except OzonApiError as e:
                logging.warning(f"[get_category_attributes] cat={description_category_id} type={type_id}: {e}")
                break
            batch = data.get("result", []) or []
            all_attrs.extend(batch)
            if len(batch) < 200:
                break
            last_id = batch[-1].get("id", 0)
        names = [a.get("name","") for a in all_attrs]
        logging.info(f"[get_category_attributes] cat={description_category_id} type={type_id}: {len(all_attrs)} атрибутов: {names}")
        self._category_attributes_cache[cache_key] = all_attrs
        return all_attrs

    def _fetch_dict_values(self, attr_id: int, dict_id: int,
                           description_category_id: int, type_id: int) -> List[Dict[str, Any]]:
        """Загружает все значения словарного атрибута."""
        cache_key = f"dict_{attr_id}_{description_category_id}_{type_id}"
        if cache_key in self._category_attributes_cache:
            return self._category_attributes_cache[cache_key]
        all_vals: List[Dict[str, Any]] = []
        last_value_id = 0
        for _ in range(20):
            try:
                resp = self._post("/v1/description-category/attribute/values", {
                    "attribute_id": attr_id,
                    "description_category_id": description_category_id,
                    "type_id": type_id,
                    "language": "RU",
                    "last_value_id": last_value_id,
                    "limit": 5000,
                })
            except Exception:
                break
            vals = resp.get("result", []) or []
            all_vals.extend(vals)
            if not resp.get("has_next") or not vals:
                break
            last_value_id = vals[-1].get("id", 0)
        self._category_attributes_cache[cache_key] = all_vals
        return all_vals

    def find_attribute_id_by_name_fragment(self, description_category_id: int, type_id: int, fragments: List[str]) -> Optional[int]:
        attrs = self.get_category_attributes(description_category_id, type_id)
        fragments_lower = [f.lower() for f in fragments]
        for attr in attrs:
            attr_name = (attr.get("name") or "").lower()
            if any(fr in attr_name for fr in fragments_lower):
                return attr.get("id")
        return None

    def find_hashtags_attribute_id(self, description_category_id: int, type_id: int) -> Optional[int]:
        attr_id = self.find_attribute_id_by_name_fragment(description_category_id, type_id, ["хештег", "hashtag"])
        if attr_id:
            return attr_id
        return self.find_attribute_id_by_name_fragment(description_category_id, type_id, ["ключев", "поисков"])

    def find_description_attribute_id(self, description_category_id: int, type_id: int) -> Optional[int]:
        return self.find_attribute_id_by_name_fragment(description_category_id, type_id, ["описа", "description", "аннотац"])

    def get_attribute_info(self, description_category_id: int, type_id: int, attr_id: int) -> Optional[Dict[str, Any]]:
        attrs = self.get_category_attributes(description_category_id, type_id)
        for attr in attrs:
            if attr.get("id") == attr_id:
                return attr
        return None

    def update_product(self, snapshot: ProductSnapshot, new_description: str,
                       hashtags: Optional[List[str]] = None, description_attr_id: Optional[int] = None,
                       hashtags_attr_id: Optional[int] = None) -> Dict[str, Any]:
        # Отправляем ТОЛЬКО те атрибуты, которые меняем — описание и хештеги.
        # Остальные атрибуты не трогаем, чтобы не вызывать проверку обязательных полей.
        attributes = []

        if description_attr_id:
            attributes = self._upsert_attribute(attributes, attr_id=description_attr_id, value=new_description)

        if hashtags_attr_id and hashtags:
            attr_info = self.get_attribute_info(snapshot.description_category_id, snapshot.type_id, hashtags_attr_id)
            is_dictionary = attr_info and attr_info.get("dictionary_id", 0) > 0
            is_collection = attr_info and attr_info.get("is_collection", False)

            if is_dictionary:
                # Словарный атрибут хештегов: ищем каждый хештег в словаре
                dict_id = attr_info.get("dictionary_id", 0)
                dict_values = self._fetch_dict_values(hashtags_attr_id, dict_id,
                                                       snapshot.description_category_id,
                                                       snapshot.type_id)
                matched_vals = []
                for tag in hashtags:
                    tag_clean = tag.lstrip("#").lower()
                    for dv in dict_values:
                        dv_val = dv.get("value", "").lower()
                        if tag_clean == dv_val or tag_clean in dv_val or dv_val in tag_clean:
                            matched_vals.append({
                                "dictionary_value_id": dv.get("id", 0),
                                "value": dv.get("value", "")
                            })
                            break
                if matched_vals:
                    attributes = self._upsert_attribute_multi(
                        attributes, attr_id=hashtags_attr_id,
                        values=[v["value"] for v in matched_vals])
                    # Заменяем values с dictionary_value_id
                    for attr in attributes:
                        if attr.get("id") == hashtags_attr_id:
                            attr["values"] = matched_vals
                            break
            elif is_collection:
                # Несловарный, но множественный — каждый хештег отдельным значением
                attributes = self._upsert_attribute_multi(
                    attributes, attr_id=hashtags_attr_id, values=hashtags)
            else:
                # Одиночный текстовый — всё в одну строку через пробел
                hashtags_str = " ".join(hashtags)
                attributes = self._upsert_attribute(attributes, attr_id=hashtags_attr_id, value=hashtags_str)

        if not attributes:
            return {"result": "nothing_to_update"}

        payload = {
            "items": [{
                "offer_id": snapshot.offer_id,
                "description_category_id": snapshot.description_category_id,
                "type_id": snapshot.type_id,
                "attributes": attributes,
            }]
        }
        return self._post("/v1/product/attributes/update", payload)

    @staticmethod
    def _upsert_attribute_multi(attributes, attr_id, values):
        new_attrs = []
        replaced = False
        for attr in attributes:
            if attr.get("id") == attr_id:
                new_attrs.append({"id": attr_id, "values": [{"value": v} for v in values]})
                replaced = True
            else:
                new_attrs.append(attr)
        if not replaced:
            new_attrs.append({"id": attr_id, "values": [{"value": v} for v in values]})
        return new_attrs

    @staticmethod
    def _upsert_attribute(attributes, attr_id, value):
        new_attrs = []
        replaced = False
        for attr in attributes:
            if attr.get("id") == attr_id:
                new_attrs.append({"id": attr_id, "values": [{"value": value}]})
                replaced = True
            else:
                new_attrs.append(attr)
        if not replaced:
            new_attrs.append({"id": attr_id, "values": [{"value": value}]})
        return new_attrs

    def get_product_search_queries(self, skus, date_from, date_to, limit=30, page=1):
        payload = {"skus": [str(s) for s in skus], "date_from": date_from, "date_to": date_to, "page": page, "page_size": limit}
        data = self._post("/v1/analytics/product-queries", payload)
        return data.get("result", {}).get("items", []) or data.get("items", [])

    def get_current_description(self, snapshot: ProductSnapshot, description_attr_id: Optional[int]) -> str:
        if not description_attr_id:
            return ""
        for attr in snapshot.current_attributes:
            if attr.get("id") == description_attr_id:
                values = attr.get("values", [])
                if values:
                    return values[0].get("value", "")
        return ""

    def collect_seed_keywords(self, sku: int, days_back: int = 30, top_n: int = 20) -> List[str]:
        tz = datetime.timezone(datetime.timedelta(hours=3))
        now = datetime.datetime.now(tz)
        date_from = (now - datetime.timedelta(days=days_back)).isoformat()
        date_to = now.isoformat()
        try:
            items = self.get_product_search_queries(skus=[sku], date_from=date_from, date_to=date_to, limit=top_n * 2)
        except OzonApiError:
            return []

        def _freq(item):
            return item.get("queries_count") or item.get("shows") or item.get("count") or item.get("impressions") or 0

        items_sorted = sorted(items, key=_freq, reverse=True)
        queries = [item.get("query") or item.get("text") for item in items_sorted]
        return [q for q in queries if q][:top_n]


# ============================================================================
# КЛИЕНТ WILDBERRIES API
# ============================================================================

class WbClient:
    """Клиент для Wildberries Content API (управление карточками товаров)."""

    def __init__(self, api_key: str, timeout: int = 30):
        self.api_key = api_key
        self.timeout = timeout
        self.session = requests.Session()
        self.session.headers.update({
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        })

    def _post(self, path: str, payload: Any, max_retries: int = 5) -> Any:
        url = WB_API_BASE + path
        for attempt in range(max_retries + 1):
            resp = self.session.post(url, json=payload, timeout=self.timeout)
            if resp.status_code == 429 and attempt < max_retries:
                retry_after = resp.headers.get("Retry-After") or resp.headers.get("X-Ratelimit-Retry")
                try:
                    wait = float(retry_after) if retry_after else 0
                except ValueError:
                    wait = 0
                wait = max(wait, 2 ** attempt)
                logging.info(f"[WB {path}] 429 — ждём {wait}с (попытка {attempt + 1}/{max_retries})")
                time.sleep(wait)
                continue
            if not resp.ok:
                raise WbApiError(f"{path} -> {resp.status_code}: {resp.text[:400]}")
            return resp.json()
        raise WbApiError(f"{path} -> 429: превышен лимит запросов после {max_retries} попыток")

    def test_connection(self) -> str:
        try:
            self._post("/content/v2/get/cards/list", {
                "settings": {"cursor": {"limit": 1}, "filter": {"withPhoto": -1}}
            })
            return "ok"
        except Exception as exc:
            return str(exc)

    def get_all_cards(self) -> List[Dict[str, Any]]:
        """Постранично загружает все карточки товаров."""
        cards = []
        cursor: Dict[str, Any] = {"limit": 100}
        first_page = True
        while True:
            data = self._post("/content/v2/get/cards/list", {
                "settings": {"cursor": cursor, "filter": {"withPhoto": -1}}
            })
            if first_page:
                logging.info(f"[WB get_all_cards] raw keys: {list(data.keys())}, sample: {str(data)[:500]}")
                first_page = False
            # API может вернуть карточки напрямую в data или в data["data"]
            if "cards" in data:
                batch = data.get("cards") or []
                new_cursor = data.get("cursor") or {}
            else:
                batch = (data.get("data") or {}).get("cards") or []
                new_cursor = (data.get("data") or {}).get("cursor") or {}
            cards.extend(batch)
            if len(batch) < cursor.get("limit", 100):
                break
            nm_id = new_cursor.get("nmID")
            updated = new_cursor.get("updatedAt")
            if not nm_id:
                break
            cursor = {"limit": 100, "nmID": nm_id, "updatedAt": updated}
        return cards

    def update_card(self, card: Dict[str, Any]) -> None:
        """Обновляет карточку целиком (WB перезаписывает все поля)."""
        self._post("/content/v2/cards/update", [card])

    def update_cards(self, cards: List[Dict[str, Any]], chunk_size: int = 500) -> None:
        """Обновляет несколько карточек за минимум запросов.
        WB принимает до 3000 карточек / 10 МБ за один запрос к /content/v2/cards/update —
        это резко снижает число запросов и риск упереться в лимит 100 запросов/мин."""
        for i in range(0, len(cards), chunk_size):
            self._post("/content/v2/cards/update", cards[i:i + chunk_size])

    def get_current_description(self, card: Dict[str, Any]) -> str:
        return card.get("description") or ""

    def get_subjects(self, name: str = "") -> List[Dict[str, Any]]:
        """Поиск предметов (категорий) WB по названию с client-side фильтрацией."""
        try:
            params = {"lang": "ru"}
            if name:
                params["name"] = name
            resp = self.session.get(f"{WB_API_BASE}/content/v2/object/all",
                                    params=params, timeout=self.timeout)
            if resp.ok:
                items = resp.json().get("data", []) or []
                # Нормализуем поля: WB возвращает subjectName или name
                result = []
                for item in items:
                    subj_name = item.get("subjectName") or item.get("name") or ""
                    subj_id = item.get("id") or item.get("subjectID") or 0
                    parent = item.get("parentName") or item.get("parent") or ""
                    if subj_name and subj_id:
                        result.append({
                            "name": subj_name,
                            "id": subj_id,
                            "parentName": parent,
                        })
                # Client-side фильтрация если API не отфильтровал
                if name and result:
                    name_lower = name.lower()
                    filtered = [s for s in result
                                if name_lower in s["name"].lower()
                                or name_lower in s.get("parentName", "").lower()]
                    if filtered:
                        result = filtered
                return result
        except Exception:
            pass
        return []

    def get_card_by_nm(self, nm_id: int) -> Optional[Dict[str, Any]]:
        """Получает полные данные карточки по nmID."""
        try:
            data = self._post("/content/v2/get/cards/list", {
                "settings": {
                    "cursor": {"limit": 1, "nmID": nm_id},
                    "filter": {"withPhoto": -1}
                }
            })
            cards = (data.get("data") or {}).get("cards") or []
            for c in cards:
                if c.get("nmID") == nm_id:
                    return c
        except Exception:
            pass
        return None

    def create_card(self, subject_id: int, vendor_code: str, name: str,
                    description: str, price: int,
                    images: Optional[List[str]] = None) -> Dict[str, Any]:
        """Создаёт карточку товара на WB."""
        card: Dict[str, Any] = {
            "subjectID": subject_id,
            "variants": [{
                "vendorCode": vendor_code,
                "title": name[:60],
                "description": description[:2000],
                "brand": "",
                "dimensions": {"length": 0, "width": 0, "height": 0, "weightBrutto": 0},
                "characteristics": [],
            }]
        }
        if images:
            card["variants"][0]["photos"] = [{"url": u} for u in images[:30]]
        result = self._post("/content/v2/cards/upload", [card])
        return result


# ============================================================================
# КЛИЕНТ WB PRICES API
# ============================================================================

WB_PRICES_BASE = "https://discounts-prices-api.wildberries.ru"


class WbPricesClient:
    """Клиент для управления ценами и скидками Wildberries."""

    def __init__(self, api_key: str, timeout: int = 30):
        self.timeout = timeout
        self.session = requests.Session()
        self.session.headers.update({
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        })

    def get_goods(self, limit: int = 1000, offset: int = 0, log_fn=None,
                  max_attempts: int = 5) -> Optional[List[Dict[str, Any]]]:
        """Возвращает список товаров с ценами. None означает 429 (rate limit)."""
        all_goods: List[Dict[str, Any]] = []
        cur_offset = offset
        while True:
            got_429 = False
            for attempt in range(max_attempts):
                try:
                    resp = self.session.get(
                        f"{WB_PRICES_BASE}/api/v2/list/goods/filter",
                        params={"limit": limit, "offset": cur_offset},
                        timeout=self.timeout,
                    )
                    # WB может вернуть 429 и в HTTP статусе и в теле JSON
                    is_429 = (resp.status_code == 429 or
                              (resp.status_code == 200 and
                               resp.json().get("status") == 429))
                    if is_429:
                        got_429 = True
                        if attempt + 1 < max_attempts:
                            wait = 60 * (attempt + 1)
                            logging.info(f"[WB get_goods] 429, ждём {wait}s (попытка {attempt+1}/{max_attempts})")
                            if log_fn:
                                log_fn(f"WB: превышен лимит запросов, ждём {wait} сек (попытка {attempt+1}/{max_attempts})...")
                            time.sleep(wait)
                        continue
                    resp.raise_for_status()
                    got_429 = False
                    break
                except requests.exceptions.RequestException:
                    if attempt + 1 >= max_attempts:
                        raise
                    time.sleep(10)
            if got_429:
                logging.info("[WB get_goods] 429 — все попытки исчерпаны")
                return None  # сигнал о rate limit
            raw_json = resp.json()
            if cur_offset == 0:
                logging.info(f"[WB get_goods] full response: {str(raw_json)[:800]}")
            if raw_json.get("status") == 429:
                logging.info("[WB get_goods] 429 в JSON — возвращаем None")
                return None
            data = raw_json.get("data", {})
            batch = data.get("listGoods") or []
            all_goods.extend(batch)
            if len(batch) < limit:
                break
            cur_offset += limit
            time.sleep(0.5)
        return all_goods

    def update_prices(self, items: List[Dict[str, Any]]) -> None:
        """items: [{"nmID": 123, "price": 1000, "discount": 0}]"""
        resp = self.session.post(
            f"{WB_PRICES_BASE}/api/v2/upload/task",
            json={"data": items},
            timeout=self.timeout,
        )
        resp.raise_for_status()

    def test_connection(self) -> str:
        try:
            self.session.get(
                f"{WB_PRICES_BASE}/api/v2/list/goods/filter",
                params={"limit": 1, "offset": 0},
                timeout=10,
            ).raise_for_status()
            return "ok"
        except Exception as exc:
            return str(exc)


# ============================================================================
# КЛИЕНТ WB ADVERT API (реклама) — управление ставками
# ============================================================================

WB_ADVERT_BASE = "https://advert-api.wildberries.ru"


class WbAdvertError(RuntimeError):
    pass


class WbAdvertClient:
    """Клиент для WB Advertising API: список кампаний, статистика, изменение ставки (CPM).

    Точная схема некоторых полей ответа WB не подтверждена официальной документацией
    на момент написания (сама документация — JS-страница, недоступна для автоматической
    проверки в этой среде) — поэтому код логирует «сырые» ответы на уровне INFO в
    marketplace_manager.log при первом использовании каждого метода, чтобы расхождение
    со схемой было сразу видно и легко исправить."""

    def __init__(self, api_key: str, timeout: int = 30):
        self.timeout = timeout
        self.session = requests.Session()
        self.session.headers.update({
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        })

    def _request(self, method: str, path: str, max_retries: int = 0, **kwargs) -> Any:
        # По умолчанию без ретраев: на практике 429 этого API держится часами, а не
        # секундами — повторные попытки внутри одного вызова только добавляют лишние
        # запросы и не помогают. Явно передайте max_retries>0, если нужен обратный эффект.
        url = WB_ADVERT_BASE + path
        for attempt in range(max_retries + 1):
            resp = self.session.request(method, url, timeout=self.timeout, **kwargs)
            if resp.status_code == 429 and attempt < max_retries:
                retry_after = resp.headers.get("Retry-After")
                try:
                    wait = float(retry_after) if retry_after else 0
                except ValueError:
                    wait = 0
                wait = max(wait, 2 ** attempt)
                logging.info(f"[WB Advert {path}] 429 — ждём {wait}с (попытка {attempt + 1}/{max_retries})")
                time.sleep(wait)
                continue
            if not resp.ok:
                raise WbAdvertError(f"{path} -> {resp.status_code}: {resp.text[:500]}")
            return resp

    def get_campaigns(self, include_statuses: Optional[List[int]] = None) -> List[Dict[str, Any]]:
        """Возвращает список кампаний с полями advertId, name, type, status и, если удалось
        определить, текущей ставкой в "_cpm". Кампании, где ставку определить не удалось,
        по-прежнему возвращаются (для отображения), но с "_cpm": None — их нужно
        пропускать при автоматической регулировке.

        По умолчанию берём только активные (9) и приостановленные (11) кампании —
        менять ставку на завершённых/отклонённых бессмысленно, а их может быть очень
        много (сотни), что и увеличивает риск запроса деталей упереться в лимит WB."""
        if include_statuses is None:
            include_statuses = [9, 11]

        resp = self._request("GET", "/adv/v1/promotion/count")
        data = resp.json()
        logging.info(f"[WB Advert promotion/count] group counts: "
                    f"{[(g.get('type'), g.get('status'), g.get('count')) for g in (data.get('adverts') or [])]}")
        ids: List[int] = []
        for group in (data.get("adverts") or []):
            if group.get("status") not in include_statuses:
                continue
            for item in (group.get("advert_list") or []):
                advert_id = item.get("advertId")
                if advert_id:
                    ids.append(advert_id)
        if not ids:
            return []

        details = self._fetch_advert_details(ids)
        result: List[Dict[str, Any]] = []
        for camp in details:
            if not isinstance(camp, dict):
                continue
            advert_id = camp.get("advertId") or camp.get("id")
            cpm = self._extract_cpm(camp)
            result.append({
                "advertId": advert_id,
                "name": camp.get("name") or str(advert_id),
                "type": camp.get("type"),
                "status": camp.get("status"),
                "_cpm": cpm,
                "_raw": camp,
            })
        return result

    def _fetch_advert_details(self, ids: List[int]) -> List[Dict[str, Any]]:
        """Запрашивает детали кампаний (имя, тип, ставка) по списку advertId.
        Точная схема этого эндпоинта не подтверждена официальной документацией на
        момент написания — пробуем самый вероятный вариант (GET со списком id в
        query), затем один запасной (POST с телом-списком id). Оба варианта
        логируются на уровне INFO, чтобы при ошибке сразу было видно, что вернул WB."""
        attempts = [
            ("GET", "/adv/v1/promotion/adverts", {"params": {"ids": ",".join(map(str, ids))}}),
            ("POST", "/adv/v1/promotion/adverts", {"json": ids}),
        ]
        for method, path, kwargs in attempts:
            try:
                resp = self._request(method, path, **kwargs)
            except WbAdvertError as exc:
                logging.info(f"[WB Advert details] {method} {path} — ошибка: {exc}")
                continue
            details = resp.json()
            if not isinstance(details, list):
                details = details.get("adverts") or details.get("result") or []
            if details:
                logging.info(f"[WB Advert details] {method} {path} — OK, sample: {str(details[0])[:800]}")
                return details
            logging.info(f"[WB Advert details] {method} {path} — пустой ответ")
        logging.info("[WB Advert details] не удалось получить детали кампаний ни одним из вариантов")
        return []

    @staticmethod
    def _extract_cpm(camp: Dict[str, Any]) -> Optional[int]:
        """Ищет текущую ставку (CPM) в структуре кампании — расположение поля отличается
        по типу кампании (авто/аукцион/старые ручные), поэтому пробуем несколько вариантов."""
        if isinstance(camp.get("cpm"), (int, float)):
            return int(camp["cpm"])
        united = camp.get("unitedParams")
        if isinstance(united, list) and united:
            for u in united:
                if isinstance(u, dict) and isinstance(u.get("cpm"), (int, float)):
                    return int(u["cpm"])
        for key in ("params", "searchPluses", "seacat"):
            block = camp.get(key)
            if isinstance(block, list):
                for item in block:
                    if isinstance(item, dict) and isinstance(item.get("cpm"), (int, float)):
                        return int(item["cpm"])
            elif isinstance(block, dict) and isinstance(block.get("cpm"), (int, float)):
                return int(block["cpm"])
        return None

    def get_campaign_stats_3d(self, campaign_id: int) -> Dict[str, float]:
        """Расход, выручка и число заказов кампании за последние 3 дня (сегодня + 2)."""
        today = datetime.date.today()
        dates = [(today - datetime.timedelta(days=i)).isoformat() for i in range(3)]
        resp = self._request("POST", "/adv/v2/fullstats", json=[{"id": campaign_id, "dates": dates}])
        data = resp.json()
        logging.info(f"[WB Advert fullstats {campaign_id}] sample: {str(data)[:600]}")

        rows = data if isinstance(data, list) else (data.get("result") or [])
        spend = revenue = 0.0
        orders = 0
        for camp_stat in rows:
            if not isinstance(camp_stat, dict):
                continue
            if camp_stat.get("advertId") not in (None, campaign_id):
                continue
            for day in (camp_stat.get("days") or []):
                spend += float(day.get("sum") or 0)
                revenue += float(day.get("sum_price") or day.get("sumPrice") or 0)
                orders += int(day.get("orders") or 0)
        return {"spend": spend, "revenue": revenue, "orders": orders}

    def set_campaign_bid(self, campaign_id: int, campaign_type: Optional[int], new_cpm: int) -> None:
        """Изменяет ставку (CPM) кампании. campaign_type обязателен для аукционных
        кампаний (type=9) — передаём тот же тип, что вернул get_campaigns()."""
        payload: Dict[str, Any] = {"advertId": campaign_id, "cpm": int(new_cpm)}
        if campaign_type is not None:
            payload["type"] = campaign_type
        logging.info(f"[WB Advert set_bid {campaign_id}] payload: {payload}")
        self._request("POST", "/adv/v0/cpm", json=payload)


# ДРР (доля рекламных расходов) вне этого коридора вокруг цели считается отклонением —
# без него ставка дёргалась бы каждый цикл из-за шумовых колебаний в пределах цели
WB_ADS_DRR_DEADBAND_PP = 1.0


def run_wb_ads_cycle(advert: WbAdvertClient, campaigns_cfg: Dict[str, Dict[str, Any]],
                     step_pct: float, log_fn=None) -> None:
    """Один проход по включённым рекламным кампаниям WB: считает фактический ДРР за
    последние 3 дня и меняет ставку на step_pct в сторону цели, если отклонение больше
    WB_ADS_DRR_DEADBAND_PP. campaigns_cfg: {"<advertId>": {"target_drr": float, "enabled": bool}}."""
    def log(msg):
        if log_fn:
            log_fn(msg)

    log(f"{'=' * 50}")
    log(f"Проверка ДРР по кампаниям — {datetime.datetime.now().strftime('%H:%M:%S')}")

    try:
        campaigns = advert.get_campaigns()
    except Exception as exc:
        log(f"  ОШИБКА получения списка кампаний: {exc}")
        return

    active = [c for c in campaigns if str(c.get("advertId")) in campaigns_cfg
             and campaigns_cfg[str(c.get("advertId"))].get("enabled")]
    if not active:
        log("  Нет кампаний с включённой авторегулировкой")
        return

    for camp in active:
        camp_id = camp.get("advertId")
        name = camp.get("name") or str(camp_id)
        target_drr = campaigns_cfg[str(camp_id)].get("target_drr")
        current_cpm = camp.get("_cpm")

        if not target_drr:
            log(f"  {name}: не задана целевая ДРР — пропуск")
            continue
        if current_cpm is None:
            log(f"  {name}: не удалось определить текущую ставку из ответа WB — пропуск")
            continue

        try:
            stats = advert.get_campaign_stats_3d(camp_id)
        except Exception as exc:
            log(f"  {name}: ошибка получения статистики — {exc}")
            continue

        spend = stats["spend"]
        revenue = stats["revenue"]
        if revenue <= 0:
            log(f"  {name}: нет заказов за 3 дня (расход {spend:.0f}₽) — недостаточно данных, пропуск")
            continue

        actual_drr = spend / revenue * 100
        log(f"  {name}: ставка={current_cpm}, расход(3д)={spend:.0f}₽, выручка(3д)={revenue:.0f}₽, "
            f"факт. ДРР={actual_drr:.1f}% (цель {target_drr:.1f}%)")

        if actual_drr > target_drr + WB_ADS_DRR_DEADBAND_PP:
            new_cpm = max(1, round(current_cpm * (1 - step_pct / 100)))
            direction = "ДРР выше цели — понижаем"
        elif actual_drr < target_drr - WB_ADS_DRR_DEADBAND_PP:
            new_cpm = round(current_cpm * (1 + step_pct / 100))
            direction = "ДРР ниже цели — повышаем"
        else:
            log(f"  {name}: ДРР в пределах цели — ставку не меняем")
            continue

        if new_cpm == current_cpm:
            log(f"  {name}: расчётная ставка не изменилась ({new_cpm})")
            continue

        try:
            advert.set_campaign_bid(camp_id, camp.get("type"), new_cpm)
            log(f"  {name}: {direction}, {current_cpm} → {new_cpm}")
        except Exception as exc:
            log(f"  {name}: ОШИБКА установки ставки — {exc}")


# ============================================================================
# КЛИЕНТ MPSTATS API
# ============================================================================

MPSTATS_BASE = "https://mpstats.io/api"


class MpstatsClient:
    """Клиент для получения аналитики с mpstats.io."""

    def __init__(self, token: str, timeout: int = 30):
        self.timeout = timeout
        self.session = requests.Session()
        self.session.headers.update({
            "X-Mpstats-TOKEN": token,
            "Content-Type": "application/json",
        })

    def test_connection(self) -> str:
        try:
            resp = self.session.get(f"{MPSTATS_BASE}/wb/get/categories", timeout=10)
            if resp.ok:
                return "ok"
            return f"HTTP {resp.status_code}: {resp.text[:200]}"
        except Exception as exc:
            return str(exc)

    def get_wb_item(self, nm_id: int):
        """Получает данные о товаре WB по nmID. Возвращает dict или list."""
        end = datetime.date.today()
        start = end - datetime.timedelta(days=30)
        try:
            resp = self.session.get(
                f"{MPSTATS_BASE}/wb/get/item/{nm_id}",
                params={"d1": start.isoformat(), "d2": end.isoformat()},
                timeout=self.timeout,
            )
            if resp.ok:
                data = resp.json()
                logging.info(f"[MPStats {nm_id}] type={type(data).__name__} sample={str(data)[:400]}")
                return data
            logging.info(f"[MPStats {nm_id}] HTTP {resp.status_code}: {resp.text[:200]}")
        except Exception as exc:
            logging.info(f"[MPStats {nm_id}] err: {exc}")
        return {}

    def get_wb_item_keywords(self, nm_id: int, top_n: int = 30) -> List[str]:
        """Получает поисковые запросы для товара WB из MPStats."""
        keywords: List[str] = []
        # Пробуем endpoint keywords
        for path in [
            f"/wb/get/item/{nm_id}/keywords",
            f"/wb/get/item/{nm_id}/search_queries",
        ]:
            try:
                resp = self.session.get(f"{MPSTATS_BASE}{path}", timeout=self.timeout)
                if resp.ok:
                    data = resp.json()
                    # Разные форматы ответа
                    if isinstance(data, list):
                        for item in data:
                            q = item.get("keyword") or item.get("query") or item.get("name") or item.get("text")
                            if q and isinstance(q, str):
                                keywords.append(q.strip().lower())
                    elif isinstance(data, dict):
                        for item in (data.get("data") or data.get("keywords") or data.get("items") or []):
                            q = item.get("keyword") or item.get("query") or item.get("name") or item.get("text")
                            if q and isinstance(q, str):
                                keywords.append(q.strip().lower())
                    if keywords:
                        logging.info(f"[MPStats keywords {nm_id}] {path}: {len(keywords)} ключевых слов")
                        break
            except Exception as exc:
                logging.debug(f"[MPStats keywords {nm_id}] {path}: {exc}")
        # Дедупликация
        seen: set = set()
        result = []
        for kw in keywords:
            if kw not in seen:
                seen.add(kw)
                result.append(kw)
        return result[:top_n]

    def get_wb_items_batch(self, nm_ids: List[int]) -> Dict[int, Dict[str, Any]]:
        """Получает данные о нескольких товарах WB."""
        result: Dict[int, Dict[str, Any]] = {}
        for nm_id in nm_ids:
            data = self.get_wb_item(nm_id)
            if data:
                result[nm_id] = data
            time.sleep(0.3)
        return result

    def get_ozon_item(self, sku: int):
        """Получает данные о товаре Ozon по SKU из MPStats /oz/get/item/{sku}.
        Возвращает dict с полями item.final_price, item.wallet_price и т.д."""
        end = datetime.date.today()
        start = end - datetime.timedelta(days=30)
        try:
            resp = self.session.get(
                f"{MPSTATS_BASE}/oz/get/item/{sku}",
                params={"d1": start.isoformat(), "d2": end.isoformat()},
                timeout=self.timeout,
            )
            if resp.ok:
                data = resp.json()
                logging.info(f"[MPStats Ozon {sku}] type={type(data).__name__} sample={str(data)[:300]}")
                return data
            logging.info(f"[MPStats Ozon {sku}] HTTP {resp.status_code}: {resp.text[:200]}")
        except Exception as exc:
            logging.info(f"[MPStats Ozon {sku}] err: {exc}")
        return {}


def extract_main_keyword(product_name: str) -> str:
    """Извлекает главное ключевое слово из названия товара.
    Возвращает первое значимое слово (тип товара). Если оно короткое (≤5 символов),
    добавляет следующее значимое слово (например 'бета аланин')."""
    UNITS = {"мг", "г", "кг", "мл", "л", "шт", "уп", "mg", "ml", "kg", "g", "iу", "iu",
             "me", "мe", "табл", "капс", "caps", "tab", "ct", "pcs", "oz", "lb"}

    cleaned = re.sub(r'[^\w\s]', ' ', product_name)
    words = cleaned.split()

    def is_valid(w: str) -> bool:
        wl = w.lower()
        if len(wl) < 3:
            return False
        if wl.isdigit() or re.match(r'^\d+[\.,]?\d*$', wl):
            return False
        if wl in STOP_WORDS or wl in UNITS:
            return False
        return bool(re.search(r'[а-яёa-z]', wl, re.IGNORECASE))

    first_idx = next((i for i, w in enumerate(words) if is_valid(w)), None)
    if first_idx is None:
        return product_name.split()[0].lower()

    first = words[first_idx].lower()

    # Если слово короткое — объединить со следующим значимым (напр. "бета аланин")
    if len(first) <= 5 and first_idx + 1 < len(words) and is_valid(words[first_idx + 1]):
        return first + " " + words[first_idx + 1].lower()

    return first


# ============================================================================
# ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ
# ============================================================================

def extract_keywords_from_name(product_name: str, max_words: int = 15) -> List[str]:
    """Извлекает смысловые фразы (1-3 слова) из названия товара для хештегов."""
    cleaned = re.sub(r'[^\w\s]', ' ', product_name)
    words = [w.lower() for w in cleaned.split() if len(w) > 2 and not w.isdigit()]
    meaningful = [w for w in words if w not in STOP_WORDS]

    phrases = []
    seen = set()

    # Сначала биграммы из значимых соседних слов (без стоп-слов между ними)
    all_words_lower = [w.lower() for w in cleaned.split() if len(w) > 1]
    for i in range(len(all_words_lower) - 1):
        w1, w2 = all_words_lower[i], all_words_lower[i + 1]
        if w1 not in STOP_WORDS and w2 not in STOP_WORDS and not w1.isdigit() and not w2.isdigit():
            phrase = f"{w1} {w2}"
            if phrase not in seen:
                seen.add(phrase)
                phrases.append(phrase)

    # Затем одиночные значимые слова (длиннее 3 символов)
    for w in meaningful:
        if len(w) > 3 and w not in seen:
            seen.add(w)
            phrases.append(w)

    return phrases[:max_words]


# ============================================================================
# АНАЛИЗ КОНКУРЕНТОВ — публичные API WB и Ozon
# ============================================================================

_COMPETITOR_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept-Language": "ru-RU,ru;q=0.9",
}


def fetch_wb_competitors(keyword: str, top_n: int = 3) -> List[Dict[str, str]]:
    """Возвращает описания топ-N карточек WB по ключевому слову (публичный API)."""
    try:
        # 1. Поиск — получаем nmID топ товаров
        search_resp = requests.get(
            "https://search.wb.ru/exactmatch/ru/common/v5/search",
            params={"appType": 1, "curr": "rub", "dest": -1257786,
                    "query": keyword, "resultset": "catalog", "sort": "popular", "spp": 30},
            headers=_COMPETITOR_HEADERS, timeout=10,
        )
        if not search_resp.ok:
            logging.info(f"[WB competitors search '{keyword}'] HTTP {search_resp.status_code}: {search_resp.text[:200]}")
            return []
        products = search_resp.json().get("data", {}).get("products", [])
        nm_ids = [str(p["id"]) for p in products if p.get("id")][:top_n * 2]
        if not nm_ids:
            logging.info(f"[WB competitors search '{keyword}'] 0 товаров в выдаче")
    except Exception as exc:
        logging.info(f"[WB competitors search '{keyword}'] {exc}")
        return []

    # 2. Детальные карточки — берём описание
    result: List[Dict[str, str]] = []
    for nm_id in nm_ids:
        if len(result) >= top_n:
            break
        try:
            det_resp = requests.get(
                "https://card.wb.ru/cards/v2/detail",
                params={"appType": 1, "curr": "rub", "dest": -1257786, "nm": nm_id},
                headers=_COMPETITOR_HEADERS, timeout=10,
            )
            if not det_resp.ok:
                logging.info(f"[WB competitors detail {nm_id}] HTTP {det_resp.status_code}: {det_resp.text[:150]}")
                continue
            cards = det_resp.json().get("data", {}).get("products", [])
            if not cards:
                continue
            card = cards[0]
            desc = (card.get("description") or "").strip()
            name = (card.get("name") or "").strip()
            if desc and len(desc) > 100:
                result.append({"name": name, "description": desc[:1200]})
            else:
                logging.info(f"[WB competitors detail {nm_id}] описание пустое/короткое ({len(desc)} симв.)")
            time.sleep(0.2)
        except Exception as exc:
            logging.info(f"[WB competitors detail {nm_id}] {exc}")
    logging.info(f"[WB competitors '{keyword}'] итого найдено: {len(result)}")
    return result


def fetch_ozon_competitors(keyword: str, top_n: int = 3) -> List[Dict[str, str]]:
    """Возвращает описания топ-N карточек Ozon по ключевому слову (публичный API)."""
    try:
        search_resp = requests.get(
            "https://www.ozon.ru/api/entrypoint-api.bx/page/json/v2",
            params={"url": f"/search/?text={keyword}&layout_container=searchResultsV2&layout_page_index=1"},
            headers={**_COMPETITOR_HEADERS, "Accept": "application/json"},
            timeout=12,
        )
        if not search_resp.ok:
            logging.info(f"[Ozon competitors search '{keyword}'] HTTP {search_resp.status_code}: {search_resp.text[:200]}")
            return []
        data = search_resp.json()
        # Ищем блок с товарами в структуре ответа
        items = []
        for widget in (data.get("widgetStates") or {}).values():
            try:
                w = widget if isinstance(widget, dict) else __import__("json").loads(widget)
                if isinstance(w, dict) and w.get("items"):
                    for item in w["items"]:
                        item_id = (item.get("action") or {}).get("id") or item.get("id")
                        title = (item.get("title") or item.get("name") or "").strip()
                        if item_id and title:
                            items.append({"id": item_id, "title": title})
            except Exception:
                pass
        items = items[:top_n * 2]
        if not items:
            logging.info(f"[Ozon competitors search '{keyword}'] 0 товаров в widgetStates, "
                         f"ключи ответа: {list(data.keys())[:10]}")
    except Exception as exc:
        logging.info(f"[Ozon competitors search '{keyword}'] {exc}")
        return []

    # Детальные карточки
    result: List[Dict[str, str]] = []
    for item in items:
        if len(result) >= top_n:
            break
        try:
            det_resp = requests.get(
                "https://www.ozon.ru/api/entrypoint-api.bx/page/json/v2",
                params={"url": f"/product/{item['id']}/"},
                headers={**_COMPETITOR_HEADERS, "Accept": "application/json"},
                timeout=12,
            )
            if not det_resp.ok:
                logging.info(f"[Ozon competitors detail {item['id']}] HTTP {det_resp.status_code}: {det_resp.text[:150]}")
                continue
            det_data = det_resp.json()
            desc = ""
            for widget in (det_data.get("widgetStates") or {}).values():
                try:
                    w = widget if isinstance(widget, dict) else __import__("json").loads(widget)
                    if isinstance(w, dict):
                        d = (w.get("description") or w.get("text") or "")
                        if isinstance(d, str) and len(d) > 150:
                            desc = d.strip()
                            break
                except Exception:
                    pass
            if desc:
                result.append({"name": item["title"], "description": desc[:1200]})
            else:
                logging.info(f"[Ozon competitors detail {item['id']}] описание не найдено в widgetStates")
            time.sleep(0.3)
        except Exception as exc:
            logging.info(f"[Ozon competitors detail {item['id']}] {exc}")
    logging.info(f"[Ozon competitors '{keyword}'] итого найдено: {len(result)}")
    return result


def find_competitors_via_ai(api_key: str, keyword: str, marketplace_name: str,
                            top_n: int = 3, model: str = "claude-opus-4-8") -> List[Dict[str, str]]:
    """Ищет карточки товаров-конкурентов через веб-поиск Claude (server-side web_search tool).
    Используется как запасной способ, когда прямой запрос к сайту маркетплейса
    блокируется антибот-защитой (см. fetch_ozon_competitors/fetch_wb_competitors)."""
    if not api_key:
        return []
    try:
        client = anthropic.Anthropic(api_key=api_key, max_retries=0)
        prompt = (
            f"Используя веб-поиск, найди {top_n} реальные карточки товаров-конкурентов на {marketplace_name} "
            f"по запросу «{keyword}». Для каждого найденного товара составь краткое связное описание "
            f"(300-700 символов), опираясь на реальный текст со страницы товара конкурента, а не выдумывай.\n\n"
            f"Верни ТОЛЬКО JSON-массив без пояснений до или после, в формате:\n"
            f'[{{"name": "название товара", "description": "текст описания"}}, ...]'
        )
        resp = client.messages.create(
            model=model,
            max_tokens=2000,
            tools=[{"type": "web_search_20250305", "name": "web_search", "max_uses": 4}],
            messages=[{"role": "user", "content": prompt}],
        )
        text = "".join(block.text for block in resp.content if block.type == "text")
        match = re.search(r'\[.*\]', text, re.DOTALL)
        if not match:
            logging.info(f"[AI competitors '{keyword}'] JSON не найден в ответе")
            return []
        data = json.loads(match.group(0))
        result: List[Dict[str, str]] = []
        for item in data[:top_n]:
            name = str(item.get("name", "")).strip()
            desc = str(item.get("description", "")).strip()
            if name and desc:
                result.append({"name": name, "description": desc[:1200]})
        logging.info(f"[AI competitors '{keyword}'] найдено: {len(result)}")
        return result
    except Exception as exc:
        logging.info(f"[AI competitors '{keyword}'] ошибка: {exc}")
        return []


def find_keywords_via_ai(api_key: str, product_name: str, marketplace_name: str,
                         top_n: int = 15, model: str = "claude-opus-4-8") -> List[str]:
    """Ищет реальные поисковые запросы покупателей через веб-поиск Claude.
    Используется, когда нет данных аналитики (Seller API / MPStats) — например для новых
    товаров без истории показов. Не подменяет собой реальную аналитику, а лишь заполняет пробел,
    когда её ещё нет."""
    if not api_key:
        return []
    try:
        client = anthropic.Anthropic(api_key=api_key, max_retries=0)
        prompt = (
            f"Используя веб-поиск, изучи реальные карточки товара «{product_name}» и похожих на него "
            f"товаров на {marketplace_name} — их заголовки, описания и SEO-ключи. На основе этого "
            f"определи {top_n} реальных поисковых запросов и ключевых слов, по которым покупатели "
            f"ищут такие товары на маркетплейсе.\n\n"
            f"Верни ТОЛЬКО JSON-массив строк без пояснений, от самых частотных запросов к менее частотным:\n"
            f'["запрос 1", "запрос 2", ...]'
        )
        resp = client.messages.create(
            model=model,
            max_tokens=1500,
            tools=[{"type": "web_search_20250305", "name": "web_search", "max_uses": 4}],
            messages=[{"role": "user", "content": prompt}],
        )
        text = "".join(block.text for block in resp.content if block.type == "text")
        match = re.search(r'\[.*\]', text, re.DOTALL)
        if not match:
            logging.info(f"[AI keywords '{product_name}'] JSON не найден в ответе")
            return []
        data = json.loads(match.group(0))
        result = [str(kw).strip().lower() for kw in data if str(kw).strip()]
        logging.info(f"[AI keywords '{product_name}'] найдено: {len(result)}")
        return result[:top_n]
    except Exception as exc:
        logging.info(f"[AI keywords '{product_name}'] ошибка: {exc}")
        return []


def find_competitors_via_gemini(api_key: str, keyword: str, marketplace_name: str,
                                top_n: int = 3, model: str = "gemini-2.5-flash") -> List[Dict[str, str]]:
    """Ищет карточки товаров-конкурентов через веб-поиск Gemini (Google Search grounding).
    Аналог find_competitors_via_ai, но для провайдера Gemini — используется, когда в качестве
    основного генератора выбран Gemini, чтобы не зависеть от отдельного ключа/баланса Anthropic."""
    if not api_key:
        return []
    try:
        from google import genai
        from google.genai import types
        client = genai.Client(api_key=api_key)
        prompt = (
            f"Используя веб-поиск, найди {top_n} реальные карточки товаров-конкурентов на {marketplace_name} "
            f"по запросу «{keyword}». Для каждого найденного товара составь краткое связное описание "
            f"(300-700 символов), опираясь на реальный текст со страницы товара конкурента, а не выдумывай.\n\n"
            f"Верни ТОЛЬКО JSON-массив без пояснений до или после, в формате:\n"
            f'[{{"name": "название товара", "description": "текст описания"}}, ...]'
        )
        resp = client.models.generate_content(
            model=model,
            contents=prompt,
            config=types.GenerateContentConfig(tools=[types.Tool(google_search=types.GoogleSearch())]),
        )
        text = (resp.text or "").strip()
        match = re.search(r'\[.*\]', text, re.DOTALL)
        if not match:
            logging.info(f"[Gemini AI competitors '{keyword}'] JSON не найден в ответе")
            return []
        data = json.loads(match.group(0))
        result: List[Dict[str, str]] = []
        for item in data[:top_n]:
            name = str(item.get("name", "")).strip()
            desc = str(item.get("description", "")).strip()
            if name and desc:
                result.append({"name": name, "description": desc[:1200]})
        logging.info(f"[Gemini AI competitors '{keyword}'] найдено: {len(result)}")
        return result
    except Exception as exc:
        logging.info(f"[Gemini AI competitors '{keyword}'] ошибка: {exc}")
        return []


def find_keywords_via_gemini(api_key: str, product_name: str, marketplace_name: str,
                             top_n: int = 15, model: str = "gemini-2.5-flash") -> List[str]:
    """Ищет реальные поисковые запросы покупателей через веб-поиск Gemini (Google Search grounding).
    Аналог find_keywords_via_ai, но для провайдера Gemini."""
    if not api_key:
        return []
    try:
        from google import genai
        from google.genai import types
        client = genai.Client(api_key=api_key)
        prompt = (
            f"Используя веб-поиск, изучи реальные карточки товара «{product_name}» и похожих на него "
            f"товаров на {marketplace_name} — их заголовки, описания и SEO-ключи. На основе этого "
            f"определи {top_n} реальных поисковых запросов и ключевых слов, по которым покупатели "
            f"ищут такие товары на маркетплейсе.\n\n"
            f"Верни ТОЛЬКО JSON-массив строк без пояснений, от самых частотных запросов к менее частотным:\n"
            f'["запрос 1", "запрос 2", ...]'
        )
        resp = client.models.generate_content(
            model=model,
            contents=prompt,
            config=types.GenerateContentConfig(tools=[types.Tool(google_search=types.GoogleSearch())]),
        )
        text = (resp.text or "").strip()
        match = re.search(r'\[.*\]', text, re.DOTALL)
        if not match:
            logging.info(f"[Gemini AI keywords '{product_name}'] JSON не найден в ответе")
            return []
        data = json.loads(match.group(0))
        result = [str(kw).strip().lower() for kw in data if str(kw).strip()]
        logging.info(f"[Gemini AI keywords '{product_name}'] найдено: {len(result)}")
        return result[:top_n]
    except Exception as exc:
        logging.info(f"[Gemini AI keywords '{product_name}'] ошибка: {exc}")
        return []


# ============================================================================
# КЛИЕНТ OZON PERFORMANCE API
# ============================================================================

class OzonPerformanceClient:
    """
    Клиент для Ozon Performance API.
    Получает ключевые слова из рекламных кампаний товара.
    Документация: https://performance.ozon.ru/docs
    """

    def __init__(self, client_id: str, client_secret: str, timeout: int = 30):
        self.client_id = client_id
        self.client_secret = client_secret
        self.timeout = timeout
        self._token: Optional[str] = None
        self._token_expires: float = 0.0
        self.session = requests.Session()

    def _ensure_token(self):
        if self._token and time.time() < self._token_expires - 60:
            return
        resp = self.session.post(
            f"{OZON_PERF_BASE}/api/client/token",
            data={
                "client_id": self.client_id,
                "client_secret": self.client_secret,
                "grant_type": "client_credentials",
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            timeout=self.timeout,
        )
        resp.raise_for_status()
        data = resp.json()
        self._token = data["access_token"]
        self._token_expires = time.time() + data.get("expires_in", 3600)

    def _get(self, path: str, params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        self._ensure_token()
        resp = self.session.get(
            f"{OZON_PERF_BASE}{path}",
            headers={"Authorization": f"Bearer {self._token}"},
            params=params or {},
            timeout=self.timeout,
        )
        resp.raise_for_status()
        return resp.json()

    def _post(self, path: str, body: Dict[str, Any]) -> Dict[str, Any]:
        self._ensure_token()
        resp = self.session.post(
            f"{OZON_PERF_BASE}{path}",
            headers={"Authorization": f"Bearer {self._token}"},
            json=body,
            timeout=self.timeout,
        )
        resp.raise_for_status()
        return resp.json()

    def _put(self, path: str, body: Dict[str, Any]) -> Dict[str, Any]:
        self._ensure_token()
        resp = self.session.put(
            f"{OZON_PERF_BASE}{path}",
            headers={"Authorization": f"Bearer {self._token}"},
            json=body,
            timeout=self.timeout,
        )
        if not resp.ok:
            raise RuntimeError(f"{path} -> {resp.status_code}: {resp.text[:500]}")
        return resp.json() if resp.text else {}

    def get_campaigns(self) -> List[Dict[str, Any]]:
        data = self._get("/api/client/campaign", {"state": "CAMPAIGN_STATE_RUNNING"})
        return data.get("list", [])

    def get_campaign_skus(self) -> List[str]:
        """Возвращает список SKU всех товаров из активных кампаний."""
        skus = set()
        try:
            for camp in self.get_campaigns():
                camp_id = str(camp.get("id", ""))
                if not camp_id:
                    continue
                try:
                    data = self._get(f"/api/client/campaign/{camp_id}/objects")
                    for obj in data.get("list", []):
                        sku = str(obj.get("id", ""))
                        if sku:
                            skus.add(sku)
                except Exception:
                    continue
        except Exception:
            pass
        return list(skus)

    def get_campaign_products(self, campaign_id: str) -> List[str]:
        """SKU всех товаров конкретной кампании (без агрегации по всем кампаниям)."""
        data = self._get(f"/api/client/campaign/{campaign_id}/objects")
        return [str(o.get("id")) for o in data.get("list", []) if o.get("id")]

    def get_competitive_bids(self, campaign_id: str, skus: List[str]) -> Dict[str, float]:
        """Ставка (в рублях) по товарам кампании — эндпоинт "Конкурентные ставки для
        товара" (/products/bids/competitive). Возвращает данные только для SKU, уже
        добавленных в эту кампанию. Ozon хранит денежные величины как целое ×1_000_000
        (как и бюджеты кампаний) — здесь переводим сразу в рубли.
        Точная семантика поля "bid" не подтверждена официальной документацией: похоже,
        что это скорее эффективная ставка товара в рамках кампании, чем независимая
        рыночная ставка конкурентов — используем как лучший доступный ориентир."""
        if not skus:
            return {}
        data = self._get(f"/api/client/campaign/{campaign_id}/products/bids/competitive",
                         params={"skus": skus})
        result: Dict[str, float] = {}
        for item in data.get("bids", []):
            sku = str(item.get("sku", ""))
            try:
                result[sku] = float(item.get("bid", 0)) / 1_000_000
            except (TypeError, ValueError):
                continue
        return result

    def set_bids(self, campaign_id: str, bids: Dict[str, float]) -> None:
        """Устанавливает ставку (CPC, в рублях) для товаров кампании.
        bids: {sku: ставка_в_рублях}."""
        payload = {
            "bids": [
                {"sku": sku, "bid": str(int(round(bid * 1_000_000)))}
                for sku, bid in bids.items()
            ]
        }
        self._put(f"/api/client/campaign/{campaign_id}/products", payload)

    def test_connection(self) -> str:
        """Проверяет подключение. Возвращает 'ok' или описание ошибки."""
        try:
            self._ensure_token()
            return "ok"
        except Exception as exc:
            return str(exc)


def _ozon_campaign_bid_editable(campaign: Dict[str, Any]) -> bool:
    """True, если для кампании можно вручную задавать ставку через PUT .../products.

    ВАЖНО: поле productAutopilotStrategy — это не флаг "автопилот включён/выключен",
    а тип стратегии, назначенный по умолчанию для данного вида кампании (TARGET_BIDS
    для обычных CPC/SKU-кампаний, NO_AUTO_STRATEGY для баннеров и для кампаний
    "Оплата за заказ" — там просто нет этого понятия). Реальный признак включённого
    автопилота — непустое поле "autopilot"; на практике оно None даже у кампаний с
    productAutopilotStrategy=TARGET_BIDS, если автопилот не включён вручную в кабинете.

    Также годятся только классические товарные CPC-кампании (advObjectType=SKU,
    PaymentType=CPC) — эндпоинт PUT .../products относится именно к ним; у баннерных
    (CPM) и "Оплата за заказ" (CPO/SEARCH_PROMO) кампаний ставка настраивается иначе,
    и метод её установки для CPO-кампаний Ozon вообще пометил устаревшим."""
    if campaign.get("PaymentType") != "CPC" or campaign.get("advObjectType") != "SKU":
        return False
    return not campaign.get("autopilot")


def run_ozon_ads_cycle(perf: OzonPerformanceClient, products_cfg: Dict[str, Dict[str, Any]],
                       log_fn=None) -> None:
    """Один проход по включённым товарам в рекламе Ozon: сравнивает конкурентную ставку
    с целевой наценкой и подстраивает свою ставку (CPC), чтобы держаться выше конкурентов
    на заданный процент. products_cfg: ключ "<campaignId>:<sku>" -> {"margin_pct", "enabled"}."""
    def log(msg):
        if log_fn:
            log_fn(msg)

    log(f"{'=' * 50}")
    log(f"Проверка ставок Ozon — {datetime.datetime.now().strftime('%H:%M:%S')}")

    active = {k: v for k, v in products_cfg.items() if v.get("enabled")}
    if not active:
        log("  Нет товаров с включённой авторегулировкой")
        return

    by_campaign: Dict[str, List[str]] = {}
    for key in active:
        camp_id, _, sku = key.partition(":")
        if camp_id and sku:
            by_campaign.setdefault(camp_id, []).append(sku)

    for camp_id, skus in by_campaign.items():
        try:
            competitive = perf.get_competitive_bids(camp_id, skus)
        except Exception as exc:
            log(f"  Кампания {camp_id}: ошибка получения конкурентных ставок — {exc}")
            continue

        new_bids: Dict[str, float] = {}
        for sku in skus:
            cfg = active[f"{camp_id}:{sku}"]
            margin_pct = cfg.get("margin_pct", 10.0)
            comp_bid = competitive.get(sku)
            if not comp_bid:
                log(f"  {camp_id}/{sku}: нет данных конкурентной ставки — пропуск")
                continue
            target_bid = round(comp_bid * (1 + margin_pct / 100), 2)
            log(f"  {camp_id}/{sku}: конкурентная={comp_bid:.2f}₽, наценка={margin_pct:.0f}%, "
                f"новая ставка={target_bid:.2f}₽")
            new_bids[sku] = target_bid

        if not new_bids:
            continue
        try:
            perf.set_bids(camp_id, new_bids)
            log(f"  Кампания {camp_id}: обновлено ставок — {len(new_bids)}")
        except Exception as exc:
            log(f"  Кампания {camp_id}: ОШИБКА установки ставок — {exc}")
        time.sleep(0.5)


# ============================================================================
# ГЕНЕРАТОР ОПИСАНИЯ — ШАБЛОННЫЙ (без API)
# ============================================================================

# Таблица замены окончаний для типичных существительных/прилагательных
# Ключ — исходное окончание, значение — (в родительном, в творительном, в предложном)
_ENDINGS: List[tuple] = [
    # прилагательные множественного числа: -ые/-ие → -ых/-ими/-ых
    ("ые", "ых", "ыми", "ых"),
    ("ие", "их", "ими", "их"),
    # прилагательные средний род: -ое/-ее → -ого/-ым/-ом
    ("ее", "его", "им", "ем"),
    ("ое", "ого", "ым", "ом"),
    # прилагательные мужской род: -ый/-ий → -ого/-ым/-ом
    ("ый", "ого", "ым", "ом"),
    ("ий", "его", "им", "ем"),
    # прилагательные женский род: -ая/-яя → -ой/-ей
    ("яя", "ей", "ей", "ей"),
    ("ая", "ой", "ой", "ой"),
    # существительные на -ость → -ости/-остью/-ости
    ("ость", "ости", "остью", "ости"),
    # существительные на -ание/-яние раньше общего -ние
    ("ание", "ания", "анием", "ании"),
    ("яние", "яния", "янием", "янии"),
    # существительные на -ние → -ния/-нием/-нии
    ("ние", "ния", "нием", "нии"),
    # существительные на -тор/-сор
    ("тор", "тора", "тором", "торе"),
    ("сор", "сора", "сором", "соре"),
    # существительные ж.р. на -ка → -ки/-кой/-ке
    ("ка", "ки", "кой", "ке"),
    # существительные на -ок (убегающая гласная): порошок → порошка
    ("ок", "ка", "ком", "ке"),
    # существительные на -ер/-ор → -ера/-ром
    ("ер", "ера", "ером", "ере"),
    # существительные на -ат/-ит → -ата/-атом
    ("ат", "ата", "атом", "ате"),
    ("ит", "ита", "итом", "ите"),
    # существительные ж.р. на -ь → -и/-ью/-и
    ("ь", "и", "ью", "и"),
    # существительные на -а → -ы/-ой/-е
    ("а", "ы", "ой", "е"),
]


def _inflect_word(word: str, case: str) -> str:
    """Склоняет одно слово по эвристике окончаний."""
    w = word.strip()
    for ending, gen, ins, loc in _ENDINGS:
        if w.lower().endswith(ending):
            suffix = {"gen": gen, "ins": ins, "loc": loc}[case]
            return w[: len(w) - len(ending)] + suffix
    return w


def _inflect(phrase: str, case: str = "gen") -> str:
    """
    Склоняет фразу: каждое слово обрабатывается отдельно.
    Предлоги и частицы не склоняются.
    """
    PREPOSITIONS = {"для", "при", "на", "в", "с", "по", "от", "из", "до", "без",
                    "за", "над", "под", "про", "через", "к", "у", "об", "о"}
    words = phrase.strip().split()
    result = []
    for w in words:
        if w.lower() in PREPOSITIONS:
            result.append(w)
        else:
            result.append(_inflect_word(w, case))
    return " ".join(result)


def _pick(lst: List[str], i: int) -> str:
    """Циклически выбирает элемент из списка по индексу."""
    return lst[i % len(lst)] if lst else ""


class TemplateDescriptionGenerator:
    # Шаблоны: {name} — полное название (только в начале), {kw0}/{kw1} — ключевые слова без склонения
    _P1 = [
        "{name} — практичное решение для тех, кто ценит надёжность и удобство в повседневной жизни.",
        "{name} создан для людей, которым важны качество исполнения и долговечность при ежедневном использовании.",
        "{name} станет отличным выбором благодаря продуманным характеристикам и высокому качеству исполнения.",
        "{name} сочетает в себе современный подход и практичность, что делает этот товар универсальным решением на каждый день.",
    ]
    _P2 = [
        "Благодаря качественному составу и надёжной конструкции товар демонстрирует стабильно высокие результаты при регулярном использовании.",
        "Покупатели отмечают эффективность и долговечность: этот товар справляется со своей задачей быстро и без нареканий.",
        "Особого внимания заслуживает проработка деталей — именно в этом данная модель выгодно отличается от аналогов.",
        "Пользователи убедились: при интенсивном использовании товар показывает себя с лучшей стороны и сохраняет свои свойства.",
    ]
    _P3 = [
        "Продукт одинаково хорошо подходит для домашнего и профессионального применения — широкий диапазон использования это подтверждает.",
        "Конструкция продумана до мелочей и рассчитана на длительную эксплуатацию в различных условиях.",
        "Если вам нужен надёжный товар, обратите особое внимание на эту модель: она создавалась с учётом реальных требований покупателей.",
        "Результат всегда предсказуем: состав и исполнение рассчитаны именно на стабильную долгосрочную работу.",
    ]
    _P4 = [
        "Качество материалов и сборки соответствует современным стандартам — товар не потребует частого обслуживания или замены.",
        "Производитель уделил особое внимание деталям, благодаря чему продукт сохраняет рабочие свойства на протяжении всего срока службы.",
        "Компактные размеры и продуманная упаковка позволяют хранить и транспортировать товар без каких-либо затруднений.",
        "Высокое качество исполнения — главная причина, по которой покупатели возвращаются за этим товаром снова.",
    ]
    _P5 = [
        "Оцените этот товар лично и убедитесь: характеристики полностью соответствуют ожиданиям.",
        "Проверенный выбор для тех, кто не готов идти на компромисс с качеством.",
        "Те, кто уже сделал этот выбор, остаются довольны — попробуйте и оцените разницу сами.",
        "Характеристики и качество исполнения говорят сами за себя — этот товар заслуживает вашего внимания.",
    ]

    def generate_description(self, product_name: str, keywords: List[str]) -> str:
        import hashlib
        seed = int(hashlib.md5(product_name.encode()).hexdigest(), 16)

        kw = keywords[:6] if keywords else []
        ctx = {"name": product_name}

        paragraphs = [
            _pick(self._P1, seed).format(**ctx),
            _pick(self._P2, seed + 1).format(**ctx),
            _pick(self._P3, seed + 2).format(**ctx),
            _pick(self._P4, seed + 3).format(**ctx),
            _pick(self._P5, seed + 4).format(**ctx),
        ]

        # Вплетаем оставшиеся ключевые слова (3–6) в конец предпоследнего абзаца
        extra = kw[2:6]
        if extra:
            extra_parts = []
            for w in extra:
                extra_parts.append(_inflect(w, "gen"))
            paragraphs[3] += (
                f" Товар также востребован при {_inflect(extra[0], 'loc')}"
                + (f", {_inflect(extra[1], 'loc')}" if len(extra) > 1 else "")
                + (f" и {_inflect(extra[-1], 'loc')}" if len(extra) > 2 else "")
                + "."
            )

        return _sanitize_description("\n\n".join(paragraphs))


# ============================================================================
# ФИЛЬТР ОПИСАНИЯ — удаляет фразы запрещённые правилами Ozon
# ============================================================================

# Паттерны предложений/фраз, запрещённых Ozon (доставка, возврат, обмен, гарантия продавца)
_FORBIDDEN_PATTERNS = [
    # Доставка
    r'[^.!?\n]*\bдоставк[аеиуёю]\b[^.!?\n]*[.!?]',
    r'[^.!?\n]*\bдоставляем\b[^.!?\n]*[.!?]',
    r'[^.!?\n]*\bбесплатная\s+доставка\b[^.!?\n]*[.!?]',
    r'[^.!?\n]*\bсрок[и]?\s+доставки\b[^.!?\n]*[.!?]',
    r'[^.!?\n]*\bстоимость\s+доставки\b[^.!?\n]*[.!?]',
    r'[^.!?\n]*\bспособ[ы]?\s+доставки\b[^.!?\n]*[.!?]',
    # Возврат и обмен
    r'[^.!?\n]*\bвозврат[а-я]*\b[^.!?\n]*[.!?]',
    r'[^.!?\n]*\bобмен[а-я]*\b[^.!?\n]*[.!?]',
    r'[^.!?\n]*\bзамен[аеиу]\b[^.!?\n]*[.!?]',
    r'[^.!?\n]*\bгарантируем\b[^.!?\n]*[.!?]',
    # Цены, акции, скидки
    r'[^.!?\n]*\bскидк[аиуе]\b[^.!?\n]*[.!?]',
    r'[^.!?\n]*\bакци[яи]\b[^.!?\n]*[.!?]',
    r'[^.!?\n]*\bтолько\s+сегодня\b[^.!?\n]*[.!?]',
    r'[^.!?\n]*\bуспейте\b[^.!?\n]*[.!?]',
    r'[^.!?\n]*\bвыгодн[а-я]+\s+цен[аеу]\b[^.!?\n]*[.!?]',
    r'[^.!?\n]*\bлучш[а-я]+\s+цен[аеу]\b[^.!?\n]*[.!?]',
    r'[^.!?\n]*\bкэшбэк\b[^.!?\n]*[.!?]',
    r'[^.!?\n]*\bcashback\b[^.!?\n]*[.!?]',
    # Контакты и призывы обращаться
    r'[^.!?\n]*\bсвяжитесь\b[^.!?\n]*[.!?]',
    r'[^.!?\n]*\bпишите\s+нам\b[^.!?\n]*[.!?]',
    r'[^.!?\n]*\bзвоните\b[^.!?\n]*[.!?]',
    # Ссылки на другие площадки
    r'[^.!?\n]*\bwildberries\b[^.!?\n]*[.!?]',
    r'[^.!?\n]*\bаliexpress\b[^.!?\n]*[.!?]',
    r'[^.!?\n]*\bамazon\b[^.!?\n]*[.!?]',
    r'[^.!?\n]*\bв\s+нашем\s+магазин[еа]\b[^.!?\n]*[.!?]',
]
_FORBIDDEN_RE = [re.compile(p, re.IGNORECASE) for p in _FORBIDDEN_PATTERNS]

# Символы, запрещённые Ozon в описаниях
_FORBIDDEN_CHARS_RE = re.compile(r'[®©™]')


def _sanitize_description(text: str) -> str:
    """Удаляет из описания контент запрещённый правилами Ozon."""
    # Убираем markdown-форматирование (жирный, курсив, заголовки)
    text = re.sub(r'\*{1,3}(.+?)\*{1,3}', r'\1', text)  # **bold** / *italic*
    text = re.sub(r'#{1,6}\s+', '', text)                # # заголовки
    text = re.sub(r'_{1,2}(.+?)_{1,2}', r'\1', text)    # __bold__ / _italic_
    # Убираем запрещённые символы
    text = _FORBIDDEN_CHARS_RE.sub('', text)
    # Убираем предложения с запрещёнными темами
    for pattern in _FORBIDDEN_RE:
        text = pattern.sub("", text)
    # Убираем образовавшиеся двойные пробелы и пустые строки
    text = re.sub(r'\n{3,}', '\n\n', text)
    text = re.sub(r'[ \t]{2,}', ' ', text)
    return text.strip()


# ============================================================================
# ФИЛЬТР ОПИСАНИЯ — Wildberries (запреты схожи с Ozon, но есть отличия)
# ============================================================================

_WB_FORBIDDEN_RE = [re.compile(p, re.IGNORECASE) for p in [
    r'[^.!?\n]*\bдоставк[аеиуёю]\b[^.!?\n]*[.!?]',
    r'[^.!?\n]*\bвозврат[а-я]*\b[^.!?\n]*[.!?]',
    r'[^.!?\n]*\bобмен[а-я]*\b[^.!?\n]*[.!?]',
    r'[^.!?\n]*\bскидк[аиуе]\b[^.!?\n]*[.!?]',
    r'[^.!?\n]*\bакци[яи]\b[^.!?\n]*[.!?]',
    r'[^.!?\n]*\bтолько\s+сегодня\b[^.!?\n]*[.!?]',
    r'[^.!?\n]*\bуспейте\b[^.!?\n]*[.!?]',
    r'[^.!?\n]*\bкэшбэк\b[^.!?\n]*[.!?]',
    r'[^.!?\n]*\bсвяжитесь\b[^.!?\n]*[.!?]',
    r'[^.!?\n]*\bзвоните\b[^.!?\n]*[.!?]',
    r'[^.!?\n]*\bozon\b[^.!?\n]*[.!?]',
    r'[^.!?\n]*\bwildberries\b[^.!?\n]*[.!?]',
    r'[^.!?\n]*\baliexpress\b[^.!?\n]*[.!?]',
    r'[^.!?\n]*\bрепликк?\b[^.!?\n]*[.!?]',
    r'[^.!?\n]*\bаналог\b[^.!?\n]*[.!?]',
]]


def _sanitize_wb_description(text: str) -> str:
    """Удаляет из описания контент, запрещённый правилами Wildberries."""
    # Убираем markdown-форматирование
    text = re.sub(r'\*{1,3}(.+?)\*{1,3}', r'\1', text)
    text = re.sub(r'#{1,6}\s+', '', text)
    text = re.sub(r'_{1,2}(.+?)_{1,2}', r'\1', text)
    text = _FORBIDDEN_CHARS_RE.sub('', text)
    for pattern in _WB_FORBIDDEN_RE:
        text = pattern.sub("", text)
    text = re.sub(r'\n{3,}', '\n\n', text)
    text = re.sub(r'[ \t]{2,}', ' ', text)
    return text.strip()


# WB: название карточки — жёсткий лимит 60 символов (документация WB API).
# Описание: реальный лимит зависит от категории (1000–5000 символов, чаще всего 2000) —
# точный лимит категории через API недоступен, поэтому используем консервативный
# безопасный потолок, подходящий для подавляющего большинства категорий.
WB_TITLE_MAX = 60
WB_DESCRIPTION_SAFE_MAX = 2000


# Спецсимволы, прямо запрещённые в названии карточки правилами WB
# (seller.wildberries.ru → «Как создать карточку товара»)
_WB_TITLE_FORBIDDEN_CHARS_RE = re.compile(r'[/*\-+@№%&$!=(){}\[\]]')

# Предлоги/союзы, которые бессмысленны, если остаются последним словом при обрезке
# (например «...с витамином С со» — «со» без «вкусом» после теряет смысл)
_WB_TITLE_DANGLING_WORDS = STOP_WORDS | {'со', 'во', 'ко', 'изо', 'обо', 'ото', 'подо', 'надо', 'передо'}


def _enforce_wb_title(title: str, fallback: str = "", brand: str = "") -> str:
    """Приводит название к требованиям WB: без markdown/эмодзи/спецсимволов, без бренда,
    не длиннее WB_TITLE_MAX. При обрезке предпочитает границу последней запятой (тогда
    остаётся законченная по смыслу фраза), иначе — границу слова с удалением повисших
    предлогов/союзов на конце.
    brand — если известен бренд товара (card["brand"]), он вырезается из названия как
    подстраховка на случай, если генератор всё же включил его вопреки промпту."""
    def _clean_and_cut(raw: str) -> str:
        cleaned = re.sub(r'[*_`#>~]', '', (raw or "").strip())
        if brand:
            cleaned = re.sub(re.escape(brand), '', cleaned, flags=re.IGNORECASE)
        cleaned = _WB_TITLE_FORBIDDEN_CHARS_RE.sub(' ', cleaned)
        cleaned = re.sub(r'\s+', ' ', cleaned).strip(' ,')
        if len(cleaned) <= WB_TITLE_MAX:
            return cleaned

        head = cleaned[:WB_TITLE_MAX]
        comma_cut = head.rfind(',')
        if comma_cut >= 15:  # не берём слишком короткий обрубок до запятой
            cleaned = cleaned[:comma_cut]
        else:
            if ' ' in head:
                head = head.rsplit(' ', 1)[0]
            cleaned = head

        words = cleaned.strip(' ,').split(' ')
        while len(words) > 1 and words[-1].lower().strip(',') in _WB_TITLE_DANGLING_WORDS:
            words.pop()
        return ' '.join(words).strip(' ,')

    result = _clean_and_cut(title)
    return result or _clean_and_cut(fallback)


def _enforce_wb_description(description: str, max_len: int = WB_DESCRIPTION_SAFE_MAX) -> str:
    """Обрезает описание по безопасному лимиту символов WB, по границе слова."""
    description = (description or "").strip()
    if len(description) > max_len:
        cut = description[:max_len]
        if ' ' in cut:
            cut = cut.rsplit(' ', 1)[0]
        description = cut.strip()
    return description


class WbDescriptionGenerator:
    """Генератор описаний для Wildberries на основе шаблонов."""

    _P1 = [
        "{name} — выбор тех, кто ценит практичность и надёжность в повседневных задачах.",
        "{name}: продуманное решение для тех, кто хочет получить стабильный результат без лишних усилий.",
        "{name} разработан с учётом реальных потребностей — состав и конструкция подобраны для долгосрочного использования.",
        "{name} — надёжный вариант с характеристиками, которые говорят сами за себя.",
    ]
    _P2 = [
        "Товар демонстрирует стабильно высокий результат даже при интенсивном использовании.",
        "Покупатели выделяют высокую эффективность как ключевое достоинство этой модели.",
        "Продуманный состав и качественные материалы обеспечивают надёжную работу при любом режиме эксплуатации.",
        "Качество исполнения подтверждается отзывами: товар справляется со своей задачей быстро и без нареканий.",
    ]
    _P3 = [
        "Подходит как для домашнего, так и для профессионального применения — широкий диапазон использования это подтверждает.",
        "Конструкция и состав рассчитаны на различные условия эксплуатации, что делает товар поистине универсальным.",
        "Сложно найти более подходящий вариант: этот продукт создавался с учётом реальных требований покупателей.",
        "Модель проявляет себя с лучшей стороны в любых условиях — конструкция продумана до мелочей.",
    ]
    _P4 = [
        "Материалы и качество сборки соответствуют современным стандартам — товар не потребует частого обслуживания.",
        "Производитель уделил особое внимание деталям, поэтому продукт сохраняет свойства на протяжении всего срока службы.",
        "Удобная упаковка и компактные размеры делают хранение и транспортировку товара простым и практичным.",
        "Высокое качество исполнения — главная причина, по которой покупатели возвращаются за этим товаром снова.",
    ]
    _P5 = [
        "Проверенный выбор для тех, кто не готов идти на компромисс с качеством.",
        "Убедитесь лично: характеристики полностью соответствуют ожиданиям.",
        "Те, кто уже сделал этот выбор, остаются довольны — попробуйте и оцените разницу.",
        "Качество исполнения и продуманность конструкции очевидны при первом использовании.",
    ]

    def generate_description(self, product_name: str, keywords: List[str]) -> str:
        import hashlib
        seed = int(hashlib.md5(product_name.encode()).hexdigest(), 16)
        kw = keywords[:6] if keywords else []
        ctx = {"name": product_name}
        paragraphs = [
            _pick(self._P1, seed).format(**ctx),
            _pick(self._P2, seed + 1).format(**ctx),
            _pick(self._P3, seed + 2).format(**ctx),
            _pick(self._P4, seed + 3).format(**ctx),
            _pick(self._P5, seed + 4).format(**ctx),
        ]
        extra = kw[2:6]
        if extra:
            paragraphs[3] += (
                f" Товар также востребован при {_inflect(extra[0], 'loc')}"
                + (f", {_inflect(extra[1], 'loc')}" if len(extra) > 1 else "")
                + (f" и {_inflect(extra[-1], 'loc')}" if len(extra) > 2 else "")
                + "."
            )
        result = "\n\n".join(paragraphs)
        result = _sanitize_wb_description(result)
        return _enforce_wb_description(result)

    def generate_wb_content(self, product_name: str, keywords: List[str],
                            competitors: Optional[List[Dict[str, str]]] = None,
                            brand: str = "") -> Dict[str, str]:
        """Возвращает {"title", "description"} для карточки WB.
        Название здесь не переписывается творчески (шаблонный генератор без AI) —
        только нормализуется под требования WB (без markdown/спецсимволов/бренда,
        не длиннее WB_TITLE_MAX)."""
        description = self.generate_description(product_name, keywords)
        title = _enforce_wb_title(product_name, fallback=product_name, brand=brand)
        return {"title": title, "description": description}


# ============================================================================
# ГЕНЕРАТОР ОПИСАНИЯ — CLAUDE API
# ============================================================================

class DescriptionGenerator:
    def __init__(self, api_key: str, model: str = "claude-opus-4-8"):
        if not api_key:
            raise ValueError("Требуется Anthropic API key")
        # max_retries=0 — не ретраить автоматически, обрабатываем ошибки сами
        self.client = anthropic.Anthropic(api_key=api_key, max_retries=0)
        self.model = model

    def generate_description(self, product_name: str, keywords: List[str],
                             competitors: Optional[List[Dict[str, str]]] = None) -> str:
        keywords_str = ", ".join(keywords[:10]) if keywords else "не указаны"
        comp_block = ""
        if competitors:
            lines = []
            for i, c in enumerate(competitors[:3], 1):
                lines.append(f"Конкурент {i} — {c.get('name','')[:80]}:\n{c.get('description','')[:900]}")
            comp_block = (
                "\n\nАНАЛИЗ КОНКУРЕНТОВ (топ по запросу):\n"
                "Изучи структуру и содержание этих описаний. Используй похожий подход — "
                "те же смысловые акценты, структуру абзацев, упомянутые свойства. "
                "Но напиши СВОЁ уникальное описание, не копируй текст:\n\n"
                + "\n\n".join(lines)
            )
        prompt = f"""Ты — профессиональный копирайтер для маркетплейса Ozon. Напиши SEO-оптимизированное описание товара.

Название товара: {product_name}
Ключевые слова для включения: {keywords_str}
{comp_block}

Требования:
- Длина: 1000–2000 символов
- Структура: 3–5 абзацев
- Вставляй ключевые слова только там, где они естественно вписываются по смыслу в предложение — не перечисляй их подряд списком и не вставляй слово ради самого факта его наличия. Если ключевое слово не удаётся органично встроить в текст, лучше перефразируй предложение вокруг него или используй его словоформу (падеж, число), но не жертвуй читаемостью и смыслом текста
- Если ключевое слово — это словосочетание из нескольких слов (например, название товара с брендом и характеристиками), НЕ вставляй его целиком как готовый оборот и НЕ используй его как подлежащее предложения. Разбей словосочетание на смысловые части и используй только те слова, которые естественно ложатся в грамматику текущего предложения, согласовав их по роду, числу и падежу с остальными словами
- Не смешивай кириллицу и латиницу внутри одного словосочетания (например, «Жир Omega 3», «омега рыбий продукт») — такие конструкции ломают грамматику русского языка и не должны попадать в текст
- Текст должен читаться как связное, осмысленное описание живым русским языком, а не как набор фраз с натянутыми ключевыми словами
- Выдели преимущества товара
- Без ссылок, контактов, соцсетей
- На русском языке, грамотно
- Перед выводом проверь готовый текст на орфографические и пунктуационные ошибки, а также на логическую согласованность (нет противоречий, повторов и бессмысленных фраз) и исправь их
- СТРОГО ЗАПРЕЩЕНО (штраф и блокировка карточки):
  * инструкции по применению: "принимайте по...", "нанесите...", дозировки, схемы приёма
  * доставка, сроки/стоимость/способ доставки
  * возврат, обмен, замена товара
  * цены, скидки, акции, кэшбэк, промокоды
  * призывы купить: "оформите заказ", "купите сейчас", "успейте"
  * контакты: телефоны, email, ссылки, соцсети
  * упоминание других магазинов и маркетплейсов (Wildberries, AliExpress и др.)
  * символы ® © ™
  * слова "реплика", "аналог", "копия", "оригинал"

Описание:"""

        # thinking=adaptive поддерживается только в claude-opus-4-x и claude-sonnet-4-x
        _THINKING_MODELS = ("claude-opus-4", "claude-sonnet-4", "claude-fable")
        use_thinking = any(self.model.startswith(m) for m in _THINKING_MODELS)

        kwargs: Dict[str, Any] = {
            "model": self.model,
            "max_tokens": 2000,
            "messages": [{"role": "user", "content": prompt}],
        }
        if use_thinking:
            kwargs["thinking"] = {"type": "adaptive"}

        try:
            response = self.client.messages.create(**kwargs)
            description = ""
            for block in response.content:
                if block.type == "text":
                    description = block.text.strip()
                    break

            if not description:
                raise AIGenerationError("Claude вернул пустое описание")

            description = _sanitize_description(description)

            if len(description) > 6000:
                description = description[:5997] + "..."

            return description

        except anthropic.APIError as exc:
            # Anthropic возвращает нехватку кредитов как 400 invalid_request_error,
            # поэтому проверяем текст ошибки ДО различения по типу/статус-коду —
            # иначе BadRequestError маскирует реальную причину (кончились кредиты)
            msg = str(exc)
            if "credit balance is too low" in msg or "insufficient_quota" in msg:
                raise AIGenerationError(f"CREDITS_EXHAUSTED:{exc}")
            if isinstance(exc, anthropic.BadRequestError):
                raise AIGenerationError(f"Неверный запрос к Claude API (400): {exc}")
            raise AIGenerationError(f"Ошибка Claude API: {exc}")
        except Exception as exc:
            raise AIGenerationError(f"Ошибка генерации: {exc}")

    def generate_wb_content(self, product_name: str, keywords: List[str],
                            competitors: Optional[List[Dict[str, str]]] = None,
                            brand: str = "") -> Dict[str, str]:
        """Генерирует название и описание карточки WB одним запросом (JSON-ответ),
        с учётом официальных требований Wildberries к полю «Наименование» и к описанию."""
        keywords_str = ", ".join(keywords[:10]) if keywords else "не указаны"
        comp_block = ""
        if competitors:
            lines = []
            for i, c in enumerate(competitors[:3], 1):
                lines.append(f"Конкурент {i} — {c.get('name','')[:80]}:\n{c.get('description','')[:900]}")
            comp_block = (
                "\n\nАНАЛИЗ КОНКУРЕНТОВ (топ по запросу):\n"
                "Изучи структуру и содержание этих описаний. Используй похожий подход — "
                "те же смысловые акценты, структуру абзацев, упомянутые свойства. "
                "Но напиши СВОЁ уникальное описание, не копируй текст:\n\n"
                + "\n\n".join(lines)
            )
        brand_line = f"\nБренд товара: «{brand}» — это слово НЕЛЬЗЯ включать в название (см. запрет ниже)." if brand else ""
        prompt = f"""Ты — профессиональный копирайтер для маркетплейса Wildberries. Составь название и SEO-описание карточки товара.

Текущее название товара: {product_name}
Ключевые слова для включения (в описание, не в название): {keywords_str}{brand_line}
{comp_block}

ТРЕБОВАНИЯ К НАЗВАНИЮ (официальные правила Wildberries, за нарушение карточку блокируют):
- Не длиннее {WB_TITLE_MAX} символов, и чем короче — тем лучше: название должно коротко и точно отвечать на вопрос "что изображено на фото в карточке", это НЕ SEO-заголовок со всеми характеристиками
- Схема: тип товара + при необходимости одна ключевая характеристика, без воды
- СТРОГО ЗАПРЕЩЕНО указывать в названии:
  * бренд или производителя — для этого отдельное поле карточки, в названии бренда быть не должно
  * синонимы и повторы слов
  * лишние подробности: состав, способ применения, характеристики, которые не нужны для идентификации товара на фото
  * слова ЗАГЛАВНЫМИ БУКВАМИ
  * текст только латиницей без кириллицы (латиница допустима только внутри устоявшихся аббревиатур/единиц)
  * спецсимволы: / \\ * - + @ № % & $ ! = ( ) {{ }} [ ]
  * телефоны, email, ссылки, мессенджеры, эмодзи
  * оценочные суждения: "лучший", "супер", "хит", "премиум", "топ"
  * указание пола, возраста или сезона

ТРЕБОВАНИЯ К ОПИСАНИЮ:
- Длина: 900–1800 символов
- Структура: 3–5 абзацев
- Вставляй ключевые слова только там, где они естественно вписываются по смыслу — не перечисляй их подряд списком и не вставляй слово ради самого факта его наличия
- Если ключевое слово — словосочетание из нескольких слов, НЕ вставляй его целиком как готовый оборот и НЕ используй как подлежащее; разбей на смысловые части и согласуй по роду, числу и падежу с остальным текстом
- Не смешивай кириллицу и латиницу внутри одного словосочетания
- Текст должен читаться как связное, осмысленное описание живым русским языком
- На русском языке, грамотно; перед выводом проверь орфографию, пунктуацию и логическую согласованность
- СТРОГО ЗАПРЕЩЕНО (штраф и блокировка карточки):
  * инструкции по применению: дозировки, схемы приёма
  * доставка, возврат, обмен товара
  * цены, скидки, акции, кэшбэк, промокоды
  * призывы купить: "оформите заказ", "купите сейчас", "успейте"
  * контакты: телефоны, email, ссылки, соцсети, домены сайтов (.ru, .com и т.п.)
  * упоминание других маркетплейсов (Ozon, AliExpress и др.) и самого Wildberries
  * хэштеги и SEO-теги внутри текста описания
  * символы ® © ™
  * слова "реплика", "аналог", "копия", "оригинал"

Верни СТРОГО JSON без пояснений до или после, в формате:
{{"title": "новое название", "description": "текст описания"}}"""

        _THINKING_MODELS = ("claude-opus-4", "claude-sonnet-4", "claude-fable")
        use_thinking = any(self.model.startswith(m) for m in _THINKING_MODELS)

        kwargs: Dict[str, Any] = {
            "model": self.model,
            "max_tokens": 2000,
            "messages": [{"role": "user", "content": prompt}],
        }
        if use_thinking:
            kwargs["thinking"] = {"type": "adaptive"}

        try:
            response = self.client.messages.create(**kwargs)
            text = "".join(block.text for block in response.content if block.type == "text")
            match = re.search(r'\{.*\}', text, re.DOTALL)
            if not match:
                raise AIGenerationError("Claude не вернул JSON с названием и описанием")
            data = json.loads(match.group(0))
            title = _enforce_wb_title(str(data.get("title", "")), fallback=product_name, brand=brand)
            description = _sanitize_wb_description(str(data.get("description", "")))
            description = _enforce_wb_description(description)
            if not description:
                raise AIGenerationError("Claude вернул пустое описание")
            return {"title": title, "description": description}

        except anthropic.APIError as exc:
            # Anthropic возвращает нехватку кредитов как 400 invalid_request_error,
            # поэтому проверяем текст ошибки ДО различения по типу/статус-коду
            msg = str(exc)
            if "credit balance is too low" in msg or "insufficient_quota" in msg:
                raise AIGenerationError(f"CREDITS_EXHAUSTED:{exc}")
            if isinstance(exc, anthropic.BadRequestError):
                raise AIGenerationError(f"Неверный запрос к Claude API (400): {exc}")
            raise AIGenerationError(f"Ошибка Claude API: {exc}")
        except AIGenerationError:
            raise
        except Exception as exc:
            raise AIGenerationError(f"Ошибка генерации: {exc}")


# ============================================================================
# ГЕНЕРАТОР ОПИСАНИЯ — GEMINI API (бесплатный тариф)
# ============================================================================

class GeminiDescriptionGenerator:
    """Генератор описаний через Google Gemini API (бесплатно до 1500 запросов/день)."""

    _OZON_PROMPT = """Напиши описание товара для маркетплейса Ozon на русском языке.

Товар: {product_name}

Ключевые поисковые запросы (вплети большинство из них в текст естественно):
{keywords_str}

КАК ПИСАТЬ:
- Пиши как живой человек, а не как робот или копирайтер-шаблонщик
- Разговорный, но грамотный русский язык — как будто опытный продавец рекомендует товар знакомому
- Не начинай с названия товара как заголовка — сразу входи в суть
- Ключевые слова вплетай так, чтобы предложение звучало естественно; если слово не вписывается — не вставляй насильно
- Если ключевые слова на английском или содержат аббревиатуры (МЕ, IU, мг и т.п.) — используй их только там, где это уместно в русском предложении
- НЕ перечисляй ключевые слова списком и не повторяй одно слово подряд несколько раз

СТРУКТУРА (4–5 абзацев, 1000–1800 символов):
1. Что это и зачем нужно — кратко и по делу, 2–3 предложения
2. Польза и для кого подходит — конкретно, без воды
3. Состав, особенности, отличия — то, что покупатель хочет знать
4. Результат и ожидания от использования — без инструкций по применению
5. Краткий вывод — 1–2 предложения

ФОРМАТ:
- Только чистый текст, абзацы разделены пустой строкой
- Никакого markdown: **, *, #, _, ---
- Никаких списков с дефисами или номерами
- Весь текст на русском; английские слова только если это название/аббревиатура без русского аналога

ЗАПРЕЩЕНО:
- Инструкции по применению: "принимайте по...", "нанесите...", дозировки, схемы приёма
- Доставка, возврат, цены, скидки, промокоды
- "Купите сейчас", "оформите заказ", "не упустите"
- Контакты, ссылки, соцсети
- Упоминание других магазинов (Wildberries, AliExpress, Amazon)
- Символы ® © ™
- Слова: реплика, аналог, копия

Текст описания:"""

    _WB_PROMPT = """Напиши описание товара для маркетплейса Wildberries на русском языке.

Товар: {product_name}

Ключевые поисковые запросы (вплети большинство в текст):
{keywords_str}

КАК ПИСАТЬ:
- Живой человеческий язык, не шаблонный копирайтинг
- Пиши как хороший продавец — понятно, по делу, без штампов вроде "высокое качество" и "лучший выбор"
- Ключевые слова вставляй органично; если не вписывается — не форсируй
- Английские аббревиатуры (ME, IU, мг и т.п.) используй только там, где это естественно в русском тексте
- Весь текст на русском языке

СТРУКТУРА (3–4 абзаца, 700–1200 символов):
1. Суть товара и его главная польза
2. Состав, характеристики, особенности
3. Кому подходит и какой результат даёт — без инструкций по применению
4. (опционально) Краткий итог

ФОРМАТ:
- Чистый текст без markdown (**, *, #, _)
- Абзацы через пустую строку
- Никаких списков с дефисами

ЗАПРЕЩЕНО:
- Инструкции по применению: "принимайте по...", "нанесите...", дозировки, схемы приёма
- Доставка, возврат, скидки, цены
- Контакты, ссылки
- Другие маркетплейсы (Ozon, AliExpress)
- Символы ® © ™

Текст описания:"""

    def __init__(self, api_key: str, model: str = "gemini-2.0-flash"):
        if not api_key:
            raise ValueError("Требуется Gemini API key")
        try:
            from google import genai
            # max_retries=0 — отключаем внутренние ретраи SDK, чтобы 503/429
            # обрабатывались нашим кодом с правильными паузами
            self._client = genai.Client(
                api_key=api_key,
                http_options={"retry_config": {"initial_delay": 1.0, "multiplier": 1.0, "max_retries": 0}},
            )
        except Exception:
            # Fallback: старый вариант без http_options (если SDK не поддерживает retry_config)
            try:
                from google import genai as _genai
                self._client = _genai.Client(api_key=api_key)
            except ImportError:
                raise ImportError("Установите библиотеку: pip install google-genai")
        self.model = model
        self._wb_mode = False

    def _call(self, prompt: str) -> str:
        return _sanitize_description(self._call_raw(prompt))[:6000]

    def _call_raw(self, prompt: str) -> str:
        """Как _call, но без санитайзинга/обрезки — нужно для JSON-ответов
        (generate_wb_content), где нельзя портить структуру JSON."""
        _RATE_LIMIT_PAUSE = 65   # секунд ожидания при 429
        _UNAVAIL_PAUSE   = 45   # секунд ожидания при 503
        _MAX_ATTEMPTS    = 5    # попыток всего
        last_exc: Exception = RuntimeError("нет попыток")
        for attempt in range(_MAX_ATTEMPTS):
            try:
                response = self._client.models.generate_content(
                    model=self.model,
                    contents=prompt,
                )
                text = (response.text or "").strip()
                if not text:
                    raise AIGenerationError("Gemini вернул пустой ответ")
                return text
            except AIGenerationError:
                raise
            except Exception as exc:
                last_exc = exc
                msg = str(exc)
                is_rate_limit = "429" in msg or "resource_exhausted" in msg.lower()
                is_unavailable = "503" in msg or "unavailable" in msg.lower()
                is_quota_day = "quota" in msg.lower() and ("day" in msg.lower() or "per_day" in msg.lower() or "GenerateRequestsPerDay" in msg)
                if is_quota_day:
                    raise AIGenerationError(f"CREDITS_EXHAUSTED:Дневной лимит Gemini исчерпан (1500 req/day): {exc}")
                if is_rate_limit and attempt < _MAX_ATTEMPTS - 1:
                    logging.warning(f"[Gemini] 429 rate limit, ожидание {_RATE_LIMIT_PAUSE}с (попытка {attempt+1}/{_MAX_ATTEMPTS})...")
                    time.sleep(_RATE_LIMIT_PAUSE)
                    continue
                if is_unavailable and attempt < _MAX_ATTEMPTS - 1:
                    logging.warning(f"[Gemini] 503 unavailable, ожидание {_UNAVAIL_PAUSE}с (попытка {attempt+1}/{_MAX_ATTEMPTS})...")
                    time.sleep(_UNAVAIL_PAUSE)
                    continue
                if is_rate_limit:
                    raise AIGenerationError(f"CREDITS_EXHAUSTED:Лимит запросов Gemini: {exc}")
                if is_unavailable:
                    raise AIGenerationError(f"UNAVAILABLE:Gemini перегружен, попробуйте позже: {exc}")
                raise AIGenerationError(f"Ошибка Gemini API: {exc}")
        raise AIGenerationError(f"Gemini не ответил после {_MAX_ATTEMPTS} попыток: {last_exc}")

    def generate_description(self, product_name: str, keywords: List[str],
                             competitors: Optional[List[Dict[str, str]]] = None) -> str:
        kws = keywords[:25] if keywords else []
        keywords_str = "\n".join(f"- {kw}" for kw in kws) if kws else "- (не указаны)"
        prompt_tpl = self._WB_PROMPT if self._wb_mode else self._OZON_PROMPT
        prompt = prompt_tpl.format(product_name=product_name, keywords_str=keywords_str)
        if competitors:
            comp_lines = []
            for i, c in enumerate(competitors[:3], 1):
                comp_lines.append(f"Конкурент {i} — {c.get('name', '')[:80]}:\n{c.get('description', '')[:900]}")
            comp_block = "\n\n".join(comp_lines)
            prompt += (
                f"\n\nАНАЛИЗ КОНКУРЕНТОВ (топ по запросу):\n"
                f"Изучи структуру и содержание этих описаний. Используй похожий подход — "
                f"те же смысловые акценты, структуру абзацев, упомянутые свойства. "
                f"Но напиши СВОЁ уникальное описание, не копируй текст:\n\n{comp_block}"
            )
        return self._call(prompt)

    _WB_JSON_PROMPT = """Ты — профессиональный копирайтер для маркетплейса Wildberries. Составь название и описание карточки товара.

Текущее название товара: {product_name}
Ключевые поисковые запросы (вплети большинство в текст описания органично, не в название):
{keywords_str}{brand_line}

ТРЕБОВАНИЯ К НАЗВАНИЮ (официальные правила Wildberries, за нарушение карточку блокируют):
- Не длиннее 60 символов, и чем короче — тем лучше: название должно коротко и точно отвечать на вопрос "что изображено на фото в карточке", это НЕ SEO-заголовок со всеми характеристиками
- Схема: тип товара + при необходимости одна ключевая характеристика, без воды
- СТРОГО ЗАПРЕЩЕНО указывать в названии:
  * бренд или производителя — для этого отдельное поле карточки
  * синонимы и повторы слов
  * лишние подробности: состав, способ применения, характеристики, не нужные для идентификации товара на фото
  * слова ЗАГЛАВНЫМИ БУКВАМИ
  * текст только латиницей без кириллицы (латиница допустима только внутри устоявшихся аббревиатур/единиц)
  * спецсимволы: / \\ * - + @ № % & $ ! = ( ) {{ }} [ ]
  * телефоны, email, ссылки, мессенджеры, эмодзи
  * оценочные суждения: "лучший", "супер", "хит", "премиум", "топ"
  * указание пола, возраста или сезона

ТРЕБОВАНИЯ К ОПИСАНИЮ (700–1600 символов, живой человеческий язык, не шаблонный копирайтинг):
1. Суть товара и его главная польза
2. Состав, характеристики, особенности
3. Кому подходит и какой результат даёт — без инструкций по применению
Ключевые слова вставляй органично, только там, где это естественно по смыслу; не форсируй и не перечисляй списком.

ФОРМАТ ОПИСАНИЯ: чистый текст без markdown (**, *, #, _), абзацы через пустую строку, без списков с дефисами.

ЗАПРЕЩЕНО (и в названии, и в описании):
- Инструкции по применению: дозировки, схемы приёма
- Доставка, возврат, скидки, цены, промокоды
- Контакты, ссылки, домены сайтов (.ru, .com и т.п.)
- Другие маркетплейсы (Ozon, AliExpress) и упоминание самого Wildberries
- Символы ® © ™; слова "реплика", "аналог", "копия"

Верни СТРОГО JSON без пояснений до или после, в формате:
{{"title": "новое название", "description": "текст описания"}}"""

    def generate_wb_content(self, product_name: str, keywords: List[str],
                            competitors: Optional[List[Dict[str, str]]] = None,
                            brand: str = "") -> Dict[str, str]:
        """Генерирует название и описание карточки WB одним запросом (JSON-ответ)."""
        kws = keywords[:25] if keywords else []
        keywords_str = "\n".join(f"- {kw}" for kw in kws) if kws else "- (не указаны)"
        brand_line = (f"\nБренд товара: «{brand}» — это слово НЕЛЬЗЯ включать в название "
                      f"(см. запрет ниже)." if brand else "")
        prompt = self._WB_JSON_PROMPT.format(product_name=product_name, keywords_str=keywords_str,
                                             brand_line=brand_line)
        if competitors:
            comp_lines = []
            for i, c in enumerate(competitors[:3], 1):
                comp_lines.append(f"Конкурент {i} — {c.get('name', '')[:80]}:\n{c.get('description', '')[:900]}")
            comp_block = "\n\n".join(comp_lines)
            prompt += (
                f"\n\nАНАЛИЗ КОНКУРЕНТОВ (топ по запросу):\n"
                f"Изучи структуру и содержание этих описаний. Используй похожий подход — "
                f"те же смысловые акценты, структуру абзацев, упомянутые свойства. "
                f"Но напиши СВОЁ уникальное описание, не копируй текст:\n\n{comp_block}"
            )
        text = self._call_raw(prompt)
        match = re.search(r'\{.*\}', text, re.DOTALL)
        if not match:
            raise AIGenerationError("Gemini не вернул JSON с названием и описанием")
        try:
            data = json.loads(match.group(0))
        except json.JSONDecodeError as exc:
            raise AIGenerationError(f"Gemini вернул повреждённый JSON: {exc}")
        title = _enforce_wb_title(str(data.get("title", "")), fallback=product_name, brand=brand)
        description = _sanitize_wb_description(str(data.get("description", "")))
        description = _enforce_wb_description(description)
        if not description:
            raise AIGenerationError("Gemini вернул пустое описание")
        return {"title": title, "description": description}

    def for_wb(self) -> "GeminiDescriptionGenerator":
        """Возвращает копию генератора настроенную на WB-промпт."""
        clone = GeminiDescriptionGenerator.__new__(GeminiDescriptionGenerator)
        clone._client = self._client
        clone.model = self.model
        clone._wb_mode = True
        return clone


# ============================================================================
# ЛОГИКА ОБНОВЛЕНИЯ
# ============================================================================

def update_product_card(ozon: OzonClient, generator, offer_id: str,
                        days_back: int = 30, top_keywords: int = 15,
                        log_fn=None, confirm_fn=None,
                        mpstats: "MpstatsClient | None" = None,
                        anthropic_api_key: str = "", anthropic_model: str = "claude-opus-4-8",
                        gemini_api_key: str = "", gemini_model: str = "gemini-2.0-flash") -> str:
    """
    Возвращает: "ok" | "skipped" | "error" | "skip_all" | "apply_all"
    confirm_fn(offer_id, name, old_desc, new_desc) -> "apply"|"skip"|"apply_all"|"skip_all"
    """
    def log(msg):
        if log_fn:
            log_fn(msg)

    # Веб-поиск ключевых слов/конкурентов делаем тем же провайдером, что выбран для
    # генерации — чтобы не зависеть от ключа/баланса другого провайдера, которым
    # пользователь мог и не пользоваться
    use_gemini_search = isinstance(generator, GeminiDescriptionGenerator)
    ai_search_key = gemini_api_key if use_gemini_search else anthropic_api_key
    ai_search_name = "Gemini" if use_gemini_search else "Claude"

    try:
        log(f"{'=' * 50}")
        log(f"Обработка товара: {offer_id}")
        snapshot = ozon.build_snapshot(offer_id)
        log(f"  Название: {snapshot.name[:60]}")
        log(f"  SKU: {snapshot.sku}")

        if not snapshot.sku:
            log("  Нет SKU — пропускаем")
            return "skipped"

        # 1. Аналитика Seller API — реальные поисковые запросы покупателей по этому SKU,
        # отсортированы по частотности (самый частотный — первый)
        keywords = ozon.collect_seed_keywords(sku=snapshot.sku, days_back=days_back, top_n=top_keywords)
        if keywords:
            log(f"  Аналитика Ozon: {len(keywords)} поисковых запросов")

        main_kw = extract_main_keyword(snapshot.name)
        log(f"  Главное ключевое слово: «{main_kw}»")

        # 2. Если аналитики по SKU ещё нет (новый товар без истории показов) —
        # ищем реальные запросы покупателей через веб-поиск (тем же провайдером, что и генерация)
        if not keywords and ai_search_key:
            log(f"  Аналитики по SKU нет — ищем ключевые слова через веб-поиск {ai_search_name}...")
            if use_gemini_search:
                keywords = find_keywords_via_gemini(ai_search_key, snapshot.name, "Ozon",
                                                    top_n=top_keywords, model=gemini_model)
            else:
                keywords = find_keywords_via_ai(ai_search_key, snapshot.name, "Ozon",
                                                top_n=top_keywords, model=anthropic_model)
            if keywords:
                log(f"  Найдено через {ai_search_name}: {len(keywords)} запросов")

        # 3. Последний резерв — извлечение слов из названия товара
        if not keywords:
            log("  Извлекаем ключевые слова из названия товара")
            keywords = extract_keywords_from_name(snapshot.name, max_words=top_keywords)
            if not keywords:
                log("  Не удалось извлечь ключевые слова — пропускаем")
                return "skipped"

        keywords = keywords[:top_keywords + 15]  # берём чуть больше для генератора
        log(f"  Итого ключевых слов: {len(keywords)} — {', '.join(keywords[:5])}...")

        # Конкуренты ищем по самому частотному реальному запросу покупателей
        # (а не по эвристике из названия) — так выдача точнее соответствует тому,
        # что реально ищут в маркетплейсе
        competitor_kw = keywords[0] if keywords else main_kw
        competitors: List[Dict[str, str]] = []
        if not isinstance(generator, TemplateDescriptionGenerator):
            log(f"  Ищем конкурентов Ozon по «{competitor_kw}»...")
            competitors = fetch_ozon_competitors(competitor_kw, top_n=3)
            if competitors:
                log(f"  Конкуренты: найдено {len(competitors)} карточек")
            elif ai_search_key:
                log(f"  Прямой доступ заблокирован — ищем конкурентов через веб-поиск {ai_search_name}...")
                if use_gemini_search:
                    competitors = find_competitors_via_gemini(ai_search_key, competitor_kw, "Ozon",
                                                              top_n=3, model=gemini_model)
                else:
                    competitors = find_competitors_via_ai(ai_search_key, competitor_kw, "Ozon",
                                                          top_n=3, model=anthropic_model)
                if competitors:
                    log(f"  Конкуренты (через {ai_search_name}): найдено {len(competitors)} карточек")
                else:
                    log("  Конкуренты: не найдено")
            else:
                log("  Конкуренты: не найдено")

        if isinstance(generator, TemplateDescriptionGenerator):
            gen_name = "шаблону"
        elif isinstance(generator, GeminiDescriptionGenerator):
            gen_name = "Gemini"
        else:
            gen_name = "Claude"
        log(f"  Генерируем описание по {gen_name}...")

        try:
            new_description = generator.generate_description(snapshot.name, keywords,
                                                             competitors=competitors or None)
        except AIGenerationError as exc:
            emsg = str(exc)
            if emsg.startswith("CREDITS_EXHAUSTED:"):
                log(f"  Лимит AI исчерпан ({emsg[18:60]}) — переключаемся на шаблонный генератор")
                fallback = TemplateDescriptionGenerator()
                try:
                    new_description = fallback.generate_description(snapshot.name, keywords)
                except Exception as exc2:
                    log(f"  ОШИБКА шаблонного генератора: {exc2}")
                    return "error"
            elif emsg.startswith("UNAVAILABLE:"):
                log(f"  Gemini перегружен — пропускаем товар, продолжаем со следующим")
                return "skipped"
            else:
                log(f"  ОШИБКА генерации: {exc}")
                return "error"

        log(f"  Описание сгенерировано ({len(new_description)} симв.)")

        desc_attr_id = ozon.find_description_attribute_id(snapshot.description_category_id, snapshot.type_id)
        hashtags_attr_id = ozon.find_hashtags_attribute_id(snapshot.description_category_id, snapshot.type_id)

        # Форматируем хэштеги по правилам Ozon:
        # - однословные стоп-слова пропускаем целиком
        # - многословные фразы: слова соединяем через _, стоп-слова внутри сохраняем
        # - только буквы/цифры/подчёркивание, max 30 символов
        # - дедупликация, лимит 20 штук
        def to_hashtag(kw: str) -> str:
            words = re.split(r'\s+', kw.lower().strip())
            words = [w for w in words if w]
            if not words:
                return ""
            if len(words) == 1:
                # Одно слово — пропускаем стоп-слова и числа
                if words[0] in STOP_WORDS or words[0].isdigit():
                    return ""
                tag = re.sub(r'[^\w]', '', words[0], flags=re.UNICODE)
            else:
                # Фраза: объединяем через _, очищаем каждое слово
                cleaned = [re.sub(r'[^\w]', '', w, flags=re.UNICODE) for w in words]
                cleaned = [w for w in cleaned if w]
                tag = "_".join(cleaned)
            tag = tag.strip("_")
            if not tag or len(tag) < 2:
                return ""
            return ("#" + tag)[:30]

        seen_tags: set = set()
        formatted_hashtags = []
        for kw in keywords:
            t = to_hashtag(kw)
            if t and t not in seen_tags:
                seen_tags.add(t)
                formatted_hashtags.append(t)
        formatted_hashtags = formatted_hashtags[:20]  # Ozon принимает не более 20

        # Показываем диалог "было / стало" если передан коллбэк
        if confirm_fn is not None:
            old_description = ozon.get_current_description(snapshot, desc_attr_id)
            decision = confirm_fn(offer_id, snapshot.name, old_description, new_description)
            if decision in ("skip", "skip_all"):
                log(f"  Пропущено пользователем")
                return decision
            if decision == "apply_all":
                log(f"  Пользователь выбрал 'Применить все'")
                # применяем текущий и сигнализируем о режиме apply_all
        else:
            decision = "apply"

        result = ozon.update_product(
            snapshot=snapshot,
            new_description=new_description,
            hashtags=formatted_hashtags,
            description_attr_id=desc_attr_id,
            hashtags_attr_id=hashtags_attr_id,
        )

        task_id = result.get("result", {}).get("task_id")
        log(f"  [OK] Отправлено на обновление, task_id={task_id}")
        return decision if decision == "apply_all" else "ok"

    except OzonApiError as exc:
        log(f"  [ОШИБКА] API Ozon: {exc}")
        return "error"
    except Exception as exc:
        log(f"  [ОШИБКА] Непредвиденная ошибка: {exc}")
        return "error"


def update_wb_product_card(wb: WbClient, generator, card: Dict[str, Any],
                           log_fn=None, confirm_fn=None,
                           mpstats: "MpstatsClient | None" = None,
                           anthropic_api_key: str = "", anthropic_model: str = "claude-opus-4-8",
                           gemini_api_key: str = "", gemini_model: str = "gemini-2.0-flash") -> str:
    """
    Обновляет описание одной карточки WB.
    Возвращает: "ok" | "skipped" | "error" | "skip_all" | "apply_all"
    """
    def log(msg):
        if log_fn:
            log_fn(msg)

    # Веб-поиск ключевых слов/конкурентов делаем тем же провайдером, что выбран для
    # генерации — чтобы не зависеть от ключа/баланса другого провайдера, которым
    # пользователь мог и не пользоваться
    use_gemini_search = isinstance(generator, GeminiDescriptionGenerator)
    ai_search_key = gemini_api_key if use_gemini_search else anthropic_api_key
    ai_search_name = "Gemini" if use_gemini_search else "Claude"

    try:
        vendor_code = card.get("vendorCode") or card.get("nmID", "?")
        product_name = card.get("title") or card.get("subjectName") or str(vendor_code)
        nm_id = card.get("nmID")
        brand = str(card.get("brand") or "").strip()
        log(f"{'=' * 50}")
        log(f"Обработка: {vendor_code}")
        log(f"  Название: {str(product_name)[:60]}")

        # Главное ключевое слово из названия
        main_kw = extract_main_keyword(product_name)
        log(f"  Главное ключевое слово: «{main_kw}»")

        # Ключевые слова из MPStats по конкретному товару (реальные позиции по nmID)
        keywords: List[str] = []
        if mpstats and nm_id:
            try:
                mp_kws = mpstats.get_wb_item_keywords(nm_id, top_n=30)
                if mp_kws:
                    keywords = mp_kws
                    log(f"  MPStats (товар): {len(keywords)} ключевых слов")
            except Exception as exc:
                log(f"  MPStats ошибка: {exc}")

        # Если данных MPStats нет — ищем реальные запросы через веб-поиск (тем же провайдером, что и генерация)
        if not keywords and ai_search_key:
            log(f"  Данных MPStats нет — ищем ключевые слова через веб-поиск {ai_search_name}...")
            if use_gemini_search:
                keywords = find_keywords_via_gemini(ai_search_key, product_name, "Wildberries",
                                                    top_n=15, model=gemini_model)
            else:
                keywords = find_keywords_via_ai(ai_search_key, product_name, "Wildberries",
                                                top_n=15, model=anthropic_model)
            if keywords:
                log(f"  Найдено через {ai_search_name}: {len(keywords)} запросов")

        # Последний резерв — извлечение слов из названия товара
        if not keywords:
            log("  Извлекаем ключевые слова из названия товара")
            keywords = extract_keywords_from_name(product_name, max_words=15)

        if not keywords:
            log("  Нет ключевых слов — пропускаем")
            return "skipped"

        log(f"  Итого ключевых слов: {len(keywords)} — {', '.join(keywords[:5])}...")

        # Конкуренты ищем по самому частотному реальному запросу (из MPStats по товару
        # или из названия), а не по эвристике — так выдача точнее
        competitor_kw = keywords[0] if keywords else main_kw
        competitors: List[Dict[str, str]] = []
        if not isinstance(generator, WbDescriptionGenerator):
            log(f"  Ищем конкурентов WB по «{competitor_kw}»...")
            competitors = fetch_wb_competitors(competitor_kw, top_n=3)
            if competitors:
                log(f"  Конкуренты: найдено {len(competitors)} карточек")
            elif ai_search_key:
                log(f"  Прямой доступ заблокирован — ищем конкурентов через веб-поиск {ai_search_name}...")
                if use_gemini_search:
                    competitors = find_competitors_via_gemini(ai_search_key, competitor_kw, "Wildberries",
                                                              top_n=3, model=gemini_model)
                else:
                    competitors = find_competitors_via_ai(ai_search_key, competitor_kw, "Wildberries",
                                                          top_n=3, model=anthropic_model)
                if competitors:
                    log(f"  Конкуренты (через {ai_search_name}): найдено {len(competitors)} карточек")
                else:
                    log("  Конкуренты: не найдено")
            else:
                log("  Конкуренты: не найдено")

        if isinstance(generator, WbDescriptionGenerator):
            gen_name = "шаблону WB"
        elif isinstance(generator, GeminiDescriptionGenerator):
            gen_name = "Gemini"
        else:
            gen_name = "Claude"
        log(f"  Генерируем название и описание по {gen_name}...")

        try:
            content = generator.generate_wb_content(product_name, keywords,
                                                    competitors=competitors or None, brand=brand)
        except AIGenerationError as exc:
            emsg = str(exc)
            if emsg.startswith("CREDITS_EXHAUSTED:"):
                log(f"  Лимит AI исчерпан ({emsg[18:60]}) — переключаемся на шаблонный генератор WB")
                fallback = WbDescriptionGenerator()
                try:
                    content = fallback.generate_wb_content(product_name, keywords, brand=brand)
                except Exception as exc2:
                    log(f"  ОШИБКА шаблонного генератора: {exc2}")
                    return "error"
            elif emsg.startswith("UNAVAILABLE:"):
                log(f"  Gemini перегружен — пропускаем товар, продолжаем со следующим")
                return "skipped"
            else:
                log(f"  ОШИБКА генерации: {exc}")
                return "error"

        # Дополнительная проверка на соответствие требованиям WB — на случай, если
        # генератор всё же вернул что-то, выходящее за лимиты (защита от регрессий)
        new_title = _enforce_wb_title(content.get("title", ""), fallback=product_name, brand=brand)
        new_description = _enforce_wb_description(_sanitize_wb_description(content.get("description", "")))
        if not new_description:
            log("  ОШИБКА: пустое описание после проверки требований WB")
            return "error"

        log(f"  Название сгенерировано ({len(new_title)}/{WB_TITLE_MAX} симв.), "
            f"описание сгенерировано ({len(new_description)} симв.)")

        old_title = str(card.get("title") or "")
        old_description = wb.get_current_description(card)

        if confirm_fn is not None:
            decision = confirm_fn(str(vendor_code), product_name, old_description, new_description,
                                  old_title=old_title, new_title=new_title)
            if decision in ("skip", "skip_all"):
                log("  Пропущено пользователем")
                return decision
            if decision == "apply_all":
                log("  Пользователь выбрал 'Применить все'")
        else:
            decision = "apply"

        # Обновляем название и описание — остальные поля карточки сохраняем как есть
        updated_card = dict(card)
        updated_card["title"] = new_title
        updated_card["description"] = new_description
        wb.update_card(updated_card)
        log(f"  [OK] Название и описание обновлены")
        return decision if decision == "apply_all" else "ok"

    except WbApiError as exc:
        log(f"  [ОШИБКА] API WB: {exc}")
        return "error"
    except Exception as exc:
        log(f"  [ОШИБКА] Непредвиденная ошибка: {exc}")
        return "error"


# ============================================================================
# КОНФИГ
# ============================================================================

def load_config() -> Dict[str, Any]:
    if not os.path.exists(CONFIG_PATH):
        return {
            "ozon": {"client_id": "", "api_key": ""},
            "performance": {"enabled": False, "client_id": "", "client_secret": ""},
            "wb": {"api_key": ""},
            "mpstats": {"token": ""},
            "ai": {"provider": "template", "anthropic_api_key": "", "model": "claude-opus-4-8",
                   "gemini_api_key": "", "gemini_model": "gemini-2.0-flash"},
            "update": {"days_back": 30, "top_keywords": 15, "target_offer_ids": []},
            "wb_ads": {"step_pct": 10.0, "campaigns": {}},
            "ozon_ads": {"products": {}},
        }
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def save_config(cfg: Dict[str, Any]):
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)


# ============================================================================
# ДИАЛОГ ЗАПОЛНЕНИЯ ОБЯЗАТЕЛЬНЫХ АТРИБУТОВ OZON
# ============================================================================

class OzonAttributesDialog(tk.Toplevel):
    """Диалог для заполнения обязательных атрибутов при создании товара на Ozon."""

    def __init__(self, parent, ozon_client: "OzonClient",
                 category_id: int, type_id: int,
                 prefill: Optional[Dict[str, str]] = None,
                 wb_card: Optional[Dict] = None):
        super().__init__(parent)
        self.title("Обязательные атрибуты Ozon")
        self.geometry("680x580")
        self.resizable(True, True)
        self.grab_set()

        self.ozon_client = ozon_client
        self.category_id = category_id
        self.type_id = type_id
        self.result: Optional[List[Dict]] = None  # None = отмена, [] = нет обязательных
        self._widgets: List[Dict] = []  # [{attr_id, name, widget, is_dict}]
        self._dict_options: Dict[int, List[Dict]] = {}  # attr_id -> [{id, value}]

        # Строим карту автозаполнения из WB карточки
        wb_card = wb_card or {}
        prefill = dict(prefill or {})
        self._wb_prefill = self._build_wb_prefill(wb_card, prefill)

        # Заголовок
        ttk.Label(self, text="Заполните обязательные атрибуты категории",
                  font=("", 10, "bold")).pack(padx=12, pady=(10, 4), anchor="w")
        ttk.Label(self, text="Поля без звёздочки (*) — рекомендованные.",
                  foreground="gray").pack(padx=12, anchor="w")

        # Область прокрутки
        outer = ttk.Frame(self)
        outer.pack(fill="both", expand=True, padx=12, pady=8)
        canvas = tk.Canvas(outer, borderwidth=0, highlightthickness=0)
        vsb = ttk.Scrollbar(outer, orient="vertical", command=canvas.yview)
        canvas.configure(yscrollcommand=vsb.set)
        vsb.pack(side="right", fill="y")
        canvas.pack(side="left", fill="both", expand=True)
        self._form_frame = ttk.Frame(canvas)
        self._form_win = canvas.create_window((0, 0), window=self._form_frame, anchor="nw")
        self._form_frame.bind("<Configure>", lambda e: canvas.configure(
            scrollregion=canvas.bbox("all")))
        canvas.bind("<Configure>", lambda e: canvas.itemconfig(
            self._form_win, width=e.width))
        def _on_mousewheel(e):
            try:
                canvas.yview_scroll(-1 * (e.delta // 120), "units")
            except Exception:
                pass
        canvas.bind("<MouseWheel>", _on_mousewheel)
        self._form_frame.bind("<MouseWheel>", _on_mousewheel)
        self.protocol("WM_DELETE_WINDOW", self._on_close)

        # Статус загрузки
        self._status = ttk.Label(self._form_frame, text="Загрузка атрибутов...", foreground="gray")
        self._status.grid(row=0, column=0, columnspan=2, padx=8, pady=8, sticky="w")

        # Кнопки
        bf = ttk.Frame(self)
        bf.pack(pady=10)
        self._ok_btn = ttk.Button(bf, text="Создать товар", state="disabled",
                                  command=self._on_ok)
        self._ok_btn.pack(side="left", padx=8)
        ttk.Button(bf, text="Отмена", command=self._on_close).pack(side="left", padx=8)

        self._prefill = self._wb_prefill
        threading.Thread(target=self._load_attrs, daemon=True).start()
        self.wait_window()

    @staticmethod
    def _build_wb_prefill(wb_card: Dict, base: Dict) -> Dict[str, str]:
        """Строит словарь автозаполнения из WB карточки для атрибутов Ozon."""
        result = dict(base)

        brand = wb_card.get("brand") or ""

        # Прямые поля — добавляем под всеми возможными именами Ozon-атрибутов
        direct_multi: Dict[str, str] = {
            "Название": wb_card.get("title") or wb_card.get("name") or "",
            "Наименование": wb_card.get("title") or wb_card.get("name") or "",
            "Бренд": brand,
            "Brand": brand,
            "Торговая марка": brand,
            "Марка": brand,
            "Производитель": brand,
            "Описание": wb_card.get("description") or "",
            "Аннотация": wb_card.get("description") or "",
            "аннотация": wb_card.get("description") or "",
            "Annotation": wb_card.get("description") or "",
            "Аннотация к товару": wb_card.get("description") or "",
            "аннотация к товару": wb_card.get("description") or "",
            "Описание товара": wb_card.get("description") or "",
            "описание товара": wb_card.get("description") or "",
            "Артикул": wb_card.get("vendorCode") or "",
            "Артикул производителя": wb_card.get("vendorCode") or "",
            "Артикул товара": wb_card.get("vendorCode") or "",
        }
        for k, v in direct_multi.items():
            if v:
                result[k] = v
                result[k.lower()] = v

        # Характеристики WB — две возможные структуры:
        # 1. [{name: str, value: [str, ...]}]  — новый формат
        # 2. {attr_name: attr_value, ...}       — старый формат
        wb_chars: Dict[str, str] = {}
        for ch in wb_card.get("characteristics", []) or []:
            if not isinstance(ch, dict):
                continue
            # Новый формат: {name: "Бренд", value: ["Три кота"]}
            if "name" in ch and "value" in ch:
                attr_name = str(ch["name"])
                attr_val = ch["value"]
                val_str = ", ".join(str(v) for v in attr_val) if isinstance(attr_val, list) else str(attr_val)
            else:
                # Старый формат: {"Бренд": ["Три кота"], ...}
                for attr_name, attr_val in ch.items():
                    if attr_name == "id":
                        continue
                    val_str = ", ".join(str(v) for v in attr_val) if isinstance(attr_val, list) else str(attr_val)
                    wb_chars[attr_name] = val_str
                    wb_chars[attr_name.lower()] = val_str
                continue
            if val_str:
                wb_chars[attr_name] = val_str
                wb_chars[attr_name.lower()] = val_str

        # Явные маппинги WB → Ozon для часто несовпадающих имён
        _WB_TO_OZON = {
            "ТН ВЭД": "ТН ВЭД коды ЕАЭС",
            "тн вэд": "ТН ВЭД коды ЕАЭС",
            "тнвэд": "ТН ВЭД коды ЕАЭС",
            "ТНВЭД": "ТН ВЭД коды ЕАЭС",
            "Код ТНВЭД": "ТН ВЭД коды ЕАЭС",
            "код тнвэд": "ТН ВЭД коды ЕАЭС",
            "Код ТН ВЭД": "ТН ВЭД коды ЕАЭС",
            "Страна производства": "Страна-изготовитель",
            "Страна изготовления": "Страна-изготовитель",
            "страна производства": "Страна-изготовитель",
            "Пол": "Пол",
            "Возраст": "Возраст",
            "Цвет": "Цвет товара",
            "цвет": "Цвет товара",
            "Материал": "Материал",
            "Описание": "Аннотация",
            "описание": "Аннотация",
        }
        # Ключи WB для срока годности — конвертируем в дни для Ozon
        _SHELF_LIFE_KEYS = {
            "срок годности", "срок хранения", "shelf life",
            "срок годности (лет)", "срок хранения (лет)",
            "срок годности (дней)", "срок хранения (дней)",
        }

        for wb_key, val in wb_chars.items():
            result[wb_key] = val
            ozon_key = _WB_TO_OZON.get(wb_key)
            if ozon_key:
                result[ozon_key] = val
                result[ozon_key.lower()] = val

            # Конвертация срока годности с определением единицы измерения
            if wb_key.lower() in _SHELF_LIFE_KEYS:
                try:
                    val_lower = val.lower()
                    num_str = re.sub(r"[^\d.,]", "", val.replace(",", ".")).strip(".")
                    num = float(num_str) if num_str else 0
                    if num > 0:
                        # Определяем единицу из текста значения
                        if any(u in val_lower for u in ("дн", "day", "суток")):
                            days = int(round(num))          # уже в днях
                            unit_desc = "дней"
                        elif any(u in val_lower for u in ("мес", "month")):
                            days = int(round(num * 30))     # месяцы → дни
                            unit_desc = f"мес → {days} дней"
                        elif any(u in val_lower for u in ("лет", "год", "year")) or "(лет)" in wb_key.lower():
                            days = int(round(num * 365))    # годы → дни
                            unit_desc = f"лет → {days} дней"
                        elif num < 50:
                            # Малое число без единицы — скорее всего годы
                            days = int(round(num * 365))
                            unit_desc = f"(авто: годы) → {days} дней"
                        else:
                            # Большое число без единицы — скорее всего дни
                            days = int(round(num))
                            unit_desc = "дней (авто)"
                        # Защита от абсурдных значений (напр. "720 лет" = ошибка в WB)
                        _MAX_DAYS = 3650  # 10 лет максимум
                        if days > _MAX_DAYS:
                            logging.warning(f"[wb_prefill] срок годности {days} дней слишком большой ({val!r}), ограничиваем до {_MAX_DAYS}")
                            days = _MAX_DAYS
                        days_str = str(days)
                        result["Срок годности"] = days_str
                        result["срок годности"] = days_str
                        result["Срок хранения"] = days_str
                        result["срок хранения"] = days_str
                        result["Срок годности в днях"] = days_str
                        result["срок годности в днях"] = days_str
                        logging.info(f"[wb_prefill] срок годности: {val!r} → {unit_desc}")
                except (ValueError, AttributeError):
                    pass

        logging.info(f"[wb_prefill] ключей: {len(result)}, примеры: { {k: v[:30] for k, v in list(result.items())[:6]} }")
        return result

    def _on_close(self):
        self.result = None
        self.destroy()

    def _load_attrs(self):
        try:
            attrs = self.ozon_client.get_category_attributes(self.category_id, self.type_id)
            required = [a for a in attrs if a.get("is_required")]
            # Рекомендованные: с group_id ИЛИ те для которых есть prefill-значение
            prefill = self._wb_prefill
            def _has_prefill(a):
                name = a.get("name", "")
                return (prefill.get(name) or prefill.get(name.lower()) or
                        any(pk.lower() in name.lower() or name.lower() in pk.lower()
                            for pk in prefill if prefill[pk]))
            recommended = [a for a in attrs
                           if not a.get("is_required")
                           and (a.get("group_id") or _has_prefill(a))]
            show = required + recommended[:20]
            self.after(0, lambda: self._build_form(show, required))
        except Exception as e:
            err_msg = str(e)
            self.after(0, lambda m=err_msg: (
                self._status.config(text=f"Ошибка загрузки атрибутов: {m}", foreground="red"),
                self._ok_btn.config(state="normal"),
            ))

    def _build_form(self, attrs: List[Dict], required_attrs: List[Dict]):
        self._status.destroy()
        required_ids = {a.get("id") for a in required_attrs}

        if not attrs:
            ttk.Label(self._form_frame, text="Обязательных атрибутов нет — товар можно создать.",
                      foreground="green").grid(row=0, column=0, columnspan=2, padx=8, pady=8, sticky="w")
            self._ok_btn.config(state="normal")
            return

        row = 0
        has_recommended = any(a.get("id") not in required_ids for a in attrs)
        req_header_added = False
        rec_header_added = False

        for attr in attrs:
            attr_id = attr.get("id", 0)
            name = attr.get("name", f"attr_{attr_id}")
            description = attr.get("description", "")
            is_req = attr_id in required_ids
            dict_id = attr.get("dictionary_id", 0)
            is_multi = attr.get("is_collection", False)
            attr_type = attr.get("type", "String")

            # Разделители секций
            if is_req and not req_header_added:
                ttk.Separator(self._form_frame, orient="horizontal").grid(
                    row=row, column=0, columnspan=2, sticky="ew", padx=8, pady=(4, 0))
                row += 1
                ttk.Label(self._form_frame, text="  Обязательные поля  ",
                          foreground="#cc4444", font=("", 9, "bold")).grid(
                    row=row, column=0, columnspan=2, sticky="w", padx=8, pady=(0, 4))
                row += 1
                req_header_added = True
            elif not is_req and has_recommended and not rec_header_added:
                ttk.Separator(self._form_frame, orient="horizontal").grid(
                    row=row, column=0, columnspan=2, sticky="ew", padx=8, pady=(8, 0))
                row += 1
                ttk.Label(self._form_frame, text="  Рекомендованные  ",
                          foreground="#888888", font=("", 9, "bold")).grid(
                    row=row, column=0, columnspan=2, sticky="w", padx=8, pady=(0, 4))
                row += 1
                rec_header_added = True

            # Метка атрибута
            marker = "* " if is_req else "   "
            _type_hints = {"Integer": "целое число", "Decimal": "число", "Boolean": "да/нет"}
            hint = f" ({_type_hints.get(attr_type, attr_type)})" if attr_type and attr_type not in ("String", "URL", "ImageURL") else ""
            hint += " [несколько]" if is_multi else ""
            label_text = f"{marker}{name}{hint}"
            lbl = ttk.Label(self._form_frame, text=label_text,
                            foreground="#cc4444" if is_req else "#555555",
                            cursor="question_arrow" if description else "")
            lbl.grid(row=row, column=0, sticky="nw", padx=(8, 4), pady=3)
            if description:
                # Tooltip через balloon
                lbl.bind("<Enter>", lambda e, d=description, w=lbl:
                         w.config(text=w.cget("text").split("\n")[0] + f"\n{d[:120]}"))
                lbl.bind("<Leave>", lambda e, t=label_text, w=lbl: w.config(text=t))

            prefill_val = self._prefill.get(name) or self._prefill.get(name.lower()) or ""
            # Нечёткий поиск: ищем ключ prefill содержащий название атрибута
            if not prefill_val:
                name_lower = name.lower()
                for pk, pv in self._prefill.items():
                    if pk.lower() in name_lower or name_lower in pk.lower():
                        prefill_val = pv
                        break
            if prefill_val:
                logging.info(f"[prefill] '{name}' → {prefill_val[:60]!r}")
            else:
                logging.info(f"[prefill] '{name}' → (пусто)")

            if attr_type == "Boolean":
                # Булевый атрибут — выпадающий Да/Нет
                var = tk.StringVar()
                combo = ttk.Combobox(self._form_frame, textvariable=var, width=42,
                                     values=["Да", "Нет"], state="readonly")
                # Автозаполнение: если prefill содержит да/true/1 → Да, иначе Нет
                pv_low = prefill_val.lower() if prefill_val else ""
                combo.set("Да" if pv_low in ("да", "true", "1", "yes") else "Нет")
                combo.grid(row=row, column=1, sticky="ew", padx=(0, 8), pady=3)
                self._widgets.append({"attr_id": attr_id, "name": name, "widget": combo,
                                      "var": var, "is_dict": False, "is_req": is_req,
                                      "is_multi": False, "is_bool": True, "attr_type": "Boolean"})

            elif dict_id:
                # Словарный атрибут — Combobox с поиском (редактируемый)
                var = tk.StringVar(value=prefill_val)
                combo = ttk.Combobox(self._form_frame, textvariable=var, width=42)
                combo.grid(row=row, column=1, sticky="ew", padx=(0, 8), pady=3)
                combo["values"] = ["Загрузка..."]
                combo.set("Загрузка...")
                # Поиск при вводе (только после загрузки словаря)
                combo.bind("<KeyRelease>", lambda e, c=combo, aid=attr_id: self._filter_combo(c, aid))
                self._widgets.append({"attr_id": attr_id, "name": name, "widget": combo,
                                      "var": var, "is_dict": True, "is_req": is_req,
                                      "is_multi": is_multi, "is_bool": False, "attr_type": attr_type})
                threading.Thread(target=self._load_dict_values,
                                 args=(attr_id, dict_id, combo, var, prefill_val),
                                 daemon=True).start()
            else:
                # Текстовый / числовой атрибут
                entry = ttk.Entry(self._form_frame, width=44)
                entry.insert(0, prefill_val)
                entry.grid(row=row, column=1, sticky="ew", padx=(0, 8), pady=3)
                if is_multi:
                    ttk.Label(self._form_frame, text="(через запятую)",
                              foreground="gray", font=("", 8)).grid(
                        row=row + 1, column=1, sticky="w", padx=(0, 8))
                    row += 1
                self._widgets.append({"attr_id": attr_id, "name": name, "widget": entry,
                                      "is_dict": False, "is_req": is_req, "is_multi": is_multi,
                                      "is_bool": False, "attr_type": attr_type})

            row += 1

        self._form_frame.columnconfigure(1, weight=1)
        self._ok_btn.config(state="normal")

    def _filter_combo(self, combo: ttk.Combobox, attr_id: int):
        """Фильтрует варианты combobox по введённому тексту."""
        typed = combo.get().lower()
        options = self._dict_options.get(attr_id, [])
        if not typed:
            combo["values"] = [v.get("value", "") for v in options]
        else:
            filtered = [v.get("value", "") for v in options if typed in v.get("value", "").lower()]
            combo["values"] = filtered[:200]

    def _load_dict_values(self, attr_id: int, dict_id: int, combo: ttk.Combobox,
                          var: tk.StringVar, prefill_val: str):
        try:
            # Загружаем значения словаря через API
            last_value_id = 0
            all_vals: List[Dict] = []
            for _ in range(20):  # max 20 страниц
                resp = self.ozon_client._post("/v1/description-category/attribute/values", {
                    "attribute_id": attr_id,
                    "description_category_id": self.category_id,
                    "type_id": self.type_id,
                    "language": "RU",
                    "last_value_id": last_value_id,
                    "limit": 5000,
                })
                vals = resp.get("result", []) or []
                all_vals.extend(vals)
                if not resp.get("has_next"):
                    break
                last_value_id = vals[-1].get("id", 0) if vals else 0
            self._dict_options[attr_id] = all_vals
            display = [v.get("value", "") for v in all_vals]
            def upd(d=display, pv=prefill_val):
                combo["values"] = d
                # Оставляем редактируемым для поиска
                if pv:
                    pv_lower = pv.lower()
                    # 1. Точное совпадение
                    if pv in d:
                        combo.set(pv)
                        return
                    # 2. Без учёта регистра
                    for item in d:
                        if item.lower() == pv_lower:
                            combo.set(item)
                            return
                    # 3. Начинается с prefill
                    for item in d:
                        if item.lower().startswith(pv_lower):
                            combo.set(item)
                            return
                    # 4. prefill содержится в значении словаря
                    for item in d:
                        if pv_lower in item.lower():
                            combo.set(item)
                            return
                combo.set("")
            self.after(0, upd)
        except Exception as e:
            err_msg = str(e)
            self.after(0, lambda m=err_msg: combo.configure(values=[f"Ошибка: {m}"], state="readonly"))

    def _on_ok(self):
        attributes: List[Dict] = []
        errors = []
        for w in self._widgets:
            attr_id = w["attr_id"]
            is_req = w["is_req"]
            is_dict = w["is_dict"]
            is_multi = w["is_multi"]
            is_bool = w.get("is_bool", False)

            attr_type = w.get("attr_type", "String")

            if is_bool:
                val_text = w["var"].get().strip()
                # Boolean: Ozon принимает "true"/"false" как строку
                bool_val = "true" if val_text == "Да" else "false"
                attributes.append({
                    "id": attr_id, "complex_id": 0,
                    "values": [{"value": bool_val}]
                })

            elif is_dict:
                val_text = w["var"].get().strip()
                if not val_text or val_text.startswith("Загрузка") or val_text.startswith("Ошибка"):
                    if is_req:
                        errors.append(w["name"])
                    continue
                options = self._dict_options.get(attr_id, [])
                # Точное совпадение, потом без регистра
                matched = [o for o in options if o.get("value") == val_text]
                if not matched:
                    val_lower = val_text.lower()
                    matched = [o for o in options if o.get("value", "").lower() == val_lower]
                if matched:
                    attributes.append({
                        "id": attr_id, "complex_id": 0,
                        "values": [{"dictionary_value_id": matched[0]["id"], "value": matched[0]["value"]}]
                    })
                else:
                    # Значения нет в словаре — отправляем как текст (некоторые словари это допускают)
                    attributes.append({
                        "id": attr_id, "complex_id": 0,
                        "values": [{"value": val_text}]
                    })
                    if is_req and not val_text:
                        errors.append(w["name"])
            else:
                val_text = w["widget"].get().strip()
                if not val_text:
                    if is_req:
                        errors.append(w["name"])
                    continue
                # Для Integer — проверяем что значение числовое
                if attr_type in ("Integer", "Decimal"):
                    # Берём только цифры (убираем единицы измерения типа "120 шт")
                    digits = re.sub(r"[^\d]", "", val_text.split()[0]) if val_text else ""
                    if not digits:
                        if is_req:
                            errors.append(f"{w['name']} (требуется число)")
                        continue
                    val_text = digits
                if is_multi:
                    parts = [p.strip() for p in val_text.split(",") if p.strip()]
                    attributes.append({
                        "id": attr_id, "complex_id": 0,
                        "values": [{"value": p} for p in parts]
                    })
                else:
                    attributes.append({
                        "id": attr_id, "complex_id": 0,
                        "values": [{"value": val_text}]
                    })

        if errors:
            messagebox.showwarning(
                "Обязательные поля",
                "Заполните обязательные атрибуты:\n• " + "\n• ".join(errors),
                parent=self
            )
            return

        self.result = attributes
        self.destroy()


# ============================================================================
# ДИАЛОГ ВЫБОРА ТОВАРОВ ДЛЯ ОБНОВЛЕНИЯ
# ============================================================================

class ProductSelectorDialog(tk.Toplevel):
    """Диалог выбора конкретных товаров для обновления описаний."""

    def __init__(self, parent, items: list, title: str = "Выбор товаров"):
        """
        items: список кортежей (id, display_name)
        После закрытия self.selected содержит список выбранных id, или None если отменено.
        """
        super().__init__(parent)
        self.title(title)
        self.resizable(True, True)
        self.geometry("700x520")
        self.transient(parent)
        self.grab_set()
        self.selected = None
        self._all_items = items  # [(id, name), ...]
        self._vars: Dict[str, tk.BooleanVar] = {}
        self._build(items)
        self.wait_window(self)

    def _build(self, items):
        # Поиск
        sf = ttk.Frame(self); sf.pack(fill="x", padx=10, pady=(8, 4))
        ttk.Label(sf, text="Поиск:").pack(side="left")
        self._search_var = tk.StringVar()
        self._search_var.trace_add("write", lambda *_: self._filter())
        ttk.Entry(sf, textvariable=self._search_var, width=40).pack(side="left", padx=6)
        ttk.Button(sf, text="Выбрать все", command=self._select_all).pack(side="left", padx=4)
        ttk.Button(sf, text="Снять все", command=self._deselect_all).pack(side="left", padx=2)
        self._count_label = ttk.Label(sf, text="")
        self._count_label.pack(side="right", padx=6)

        # Список с чекбоксами
        lf = ttk.Frame(self); lf.pack(fill="both", expand=True, padx=10, pady=4)
        vsb = ttk.Scrollbar(lf, orient="vertical")
        self._canvas = tk.Canvas(lf, yscrollcommand=vsb.set, highlightthickness=0)
        vsb.config(command=self._canvas.yview)
        vsb.pack(side="right", fill="y")
        self._canvas.pack(side="left", fill="both", expand=True)
        self._inner = ttk.Frame(self._canvas)
        self._canvas_window = self._canvas.create_window((0, 0), window=self._inner, anchor="nw")
        self._inner.bind("<Configure>", lambda e: self._canvas.configure(
            scrollregion=self._canvas.bbox("all")))
        self._canvas.bind("<Configure>", lambda e: self._canvas.itemconfig(
            self._canvas_window, width=e.width))

        def _on_mousewheel(e):
            if self._canvas.winfo_exists():
                self._canvas.yview_scroll(-1 * (e.delta // 120), "units")

        self._canvas.bind_all("<MouseWheel>", _on_mousewheel)
        self.bind("<Destroy>", lambda e: self._canvas.unbind_all("<MouseWheel>") if e.widget is self else None)

        for item_id, name in items:
            var = tk.BooleanVar(value=True)
            self._vars[item_id] = var
            ttk.Checkbutton(self._inner, text=f"{item_id}  —  {name[:80]}", variable=var,
                            style="TCheckbutton").pack(anchor="w", padx=6, pady=1)

        self._update_count()

        # Кнопки OK/Отмена
        bf = ttk.Frame(self); bf.pack(pady=8)
        ttk.Button(bf, text="Запустить выбранные", command=self._ok).pack(side="left", padx=8)
        ttk.Button(bf, text="Отмена", command=self._cancel).pack(side="left", padx=4)

    def _filter(self):
        q = self._search_var.get().lower()
        for w in self._inner.winfo_children():
            w.destroy()
        for item_id, name in self._all_items:
            if q in str(item_id).lower() or q in str(name).lower():
                var = self._vars[item_id]
                ttk.Checkbutton(self._inner, text=f"{item_id}  —  {name[:80]}", variable=var).pack(
                    anchor="w", padx=6, pady=1)
        self._canvas.configure(scrollregion=self._canvas.bbox("all"))
        self._update_count()

    def _select_all(self):
        q = self._search_var.get().lower()
        for item_id, name in self._all_items:
            if not q or q in str(item_id).lower() or q in str(name).lower():
                self._vars[item_id].set(True)
        self._update_count()

    def _deselect_all(self):
        q = self._search_var.get().lower()
        for item_id, name in self._all_items:
            if not q or q in str(item_id).lower() or q in str(name).lower():
                self._vars[item_id].set(False)
        self._update_count()

    def _update_count(self):
        total = len(self._vars)
        checked = sum(1 for v in self._vars.values() if v.get())
        self._count_label.config(text=f"Выбрано: {checked}/{total}")

    def _ok(self):
        self.selected = [item_id for item_id, _ in self._all_items if self._vars[item_id].get()]
        self.destroy()

    def _cancel(self):
        self.selected = None
        self.destroy()


# ============================================================================
# ДИАЛОГ ПРЕДПРОСМОТРА "БЫЛО / СТАЛО"
# ============================================================================

class TransferDialog(tk.Toplevel):
    """Диалог переноса товара с одной площадки на другую."""

    def __init__(self, parent, direction: str, sku: str, name: str,
                 description: str, price: float,
                 images: List[str], ozon_client: "OzonClient",
                 wb_client: "WbClient", wb_card: Optional[Dict] = None):
        super().__init__(parent)
        self.direction = direction  # "ozon_to_wb" or "wb_to_ozon"
        self.sku = sku
        self.ozon_client = ozon_client
        self.wb_client = wb_client
        self.wb_card = wb_card or {}
        self.result = None
        self._categories: List[Dict] = []
        self._flat_cats: List[tuple] = []  # (display_name, id, type_id_or_subject_id)

        arrow = "Ozon → WB" if direction == "ozon_to_wb" else "WB → Ozon"
        self.title(f"Перенос товара: {arrow}")
        self.geometry("720x600")
        self.resizable(True, True)
        self.grab_set()

        # ── Поля товара ──
        top = ttk.Frame(self); top.pack(fill="x", padx=16, pady=10)

        ttk.Label(top, text="Артикул (vendor code / offer_id):").grid(row=0, column=0, sticky="w", pady=3)
        self.e_sku = ttk.Entry(top, width=50); self.e_sku.insert(0, sku)
        self.e_sku.grid(row=0, column=1, sticky="ew", padx=8, pady=3)

        ttk.Label(top, text="Название:").grid(row=1, column=0, sticky="w", pady=3)
        self.e_name = ttk.Entry(top, width=50); self.e_name.insert(0, name)
        self.e_name.grid(row=1, column=1, sticky="ew", padx=8, pady=3)

        ttk.Label(top, text="Цена (₽):").grid(row=2, column=0, sticky="w", pady=3)
        self.e_price = ttk.Entry(top, width=20)
        self.e_price.insert(0, str(int(price)) if price else "0")
        self.e_price.grid(row=2, column=1, sticky="w", padx=8, pady=3)

        ttk.Label(top, text="Описание:").grid(row=3, column=0, sticky="nw", pady=3)
        self.t_desc = tk.Text(top, width=50, height=5, wrap="word")
        self.t_desc.insert("1.0", description)
        self.t_desc.grid(row=3, column=1, sticky="ew", padx=8, pady=3)
        top.columnconfigure(1, weight=1)

        ttk.Label(top, text="Изображения (URL, каждый с новой строки):").grid(row=4, column=0, sticky="nw", pady=3)
        self.t_images = tk.Text(top, width=50, height=3, wrap="none")
        self.t_images.insert("1.0", "\n".join(images[:10]))
        self.t_images.grid(row=4, column=1, sticky="ew", padx=8, pady=3)

        # ── Выбор категории ──
        cat_frame = ttk.LabelFrame(self, text="Категория на целевой площадке")
        cat_frame.pack(fill="x", padx=16, pady=6)

        sf = ttk.Frame(cat_frame); sf.pack(fill="x", padx=8, pady=6)
        ttk.Label(sf, text="Поиск:").pack(side="left")
        self.cat_search = ttk.Entry(sf, width=40); self.cat_search.pack(side="left", padx=6)
        ttk.Button(sf, text="Найти", command=self._search_categories).pack(side="left")
        ttk.Button(sf, text="Загрузить все", command=self._load_all_categories).pack(side="left", padx=4)

        self.cat_listbox = tk.Listbox(cat_frame, height=6, selectmode="single")
        sb = ttk.Scrollbar(cat_frame, orient="vertical", command=self.cat_listbox.yview)
        self.cat_listbox.config(yscrollcommand=sb.set)
        self.cat_listbox.pack(side="left", fill="both", expand=True, padx=8, pady=4)
        sb.pack(side="right", fill="y", pady=4)

        self.cat_label = ttk.Label(cat_frame, text="Категория не выбрана", foreground="gray")
        self.cat_label.pack(padx=8, pady=2, anchor="w")
        self.cat_listbox.bind("<<ListboxSelect>>", self._on_cat_select)

        # ── Кнопки ──
        bf = ttk.Frame(self); bf.pack(pady=12)
        self.transfer_btn = ttk.Button(bf, text=f"Создать товар ({arrow})", command=self._do_transfer)
        self.transfer_btn.pack(side="left", padx=8)
        ttk.Button(bf, text="Отмена", command=self.destroy).pack(side="left", padx=8)

        self.status_lbl = ttk.Label(self, text="", foreground="gray")
        self.status_lbl.pack(pady=4)

        self._selected_cat_id: Optional[int] = None
        self._selected_type_id: Optional[int] = None

        # Авто-поиск категории по первым 2 словам названия
        if name:
            words = [w for w in name.split() if len(w) > 2]
            auto_query = " ".join(words[:2]) if words else name.split()[0]
            self.cat_search.insert(0, auto_query)
            self.after(300, self._search_categories)

    def _search_categories(self):
        query = self.cat_search.get().strip()
        self.status_lbl.config(text="Поиск категорий...", foreground="gray")
        threading.Thread(target=self._fetch_cats, args=(query,), daemon=True).start()

    def _load_all_categories(self):
        self.status_lbl.config(text="Загрузка всех категорий...", foreground="gray")
        threading.Thread(target=self._fetch_cats, args=("",), daemon=True).start()

    def _fetch_cats(self, query: str):
        try:
            if self.direction == "ozon_to_wb":
                raw = self.wb_client.get_subjects(query)
                flat = []
                for s in raw:
                    parent = s.get("parentName", "")
                    sname = s.get("name", "")
                    sid = s.get("id", 0)
                    display = f"{parent} → {sname} (ID {sid})" if parent else f"{sname} (ID {sid})"
                    flat.append((display, sid, 0))
                # Сортируем: сначала точные совпадения
                if query:
                    q = query.lower()
                    flat.sort(key=lambda x: (0 if q in x[0].lower().split("→")[-1].lower() else 1, x[0]))
            else:
                raw = self.ozon_client.get_categories(query)
                flat = self._flatten_ozon_cats(raw)
                # Client-side фильтрация для Ozon
                if query:
                    q = query.lower()
                    filtered = [t for t in flat if q in t[0].lower()]
                    if filtered:
                        flat = filtered
        except Exception as exc:
            flat = []
            self.after(0, lambda: self.status_lbl.config(text=f"Ошибка: {exc}", foreground="red"))
        self._flat_cats = flat
        self.after(0, self._fill_listbox)

    def _flatten_ozon_cats(self, nodes: List[Dict], prefix: str = "", parent_cat_id: int = 0) -> List[tuple]:
        """
        Структура дерева Ozon:
          category (description_category_id, category_name, children:[...])
            subcategory (description_category_id, category_name, children:[...])
              type (type_id, type_name, children:[])  ← листы, нет description_category_id
        """
        result = []
        for node in nodes:
            cat_id = node.get("description_category_id") or 0
            cat_name = node.get("category_name", "")
            type_id = node.get("type_id") or 0
            type_name = node.get("type_name", "")
            children = node.get("children") or []
            disabled = node.get("disabled", False)

            if disabled:
                continue

            if cat_name:
                # Узел-категория — рекурсируем, передавая свой cat_id вниз
                full_name = f"{prefix} → {cat_name}" if prefix else cat_name
                result.extend(self._flatten_ozon_cats(children, full_name, cat_id))
            elif type_name and type_id:
                # Листовой узел-тип — берём cat_id от родителя
                full_name = f"{prefix} → {type_name}" if prefix else type_name
                result.append((full_name, parent_cat_id, type_id))

        return result

    def _fill_listbox(self):
        self.cat_listbox.delete(0, "end")
        for display, _, _ in self._flat_cats:
            self.cat_listbox.insert("end", display)
        count = len(self._flat_cats)
        self.status_lbl.config(text=f"Найдено категорий: {count}", foreground="black")

    def _on_cat_select(self, _event=None):
        sel = self.cat_listbox.curselection()
        if not sel:
            return
        idx = sel[0]
        if idx < len(self._flat_cats):
            display, cat_id, type_id = self._flat_cats[idx]
            self._selected_cat_id = cat_id
            self._selected_type_id = type_id
            self.cat_label.config(text=f"Выбрано: {display}", foreground="green")

    def _do_transfer(self):
        if self._selected_cat_id is None:
            messagebox.showwarning("Перенос", "Выберите категорию на целевой площадке.", parent=self)
            return
        sku = self.e_sku.get().strip()
        name = self.e_name.get().strip()
        desc = self.t_desc.get("1.0", "end").strip()
        try:
            price = float(self.e_price.get().strip() or "0")
        except ValueError:
            price = 0.0
        imgs = [u.strip() for u in self.t_images.get("1.0", "end").splitlines() if u.strip()]

        # Для переноса на Ozon — показываем диалог обязательных атрибутов
        extra_attrs: List[Dict] = []
        if self.direction == "wb_to_ozon":
            dlg = OzonAttributesDialog(
                self, self.ozon_client,
                category_id=self._selected_cat_id,
                type_id=self._selected_type_id or 0,
                prefill={"Название": name, "Описание": desc},
                wb_card=self.wb_card,
            )
            if dlg.result is None:
                return  # пользователь нажал Отмена
            extra_attrs = dlg.result

        self.transfer_btn.config(state="disabled")
        self.status_lbl.config(text="Создаём товар...", foreground="gray")
        threading.Thread(target=self._transfer_thread,
                         args=(sku, name, desc, price, imgs, extra_attrs), daemon=True).start()

    @staticmethod
    def _extract_wb_dimensions(wb_card: Dict) -> Dict[str, int]:
        """Извлекает габариты и вес из WB карточки, возвращает в формате Ozon (мм, г)."""
        depth_mm = width_mm = height_mm = weight_g = 0

        # WB dimensions: {length, width, height} в сантиметрах, weightBrutto в кг
        dims = wb_card.get("dimensions") or {}
        if dims:
            raw_depth  = dims.get("length") or dims.get("depth") or 0
            raw_width  = dims.get("width") or 0
            raw_height = dims.get("height") or 0
            raw_weight = dims.get("weightBrutto") or dims.get("weight") or 0
            # WB хранит в см → Ozon требует мм
            depth_mm  = int(float(raw_depth)  * 10)
            width_mm  = int(float(raw_width)  * 10)
            height_mm = int(float(raw_height) * 10)
            # WB хранит вес в кг → Ozon требует г
            weight_g  = int(float(raw_weight) * 1000)

        # Дополнительно ищем в характеристиках карточки
        if not (depth_mm and width_mm and height_mm and weight_g):
            _DIM_KEYS = {
                "длина упаковки": "depth", "ширина упаковки": "width",
                "высота упаковки": "height", "глубина упаковки": "depth",
                "длина": "depth", "ширина": "width", "высота": "height",
            }
            _W_KEYS = {
                "вес с упаковкой", "вес брутто", "вес товара", "масса",
                "вес", "weight",
            }
            chars = wb_card.get("characteristics") or []
            for ch in chars:
                n = (ch.get("name") or "").lower().strip()
                vals = ch.get("value") or []
                if not vals:
                    continue
                raw = str(vals[0]).replace(",", ".").strip()
                try:
                    num = float("".join(c for c in raw if c in "0123456789."))
                except ValueError:
                    continue
                # Определяем единицу из значения (см, мм, кг, г)
                unit = ""
                for token in raw.lower().split():
                    if token in ("см", "cm"): unit = "cm"; break
                    if token in ("мм", "mm"): unit = "mm"; break
                    if token in ("кг", "kg"): unit = "kg"; break
                    if token in ("г",  "g"):  unit = "g";  break
                field = _DIM_KEYS.get(n)
                if field:
                    # Конвертируем в мм
                    if unit == "mm" or (not unit and num < 10):
                        val_mm = int(num)
                    elif unit == "cm" or (not unit and num < 300):
                        val_mm = int(num * 10)
                    else:
                        val_mm = int(num)  # уже мм
                    if field == "depth"  and not depth_mm:  depth_mm  = val_mm
                    if field == "width"  and not width_mm:  width_mm  = val_mm
                    if field == "height" and not height_mm: height_mm = val_mm
                elif n in _W_KEYS:
                    if unit == "kg" or (not unit and num < 50):
                        val_g = int(num * 1000)
                    else:
                        val_g = int(num)
                    if not weight_g:
                        weight_g = val_g

        result = {
            "depth":  depth_mm  or 100,
            "width":  width_mm  or 100,
            "height": height_mm or 100,
            "weight": weight_g  or 100,
        }
        logging.info(f"[wb_dimensions] depth={result['depth']}мм width={result['width']}мм "
                     f"height={result['height']}мм weight={result['weight']}г")
        return result

    def _transfer_thread(self, sku, name, desc, price, imgs, extra_attrs=None):
        try:
            if self.direction == "ozon_to_wb":
                result = self.wb_client.create_card(
                    subject_id=self._selected_cat_id,
                    vendor_code=sku, name=name,
                    description=desc, price=int(price), images=imgs
                )
                msg = f"Товар создан на WB. Ответ: {str(result)[:200]}"
            else:
                dims = self._extract_wb_dimensions(self.wb_card)
                logging.info(
                    f"[transfer→Ozon] offer_id={sku!r} cat={self._selected_cat_id} "
                    f"type={self._selected_type_id} price={price} "
                    f"imgs={len(imgs)} attrs={len(extra_attrs or [])}"
                )
                result = self.ozon_client.create_product(
                    offer_id=sku, name=name, description=desc,
                    category_id=self._selected_cat_id,
                    type_id=self._selected_type_id or 0,
                    price=str(int(price)), images=imgs,
                    attributes=extra_attrs or [],
                    weight=dims["weight"], depth=dims["depth"],
                    width=dims["width"], height=dims["height"],
                )
                logging.info(f"[transfer→Ozon] result: {result}")
                res_inner = result.get("result", result)
                product_id = res_inner.get("product_id") or ""
                status = res_inner.get("status", "submitted")
                msg = f"Товар отправлен на Ozon (offer_id={sku}"
                if product_id:
                    msg += f", product_id={product_id}"
                msg += f", статус: {status})\n\nПроверьте личный кабинет Ozon через 1-2 минуты."
            self.result = "ok"
            msg_copy = msg
            self.after(0, lambda m=msg_copy: (
                self.status_lbl.config(text=m.split("\n")[0], foreground="green"),
                self.transfer_btn.config(state="normal"),
                messagebox.showinfo("Перенос", m, parent=self)
            ))
        except Exception as exc:
            err = str(exc)
            logging.error(f"[transfer→Ozon] ОШИБКА: {err}")
            self.after(0, lambda e=err: (
                self.status_lbl.config(text=f"Ошибка: {e}", foreground="red"),
                self.transfer_btn.config(state="normal"),
                messagebox.showerror("Ошибка переноса", e, parent=self)
            ))


class AdsCampaignSettingsDialog(tk.Toplevel):
    """Диалог задания целевой ДРР и включения авторегулировки для одной рекламной кампании WB."""

    def __init__(self, parent, campaign: Dict[str, Any], current: Dict[str, Any]):
        super().__init__(parent)
        self.title(f"Настройка кампании — {campaign.get('name', '')}")
        self.geometry("420x220")
        self.resizable(False, False)
        self.transient(parent)
        self.grab_set()

        self.result: Optional[Dict[str, Any]] = None

        info = (f"{campaign.get('name', '')}\n"
               f"ID: {campaign.get('advertId')}   Текущая ставка: {campaign.get('_cpm', '?')}")
        ttk.Label(self, text=info, wraplength=380, justify="left").pack(padx=14, pady=(14, 8), anchor="w")

        rf = ttk.Frame(self); rf.pack(padx=14, pady=6, fill="x")
        ttk.Label(rf, text="Целевая ДРР, %:").pack(side="left")
        self.target_var = tk.StringVar(value=str(current.get("target_drr", "") or ""))
        ttk.Entry(rf, textvariable=self.target_var, width=10).pack(side="left", padx=8)

        self.enabled_var = tk.BooleanVar(value=bool(current.get("enabled")))
        ttk.Checkbutton(self, text="Включить авторегулировку ставки для этой кампании",
                       variable=self.enabled_var).pack(padx=14, pady=8, anchor="w")

        hint = ttk.Label(self, text="ДРР = расход на рекламу / выручка от заказов × 100%.\n"
                                    "Если фактическая ДРР выше цели — ставка снижается, если ниже — повышается.",
                         foreground="gray", wraplength=380, justify="left")
        hint.pack(padx=14, pady=(0, 8), anchor="w")

        bf = ttk.Frame(self); bf.pack(pady=10)
        ttk.Button(bf, text="Сохранить", command=self._save).pack(side="left", padx=6)
        ttk.Button(bf, text="Отмена", command=self.destroy).pack(side="left", padx=6)

        self.protocol("WM_DELETE_WINDOW", self.destroy)
        self.wait_window()

    def _save(self):
        raw = self.target_var.get().strip().replace(",", ".")
        target_drr = None
        if raw:
            try:
                target_drr = float(raw)
            except ValueError:
                messagebox.showerror("Ошибка", "Целевая ДРР должна быть числом.", parent=self)
                return
        if self.enabled_var.get() and not target_drr:
            messagebox.showerror("Ошибка", "Укажите целевую ДРР, чтобы включить авторегулировку.", parent=self)
            return
        self.result = {"target_drr": target_drr, "enabled": self.enabled_var.get()}
        self.destroy()


class OzonAdsProductSettingsDialog(tk.Toplevel):
    """Диалог задания наценки над конкурентной ставкой и включения авторегулировки
    для одного товара в рекламной кампании Ozon."""

    def __init__(self, parent, campaign: Dict[str, Any], sku: str,
                 competitive_bid: Optional[float], current: Dict[str, Any]):
        super().__init__(parent)
        self.title(f"Настройка товара — {sku}")
        self.geometry("420x240")
        self.resizable(False, False)
        self.transient(parent)
        self.grab_set()

        self.result: Optional[Dict[str, Any]] = None
        is_manual = _ozon_campaign_bid_editable(campaign)

        bid_line = f"{competitive_bid:.2f}₽" if competitive_bid is not None else "нет данных"
        info = f"Кампания: {campaign.get('title', '')}\nSKU: {sku}\nКонкурентная ставка: {bid_line}"
        ttk.Label(self, text=info, wraplength=380, justify="left").pack(padx=14, pady=(14, 8), anchor="w")

        if not is_manual:
            reason = ("включён автопилот Ozon — вручную заданная ставка игнорируется"
                     if campaign.get("autopilot")
                     else "это не обычная товарная CPC-кампания (баннер или «Оплата за заказ») — "
                          "ставка для неё настраивается иначе")
            warn = ttk.Label(self, text=f"Авторегулировка недоступна: {reason}.",
                             foreground="#cc6600", wraplength=380, justify="left")
            warn.pack(padx=14, pady=(0, 8), anchor="w")

        rf = ttk.Frame(self); rf.pack(padx=14, pady=6, fill="x")
        ttk.Label(rf, text="Наценка над конкурентом, %:").pack(side="left")
        self.margin_var = tk.StringVar(value=str(current.get("margin_pct", "") or "10"))
        ttk.Entry(rf, textvariable=self.margin_var, width=10,
                 state="normal" if is_manual else "disabled").pack(side="left", padx=8)

        self.enabled_var = tk.BooleanVar(value=bool(current.get("enabled")) and is_manual)
        ttk.Checkbutton(self, text="Включить авторегулировку ставки для этого товара",
                       variable=self.enabled_var,
                       state="normal" if is_manual else "disabled").pack(padx=14, pady=8, anchor="w")

        hint = ttk.Label(self, text="Новая ставка = конкурентная ставка × (1 + наценка/100).\n"
                                    "Применяется как ставка CPC (за клик) в кампании.",
                         foreground="gray", wraplength=380, justify="left")
        hint.pack(padx=14, pady=(0, 8), anchor="w")

        bf = ttk.Frame(self); bf.pack(pady=10)
        ttk.Button(bf, text="Сохранить", command=self._save, state="normal" if is_manual else "disabled").pack(side="left", padx=6)
        ttk.Button(bf, text="Отмена", command=self.destroy).pack(side="left", padx=6)

        self.protocol("WM_DELETE_WINDOW", self.destroy)
        self.wait_window()

    def _save(self):
        raw = self.margin_var.get().strip().replace(",", ".")
        margin_pct = 10.0
        if raw:
            try:
                margin_pct = float(raw)
            except ValueError:
                messagebox.showerror("Ошибка", "Наценка должна быть числом.", parent=self)
                return
        self.result = {"margin_pct": margin_pct, "enabled": self.enabled_var.get()}
        self.destroy()


class PreviewDialog(tk.Toplevel):
    """Модальный диалог для сравнения старого и нового описания."""

    def __init__(self, parent, offer_id: str, product_name: str,
                 old_description: str, new_description: str,
                 old_title: Optional[str] = None, new_title: Optional[str] = None):
        super().__init__(parent)
        self.title(f"Предпросмотр — {offer_id}")
        self.geometry("1000x680" if new_title is not None else "1000x620")
        self.resizable(True, True)
        self.grab_set()  # модальный

        self.result: Optional[str] = None  # "apply" | "skip" | "apply_all" | "skip_all"

        # Заголовок
        ttk.Label(self, text=product_name, font=("", 11, "bold"), wraplength=960).pack(
            padx=12, pady=(10, 4), anchor="w"
        )
        ttk.Label(self, text=f"offer_id: {offer_id}", foreground="gray").pack(
            padx=12, anchor="w"
        )

        # Сравнение названия (только если генератор менял и название, например для WB)
        if new_title is not None:
            title_frame = ttk.LabelFrame(self, text="  Название  ")
            title_frame.pack(fill="x", padx=12, pady=(8, 0))
            title_old_len = len(old_title or "")
            title_new_len = len(new_title)
            tk.Label(title_frame, text=f"Было ({title_old_len}): {old_title or '(не задано)'}",
                    bg="#2d2d2d", fg="#ff9999", wraplength=940, justify="left", anchor="w").pack(
                fill="x", padx=4, pady=(4, 2))
            tk.Label(title_frame, text=f"Стало ({title_new_len}/{WB_TITLE_MAX}): {new_title}",
                    bg="#1e2d1e", fg="#99ff99", wraplength=940, justify="left", anchor="w").pack(
                fill="x", padx=4, pady=(0, 4))

        # Панель сравнения
        panes = ttk.PanedWindow(self, orient="horizontal")
        panes.pack(fill="both", expand=True, padx=12, pady=8)

        # --- БЫЛО ---
        left = ttk.LabelFrame(panes, text="  БЫЛО  ")
        panes.add(left, weight=1)
        old_text = scrolledtext.ScrolledText(left, wrap="word", font=("", 9),
                                              bg="#2d2d2d", fg="#ff9999", state="normal")
        old_text.insert("1.0", old_description if old_description else "(описание отсутствует)")
        old_text.config(state="disabled")
        old_text.pack(fill="both", expand=True, padx=4, pady=4)

        # --- СТАЛО ---
        right = ttk.LabelFrame(panes, text="  СТАЛО  ")
        panes.add(right, weight=1)
        new_text = scrolledtext.ScrolledText(right, wrap="word", font=("", 9),
                                              bg="#1e2d1e", fg="#99ff99", state="normal")
        new_text.insert("1.0", new_description)
        new_text.config(state="disabled")
        new_text.pack(fill="both", expand=True, padx=4, pady=4)

        # Счётчики символов
        info_frame = ttk.Frame(self)
        info_frame.pack(fill="x", padx=12)
        old_len = len(old_description)
        new_len = len(new_description)
        diff = new_len - old_len
        diff_str = f"+{diff}" if diff >= 0 else str(diff)
        ttk.Label(info_frame, text=f"Было: {old_len} симв.", foreground="gray").pack(side="left", padx=4)
        ttk.Label(info_frame, text=f"Стало: {new_len} симв.", foreground="gray").pack(side="left", padx=4)
        ttk.Label(info_frame, text=f"Разница: {diff_str}", foreground="#aaaaaa").pack(side="left", padx=4)

        # Кнопки
        btn_frame = ttk.Frame(self)
        btn_frame.pack(pady=10)
        ttk.Button(btn_frame, text="Применить", width=14,
                   command=lambda: self._close("apply")).pack(side="left", padx=6)
        ttk.Button(btn_frame, text="Пропустить", width=14,
                   command=lambda: self._close("skip")).pack(side="left", padx=6)
        ttk.Separator(btn_frame, orient="vertical").pack(side="left", fill="y", padx=10)
        ttk.Button(btn_frame, text="Применить все", width=14,
                   command=lambda: self._close("apply_all")).pack(side="left", padx=6)
        ttk.Button(btn_frame, text="Пропустить все", width=14,
                   command=lambda: self._close("skip_all")).pack(side="left", padx=6)

        self.protocol("WM_DELETE_WINDOW", lambda: self._close("skip"))
        self.wait_window()

    def _close(self, result: str):
        self.result = result
        self.destroy()


# ============================================================================
# MARKETPLACE MANAGER — GUI
# ============================================================================

class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("Marketplace Manager")
        self.geometry("1100x750")
        self.minsize(900, 620)
        self.resizable(True, True)

        self.config_data = load_config()
        self._running = False
        self._stop_event = threading.Event()
        self._wb_stop_event = threading.Event()
        self._prices_stop_event = threading.Event()
        self._ads_stop_event = threading.Event()
        self._ads_campaigns: List[Dict[str, Any]] = []
        self._ozon_ads_stop_event = threading.Event()
        self._ozon_ads_products: List[Dict[str, Any]] = []
        self._log_queue: queue.Queue = queue.Queue()

        self._setup_logging()
        self._build_ui()
        self._load_config_to_ui()
        self._poll_log_queue()

    def _setup_logging(self):
        _log_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "marketplace_manager.log")
        handler = logging.FileHandler(_log_path, encoding="utf-8")
        handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
        logging.getLogger().addHandler(handler)
        logging.getLogger().setLevel(logging.INFO)

    # ─────────────────────────── ПОСТРОЕНИЕ UI ───────────────────────────

    def _build_ui(self):
        nb = ttk.Notebook(self)
        nb.pack(fill="both", expand=True, padx=8, pady=8)

        # Настройки
        sf = ttk.Frame(nb); nb.add(sf, text="  Настройки  ")
        self._build_settings_tab(sf)

        # Ozon (вложенные вкладки)
        of = ttk.Frame(nb); nb.add(of, text="  Ozon  ")
        self._build_ozon_section(of)

        # Wildberries (вложенные вкладки)
        wf = ttk.Frame(nb); nb.add(wf, text="  Wildberries  ")
        self._build_wb_section(wf)

        # Общий лог
        lf = ttk.Frame(nb); nb.add(lf, text="  Общий лог  ")
        self._build_log_tab(lf)

    # ────────────────────────── НАСТРОЙКИ ──────────────────────────────

    def _build_settings_tab(self, parent: ttk.Frame):
        canvas = tk.Canvas(parent, highlightthickness=0)
        vsb = ttk.Scrollbar(parent, orient="vertical", command=canvas.yview)
        canvas.configure(yscrollcommand=vsb.set)
        vsb.pack(side="right", fill="y")
        canvas.pack(side="left", fill="both", expand=True)
        inner = ttk.Frame(canvas)
        inner_win = canvas.create_window((0, 0), window=inner, anchor="nw")
        def _resize(e): canvas.configure(scrollregion=canvas.bbox("all")); canvas.itemconfig(inner_win, width=e.width)
        canvas.bind("<Configure>", _resize)
        inner.columnconfigure(1, weight=1)

        def sep(r):
            ttk.Separator(inner, orient="horizontal").grid(row=r, column=0, columnspan=2, sticky="ew", pady=8, padx=10)

        def lbl_entry(r, text, show=""):
            ttk.Label(inner, text=text).grid(row=r, column=0, sticky="w", padx=10, pady=5)
            e = ttk.Entry(inner, width=52, show=show)
            e.grid(row=r, column=1, sticky="ew", padx=10, pady=5)
            return e

        row = 0
        ttk.Label(inner, text="Ozon Seller API", font=("", 10, "bold")).grid(row=row, column=0, columnspan=2, sticky="w", padx=10, pady=(10, 2))
        row += 1; self.ozon_client_id = lbl_entry(row, "Client ID:")
        row += 1; self.ozon_api_key   = lbl_entry(row, "API Key:", show="*")
        row += 1
        bf_ozon = ttk.Frame(inner); bf_ozon.grid(row=row, column=1, sticky="w", padx=10, pady=2)
        ttk.Button(bf_ozon, text="✓ Проверить Ozon API", command=self._check_ozon).pack(side="left")
        row += 1; sep(row)

        row += 1
        ttk.Label(inner, text="Wildberries API", font=("", 10, "bold")).grid(row=row, column=0, columnspan=2, sticky="w", padx=10, pady=(4, 2))
        row += 1; self.wb_api_key = lbl_entry(row, "WB API Token:", show="*")
        ttk.Label(inner, text="seller.wildberries.ru → Настройки → Доступ к API", foreground="gray").grid(row=row+1, column=1, sticky="w", padx=10)
        row += 1
        bf_wb = ttk.Frame(inner); bf_wb.grid(row=row+1, column=1, sticky="w", padx=10, pady=2)
        ttk.Button(bf_wb, text="✓ Проверить WB API", command=self._check_wb).pack(side="left")
        row += 2; sep(row)

        row += 1
        ttk.Label(inner, text="MPStats API", font=("", 10, "bold")).grid(row=row, column=0, columnspan=2, sticky="w", padx=10, pady=(4, 2))
        row += 1; self.mpstats_token = lbl_entry(row, "MPStats Token:", show="*")
        ttk.Label(inner, text="mpstats.io → Настройки аккаунта → API токен", foreground="gray").grid(row=row+1, column=1, sticky="w", padx=10)
        row += 1
        bf = ttk.Frame(inner); bf.grid(row=row+1, column=1, sticky="w", padx=10, pady=2)
        ttk.Button(bf, text="Проверить подключение MPStats", command=self._check_mpstats).pack(side="left")
        row += 2; sep(row)

        row += 1
        ttk.Label(inner, text="Генератор описаний (AI)", font=("", 10, "bold")).grid(row=row, column=0, columnspan=2, sticky="w", padx=10, pady=(4, 2))
        row += 1
        ttk.Label(inner, text="Провайдер:").grid(row=row, column=0, sticky="w", padx=10, pady=5)
        self.ai_provider = tk.StringVar(value="template")
        pf = ttk.Frame(inner); pf.grid(row=row, column=1, sticky="w", padx=10)
        ttk.Radiobutton(pf, text="Claude API", variable=self.ai_provider, value="claude", command=self._on_provider_change).pack(side="left", padx=(0,8))
        ttk.Radiobutton(pf, text="Gemini (бесплатно)", variable=self.ai_provider, value="gemini", command=self._on_provider_change).pack(side="left", padx=(0,8))
        ttk.Radiobutton(pf, text="Шаблон (без API)", variable=self.ai_provider, value="template", command=self._on_provider_change).pack(side="left")
        row += 1; self._claude_key_label = ttk.Label(inner, text="Anthropic API Key:")
        self._claude_key_label.grid(row=row, column=0, sticky="w", padx=10, pady=5)
        self.anthropic_api_key = ttk.Entry(inner, width=52, show="*")
        self.anthropic_api_key.grid(row=row, column=1, sticky="ew", padx=10, pady=5)
        row += 1; self._claude_model_label = ttk.Label(inner, text="Модель Claude:")
        self._claude_model_label.grid(row=row, column=0, sticky="w", padx=10, pady=5)
        self.claude_model = ttk.Combobox(inner, width=50, values=["claude-opus-4-8", "claude-sonnet-4-6", "claude-haiku-4-5"])
        self.claude_model.grid(row=row, column=1, sticky="ew", padx=10, pady=5)
        row += 1; self._claude_check_frame = ttk.Frame(inner)
        self._claude_check_frame.grid(row=row, column=1, sticky="w", padx=10, pady=2)
        ttk.Button(self._claude_check_frame, text="✓ Проверить Claude API", command=self._check_claude).pack(side="left")
        row += 1; self._gemini_key_label = ttk.Label(inner, text="Gemini API Key:")
        self._gemini_key_label.grid(row=row, column=0, sticky="w", padx=10, pady=5)
        self.gemini_api_key = ttk.Entry(inner, width=52, show="*")
        self.gemini_api_key.grid(row=row, column=1, sticky="ew", padx=10, pady=5)
        row += 1; self._gemini_model_label = ttk.Label(inner, text="Модель Gemini:")
        self._gemini_model_label.grid(row=row, column=0, sticky="w", padx=10, pady=5)
        self.gemini_model = ttk.Combobox(inner, width=50, values=["gemini-2.0-flash", "gemini-2.5-flash", "gemini-2.5-pro"])
        self.gemini_model.grid(row=row, column=1, sticky="ew", padx=10, pady=5)
        row += 1; self._gemini_check_frame = ttk.Frame(inner)
        self._gemini_check_frame.grid(row=row, column=1, sticky="w", padx=10, pady=2)
        ttk.Button(self._gemini_check_frame, text="✓ Проверить Gemini API", command=self._check_gemini).pack(side="left")
        self._gemini_hint_label = ttk.Label(inner, text="Ключ: aistudio.google.com → Get API key (бесплатно)", foreground="gray")
        self._gemini_hint_label.grid(row=row + 1, column=1, sticky="w", padx=10)
        self._template_info_label = ttk.Label(inner, text="Описание по шаблону — API не требуется.", foreground="gray")
        self._template_info_label.grid(row=row, column=1, sticky="w", padx=10, pady=5)
        self._template_info_label.grid_remove()
        row += 1; sep(row)

        row += 1
        ttk.Label(inner, text="Ozon Performance API (ключевые слова)", font=("", 10, "bold")).grid(row=row, column=0, columnspan=2, sticky="w", padx=10, pady=(4, 2))
        row += 1
        self.perf_enabled = tk.BooleanVar(value=False)
        ttk.Checkbutton(inner, text="Включить Ozon Performance API", variable=self.perf_enabled, command=self._on_perf_toggle).grid(row=row, column=0, columnspan=2, sticky="w", padx=10)
        row += 1; self._perf_id_label = ttk.Label(inner, text="Performance Client ID:")
        self._perf_id_label.grid(row=row, column=0, sticky="w", padx=10, pady=4)
        self.perf_client_id = ttk.Entry(inner, width=52)
        self.perf_client_id.grid(row=row, column=1, sticky="ew", padx=10, pady=4)
        row += 1; self._perf_secret_label = ttk.Label(inner, text="Performance Client Secret:")
        self._perf_secret_label.grid(row=row, column=0, sticky="w", padx=10, pady=4)
        self.perf_client_secret = ttk.Entry(inner, width=52, show="*")
        self.perf_client_secret.grid(row=row, column=1, sticky="ew", padx=10, pady=4)
        row += 1; self._perf_btn_frame = ttk.Frame(inner)
        self._perf_btn_frame.grid(row=row, column=1, sticky="w", padx=10, pady=2)
        ttk.Button(self._perf_btn_frame, text="Проверить Performance API", command=self._check_performance).pack(side="left")
        for w in (self._perf_id_label, self.perf_client_id, self._perf_secret_label, self.perf_client_secret, self._perf_btn_frame):
            w.grid_remove()
        row += 1; sep(row)

        row += 1
        ttk.Label(inner, text="Параметры обновления", font=("", 10, "bold")).grid(row=row, column=0, columnspan=2, sticky="w", padx=10, pady=(4, 2))
        row += 1
        ttk.Label(inner, text="Дней назад (аналитика):").grid(row=row, column=0, sticky="w", padx=10, pady=5)
        self.days_back = ttk.Spinbox(inner, from_=1, to=365, width=10); self.days_back.grid(row=row, column=1, sticky="w", padx=10, pady=5)
        row += 1
        ttk.Label(inner, text="Топ ключевых слов:").grid(row=row, column=0, sticky="w", padx=10, pady=5)
        self.top_keywords = ttk.Spinbox(inner, from_=5, to=50, width=10); self.top_keywords.grid(row=row, column=1, sticky="w", padx=10, pady=5)
        row += 1
        ttk.Label(inner, text="Целевые offer_id Ozon\n(через запятую, пусто=все):").grid(row=row, column=0, sticky="nw", padx=10, pady=5)
        self.target_ids = ttk.Entry(inner, width=52); self.target_ids.grid(row=row, column=1, sticky="ew", padx=10, pady=5)
        row += 1
        bf2 = ttk.Frame(inner); bf2.grid(row=row, column=0, columnspan=2, pady=14)
        ttk.Button(bf2, text="Сохранить настройки", command=self._save_settings).pack(side="left", padx=6)
        ttk.Button(bf2, text="Показать/скрыть ключи", command=self._toggle_keys).pack(side="left", padx=6)

    # ─────────────────────── OZON — ВЛОЖЕННЫЕ ВКЛАДКИ ───────────────────

    def _build_ozon_section(self, parent: ttk.Frame):
        nb = ttk.Notebook(parent)
        nb.pack(fill="both", expand=True)
        df = ttk.Frame(nb); nb.add(df, text="  Описание  ")
        self._build_ozon_desc_tab(df)
        pf = ttk.Frame(nb); nb.add(pf, text="  Управление ценами  ")
        self._build_prices_tab(pf, marketplace="ozon")
        tf = ttk.Frame(nb); nb.add(tf, text="  Товары  ")
        self._build_products_tab(tf, marketplace="ozon")
        af = ttk.Frame(nb); nb.add(af, text="  Реклама  ")
        self._build_ozon_ads_tab(af)

    def _build_ozon_desc_tab(self, parent: ttk.Frame):
        ttk.Label(parent, text="Обновление описаний товаров Ozon", font=("", 10, "bold")).pack(padx=16, pady=(12, 4), anchor="w")

        gf = ttk.LabelFrame(parent, text="Генератор описаний Ozon")
        gf.pack(fill="x", padx=16, pady=4)
        self.ozon_ai_provider = tk.StringVar(value="settings")
        ttk.Radiobutton(gf, text="Из настроек", variable=self.ozon_ai_provider, value="settings").pack(side="left", padx=10, pady=6)
        ttk.Radiobutton(gf, text="Gemini (бесплатно)", variable=self.ozon_ai_provider, value="gemini").pack(side="left", padx=10, pady=6)
        ttk.Radiobutton(gf, text="Claude API", variable=self.ozon_ai_provider, value="claude").pack(side="left", padx=10, pady=6)
        ttk.Radiobutton(gf, text="Шаблон (без API)", variable=self.ozon_ai_provider, value="template").pack(side="left", padx=10, pady=6)

        self.progress_var = tk.DoubleVar()
        ttk.Progressbar(parent, variable=self.progress_var, maximum=100).pack(fill="x", padx=16, pady=4)
        self.status_label = ttk.Label(parent, text="Готов к запуску")
        self.status_label.pack(padx=16)

        self.preview_mode = tk.BooleanVar(value=True)
        ttk.Checkbutton(parent, text="Предпросмотр 'Было/Стало' перед каждым обновлением", variable=self.preview_mode).pack(padx=16, anchor="w")

        br = ttk.Frame(parent); br.pack(pady=10)
        self.start_btn = ttk.Button(br, text="Запустить обновление описаний", command=self._start_update)
        self.start_btn.pack(side="left", padx=6)
        self.stop_btn = ttk.Button(br, text="Остановить", command=self._stop_update, state="disabled")
        self.stop_btn.pack(side="left", padx=6)

        sf = ttk.LabelFrame(parent, text="Расписание (Ozon)")
        sf.pack(fill="x", padx=16, pady=8)
        ttk.Label(sf, text="Каждый понедельник в:").grid(row=0, column=0, padx=8, pady=6, sticky="w")
        self.sched_time = ttk.Entry(sf, width=8); self.sched_time.insert(0, "03:00")
        self.sched_time.grid(row=0, column=1, padx=8, pady=6, sticky="w")
        self.sched_btn = ttk.Button(sf, text="Включить расписание", command=self._start_scheduler)
        self.sched_btn.grid(row=0, column=2, padx=8, pady=6)

        lf = ttk.LabelFrame(parent, text="Журнал Ozon")
        lf.pack(fill="both", expand=True, padx=16, pady=6)
        self.ozon_log_text = scrolledtext.ScrolledText(lf, state="disabled", wrap="word",
                                                        font=("Consolas", 9), bg="#1e1e1e", fg="#d4d4d4")
        self.ozon_log_text.pack(fill="both", expand=True, padx=4, pady=4)
        ttk.Button(lf, text="Очистить", command=lambda: self._clear_tab_log(self.ozon_log_text)).pack(anchor="w", padx=4, pady=2)

    # ─────────────────────── WB — ВЛОЖЕННЫЕ ВКЛАДКИ ─────────────────────

    def _build_wb_section(self, parent: ttk.Frame):
        nb = ttk.Notebook(parent)
        nb.pack(fill="both", expand=True)
        df = ttk.Frame(nb); nb.add(df, text="  Описание  ")
        self._build_wb_desc_tab(df)
        pf = ttk.Frame(nb); nb.add(pf, text="  Управление ценами  ")
        self._build_prices_tab(pf, marketplace="wb")
        tf = ttk.Frame(nb); nb.add(tf, text="  Товары  ")
        self._build_products_tab(tf, marketplace="wb")
        af = ttk.Frame(nb); nb.add(af, text="  Реклама  ")
        self._build_wb_ads_tab(af)

    def _build_wb_desc_tab(self, parent: ttk.Frame):
        ttk.Label(parent, text="Обновление описаний товаров Wildberries", font=("", 10, "bold")).pack(padx=16, pady=(12, 4), anchor="w")

        gf = ttk.LabelFrame(parent, text="Генератор описаний WB")
        gf.pack(fill="x", padx=16, pady=6)
        self.wb_ai_provider = tk.StringVar(value="template")
        ttk.Radiobutton(gf, text="Шаблон WB (без API)", variable=self.wb_ai_provider, value="template").pack(side="left", padx=10, pady=8)
        ttk.Radiobutton(gf, text="Gemini (бесплатно)", variable=self.wb_ai_provider, value="gemini").pack(side="left", padx=10, pady=8)
        ttk.Radiobutton(gf, text="Claude API", variable=self.wb_ai_provider, value="claude").pack(side="left", padx=10, pady=8)

        self.wb_progress_var = tk.DoubleVar()
        ttk.Progressbar(parent, variable=self.wb_progress_var, maximum=100).pack(fill="x", padx=16, pady=4)
        self.wb_status_label = ttk.Label(parent, text="Готов к запуску")
        self.wb_status_label.pack(padx=16)

        self.wb_preview_mode = tk.BooleanVar(value=True)
        ttk.Checkbutton(parent, text="Предпросмотр 'Было/Стало' перед каждым обновлением", variable=self.wb_preview_mode).pack(padx=16, anchor="w")

        br = ttk.Frame(parent); br.pack(pady=10)
        self.wb_start_btn = ttk.Button(br, text="Запустить обновление описаний WB", command=self._start_wb_update)
        self.wb_start_btn.pack(side="left", padx=6)
        self.wb_stop_btn = ttk.Button(br, text="Остановить", command=self._stop_wb_update, state="disabled")
        self.wb_stop_btn.pack(side="left", padx=6)

        lf = ttk.LabelFrame(parent, text="Журнал WB")
        lf.pack(fill="both", expand=True, padx=16, pady=6)
        self.wb_log_text = scrolledtext.ScrolledText(lf, state="disabled", wrap="word",
                                                     font=("Consolas", 9), bg="#1e1e1e", fg="#d4d4d4")
        self.wb_log_text.pack(fill="both", expand=True, padx=4, pady=4)
        ttk.Button(lf, text="Очистить", command=lambda: self._clear_tab_log(self.wb_log_text)).pack(anchor="w", padx=4, pady=2)

    # ─────────────────────── УПРАВЛЕНИЕ ЦЕНАМИ ──────────────────────────

    def _build_prices_tab(self, parent: ttk.Frame, marketplace: str):
        """Единый UI для вкладки цен Ozon и WB."""
        is_ozon = marketplace == "ozon"
        title = "Сравнение и синхронизация цен Ozon ↔ Wildberries"
        ttk.Label(parent, text=title, font=("", 10, "bold")).pack(padx=16, pady=(12, 2), anchor="w")
        hint = ("Цены сопоставляются по совпадению артикула (offer_id Ozon = vendorCode WB).\n"
                "WB — цена покупателя с учётом СПП (wallet_price). MPStats — средняя цена покупателя по данной категории ваших товаров.")
        ttk.Label(parent, text=hint, foreground="gray", wraplength=900).pack(padx=16, anchor="w")

        # Кнопки управления
        bf = ttk.Frame(parent); bf.pack(padx=16, pady=8, anchor="w")
        ttk.Button(bf, text="Загрузить и сравнить", command=self._load_prices).pack(side="left", padx=4)
        ttk.Button(bf, text="WB цены через MPStats (если 429)", command=self._load_wb_prices_via_mpstats,
                   style="Accent.TButton" if False else "TButton").pack(side="left", padx=4)
        if is_ozon:
            ttk.Button(bf, text="Выровнять Ozon → по WB", command=lambda: self._sync_prices("ozon_from_wb")).pack(side="left", padx=4)
            ttk.Button(bf, text="Выровнять Ozon → по MPStats", command=lambda: self._sync_prices("ozon_from_mpstats")).pack(side="left", padx=4)
        else:
            ttk.Button(bf, text="Выровнять WB → по Ozon", command=lambda: self._sync_prices("wb_from_ozon")).pack(side="left", padx=4)
            ttk.Button(bf, text="Выровнять WB → по MPStats", command=lambda: self._sync_prices("wb_from_mpstats")).pack(side="left", padx=4)
        ttk.Button(bf, text="Выровнять обе → по Ozon", command=lambda: self._sync_prices("both_from_ozon")).pack(side="left", padx=4) if is_ozon else None

        # Таблица цен — одна общая для обоих вкладок (через атрибут)
        cols = ("offer_id", "name", "ozon_price", "wb_price", "mpstats_price", "diff_pct", "status")
        heads = ("Артикул", "Название", "Ozon, ₽", "WB, ₽", "MPStats, ₽", "Разница, %", "Статус")
        widths = (130, 280, 80, 80, 90, 80, 100)

        tree_frame = ttk.Frame(parent)
        tree_frame.pack(fill="both", expand=True, padx=16, pady=4)

        vsb = ttk.Scrollbar(tree_frame, orient="vertical")
        hsb = ttk.Scrollbar(tree_frame, orient="horizontal")
        tree = ttk.Treeview(tree_frame, columns=cols, show="headings",
                            yscrollcommand=vsb.set, xscrollcommand=hsb.set, height=18)
        vsb.config(command=tree.yview); hsb.config(command=tree.xview)
        vsb.pack(side="right", fill="y"); hsb.pack(side="bottom", fill="x")
        tree.pack(fill="both", expand=True)

        for col, head, w in zip(cols, heads, widths):
            tree.heading(col, text=head, command=lambda c=col: self._sort_prices_tree(tree, c))
            tree.column(col, width=w, minwidth=50, anchor="center")
        tree.column("name", anchor="w")

        tree.tag_configure("equal", foreground="#4caf50")
        tree.tag_configure("diff", foreground="#ff9800")
        tree.tag_configure("missing", foreground="#888888")

        if is_ozon:
            self.prices_tree_ozon = tree
        else:
            self.prices_tree_wb = tree

        # Прогресс-бар и строка статуса
        pb = ttk.Progressbar(parent, mode="indeterminate", length=400)
        pb.pack(padx=16, pady=(4, 0), anchor="w")
        sl = ttk.Label(parent, text="Нажмите 'Загрузить и сравнить'", foreground="gray")
        sl.pack(padx=16, pady=(2, 4), anchor="w")
        if is_ozon:
            self.prices_status_ozon = sl
            self.prices_pb_ozon = pb
        else:
            self.prices_status_wb = sl
            self.prices_pb_wb = pb

    # ───────────────────────── ВКЛАДКА ТОВАРЫ ───────────────────────────

    def _build_products_tab(self, parent: ttk.Frame, marketplace: str):
        is_ozon = marketplace == "ozon"
        title = "Товары Ozon — сравнение с Wildberries" if is_ozon else "Товары Wildberries — сравнение с Ozon"
        ttk.Label(parent, text=title, font=("", 10, "bold")).pack(padx=16, pady=(12, 2), anchor="w")
        hint = ("Сопоставление по артикулу: offer_id (Ozon) = vendorCode (WB).\n"
                "Зелёный — товар есть на обеих площадках, оранжевый — только на одной.")
        ttk.Label(parent, text=hint, foreground="gray", wraplength=900).pack(padx=16, anchor="w")

        bf = ttk.Frame(parent); bf.pack(padx=16, pady=8, anchor="w")
        ttk.Button(bf, text="Загрузить товары", command=self._load_products).pack(side="left", padx=4)
        ttk.Button(bf, text="Копировать артикулы только здесь",
                   command=lambda mp=marketplace: self._copy_missing_skus(mp)).pack(side="left", padx=4)
        ttk.Button(bf, text="Экспорт в CSV",
                   command=lambda mp=marketplace: self._export_products_csv(mp)).pack(side="left", padx=4)
        if not is_ozon:
            ttk.Button(bf, text="Скопировать название и описание Ozon → WB",
                       command=self._copy_ozon_name_desc_to_wb).pack(side="left", padx=4)

        hint2 = ttk.Label(parent,
                          text="Двойной клик по строке с '—' → открыть диалог переноса товара на другую площадку",
                          foreground="#1976d2")
        hint2.pack(padx=16, anchor="w")

        cols = ("sku", "name", "on_ozon", "on_wb", "status")
        heads = ("Артикул", "Название", "Ozon", "WB", "Статус")
        widths = (160, 360, 60, 60, 140)

        tf = ttk.Frame(parent); tf.pack(fill="both", expand=True, padx=16, pady=4)
        vsb = ttk.Scrollbar(tf, orient="vertical")
        hsb = ttk.Scrollbar(tf, orient="horizontal")
        tree = ttk.Treeview(tf, columns=cols, show="headings",
                            yscrollcommand=vsb.set, xscrollcommand=hsb.set, height=20)
        vsb.config(command=tree.yview); hsb.config(command=tree.xview)
        vsb.pack(side="right", fill="y"); hsb.pack(side="bottom", fill="x")
        tree.pack(fill="both", expand=True)

        for col, head, w in zip(cols, heads, widths):
            tree.heading(col, text=head, command=lambda c=col, t=tree: self._sort_tree(t, c))
            tree.column(col, width=w, minwidth=40, anchor="center")
        tree.column("name", anchor="w")

        tree.tag_configure("both", foreground="#4caf50")
        tree.tag_configure("ozon_only", foreground="#2196f3")
        tree.tag_configure("wb_only", foreground="#ff9800")

        # Двойной клик — открыть диалог переноса
        tree.bind("<Double-1>", lambda e, mp=marketplace, t=tree: self._on_product_dblclick(e, mp, t))

        # Контекстное меню
        menu = tk.Menu(tree, tearoff=0)
        tree.bind("<Button-3>", lambda e, m=menu, mp=marketplace, t=tree: self._show_product_menu(e, m, mp, t))

        sl = ttk.Label(parent, text="Нажмите 'Загрузить товары'", foreground="gray")
        sl.pack(padx=16, pady=4, anchor="w")
        pb = ttk.Progressbar(parent, mode="indeterminate", length=300)
        pb.pack(padx=16, pady=(0, 4), anchor="w")

        if is_ozon:
            self.products_tree_ozon = tree
            self.products_menu_ozon = menu
            self.products_status_ozon = sl
            self.products_pb_ozon = pb
        else:
            self.products_tree_wb = tree
            self.products_menu_wb = menu
            self.products_status_wb = sl
            self.products_pb_wb = pb

    # ─────────────────────── WB — РЕКЛАМА (АВТО-СТАВКИ) ─────────────────

    _ADS_TYPE_LABELS = {4: "каталог", 5: "поиск", 6: "карточка товара", 7: "рекомендации",
                        8: "авто", 9: "аукцион"}
    _ADS_STATUS_LABELS = {4: "готова к запуску", 7: "завершена", 8: "отклонена",
                          9: "активна", 11: "на паузе"}

    def _build_wb_ads_tab(self, parent: ttk.Frame):
        ttk.Label(parent, text="Автоматическая регулировка ставок по ДРР",
                 font=("", 10, "bold")).pack(padx=16, pady=(12, 2), anchor="w")
        hint = ("Каждые 5 минут скрипт считает фактическую ДРР кампании за скользящие 3 дня "
                "(расход / выручка) и меняет ставку на заданный шаг в сторону цели. "
                "Изменения применяются сразу к живым кампаниям, без предпросмотра.\n"
                "Дважды кликните по кампании, чтобы задать целевую ДРР и включить для неё авторегулировку.")
        ttk.Label(parent, text=hint, foreground="gray", wraplength=1000, justify="left").pack(padx=16, anchor="w")

        bf = ttk.Frame(parent); bf.pack(padx=16, pady=8, anchor="w")
        self.ads_load_btn = ttk.Button(bf, text="Загрузить кампании", command=self._load_wb_ads_campaigns)
        self.ads_load_btn.pack(side="left", padx=4)
        ttk.Label(bf, text="Шаг ставки, %:").pack(side="left", padx=(16, 4))
        self.ads_step_pct = tk.StringVar(value=str(self.config_data.get("wb_ads", {}).get("step_pct", 10)))
        ttk.Spinbox(bf, from_=1, to=50, increment=1, width=6, textvariable=self.ads_step_pct).pack(side="left")
        self.ads_start_btn = ttk.Button(bf, text="Запустить авторегулировку", command=self._start_ads_auto)
        self.ads_start_btn.pack(side="left", padx=(16, 4))
        self.ads_stop_btn = ttk.Button(bf, text="Остановить", command=self._stop_ads_auto, state="disabled")
        self.ads_stop_btn.pack(side="left", padx=4)

        self.ads_status_label = ttk.Label(parent, text="Нажмите 'Загрузить кампании'", foreground="gray")
        self.ads_status_label.pack(padx=16, pady=(0, 4), anchor="w")

        cols = ("id", "name", "type", "status", "cpm", "target_drr", "enabled")
        heads = ("ID", "Кампания", "Тип", "Статус", "Ставка", "Цель ДРР, %", "Авто")
        widths = (90, 320, 100, 110, 80, 90, 60)
        tf = ttk.Frame(parent); tf.pack(fill="both", expand=True, padx=16, pady=4)
        vsb = ttk.Scrollbar(tf, orient="vertical")
        tree = ttk.Treeview(tf, columns=cols, show="headings", yscrollcommand=vsb.set, height=12)
        vsb.config(command=tree.yview); vsb.pack(side="right", fill="y")
        tree.pack(fill="both", expand=True)
        for col, head, w in zip(cols, heads, widths):
            tree.heading(col, text=head)
            tree.column(col, width=w, minwidth=40, anchor="center")
        tree.column("name", anchor="w")
        tree.tag_configure("enabled", foreground="#4caf50")
        tree.bind("<Double-1>", self._on_ads_campaign_dblclick)
        self.ads_tree = tree

        lf = ttk.LabelFrame(parent, text="Журнал авторегулировки")
        lf.pack(fill="both", expand=True, padx=16, pady=6)
        self.ads_log_text = scrolledtext.ScrolledText(lf, state="disabled", wrap="word", height=8,
                                                       font=("Consolas", 9), bg="#1e1e1e", fg="#d4d4d4")
        self.ads_log_text.pack(fill="both", expand=True, padx=4, pady=4)
        ttk.Button(lf, text="Очистить", command=lambda: self._clear_tab_log(self.ads_log_text)).pack(anchor="w", padx=4, pady=2)

    def _ads_log(self, msg: str):
        ts = datetime.datetime.now().strftime("%H:%M:%S")
        line = f"[{ts}] {msg}\n"
        def _append():
            self.ads_log_text.config(state="normal")
            self.ads_log_text.insert("end", line)
            self.ads_log_text.see("end")
            self.ads_log_text.config(state="disabled")
        self.after(0, _append)

    def _load_wb_ads_campaigns(self):
        wb_key = self.config_data.get("wb", {}).get("api_key", "")
        if not wb_key:
            messagebox.showerror("Реклама WB", "Укажите WB API Token в Настройках.")
            return
        # Блокируем кнопку на время запроса — повторный клик во время ожидания запускал
        # второй параллельный запрос к тому же лимитированному эндпоинту и только продлевал 429
        self.ads_load_btn.config(state="disabled")
        self.ads_status_label.config(text="Загрузка кампаний...", foreground="gray")
        threading.Thread(target=self._load_wb_ads_campaigns_thread, args=(wb_key,), daemon=True).start()

    def _load_wb_ads_campaigns_thread(self, wb_key: str):
        advert = WbAdvertClient(wb_key)
        try:
            campaigns = advert.get_campaigns()
        except Exception as exc:
            emsg = str(exc)
            if "429" in emsg:
                self._ads_log("Лимит запросов WB Advert API исчерпан — подождите и попробуйте позже "
                             "(не нажимайте кнопку повторно сразу, это только продлевает блокировку)")
                self.after(0, lambda: self.ads_status_label.config(
                    text="Лимит запросов WB исчерпан — попробуйте позже", foreground="red"))
            else:
                self._ads_log(f"Ошибка загрузки кампаний: {exc}")
                self.after(0, lambda: self.ads_status_label.config(text=f"Ошибка: {exc}", foreground="red"))
            self.after(0, lambda: self.ads_load_btn.config(state="normal"))
            return
        self._ads_campaigns = campaigns
        campaigns_cfg = self.config_data.get("wb_ads", {}).get("campaigns", {})

        def _fill():
            tree = self.ads_tree
            tree.delete(*tree.get_children())
            for c in campaigns:
                camp_id = str(c.get("advertId"))
                camp_cfg = campaigns_cfg.get(camp_id, {})
                target = camp_cfg.get("target_drr")
                enabled = bool(camp_cfg.get("enabled"))
                type_label = self._ADS_TYPE_LABELS.get(c.get("type"), str(c.get("type")))
                status_label = self._ADS_STATUS_LABELS.get(c.get("status"), str(c.get("status")))
                cpm = c.get("_cpm")
                tree.insert("", "end", iid=camp_id, values=(
                    camp_id, c.get("name"), type_label, status_label,
                    cpm if cpm is not None else "?",
                    f"{target:.0f}" if target else "—",
                    "Да" if enabled else "Нет",
                ), tags=("enabled",) if enabled else ())
            self.ads_status_label.config(text=f"Кампаний: {len(campaigns)}", foreground="black")
            self.ads_load_btn.config(state="normal")
        self.after(0, _fill)

    def _on_ads_campaign_dblclick(self, event):
        item = self.ads_tree.identify_row(event.y)
        if not item:
            return
        camp = next((c for c in self._ads_campaigns if str(c.get("advertId")) == item), None)
        if not camp:
            return
        campaigns_cfg = self.config_data.setdefault("wb_ads", {}).setdefault("campaigns", {})
        current = campaigns_cfg.get(item, {})
        dlg = AdsCampaignSettingsDialog(self, camp, current)
        if dlg.result is None:
            return
        campaigns_cfg[item] = dlg.result
        self._save_ads_settings()
        self._load_wb_ads_campaigns()

    def _save_ads_settings(self):
        cfg = dict(self.config_data)
        wb_ads = dict(cfg.get("wb_ads", {}))
        try:
            wb_ads["step_pct"] = float(self.ads_step_pct.get())
        except (ValueError, AttributeError):
            wb_ads["step_pct"] = wb_ads.get("step_pct", 10.0)
        cfg["wb_ads"] = wb_ads
        save_config(cfg)
        self.config_data = cfg

    def _start_ads_auto(self):
        self._save_ads_settings()
        wb_key = self.config_data.get("wb", {}).get("api_key", "")
        if not wb_key:
            messagebox.showerror("Реклама WB", "Укажите WB API Token в Настройках.")
            return
        campaigns_cfg = self.config_data.get("wb_ads", {}).get("campaigns", {})
        if not any(c.get("enabled") for c in campaigns_cfg.values()):
            messagebox.showinfo("Реклама WB",
                               "Нет кампаний с включённой авторегулировкой. "
                               "Дважды кликните по кампании в списке, чтобы включить.")
            return
        if not messagebox.askyesno(
            "Подтверждение",
            "Каждые 5 минут ставка на включённых кампаниях будет автоматически меняться "
            "на основе фактической ДРР. Изменения применяются сразу к живым кампаниям, "
            "без предварительного подтверждения каждого шага. Запустить?"
        ):
            return
        self._ads_stop_event.clear()
        self.ads_start_btn.config(state="disabled")
        self.ads_stop_btn.config(state="normal")
        self._ads_log("Автоматическая регулировка ставок запущена (проверка каждые 5 минут)")
        threading.Thread(target=self._ads_loop, daemon=True).start()

    def _stop_ads_auto(self):
        self._ads_stop_event.set()
        self.ads_start_btn.config(state="normal")
        self.ads_stop_btn.config(state="disabled")
        self._ads_log("Остановлено пользователем")

    def _ads_loop(self):
        wb_key = self.config_data.get("wb", {}).get("api_key", "")
        advert = WbAdvertClient(wb_key)
        while not self._ads_stop_event.is_set():
            wb_ads_cfg = self.config_data.get("wb_ads", {})
            step_pct = wb_ads_cfg.get("step_pct", 10.0)
            campaigns_cfg = wb_ads_cfg.get("campaigns", {})
            try:
                run_wb_ads_cycle(advert, campaigns_cfg, step_pct, log_fn=self._ads_log)
            except Exception as exc:
                self._ads_log(f"ОШИБКА цикла регулировки: {exc}")
            self._ads_stop_event.wait(300)  # 5 минут, но реагирует на Стоп сразу

    # ─────────────────────── OZON — РЕКЛАМА (АВТО-СТАВКИ) ────────────────

    def _build_ozon_ads_tab(self, parent: ttk.Frame):
        ttk.Label(parent, text="Автоматическая регулировка ставок по конкурентам",
                 font=("", 10, "bold")).pack(padx=16, pady=(12, 2), anchor="w")
        hint = ("Каждые 5 минут скрипт сравнивает вашу ставку с конкурентной ставкой по "
                "товару (Ozon Performance API) и держит вашу ставку выше на заданный процент. "
                "Работает только для обычных товарных CPC-кампаний с выключенным автопилотом "
                "Ozon — для баннерных, «Оплата за заказ» и кампаний с включённым автопилотом "
                "ручная ставка недоступна или игнорируется.\n"
                "Дважды кликните по товару, чтобы задать наценку и включить авторегулировку.")
        ttk.Label(parent, text=hint, foreground="gray", wraplength=1000, justify="left").pack(padx=16, anchor="w")

        bf = ttk.Frame(parent); bf.pack(padx=16, pady=8, anchor="w")
        self.ozon_ads_load_btn = ttk.Button(bf, text="Загрузить кампании и товары",
                                            command=self._load_ozon_ads_campaigns)
        self.ozon_ads_load_btn.pack(side="left", padx=4)
        self.ozon_ads_start_btn = ttk.Button(bf, text="Запустить авторегулировку", command=self._start_ozon_ads_auto)
        self.ozon_ads_start_btn.pack(side="left", padx=(16, 4))
        self.ozon_ads_stop_btn = ttk.Button(bf, text="Остановить", command=self._stop_ozon_ads_auto, state="disabled")
        self.ozon_ads_stop_btn.pack(side="left", padx=4)

        self.ozon_ads_status_label = ttk.Label(parent, text="Нажмите 'Загрузить кампании и товары'", foreground="gray")
        self.ozon_ads_status_label.pack(padx=16, pady=(0, 4), anchor="w")

        cols = ("campaign", "sku", "strategy", "competitive", "margin", "enabled")
        heads = ("Кампания", "SKU", "Стратегия", "Конкурентная, ₽", "Наценка, %", "Авто")
        widths = (220, 110, 140, 120, 90, 60)
        tf = ttk.Frame(parent); tf.pack(fill="both", expand=True, padx=16, pady=4)
        vsb = ttk.Scrollbar(tf, orient="vertical")
        tree = ttk.Treeview(tf, columns=cols, show="headings", yscrollcommand=vsb.set, height=12)
        vsb.config(command=tree.yview); vsb.pack(side="right", fill="y")
        tree.pack(fill="both", expand=True)
        for col, head, w in zip(cols, heads, widths):
            tree.heading(col, text=head)
            tree.column(col, width=w, minwidth=40, anchor="center")
        tree.column("campaign", anchor="w")
        tree.tag_configure("enabled", foreground="#4caf50")
        tree.tag_configure("auto", foreground="#888888")
        tree.bind("<Double-1>", self._on_ozon_ads_dblclick)
        self.ozon_ads_tree = tree

        lf = ttk.LabelFrame(parent, text="Журнал авторегулировки")
        lf.pack(fill="both", expand=True, padx=16, pady=6)
        self.ozon_ads_log_text = scrolledtext.ScrolledText(lf, state="disabled", wrap="word", height=8,
                                                           font=("Consolas", 9), bg="#1e1e1e", fg="#d4d4d4")
        self.ozon_ads_log_text.pack(fill="both", expand=True, padx=4, pady=4)
        ttk.Button(lf, text="Очистить", command=lambda: self._clear_tab_log(self.ozon_ads_log_text)).pack(anchor="w", padx=4, pady=2)

    def _ozon_ads_log(self, msg: str):
        ts = datetime.datetime.now().strftime("%H:%M:%S")
        line = f"[{ts}] {msg}\n"
        def _append():
            self.ozon_ads_log_text.config(state="normal")
            self.ozon_ads_log_text.insert("end", line)
            self.ozon_ads_log_text.see("end")
            self.ozon_ads_log_text.config(state="disabled")
        self.after(0, _append)

    def _load_ozon_ads_campaigns(self):
        perf_cfg = self.config_data.get("performance", {})
        if not perf_cfg.get("client_id") or not perf_cfg.get("client_secret"):
            messagebox.showerror("Реклама Ozon", "Укажите Performance API Client ID/Secret в Настройках.")
            return
        self.ozon_ads_load_btn.config(state="disabled")
        self.ozon_ads_status_label.config(text="Загрузка кампаний и товаров...", foreground="gray")
        threading.Thread(target=self._load_ozon_ads_campaigns_thread, args=(perf_cfg,), daemon=True).start()

    def _load_ozon_ads_campaigns_thread(self, perf_cfg: Dict[str, Any]):
        perf = OzonPerformanceClient(perf_cfg.get("client_id", ""), perf_cfg.get("client_secret", ""))
        try:
            campaigns = perf.get_campaigns()
        except Exception as exc:
            self._ozon_ads_log(f"Ошибка загрузки кампаний: {exc}")
            self.after(0, lambda: (
                self.ozon_ads_status_label.config(text=f"Ошибка: {exc}", foreground="red"),
                self.ozon_ads_load_btn.config(state="normal"),
            ))
            return

        rows: List[Dict[str, Any]] = []  # {campaign, sku, competitive_bid}
        for camp in campaigns:
            camp_id = str(camp.get("id", ""))
            if not camp_id or camp.get("advObjectType") != "SKU":
                continue
            try:
                skus = perf.get_campaign_products(camp_id)
            except Exception as exc:
                self._ozon_ads_log(f"  Кампания {camp.get('title')}: ошибка получения товаров — {exc}")
                continue
            if not skus:
                continue
            is_manual = _ozon_campaign_bid_editable(camp)
            competitive: Dict[str, float] = {}
            if is_manual:
                # Конкурентные ставки запрашиваем только там, где авторегулировка вообще
                # возможна — экономим запросы для кампаний на автопилоте, где это не нужно
                try:
                    competitive = perf.get_competitive_bids(camp_id, skus)
                except Exception as exc:
                    self._ozon_ads_log(f"  Кампания {camp.get('title')}: ошибка конкурентных ставок — {exc}")
            for sku in skus:
                rows.append({"campaign": camp, "sku": sku, "competitive_bid": competitive.get(sku)})
            time.sleep(0.3)

        self._ozon_ads_products = rows
        products_cfg = self.config_data.get("ozon_ads", {}).get("products", {})

        def _fill():
            tree = self.ozon_ads_tree
            tree.delete(*tree.get_children())
            for r in rows:
                camp = r["campaign"]
                camp_id = str(camp.get("id"))
                sku = r["sku"]
                key = f"{camp_id}:{sku}"
                is_manual = _ozon_campaign_bid_editable(camp)
                cfg = products_cfg.get(key, {})
                enabled = bool(cfg.get("enabled")) and is_manual
                margin = cfg.get("margin_pct")
                comp_bid = r["competitive_bid"]
                strategy_label = "ручная" if is_manual else "автопилот Ozon"
                tree.insert("", "end", iid=key, values=(
                    camp.get("title", camp_id), sku, strategy_label,
                    f"{comp_bid:.2f}" if comp_bid is not None else "—",
                    f"{margin:.0f}" if margin else "—",
                    "Да" if enabled else "Нет",
                ), tags=("enabled",) if enabled else (("auto",) if not is_manual else ()))
            self.ozon_ads_status_label.config(text=f"Товаров: {len(rows)}", foreground="black")
            self.ozon_ads_load_btn.config(state="normal")
        self.after(0, _fill)

    def _on_ozon_ads_dblclick(self, event):
        item = self.ozon_ads_tree.identify_row(event.y)
        if not item:
            return
        row = next((r for r in self._ozon_ads_products
                   if f"{r['campaign'].get('id')}:{r['sku']}" == item), None)
        if not row:
            return
        products_cfg = self.config_data.setdefault("ozon_ads", {}).setdefault("products", {})
        current = products_cfg.get(item, {})
        dlg = OzonAdsProductSettingsDialog(self, row["campaign"], row["sku"], row["competitive_bid"], current)
        if dlg.result is None:
            return
        products_cfg[item] = dlg.result
        self._save_ozon_ads_settings()
        self._load_ozon_ads_campaigns()

    def _save_ozon_ads_settings(self):
        cfg = dict(self.config_data)
        cfg["ozon_ads"] = dict(cfg.get("ozon_ads", {}))
        save_config(cfg)
        self.config_data = cfg

    def _start_ozon_ads_auto(self):
        perf_cfg = self.config_data.get("performance", {})
        if not perf_cfg.get("client_id") or not perf_cfg.get("client_secret"):
            messagebox.showerror("Реклама Ozon", "Укажите Performance API Client ID/Secret в Настройках.")
            return
        products_cfg = self.config_data.get("ozon_ads", {}).get("products", {})
        if not any(p.get("enabled") for p in products_cfg.values()):
            messagebox.showinfo("Реклама Ozon",
                               "Нет товаров с включённой авторегулировкой. "
                               "Дважды кликните по товару в списке, чтобы включить (доступно "
                               "только для кампаний без автостратегии Ozon).")
            return
        if not messagebox.askyesno(
            "Подтверждение",
            "Каждые 5 минут ставка на включённых товарах будет автоматически подстраиваться "
            "под конкурентную ставку с заданной наценкой. Изменения применяются сразу к живым "
            "кампаниям, без предварительного подтверждения каждого шага. Запустить?"
        ):
            return
        self._ozon_ads_stop_event.clear()
        self.ozon_ads_start_btn.config(state="disabled")
        self.ozon_ads_stop_btn.config(state="normal")
        self._ozon_ads_log("Автоматическая регулировка ставок запущена (проверка каждые 5 минут)")
        threading.Thread(target=self._ozon_ads_loop, daemon=True).start()

    def _stop_ozon_ads_auto(self):
        self._ozon_ads_stop_event.set()
        self.ozon_ads_start_btn.config(state="normal")
        self.ozon_ads_stop_btn.config(state="disabled")
        self._ozon_ads_log("Остановлено пользователем")

    def _ozon_ads_loop(self):
        perf_cfg = self.config_data.get("performance", {})
        perf = OzonPerformanceClient(perf_cfg.get("client_id", ""), perf_cfg.get("client_secret", ""))
        while not self._ozon_ads_stop_event.is_set():
            products_cfg = self.config_data.get("ozon_ads", {}).get("products", {})
            try:
                run_ozon_ads_cycle(perf, products_cfg, log_fn=self._ozon_ads_log)
            except Exception as exc:
                self._ozon_ads_log(f"ОШИБКА цикла регулировки: {exc}")
            self._ozon_ads_stop_event.wait(300)  # 5 минут, но реагирует на Стоп сразу

    def _on_product_dblclick(self, event, marketplace: str, tree: ttk.Treeview):
        item = tree.identify_row(event.y)
        if not item:
            return
        vals = tree.item(item, "values")
        if not vals:
            return
        sku, name, on_ozon, on_wb = vals[0], vals[1], vals[2], vals[3]
        # Перенос только если товара нет на одной из площадок
        if on_ozon == "—":
            self._open_transfer_dialog(sku, name, direction="wb_to_ozon")
        elif on_wb == "—":
            self._open_transfer_dialog(sku, name, direction="ozon_to_wb")
        else:
            messagebox.showinfo("Товары", f"Товар '{sku}' уже есть на обеих площадках.")

    def _show_product_menu(self, event, menu: tk.Menu, marketplace: str, tree: ttk.Treeview):
        item = tree.identify_row(event.y)
        if not item:
            return
        tree.selection_set(item)
        vals = tree.item(item, "values")
        if not vals:
            return
        sku, name, on_ozon, on_wb = vals[0], vals[1], vals[2], vals[3]
        menu.delete(0, "end")
        if on_ozon == "—":
            menu.add_command(label=f"Перенести '{sku}' на Ozon",
                             command=lambda: self._open_transfer_dialog(sku, name, "wb_to_ozon"))
        if on_wb == "—":
            menu.add_command(label=f"Перенести '{sku}' на WB",
                             command=lambda: self._open_transfer_dialog(sku, name, "ozon_to_wb"))
        if on_ozon != "—" and on_wb != "—":
            menu.add_command(label="Товар уже на обеих площадках", state="disabled")
        menu.add_separator()
        menu.add_command(label="Закрыть меню")
        try:
            menu.tk_popup(event.x_root, event.y_root)
        finally:
            menu.grab_release()

    def _open_transfer_dialog(self, sku: str, name: str, direction: str):
        cfg = self.config_data
        ozon = OzonClient(OzonCredentials(cfg["ozon"]["client_id"], cfg["ozon"]["api_key"]))
        wb_key = cfg.get("wb", {}).get("api_key", "")
        if not wb_key:
            messagebox.showerror("WB", "Укажите WB API Token в Настройках.")
            return
        wb = WbClient(wb_key)

        # Подтягиваем данные товара с исходной площадки
        description = ""
        price = 0.0
        images: List[str] = []

        if direction == "ozon_to_wb":
            # Берём данные с Ozon
            info = ozon.get_product_info(sku)
            description = ""
            for attr in info.get("attributes", []):
                # атрибут описания — id 4191 или ищем по названию
                if attr.get("attribute_id") in (4191, 11254):
                    vals = attr.get("values", [])
                    if vals:
                        description = vals[0].get("value", "")
                        break
            price = float(info.get("price", "0") or "0")
            images = info.get("images", []) or []
        else:
            # Берём данные с WB — ищем в _products_data
            if hasattr(self, "_products_data"):
                for row in self._products_data:
                    if row[0] == sku:
                        name = row[1]
                        break
            # Загружаем карточку WB
            wb_card: Dict[str, Any] = {}
            try:
                cards = wb.get_all_cards()
                for card in cards:
                    if str(card.get("vendorCode", "")) == sku:
                        wb_card = card
                        description = card.get("description", "")
                        photos = card.get("photos", []) or []
                        images = [p.get("big") or p.get("tm") or "" for p in photos if isinstance(p, dict)]
                        images = [i for i in images if i]
                        break
            except Exception:
                pass

        TransferDialog(self, direction=direction, sku=sku, name=name,
                       description=description, price=price, images=images,
                       ozon_client=ozon, wb_client=wb,
                       wb_card=wb_card if direction == "wb_to_ozon" else {})

    def _sort_tree(self, tree: ttk.Treeview, col: str):
        items = [(tree.set(k, col), k) for k in tree.get_children("")]
        items.sort(key=lambda x: x[0].lower() if x[0] else "")
        for idx, (_, k) in enumerate(items):
            tree.move(k, "", idx)

    def _load_products(self):
        for attr in ("products_pb_ozon", "products_pb_wb"):
            if hasattr(self, attr):
                getattr(self, attr).start(12)
        for attr in ("products_status_ozon", "products_status_wb"):
            if hasattr(self, attr):
                getattr(self, attr).config(text="Загрузка...", foreground="gray")
        threading.Thread(target=self._load_products_thread, daemon=True).start()

    def _load_products_thread(self):
        cfg = self.config_data
        self._prices_log("Загружаем список товаров Ozon...")
        ozon_products: Dict[str, str] = {}  # offer_id -> name
        try:
            ozon = OzonClient(OzonCredentials(cfg["ozon"]["client_id"], cfg["ozon"]["api_key"]))
            offer_ids = ozon.list_all_offer_ids()
            names = ozon.get_product_names(offer_ids)
            for oid in offer_ids:
                ozon_products[oid] = names.get(oid, oid)
            self._prices_log(f"Ozon товаров: {len(ozon_products)}")
        except Exception as exc:
            self._prices_log(f"Ошибка Ozon товары: {exc}")

        self._prices_log("Загружаем список товаров WB...")
        wb_products: Dict[str, str] = {}  # vendorCode -> name
        try:
            wb_key = cfg.get("wb", {}).get("api_key", "")
            if wb_key:
                wb = WbClient(wb_key)
                cards = wb.get_all_cards()
                for card in cards:
                    vc = str(card.get("vendorCode", ""))
                    name = card.get("title") or card.get("name") or vc
                    if vc:
                        wb_products[vc] = name
                self._prices_log(f"WB товаров: {len(wb_products)}")
            else:
                self._prices_log("WB: токен не указан")
        except Exception as exc:
            self._prices_log(f"Ошибка WB товары: {exc}")

        all_skus = sorted(set(ozon_products) | set(wb_products))
        rows = []
        for sku in all_skus:
            on_ozon = sku in ozon_products
            on_wb = sku in wb_products
            name = ozon_products.get(sku) or wb_products.get(sku, sku)
            if on_ozon and on_wb:
                status, tag = "Обе площадки", "both"
            elif on_ozon:
                status, tag = "Только Ozon", "ozon_only"
            else:
                status, tag = "Только WB", "wb_only"
            rows.append((sku, name[:50], "✓" if on_ozon else "—",
                         "✓" if on_wb else "—", status, tag))

        self._products_data = rows

        def _fill(tree):
            tree.delete(*tree.get_children())
            for r in rows:
                tree.insert("", "end", values=r[:-1], tags=(r[-1],))

        both = sum(1 for r in rows if r[-1] == "both")
        ozon_only = sum(1 for r in rows if r[-1] == "ozon_only")
        wb_only = sum(1 for r in rows if r[-1] == "wb_only")
        msg = f"Всего: {len(rows)} | Обе: {both} | Только Ozon: {ozon_only} | Только WB: {wb_only}"

        for attr in ("products_tree_ozon", "products_tree_wb"):
            if hasattr(self, attr):
                self.after(0, lambda t=getattr(self, attr): _fill(t))
        for attr in ("products_pb_ozon", "products_pb_wb"):
            if hasattr(self, attr):
                self.after(0, lambda pb=getattr(self, attr): pb.stop())
        for attr in ("products_status_ozon", "products_status_wb"):
            if hasattr(self, attr):
                self.after(0, lambda sl=getattr(self, attr): sl.config(text=msg, foreground="black"))
        self._prices_log(msg)

    def _copy_missing_skus(self, marketplace: str):
        if not hasattr(self, "_products_data"):
            messagebox.showinfo("Товары", "Сначала нажмите 'Загрузить товары'.")
            return
        tag = "ozon_only" if marketplace == "wb" else "wb_only"
        label = "только на Ozon (нет на WB)" if marketplace == "wb" else "только на WB (нет на Ozon)"
        skus = [r[0] for r in self._products_data if r[-1] == tag]
        if not skus:
            messagebox.showinfo("Товары", f"Нет товаров {label}.")
            return
        text = "\n".join(skus)
        self.clipboard_clear()
        self.clipboard_append(text)
        messagebox.showinfo("Товары", f"Скопировано {len(skus)} артикулов {label} в буфер обмена.")

    def _export_products_csv(self, marketplace: str):
        if not hasattr(self, "_products_data"):
            messagebox.showinfo("Товары", "Сначала нажмите 'Загрузить товары'.")
            return
        from tkinter.filedialog import asksaveasfilename
        path = asksaveasfilename(defaultextension=".csv",
                                  filetypes=[("CSV files", "*.csv"), ("All", "*.*")],
                                  initialfile=f"products_{marketplace}.csv")
        if not path:
            return
        import csv
        with open(path, "w", newline="", encoding="utf-8-sig") as f:
            w = csv.writer(f)
            w.writerow(["Артикул", "Название", "Ozon", "WB", "Статус"])
            for r in self._products_data:
                w.writerow(r[:-1])
        messagebox.showinfo("Экспорт", f"Сохранено {len(self._products_data)} строк в {path}")

    def _copy_ozon_name_desc_to_wb(self):
        if not hasattr(self, "_products_data"):
            messagebox.showinfo("Товары", "Сначала нажмите 'Загрузить товары'.")
            return
        items = [(r[0], r[1]) for r in self._products_data if r[-1] == "both"]
        if not items:
            messagebox.showinfo("Товары", "Нет товаров, присутствующих на обеих площадках.")
            return

        dlg = ProductSelectorDialog(self, items, "Выбор товаров для копирования Ozon → WB")
        if dlg.selected is None:
            return
        if not dlg.selected:
            messagebox.showinfo("Товары", "Ни один товар не выбран.")
            return
        skus = dlg.selected

        if not messagebox.askyesno(
            "Подтверждение",
            f"Скопировать название и описание с Ozon на WB для {len(skus)} товаров "
            f"(сопоставление по артикулу)?\nТекущие название и описание на WB будут перезаписаны."
        ):
            return
        threading.Thread(target=self._copy_ozon_name_desc_to_wb_thread, args=(skus,), daemon=True).start()

    def _copy_ozon_name_desc_to_wb_thread(self, skus: List[str]):
        cfg = self.config_data
        ozon = OzonClient(OzonCredentials(cfg["ozon"]["client_id"], cfg["ozon"]["api_key"]))
        wb_key = cfg.get("wb", {}).get("api_key", "")
        if not wb_key:
            self._prices_log("Копирование Ozon → WB: нет WB токена")
            return
        wb = WbClient(wb_key)

        self._prices_log(f"Копирование название+описание Ozon → WB: {len(skus)} товаров...")
        try:
            cards = wb.get_all_cards()
        except Exception as exc:
            self._prices_log(f"Копирование Ozon → WB: ошибка загрузки карточек WB — {exc}")
            return
        wb_by_vendor = {str(c.get("vendorCode", "")): c for c in cards}

        # Сначала собираем все обновлённые карточки, затем отправляем их
        # одним batch-запросом (WB принимает до 3000 карточек за раз) —
        # это почти исключает попадание в лимит 100 запросов/мин.
        to_update: List[tuple] = []  # (sku, updated_card)
        skipped = 0
        for sku in skus:
            card = wb_by_vendor.get(sku)
            if not card:
                self._prices_log(f"  {sku}: нет карточки на WB — пропущено")
                skipped += 1
                continue
            try:
                info = ozon.get_product_info(sku)
            except Exception as exc:
                self._prices_log(f"  {sku}: ошибка Ozon — {exc}")
                skipped += 1
                continue

            name = (info.get("name") or "").strip()
            description = ""
            for attr in info.get("attributes", []):
                if attr.get("attribute_id") in (4191, 11254):
                    vals = attr.get("values", [])
                    if vals:
                        description = vals[0].get("value", "")
                        break

            if not name and not description:
                self._prices_log(f"  {sku}: нет названия/описания на Ozon — пропущено")
                skipped += 1
                continue

            updated_card = dict(card)
            if name:
                updated_card["title"] = name[:100]
            if description:
                updated_card["description"] = description[:5000]
            to_update.append((sku, updated_card))

        if not to_update:
            self._prices_log(f"Копирование Ozon → WB завершено: обновлять нечего (пропущено {skipped})")
            self.after(0, lambda: messagebox.showinfo("Товары", f"Пропущено: {skipped}"))
            return

        updated = 0
        chunk_size = 500
        for i in range(0, len(to_update), chunk_size):
            chunk = to_update[i:i + chunk_size]
            try:
                wb.update_cards([c for _, c in chunk])
                updated += len(chunk)
                for sku, _ in chunk:
                    self._prices_log(f"  {sku}: обновлено")
            except Exception as exc:
                skipped += len(chunk)
                self._prices_log(f"  Ошибка обновления пачки из {len(chunk)} товаров: {exc}")

        self._prices_log(f"Копирование Ozon → WB завершено: обновлено {updated}, пропущено {skipped}")
        self.after(0, lambda: messagebox.showinfo(
            "Товары", f"Скопировано на WB: {updated}\nПропущено: {skipped}"))

    def _sort_prices_tree(self, tree: ttk.Treeview, col: str):
        items = [(tree.set(k, col), k) for k in tree.get_children("")]
        try:
            items.sort(key=lambda x: float(x[0].replace(" ", "").replace(",", ".").replace("₽", "").replace("%", "") or 0))
        except ValueError:
            items.sort()
        for idx, (_, k) in enumerate(items):
            tree.move(k, "", idx)

    # ─────────────────────────── ОБЩИЙ ЛОГ ──────────────────────────────

    def _build_log_tab(self, parent: ttk.Frame):
        self.log_text = scrolledtext.ScrolledText(parent, state="disabled", wrap="word",
                                                   font=("Consolas", 9), bg="#1e1e1e", fg="#d4d4d4")
        self.log_text.pack(fill="both", expand=True, padx=6, pady=6)
        bf = ttk.Frame(parent); bf.pack(fill="x", padx=6, pady=4)
        ttk.Button(bf, text="Очистить", command=lambda: self._clear_tab_log(self.log_text)).pack(side="left", padx=4)
        ttk.Button(bf, text="Сохранить в файл", command=self._save_log).pack(side="left", padx=4)

    # ─────────────────────────── НАСТРОЙКИ ──────────────────────────────

    def _load_config_to_ui(self):
        cfg = self.config_data
        self.ozon_client_id.insert(0, cfg.get("ozon", {}).get("client_id", ""))
        self.ozon_api_key.insert(0, cfg.get("ozon", {}).get("api_key", ""))
        self.wb_api_key.insert(0, cfg.get("wb", {}).get("api_key", ""))
        self.mpstats_token.insert(0, cfg.get("mpstats", {}).get("token", ""))

        perf = cfg.get("performance", {})
        self.perf_enabled.set(perf.get("enabled", False))
        self.perf_client_id.insert(0, perf.get("client_id", ""))
        self.perf_client_secret.insert(0, perf.get("client_secret", ""))
        self._on_perf_toggle()

        ai_cfg = cfg.get("ai", {})
        self.ai_provider.set(ai_cfg.get("provider", "template"))
        self.anthropic_api_key.insert(0, ai_cfg.get("anthropic_api_key", ""))
        self.claude_model.set(ai_cfg.get("model", "claude-opus-4-8"))
        self.gemini_api_key.insert(0, ai_cfg.get("gemini_api_key", ""))
        self.gemini_model.set(ai_cfg.get("gemini_model", "gemini-2.0-flash"))
        self._on_provider_change()

        upd = cfg.get("update", {})
        self.days_back.set(upd.get("days_back", 30))
        self.top_keywords.set(upd.get("top_keywords", 15))
        self.target_ids.insert(0, ", ".join(upd.get("target_offer_ids", [])))

    def _save_settings(self):
        try:
            # Начинаем с копии текущего конфига, чтобы не затереть разделы, которыми
            # эта вкладка не управляет (например wb_ads — настройки авторегулировки ставок)
            cfg = dict(self.config_data)
            cfg.update({
                "ozon": {"client_id": self.ozon_client_id.get().strip(), "api_key": self.ozon_api_key.get().strip()},
                "wb": {"api_key": self.wb_api_key.get().strip()},
                "mpstats": {"token": self.mpstats_token.get().strip()},
                "performance": {
                    "enabled": self.perf_enabled.get(),
                    "client_id": self.perf_client_id.get().strip(),
                    "client_secret": self.perf_client_secret.get().strip(),
                },
                "ai": {
                    "provider": self.ai_provider.get(),
                    "anthropic_api_key": self.anthropic_api_key.get().strip(),
                    "model": self.claude_model.get().strip() or "claude-opus-4-8",
                    "gemini_api_key": self.gemini_api_key.get().strip(),
                    "gemini_model": self.gemini_model.get().strip() or "gemini-2.0-flash",
                },
                "update": {
                    "days_back": int(self.days_back.get()),
                    "top_keywords": int(self.top_keywords.get()),
                    "target_offer_ids": [s.strip() for s in self.target_ids.get().split(",") if s.strip()],
                },
            })
            save_config(cfg)
            self.config_data = cfg
            messagebox.showinfo("Настройки", "Сохранено.")
        except Exception as exc:
            messagebox.showerror("Ошибка", str(exc))

    def _toggle_keys(self):
        fields = [self.ozon_api_key, self.wb_api_key, self.mpstats_token,
                  self.anthropic_api_key, self.gemini_api_key, self.perf_client_secret]
        show = "" if self.ozon_api_key.cget("show") == "*" else "*"
        for f in fields:
            try: f.config(show=show)
            except Exception: pass

    def _on_perf_toggle(self):
        ws = (self._perf_id_label, self.perf_client_id, self._perf_secret_label,
              self.perf_client_secret, self._perf_btn_frame)
        for w in ws:
            w.grid() if self.perf_enabled.get() else w.grid_remove()

    def _on_provider_change(self):
        provider = self.ai_provider.get()
        is_claude = provider == "claude"
        is_gemini = provider == "gemini"
        is_template = provider == "template"
        for w in (self._claude_key_label, self.anthropic_api_key, self._claude_model_label, self.claude_model, self._claude_check_frame):
            w.grid() if is_claude else w.grid_remove()
        for w in (self._gemini_key_label, self.gemini_api_key, self._gemini_model_label, self.gemini_model, self._gemini_check_frame, self._gemini_hint_label):
            w.grid() if is_gemini else w.grid_remove()
        self._template_info_label.grid() if is_template else self._template_info_label.grid_remove()

    def _check_performance(self):
        client = OzonPerformanceClient(self.perf_client_id.get().strip(), self.perf_client_secret.get().strip())
        r = client.test_connection()
        (messagebox.showinfo if r == "ok" else messagebox.showerror)("Performance API", "Подключение успешно!" if r == "ok" else r)

    def _check_ozon(self):
        cid = self.ozon_client_id.get().strip()
        key = self.ozon_api_key.get().strip()
        if not cid or not key:
            messagebox.showerror("Ozon API", "Заполните Client ID и API Key.")
            return
        try:
            ozon = OzonClient(OzonCredentials(cid, key))
            ozon._post("/v3/product/list", {"filter": {}, "last_id": "", "limit": 1})
            messagebox.showinfo("Ozon API", "✓ Подключение успешно!")
        except Exception as e:
            messagebox.showerror("Ozon API", f"Ошибка: {e}")

    def _check_wb(self):
        key = self.wb_api_key.get().strip()
        if not key:
            messagebox.showerror("WB API", "Введите WB API Token.")
            return
        try:
            wb = WbClient(key)
            result = wb.test_connection()
            if result == "ok":
                messagebox.showinfo("WB API", "✓ Подключение успешно!")
            else:
                messagebox.showerror("WB API", f"Ошибка: {result}")
        except Exception as e:
            messagebox.showerror("WB API", f"Ошибка: {e}")

    def _check_claude(self):
        key = self.anthropic_api_key.get().strip()
        model = self.claude_model.get().strip() or "claude-haiku-4-5"
        if not key:
            messagebox.showerror("Claude API", "Введите Anthropic API Key.")
            return
        try:
            import anthropic as _anthropic
            client = _anthropic.Anthropic(api_key=key, max_retries=0)
            resp = client.messages.create(model=model, max_tokens=10,
                                          messages=[{"role": "user", "content": "Hi"}])
            messagebox.showinfo("Claude API", f"✓ Подключение успешно! Модель: {model}")
        except Exception as e:
            messagebox.showerror("Claude API", f"Ошибка: {e}")

    def _check_gemini(self):
        key = self.gemini_api_key.get().strip()
        model = self.gemini_model.get().strip() or "gemini-2.0-flash"
        if not key:
            messagebox.showerror("Gemini API", "Введите Gemini API Key.")
            return
        try:
            from google import genai
            client = genai.Client(api_key=key)
            client.models.generate_content(model=model, contents="Hi")
            messagebox.showinfo("Gemini API", f"✓ Подключение успешно! Модель: {model}")
        except Exception as e:
            err_str = str(e)
            if "404" in err_str or "NOT_FOUND" in err_str:
                messagebox.showerror("Gemini API",
                    f"Модель '{model}' недоступна.\n\n"
                    "Используйте одну из доступных моделей:\n"
                    "• gemini-2.0-flash (рекомендуется)\n"
                    "• gemini-2.5-flash\n"
                    "• gemini-2.5-pro")
            elif "429" in err_str or "quota" in err_str.lower() or "exceeded" in err_str.lower() or "resource_exhausted" in err_str.lower():
                messagebox.showerror("Gemini API",
                    "Лимит запросов исчерпан (429 Quota Exceeded).\n\n"
                    "Бесплатный тир Gemini ограничен:\n"
                    "• 15 запросов/мин\n"
                    "• 1500 запросов/день\n\n"
                    "Подождите минуту и попробуйте снова, или перейдите на платный тариф на aistudio.google.com")
            elif "api_key" in err_str.lower() or "invalid" in err_str.lower() or "401" in err_str:
                messagebox.showerror("Gemini API", "Неверный API ключ. Проверьте ключ на aistudio.google.com")
            else:
                messagebox.showerror("Gemini API", f"Ошибка: {e}")

    def _check_mpstats(self):
        token = self.mpstats_token.get().strip()
        if not token:
            messagebox.showerror("MPStats", "Введите MPStats Token.")
            return
        r = MpstatsClient(token).test_connection()
        (messagebox.showinfo if r == "ok" else messagebox.showerror)("MPStats", "Подключение успешно!" if r == "ok" else r)

    # ─────────────────────────── ЛОГ ────────────────────────────────────

    def _log(self, msg: str):
        self._log_queue.put(msg)
        self._tab_log(self.ozon_log_text, msg)

    def _wb_log(self, msg: str):
        self._log_queue.put(f"[WB] {msg}")
        self._tab_log(self.wb_log_text, msg)

    def _prices_log(self, msg: str):
        self._log_queue.put(f"[Цены] {msg}")

    _LOG_MAX_LINES = 500  # максимум строк в каждом виджете лога

    def _trim_log(self, widget: scrolledtext.ScrolledText):
        """Удаляет первую половину строк если превышен лимит."""
        lines = int(widget.index("end-1c").split(".")[0])
        if lines > self._LOG_MAX_LINES:
            keep_from = lines - self._LOG_MAX_LINES // 2
            widget.delete("1.0", f"{keep_from}.0")

    def _tab_log(self, widget: scrolledtext.ScrolledText, msg: str):
        def _append():
            widget.config(state="normal")
            ts = datetime.datetime.now().strftime("%H:%M:%S")
            widget.insert("end", f"[{ts}] {msg}\n")
            self._trim_log(widget)
            widget.see("end")
            widget.config(state="disabled")
        self.after(0, _append)

    def _poll_log_queue(self):
        try:
            while True:
                msg = self._log_queue.get_nowait()
                self.log_text.config(state="normal")
                ts = datetime.datetime.now().strftime("%H:%M:%S")
                self.log_text.insert("end", f"[{ts}] {msg}\n")
                self._trim_log(self.log_text)
                self.log_text.see("end")
                self.log_text.config(state="disabled")
        except queue.Empty:
            pass
        self.after(100, self._poll_log_queue)

    def _clear_tab_log(self, widget: scrolledtext.ScrolledText):
        widget.config(state="normal")
        widget.delete("1.0", "end")
        widget.config(state="disabled")

    def _save_log(self):
        from tkinter.filedialog import asksaveasfilename
        path = asksaveasfilename(defaultextension=".txt", filetypes=[("Text files", "*.txt"), ("All", "*.*")])
        if path:
            with open(path, "w", encoding="utf-8") as f:
                f.write(self.log_text.get("1.0", "end"))

    def _make_confirm_fn(self):
        def confirm(offer_id, product_name, old_desc, new_desc, old_title=None, new_title=None):
            result_holder = [None]
            done_event = threading.Event()
            def show_dialog():
                dlg = PreviewDialog(self, offer_id, product_name, old_desc, new_desc,
                                    old_title=old_title, new_title=new_title)
                result_holder[0] = dlg.result or "skip"
                done_event.set()
            self.after(0, show_dialog)
            done_event.wait()
            return result_holder[0]
        return confirm

    # ─────────────────── OZON ОПИСАНИЕ — ПОТОК ───────────────────────────

    def _build_clients(self):
        cfg = self.config_data
        ozon = OzonClient(OzonCredentials(cfg["ozon"]["client_id"], cfg["ozon"]["api_key"]))
        ai_cfg = cfg.get("ai", {})
        # Приоритет: выбор на вкладке → из настроек
        tab_choice = getattr(self, "ozon_ai_provider", None)
        tab_val = tab_choice.get() if tab_choice else "settings"
        provider = tab_val if tab_val != "settings" else ai_cfg.get("provider", "template")
        generator = self._make_generator(provider, ai_cfg, for_wb=False)
        return ozon, generator

    def _make_generator(self, provider: str, ai_cfg: dict, for_wb: bool = False):
        if provider == "gemini":
            try:
                g = GeminiDescriptionGenerator(ai_cfg.get("gemini_api_key", ""), ai_cfg.get("gemini_model", "gemini-2.0-flash"))
                if for_wb:
                    g = g.for_wb()
                label = f"Gemini ({ai_cfg.get('gemini_model', 'gemini-2.0-flash')})"
                (self._wb_log if for_wb else self._log)(f"Генератор: {label}")
                return g
            except Exception as e:
                (self._wb_log if for_wb else self._log)(f"Ошибка Gemini: {e} — шаблон")
                return WbDescriptionGenerator() if for_wb else TemplateDescriptionGenerator()
        elif provider == "claude":
            try:
                g = DescriptionGenerator(ai_cfg.get("anthropic_api_key", ""), ai_cfg.get("model", "claude-opus-4-8"))
                (self._wb_log if for_wb else self._log)(f"Генератор: Claude ({ai_cfg.get('model')})")
                return g
            except Exception as e:
                (self._wb_log if for_wb else self._log)(f"Ошибка Claude: {e} — шаблон")
                return WbDescriptionGenerator() if for_wb else TemplateDescriptionGenerator()
        else:
            label = "шаблон WB" if for_wb else "шаблонный"
            (self._wb_log if for_wb else self._log)(f"Генератор: {label}")
            return WbDescriptionGenerator() if for_wb else TemplateDescriptionGenerator()

    def _start_update(self):
        self._save_settings()
        # Загружаем список товаров для выбора
        self.start_btn.config(state="disabled")
        self.status_label.config(text="Загружаем список товаров...")
        threading.Thread(target=self._load_and_select_ozon, daemon=True).start()

    def _load_and_select_ozon(self):
        try:
            ozon, _ = self._build_clients()
            all_ids = ozon.list_all_offer_ids()
        except Exception as exc:
            self.after(0, lambda: (
                self.start_btn.config(state="normal"),
                self.status_label.config(text="Ошибка загрузки"),
                messagebox.showerror("Ошибка", f"Не удалось загрузить товары: {exc}")
            ))
            return
        # Получаем названия товаров
        try:
            snapshots = ozon.get_products_snapshot(all_ids[:200])
            items = [(s.offer_id, s.name or s.offer_id) for s in snapshots]
            missing = [(oid, oid) for oid in all_ids if oid not in {s.offer_id for s in snapshots}]
            items = items + missing
        except Exception:
            items = [(oid, oid) for oid in all_ids]

        def show_dialog():
            dlg = ProductSelectorDialog(self, items, "Выбор товаров Ozon для обновления описаний")
            if dlg.selected is None:
                self.start_btn.config(state="normal")
                self.status_label.config(text="Отменено")
                return
            if not dlg.selected:
                messagebox.showinfo("Выбор товаров", "Ни один товар не выбран.")
                self.start_btn.config(state="normal")
                self.status_label.config(text="Нет выбранных товаров")
                return
            self._selected_ozon_ids = dlg.selected
            self._stop_event.clear()
            self.stop_btn.config(state="normal")
            self.status_label.config(text="Запущено...")
            self.progress_var.set(0)
            threading.Thread(target=self._run_update_thread, daemon=True).start()
        self.after(0, show_dialog)

    def _stop_update(self):
        self._stop_event.set(); self._log("Остановка...")

    def _run_update_thread(self):
        try:
            ozon, generator = self._build_clients()
        except Exception as exc:
            self._log(f"Ошибка инициализации: {exc}")
            self.after(0, lambda: (self.start_btn.config(state="normal"), self.stop_btn.config(state="disabled"), self.status_label.config(text="Ошибка")))
            return
        cfg = self.config_data; upd = cfg.get("update", {})
        days_back = upd.get("days_back", 30); top_keywords = upd.get("top_keywords", 15)
        target_ids = upd.get("target_offer_ids", [])
        mp_token = cfg.get("mpstats", {}).get("token", "")
        mpstats_client = MpstatsClient(mp_token) if mp_token else None
        if mpstats_client:
            self._log("MPStats подключён — будем искать ключевые слова по главному запросу")
        selected_ids = getattr(self, "_selected_ozon_ids", None)
        self._selected_ozon_ids = None  # reset after use
        self._log("Получаем товары Ozon...")
        try:
            all_ids = ozon.list_all_offer_ids()
        except OzonApiError as exc:
            self._log(f"Ошибка: {exc}")
            self.after(0, lambda: (self.start_btn.config(state="normal"), self.stop_btn.config(state="disabled"), self.status_label.config(text="Ошибка")))
            return
        if selected_ids:
            offer_ids = [o for o in all_ids if o in set(selected_ids)]
        elif target_ids:
            offer_ids = [o for o in all_ids if o in target_ids]
        else:
            offer_ids = all_ids
        self._log(f"Всего: {len(all_ids)}, обрабатываем: {len(offer_ids)}")
        use_preview = self.preview_mode.get(); auto_apply = False
        success = skipped = 0
        for idx, offer_id in enumerate(offer_ids, 1):
            if self._stop_event.is_set():
                self._log("Остановлено"); break
            self._log(f"[{idx}/{len(offer_ids)}] {offer_id}")
            self.after(0, lambda i=idx, t=len(offer_ids), oid=offer_id: (
                self.progress_var.set(i / t * 100),
                self.status_label.config(text=f"{i}/{t}: {oid}")
            ))
            confirm_fn = self._make_confirm_fn() if use_preview and not auto_apply else None
            result = update_product_card(ozon, generator, offer_id, days_back, top_keywords,
                                         log_fn=self._log, confirm_fn=confirm_fn,
                                         mpstats=mpstats_client,
                                         anthropic_api_key=cfg.get("ai", {}).get("anthropic_api_key", ""),
                                         anthropic_model=cfg.get("ai", {}).get("model", "claude-opus-4-8"),
                                         gemini_api_key=cfg.get("ai", {}).get("gemini_api_key", ""),
                                         gemini_model=cfg.get("ai", {}).get("gemini_model", "gemini-2.0-flash"))
            if result == "ok": success += 1
            elif result == "apply_all": success += 1; auto_apply = True
            elif result in ("skip", "skipped"): skipped += 1
            elif result == "skip_all": skipped += 1; break
            time.sleep(2)
        self._log(f"ГОТОВО: {success} применено, {skipped} пропущено")
        self.after(0, lambda: (self.start_btn.config(state="normal"), self.stop_btn.config(state="disabled"),
                               self.status_label.config(text=f"Готово: {success}/{len(offer_ids)}")))

    def _start_scheduler(self):
        t = self.sched_time.get().strip()
        self._log(f"Расписание: каждый понедельник в {t}")
        self.sched_btn.config(state="disabled", text="Расписание активно")
        def _sched():
            schedule.every().monday.at(t).do(self._start_update)
            while True:
                schedule.run_pending(); time.sleep(30)
        threading.Thread(target=_sched, daemon=True).start()

    # ─────────────────── WB ОПИСАНИЕ — ПОТОК ────────────────────────────

    def _start_wb_update(self):
        self._save_settings()
        self.wb_start_btn.config(state="disabled")
        self.wb_status_label.config(text="Загружаем список товаров...")
        threading.Thread(target=self._load_and_select_wb, daemon=True).start()

    def _load_and_select_wb(self):
        key = self.wb_api_key.get().strip()
        if not key:
            self.after(0, lambda: (
                self.wb_start_btn.config(state="normal"),
                self.wb_status_label.config(text="Нет WB API Token"),
            ))
            return
        try:
            wb = WbClient(key)
            cards = wb.get_all_cards()
        except Exception as exc:
            self.after(0, lambda: (
                self.wb_start_btn.config(state="normal"),
                self.wb_status_label.config(text="Ошибка загрузки"),
                messagebox.showerror("Ошибка", f"Не удалось загрузить карточки: {exc}")
            ))
            return
        items = []
        for c in cards:
            nm_id = c.get("nmID") or c.get("nmId")
            vc = c.get("vendorCode") or str(nm_id or "?")
            title = ""
            for sz in (c.get("sizes") or []):
                for ch in (sz.get("characteristics") or []):
                    if ch.get("name") == "Наименование":
                        title = ch.get("value", [""])[0]; break
                if title:
                    break
            label = f"{vc} — {title}" if title else vc
            items.append((nm_id, label))

        def show_dialog():
            dlg = ProductSelectorDialog(self, items, "Выбор карточек WB для обновления описаний")
            if dlg.selected is None:
                self.wb_start_btn.config(state="normal")
                self.wb_status_label.config(text="Отменено")
                return
            if not dlg.selected:
                messagebox.showinfo("Выбор товаров", "Ни одна карточка не выбрана.")
                self.wb_start_btn.config(state="normal")
                self.wb_status_label.config(text="Нет выбранных карточек")
                return
            selected_nm_ids = set(dlg.selected)
            self._selected_wb_cards = [c for c in cards
                                       if (c.get("nmID") or c.get("nmId")) in selected_nm_ids]
            self._wb_stop_event.clear()
            self.wb_stop_btn.config(state="normal")
            self.wb_status_label.config(text="Запущено...")
            self.wb_progress_var.set(0)
            threading.Thread(target=self._run_wb_thread, daemon=True).start()
        self.after(0, show_dialog)

    def _stop_wb_update(self):
        self._wb_stop_event.set(); self._wb_log("Остановка...")

    def _run_wb_thread(self):
        key = self.wb_api_key.get().strip()
        if not key:
            self._wb_log("Нет WB API Token")
            self.after(0, lambda: (self.wb_start_btn.config(state="normal"), self.wb_stop_btn.config(state="disabled"), self.wb_status_label.config(text="Ошибка")))
            return
        wb = WbClient(key)
        # MPStats для ключевых слов
        mp_token = self.config_data.get("mpstats", {}).get("token", "")
        mpstats_client = MpstatsClient(mp_token) if mp_token else None
        if mpstats_client:
            self._wb_log("MPStats подключён — будем использовать поисковые запросы")
        ai_cfg = self.config_data.get("ai", {})
        tab_val = self.wb_ai_provider.get()  # "template" / "gemini" / "claude"
        provider = tab_val if tab_val != "template" else "template"
        generator = self._make_generator(provider, ai_cfg, for_wb=True)
        selected_wb_cards = getattr(self, "_selected_wb_cards", None)
        self._selected_wb_cards = None  # reset after use
        if selected_wb_cards is not None:
            cards = selected_wb_cards
            self._wb_log(f"Обрабатываем выбранные карточки: {len(cards)}")
        else:
            self._wb_log("Получаем карточки WB...")
            try:
                cards = wb.get_all_cards()
            except WbApiError as exc:
                self._wb_log(f"Ошибка: {exc}")
                self.after(0, lambda: (self.wb_start_btn.config(state="normal"), self.wb_stop_btn.config(state="disabled"), self.wb_status_label.config(text="Ошибка")))
                return
            self._wb_log(f"Карточек: {len(cards)}")
        use_preview = self.wb_preview_mode.get(); auto_apply = False
        success = skipped = 0
        for idx, card in enumerate(cards, 1):
            if self._wb_stop_event.is_set():
                self._wb_log("Остановлено"); break
            vc = card.get("vendorCode") or str(card.get("nmID", idx))
            self._wb_log(f"[{idx}/{len(cards)}] {vc}")
            self.after(0, lambda i=idx, t=len(cards), v=vc: (
                self.wb_progress_var.set(i / t * 100),
                self.wb_status_label.config(text=f"{i}/{t}: {v}")
            ))
            confirm_fn = self._make_confirm_fn() if use_preview and not auto_apply else None
            result = update_wb_product_card(wb, generator, card, log_fn=self._wb_log,
                                            confirm_fn=confirm_fn, mpstats=mpstats_client,
                                            anthropic_api_key=ai_cfg.get("anthropic_api_key", ""),
                                            anthropic_model=ai_cfg.get("model", "claude-opus-4-8"),
                                            gemini_api_key=ai_cfg.get("gemini_api_key", ""),
                                            gemini_model=ai_cfg.get("gemini_model", "gemini-2.0-flash"))
            if result == "ok": success += 1
            elif result == "apply_all": success += 1; auto_apply = True
            elif result in ("skip", "skipped"): skipped += 1
            elif result == "skip_all": skipped += 1; break
            time.sleep(1)
        self._wb_log(f"ГОТОВО WB: {success} применено, {skipped} пропущено")
        self.after(0, lambda: (self.wb_start_btn.config(state="normal"), self.wb_stop_btn.config(state="disabled"),
                               self.wb_status_label.config(text=f"WB: {success}/{len(cards)}")))

    # ─────────────────── УПРАВЛЕНИЕ ЦЕНАМИ — ПОТОК ──────────────────────

    def _load_wb_prices_via_mpstats(self):
        """Загружает WB цены через Content API + MPStats, обходя лимит WB Prices API."""
        mp_token = self.config_data.get("mpstats", {}).get("token", "")
        if not mp_token:
            messagebox.showerror("MPStats", "Укажите MPStats Token в Настройках.")
            return
        for pb in (self.prices_pb_ozon, self.prices_pb_wb):
            pb.start(12)
        self._prices_status("Загружаем WB карточки + MPStats цены...")
        threading.Thread(target=self._load_wb_via_mpstats_thread, daemon=True).start()

    def _load_wb_via_mpstats_thread(self):
        cfg = self.config_data
        wb_key = cfg.get("wb", {}).get("api_key", "")
        mp_token = cfg.get("mpstats", {}).get("token", "")

        # Шаг 1: WB Content API — получаем карточки с nmID, vendorCode, subjectName
        self._prices_log("MPStats-путь: загружаем карточки WB...")
        wb_cards: Dict[str, Dict] = {}  # vendorCode -> {nm_id, subject_name, name}
        try:
            if wb_key:
                wb = WbClient(wb_key)
                for card in wb.get_all_cards():
                    vc = str(card.get("vendorCode", ""))
                    nm = card.get("nmID")
                    if vc and nm:
                        wb_cards[vc] = {
                            "nm_id": int(nm),
                            "subject_name": card.get("subjectName") or "",
                            "name": card.get("title") or card.get("name") or vc,
                        }
            self._prices_log(f"WB карточек: {len(wb_cards)}")
        except Exception as exc:
            self._prices_log(f"WB Content API ошибка: {exc}")

        # Шаг 2: MPStats — получаем wallet_price (цена покупателя с СПП)
        self._prices_log(f"MPStats: загружаем цены для {len(wb_cards)} товаров...")
        wb_prices: Dict[str, Dict] = {}
        mp_prices: Dict[str, float] = {}
        mps = MpstatsClient(mp_token)
        done = 0
        for vc, cd in wb_cards.items():
            nm_id = cd["nm_id"]
            raw = mps.get_wb_item(nm_id)
            item_data = raw.get("item", {}) if isinstance(raw, dict) else {}
            wallet_price = float(item_data.get("wallet_price") or 0)
            final_price = float(item_data.get("final_price") or 0)
            base_price = float(item_data.get("price") or 0)
            discount = int(item_data.get("discount") or 0)
            name = item_data.get("name") or cd["name"]
            # wallet_price — цена покупателя; fallback на final_price
            display_price = wallet_price or final_price
            wb_prices[vc] = {
                "nm_id": nm_id,
                "price": display_price,        # показываем в колонке WB
                "wallet_price": display_price,
                "base_price": base_price,
                "discount": discount,
                "subject_name": cd["subject_name"],
                "name": name,
            }
            done += 1
            if done % 10 == 0:
                self._prices_log(f"MPStats: {done}/{len(wb_cards)}")
                self._prices_status(f"MPStats: {done}/{len(wb_cards)}...")

        self._prices_log(f"MPStats: получено {sum(1 for v in wb_prices.values() if v['price'])} цен")

        # Вычисляем среднюю wallet_price по subjectName — это mp_prices (средняя по категории)
        from collections import defaultdict
        subject_wallet: Dict[str, list] = defaultdict(list)
        for vc, wbd in wb_prices.items():
            subj = wbd.get("subject_name", "")
            wlp = wbd.get("wallet_price", 0)
            if subj and wlp:
                subject_wallet[subj].append(wlp)
        subject_avg_price: Dict[str, float] = {
            s: round(sum(v) / len(v)) for s, v in subject_wallet.items()
        }
        for vc, wbd in wb_prices.items():
            subj = wbd.get("subject_name", "")
            if subj and subj in subject_avg_price:
                mp_prices[vc] = subject_avg_price[subj]

        # Шаг 3: Ozon цены
        self._prices_log("Загружаем цены Ozon...")
        ozon_prices: Dict[str, Dict] = {}
        try:
            ozon = OzonClient(OzonCredentials(cfg["ozon"]["client_id"], cfg["ozon"]["api_key"]))
            for item in ozon.get_prices():
                oid = item.get("offer_id", "")
                if not oid:
                    continue
                pr = item.get("price", {}) or {}
                raw_price = (pr.get("marketing_seller_price") or
                             pr.get("price") or "0")
                try:
                    price_val = float(str(raw_price).replace(",", "."))
                except (ValueError, TypeError):
                    price_val = 0.0
                ozon_prices[oid] = {"name": oid, "price": price_val}
            if ozon_prices:
                names = ozon.get_product_names(list(ozon_prices.keys()))
                for oid, name in names.items():
                    if oid in ozon_prices:
                        ozon_prices[oid]["name"] = name
            self._prices_log(f"Ozon: {len(ozon_prices)} товаров")
        except Exception as exc:
            self._prices_log(f"Ошибка Ozon: {exc}")

        # Сохраняем и заполняем таблицу
        self._ozon_prices_data = ozon_prices
        self._wb_prices_data = wb_prices
        self._mp_prices_data = mp_prices
        self._fill_prices_table(ozon_prices, wb_prices, mp_prices)

    def _fill_prices_table(self, ozon_prices, wb_prices, mp_prices):
        """Общий метод заполнения таблицы цен."""
        all_keys = set(ozon_prices) | set(wb_prices)
        rows = []
        for k in sorted(all_keys):
            o = ozon_prices.get(k, {})
            w = wb_prices.get(k, {})
            op = o.get("price", 0)
            # wallet_price — цена покупателя с СПП; fallback на price
            wp = w.get("wallet_price") or w.get("price", 0)
            mp = mp_prices.get(k, 0)
            name = o.get("name") or w.get("name", k)
            if op and wp:
                diff = round((wp - op) / op * 100, 1) if op else 0
                status = "Совпадают" if abs(diff) < 1 else "Расхождение"
                tag = "equal" if abs(diff) < 1 else "diff"
            else:
                diff = 0
                status = "Только Ozon" if op else "Только WB"
                tag = "missing"
            rows.append((k, name[:40], f"{op:.0f}" if op else "—",
                         f"{wp:.0f}" if wp else "—",
                         f"{mp:.0f}" if mp else "—",
                         f"{diff:+.1f}%" if (op and wp) else "—",
                         status, tag))

        def _fill(tree):
            tree.delete(*tree.get_children())
            for r in rows:
                tree.insert("", "end", values=r[:-1], tags=(r[-1],))

        self.after(0, lambda: _fill(self.prices_tree_ozon))
        self.after(0, lambda: _fill(self.prices_tree_wb))
        self.after(0, lambda: self.prices_pb_ozon.stop())
        self.after(0, lambda: self.prices_pb_wb.stop())
        msg = (f"Загружено: Ozon={len(ozon_prices)}, WB={len(wb_prices)}, "
               f"совпадений={sum(1 for r in rows if r[-1] != 'missing')}")
        self.after(0, lambda: self.prices_status_ozon.config(text=msg, foreground="black"))
        self.after(0, lambda: self.prices_status_wb.config(text=msg, foreground="black"))
        self._prices_log(msg)

    def _load_prices(self):
        for pb in (self.prices_pb_ozon, self.prices_pb_wb):
            pb.start(12)
        for sl in (self.prices_status_ozon, self.prices_status_wb):
            sl.config(text="Загрузка...", foreground="gray")
        threading.Thread(target=self._load_prices_thread, daemon=True).start()

    def _prices_status(self, msg: str, color: str = "gray"):
        self.after(0, lambda: self.prices_status_ozon.config(text=msg, foreground=color))
        self.after(0, lambda: self.prices_status_wb.config(text=msg, foreground=color))

    def _load_prices_thread(self):
        cfg = self.config_data
        self._prices_log("Загружаем цены Ozon...")
        self._prices_status("Загружаем цены Ozon...")
        ozon_prices: Dict[str, Dict] = {}
        try:
            ozon = OzonClient(OzonCredentials(cfg["ozon"]["client_id"], cfg["ozon"]["api_key"]))
            raw_items = ozon.get_prices()
            if raw_items:
                self._prices_log(f"Ozon: {len(raw_items)} товаров")
            else:
                self._prices_log("Ozon: 0 товаров")
            logged_first = False
            for item in raw_items:
                oid = item.get("offer_id", "")
                if not oid:
                    continue
                pr = item.get("price", {}) or {}
                if not logged_first:
                    logging.info(f"[Ozon prices] первый price-объект: {pr}")
                    logged_first = True
                if oid in ("3497207411", "3497207412", "3497207413"):
                    logging.info(f"[Ozon debug {oid}] price-объект: {pr}")
                # marketing_price — цена для покупателя с учётом скидки Ozon (эластичность)
                # marketing_seller_price — цена после акций продавца
                # price — базовая цена продавца
                mp_raw = pr.get("marketing_price")
                try:
                    mp_val = float(str(mp_raw).replace(",", ".")) if mp_raw else 0.0
                except (ValueError, TypeError):
                    mp_val = 0.0
                has_marketing_price = mp_val > 0
                raw_price = mp_raw if has_marketing_price else (
                    pr.get("marketing_seller_price") or pr.get("price") or "0")
                try:
                    price_val = float(str(raw_price).replace(",", "."))
                except (ValueError, TypeError):
                    price_val = 0.0
                seller_raw = pr.get("marketing_seller_price") or pr.get("price") or "0"
                try:
                    seller_val = float(str(seller_raw).replace(",", "."))
                except (ValueError, TypeError):
                    seller_val = price_val
                ozon_prices[oid] = {
                    "name": oid,
                    "price": price_val,
                    "seller_price": seller_val,
                    "sku": None,
                    "has_marketing_price": has_marketing_price,
                }
            self._prices_log(f"Ozon: {len(ozon_prices)} товаров, загружаем названия и SKU...")
            if ozon_prices:
                names = ozon.get_product_names(list(ozon_prices.keys()))
                for oid, name in names.items():
                    if oid in ozon_prices:
                        ozon_prices[oid]["name"] = name
                # получаем fbo_sku для MPStats через /v2/product/info/list
                sku_map = ozon.get_fbo_skus_bulk(list(ozon_prices.keys()))
                for oid, sku in sku_map.items():
                    if oid in ozon_prices:
                        ozon_prices[oid]["sku"] = sku
                self._prices_log(f"Ozon: получено SKU для {len(sku_map)}/{len(ozon_prices)} товаров")
        except Exception as exc:
            import traceback
            self._prices_log(f"Ошибка Ozon: {exc}")
            self._prices_log(traceback.format_exc()[-500:])

        # MPStats: получаем цену покупателя для Ozon (final_price / wallet_price)
        mp_token = cfg.get("mpstats", {}).get("token", "")
        if mp_token and ozon_prices:
            items_with_sku = [(oid, d["sku"]) for oid, d in ozon_prices.items() if d.get("sku")]
            if items_with_sku:
                self._prices_log(f"MPStats Ozon: запрашиваем цены покупателя для {len(items_with_sku)} товаров...")
                try:
                    mps = MpstatsClient(mp_token)
                    mp_got = 0
                    for oid, sku in items_with_sku:
                        raw = mps.get_ozon_item(int(sku))
                        if isinstance(raw, dict):
                            item_data = raw.get("item", raw)
                        elif isinstance(raw, list) and raw:
                            item_data = raw[-1].get("item", raw[-1]) if isinstance(raw[-1], dict) else {}
                        else:
                            item_data = {}
                        wallet_price = float(item_data.get("wallet_price") or 0)
                        final_price = float(item_data.get("final_price") or 0)
                        mp_price = float(item_data.get("price") or 0)
                        # для Ozon: final_price — реальная цена покупателя после скидки Ozon
                        # wallet_price может отсутствовать или быть равен базовой цене
                        buyer_price = final_price or wallet_price
                        already_exact = ozon_prices[oid].get("has_marketing_price", False)
                        logging.info(
                            f"[Ozon MPStats {sku}] price={mp_price} final={final_price} "
                            f"wallet={wallet_price} → buyer={buyer_price} "
                            f"(seller={ozon_prices[oid].get('seller_price')}) "
                            f"exact_from_ozon={already_exact}"
                        )
                        if buyer_price and not already_exact:
                            # Используем MPStats только если Ozon API не вернул marketing_price
                            ozon_prices[oid]["price"] = buyer_price
                            mp_got += 1
                        elif already_exact:
                            # marketing_price из Ozon API точнее устаревших данных MPStats
                            pass
                        time.sleep(0.25)
                    self._prices_log(f"MPStats Ozon: получено цен покупателя: {mp_got}/{len(items_with_sku)}")
                except Exception as exc:
                    self._prices_log(f"MPStats Ozon ошибка: {exc}")
            else:
                self._prices_log("MPStats Ozon: SKU не найдены в ответе Ozon API, используем цены продавца")

        self._prices_log("Загружаем цены WB...")
        self._prices_status("Загружаем цены WB...")
        wb_prices: Dict[str, Dict] = {}
        wb_rate_limited = False
        try:
            wb_key = cfg.get("wb", {}).get("api_key", "")
            if wb_key:
                wbp = WbPricesClient(wb_key)
                # max_attempts=2: при 429 ждём 60 сек и повторяем; при повторной ошибке → MPStats
                raw_goods = wbp.get_goods(log_fn=self._prices_log, max_attempts=2)
                if raw_goods is None:
                    wb_rate_limited = True
                    self._prices_log("WB Prices API: 429 — переключаемся на WB Content API + MPStats...")
                else:
                    self._prices_log(f"WB get_goods: {len(raw_goods)} записей")
                    for g in raw_goods:
                        vc = str(g.get("vendorCode") or g.get("nmID", ""))
                        sizes = g.get("sizes") or []
                        base_price = 0.0
                        buyer_price = 0.0
                        discount = int(g.get("discount") or 0)
                        if sizes:
                            s0 = sizes[0]
                            base_price = float(s0.get("price", 0) or 0)
                            # discountedPrice = цена без скидки карты (обычный покупатель)
                            # clubDiscountedPrice = цена с WB-картой (дешевле)
                            # Показываем цену обычного покупателя, а не клубную
                            buyer_price = float(
                                s0.get("discountedPrice") or
                                s0.get("clubDiscountedPrice") or
                                s0.get("price") or 0
                            )
                            discount = int(s0.get("discount") or g.get("discount") or 0)
                        if g.get("nmID") == 803486780:
                            logging.info(
                                f"[WB API debug nm=803486780] vc={vc!r} "
                                f"sizes={sizes!r} base={base_price} buyer={buyer_price}"
                            )
                        wb_prices[vc] = {
                            "nm_id": g.get("nmID"),
                            "price": buyer_price if buyer_price else base_price,
                            "wallet_price": buyer_price,
                            "base_price": base_price,
                            "discount": discount,
                            "subject_name": "",
                            "has_wb_api_price": buyer_price > 0,
                        }
                    self._prices_log(f"WB: {len(wb_prices)} товаров")
            else:
                self._prices_log("WB: токен не указан")
        except Exception as exc:
            import traceback
            self._prices_log(f"Ошибка WB: {exc}")
            self._prices_log(traceback.format_exc()[-500:])

        # MPStats — wallet_price (цена покупателя с СПП)
        mp_names: Dict[str, str] = {}
        mp_token = cfg.get("mpstats", {}).get("token", "")

        # Если WB Prices API вернул 429 — получаем nmID из WB Content API (без лимитов)
        if wb_rate_limited:
            try:
                wb_key = cfg.get("wb", {}).get("api_key", "")
                if wb_key:
                    wb_client = WbClient(wb_key)
                    cards = wb_client.get_all_cards()
                    for card in cards:
                        vc = str(card.get("vendorCode", ""))
                        nm = card.get("nmID")
                        if vc and nm:
                            wb_prices[vc] = {
                                "nm_id": nm,
                                "price": 0,
                                "wallet_price": 0,
                                "base_price": 0,
                                "discount": 0,
                                "subject_name": card.get("subjectName") or "",
                            }
                    logging.info(f"[WB fallback] Content API: {len(wb_prices)} карточек")
                    self._prices_log(f"WB Content API: {len(wb_prices)} карточек (цены получим через MPStats)")
            except Exception as exc:
                logging.info(f"[WB fallback] Content API ошибка: {exc}")
                self._prices_log(f"WB Content API ошибка: {exc}")

        if mp_token and wb_prices:
            self._prices_log(f"Загружаем цены MPStats для {len(wb_prices)} товаров WB...")
            try:
                mps = MpstatsClient(mp_token)
                done = 0
                for vc, wbd in wb_prices.items():
                    nm = wbd.get("nm_id")
                    if not nm:
                        continue
                    raw = mps.get_wb_item(int(nm))
                    # MPStats возвращает {"item": {...}}
                    if isinstance(raw, dict):
                        item_data = raw.get("item", raw)
                    elif isinstance(raw, list) and raw:
                        item_data = raw[-1].get("item", raw[-1]) if isinstance(raw[-1], dict) else {}
                    else:
                        item_data = {}
                    wallet_price = float(item_data.get("wallet_price") or 0)
                    final_price = float(item_data.get("final_price") or 0)
                    base_price = float(item_data.get("price") or 0)
                    discount = int(item_data.get("discount") or 0)
                    # final_price = обычный покупатель; wallet_price = с WB-картой (дешевле)
                    display_price = final_price or wallet_price
                    if "Brewers" in vc or "brewers" in vc.lower() or "drsara" in vc.lower().replace(".", "").replace(" ", "") or "260" in vc:
                        already_exact_wb = wb_prices[vc].get("has_wb_api_price", False)
                        logging.info(
                            f"[WB debug {vc}] nm={nm} mpstats_base={base_price} "
                            f"mpstats_final={final_price} mpstats_wallet={wallet_price} "
                            f"→ display={display_price} "
                            f"(wb_api_price={wb_prices[vc].get('price')} "
                            f"has_wb_api_price={already_exact_wb})"
                        )
                    already_exact_wb = wb_prices[vc].get("has_wb_api_price", False)
                    if display_price and not already_exact_wb:
                        # MPStats только если WB Prices API не вернул цену для этого товара
                        wb_prices[vc]["wallet_price"] = display_price
                        wb_prices[vc]["price"] = display_price
                    elif already_exact_wb:
                        # WB API цена точнее устаревших данных MPStats — пропускаем
                        pass
                    if base_price:
                        wb_prices[vc]["base_price"] = base_price
                    if discount:
                        wb_prices[vc]["discount"] = discount
                    name = item_data.get("name") or item_data.get("full_name", "")
                    if name:
                        mp_names[vc] = name
                    done += 1
                    if done % 10 == 0:
                        self._prices_log(f"MPStats: {done}/{len(wb_prices)}...")
                self._prices_log(f"MPStats: получено {sum(1 for v in wb_prices.values() if v.get('wallet_price'))} цен")
            except Exception as exc:
                self._prices_log(f"MPStats ошибка: {exc}")

        # WB Public Card API — фактические цены покупателей с учётом акций WB
        # card.wb.ru возвращает salePriceU = цена покупателя после всех скидок (в рублях × 100)
        if wb_prices:
            nm_to_vc: Dict[int, str] = {}
            for vc, wbd in wb_prices.items():
                nm = wbd.get("nm_id")
                if nm:
                    nm_to_vc[int(nm)] = vc
            if nm_to_vc:
                self._prices_log(f"WB Public API: загружаем реальные цены покупателей ({len(nm_to_vc)} товаров)...")
                try:
                    import requests as _req
                    nm_list = list(nm_to_vc.keys())
                    chunk_size = 100
                    pub_got = 0
                    for i in range(0, len(nm_list), chunk_size):
                        chunk = nm_list[i:i + chunk_size]
                        nm_param = ";".join(str(x) for x in chunk)
                        try:
                            r = _req.get(
                                "https://card.wb.ru/cards/v2/detail",
                                params={"appType": 1, "curr": "rub", "dest": -1257786, "nm": nm_param},
                                timeout=15,
                                headers={"User-Agent": "Mozilla/5.0"},
                            )
                            if r.status_code == 200:
                                products = r.json().get("data", {}).get("products", []) or []
                                for prod in products:
                                    nm = prod.get("id")
                                    sale_u = prod.get("salePriceU", 0)
                                    if nm and sale_u and nm in nm_to_vc:
                                        actual = round(sale_u / 100.0, 2)
                                        vc = nm_to_vc[nm]
                                        if nm == 803486780:
                                            logging.info(
                                                f"[WB public nm=803486780] salePriceU={sale_u} → {actual} руб"
                                            )
                                        wb_prices[vc]["price"] = actual
                                        wb_prices[vc]["wallet_price"] = actual
                                        pub_got += 1
                        except Exception as exc_chunk:
                            logging.info(f"[WB public card chunk] {exc_chunk}")
                    self._prices_log(f"WB Public API: обновлено {pub_got}/{len(nm_to_vc)} цен")
                except Exception as exc:
                    self._prices_log(f"WB Public API ошибка: {exc}")

        # Заполняем названия WB из mpstats если нет своих
        for vc, name in mp_names.items():
            if vc in wb_prices and not wb_prices[vc].get("name"):
                wb_prices[vc]["name"] = name

        # Вычисляем среднюю wallet_price по subjectName — колонка MPStats
        from collections import defaultdict
        subject_wallet: Dict[str, list] = defaultdict(list)
        for vc, wbd in wb_prices.items():
            subj = wbd.get("subject_name", "")
            wlp = wbd.get("wallet_price", 0)
            if subj and wlp:
                subject_wallet[subj].append(wlp)
        subject_avg_price: Dict[str, float] = {
            s: round(sum(v) / len(v)) for s, v in subject_wallet.items()
        }
        mp_prices: Dict[str, float] = {}
        for vc, wbd in wb_prices.items():
            subj = wbd.get("subject_name", "")
            if subj and subj in subject_avg_price:
                mp_prices[vc] = subject_avg_price[subj]

        # сохраняем и заполняем таблицу
        self._ozon_prices_data = ozon_prices
        self._wb_prices_data = wb_prices
        self._mp_prices_data = mp_prices
        self._fill_prices_table(ozon_prices, wb_prices, mp_prices)

    def _sync_prices(self, mode: str):
        if not hasattr(self, "_ozon_prices_data"):
            messagebox.showinfo("Цены", "Сначала нажмите 'Загрузить и сравнить'.")
            return
        msg = {
            "ozon_from_wb": "Обновить цены Ozon по данным WB?",
            "wb_from_ozon": "Обновить цены WB по данным Ozon?",
            "both_from_ozon": "Обновить цены WB, чтобы они совпали с Ozon?",
            "ozon_from_mpstats": "Обновить цены Ozon по средним ценам MPStats?",
            "wb_from_mpstats": "Обновить цены WB по средним ценам MPStats?",
        }.get(mode, "Синхронизировать цены?")
        if not messagebox.askyesno("Подтверждение", msg):
            return
        threading.Thread(target=self._sync_prices_thread, args=(mode,), daemon=True).start()

    def _sync_prices_thread(self, mode: str):
        cfg = self.config_data
        ozon_data = getattr(self, "_ozon_prices_data", {})
        wb_data = getattr(self, "_wb_prices_data", {})
        mp_data = getattr(self, "_mp_prices_data", {})

        try:
            if mode in ("ozon_from_wb", "ozon_from_mpstats", "both_from_ozon"):
                ozon = OzonClient(OzonCredentials(cfg["ozon"]["client_id"], cfg["ozon"]["api_key"]))

            if mode in ("wb_from_ozon", "wb_from_mpstats", "both_from_ozon"):
                wb_key = cfg.get("wb", {}).get("api_key", "")
                if not wb_key:
                    self._prices_log("Ошибка: нет WB токена")
                    return
                wbp = WbPricesClient(wb_key)

            def _wb_base_price(target_buyer: float, wb_entry: dict) -> tuple:
                """
                Вычисляет (base_price, discount) для WB чтобы покупатель видел target_buyer.
                Использует соотношение wallet_price/base_price из MPStats.
                """
                wallet = wb_entry.get("wallet_price", 0)
                base = wb_entry.get("base_price", 0)
                discount = wb_entry.get("discount", 0)
                if wallet and base and base > wallet:
                    # коэффициент: сколько от базовой цены составляет цена покупателя
                    ratio = wallet / base
                    new_base = max(1, round(target_buyer / ratio))
                    return new_base, discount
                # fallback: если нет данных MPStats, просто выставляем target напрямую
                return max(1, round(target_buyer)), discount

            updated = 0
            all_keys = set(ozon_data) | set(wb_data)

            ozon_updates: List[Dict] = []
            wb_updates: List[Dict] = []

            for k in all_keys:
                op = ozon_data.get(k, {}).get("price", 0)
                # wallet_price — цена покупателя, именно её сравниваем
                wp = wb_data.get(k, {}).get("wallet_price") or wb_data.get(k, {}).get("price", 0)
                mp = mp_data.get(k, 0)

                if mode == "ozon_from_wb" and wp and k in ozon_data:
                    ozon_updates.append({"offer_id": k, "price": str(int(wp)), "min_price": "0", "old_price": "0"})
                    updated += 1
                elif mode == "wb_from_ozon" and op and k in wb_data:
                    nm = wb_data[k].get("nm_id")
                    if nm:
                        new_base, disc = _wb_base_price(op, wb_data[k])
                        wb_updates.append({"nmID": nm, "price": new_base, "discount": disc})
                        updated += 1
                elif mode == "both_from_ozon" and op and k in wb_data:
                    nm = wb_data[k].get("nm_id")
                    if nm:
                        new_base, disc = _wb_base_price(op, wb_data[k])
                        wb_updates.append({"nmID": nm, "price": new_base, "discount": disc})
                        updated += 1
                elif mode == "ozon_from_mpstats" and mp and k in ozon_data:
                    ozon_updates.append({"offer_id": k, "price": str(int(mp)), "min_price": "0", "old_price": "0"})
                    updated += 1
                elif mode == "wb_from_mpstats" and mp and k in wb_data:
                    nm = wb_data[k].get("nm_id")
                    if nm:
                        new_base, disc = _wb_base_price(mp, wb_data[k])
                        wb_updates.append({"nmID": nm, "price": new_base, "discount": disc})
                        updated += 1

            overlap = set(ozon_data) & set(wb_data)
            self._prices_log(
                f"Совпадений артикулов Ozon↔WB: {len(overlap)}/{len(all_keys)} "
                f"(Ozon={len(ozon_data)}, WB={len(wb_data)})"
            )
            if updated == 0:
                self._prices_log("Нет позиций для обновления — артикулы Ozon и WB не совпадают")

            # Отправляем Ozon батчами по 100
            if ozon_updates:
                self._prices_log(f"Обновляем {len(ozon_updates)} цен Ozon...")
                chunk = 100
                for i in range(0, len(ozon_updates), chunk):
                    ozon.update_prices(ozon_updates[i:i + chunk])
                    time.sleep(0.5)

            # Отправляем WB одним запросом (API принимает до 1000 nmID)
            if wb_updates:
                self._prices_log(f"Обновляем {len(wb_updates)} цен WB батчем...")
                chunk = 500
                for i in range(0, len(wb_updates), chunk):
                    wbp.update_prices(wb_updates[i:i + chunk])
                    if i + chunk < len(wb_updates):
                        time.sleep(2)

            self._prices_log(f"Синхронизация завершена: обновлено {updated} позиций")
            self.after(0, lambda: messagebox.showinfo("Цены", f"Обновлено позиций: {updated}"))
            self._load_prices()

        except Exception as exc:
            msg = str(exc)
            self._prices_log(f"Ошибка синхронизации: {msg}")
            self.after(0, lambda m=msg: messagebox.showerror("Ошибка", m))


# ============================================================================
# ТОЧКА ВХОДА
# ============================================================================

if __name__ == "__main__":
    app = App()
    app.mainloop()
