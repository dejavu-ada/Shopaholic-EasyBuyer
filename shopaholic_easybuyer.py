"""Shopaholic EasyBuyer

The application turns a natural-language shopping request into a short list of
product recommendations. Its main pipeline is:

1. Parse the requested product, budget, and currency with OpenRouter when
   configured, falling back to local parsing when the service is unavailable.
2. Retrieve listings from BuyWhere, or load the local CSV snapshot when live
   search returns no usable results.
3. Normalize listings, reject mismatched or unavailable products, enforce
   budget constraints, and check candidate purchase pages.
4. Return up to five recommendations through the JSON API and record
   accept/reject feedback in a local JSON Lines file.

The embedded HTML template provides the browser interface; Flask routes and
the recommendation pipeline are kept in this file for the project prototype.
API keys and file paths are read from environment variables. Running this file
starts the local development server; ``--refresh-snapshot`` writes a separate
timestamped CSV and does not replace the configured snapshot.
"""

import csv
import argparse
from concurrent.futures import ThreadPoolExecutor
import json
import os
import re
import time
import uuid
from datetime import datetime
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import requests
from flask import Flask, jsonify, render_template_string, request

# Configuration and shared runtime state -----------------------------------
BASE_DIR = Path(__file__).resolve().parent
PRODUCTS_CSV = Path(os.getenv("PRODUCTS_CSV", BASE_DIR / "data" / "products_snapshot.csv"))
FEEDBACK_FILE = Path(os.getenv("FEEDBACK_FILE", BASE_DIR / "shopping_feedback.jsonl"))
EVALUATION_LOG_ENABLED = os.getenv("EVALUATION_LOG_ENABLED", "").strip().lower() in {"1", "true", "yes"}
EVALUATION_LOG_FILE = Path(os.getenv("EVALUATION_LOG_FILE", BASE_DIR / "evals" / "evaluation_runs.jsonl"))
OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY", "")
BUYWHERE_API_KEY = os.getenv("BUYWHERE_API_KEY", "")
OPENROUTER_MODEL = os.getenv("OPENROUTER_MODEL", "openai/gpt-5-mini")
BUYWHERE_SEARCH_URL = "https://api.buywhere.ai/v1/products/search"
DEFAULT_COUNTRY = "SG"
# Harvey Norman retired this product URL (confirmed as Page Not Found in the live browser).
INVALID_PRODUCT_URLS = {
    "https://www.harveynorman.com.sg/tv-and-audio/headphones-en/wireless-headphones-en/oppo-enco-buds3-true-wireless-earbuds-slate-black.html",
    "https://www.sporter.com/optimum-tshirt-gray-xlarge.html",
    # Reported as unusable from the user's location/session.
    "https://www.callawayapparel.com/products/balboa-vent-20-golf-shoes-black-cgfs800mcg-009",
}
INVALID_PRODUCT_TITLE_PATTERNS = (
    re.compile(r"\boptimum\s+t-?shirt\s*-\s*gray\s*-\s*xlarge\b", re.I),
    re.compile(r"\bbalboa\s+vent\s+2\.0(?:\s+golf)?\s+shoes\b", re.I),
    re.compile(r"\bwomens\s+coronado\s+v2\s+spikeless\s+golf\s+shoes\b", re.I),
)
INPUT_USD_PER_1M_TOKENS = float(os.getenv("INPUT_USD_PER_1M_TOKENS", "0.25"))
OUTPUT_USD_PER_1M_TOKENS = float(os.getenv("OUTPUT_USD_PER_1M_TOKENS", "2.00"))
LAST_COST = {"estimated_usd": 0.0, "method": "CSV snapshot; no metered API usage"}


def _save_evaluation_run(record):
    """Append a recommendation run to the opt-in evaluation JSONL log."""
    if not EVALUATION_LOG_ENABLED:
        return False
    try:
        EVALUATION_LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
        with EVALUATION_LOG_FILE.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
        return True
    except OSError as exc:
        app.logger.warning("Could not save evaluation run: %s", exc)
        return False


# Query understanding -------------------------------------------------------
def normalize_currency(value):
    """Map common currency symbols and aliases to ISO-style currency codes."""
    if not value:
        return None
    value = str(value).upper().strip()
    return {"RMB": "CNY", "RMB¥": "CNY", "CN¥": "CNY", "￥": "CNY", "S$": "SGD", "US$": "USD", "$": "USD"}.get(value, value)


def extract_budget(query):
    """Extract a supported maximum price and currency from a user query."""
    m = re.search(r"(?:under|below|less than|within|预算|低于|不超过|以内)\s*([\d,.]+)\s*(sgd|s\$|cny|rmb|usd|\$|新币|人民币|美元)?", query, re.I)
    if not m:
        return None, None
    currency = normalize_currency(m.group(2))
    if not currency:
        currency = "SGD"
    return float(m.group(1).replace(",", "")), currency


def parse_user_intent(query):
    """Parse product keywords and constraints, using a deterministic fallback."""
    budget, currency = extract_budget(query)
    if not OPENROUTER_API_KEY:
        cleaned = re.sub(r"(?:under|below|less than|within|预算|低于|不超过|以内)\s*[\d,.]+\s*(?:sgd|s\$|cny|rmb|usd|\$|新币|人民币|美元)?", "", query, flags=re.I)
        aliases = {"耳机": "earbuds", "蓝牙耳机": "earbuds", "跑鞋": "running shoes", "运动鞋": "running shoes", "水瓶": "water bottle", "水壶": "water bottle", "保温杯": "water bottle"}
        for zh, en in aliases.items():
            if zh in query:
                cleaned += " " + en
        return {"category": None, "max_price": budget, "budget_currency": currency or "SGD", "keywords": [cleaned.strip() or query], "exclude_keywords": []}
    prompt = ("Return JSON only with category, max_price, budget_currency, keywords (array), "
              "exclude_keywords (array). Extract an English product search term. Do not include price in keywords. "
              "Prefer the requested primary product itself, not compatible accessories; include accessories only when explicitly requested.\nQuery: " + query)
    try:
        response = requests.post("https://openrouter.ai/api/v1/chat/completions", headers={
            "Authorization": f"Bearer {OPENROUTER_API_KEY}", "Content-Type": "application/json"},
            json={"model": OPENROUTER_MODEL, "messages": [{"role": "user", "content": prompt}], "max_completion_tokens": 250}, timeout=20)
        response.raise_for_status()
        print(f"OpenRouter API OK (HTTP {response.status_code}, model={OPENROUTER_MODEL})")
        body = response.json()
        usage = body.get("usage", {})
        cost = (usage.get("prompt_tokens", 0) * INPUT_USD_PER_1M_TOKENS + usage.get("completion_tokens", 0) * OUTPUT_USD_PER_1M_TOKENS) / 1_000_000
        LAST_COST.update(estimated_usd=cost, method="estimated from OpenRouter token usage and configured rates")
        content = body["choices"][0]["message"]["content"]
        match = re.search(r"\{[\s\S]*\}", content)
        result = json.loads(match.group()) if match else {}
        result.setdefault("keywords", [query])
        result.setdefault("exclude_keywords", [])
        # Explicit budget text from the user is authoritative; the model may
        # otherwise misread attached currency codes or omit the budget.
        result["max_price"] = budget if budget is not None else (
            float(result["max_price"]) if result.get("max_price") not in (None, "") else None
        )
        result["budget_currency"] = currency or normalize_currency(result.get("budget_currency")) or "SGD"
        return result
    except Exception as exc:
        print("Intent API fallback:", type(exc).__name__, str(exc)[:160])
        return {"category": None, "max_price": budget, "budget_currency": currency or "SGD", "keywords": [query], "exclude_keywords": []}


# Catalog loading and normalization ----------------------------------------
def _normalize_product(item):
    """Convert provider or CSV fields into the app's common product schema.

    Invalid, unavailable, unpriced, or non-purchasable listings return None.
    """
    title = item.get("name") or item.get("title") or item.get("product_name") or ""
    # Drop listings explicitly marked as unavailable by the source API.
    status_values = [item.get(key) for key in ("availability", "availability_status", "stock_status", "inventory_status")]
    unavailable_terms = re.compile(r"\b(?:sold\s*out|out\s*of\s*stock|unavailable|discontinued|not\s*available|false)\b", re.I)
    if any(value is False or unavailable_terms.search(str(value).replace("_", " ").replace("-", " "))
           for value in status_values if value not in (None, "")):
        return None
    for key in ("in_stock", "is_in_stock", "available", "is_available"):
        value = item.get(key)
        if value is False or (isinstance(value, str) and value.strip().lower() in {"false", "no", "0", "sold out", "out of stock"}):
            return None
    for key in ("inventory_quantity", "stock_quantity", "quantity"):
        value = item.get(key)
        try:
            if value not in (None, "") and float(value) <= 0:
                return None
        except (TypeError, ValueError):
            pass
    raw_price = item.get("price")
    price_currency = None
    if isinstance(raw_price, dict):
        price_currency = raw_price.get("currency")
        raw_price = raw_price.get("amount")
    try:
        price = float(raw_price or item.get("sale_price") or item.get("current_price") or 0)
    except (TypeError, ValueError):
        price = 0
    # Prefer explicit merchant product URLs over generic or affiliate links.
    url = (item.get("product_url") or item.get("canonical_url") or item.get("merchant_url")
           or item.get("url") or item.get("purchase_url") or item.get("affiliate_url") or "")
    if (not title or price <= 0 or not url.startswith("http") or url in INVALID_PRODUCT_URLS
            or any(pattern.search(title) for pattern in INVALID_PRODUCT_TITLE_PATTERNS)):
        return None
    host = (url.split("/", 3)[2] if url.startswith("http") else str(domain)).lower()
    domain = item.get("domain") or item.get("source") or item.get("merchant") or host
    platform_name = ("Taobao" if "taobao." in host else
                     "TikTok Shop" if "tiktok." in host else
                     item.get("platform") or item.get("merchant_name") or item.get("merchant") or domain)
    description = (item.get("description") or item.get("product_description") or
                   item.get("short_description") or item.get("summary") or "")
    return {"id": str(item.get("id") or url), "name": title,
            "category": item.get("category") or "", "price": price,
            "currency": normalize_currency(item.get("currency") or price_currency) or "SGD", "rating": item.get("rating") or "",
            "image": item.get("image") or item.get("image_url") or "", "seller": item.get("seller") or item.get("merchant") or domain,
            "platform": platform_name, "url": url, "description": str(description).strip(),
            "sale_price": item.get("sale_price") or "", "original_price": item.get("original_price") or ""}


def load_snapshot_products():
    """Load and normalize valid rows from the configured CSV catalog."""
    if not PRODUCTS_CSV.exists():
        return []
    with PRODUCTS_CSV.open(encoding="utf-8-sig", newline="") as f:
        return [p for p in (_normalize_product(row) for row in csv.DictReader(f)) if p]


def search_live_products(keyword, max_items=20):
    """Search BuyWhere for one term; return normalized results or an empty list."""
    if not BUYWHERE_API_KEY:
        return []
    try:
        response = requests.get(BUYWHERE_SEARCH_URL, params={"q": keyword, "deliver_to": DEFAULT_COUNTRY,
                                                             "country_code": DEFAULT_COUNTRY, "limit": max_items},
                                headers={"Authorization": f"Bearer {BUYWHERE_API_KEY}", "Accept": "application/json"}, timeout=20)
        response.raise_for_status()
        print(f"BuyWhere API HTTP {response.status_code}")
        payload = response.json()
        rows = payload if isinstance(payload, list) else next((payload.get(k) for k in ("data", "items", "products") if isinstance(payload.get(k), list)), [])
        normalized = []
        for row in rows:
            product = _normalize_product({**row, "data_source": "BuyWhere live API"})
            if product:
                normalized.append(product)
        print(f"BuyWhere API OK: {len(rows)} result(s), {len(normalized)} usable product(s)")
        return normalized
    except Exception as exc:
        print("Live product API unavailable; using snapshot:", type(exc).__name__, str(exc)[:160])
        return []


# Product-page validation ---------------------------------------------------
def _page_indicates_sold_out(html):
    """Detect definitive sold-out signals in common product-page markup."""
    availability = re.findall(
        r'"availability"\s*:\s*"(?:https?://schema\.org/)?([A-Za-z]+)"', html, re.I
    )
    normalized = [value.lower() for value in availability]
    if normalized:
        if any(value in {"instock", "limitedavailability", "onlineonly"} for value in normalized):
            return False
        if all(value in {"outofstock", "soldout", "discontinued", "notavailable"} for value in normalized):
            return True

    # Fallback for storefronts that render stock state as a button or message.
    text = re.sub(r"<[^>]+>", " ", html)
    text = re.sub(r"\s+", " ", text).lower()
    says_sold_out = re.search(r"\b(?:sold\s*out|out\s*of\s*stock|currently\s*unavailable)\b", text)
    says_available = re.search(r"\b(?:in\s*stock|available\s*now)\b", text)
    return bool(says_sold_out and not says_available)


def _is_specific_product_url(url):
    """Return whether a URL appears to identify a product rather than a store."""
    parsed = urlparse(url)
    path_parts = [part for part in parsed.path.split("/") if part]
    if not path_parts:
        # A root URL is only product-specific if it has an explicit item ID.
        params = parse_qs(parsed.query)
        return any(key.lower() in {"id", "pid", "product_id", "item_id", "sku"} for key in params)
    generic_paths = {"search", "category", "categories", "collections", "catalog", "shop", "stores", "brands"}
    return path_parts[0].lower() not in generic_paths


def _check_product_page(product):
    """Reject definitive broken, generic, or explicitly sold-out product pages.

    Network failures are treated as inconclusive, so the original listing can
    still be shown for the user to verify.
    """
    url = product.get("url", "")
    if url in INVALID_PRODUCT_URLS:
        return False
    try:
        response = requests.get(
            url,
            headers={"User-Agent": "Mozilla/5.0 (compatible; EasyBuyerProductCheck/1.0)"},
            timeout=(3, 6),
            allow_redirects=True,
        )
        if response.status_code in (404, 410):
            print(f"Product link rejected ({response.status_code}): {url}")
            return False
        final_url = response.url or url
        if not _is_specific_product_url(final_url):
            print(f"Generic platform/search page rejected: {final_url}")
            return False
        # If the API supplied an affiliate/redirect link, resolve it to the
        # merchant's item page before exposing the purchase link to the user.
        product["url"] = final_url
        if "text/html" in response.headers.get("Content-Type", "").lower() and _page_indicates_sold_out(response.text):
            print(f"Sold-out product rejected: {url}")
            return False
        return True
    except requests.RequestException as exc:
        # A timeout or anti-bot response is inconclusive, so do not label it
        # invalid; retain the provider's link and let the buyer verify it.
        print(f"Product link check inconclusive ({type(exc).__name__}): {url}")
        return True


def _keep_available_products(products, limit=5, max_checks=25):
    """Check a ranked candidate set concurrently and return usable product pages."""
    ordered = sorted(products, key=lambda p: (-float(p.get("rating") or 0), p["price"]))
    candidates = ordered[:max_checks]
    with ThreadPoolExecutor(max_workers=8) as pool:
        checks = list(pool.map(_check_product_page, candidates))
    available = [product for product, is_available in zip(candidates, checks) if is_available]
    print(f"Product page checks: {sum(checks)}/{len(checks)} usable");
    return available[:limit]


# Optional live catalog refresh ---------------------------------------------
SNAPSHOT_QUERIES = {
    "earbuds": ["wireless earbuds", "bluetooth earphones", "noise cancelling earbuds"],
    "phones": ["smartphone", "android phone", "iPhone"],
    "laptops": ["laptop", "gaming laptop", "ultrabook"],
    "shoes": ["running shoes", "walking shoes", "sneakers"],
    "water bottles": ["water bottle", "insulated bottle", "sports flask"],
    "backpacks": ["backpack", "laptop backpack", "travel backpack"],
    "headphones": ["headphones", "over ear headphones", "gaming headset"],
    "small appliances": ["air fryer", "robot vacuum", "coffee machine"],
}


def refresh_snapshot(target=200):
    """Fetch a catalog snapshot into a new timestamped CSV file.

    The configured working snapshot is never overwritten. Raises RuntimeError
    when the API is unavailable or cannot supply the requested unique rows.
    """
    if not BUYWHERE_API_KEY:
        raise RuntimeError("Set BUYWHERE_API_KEY in the local environment before refreshing the live snapshot.")
    columns = ["id", "name", "category", "platform", "price", "currency", "rating", "image", "seller", "url"]
    items, seen = [], set()
    per_category = max(1, target // len(SNAPSHOT_QUERIES))
    for category, queries in SNAPSHOT_QUERIES.items():
        category_items = 0
        domain_counts = {"item.taobao.com": 0, "shop-sg.tiktok.com": 0}
        domain_quota = max(1, per_category // 3)
        for query in queries:
            domain_filters = ["item.taobao.com", "shop-sg.tiktok.com", None]
            for domain in domain_filters:
                if domain and domain_counts[domain] >= domain_quota:
                    continue
                if not domain and category_items >= per_category:
                    continue
                for offset in (0, 100, 200):
                    params = {"q": query, "deliver_to": DEFAULT_COUNTRY, "country_code": DEFAULT_COUNTRY,
                              "limit": 100, "offset": offset}
                    if domain:
                        params["domain"] = domain
                    response = requests.get(BUYWHERE_SEARCH_URL, params=params,
                                            headers={"Authorization": f"Bearer {BUYWHERE_API_KEY}", "Accept": "application/json"}, timeout=30)
                    response.raise_for_status()
                    payload = response.json()
                    rows = payload if isinstance(payload, list) else next(
                        (payload.get(k) for k in ("data", "items", "products") if isinstance(payload.get(k), list)), [])
                    for raw in rows:
                        raw.setdefault("category", category)
                        product = _normalize_product(raw)
                        if not product or product["url"] in seen:
                            continue
                        # Domain-specific requests must actually return that marketplace's item page.
                        if domain and domain not in product["url"].lower():
                            continue
                        seen.add(product["url"])
                        items.append({key: product.get(key, "") for key in columns})
                        category_items += 1
                        if domain:
                            domain_counts[domain] += 1
                        if len(items) >= target or category_items >= per_category:
                            break
                    if len(items) >= target or category_items >= per_category or len(rows) < 100:
                        break
                    time.sleep(0.15)
                if len(items) >= target or category_items >= per_category:
                    break
    if len(items) < target:
        raise RuntimeError(f"API returned only {len(items)} unique products with direct URLs; CSV was not overwritten.")
    # Never overwrite the working snapshot. Save each refresh beside it under a
    # unique timestamped filename so even repeated runs within one second are safe.
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    output_path = PRODUCTS_CSV.with_name(f"{PRODUCTS_CSV.stem}_live_{stamp}{PRODUCTS_CSV.suffix}")
    suffix = 1
    while output_path.exists():
        output_path = PRODUCTS_CSV.with_name(
            f"{PRODUCTS_CSV.stem}_live_{stamp}_{suffix}{PRODUCTS_CSV.suffix}"
        )
        suffix += 1
    with output_path.open("x", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=columns)
        writer.writeheader()
        writer.writerows(items[:target])
    return len(items[:target]), output_path


# Relevance, constraints, and recommendation pipeline -----------------------
def _currency_convert(amount, source, target):
    """Convert an amount through the exchange-rate service, or return None."""
    if source == target:
        return amount
    try:
        response = requests.get("https://api.frankfurter.app/latest", params={"from": source, "to": target}, timeout=8)
        response.raise_for_status()
        return amount * float(response.json()["rates"][target])
    except Exception:
        return None


def _matches(product, terms, excludes, query=""):
    """Apply product relevance rules and exclude nonphysical or accessory hits.

    The original query is treated as the primary product constraint; expanded
    model terms can improve recall but cannot override category-specific rules.
    """
    text = (product["name"] + " " + product.get("category", "")).lower()
    name = product["name"].lower()
    terms = [str(term).lower().strip() for term in terms if str(term).strip()]
    # Shopping search APIs may mix physical products with clubs, subscriptions,
    # digital content and services. These are not purchasable goods for this
    # project's product recommendations, even when their title contains the
    # requested category word (e.g. "Book Club").
    non_physical_re = re.compile(
        r"\b(?:book\s+clubs?|reading\s+clubs?|membership|subscription|"
        r"monthly\s+box|gift\s+card|digital\s+download|ebook\s+subscription|"
        r"online\s+course|coaching|consultation|repair\s+service|"
        r"streaming|software\s+license|service\s+plan|event\s+ticket|"
        r"magazine\s+subscription|rental\s+plan)\b",
        re.I,
    )
    if non_physical_re.search(name):
        return False

    # For book searches, require a book-like item title and reject clubs or
    # reading services. This prevents a token match on "book" alone from
    # treating a community, subscription, or service as a physical book.
    book_request = any(re.search(r"\bbooks?\b", term) for term in terms) or re.search(r"\bbooks?\b", query, re.I)
    if book_request:
        if re.search(r"\b(?:book\s+clubs?|reading\s+clubs?|book\s+subscription|book\s+membership)\b", name, re.I):
            return False
        book_item = re.search(
            r"\b(?:books?|paperback|hardcover|hardback|novel|textbook|workbook|comic\s+book|"
            r"children(?:'s)?\s+book|cookbook|isbn\s*[-:]?\s*\d)\b",
            name,
            re.I,
        )
        if not book_item:
            return False

    # A generic "fragrance" listing can be a candle, diffuser or room scent.
    # For perfume searches, require an explicit personal-fragrance product cue
    # in the title or product category.
    perfume_request = bool(re.search(r"\b(?:perfumes?|parfum|eau de parfum|eau de toilette|cologne|body mist)\b", query, re.I)) or any(
        re.search(r"\b(?:perfumes?|parfum|eau de parfum|eau de toilette|cologne|body mist)\b", term, re.I)
        for term in terms
    )
    if perfume_request:
        product_type_text = name + " " + str(product.get("category", "")).lower()
        home_fragrance_re = re.compile(
            r"\b(?:candle|candles|diffuser|diffusers|reed diffuser|room spray|linen spray|"
            r"home fragrance|incense|wax melt|essential oil|scented oil|fragrance oil|"
            r"car air freshener|air freshener)\b", re.I)
        personal_perfume_re = re.compile(
            r"\b(?:perfumes?|parfum|eau de parfum|eau de toilette|eau de cologne|"
            r"cologne|body mist|edp|edt|extrait de parfum)\b", re.I)
        if home_fragrance_re.search(product_type_text) or not personal_perfume_re.search(product_type_text):
            return False

    # "Suit" by itself means clothing in a shopping query. Catalogs can also
    # return unrelated items whose titles merely contain phrases such as
    # "flight suit pen". Keep suit apparel and reject pen/ink listings.
    suit_request = (re.search(r"\bsuits?\b", query, re.I) or
                    any(re.search(r"\bsuits?\b", term, re.I) for term in terms))
    if suit_request and re.search(r"\b(?:pens?|refills?|ink|writing instruments?|spare fill|maratac)\b", name, re.I):
        return False
    # When the user asks for a product, reject common accessory/part listings
    # that mention that product only as compatibility text. Keep them when the
    # user's actual query explicitly asks for an accessory.
    accessory_re = re.compile(
        r"\b(?:accessor(?:y|ies)|add-ons?|gadgets?|controllers?|brackets?|"
        r"cases?|covers?|coques?|screen protectors?|protective films?|films?|"
        r"holders?|dispensers?|mounts?|docks?|docking|chargers?|charging cables?|adapters?|"
        r"replacement(?:s)?|spare(?: parts?| parts?| fill| items?)?|repair kits?|parts?|"
        r"filters?|attachments?|extensions?|stands?|straps?|clips?|sleeves?|"
        r"refills?|ink cartridges?|sharpeners?|eraser caps?|batteries|battery packs?|power banks?|"
        r"screen guards?|skins?|decals?|stickers?|cleaning kits?|repair services?|"
        r"ear pads?|ear tips?|"
        r"tripods?|batteries|lenses|wallets?|sim cards?|fibrewire|broadband|"
        r"phone plans?|shakers?|cocktail glasses?|martini glasses?|bar tools?|mixology kits?|"
        r"candy|candies|sweets?|confectionery|novelty|toys?|dummy|"
        r"props?|gift sets?|shoehorns?|insoles?|socks?|shoe cleaners?|shoe polish|"
        r"bottle lids?|straws?|bottle carriers?|rain covers?|bag inserts?)\b",
        re.I,
    )
    explicit_accessory_request = bool(accessory_re.search(query))
    if not explicit_accessory_request and accessory_re.search(name):
        return False

    # Exclude common companion products by the requested product family. These
    # terms are contextual so a search for "backpack" or "keyboard" still
    # returns that item as the primary product.
    requested_text = " ".join([query] + terms).lower()
    family_accessories = {
        "phone": (r"\b(?:phones?|smartphones?|mobile phones?|cell phones?|cellphones?|iphones?)\b",
                  r"\b(?:backpacks?|phone bags?|phone charms?|pop sockets?|camera lens protectors?)\b"),
        "laptop": (r"\b(?:laptops?|notebooks?|ultrabooks?)\b",
                   r"\b(?:backpacks?|laptop bags?|sleeves?|cooling pads?|laptop stands?|keyboard covers?)\b"),
        "tablet": (r"\b(?:tablets?|ipads?)\b",
                   r"\b(?:tablet cases?|tablet stands?|stylus(?:es)?|screen protectors?|tablet keyboards?)\b"),
        "headphones": (r"\b(?:headphones?|headsets?|earbuds?|earphones?)\b",
                       r"\b(?:ear pads?|ear tips?|headphone stands?|headphone cases?)\b"),
        "shoes": (r"\b(?:shoes?|sneakers?|trainers?|boots?)\b",
                  r"\b(?:insoles?|shoelaces?|shoe cleaners?|shoe polish|shoe bags?)\b"),
        "camera": (r"\b(?:cameras?|dslr|mirrorless cameras?)\b",
                   r"\b(?:camera bags?|camera straps?|lens caps?|camera batteries|tripods?)\b"),
        "watch": (r"\b(?:watches?|smartwatches?)\b",
                  r"\b(?:watch bands?|watch straps?|watch chargers?|watch cases?)\b"),
    }
    if not explicit_accessory_request:
        for family, (request_pattern, accessory_pattern) in family_accessories.items():
            if re.search(request_pattern, requested_text, re.I) and re.search(accessory_pattern, name, re.I):
                return False

    # Treat an explicit product family in the user's own query as a hard
    # constraint. LLM-expanded terms are useful for recall, but must not turn a
    # knife search into unrelated outdoor products such as backpacks or bottles.
    primary_families = [
        (r"\b(?:water\s+bottles?|bottles?)\b", r"\b(?:water\s+)?bottles?\b"),
        (r"\b(?:smartphones?|mobile\s+phones?|cell\s+phones?|cellphones?|iphones?|phones?)\b", r"\b(?:smartphones?|mobile\s+phones?|cell\s+phones?|cellphones?|iphones?|phones?|pixel|galaxy|iphone)\b"),
        (r"\b(?:laptops?|notebooks?|ultrabooks?)\b", r"\b(?:laptops?|notebooks?|ultrabooks?|macbook|thinkpad|chromebook)\b"),
        (r"\b(?:tablets?|ipads?)\b", r"\b(?:tablets?|ipads?|ipad|galaxy\s+tab)\b"),
        (r"\b(?:headphones?|headsets?|earbuds?|earphones?)\b", r"\b(?:headphones?|headsets?|earbuds?|earphones?)\b"),
        (r"\b(?:shoes?|sneakers?|trainers?|boots?)\b", r"\b(?:shoes?|sneakers?|trainers?|boots?)\b"),
        (r"\b(?:backpacks?|rucksacks?)\b", r"\b(?:backpacks?|rucksacks?)\b"),
        (r"\b(?:books?|paperbacks?|hardcovers?|textbooks?)\b", r"\b(?:books?|paperbacks?|hardcovers?|textbooks?|novels?)\b"),
        (r"\b(?:suits?|blazers?|business\s+suits?)\b", r"\b(?:suits?|blazers?|business\s+suits?|pantsuits?)\b"),
        (r"\b(?:perfumes?|parfum|eau\s+de\s+parfum|eau\s+de\s+toilette|cologne)\b", r"\b(?:perfumes?|parfum|eau\s+de\s+parfum|eau\s+de\s+toilette|cologne|body\s+mist)\b"),
        (r"\b(?:knives|knife)\b", r"\b(?:knives|knife)\b"),
        (r"\b(?:cameras?|dslr|mirrorless\s+cameras?)\b", r"\b(?:cameras?|dslr|mirrorless)\b"),
        (r"\b(?:watches?|smartwatches?)\b", r"\b(?:watches?|smartwatches?|apple\s+watch)\b"),
        (r"\b(?:chocolates?|cocoa|cacao)\b", r"\b(?:chocolates?|cocoa|cacao|truffles?)\b"),
        (r"\bcocktails?\b", r"\bcocktails?\b"),
        (r"\btissues?\b", r"\b(?:tissues?|facial\s+tissues?|paper\s+tissues?|pocket\s+tissues?)\b"),
        (r"\bpencils?\b", r"\bpencils?\b"),
        (r"\berasers?\b", r"\berasers?\b"),
        (r"\bfood\b", r"\b(?:food|groceries|snacks?|chocolates?|cocoa|cacao|candies|sweets?|cookies|biscuits|chips|crisps|noodles|pasta|rice|beverages?|drinks?)\b"),
    ]
    product_text = name + " " + str(product.get("category", "")).lower()
    for query_pattern, product_pattern in primary_families:
        if re.search(query_pattern, query, re.I) and not re.search(product_pattern, product_text, re.I):
            return False
    if re.search(r"\b(?:chocolates?|cocoa|cacao|food)\b", query, re.I):
        non_food_apparel = re.compile(
            r"\b(?:t\s*-?\s*shirts?|tishirts?|tees?|shirts?|hoodies?|sweatshirts?|apparel|clothing|"
            r"mugs?|phone cases?|stickers?|posters?|costumes?)\b", re.I)
        if non_food_apparel.search(name):
            return False
    if re.search(r"\bcocktails?\b", query, re.I) and re.search(
            r"\b(?:shakers?|cocktail glasses?|martini glasses?|bar tools?|mixology kits?|recipe books?)\b", name, re.I):
        return False
    if not explicit_accessory_request and re.search(r"\bpencils?\b", query, re.I) and re.search(
            r"\b(?:pencil\s+cases?|pencil\s+sharpeners?|sharpeners?|pencil\s+holders?|pencil\s+grips?|pencil\s+leads?)\b", name, re.I):
        return False
    if not explicit_accessory_request and re.search(r"\berasers?\b", query, re.I) and re.search(
            r"\b(?:eraser\s+holders?|eraser\s+caps?|eraser\s+cases?|eraser\s+refills?)\b", name, re.I):
        return False

    phone_request = any(re.search(r"\b(?:phones?|smartphones?|mobile phones?|cell phones?|cellphones?|iphones?)\b", term) for term in terms)
    if phone_request and not explicit_accessory_request:
        phone_device = re.search(
            r"\b(?:smartphones?|mobile phones?|cell phones?|cellphones?|iphones?|pixel(?:\s+[a-z0-9-]+)?|samsung galaxy\s+(?:s|a|z|note|m)\s?\d|galaxy\s+(?:s|a|z|note|m)\s?\d|oneplus|redmi|xiaomi|poco|oppo|vivo|realme|huawei|motorola|nothing phone)\b",
            name,
        )
        if not phone_device:
            return False
    # Snapshot records are category-tagged; query matching is permissive on tokens to avoid hidden misses.
    useful = [t for t in terms if len(t) > 2 and not re.search(r"\d|under|below|sgd|usd|cny", t)]
    match_terms = list(useful)
    if any(re.search(r"\btissues?\b", term, re.I) for term in terms):
        match_terms.extend(["tissue", "tissues"])
    return not any(str(x).lower() in text for x in excludes) and (not useful or any(t in text for t in match_terms) or any(w in text for t in match_terms for w in t.split()))


def generate_description(product):
    """Return a concise English description, using source text when available."""
    description = re.sub(r"\s+", " ", str(product.get("description") or "")).strip()
    if description:
        description = re.sub(r"<[^>]*>", " ", description)
        description = re.sub(r"\s+", " ", description).strip()
        if not re.search(r"[\u3400-\u9fff]", description):
            return description[:320]
    category = str(product.get("category") or "product").strip()
    return f"{product['name']} — a {category} listing. Check the product page for specifications and included items."


def recommend_products(query):
    """Run the end-to-end retrieval, filtering, ranking, and validation flow.

    Returns ``(products, intent, cost, source)`` where products contains at
    most five recommendation records and source identifies live search or CSV.
    """
    global LAST_COST
    LAST_COST = {"estimated_usd": 0.0, "method": "CSV snapshot; no metered API usage"}
    intent = parse_user_intent(query)
    direct_search = re.sub(
        r"(?:under|below|less than|within|预算|低于|不超过|以内)\s*[\d,.]+\s*(?:sgd|s\$|cny|rmb|usd|\$|新币|人民币|美元)?",
        " ", query, flags=re.I)
    direct_search = re.sub(r"\s+", " ", direct_search).strip()
    model_terms = intent.get("keywords", [query])
    if isinstance(model_terms, str):
        model_terms = [model_terms]
    # Ignore model keywords that still contain a budget, number, or currency.
    # Such malformed terms can prevent relevant candidates from passing local
    # matching (e.g. "knife under 200s g d"). Always include the user's own
    # budget-free query as the first and authoritative search/filter term.
    terms = [direct_search] if direct_search else []
    for term in model_terms:
        cleaned_term = re.sub(r"\s+", " ", str(term)).strip()
        if (not cleaned_term or re.search(r"\d|\b(?:under|below|less than|within|sgd|usd|cny|rmb)\b", cleaned_term, re.I)
                or re.search(r"[sS]\s+[gG]\s+[dD]", cleaned_term)):
            continue
        if cleaned_term.casefold() not in {t.casefold() for t in terms}:
            terms.append(cleaned_term)
    if not terms:
        terms = [query]
    intent["keywords"] = terms
    print("Intent keywords after cleanup:", ", ".join(terms))
    live_search_terms = []
    # Retail search providers often index knife listings under the plural or
    # kitchen/chef phrasing. Search those variants while still enforcing the
    # original knife query against every returned product.
    if re.search(r"\bknife\b", direct_search, re.I):
        live_search_terms.extend([direct_search, "knives", "kitchen knife"])
    elif re.search(r"\bcocktails?\b", direct_search, re.I):
        live_search_terms.extend([direct_search, "cocktail drink", "ready to drink cocktail"])
    elif re.search(r"\bchocolates?\b", direct_search, re.I):
        live_search_terms.extend([direct_search, "chocolate bar", "chocolate snacks"])
    elif re.search(r"\btissues?\b", direct_search, re.I):
        live_search_terms.extend([direct_search, "facial tissues", "pocket tissues"])
    elif re.search(r"\bpencils?\b", direct_search, re.I):
        live_search_terms.extend([direct_search, "pencils", "wooden pencils"])
    elif re.search(r"\berasers?\b", direct_search, re.I):
        live_search_terms.extend([direct_search, "erasers", "rubber eraser"])
    for term in terms:
        cleaned_term = str(term).strip()
        if cleaned_term and cleaned_term.casefold() not in {t.casefold() for t in live_search_terms}:
            live_search_terms.append(cleaned_term)
    products = []
    live_search_terms = live_search_terms[:3]
    print("BuyWhere search queries:", ", ".join(live_search_terms))
    for term in live_search_terms:
        products.extend(search_live_products(term))
    data_source = "live_api"
    if not products:
        products = load_snapshot_products()
        data_source = "CSV snapshot"
    excludes = intent.get("exclude_keywords", [])

    def filter_products(candidates):
        matched, seen = [], set()
        for item in candidates:
            p = _normalize_product(item)
            if not p or p["url"] in seen or not _matches(p, terms, excludes, query):
                continue
            seen.add(p["url"])
            if intent.get("max_price") is not None:
                max_price = float(intent["max_price"])
                source_currency = normalize_currency(intent.get("budget_currency")) or "SGD"
                converted = _currency_convert(max_price, source_currency, p["currency"])
                # If rates cannot be fetched, exclude cross-currency items rather
                # than silently letting potentially over-budget products through.
                if converted is None or p["price"] > converted:
                    continue
            p["description"] = generate_description(p)
            matched.append(p)
        return matched

    unique = filter_products(products)
    if unique:
        unique = _keep_available_products(unique, limit=5)
    # A live provider can return items that all fail local keyword/budget checks.
    # In that case, retry against the local snapshot instead of showing a false miss.
    if not unique and data_source == "live_api":
        snapshot = load_snapshot_products()
        if snapshot:
            data_source = "CSV snapshot"
            unique = filter_products(snapshot)
            unique = _keep_available_products(unique, limit=5)
    elif unique and data_source == "CSV snapshot":
        unique = _keep_available_products(unique, limit=5)
    unique.sort(key=lambda p: (-float(p.get("rating") or 0), p["price"]))
    recommendations = unique[:5]
    LAST_COST["cost_per_recommendation_usd"] = round(LAST_COST["estimated_usd"] / len(recommendations), 8) if recommendations else None
    LAST_COST["recommendation_count"] = len(recommendations)
    return recommendations, intent, dict(LAST_COST), data_source


# Browser interface ---------------------------------------------------------
# Kept as one template so this prototype can run without a separate frontend build.
HTML_TEMPLATE = """
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>AI Shopping Assistant</title>
<style>
* { box-sizing: border-box; }
body { margin: 0; font-family: Arial, sans-serif; background: #f5f7fb; color: #1f2937; }
.container { max-width: 1050px; margin: 35px auto; padding: 20px; }
h1 { text-align: center; margin-bottom: 8px; }
.subtitle { text-align: center; color: #6b7280; margin-bottom: 28px; }
.search-box { display: flex; gap: 10px; padding: 16px; background: white; border-radius: 14px; box-shadow: 0 4px 20px rgba(0,0,0,.06); }
input { flex: 1; padding: 14px; border: 1px solid #d1d5db; border-radius: 9px; font-size: 16px; }
button { border: none; border-radius: 9px; padding: 12px 20px; cursor: pointer; font-size: 15px; }
#searchButton { background: #2563eb; color: white; }
button:disabled { opacity: .6; cursor: not-allowed; }
#status { text-align: center; margin: 20px 0; color: #4b5563; }
#intent { margin-bottom: 20px; padding: 12px 16px; border-radius: 10px; background: #eef2ff; display: none; }
.grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(270px,1fr)); gap: 18px; }
.card { background: white; border-radius: 14px; padding: 18px; box-shadow: 0 4px 16px rgba(0,0,0,.06); }
#products .description, #products .reason { display: none !important; }
.image-container { width: 100%; height: 190px; display: flex; align-items: center; justify-content: center; background: #fafafa; border-radius: 9px; overflow: hidden; }
.image-container img { width: 100%; height: 100%; object-fit: contain; }
.image-placeholder { width: 100%; height: 100%; align-items: center; justify-content: center; color: #9ca3af; display: flex; }
.name { font-size: 17px; font-weight: bold; margin-top: 13px; line-height: 1.4; }
.price-container { margin-top: 10px; display: flex; align-items: baseline; gap: 8px; }
.price { color: #e11d48; font-size: 21px; font-weight: bold; }
.original-price { color: #9ca3af; font-size: 14px; text-decoration: line-through; }
.buy { display: block; margin-top: 14px; padding: 11px; text-align: center; text-decoration: none; color: white; background: #111827; border-radius: 8px; }
.feedback-section { margin-top: 16px; padding-top: 14px; border-top: 1px solid #e5e7eb; }
.feedback-title { font-size: 14px; margin-bottom: 10px; color: #4b5563; }
.feedback-buttons { display: flex; gap: 10px; }
.accept-btn { flex: 1; background: #dcfce7; color: #166534; }
.reject-btn { flex: 1; background: #fee2e2; color: #991b1b; }
.reject-box { display: none; margin-top: 12px; }
.reject-box textarea { width: 100%; min-height: 75px; resize: vertical; padding: 10px; border: 1px solid #d1d5db; border-radius: 8px; font-family: inherit; font-size: 14px; }
.submit-reject-btn { width: 100%; margin-top: 8px; background: #dc2626; color: white; }
.feedback-result { display: none; margin-top: 12px; padding: 10px; border-radius: 8px; font-size: 14px; }
.feedback-success { background: #ecfdf5; color: #065f46; }
.feedback-error { background: #fef2f2; color: #991b1b; }
</style>
</head>
<body>
<div class="container">
<h1>🛍️ AI Shopping Assistant</h1>
<div class="subtitle">AI Intent Parsing + Real-time Product Search + Smart Sale Price Filtering</div>
<div class="search-box">
<input id="query" placeholder="e.g., Find running shoes under 900 SGD">
<button id="searchButton" onclick="searchProducts()">Search</button>
</div>
<div id="status">Please enter your shopping request</div>
<div id="intent"></div>
<div id="products" class="grid"></div>
</div>

<script>
const API_BASE = {{ api_base | tojson }};

function escapeHtml(text) {
    const div = document.createElement("div");
    div.textContent = text ?? "";
    return div.innerHTML;
}

document.getElementById("query").addEventListener("keydown", function(event) {
    if (event.key === "Enter") {
        searchProducts();
    }
});

async function submitFeedback(query, product, accepted, reason = "") {
    const response = await fetch(API_BASE + "api/feedback", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        credentials: "include",
        body: JSON.stringify({
            query: query,
            product_id: product.id || "",
            product_name: product.name || "",
            product_price: product.price || 0,
            product_currency: product.currency || "",
            product_seller: product.seller || "",
            product_url: product.url || "",
            accepted: accepted,
            reason: reason
        })
    });
    const text = await response.text();
    if (!response.ok) {
        throw new Error(text);
    }
    return JSON.parse(text);
}

async function searchProducts() {
    const query = document.getElementById("query").value.trim();
    if (!query) {
        alert("Please enter your shopping request");
        return;
    }

    const status = document.getElementById("status");
    const productsContainer = document.getElementById("products");
    const intentContainer = document.getElementById("intent");
    const button = document.getElementById("searchButton");

    status.innerHTML = "🔍 AI is analyzing intent and searching for products...";
    productsContainer.innerHTML = "";
    intentContainer.style.display = "none";
    button.disabled = true;

    try {
        const response = await fetch(API_BASE + "api/recommend", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            credentials: "include",
            body: JSON.stringify({ query: query })
        });

        const rawText = await response.text();
        if (!response.ok) {
            throw new Error("HTTP " + response.status + ": " + rawText.substring(0, 300));
        }

        const data = JSON.parse(rawText);
        if (data.error) {
            throw new Error(data.error);
        }

        const products = data.products || [];
        const intent = data.intent || {};

        let budgetText = "No budget set";
        if (intent.max_price !== null && intent.max_price !== undefined) {
            budgetText = escapeHtml(intent.max_price) + " " + escapeHtml(intent.budget_currency || "SGD");
        }

        intentContainer.innerHTML = "🤖 AI Intent: " + escapeHtml(intent.category || "Products") + " | Budget: " + budgetText + " | Keywords: " + escapeHtml((intent.keywords || []).join(", "));
        intentContainer.style.display = "block";

        if (products.length === 0) {
            status.innerHTML = "😕 No products found matching your criteria and budget";
            return;
        }

        status.innerHTML = "✅ Found " + products.length + " recommended product(s)";

        products.forEach(function(product, index) {
            const card = document.createElement("div");
            card.className = "card";

            let imageHTML;
            if (product.image) {
                imageHTML = `
                <div class="image-container">
                    <img src="${product.image}" onerror="this.style.display='none'; this.nextElementSibling.style.display='flex';">
                    <div class="image-placeholder" style="display:none;">Image unavailable</div>
                </div>`;
            } else {
                imageHTML = `
                <div class="image-container">
                    <div class="image-placeholder">Image unavailable</div>
                </div>`;
            }

            let priceHTML = `
            <div class="price-container">
                <div class="price">${escapeHtml(product.currency)} ${Number(product.price).toFixed(2)}</div>
                ${product.original_price ? `<div class="original-price">${escapeHtml(product.currency)} ${Number(product.original_price).toFixed(2)}</div>` : ''}
            </div>`;

            card.innerHTML = `
                ${imageHTML}
                <div class="name">${escapeHtml(product.name)}</div>
                ${priceHTML}
                ${product.url ? `<a class="buy" href="${product.url}" target="_blank" rel="noopener noreferrer">View Product →</a>` : ""}
                <div class="feedback-section">
                    <div class="feedback-title">Is this recommendation helpful?</div>
                    <div class="feedback-buttons">
                        <button class="accept-btn" data-index="${index}">👍 Accept</button>
                        <button class="reject-btn" data-index="${index}">👎 Reject</button>
                    </div>
                    <div class="reject-box" id="rejectBox-${index}">
                        <textarea id="rejectReason-${index}" placeholder="Please state why you reject this recommendation..."></textarea>
                        <button class="submit-reject-btn" data-index="${index}">Submit Reason</button>
                    </div>
                    <div class="feedback-result" id="feedbackResult-${index}"></div>
                </div>
            `;

            productsContainer.appendChild(card);

            const acceptButton = card.querySelector(".accept-btn");
            acceptButton.addEventListener("click", async function() {
                const resultBox = document.getElementById(`feedbackResult-${index}`);
                try {
                    acceptButton.disabled = true;
                    await submitFeedback(query, product, true, "");
                    resultBox.className = "feedback-result feedback-success";
                    resultBox.innerHTML = "✅ Recorded: Recommendation accepted";
                    resultBox.style.display = "block";
                    card.querySelector(".reject-btn").disabled = true;
                } catch(error) {
                    acceptButton.disabled = false;
                    resultBox.className = "feedback-result feedback-error";
                    resultBox.innerHTML = "❌ Failed to submit feedback";
                    resultBox.style.display = "block";
                }
            });

            const rejectButton = card.querySelector(".reject-btn");
            rejectButton.addEventListener("click", function() {
                const rejectBox = document.getElementById(`rejectBox-${index}`);
                rejectBox.style.display = "block";
                document.getElementById(`rejectReason-${index}`).focus();
            });

            const submitRejectButton = card.querySelector(".submit-reject-btn");
            submitRejectButton.addEventListener("click", async function() {
                const reasonInput = document.getElementById(`rejectReason-${index}`);
                const reason = reasonInput.value.trim();
                const resultBox = document.getElementById(`feedbackResult-${index}`);
                if (!reason) {
                    alert("Please enter a reason for rejection");
                    return;
                }
                try {
                    submitRejectButton.disabled = true;
                    await submitFeedback(query, product, false, reason);
                    resultBox.className = "feedback-result feedback-success";
                    resultBox.innerHTML = "✅ Recorded: Rejection reason saved";
                    resultBox.style.display = "block";
                    rejectButton.disabled = true;
                    acceptButton.disabled = true;
                    document.getElementById(`rejectBox-${index}`).style.display = "none";
                } catch(error) {
                    submitRejectButton.disabled = false;
                    resultBox.className = "feedback-result feedback-error";
                    resultBox.innerHTML = "❌ Failed to submit feedback";
                    resultBox.style.display = "block";
                }
            });
        });

    } catch(error) {
        console.error("❌ Request Error:", error);
        status.innerHTML = "❌ Request Failed: " + escapeHtml(error.message);
    } finally {
        button.disabled = false;
    }
}
</script>
</body>
</html>
"""

# Flask application and HTTP routes ----------------------------------------
app = Flask(__name__)


@app.get("/")
def index():
    """Render the single-page shopping interface."""
    return render_template_string(HTML_TEMPLATE, api_base="/")


@app.get("/api/test")
def api_test():
    """Expose a lightweight health check and the available snapshot row count."""
    return jsonify({"status": "ok", "product_count": len(load_snapshot_products())})


@app.post("/api/recommend")
def api_recommend():
    """Validate a search request and return recommendation data as JSON."""
    request_started = time.perf_counter()
    run_id = uuid.uuid4().hex
    data = request.get_json(silent=True) or {}
    query = str(data.get("query", "")).strip()
    if not query:
        response_time_ms = round((time.perf_counter() - request_started) * 1000, 2)
        return jsonify({"products": [], "intent": {}, "response_time_ms": response_time_ms,
                        "error": "Please enter a shopping request"}), 400
    try:
        products, intent, cost, source = recommend_products(query)
        response_time_ms = round((time.perf_counter() - request_started) * 1000, 2)
        run_record = {
            "run_id": run_id,
            "timestamp": datetime.now().astimezone().isoformat(),
            "query": query,
            "intent": intent,
            "products": products,
            "recommendation_count": len(products),
            "data_source": source,
            "response_time_ms": response_time_ms,
            "cost": cost,
            "status": "success",
        }
        logged = _save_evaluation_run(run_record)
        response = {"products": products, "intent": intent, "cost": cost,
                    "data_source": source, "response_time_ms": response_time_ms, "error": None}
        if logged:
            response["evaluation_run_id"] = run_id
        return jsonify(response)
    except Exception as exc:
        app.logger.exception("Recommendation failed")
        response_time_ms = round((time.perf_counter() - request_started) * 1000, 2)
        _save_evaluation_run({
            "run_id": run_id,
            "timestamp": datetime.now().astimezone().isoformat(),
            "query": query,
            "response_time_ms": response_time_ms,
            "status": "error",
            "error": str(exc),
        })
        return jsonify({"products": [], "intent": {}, "response_time_ms": response_time_ms,
                        "error": str(exc)}), 500


@app.post("/api/feedback")
def api_feedback():
    """Append one validated accept/reject decision to the local feedback log."""
    data = request.get_json(silent=True) or {}
    query, name, accepted = str(data.get("query", "")).strip(), str(data.get("product_name", "")).strip(), data.get("accepted")
    reason = str(data.get("reason", "")).strip()
    if not query or not name or not isinstance(accepted, bool) or (accepted is False and not reason):
        return jsonify({"error": "query, product_name, accepted and rejection reason are required"}), 400
    FEEDBACK_FILE.parent.mkdir(parents=True, exist_ok=True)
    record = {"timestamp": datetime.now().astimezone().isoformat(), **data}
    with FEEDBACK_FILE.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")
    return jsonify({"success": True})


@app.get("/api/feedback/count")
def feedback_count():
    """Return the number of non-empty feedback records currently stored."""
    count = sum(1 for line in FEEDBACK_FILE.open(encoding="utf-8") if line.strip()) if FEEDBACK_FILE.exists() else 0
    return jsonify({"count": count})


if __name__ == "__main__":
    """Support local server startup and non-destructive snapshot refreshes."""
    parser = argparse.ArgumentParser(description="Shopaholic EasyBuyer")
    parser.add_argument("--refresh-snapshot", action="store_true", help="Fetch 200 unique live SG products into a new CSV without replacing the current snapshot")
    args = parser.parse_args()
    if args.refresh_snapshot:
        count, output_path = refresh_snapshot(200)
        print(f"Saved {count} unique products to new file: {output_path}")
    else:
        print(f"Active app file: {Path(__file__).resolve()}")
        print("Search rules: direct-query-keywords-v2")
        print("OpenRouter API key:", "configured" if OPENROUTER_API_KEY else "missing (local intent fallback)")
        print("BuyWhere API key:", "configured" if BUYWHERE_API_KEY else "missing (CSV snapshot only)")
        app.run(host="127.0.0.1", port=int(os.getenv("PORT", "5001")), debug=False)
