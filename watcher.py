#!/usr/bin/env python3
"""Cloud-run Pokémon TCG drop radar for public Philippine retailer/news pages.

Designed for GitHub Actions: no local machine, browser, or Discord bot process needed.
"""
from __future__ import annotations

import html
import json
import logging
import os
import re
import sys
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

LOG = logging.getLogger("pokemon-stock-watch")
logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"), format="%(asctime)s %(levelname)s %(message)s")

STATE_PATH = Path(os.getenv("STATE_PATH", "state.json"))
DISCORD_WEBHOOK_URL = os.getenv("DISCORD_WEBHOOK_URL", "").strip()
USER_AGENT = os.getenv(
    "WATCHER_USER_AGENT",
    "PokemonPHStockWatch/1.0 (+https://github.com; periodic public-page checks)",
)
MAX_DISCORD_EMBEDS = 10
# Discord applies a 6,000-character aggregate limit across all embeds in one message.
# Leave a margin so each webhook payload remains comfortably below that limit.
MAX_DISCORD_EMBED_CHARS = 5_500
MAX_STATE_PRODUCTS = 2500
MAX_STATE_ANNOUNCEMENTS = 500

STORES: list[dict[str, Any]] = [
    {
        "key": "toy_kingdom",
        "name": "Toy Kingdom",
        "base": "https://www.toykingdom.com.ph",
        "catalog_urls": [
            ("Pokémon collection", "https://www.toykingdom.com.ph/collections/pokemon/products.json?limit=250&page=1", "https://www.toykingdom.com.ph/collections/pokemon"),
            ("Latest catalogue page", "https://www.toykingdom.com.ph/products.json?limit=250&page=1", "https://www.toykingdom.com.ph/collections/pokemon"),
        ],
        "suggest_url": "https://www.toykingdom.com.ph/search/suggest.json",
        "feeds": [
            "https://www.toykingdom.com.ph/blogs/event.atom",
            "https://www.toykingdom.com.ph/blogs/news.atom",
        ],
        "html_urls": [],
        "collection_url": "https://www.toykingdom.com.ph/collections/pokemon",
        "coverage": "Shopify-style public product JSON/search suggestions, collection HTML fallback, public event/news feeds",
    },
    {
        "key": "toys_r_us",
        "name": "Toys“R”Us Philippines",
        "base": "https://toysrus.com.ph",
        "catalog_urls": [
            ("Pokémon collection", "https://toysrus.com.ph/collections/pokemon/products.json?limit=250&page=1", "https://toysrus.com.ph/collections/pokemon"),
            ("Trading card games collection", "https://toysrus.com.ph/collections/trading-card-games/products.json?limit=250&page=1", "https://toysrus.com.ph/collections/trading-card-games"),
            ("Latest catalogue page", "https://toysrus.com.ph/products.json?limit=250&page=1", "https://toysrus.com.ph/collections/pokemon"),
        ],
        "suggest_url": "https://toysrus.com.ph/search/suggest.json",
        "feeds": [
            "https://toysrus.com.ph/blogs/event.atom",
            "https://toysrus.com.ph/blogs/events.atom",
            "https://toysrus.com.ph/blogs/news.atom",
        ],
        "html_urls": [],
        "collection_url": "https://toysrus.com.ph/collections/pokemon",
        "coverage": "Shopify-style public product JSON/search suggestions, collection HTML fallback, public event/news feeds",
    },
    {
        "key": "green_gate_hobbies",
        "name": "Green Gate Hobbies",
        "base": "https://shop.greengatehobbies.com",
        "catalog_urls": [
            ("Pokémon TCG pre-orders", "https://shop.greengatehobbies.com/collections/ptcg-pre-orders/products.json?limit=250&page=1", "https://shop.greengatehobbies.com/collections/ptcg-pre-orders"),
        ],
        "suggest_url": "https://shop.greengatehobbies.com/search/suggest.json",
        "feeds": [],
        "html_urls": [("Pokémon TCG pre-orders", "https://shop.greengatehobbies.com/collections/ptcg-pre-orders")],
        "collection_url": "https://shop.greengatehobbies.com/collections/ptcg-pre-orders",
        "coverage": "Public Pokémon TCG pre-order Shopify-style collection JSON and collection HTML fallback",
    },
    {
        "key": "jpstr",
        "name": "JPSTR PH",
        "base": "https://jpstr.ph",
        "catalog_urls": [],
        "suggest_url": None,
        "feeds": [],
        "html_urls": [
            ("Pre-order listings", "https://jpstr.ph/preorders"),
            ("On-hand product listings", "https://jpstr.ph/"),
        ],
        "collection_url": "https://jpstr.ph/preorders",
        "coverage": "Public HTML listing/detail links only; login-only details are not accessed",
    },
    {
        "key": "hobby_arena",
        "name": "Hobby Arena",
        "base": "https://www.hobbyarena.ph",
        "catalog_urls": [],
        "suggest_url": None,
        "feeds": [],
        "html_urls": [("Homepage featured products/pre-orders", "https://www.hobbyarena.ph/")],
        "collection_url": "https://www.hobbyarena.ph/",
        "coverage": "Public homepage HTML product links only; if the site renders listings only with JavaScript, results may be limited",
    },
]

OFFICIAL_NEWS_SOURCES: list[dict[str, str]] = [
    {
        "key": "pokemon_asia_news",
        "name": "Official Pokémon TCG Asia English / Philippines news",
        "url": "https://asia.pokemon-card.com/ph/info/",
    }
]

POKEMON_RE = re.compile(r"pok[eé]mon", re.IGNORECASE)
CARD_TERMS = (
    "trading card game", "trading cards", "trading card", "pokemon tcg", "tcg",
    "booster", "elite trainer", " etb", "blister", "premium collection",
    "ultra-premium", "ultra premium", "battle deck", "trainer toolkit",
    "pokemon card", "pokemon cards", "card game", "build & battle", "build and battle",
)
PREORDER_TERMS = (
    "pre-order", "pre order", "preorder", "preorders", "pre-orders", "reserve now",
    "reservation", "coming soon", "expected release", "release date", "down payment",
    "expected arrival", "estimated arrival", "est. arrival", "incoming", "in transit",
)
ANNOUNCEMENT_TERMS = (
    "restock", "stock drop", "drop", "pre-order", "preorder", "pre order", "release",
    "trading card", "tcg", "booster", "elite trainer", "30th celebration", "stock up",
)
IN_TRANSIT_PATTERNS = (
    r"\bin transit\b", r"\binbound shipment\b", r"\ben route to (?:our|the) (?:warehouse|store)\b",
    r"\bshipment (?:is )?incoming\b", r"\bshipping to us\b", r"\bdispatched by (?:the )?distributor\b",
    r"\bshipped by (?:the )?distributor\b",
)
UPCOMING_PATTERNS = (
    r"\bpre[- ]?orders?\b", r"\bcoming soon\b", r"\bincoming\b", r"\bexpected arrival\b",
    r"\best(?:imated)?\.? arrival\b", r"\barrival date\b", r"\best(?:imated)?\.? release\b",
    r"\brelease date\b", r"\barriving\b", r"\beta\s*[:：]", r"\bsubject to allocation\b",
    r"\barrived at (?:our|the) (?:warehouse|store)\b",
)

# A maintained recognition list helps report set/expansion names when the retailer includes
# them in the product title. It is not treated as a release calendar or inventory source.
KNOWN_TCG_SETS: tuple[tuple[str, str], ...] = tuple(sorted([
    # Upcoming and recently announced 2026 products / special collections.
    ("focused fighters premium collection", "Focused Fighters Premium Collection"),
    ("focused fighters", "Focused Fighters Premium Collection"),
    ("first partner illustration collection series 3", "First Partner Illustration Collection—Series 3"),
    ("first partner illustration collection series 2", "First Partner Illustration Collection—Series 2"),
    ("first partner illustration collection series 1", "First Partner Illustration Collection—Series 1"),
    ("first partner illustration collection", "First Partner Illustration Collection"),
    ("first partner collection series 3", "First Partner Illustration Collection—Series 3"),
    ("first partner collection series 2", "First Partner Illustration Collection—Series 2"),
    ("first partner collection series 1", "First Partner Illustration Collection—Series 1"),
    ("mega greninja ex league battle deck", "Mega Greninja ex League Battle Deck"),
    ("30th celebration booster bundle", "30th Celebration Booster Bundle"),
    ("30th celebration ultra premium collection", "30th Celebration Ultra-Premium Collection"),
    ("30th celebration ditto premium collection", "30th Celebration Ditto Premium Collection"),
    ("30th celebration battle deck", "30th Celebration Battle Deck"),
    ("30th celebration mini tin", "30th Celebration Mini Tin"),
    ("30th celebration tin", "30th Celebration Tin"),
    ("delta reign", "Mega Evolution—Delta Reign"),
    ("aura seeker", "Japanese Pokémon TCG—Aura Seeker"),
    ("30th celebration classic collection", "30th Celebration: Classic Collection"),
    ("mega evolution ascended heroes", "Mega Evolution—Ascended Heroes"),
    ("mega evolution phantasmal flames", "Mega Evolution—Phantasmal Flames"),
    ("mega evolution perfect order", "Mega Evolution—Perfect Order"),
    ("mega evolution chaos rising", "Mega Evolution—Chaos Rising"),
    ("mega evolution pitch black", "Mega Evolution—Pitch Black"),
    ("mega evolution delta reign", "Mega Evolution—Delta Reign"),
    ("scarlet and violet prismatic evolutions", "Scarlet & Violet—Prismatic Evolutions"),
    ("scarlet & violet prismatic evolutions", "Scarlet & Violet—Prismatic Evolutions"),
    ("scarlet and violet shrouded fable", "Scarlet & Violet—Shrouded Fable"),
    ("scarlet & violet shrouded fable", "Scarlet & Violet—Shrouded Fable"),
    ("scarlet and violet twilight masquerade", "Scarlet & Violet—Twilight Masquerade"),
    ("scarlet & violet twilight masquerade", "Scarlet & Violet—Twilight Masquerade"),
    ("scarlet and violet temporal forces", "Scarlet & Violet—Temporal Forces"),
    ("scarlet & violet temporal forces", "Scarlet & Violet—Temporal Forces"),
    ("scarlet and violet destined rivals", "Scarlet & Violet—Destined Rivals"),
    ("scarlet & violet journey together", "Scarlet & Violet—Journey Together"),
    ("scarlet and violet surging sparks", "Scarlet & Violet—Surging Sparks"),
    ("scarlet & violet stellar crown", "Scarlet & Violet—Stellar Crown"),
    ("scarlet & violet paldean fates", "Scarlet & Violet—Paldean Fates"),
    ("scarlet & violet paldea evolved", "Scarlet & Violet—Paldea Evolved"),
    ("scarlet & violet obsidian flames", "Scarlet & Violet—Obsidian Flames"),
    ("scarlet & violet paradox rift", "Scarlet & Violet—Paradox Rift"),
    ("scarlet & violet black bolt", "Scarlet & Violet—Black Bolt"),
    ("scarlet & violet white flare", "Scarlet & Violet—White Flare"),
    ("pokemon 151", "Scarlet & Violet—151"),
    ("mega evolution", "Mega Evolution"),
    ("30th celebration", "30th Celebration"),
    ("ascended heroes", "Mega Evolution—Ascended Heroes"),
    ("phantasmal flames", "Mega Evolution—Phantasmal Flames"),
    ("perfect order", "Mega Evolution—Perfect Order"),
    ("chaos rising", "Mega Evolution—Chaos Rising"),
    ("pitch black", "Mega Evolution—Pitch Black"),
    ("delta reign", "Mega Evolution—Delta Reign"),
    ("black bolt", "Scarlet & Violet—Black Bolt"),
    ("white flare", "Scarlet & Violet—White Flare"),
    ("destined rivals", "Scarlet & Violet—Destined Rivals"),
    ("journey together", "Scarlet & Violet—Journey Together"),
    ("prismatic evolutions", "Scarlet & Violet—Prismatic Evolutions"),
    ("surging sparks", "Scarlet & Violet—Surging Sparks"),
    ("stellar crown", "Scarlet & Violet—Stellar Crown"),
    ("shrouded fable", "Scarlet & Violet—Shrouded Fable"),
    ("twilight masquerade", "Scarlet & Violet—Twilight Masquerade"),
    ("temporal forces", "Scarlet & Violet—Temporal Forces"),
    ("paldean fates", "Scarlet & Violet—Paldean Fates"),
    ("paradox rift", "Scarlet & Violet—Paradox Rift"),
    ("151", "Scarlet & Violet—151"),
    ("obsidian flames", "Scarlet & Violet—Obsidian Flames"),
    ("paldea evolved", "Scarlet & Violet—Paldea Evolved"),
    ("scarlet & violet", "Scarlet & Violet"),
    ("crown zenith", "Crown Zenith"),
    ("silver tempest", "Silver Tempest"),
    ("lost origin", "Lost Origin"),
    ("pokemon go", "Pokémon GO"),
    ("astral radiance", "Astral Radiance"),
    ("brilliant stars", "Brilliant Stars"),
    ("fusion strike", "Fusion Strike"),
    ("evolving skies", "Evolving Skies"),
    ("chilling reign", "Chilling Reign"),
    ("battle styles", "Battle Styles"),
    ("shining fates", "Shining Fates"),
    ("vivid voltage", "Vivid Voltage"),
    ("darkness ablaze", "Darkness Ablaze"),
    ("hidden fates", "Hidden Fates"),
    ("celebrations", "Celebrations"),
], key=lambda pair: len(pair[0]), reverse=True))

PRODUCT_TYPE_PATTERNS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("Ultra-Premium Collection", ("ultra-premium collection", "ultra premium collection", "upc")),
    ("Elite Trainer Box (ETB)", ("elite trainer box", " etb", "etb ")),
    ("Booster Display / Booster Box", ("booster display", "booster box", "booster box display", "display box")),
    ("Booster Bundle", ("booster bundle", "bundle of booster")),
    ("Build & Battle Box", ("build & battle", "build and battle")),
    ("Premium Collection", ("premium collection",)),
    ("Collection Box", ("collection box", "special collection", "collection set")),
    ("Booster Pack", ("booster pack", "single booster", "booster (")),
    ("Blister Pack", ("3-pack blister", "three-pack blister", "blister pack", "checklane blister", "blister")),
    ("Tin", ("mini tin", "collector chest", "tin ", " tin")),
    ("Deck / Battle Deck", ("battle deck", "starter deck", "ex deck", "league battle deck", "theme deck")),
    ("Promo Card / Promo Set", ("promo card", "promo set", "promotional card")),
    ("Booster Case", ("booster case", "sealed case")),
)

IN_STOCK_PATTERNS = (
    r"\bin stock\b", r"\bon[- ]hand\b", r"\bon hand\b", r"\bready stock\b",
    r"\bavailable now\b", r"\bstocks? available\b", r"\bavailable in store\b",
    r"\bavailable for immediate purchase\b", r"\bin[- ]store stock\b",
)
CLOSED_STATUS_PATTERNS = (
    r"\bclosed\b", r"\bexpired\b", r"\bsold out\b", r"\bout of stock\b",
    r"\bunavailable\b", r"\bfully allocated\b", r"\bno longer accepting\b",
    r"\bpre[- ]?orders? closed\b", r"\bpre[- ]?order period ended\b",
)


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def clean_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, list):
        value = ", ".join(str(x) for x in value)
    text_value = html.unescape(str(value))
    text_value = BeautifulSoup(text_value, "html.parser").get_text(" ", strip=True)
    return re.sub(r"\s+", " ", text_value).strip()


def normalize_text(value: str) -> str:
    return re.sub(r"\s+", " ", value.replace("Pokémon", "Pokemon").replace("pokémon", "pokemon")).casefold()


def is_tcg_product(title: str, description: str = "", tags: Any = None) -> bool:
    combined = normalize_text(" ".join([title or "", description or "", clean_text(tags)]))
    return bool(POKEMON_RE.search(combined) and any(term in combined for term in CARD_TERMS))


def is_preorder(text: str) -> bool:
    normalized = normalize_text(text)
    return any(term in normalized for term in (
        "pre-order", "pre order", "preorder", "reserve now", "reservation", "down payment"
    ))


def incoming_signal(text: str) -> str:
    """Classify only what the public text explicitly says; never infer private logistics."""
    normalized = normalize_text(text)
    if any(re.search(pattern, normalized, re.I) for pattern in IN_TRANSIT_PATTERNS):
        return "IN_TRANSIT"
    if any(re.search(pattern, normalized, re.I) for pattern in UPCOMING_PATTERNS):
        return "UPCOMING_OR_PREORDER"
    return "NONE"


def identify_set_name(title: str) -> str:
    """Identify a known expansion when its name is present in the listing title."""
    normalized = normalize_text(title)
    normalized = normalized.replace("—", " ").replace("–", " ").replace("-", " ")
    normalized = re.sub(r"[^a-z0-9& ]+", " ", normalized)
    normalized = re.sub(r"\s+", " ", normalized).strip()
    # Anniversary phrasing is commonly used by retailers instead of the formal set name.
    if re.search(r"\b30th\s+(?:anniversary|celebration)\b", normalized):
        if "classic collection" in normalized:
            return "30th Celebration: Classic Collection"
        return "30th Celebration"
    for phrase, canonical in KNOWN_TCG_SETS:
        needle = normalize_text(phrase).replace("—", " ").replace("–", " ").replace("-", " ")
        needle = re.sub(r"[^a-z0-9& ]+", " ", needle)
        needle = re.sub(r"\s+", " ", needle).strip()
        if re.search(r"(?<![a-z0-9])" + re.escape(needle) + r"(?![a-z0-9])", normalized):
            return canonical
    # Common expansion shorthand in item titles.
    if re.search(r"\bsv0?8\b", normalized):
        return "Scarlet & Violet—Surging Sparks (SV8 shorthand)"
    if re.search(r"\bsv10\.5b?\b", normalized):
        return "Scarlet & Violet—Black Bolt (SV10.5B shorthand)"
    if re.search(r"\bsv10\.5w\b", normalized):
        return "Scarlet & Violet—White Flare (SV10.5W shorthand)"
    return "Not explicit in listing title"


def identify_product_type(title: str, description: str = "") -> str:
    """Best-effort classification of sealed Pokémon TCG product format."""
    normalized = normalize_text(f"{title} {description}")
    # Prefer the title, but descriptions can clarify an abbreviated item name.
    for label, terms in PRODUCT_TYPE_PATTERNS:
        if any(term in normalized for term in terms):
            return label
    if re.search(r"\b(booster|pack)\b", normalized):
        return "Booster product (format unclear)"
    if "pokemon tcg" in normalized or "pokemon trading card game" in normalized or "tcg" in normalized:
        return "TCG product (format unclear)"
    return "Unknown product format"


def lifecycle_status(product: dict[str, Any]) -> tuple[str, int, str]:
    """Return a conservative lifecycle status, embed color, and explanation.

    'LIVE / ORDERABLE' means the page appears orderable, not necessarily physically on-hand.
    'IN STOCK' is reserved for explicit stock wording from the page.
    """
    text_value = normalize_text(product.get("signal_text", ""))
    incoming = product.get("incoming_signal", "NONE")
    available = product.get("available")
    preorder = bool(product.get("preorder"))
    is_closed = any(re.search(pattern, text_value, re.I) for pattern in CLOSED_STATUS_PATTERNS)

    if incoming == "IN_TRANSIT":
        return "IN TRANSIT", 0x3498DB, "The public text explicitly mentions shipment/in-transit status."
    if preorder and is_closed:
        return "PRE-ORDER CLOSED / SOLD OUT", 0xE74C3C, "The listing indicates a preorder but also says it is closed or unavailable."
    if preorder and available is True:
        return "PRE-ORDER OPEN", 0xF1C40F, "The product appears orderable and preorder wording is present."
    if preorder and available is False:
        return "PRE-ORDER — STATUS CLOSED/UNAVAILABLE", 0xE67E22, "Preorder wording remains, but the public page marks the item unavailable."
    if preorder:
        return "PRE-ORDER — STATUS UNCLEAR", 0xF1C40F, "Preorder wording is present; the page does not expose reliable availability."
    if incoming == "UPCOMING_OR_PREORDER":
        return "INCOMING / UPCOMING", 0xE67E22, "The page mentions an upcoming arrival/release; this is not proof of physical transit."
    if available is False or is_closed:
        return "SOLD OUT / CLOSED", 0xE74C3C, "The public page marks the item unavailable, sold out, or closed."
    if any(re.search(pattern, text_value, re.I) for pattern in IN_STOCK_PATTERNS):
        return "IN STOCK (explicit wording)", 0x2ECC71, "The public page explicitly says in stock, on hand, or equivalent."
    if available is True:
        return "LIVE / ORDERABLE", 0x3498DB, "The public page allows ordering; physical on-hand stock is not guaranteed."
    return "UNKNOWN / NOT EXPOSED", 0x95A5A6, "The public page does not expose enough information to confirm stock status."


def extract_arrival_date(text: str) -> str | None:
    """Return a date phrase from a listing, without inventing a year or timezone."""
    value = clean_text(text)
    date_phrase = r"([A-Za-z]{3,9}[ ]+\d{4}|[A-Za-z]{3,9}[ ]+\d{1,2}(?!\d)(?:,?[ ]+\d{4})?|\d{4}-\d{2}-\d{2})"
    patterns = (
        r"\b(?:expected|estimated|est\.?|estd\.?)[ ]+(?:arrival|release)[ ]*[:：-]?[ ]*" + date_phrase,
        r"\b(?:arrival|release)[ ]+date[ ]*[:：-]?[ ]*(?:estimated?\.?[ ]+|est\.?[ ]+)?" + date_phrase,
        r"\b(?:est\.?[ ]+arrival|est\.?[ ]+release|arriving|release)[ ]*[:：-]?[ ]*" + date_phrase,
        r"\b(\d{4}-\d{2}-\d{2})\b",
    )
    for pattern in patterns:
        match = re.search(pattern, value, re.I)
        if match:
            return re.sub(r"\s+", " ", match.group(1)).strip(" ,.")[:80]
    return None


def is_relevant_announcement(title: str, summary: str = "", url: str = "") -> bool:
    text_value = normalize_text(" ".join([title, summary, url]))
    return bool(POKEMON_RE.search(text_value) and any(term in text_value for term in ANNOUNCEMENT_TERMS))


def parse_price(value: Any) -> Decimal | None:
    if value is None:
        return None
    raw = str(value).replace(",", "").strip()
    match = re.search(r"\d+(?:\.\d{1,2})?", raw)
    if not match:
        return None
    try:
        return Decimal(match.group(0))
    except InvalidOperation:
        return None


def format_price(value: Any) -> str:
    price = parse_price(value)
    if price is None:
        return "Price not shown"
    return f"₱{price:,.2f}"


def product_url(store: dict[str, Any], product: dict[str, Any]) -> str:
    raw_url = product.get("url") or product.get("onlineStoreUrl")
    handle = str(product.get("handle") or "").strip()
    if raw_url:
        absolute = urljoin(store["base"], str(raw_url))
        # Avoid accidentally linking a product to a different domain.
        if urlparse(absolute).netloc == urlparse(store["base"]).netloc:
            return absolute.split("?")[0]
    if handle:
        return f"{store['base']}/products/{handle}"
    return store["collection_url"]


def normalize_product(store: dict[str, Any], raw: dict[str, Any], source_name: str) -> dict[str, Any] | None:
    title = clean_text(raw.get("title") or raw.get("name"))
    description = clean_text(raw.get("body_html") or raw.get("descriptionHtml") or raw.get("description") or raw.get("body"))
    tags = raw.get("tags", [])
    if isinstance(tags, str):
        tags = [part.strip() for part in tags.split(",")]
    if not is_tcg_product(title, description, tags):
        return None

    handle = str(raw.get("handle") or "").strip()
    url = product_url(store, raw)
    if not handle:
        handle = urlparse(url).path.rstrip("/").split("/")[-1]
    if not handle or not title:
        return None

    variants = raw.get("variants") if isinstance(raw.get("variants"), list) else []
    variant_flags = [v.get("available") for v in variants if isinstance(v, dict) and isinstance(v.get("available"), bool)]
    if any(flag is True for flag in variant_flags):
        available: bool | None = True
    elif variant_flags and all(flag is False for flag in variant_flags):
        available = False
    elif isinstance(raw.get("available"), bool):
        available = raw.get("available")
    else:
        available = None

    prices: list[Decimal] = []
    preferred_prices: list[Decimal] = []
    for variant in variants:
        if not isinstance(variant, dict):
            continue
        price = parse_price(variant.get("price"))
        if price is not None:
            prices.append(price)
            if variant.get("available") is True:
                preferred_prices.append(price)
    if preferred_prices:
        price_text = f"{min(preferred_prices):.2f}"
    elif prices:
        price_text = f"{min(prices):.2f}"
    else:
        direct_price = raw.get("price") or raw.get("price_min")
        parsed_direct = parse_price(direct_price)
        price_text = f"{parsed_direct:.2f}" if parsed_direct is not None else None

    full_text = " ".join([title, description, clean_text(tags)])
    signal = incoming_signal(full_text)
    # Availability must not be inferred from the words "pre-order" alone: expired or
    # closed listings often still contain those words.
    normalized_full = normalize_text(full_text)
    if re.search(r"\b(closed|expired|sold out|out of stock|unavailable|fully allocated|no longer accepting)\b", normalized_full, re.I):
        available = False
    elif re.search(r"\b(open pre[- ]?order|pre[- ]?order now|add to cart|buy now|in stock|on hand)\b", normalized_full, re.I):
        if available is None:
            available = True
    product = {
        "store_key": store["key"],
        "store": store["name"],
        "handle": handle,
        "title": title[:250],
        "url": url,
        "available": available,
        "price": price_text,
        "preorder": is_preorder(full_text),
        "incoming_signal": signal,
        "arrival_date": extract_arrival_date(full_text),
        "signal_text": full_text[:1800],
        "source": source_name,
    }
    product["set_name"] = identify_set_name(title)
    product["product_type"] = identify_product_type(title, description)
    status, status_color, status_note = lifecycle_status(product)
    product["lifecycle_status"] = status
    product["lifecycle_color"] = status_color
    product["lifecycle_note"] = status_note
    return product


def merge_product(existing: dict[str, Any] | None, incoming: dict[str, Any]) -> dict[str, Any]:
    if existing is None:
        return incoming
    merged = dict(existing)
    for field in ("store_key", "store", "handle", "title", "url", "source"):
        if incoming.get(field):
            merged[field] = incoming[field]
    # Do not let a search-suggestion response with sparse fields erase a useful state.
    if incoming.get("available") is not None:
        merged["available"] = incoming["available"]
    if incoming.get("price") is not None:
        merged["price"] = incoming["price"]
    if incoming.get("preorder") is True:
        merged["preorder"] = True
    elif "preorder" not in merged:
        merged["preorder"] = incoming.get("preorder", False)
    for field in ("incoming_signal", "arrival_date", "signal_text", "set_name", "product_type", "lifecycle_status", "lifecycle_color", "lifecycle_note"):
        if incoming.get(field):
            merged[field] = incoming[field]
        elif field not in merged:
            merged[field] = incoming.get(field)
    return merged


def request_session() -> requests.Session:
    session = requests.Session()
    session.headers.update({
        "User-Agent": USER_AGENT,
        "Accept": "application/json, application/atom+xml, application/rss+xml, application/xml, text/html;q=0.9, */*;q=0.8",
        "Accept-Language": "en-PH,en;q=0.9",
    })
    return session


def get_response(session: requests.Session, url: str, params: dict[str, str] | None = None) -> requests.Response | None:
    try:
        response = session.get(url, params=params, timeout=(8, 18))
        if response.status_code in (429, 500, 502, 503, 504):
            LOG.warning("Temporary HTTP %s from %s", response.status_code, url)
        response.raise_for_status()
        return response
    except requests.RequestException as exc:
        LOG.warning("Could not fetch %s: %s", url, exc.__class__.__name__)
        return None


def products_from_json_response(data: Any) -> list[dict[str, Any]] | None:
    if isinstance(data, dict) and isinstance(data.get("products"), list):
        return [p for p in data["products"] if isinstance(p, dict)]
    if isinstance(data, dict):
        resources = data.get("resources", {})
        results = resources.get("results", {}) if isinstance(resources, dict) else {}
        products = results.get("products") if isinstance(results, dict) else None
        if isinstance(products, list):
            return [p for p in products if isinstance(p, dict)]
    return None


def parse_html_product_cards(store: dict[str, Any], page_url: str, source_name: str, content: str) -> list[dict[str, Any]]:
    soup = BeautifulSoup(content, "html.parser")
    records: dict[str, dict[str, Any]] = {}
    for anchor in soup.select('a[href]'):
        raw_href = anchor.get("href", "")
        if not re.search(r"/(?:products?|listings)/[^/?#]+", raw_href, re.I):
            continue
        href = urljoin(page_url, raw_href)
        if urlparse(href).netloc != urlparse(store["base"]).netloc:
            continue
        handle = urlparse(href).path.rstrip("/").split("/")[-1]
        if not handle:
            continue
        parent = anchor
        # Product-card containers differ by theme; inspect a few ancestors only.
        for _ in range(5):
            if parent.parent is None:
                break
            parent = parent.parent
            if parent.name in ("article", "li") or any("product" in str(c).lower() or "card" in str(c).lower() for c in parent.get("class", [])):
                break
        title = clean_text(anchor.get_text(" ", strip=True))
        if not title:
            img = anchor.find("img")
            title = clean_text(img.get("alt") if img else "")
        card_text = clean_text(parent.get_text(" ", strip=True))
        if not title:
            match = re.search(r"/(?:products?|listings)/([^/?#]+)", href)
            title = match.group(1).replace("-", " ") if match else ""
        available: bool | None = None
        if re.search(r"\b(closed|expired|sold out|out of stock|unavailable|fully allocated|no longer accepting)\b", card_text, re.I):
            available = False
        elif re.search(r"\b(open pre[- ]?order|pre[- ]?order now|add to cart|buy now|in stock|on hand)\b", card_text, re.I):
            available = True
        price_match = re.search(r"(?:₱|PHP\s*)\s*[\d,]+(?:\.\d{2})?", card_text, re.I)
        raw = {
            "title": title,
            "handle": handle,
            "url": href,
            "description": card_text,
            "available": available,
            "price": price_match.group(0) if price_match else None,
        }
        item = normalize_product(store, raw, source_name)
        if item:
            records[item["handle"]] = merge_product(records.get(item["handle"]), item)
    return list(records.values())


def scrape_catalog_url(session: requests.Session, store: dict[str, Any], endpoint: tuple[str, str, str]) -> tuple[list[dict[str, Any]], bool]:
    source_name, api_url, fallback_url = endpoint
    response = get_response(session, api_url)
    if response is not None:
        try:
            data = response.json()
            raw_products = products_from_json_response(data)
            if raw_products is not None:
                normalized: dict[str, dict[str, Any]] = {}
                for raw in raw_products:
                    item = normalize_product(store, raw, source_name)
                    if item:
                        normalized[item["handle"]] = merge_product(normalized.get(item["handle"]), item)
                return list(normalized.values()), True
            LOG.warning("Unexpected catalogue JSON format from %s", api_url)
        except (ValueError, TypeError):
            LOG.warning("Non-JSON response from catalogue endpoint %s", api_url)

    # If the public JSON endpoint is unavailable, fall back to the public storefront page.
    fallback = get_response(session, fallback_url)
    if fallback is None:
        return [], False
    return parse_html_product_cards(store, fallback_url, f"{source_name} HTML fallback", fallback.text), True


def scrape_search_suggestions(session: requests.Session, store: dict[str, Any]) -> tuple[list[dict[str, Any]], bool]:
    params = {
        "q": "pokemon",
        "resources[type]": "product",
        "resources[limit]": "25",
        "resources[options][unavailable_products]": "show",
    }
    response = get_response(session, store["suggest_url"], params=params)
    if response is None:
        # An ordinary public search page is the fallback; a missing endpoint isn't fatal.
        search_url = f"{store['base']}/search?q=pokemon&type=product"
        html_response = get_response(session, search_url)
        if html_response is None:
            return [], False
        return parse_html_product_cards(store, search_url, "Store search HTML fallback", html_response.text), True
    try:
        data = response.json()
    except ValueError:
        return [], False
    raw_products = products_from_json_response(data)
    if raw_products is None:
        return [], False
    normalized: dict[str, dict[str, Any]] = {}
    for raw in raw_products:
        item = normalize_product(store, raw, "Store search suggestions")
        if item:
            normalized[item["handle"]] = merge_product(normalized.get(item["handle"]), item)
    return list(normalized.values()), True


def scrape_store_products(session: requests.Session, store: dict[str, Any]) -> tuple[dict[str, dict[str, Any]], bool]:
    found: dict[str, dict[str, Any]] = {}
    source_successes: list[bool] = []
    for endpoint in store.get("catalog_urls", []):
        products, success = scrape_catalog_url(session, store, endpoint)
        source_successes.append(success)
        for product in products:
            found[product["handle"]] = merge_product(found.get(product["handle"]), product)

    if store.get("suggest_url"):
        suggestions, suggest_success = scrape_search_suggestions(session, store)
        source_successes.append(suggest_success)
        for product in suggestions:
            found[product["handle"]] = merge_product(found.get(product["handle"]), product)

    # Extra public HTML pages are used for retailers that do not expose a known
    # Shopify-style JSON catalogue. This parses visible server-rendered product
    # links only; it does not execute browser JavaScript or bypass logins.
    for source_name, page_url in store.get("html_urls", []):
        response = get_response(session, page_url)
        if response is None:
            source_successes.append(False)
            continue
        page_products = parse_html_product_cards(store, page_url, source_name, response.text)
        source_successes.append(True)
        for product in page_products:
            found[product["handle"]] = merge_product(found.get(product["handle"]), product)
        if not page_products:
            LOG.warning("Fetched %s but found no public HTML product links matching the Pokémon TCG filter; JavaScript-only listings may not be visible", page_url)

    # A source is healthy if at least one of its configured public paths could be read.
    return found, any(source_successes)


def entry_text(element: ET.Element, names: tuple[str, ...]) -> str:
    for name in names:
        # Accept namespaced Atom and non-namespaced RSS tags.
        matches = element.findall(f".//{{*}}{name}") + element.findall(f".//{name}")
        for node in matches:
            if name == "link":
                href = node.attrib.get("href")
                if href:
                    return href.strip()
            if node.text and node.text.strip():
                return clean_text(node.text)
    return ""


def parse_feed_items(xml_text: str, feed_url: str, store: dict[str, Any]) -> list[dict[str, str]]:
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return []
    elements = root.findall(".//{*}entry")
    if not elements:
        elements = root.findall(".//item")
    items: list[dict[str, str]] = []
    for element in elements:
        title = entry_text(element, ("title",))
        link = entry_text(element, ("link", "guid"))
        summary = entry_text(element, ("summary", "description", "content", "encoded"))
        published = entry_text(element, ("published", "updated", "pubDate", "date"))
        if not link:
            link = feed_url
        else:
            link = urljoin(store["base"], link)
        if is_relevant_announcement(title, summary, link):
            items.append({
                "store_key": store["key"],
                "store": store["name"],
                "title": title[:250] or "Pokémon stock announcement",
                "url": link,
                "published": published[:80],
                "summary": summary[:1000],
                "feed": feed_url,
            })
    return items


def scrape_announcements(session: requests.Session, store: dict[str, Any]) -> tuple[list[dict[str, str]], dict[str, bool]]:
    all_items: dict[str, dict[str, str]] = {}
    feed_success: dict[str, bool] = {}
    for feed_url in store["feeds"]:
        response = get_response(session, feed_url)
        feed_success[feed_url] = response is not None
        if response is None:
            continue
        for item in parse_feed_items(response.text, feed_url, store):
            all_items[item["url"]] = item
    return list(all_items.values()), feed_success


def parse_official_product_news(content: str, page_url: str, source: dict[str, str]) -> list[dict[str, str]]:
    """Parse public product/release entries from the official Pokémon TCG news page.

    This labels official release news as product/release information, not store stock.
    It deliberately skips tournaments, registrations, and general campaigns.
    """
    soup = BeautifulSoup(content, "html.parser")
    product_terms = (
        "arriving", "new expansion", "expansion battle", "release promo", "new promo cards",
        "product availability", "product release", "arriving on", "pokemon tcg: ",
        "pokemon trading card game",
    )
    results: dict[str, dict[str, str]] = {}
    for anchor in soup.select("a[href]"):
        title = clean_text(anchor.get_text(" ", strip=True))
        href = urljoin(page_url, anchor.get("href", ""))
        if not title or href.rstrip("/") == page_url.rstrip("/"):
            continue
        parent = anchor
        for _ in range(3):
            if parent.parent is None:
                break
            parent = parent.parent
            if parent.name in ("li", "article"):
                break
        context = clean_text(parent.get_text(" ", strip=True))
        combined = normalize_text(title + " " + context)
        is_product_item = any(term in combined for term in product_terms)
        # Site is the official Pokémon TCG news hub. Require product/release language,
        # so league sign-ups and general tournament notices do not become stock alerts.
        if not is_product_item:
            continue
        if any(term in combined for term in ("registration", "league season", "championship series", "academia:", "roadshow", "tournament")) and not any(term in combined for term in ("arriving", "new expansion", "release promo", "product availability", "new promo cards")):
            continue
        results[href] = {
            "store_key": source["key"],
            "store": source["name"],
            "title": title[:250],
            "url": href,
            "published": "",
            "summary": context[:1000],
            "feed": page_url,
            "official_product_news": "true",
        }
    return list(results.values())


def default_state() -> dict[str, Any]:
    return {
        "schema_version": 1,
        "welcome_sent": False,
        "initialized_stores": {},
        "store_health": {},
        "products": {},
        "initialized_feeds": {},
        "announcements": {},
        "source_report_sent": False,
        "source_report_version": 0,
        "official_source_health": {},
        "metrics": {
            "total_restock_signals": 0,
            "total_new_orderable_listings": 0,
            "total_preorder_signals": 0,
            "total_explicit_in_transit_signals": 0,
        },
    }


def load_state() -> dict[str, Any]:
    if not STATE_PATH.exists():
        return default_state()
    try:
        value = json.loads(STATE_PATH.read_text(encoding="utf-8"))
        if not isinstance(value, dict):
            raise ValueError("state root is not an object")
        base = default_state()
        base.update(value)
        stored_metrics = value.get("metrics", {})
        if not isinstance(stored_metrics, dict):
            stored_metrics = {}
        base["metrics"].update(stored_metrics)
        for key in ("initialized_stores", "store_health", "products", "initialized_feeds", "announcements", "official_source_health"):
            if not isinstance(base.get(key), dict):
                base[key] = {}
        return base
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        # Keep a damaged state file from crashing the workflow; user-visible baseline note is sent.
        LOG.error("State file could not be read (%s); starting a new baseline", exc.__class__.__name__)
        return default_state()


def save_state(state: dict[str, Any]) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    temporary = STATE_PATH.with_suffix(STATE_PATH.suffix + ".tmp")
    temporary.write_text(json.dumps(state, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    temporary.replace(STATE_PATH)


def product_state_key(store_key: str, handle: str) -> str:
    return f"{store_key}|{handle}"


def announcement_state_key(store_key: str, url: str) -> str:
    return f"{store_key}|{url}"


def decimal_greater(old_value: Any, new_value: Any) -> bool:
    old_price, new_price = parse_price(old_value), parse_price(new_value)
    return old_price is not None and new_price is not None and old_price > new_price


def stock_status(available: bool | None) -> str:
    if available is True:
        return "Orderable according to the public product page"
    if available is False:
        return "Currently marked unavailable / sold out"
    return "Availability not exposed by the public page"


def product_alert(kind: str, product: dict[str, Any], old: dict[str, Any] | None = None) -> dict[str, Any]:
    """Build a compact, scannable Discord embed for one TCG product signal."""
    status = stock_status(product.get("available"))
    price_text = format_price(product.get("price"))
    title_prefix = {
        "preorder": "📅 PRE-ORDER / UPCOMING TCG SIGNAL",
        "new_in_stock": "🟢 NEW TCG LISTING — ORDERABLE",
        "new_unavailable": "🕵️ POSSIBLE STEALTH DROP — NEW LISTING",
        "new_unknown": "🕵️ POSSIBLE STEALTH DROP — AVAILABILITY UNKNOWN",
        "restock": "🚨 POSSIBLE STEALTH DROP — RESTOCK SIGNAL",
        "preorder_flag": "📅 PRE-ORDER SIGNAL DETECTED",
        "in_transit": "🚚 RETAILER SAYS STOCK IS IN TRANSIT",
        "upcoming": "📦 UPCOMING TCG STOCK SIGNAL",
        "price_drop": "💸 TCG PRICE DROP",
    }.get(kind, "🔔 POKÉMON TCG UPDATE")

    signal = product.get("incoming_signal")
    if signal == "IN_TRANSIT":
        incoming_text = "Explicit in-transit/shipment wording found"
    elif signal == "UPCOMING_OR_PREORDER" or product.get("preorder"):
        incoming_text = "Pre-order / upcoming wording found; shipment not confirmed"
    else:
        incoming_text = "No public incoming-stock wording found"

    description_parts = []
    if old and kind == "price_drop":
        description_parts.append(f"**Previous price:** {format_price(old.get('price'))}")
    if product.get("signal_text"):
        description_parts.append(f"**Evidence from public page:** {product['signal_text'][:650]}")
    description_parts.append("This is a public-page signal, not access to private distributor or warehouse systems.")
    url = product.get("url") or ""
    source_value = f"[Open product page]({url})" if url else "Product link not provided by source"
    lifecycle, lifecycle_color, lifecycle_note = lifecycle_status(product)
    fields = [
        {"name": "Retailer", "value": str(product.get("store", "Unknown retailer"))[:1024], "inline": True},
        {"name": "Lifecycle status", "value": f"**{lifecycle}**\n{lifecycle_note}"[:1024], "inline": True},
        {"name": "Set / expansion", "value": str(product.get("set_name") or identify_set_name(str(product.get("title", ""))))[:1024], "inline": True},
        {"name": "Product format", "value": str(product.get("product_type") or identify_product_type(str(product.get("title", "")), str(product.get("signal_text", ""))))[:1024], "inline": True},
        {"name": "Price shown", "value": price_text[:1024], "inline": True},
        {"name": "Availability detail", "value": status[:1024], "inline": True},
        {"name": "Upcoming / shipment signal", "value": incoming_text, "inline": False},
        {"name": "ETA / release date", "value": str(product.get("arrival_date") or "Not published by source")[:1024], "inline": True},
        {"name": "Branch-level stock", "value": "Not published by monitored page", "inline": True},
        {"name": "Total units", "value": "Not published by monitored page", "inline": True},
        {"name": "Source", "value": source_value[:1024], "inline": False},
    ]
    if kind == "restock":
        color = 0xE74C3C  # newly orderable transition worth checking immediately
    elif kind == "price_drop":
        color = 0x3498DB
    elif kind in ("in_transit", "upcoming", "preorder", "preorder_flag"):
        color = lifecycle_color
    else:
        color = lifecycle_color
    return {
        "title": f"{title_prefix}: {product.get('title', 'Pokémon TCG product')}"[:256],
        "url": url or None,
        "description": "\n\n".join(description_parts)[:4000],
        "fields": fields,
        "color": color,
    }


def announcement_alert(item: dict[str, str]) -> dict[str, Any]:
    is_official = item.get("official_product_news") == "true"
    title_prefix = "🟣 OFFICIAL POKÉMON TCG PRODUCT / RELEASE NEWS" if is_official else "📣 RETAILER-PUBLISHED TCG ANNOUNCEMENT"
    note = (
        "Official product/release news; this does not confirm Philippine retail stock."
        if is_official else
        "A retailer announcement was published. Open the original post to verify date, preorder status, and availability."
    )
    fields = [
        {"name": "Set / expansion", "value": identify_set_name(item.get("title", "") + " " + item.get("summary", ""))[:1024], "inline": True},
        {"name": "Product format", "value": identify_product_type(item.get("title", ""), item.get("summary", ""))[:1024], "inline": True},
        {"name": "Source", "value": item.get("store", "Unknown source")[:1024], "inline": True},
        {"name": "Published", "value": item.get("published") or "Date not provided in parsed page", "inline": True},
        {"name": "What this confirms", "value": note, "inline": False},
    ]
    return {
        "title": f"{title_prefix}: {item.get('title', 'Pokémon TCG update')}"[:256],
        "url": item.get("url"),
        "description": (item.get("summary") or "Open the source link for announcement details.")[:1200],
        "fields": fields,
        "color": 0x8E44AD if is_official else 0x5865F2,
    }


def health_alert(store: dict[str, Any], healthy: bool) -> dict[str, Any]:
    title = "✅ Retailer watcher recovered" if healthy else "⚠️ Retailer product pages may be blocking the watcher"
    status = "Product pages are responding again." if healthy else "The watcher could not read any configured product/search endpoint for this retailer. Alerts from this store may be delayed until it recovers."
    return {
        "title": f"{title}: {store['name']}"[:256],
        "description": status + f"\n\n[Open store collection]({store['collection_url']})",
        "url": store["collection_url"],
        "color": 0x2ECC71 if healthy else 0xE74C3C,
    }


def discord_embed_char_count(embed: dict[str, Any]) -> int:
    """Return the text count Discord applies to an embed's aggregate character budget."""
    total = len(str(embed.get("title", ""))) + len(str(embed.get("description", "")))
    for field in embed.get("fields", []) or []:
        total += len(str(field.get("name", ""))) + len(str(field.get("value", "")))
    footer = embed.get("footer") or {}
    author = embed.get("author") or {}
    total += len(str(footer.get("text", ""))) + len(str(author.get("name", "")))
    return total


def prepare_discord_embed(source: dict[str, Any]) -> dict[str, Any]:
    """Clamp embed properties to Discord limits and keep each embed under the aggregate cap."""
    embed = dict(source)
    if "title" in embed:
        embed["title"] = str(embed.get("title") or "")[:256]
    if "description" in embed:
        embed["description"] = str(embed.get("description") or "")[:4096]

    # Invalid or oversized optional URL values can also make a webhook payload fail.
    for url_key in ("url",):
        url = embed.get(url_key)
        if url is not None and not (isinstance(url, str) and url.startswith(("https://", "http://"))):
            embed.pop(url_key, None)

    cleaned_fields = []
    for field in (embed.get("fields", []) or [])[:25]:
        name = str(field.get("name", "Field"))[:256] or "Field"
        value = str(field.get("value", "—"))[:1024] or "—"
        cleaned_fields.append({"name": name, "value": value, "inline": bool(field.get("inline", False))})
    if "fields" in embed:
        embed["fields"] = cleaned_fields

    for parent_key, child_key, limit in (("footer", "text", 2048), ("author", "name", 256)):
        child = embed.get(parent_key)
        if isinstance(child, dict) and child_key in child:
            child = dict(child)
            child[child_key] = str(child.get(child_key) or "")[:limit]
            embed[parent_key] = child

    # Keep the information in fields first; trim long narrative text if an embed is
    # individually too large. Most importantly, never send a malformed oversized embed.
    while discord_embed_char_count(embed) > MAX_DISCORD_EMBED_CHARS:
        excess = discord_embed_char_count(embed) - MAX_DISCORD_EMBED_CHARS
        description = str(embed.get("description", ""))
        if description:
            new_length = max(0, len(description) - excess)
            embed["description"] = description[:new_length].rstrip()
            continue
        fields = embed.get("fields", []) or []
        if fields:
            last = fields[-1]
            value = str(last.get("value", ""))
            if len(value) > 1:
                last["value"] = value[:max(1, len(value) - excess)].rstrip() or "—"
            else:
                fields.pop()
            continue
        footer = embed.get("footer") or {}
        footer_text = str(footer.get("text", ""))
        if footer_text:
            footer["text"] = footer_text[:max(0, len(footer_text) - excess)].rstrip()
            embed["footer"] = footer
            continue
        title = str(embed.get("title", ""))
        if title:
            embed["title"] = title[:max(0, len(title) - excess)].rstrip()
            continue
        break
    return embed


def chunk_discord_embeds(embeds: list[dict[str, Any]]) -> list[list[dict[str, Any]]]:
    """Pack webhook messages under both Discord's per-message embed limits."""
    prepared = [prepare_discord_embed(embed) for embed in embeds]
    chunks: list[list[dict[str, Any]]] = []
    current: list[dict[str, Any]] = []
    current_chars = 0
    for embed in prepared:
        char_count = discord_embed_char_count(embed)
        if current and (len(current) >= MAX_DISCORD_EMBEDS or current_chars + char_count > MAX_DISCORD_EMBED_CHARS):
            chunks.append(current)
            current = []
            current_chars = 0
        current.append(embed)
        current_chars += char_count
    if current:
        chunks.append(current)
    return chunks or [[]]


def send_discord(session: requests.Session, embeds: list[dict[str, Any]], content: str | None = None) -> None:
    if not DISCORD_WEBHOOK_URL:
        raise RuntimeError("DISCORD_WEBHOOK_URL is missing. Add it as a GitHub Actions repository secret.")
    chunks = chunk_discord_embeds(embeds)
    for index, chunk in enumerate(chunks):
        payload: dict[str, Any] = {"embeds": chunk, "allowed_mentions": {"parse": []}}
        if content and index == 0:
            payload["content"] = content[:2000]
        # Retry only transient webhook failures; never print the secret URL.
        delivered = False
        for attempt in range(3):
            try:
                response = session.post(DISCORD_WEBHOOK_URL, json=payload, timeout=15)
                if response.status_code in (200, 204):
                    delivered = True
                    break
                if response.status_code == 429 or 500 <= response.status_code < 600:
                    retry_after = 1.5
                    try:
                        retry_after = min(float(response.json().get("retry_after", retry_after)), 10.0)
                    except (ValueError, AttributeError, TypeError):
                        pass
                    LOG.warning("Discord returned HTTP %s; retrying", response.status_code)
                    time.sleep(retry_after)
                    continue
                # Discord's response body gives a useful validation path (e.g. embeds[0]);
                # include only that response detail and never the webhook URL or payload.
                detail = re.sub(r"\s+", " ", str(getattr(response, "text", ""))).strip()[:900]
                suffix = f": {detail}" if detail else ""
                raise RuntimeError(f"Discord webhook returned HTTP {response.status_code}{suffix}")
            except requests.RequestException as exc:
                if attempt == 2:
                    raise RuntimeError(f"Discord webhook request failed: {exc.__class__.__name__}") from exc
                time.sleep(1.5 * (attempt + 1))
        if not delivered:
            raise RuntimeError("Discord webhook could not deliver an alert after retries")
        if index < len(chunks) - 1:
            time.sleep(2.0)


def main() -> int:
    if not DISCORD_WEBHOOK_URL:
        LOG.error("DISCORD_WEBHOOK_URL secret is required; refusing to create a silent baseline.")
        return 2

    state = load_state()
    session = request_session()
    product_alerts: list[dict[str, Any]] = []
    announcement_alerts: list[dict[str, Any]] = []
    health_alerts: list[dict[str, Any]] = []
    total_products = 0
    total_available = 0
    store_counts: dict[str, tuple[int, int, bool]] = {}
    current_products_by_store: dict[str, list[dict[str, Any]]] = {}
    baseline_current_stock: list[dict[str, Any]] = []
    run_metrics = {"restock": 0, "new_orderable": 0, "preorder": 0, "upcoming": 0, "in_transit": 0}

    # Catalogue checks: detect new listings, pre-order listings, restocks and price cuts.
    for store in STORES:
        products, healthy = scrape_store_products(session, store)
        current_products_by_store[store["key"]] = list(products.values())
        store_counts[store["key"]] = (
            len(products),
            sum(1 for p in products.values() if p.get("available") is True),
            healthy,
        )
        total_products += len(products)
        total_available += sum(1 for p in products.values() if p.get("available") is True)
        previous_health = state["store_health"].get(store["key"])
        if previous_health is True and healthy is False:
            health_alerts.append(health_alert(store, False))
        elif previous_health is False and healthy is True:
            health_alerts.append(health_alert(store, True))
        state["store_health"][store["key"]] = healthy

        already_initialized = state["initialized_stores"].get(store["key"]) is True
        if not already_initialized and healthy:
            baseline_current_stock.extend(
                product for product in products.values()
                if product.get("available") is True or product.get("incoming_signal") in ("IN_TRANSIT", "UPCOMING_OR_PREORDER")
            )
        if not healthy:
            LOG.warning("No product catalogue/search endpoint was available for %s", store["name"])
            continue

        for handle, product in products.items():
            key = product_state_key(store["key"], handle)
            old = state["products"].get(key)
            # Keep old stock status when a secondary endpoint omits availability.
            if old and product.get("available") is None and old.get("available") is not None:
                product["available"] = old.get("available")
            if not already_initialized:
                if old is None:
                    product["first_seen"] = utc_now_iso()
                    state["products"][key] = product
                else:
                    state["products"][key] = merge_product(old, product)
                continue

            if old is None:
                product["first_seen"] = utc_now_iso()
                if product.get("incoming_signal") == "IN_TRANSIT":
                    product_alerts.append(product_alert("in_transit", product))
                    run_metrics["in_transit"] += 1
                elif product.get("incoming_signal") == "UPCOMING_OR_PREORDER":
                    product_alerts.append(product_alert("upcoming", product))
                    run_metrics["upcoming"] += 1
                    if product.get("preorder"):
                        run_metrics["preorder"] += 1
                elif product.get("preorder"):
                    product_alerts.append(product_alert("preorder", product))
                    run_metrics["preorder"] += 1
                elif product.get("available") is True:
                    product_alerts.append(product_alert("new_in_stock", product))
                    run_metrics["new_orderable"] += 1
                elif product.get("available") is False:
                    product_alerts.append(product_alert("new_unavailable", product))
                else:
                    product_alerts.append(product_alert("new_unknown", product))
                state["products"][key] = product
                continue

            old_signal = old.get("incoming_signal")
            new_signal = product.get("incoming_signal")
            # Only report a newly observed signal when the previous state already had
            # this field, avoiding false alarms on migration from the earlier watcher.
            if "incoming_signal" in old and old_signal != "IN_TRANSIT" and new_signal == "IN_TRANSIT":
                product_alerts.append(product_alert("in_transit", product, old))
                run_metrics["in_transit"] += 1
            elif "incoming_signal" in old and old_signal not in ("IN_TRANSIT", "UPCOMING_OR_PREORDER") and new_signal == "UPCOMING_OR_PREORDER":
                product_alerts.append(product_alert("upcoming", product, old))
                run_metrics["upcoming"] += 1
                if product.get("preorder"):
                    run_metrics["preorder"] += 1
            elif old.get("available") is not True and product.get("available") is True:
                # Public page changed to an orderable state; this could be a restock/stealth drop.
                product_alerts.append(product_alert("restock", product, old))
                run_metrics["restock"] += 1
            elif old.get("preorder") is not True and product.get("preorder") is True:
                product_alerts.append(product_alert("preorder_flag", product, old))
                run_metrics["preorder"] += 1
            elif decimal_greater(old.get("price"), product.get("price")):
                product_alerts.append(product_alert("price_drop", product, old))

            # Update only fields we actually monitor; leave first_seen stable.
            first_seen = old.get("first_seen", utc_now_iso())
            new_record = dict(product)
            new_record["first_seen"] = first_seen
            state["products"][key] = new_record

        state["initialized_stores"][store["key"]] = True

    # Official public retailer blog/event feeds. Each feed is baselined on its first successful read.
    for store in STORES:
        items, feed_health = scrape_announcements(session, store)
        for feed_url, success in feed_health.items():
            if not success:
                continue
            feed_already_initialized = state["initialized_feeds"].get(feed_url) is True
            for item in items:
                if item.get("feed") != feed_url:
                    continue
                key = announcement_state_key(store["key"], item["url"])
                if key in state["announcements"]:
                    continue
                state["announcements"][key] = {
                    "store": store["name"],
                    "title": item["title"],
                    "url": item["url"],
                    "published": item.get("published", ""),
                    "first_seen": utc_now_iso(),
                }
                if feed_already_initialized:
                    announcement_alerts.append(announcement_alert(item))
            state["initialized_feeds"][feed_url] = True

    # Official Pokémon TCG Asia / Philippines public product-news page.
    official_news_counts: dict[str, int] = {}
    for source in OFFICIAL_NEWS_SOURCES:
        response = get_response(session, source["url"])
        healthy = response is not None
        prior_health = state["official_source_health"].get(source["key"])
        state["official_source_health"][source["key"]] = healthy
        if response is None:
            LOG.warning("Official news page could not be read: %s", source["url"])
            continue
        items = parse_official_product_news(response.text, source["url"], source)
        official_news_counts[source["key"]] = len(items)
        page_already_initialized = state["initialized_feeds"].get(source["url"]) is True
        LOG.info("Official news page fetched: %s product/release item(s) matched", len(items))
        for item in items:
            key = announcement_state_key(source["key"], item["url"])
            if key in state["announcements"]:
                continue
            state["announcements"][key] = {
                "store": source["name"], "title": item["title"], "url": item["url"],
                "published": item.get("published", ""), "first_seen": utc_now_iso(),
            }
            if page_already_initialized:
                announcement_alerts.append(announcement_alert(item))
        state["initialized_feeds"][source["url"]] = True

    # Keep cumulative counts across GitHub Actions runs. These are detected signals,
    # not physical unit counts and not a branch-by-branch inventory total.
    metrics = state.setdefault("metrics", {})
    for key, value in {
        "total_restock_signals": run_metrics["restock"],
        "total_new_orderable_listings": run_metrics["new_orderable"],
        "total_preorder_signals": run_metrics["preorder"],
        "total_explicit_in_transit_signals": run_metrics["in_transit"],
    }.items():
        metrics[key] = int(metrics.get(key, 0) or 0) + value

    # Cap saved history, keeping the most recently added observations.
    state["products"] = dict(sorted(
        state["products"].items(),
        key=lambda pair: str(pair[1].get("first_seen", "")),
        reverse=True,
    )[:MAX_STATE_PRODUCTS])
    state["announcements"] = dict(sorted(
        state["announcements"].items(),
        key=lambda pair: str(pair[1].get("first_seen", "")),
        reverse=True,
    )[:MAX_STATE_ANNOUNCEMENTS])

    outbox = health_alerts + announcement_alerts + product_alerts
    if outbox:
        LOG.info("Sending %s alert(s): %s announcement(s), %s product update(s), %s source-health update(s)",
                 len(outbox), len(announcement_alerts), len(product_alerts), len(health_alerts))
        send_discord(session, outbox, content=f"🔎 Pokémon PH Stock Watch detected {len(outbox)} update(s). Open the product or source link to verify availability.")

    if not state.get("welcome_sent"):
        rows = []
        for store in STORES:
            count, available, healthy = store_counts[store["key"]]
            health_label = "reachable" if healthy else "not reachable on this run"
            rows.append(f"**{store['name']}:** {count} TCG listing(s) found, {available} marked orderable — {health_label}.")
        digest_lines = []
        # Show a compact one-time snapshot so the user can see current orderable listings
        # without pretending they're new drops. Limit detail to fit within Discord embed size.
        seen_snapshot: set[tuple[str, str]] = set()
        for product in sorted(baseline_current_stock, key=lambda p: (p.get("store", ""), p.get("title", "").casefold())):
            identity = (str(product.get("store_key", "")), str(product.get("handle", "")))
            if identity in seen_snapshot:
                continue
            seen_snapshot.add(identity)
            price = format_price(product.get("price"))
            signal = product.get("incoming_signal", "NONE")
            label = "IN TRANSIT CLAIM" if signal == "IN_TRANSIT" else ("UPCOMING / PRE-ORDER" if signal == "UPCOMING_OR_PREORDER" else "ORDERABLE")
            date_text = f" — {product.get('arrival_date')}" if product.get("arrival_date") else ""
            digest_lines.append(f"• **{label} · {product.get('store', 'Retailer')}** — [{product.get('title', 'Pokémon TCG product')}]({product.get('url')}) ({price}){date_text}")
            if len(digest_lines) >= 16:
                break
        total_snapshot = len({(str(p.get("store_key", "")), str(p.get("handle", ""))) for p in baseline_current_stock})
        if total_snapshot > len(digest_lines):
            digest_lines.append(f"… plus {total_snapshot - len(digest_lines)} other orderable/upcoming public listing(s); open the source pages to review them.")
        snapshot_block = "\n\n**Current public orderable / upcoming listing snapshot**\n" + "\n".join(digest_lines) if digest_lines else "\n\nNo TCG products were marked orderable or upcoming in the first scan, or the retailer did not expose those signals."
        welcome = {
            "title": "✅ Pokémon PH Stock Watch is online",
            "description": (
                "The first scan is now saved as a baseline, so existing listings won't all ping you as new drops. "
                "Future new TCG listings, newly orderable products, retailer-published pre-order/arrival wording, "
                "explicit in-transit wording, price cuts, retailer announcements, and official TCG product news can trigger alerts.\n\n"
                + "\n".join(rows)
                + snapshot_block
                + "\n\nThe watcher checks on a roughly five-minute GitHub Actions schedule. Scheduled jobs can be delayed; this is not a guaranteed instant-stock feed."
            )[:4000],
            "color": 0x2ECC71,
        }
        send_discord(session, [welcome])
        state["welcome_sent"] = True

    # Versioned source report: resend when the lifecycle/set-status board is upgraded.
    # Use Discord fields and separate embeds so source health is easy to scan.
    if int(state.get("source_report_version", 0) or 0) < 3:
        scan_products = [p for items in current_products_by_store.values() for p in items]
        scan_tcg_count = len(scan_products)
        scan_orderable = sum(1 for p in scan_products if p.get("available") is True)
        scan_preorder = sum(1 for p in scan_products if str(p.get("lifecycle_status", "")).startswith("PRE-ORDER"))
        scan_transit = sum(1 for p in scan_products if p.get("lifecycle_status") == "IN TRANSIT")
        scan_in_stock = sum(1 for p in scan_products if p.get("lifecycle_status") == "IN STOCK (explicit wording)")
        scan_live = sum(1 for p in scan_products if p.get("lifecycle_status") == "LIVE / ORDERABLE")
        scan_incoming = sum(1 for p in scan_products if p.get("lifecycle_status") == "INCOMING / UPCOMING")
        scan_soldout = sum(1 for p in scan_products if p.get("lifecycle_status") in ("SOLD OUT / CLOSED", "PRE-ORDER CLOSED / SOLD OUT", "PRE-ORDER — STATUS CLOSED/UNAVAILABLE"))
        reachable_retailers = sum(1 for _, _, healthy in store_counts.values() if healthy)
        known_retailers = len(STORES)
        restock_total = int(metrics.get("total_restock_signals", 0) or 0)
        now_text = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

        summary = {
            "title": "🛰️ Pokémon TCG Drop Radar · Scan Summary",
            "description": (
                f"**Scan time:** {now_text}\n"
                f"**Retailer coverage:** {reachable_retailers}/{known_retailers} sources responded on this run\n\n"
                "This report covers Pokémon TCG products only. A page being reachable does not guarantee its entire catalogue was exposed to the watcher."
            ),
            "color": 0x5865F2,
            "fields": [
                {"name": "TCG listings parsed", "value": str(scan_tcg_count), "inline": True},
                {"name": "🔵 Live / orderable", "value": str(scan_live), "inline": True},
                {"name": "🟢 Explicitly in stock", "value": str(scan_in_stock), "inline": True},
                {"name": "🟡 Pre-order statuses", "value": str(scan_preorder), "inline": True},
                {"name": "🟠 Incoming / upcoming", "value": str(scan_incoming), "inline": True},
                {"name": "🚚 In transit (explicit)", "value": str(scan_transit), "inline": True},
                {"name": "🔴 Sold out / closed", "value": str(scan_soldout), "inline": True},
                {"name": "Restock signals this scan", "value": str(run_metrics["restock"]), "inline": True},
                {"name": "Restock signals detected since tracking began", "value": str(restock_total), "inline": True},
                {"name": "Physical units / branch totals", "value": "Not available unless retailer publishes them", "inline": False},
            ],
            "footer": {"text": "Counts are public-page signals, not guaranteed stock or inventory quantities."},
        }
        report_embeds: list[dict[str, Any]] = [summary]

        for store in STORES:
            count, available_count, healthy = store_counts.get(store["key"], (0, 0, False))
            items = current_products_by_store.get(store["key"], [])
            upcoming_count = sum(1 for p in items if str(p.get("lifecycle_status", "")).startswith("PRE-ORDER") or p.get("lifecycle_status") == "INCOMING / UPCOMING")
            transit_count = sum(1 for p in items if p.get("lifecycle_status") == "IN TRANSIT")
            in_stock_count = sum(1 for p in items if p.get("lifecycle_status") == "IN STOCK (explicit wording)")
            live_count = sum(1 for p in items if p.get("lifecycle_status") == "LIVE / ORDERABLE")
            eta_count = sum(1 for p in items if p.get("arrival_date"))
            if not healthy:
                status_label, color = "🔴 NOT REACHABLE on this scan", 0xE74C3C
            elif count == 0:
                status_label, color = "🟡 PAGE RESPONDED · NO TCG ITEMS PARSED", 0xF1C40F
            else:
                status_label, color = "🟢 TCG LISTINGS PARSED", 0x2ECC71
            report_embeds.append({
                "title": f"{('🟢' if healthy and count else '🟡' if healthy else '🔴')} {store['name']}",
                "url": store.get("collection_url"),
                "description": f"**Status:** {status_label}\n**Coverage method:** {store.get('coverage', 'Public page checks only')}",
                "color": color,
                "fields": [
                    {"name": "TCG listings parsed", "value": str(count), "inline": True},
                    {"name": "🔵 Live / orderable", "value": str(live_count), "inline": True},
                    {"name": "🟢 Explicitly in stock", "value": str(in_stock_count), "inline": True},
                    {"name": "🟡 Pre-order / upcoming", "value": str(upcoming_count), "inline": True},
                    {"name": "🚚 Explicit in-transit", "value": str(transit_count), "inline": True},
                    {"name": "Listings with a date phrase", "value": str(eta_count), "inline": True},
                    {"name": "Branch-by-branch stock", "value": "Not published by this adapter", "inline": True},
                    {"name": "Physical unit count", "value": "Not exposed by monitored public page", "inline": False},
                ],
                "footer": {"text": "0 parsed listings does not mean the store has zero stock; its page may need a different adapter."},
            })

        for source in OFFICIAL_NEWS_SOURCES:
            healthy = state["official_source_health"].get(source["key"]) is True
            matched = official_news_counts.get(source["key"], 0)
            report_embeds.append({
                "title": "🟣 Official Pokémon TCG Asia news",
                "url": source["url"],
                "description": (
                    f"**Status:** {'🟢 Page fetched' if healthy else '🔴 Not reachable this scan'}\n"
                    f"**Product/release news links matched:** {matched}\n\n"
                    "Official news can help identify upcoming sets, but it does not confirm Philippine retailer stock or branch allocation."
                ),
                "color": 0x8E44AD,
                "fields": [
                    {"name": "Counts as local stock?", "value": "No — announcement/news source only", "inline": True},
                    {"name": "Branch availability", "value": "Not provided by official news page", "inline": True},
                ],
            })

        # Product-level lifecycle board: show the exact listing/set and product format,
        # and don't equate a retailer's orderable flag with confirmed physical on-hand stock.
        lifecycle_rank = {
            "IN TRANSIT": 0,
            "PRE-ORDER OPEN": 1,
            "PRE-ORDER — STATUS UNCLEAR": 2,
            "PRE-ORDER — STATUS CLOSED/UNAVAILABLE": 3,
            "INCOMING / UPCOMING": 4,
            "IN STOCK (explicit wording)": 5,
            "LIVE / ORDERABLE": 6,
            "SOLD OUT / CLOSED": 7,
            "PRE-ORDER CLOSED / SOLD OUT": 8,
            "UNKNOWN / NOT EXPOSED": 9,
        }
        lifecycle_order = sorted(
            scan_products,
            key=lambda p: (
                lifecycle_rank.get(str(p.get("lifecycle_status", "UNKNOWN / NOT EXPOSED")), 99),
                str(p.get("store", "")), str(p.get("title", "")).casefold()
            ),
        )
        lifecycle_lines: list[str] = []
        status_emoji = {
            "IN TRANSIT": "🚚", "PRE-ORDER OPEN": "🟡", "PRE-ORDER — STATUS UNCLEAR": "🟡",
            "PRE-ORDER — STATUS CLOSED/UNAVAILABLE": "🔴", "INCOMING / UPCOMING": "🟠",
            "IN STOCK (explicit wording)": "🟢", "LIVE / ORDERABLE": "🔵",
            "SOLD OUT / CLOSED": "🔴", "PRE-ORDER CLOSED / SOLD OUT": "🔴",
            "UNKNOWN / NOT EXPOSED": "⚪",
        }
        for product in lifecycle_order[:24]:
            status_name = str(product.get("lifecycle_status", "UNKNOWN / NOT EXPOSED"))
            emoji = status_emoji.get(status_name, "⚪")
            title = str(product.get("title", "Pokémon TCG product"))[:110]
            link = str(product.get("url", ""))
            title_link = f"[{title}]({link})" if link else title
            set_name = str(product.get("set_name") or identify_set_name(title))
            product_type = str(product.get("product_type") or identify_product_type(title, str(product.get("signal_text", ""))))
            retailer = str(product.get("store", "Retailer"))
            price = format_price(product.get("price"))
            eta = str(product.get("arrival_date") or "date not published")
            lifecycle_lines.append(
                f"{emoji} **{status_name}** · {retailer}\n"
                f"{title_link}\n"
                f"**Set:** {set_name} · **Type:** {product_type}\n"
                f"**Price:** {price} · **ETA/release:** {eta}"
            )
        overflow = len(lifecycle_order) - min(len(lifecycle_order), 24)
        if overflow > 0:
            lifecycle_lines.append(f"… plus {overflow} more product(s). See retailer pages for full listings.")
        report_embeds.append({
            "title": "🗂️ TCG product & set status board",
            "description": ("\n\n".join(lifecycle_lines) if lifecycle_lines else "No TCG product listings were parsed during this scan." )[:4000],
            "color": 0x5865F2,
            "footer": {"text": "Set names and product formats are best-effort matches from listing titles. LIVE/ORDERABLE does not prove physical on-hand stock."},
        })

        incoming_items = [p for p in scan_products if p.get("incoming_signal") in ("IN_TRANSIT", "UPCOMING_OR_PREORDER") or p.get("preorder")]
        incoming_items.sort(key=lambda p: (0 if p.get("incoming_signal") == "IN_TRANSIT" else 1, str(p.get("store", "")), str(p.get("title", "")).casefold()))
        if incoming_items:
            lines = []
            for product in incoming_items[:12]:
                signal = product.get("incoming_signal")
                if signal == "IN_TRANSIT":
                    label = "🚚 IN TRANSIT (explicit wording)"
                elif product.get("preorder"):
                    label = "📅 PREORDER / UPCOMING"
                else:
                    label = "📦 UPCOMING SIGNAL"
                eta = product.get("arrival_date") or "ETA not published"
                link = product.get("url") or ""
                title = str(product.get("title", "Pokémon TCG product"))[:120]
                store_name = str(product.get("store", "Retailer"))
                link_title = f"[{title}]({link})" if link else title
                set_name = str(product.get("set_name") or identify_set_name(title))
                product_type = str(product.get("product_type") or identify_product_type(title, str(product.get("signal_text", ""))))
                lifecycle, _status_color, _status_note = lifecycle_status(product)
                lines.append(f"**{label}** · {store_name}\n{link_title}\n**Detected status:** {lifecycle}\n**Set:** {set_name} · **Type:** {product_type}\n**ETA/release date:** {eta} · **Branch allocation:** not published")
            remaining = len(incoming_items) - min(len(incoming_items), 12)
            if remaining:
                lines.append(f"…plus {remaining} additional matching listing(s).")
            report_embeds.append({
                "title": "📦 Upcoming stock & shipment clues",
                "description": "\n\n".join(lines)[:4000],
                "color": 0xF39C12,
                "footer": {"text": "An ETA/release date is shown only when parsed from public text. Preorder wording is not proof that a shipment is in transit."},
            })
        else:
            report_embeds.append({
                "title": "📦 Upcoming stock & shipment clues",
                "description": "No public preorder, expected-arrival, or explicit in-transit signals matched the current scan. This does not rule out unpublished incoming inventory.",
                "color": 0x95A5A6,
            })

        report_embeds.append({
            "title": "ℹ️ How to read the numbers",
            "description": (
                "**Total restocks:** the watcher can count detected public-page transitions from unavailable/unknown to orderable, across scans. It cannot count physical units sold/restocked unless the retailer publishes a quantity.\n\n"
                "**Branches:** current adapters do not provide reliable branch-by-branch stock. Branch names and quantities will not be guessed.\n\n"
                "**Arrival dates:** displayed only when a retailer page explicitly provides a date phrase. ‘In transit’ is used only when the page explicitly says shipment/in-transit; it does not generate an invented ETA."
            ),
            "color": 0x34495E,
        })
        send_discord(session, report_embeds, content="🛰️ **Pokémon TCG Drop Radar — readable source report**\nColor key: 🟢 listings parsed · 🟡 page responded but no TCG listings parsed · 🔴 page unavailable · 🟣 official news only")
        state["source_report_sent"] = True
        state["source_report_version"] = 3

    save_state(state)
    LOG.info("Scan complete: %s product(s) visible, %s currently marked orderable; %s product alert(s), %s announcement alert(s).",
             total_products, total_available, len(product_alerts), len(announcement_alerts))
    return 0


if __name__ == "__main__":
    sys.exit(main())
