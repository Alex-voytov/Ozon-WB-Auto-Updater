import json
from ozon_auto_updater import OzonClient, ProductSnapshot

with open("config.json", encoding="utf-8") as f:
    cfg = json.load(f)

from ozon_auto_updater import OzonCredentials
creds = OzonCredentials(cfg["ozon"]["client_id"], cfg["ozon"]["api_key"])
ozon = OzonClient(creds)

# Берём несколько товаров и смотрим что сейчас лежит в атрибуте хештегов
products = ozon._post("/v3/product/list", {"filter": {}, "last_id": "", "limit": 10})
items = products.get("result", {}).get("items", [])

for item in items[:5]:
    offer_id = item.get("offer_id", "")
    try:
        snap = ozon.build_snapshot(offer_id)
        hashtag_attr_id = ozon.find_hashtags_attribute_id(snap.description_category_id, snap.type_id)
        if not hashtag_attr_id:
            continue
        for attr in snap.current_attributes:
            if attr.get("id") == hashtag_attr_id:
                vals = attr.get("values", [])
                if vals:
                    print(f"\n{offer_id} ({snap.name[:40]})")
                    print(f"  attr_id={hashtag_attr_id}")
                    for v in vals:
                        print(f"  -> '{v.get('value', '')}'")
    except Exception as e:
        print(f"{offer_id}: ERR {e}")
