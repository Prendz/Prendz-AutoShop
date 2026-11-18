"""Robô de preços da Prendz.

Roda no GitHub Actions, uma vez por hora. Para cada produto com link de afiliado em data/products.json:
1. Descobre o ID do produto no Mercado Livre seguindo o link meli.la (só na primeira vez).
2. Lê o preço atual: pela API oficial, se houver credencial (ML_CLIENT_ID / ML_CLIENT_SECRET),
   senão pela página pública do produto.
3. Grava preço, preço "de", selo de evento (ex.: Black Friday) e horário em data/products.json,
   e o histórico de 30 dias em data/history.json.
"""
import html as htmllib
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
BROWSER = {
    "User-Agent": "Mozilla/5.0 (Linux; Android 14) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/129.0 Mobile Safari/537.36",
    "Accept-Language": "pt-BR,pt;q=0.9",
}
ID_RE = re.compile(r"MLB-?\d{6,}")
CATALOG_RE = re.compile(r"/p/(MLB\d{6,})")
EVENTS = ["Black Friday", "Cyber Monday", "Oferta relâmpago", "Oferta do dia"]


def now_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def http(url, headers=None, data=None):
    req = urllib.request.Request(url, headers={**BROWSER, **(headers or {})}, data=data)
    with urllib.request.urlopen(req, timeout=30) as r:
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


def resolve(url):
    """Segue o link meli.la e acha o ID do produto (catálogo /p/MLB... ou anúncio MLB-...)."""
    final, page = http(url, {"Accept": "text/html"})
    for text in (final, page):
        m = CATALOG_RE.search(text)
        if m:
            return "product", m.group(1)
    for text in (final, page):
        m = ID_RE.search(text)
        if m:
            return "item", m.group(0).replace("-", "")
    return None, None


def page_url(kind, mlid):
    if kind == "product":
        return f"https://www.mercadolivre.com.br/p/{mlid}"
    return f"https://produto.mercadolivre.com.br/MLB-{mlid[3:]}"


def money(s):
    return float(s.replace(".", "").replace(",", ".")) if "," in s else float(s)


def from_page(kind, mlid):
    _, page = http(page_url(kind, mlid), {"Accept": "text/html"})
    price = original = None
    m = re.search(r'<meta itemprop="price" content="([\d.]+)"', page)
    if m:
        price = float(m.group(1))
    if price is None:
        m = re.search(r'"offers"\s*:\s*\{[^}]*?"price"\s*:\s*"?([\d.]+)', page)
        if m:
            price = float(m.group(1))
    m = re.search(r'"original_price"\s*:\s*([\d.]+)', page)
    if m:
        original = float(m.group(1))
    if original is None:
        m = re.search(r'aria-label="Antes:\s*([\d.]+)\s*reais(?:\s*com\s*(\d+)\s*centavos)?', page)
        if m:
            original = money(m.group(1)) + (int(m.group(2)) / 100 if m.group(2) else 0)
    pix = None
    m = re.search(r'"price"\s*:\s*\{[^{}]*?"value"\s*:\s*([\d.]+)[^{}]*?\}[^{}]{0,400}?[Pp]ix', page)
    if m:
        pix = float(m.group(1))
    text = htmllib.unescape(page)
    event = next((e for e in EVENTS if re.search(r'"(?:text|label)"\s*:\s*"' + e.upper() + '"', text)
                  or re.search(r'>\s*' + e.upper() + r'\s*<', text)), None)
    return price, original, event, pix


def from_api(kind, mlid, token):
    auth = {"Authorization": f"Bearer {token}"}
    _, txt = http(f"{API}/{'products' if kind == 'product' else 'items'}/{mlid}", auth)
    d = json.loads(txt)
    if kind == "product":
        d = d.get("buy_box_winner") or {}
    return d.get("price"), d.get("original_price"), None, None


def main():
    data = json.load(open(PRODUCTS, encoding="utf-8"))
    history = json.load(open(HISTORY, encoding="utf-8")) if os.path.exists(HISTORY) else {}
    token = get_token()
    print(f"[fonte] {'API oficial' if token else 'página pública do produto'}")
    cutoff = datetime.now(timezone.utc) - timedelta(days=30)
    ok = fail = 0
    report = []

    for cat in data["categories"]:
        for it in cat["items"]:
            if not it.get("url"):
                continue
            try:
                if not it.get("ml_id"):
                    kind, mlid = resolve(it["url"])
                    if not mlid:
                        raise RuntimeError("não achei o ID do produto no link")
                    it["ml_id"], it["ml_kind"] = mlid, kind
                kind = it.get("ml_kind", "item")
                price, original, event, pix = from_api(kind, it["ml_id"], token) if token else from_page(kind, it["ml_id"])
                if not price:
                    raise RuntimeError("preço não encontrado na página")
                it["price"] = float(price)
                it["original_price"] = float(original) if original and float(original) > float(price) else None
                it["pix_price"] = pix if pix and pix < it["price"] else None
                it["event"] = event
                it["price_source"], it["checked_at"] = ("api" if token else "page"), now_iso()

                h = [p for p in history.get(it["ml_id"], []) if datetime.fromisoformat(p["t"].replace("Z", "+00:00")) > cutoff]
                h.append({"t": it["checked_at"], "p": it["price"]})
                history[it["ml_id"]] = h
                prices = [p["p"] for p in h]
                it["lowest_30d"] = len(prices) >= 24 and it["price"] <= min(prices)
                ok += 1
                report.append((it["n"], it.get("print_price"), it["price"], it.get("print_original"), it["original_price"], it.get("print_event"), event, it["ml_kind"]))
            except Exception as e:  # noqa: BLE001
                fail += 1
                print(f"[falha] {it['n']}: {e}")

    print("\nCOMPARATIVO (print x robô)")
    for r in report:
        print(f"{r[0]} | tipo {r[7]} | preço print {r[1]} robô {r[2]} | de print {r[3]} robô {r[4]} | evento print {r[5]} robô {r[6]}")
    data["updated_at"] = now_iso()
    json.dump(data, open(PRODUCTS, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    json.dump(history, open(HISTORY, "w", encoding="utf-8"), ensure_ascii=False)
    print(f"\nResumo: {ok} atualizados, {fail} com falha")
    if ok == 0 and fail > 0:
        sys.exit(1)


if __name__ == "__main__":
    main()
