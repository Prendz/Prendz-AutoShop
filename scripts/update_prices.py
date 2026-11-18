"""Robô de preços da Prendz.

Roda no GitHub Actions. Para cada produto com link de afiliado em data/products.json:
1. Descobre o ID do produto no Mercado Livre (seguindo o link meli.la), uma única vez.
2. Consulta o preço atual na API oficial do Mercado Livre.
3. Grava preço, preço anterior e horário em data/products.json e o histórico em data/history.json.

Credenciais (opcionais no primeiro teste) vêm dos segredos do repositório:
ML_CLIENT_ID e ML_CLIENT_SECRET.
"""
import json
import os
import re
import sys
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PRODUCTS = os.path.join(ROOT, "data", "products.json")
HISTORY = os.path.join(ROOT, "data", "history.json")
API = "https://api.mercadolibre.com"
UA = {"User-Agent": "Mozilla/5.0 (PrendzPriceBot)", "Accept": "application/json"}
ID_RE = re.compile(r"MLB-?\d{6,}")
CATALOG_RE = re.compile(r"/p/(MLB\d{6,})")


def now_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def http(url, headers=None, data=None):
    req = urllib.request.Request(url, headers={**UA, **(headers or {})}, data=data)
    with urllib.request.urlopen(req, timeout=25) as r:
        return r.geturl(), r.read().decode("utf-8", "replace")


def get_token():
    cid, secret = os.environ.get("ML_CLIENT_ID"), os.environ.get("ML_CLIENT_SECRET")
    if not cid or not secret:
        return None
    body = urllib.parse.urlencode({"grant_type": "client_credentials", "client_id": cid, "client_secret": secret}).encode()
    try:
        _, txt = http(API + "/oauth/token", {"Content-Type": "application/x-www-form-urlencoded"}, body)
        return json.loads(txt).get("access_token")
    except Exception as e:  # noqa: BLE001
        print(f"[token] falhou: {e}")
        return None


def resolve_id(url):
    """Segue o link meli.la e acha o ID do produto (catálogo /p/MLB... ou anúncio MLB-...)."""
    final, html = http(url, {"Accept": "text/html"})
    for text in (final, html):
        m = CATALOG_RE.search(text)
        if m:
            return {"kind": "product", "id": m.group(1)}
    for text in (final, html):
        m = ID_RE.search(text)
        if m:
            return {"kind": "item", "id": m.group(0).replace("-", "")}
    return None


def fetch_price(ref, token):
    auth = {"Authorization": f"Bearer {token}"} if token else {}
    if ref["kind"] == "product":
        _, txt = http(f"{API}/products/{ref['id']}", auth)
        d = json.loads(txt)
        w = d.get("buy_box_winner") or {}
        return w.get("price"), w.get("original_price")
    _, txt = http(f"{API}/items/{ref['id']}", auth)
    d = json.loads(txt)
    return d.get("price"), d.get("original_price")


def main():
    data = json.load(open(PRODUCTS, encoding="utf-8"))
    history = json.load(open(HISTORY, encoding="utf-8")) if os.path.exists(HISTORY) else {}
    token = get_token()
    print(f"[token] {'ok' if token else 'sem credencial (teste público)'}")
    cutoff = datetime.now(timezone.utc) - timedelta(days=30)
    ok = fail = 0

    for cat in data["categories"]:
        for it in cat["items"]:
            if not it.get("url"):
                continue
            name = it["n"]
            try:
                if not it.get("ml_id"):
                    ref = resolve_id(it["url"])
                    if not ref:
                        raise RuntimeError("não achei o ID do produto no link")
                    it["ml_id"], it["ml_kind"] = ref["id"], ref["kind"]
                price, original = fetch_price({"id": it["ml_id"], "kind": it.get("ml_kind", "item")}, token)
                if not price:
                    raise RuntimeError("API não retornou preço")
                it["price"], it["original_price"] = float(price), (float(original) if original else None)
                it["price_note"], it["price_source"], it["checked_at"] = "", "api", now_iso()

                h = [p for p in history.get(it["ml_id"], []) if datetime.fromisoformat(p["t"].replace("Z", "+00:00")) > cutoff]
                h.append({"t": it["checked_at"], "p": it["price"]})
                history[it["ml_id"]] = h
                prices = [p["p"] for p in h]
                it["badge"] = "Menor preço em 30 dias" if len(prices) >= 24 and it["price"] <= min(prices) else None
                ok += 1
                print(f"[ok] {name}: R$ {it['price']:.2f}")
            except Exception as e:  # noqa: BLE001
                fail += 1
                print(f"[falha] {name}: {e}")

    data["updated_at"] = now_iso()
    json.dump(data, open(PRODUCTS, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    json.dump(history, open(HISTORY, "w", encoding="utf-8"), ensure_ascii=False)
    print(f"Resumo: {ok} atualizados, {fail} com falha")
    if ok == 0 and fail > 0:
        sys.exit(1)


if __name__ == "__main__":
    main()
