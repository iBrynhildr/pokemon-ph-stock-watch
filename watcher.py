#!/usr/bin/env python3
"""Cloud-run Pokémon TCG drop radar for public Philippine retailer/news pages.

Designed for GitHub Actions. Selected public retailer pages get a bounded Playwright/Chromium fallback if static parsing finds no TCG product links.
"""
from __future__ import annotations

import atexit
import html
import json
import logging
import os
import re
import sys
import time
import xml.etree.ElementTree as ET
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any
from urllib.parse import quote, urljoin, urlparse

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

# A lightweight browser is used only when a public page returns successfully but
# normal HTML/catalogue parsing finds no matching product links. This is a fallback,
# not a CAPTCHA/login bypass. The workflow installs Chromium and caps each run.
BROWSER_FALLBACK_ENABLED = os.getenv("BROWSER_FALLBACK_ENABLED", "true").strip().casefold() not in ("0", "false", "no", "off")
try:
    BROWSER_FALLBACK_MAX_PAGES = max(0, min(20, int(os.getenv("BROWSER_FALLBACK_MAX_PAGES", "10"))))
except ValueError:
    BROWSER_FALLBACK_MAX_PAGES = 10
try:
    BROWSER_FALLBACK_TIMEOUT_MS = max(3000, min(25000, int(os.getenv("BROWSER_FALLBACK_TIMEOUT_MS", "12000"))))
except ValueError:
    BROWSER_FALLBACK_TIMEOUT_MS = 12000
BROWSER_FALLBACK_TARGET_KEYS = {"toy_kingdom", "toys_r_us", "green_gate_hobbies", "jpstr", "datablitz", "gameline", "arken", "hobby_arena"}
_BROWSER_RUNTIME: Any = None
_BROWSER_INSTANCE: Any = None
_BROWSER_PAGES_USED = 0
_BROWSER_HTML_CACHE: dict[str, str | None] = {}
_BROWSER_IMPORT_WARNING_SHOWN = False

# Explicit user-requested retailer exclusions; do not re-add these to source adapters.
EXCLUDED_RETAILER_DOMAINS = {"gamextreme.ph", "www.gamextreme.ph"}

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
        "suggest_queries": ["pokemon tcg", "30th celebration", "ascended heroes", "latest pokemon tcg set"],
        "feeds": [
            "https://www.toykingdom.com.ph/blogs/event.atom",
            "https://www.toykingdom.com.ph/blogs/news.atom",
        ],
        "html_urls": [
            ("Public Pokémon TCG search", "https://www.toykingdom.com.ph/search?q=pokemon+tcg&type=product"),
            ("Public 30th Celebration search", "https://www.toykingdom.com.ph/search?q=30th+celebration&type=product"),
        ],
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
        "suggest_queries": ["pokemon tcg", "30th celebration", "ascended heroes", "latest pokemon tcg set"],
        "feeds": [
            "https://toysrus.com.ph/blogs/event.atom",
            "https://toysrus.com.ph/blogs/events.atom",
            "https://toysrus.com.ph/blogs/news.atom",
        ],
        "html_urls": [
            ("Public Pokémon TCG product search", "https://toysrus.com.ph/search?q=pokemon+tcg&type=product"),
            ("Public 30th Celebration product search", "https://toysrus.com.ph/search?q=30th+celebration&type=product"),
        ],
        "collection_url": "https://toysrus.com.ph/collections/pokemon",
        "coverage": "Shopify-style public product JSON/search suggestions, collection HTML fallback, public event/news feeds",
    },
    {
        "key": "green_gate_hobbies",
        "name": "Green Gate Hobbies",
        "base": "https://shop.greengatehobbies.com",
        "catalog_urls": [
            ("Pokémon TCG pre-orders", "https://shop.greengatehobbies.com/collections/ptcg-pre-orders/products.json?limit=250&page=1", "https://shop.greengatehobbies.com/collections/ptcg-pre-orders"),
            ("Pokémon TCG sealed products", "https://shop.greengatehobbies.com/collections/ptcg-sealed-products/products.json?limit=250&page=1", "https://shop.greengatehobbies.com/collections/ptcg-sealed-products"),
        ],
        "suggest_url": "https://shop.greengatehobbies.com/search/suggest.json",
        "suggest_queries": ["pokemon tcg", "30th celebration", "ascended heroes", "latest pokemon tcg set"],
        "feeds": [],
        "html_urls": [
            ("Pokémon TCG pre-orders", "https://shop.greengatehobbies.com/collections/ptcg-pre-orders"),
            ("Pokémon TCG sealed products", "https://shop.greengatehobbies.com/collections/ptcg-sealed-products"),
        ],
        "collection_url": "https://shop.greengatehobbies.com/collections/ptcg-sealed-products",
        "coverage": "Public Pokémon TCG pre-order and sealed-product collections, Shopify-style JSON/search suggestions, and public HTML/browser fallback",
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
        "coverage": "Public HTML listing/detail links with bounded JavaScript-rendered fallback; login-only details are not accessed",
    },
    {
        "key": "datablitz",
        "name": "DataBlitz",
        "base": "https://ecommerce.datablitz.com.ph",
        "catalog_urls": [
            ("Pokémon TCG collection", "https://ecommerce.datablitz.com.ph/collections/pokemon-tcg-collection/products.json?limit=250&page=1", "https://ecommerce.datablitz.com.ph/collections/pokemon-tcg-collection"),
            ("Pokémon company collection", "https://ecommerce.datablitz.com.ph/collections/the-pokemon-company-international-inc/products.json?limit=250&page=1", "https://ecommerce.datablitz.com.ph/collections/the-pokemon-company-international-inc"),
            ("Trading card collection", "https://ecommerce.datablitz.com.ph/collections/trading-card/products.json?limit=250&page=1", "https://ecommerce.datablitz.com.ph/collections/trading-card"),
            ("Knock Out Collection products", "https://ecommerce.datablitz.com.ph/collections/pokemon-trading-card-game-knock-out-collection/products.json?limit=250&page=1", "https://ecommerce.datablitz.com.ph/collections/pokemon-trading-card-game-knock-out-collection"),
        ],
        "suggest_url": "https://ecommerce.datablitz.com.ph/search/suggest.json",
        "suggest_queries": ["pokemon tcg", "30th celebration", "ascended heroes"],
        "feeds": [],
        "html_urls": [
            ("Pokémon TCG collection (newest-first public fallback)", "https://ecommerce.datablitz.com.ph/collections/pokemon-tcg-collection?sort_by=created-descending"),
        ],
        "collection_url": "https://ecommerce.datablitz.com.ph/collections/pokemon-tcg-collection",
        "coverage": "Public Pokémon TCG/trading-card collections and search, with a JavaScript-rendered public-page fallback when static parsing finds no product links",
    },
    {
        "key": "gameline",
        "name": "GameLine",
        "base": "https://gameline.ph",
        "catalog_urls": [
            ("Latest catalogue page", "https://gameline.ph/products.json?limit=250&page=1", "https://gameline.ph/search?q=pokemon+tcg&type=product"),
        ],
        "suggest_url": "https://gameline.ph/search/suggest.json",
        "suggest_queries": ["pokemon tcg", "30th celebration", "ascended heroes", "latest pokemon tcg set"],
        "feeds": [],
        "html_urls": [
            ("Public Pokémon TCG product search", "https://gameline.ph/search?q=pokemon+tcg&type=product"),
            ("Public Pokémon product search", "https://gameline.ph/search?q=pokemon&type=product"),
        ],
        "collection_url": "https://gameline.ph/search?q=pokemon+tcg&type=product",
        "coverage": "Public product JSON/search suggestions plus bounded JavaScript-rendered fallback when static public pages return no matching products",
    },
    {
        "key": "arken",
        "name": "ARKEN",
        "base": "https://arken.ph",
        "catalog_urls": [],
        "suggest_url": None,
        "feeds": [],
        "html_urls": [
            ("Public Pokémon TCG category (newest first)", "https://arken.ph/product-category/cardzone/pokemon-trading-card-game-en/?orderby=date"),
        ],
        "detail_urls": [
            ("30th Celebration Knock Out Collection preorder page", "https://arken.ph/shop/cardzone/pokemon-trading-card-game-en/pokemon-tcg-30th-celebration-knock-out-collection/"),
            ("30th Celebration 2-Pack Blister preorder page", "https://arken.ph/shop/cardzone/pokemon-trading-card-game-en/pokemon-tcg-30th-celebration-2-pack-blister/"),
            ("30th Celebration ex Box Assorted preorder page", "https://arken.ph/shop/cardzone/pokemon-trading-card-game-en/pokemon-tcg-30th-celebration-ex-box-assorted/"),
            ("30th Celebration Elite Trainer Box preorder page", "https://arken.ph/shop/cardzone/pokemon-trading-card-game-en/pokemon-tcg-30th-celebration-elite-trainer-box/"),
            ("30th Celebration ex Tin Assorted preorder page", "https://arken.ph/shop/cardzone/pokemon-trading-card-game-en/pokemon-tcg-30th-celebration-ex-tin-4107-assorted/"),
        ],
        "collection_url": "https://arken.ph/product-category/cardzone/pokemon-trading-card-game-en/?orderby=date",
        "coverage": "Public Pokémon TCG category plus directly monitored 30th Celebration sealed product preorder pages; only public product status is read",
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
        "coverage": "Public homepage HTML and structured data, with bounded JavaScript-rendered fallback when static parsing finds no product links",
    },
]

# Public retailer Facebook pages. Only publicly served HTML is read; this does not log in,
# bypass access controls, or use private APIs. Meta may still rate-limit or hide posts.
SOCIAL_PAGE_SOURCES: list[dict[str, str]] = [
    {"key": "gameone_facebook", "name": "Game One PH Facebook", "url": "https://www.facebook.com/gameoneph/"},
    {"key": "gameline_facebook", "name": "GameLine Philippines Facebook", "url": "https://www.facebook.com/gameline.ph/"},
]

OFFICIAL_NEWS_SOURCES: list[dict[str, str]] = [
    {
        "key": "pokemon_asia_news",
        "name": "Official Pokémon TCG Asia English / Philippines news",
        "url": "https://asia.pokemon-card.com/ph/info/",
        "kind": "asia_news",
    },
    {
        "key": "pokemon_global_news_archive",
        "name": "Official Pokémon.com TCG news and product articles",
        "url": "https://www.pokemon.com/us/news",
        "kind": "article_archive",
    },
    {
        "key": "pokemon_global_release_schedule",
        "name": "Official Pokémon TCG global product release schedule",
        "url": "https://press.pokemon.com/en/Items/Schedule/Pokemon-Trading-Card-Game?types=3",
        "kind": "release_schedule",
    },
    {
        "key": "pokemon_global_october_2026_products",
        "name": "Official Pokémon.com October 2026 TCG product releases",
        "url": "https://www.pokemon.com/us/news/check-out-every-pokemon-tcg-product-release-in-october-2026",
        "kind": "product_article",
    },
    {
        "key": "pokemon_global_30th_celebration_product_showcase",
        "name": "Official Pokémon TCG 30th Celebration product showcase",
        "url": "https://www.pokemon.com/us/news/pokemon-tcg-30th-celebration-product-showcase",
        "kind": "product_article",
    },
    {
        "key": "pokemon_global_delta_reign_announcement",
        "name": "Official Pokémon TCG Mega Evolution—Delta Reign announcement",
        "url": "https://pokemon.gamespress.com/MEDIA-ALERT-New-Pokemon-Trading-Card-Game-Mega-EvolutionDelta-Reign-Ex",
        "kind": "product_article",
    },
    {
        "key": "pokemon_global_30th_celebration_announcement",
        "name": "Official Pokémon TCG 30th Celebration announcement",
        "url": "https://pokemon.gamespress.com/MEDIA-ALERT-Pokemon-Unveils-New-Pokemon-Trading-Card-Game-30th-Celebra",
        "kind": "product_article",
    },
]

POKEMON_RE = re.compile(r"pok[eé]mon", re.IGNORECASE)
CARD_TERMS = (
    "trading card game", "trading cards", "trading card", "pokemon tcg", "tcg",
    "booster", "elite trainer", " etb", "blister", "premium collection",
    "ultra-premium", "ultra premium", "battle deck", "trainer toolkit",
    "pokemon card", "pokemon cards", "card game", "build & battle", "build and battle",
    "knock out collection", "knockout collection", "figure collection", "poster collection",
    "tech sticker collection", "binder collection", "card set", "deck crafter's collection",
    "deck crafters collection", "special deck set",
)
# Certain sealed TCG products are sometimes titled without the words Pokémon or TCG.
# These exact product-name markers let the watcher retain those listings while keeping
# general Pokémon toys/figures outside the TCG filter.
KNOWN_TCG_PRODUCT_MARKERS = (
    "30th celebration knock out collection", "30th celebration knockout collection",
    "30th celebration elite trainer box", "30th celebration pokemon center elite trainer box",
    "30th celebration poster collection", "30th celebration tech sticker collection",
    "30th celebration pokemon ex box", "30th celebration ex box", "30th celebration booster bundle",
    "30th celebration battle deck", "30th celebration mini tin", "30th celebration ex tin",
    "30th celebration ditto premium collection", "30th celebration ultra-premium collection",
    "30th celebration ultra premium collection", "30th celebration figure collection",
    "30th celebration binder collection",
    "30th celebration card set", "mega 30th celebration card set",
    "deck crafter's collection", "deck crafters collection",
    "mega evolution delta reign", "delta reign build & battle", "delta reign build and battle",
    "mega greninja ex league battle deck", "first partner illustration collection",
    "focused fighters premium collection", "special deck set with mega feraligatr",
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
    ("30th celebration figure collection mew and mewtwo", "30th Celebration Figure Collection—Mew & Mewtwo"),
    ("30th celebration figure collection", "30th Celebration Figure Collection"),
    ("30th celebration ultra premium collection day and night", "30th Celebration Ultra-Premium Collection—Day & Night"),
    ("30th celebration ultra premium collection", "30th Celebration Ultra-Premium Collection"),
    ("30th celebration elite trainer box", "30th Celebration Elite Trainer Box"),
    ("30th celebration pokemon center elite trainer box", "30th Celebration Pokémon Center Elite Trainer Box"),
    ("30th celebration poster collection", "30th Celebration Poster Collection"),
    ("30th celebration tech sticker collection", "30th Celebration Tech Sticker Collection"),
    ("30th celebration knock out collection", "30th Celebration Knock Out Collection"),
    ("30th celebration knockout collection", "30th Celebration Knock Out Collection"),
    ("30th celebration pokemon ex box", "30th Celebration Pokémon ex Box"),
    ("30th celebration ex box", "30th Celebration Pokémon ex Box"),
    ("30th celebration binder collection", "30th Celebration Binder Collection"),
    ("30th celebration battle deck espeon ex umbreon ex", "30th Celebration Battle Deck—Espeon ex & Umbreon ex"),
    ("30th celebration battle deck", "30th Celebration Battle Deck"),
    ("30th celebration mini tin", "30th Celebration Mini Tin"),
    ("30th celebration booster bundle", "30th Celebration Booster Bundle"),
    ("30th celebration ex tin", "30th Celebration ex Tin"),
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
    ("Knock Out Collection", ("knock out collection", "knockout collection")),
    ("Deck Crafter's Collection", ("deck crafter's collection", "deck crafters collection")),
    ("Ultra-Premium Collection", ("ultra-premium collection", "ultra premium collection", "upc")),
    ("Elite Trainer Box (ETB)", ("elite trainer box", " etb", "etb ")),
    ("Booster Display / Booster Box", ("booster display", "booster box", "booster box display", "display box")),
    ("Booster Bundle", ("booster bundle", "bundle of booster")),
    ("Build & Battle Box", ("build & battle", "build and battle")),
    ("Figure Collection", ("figure collection",)),
    ("Poster Collection", ("poster collection",)),
    ("Tech Sticker Collection", ("tech sticker collection",)),
    ("Binder Collection", ("binder collection",)),
    ("Card Set / Illustration Collection", ("card set", "illustration collection", "first partner collection")),
    ("Ditto Premium Collection", ("ditto premium collection",)),
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
    # Avoid treating ordinary URL-containing text as markup; parse only if it looks like HTML.
    if re.search(r"</?[A-Za-z][^>]*>", text_value):
        text_value = BeautifulSoup(text_value, "html.parser").get_text(" ", strip=True)
    return re.sub(r"\s+", " ", text_value).strip()


def normalize_text(value: str) -> str:
    return re.sub(r"\s+", " ", value.replace("Pokémon", "Pokemon").replace("pokémon", "pokemon")).casefold()


def extract_jpstr_listing_status(text: str, title: str = "") -> str | None:
    """Extract JPSTR's explicit listing badge without letting description text win.

    JPSTR detail pages can repeat the title in several places. The old parser used the
    first title occurrence and sometimes cut off the nearby Open/Closed badge. Prefer
    the listing header before "Basic Info", then inspect title-adjacent card text.
    Description wording such as "preorder closes [date]" is not a closed status.
    """
    value = clean_text(text)
    if not value:
        return None

    # On detail pages the status badge, title, and price are in the header before
    # Basic Info. This avoids stale or historical wording in the long description.
    basic_info = re.search(r"\b(?:Basic Info|Listing Details|Listing Breakdown)\b", value, re.I)
    header = value[:basic_info.start()] if basic_info else value
    candidates = [header]

    # Listing cards may contain several products on one page, so prefer the text
    # immediately around the matching title rather than unrelated neighboring cards.
    if title:
        matches = list(re.finditer(re.escape(clean_text(title)), value, re.I))
        for match in matches:
            start = max(0, match.start() - 300)
            end = min(len(value), match.end() + 260)
            candidate = value[start:end]
            local_basic = re.search(r"\b(?:Basic Info|Listing Details|Listing Breakdown)\b", candidate, re.I)
            if local_basic:
                candidate = candidate[:local_basic.start()]
            candidates.append(candidate)

    # De-duplicate while retaining the most useful header-first order.
    seen: set[str] = set()
    segments = []
    for candidate in candidates:
        candidate = clean_text(candidate)
        if candidate and candidate not in seen:
            seen.add(candidate)
            segments.append(normalize_text(candidate))

    for segment in segments:
        # Positive explicit badges must be checked before closed terms: descriptions
        # can mention old deadlines even while the current listing is open.
        if re.search(r"\bon[- ]hand\s+open\b|\bopen\s+on[- ]hand\b", segment):
            return "ON_HAND_OPEN"
        if re.search(r"\b(?:open\s+pre[- ]?order|pre[- ]?order\s+open|preorder\s+open)\b", segment):
            return "PREORDER_OPEN"
        # JPSTR sometimes renders the badge as brand + standalone Open, followed by
        # the product title (which itself contains Pre-Order), rather than one phrase.
        if re.search(r"\bthe pokemon company\s+open\b", segment) and re.search(r"\bpre[- ]?order\b|\bpreorder\b", segment):
            return "PREORDER_OPEN"
        if re.search(r"\bon[- ]hand\s+closed\b|\bclosed\s+on[- ]hand\b", segment):
            return "ON_HAND_CLOSED"
        if re.search(r"\b(?:pre[- ]?order|preorder)\s+closed\b|\bclosed\s+pre[- ]?order\b", segment):
            return "PREORDER_CLOSED"
        if re.search(r"\bthe pokemon company\s+closed\b", segment) and re.search(r"\bpre[- ]?order\b|\bpreorder\b", segment):
            return "PREORDER_CLOSED"
    return None


# Explicit names of other trading-card games should never qualify for this
# Pokémon-only watcher, even if a generic description/tag happens to contain
# phrases such as "trading card game" or "booster".
NON_POKEMON_TCG_TITLE_RE = re.compile(
    r"\b(?:magic\s*:\s*the\s*gathering|magic\s+cards?|"
    r"mtg\s+(?:cards?|boosters?|booster\s+packs?|trading\s+cards?)|"
    r"yu[\s-]?gi[\s-]?oh!?|one\s+piece\s+card\s+game|"
    r"digimon\s+card\s+game|disney\s+lorcana|weiss\s+schwarz|"
    r"flesh\s+and\s+blood(?:\s+tcg)?|star\s+wars\s+unlimited|"
    r"riftbound|union\s+arena|dragon\s+ball\s+super\s+card\s+game)\b",
    re.IGNORECASE,
)

# The user wants sealed/product TCG items, not standalone storage/accessory products.
# Match the title only: a real ETB/collection may mention sleeves or a deck box in its contents.
EXCLUDED_ACCESSORY_TITLE_RE = re.compile(
    r"\b(?:card\s+)?sleeves?\b|\bsleeve\s+protectors?\b|"
    r"\bdeck[\s-]*box(?:es)?\b|\bdeckbox(?:es)?\b|\bdeck\s+shields?\b",
    re.IGNORECASE,
)
SEALED_PRODUCT_BEFORE_ACCESSORY_RE = re.compile(
    r"\b(?:elite trainer box|\betb\b|booster(?: pack| box| bundle| display)?|"
    r"build and battle|blister(?: pack)?|\b(?:mini )?tin\b|league battle deck|"
    r"rival battle deck|battle deck|knock ?out collection|premium collection|"
    r"ultra premium collection|poster collection|figure collection|binder collection|"
    r"illustration collection|special collection|premium tournament collection|trainer toolkit)\b",
    re.IGNORECASE,
)

ARRIVED_AT_RETAILER_PATTERNS = (
    r"\barrived at (?:our|the) (?:warehouse|store|branch|shop)\b",
    r"\barrived in (?:our|the) (?:warehouse|store|branch|shop)\b",
    r"\b(?:stock|shipment|delivery|products?) (?:has |have )?(?:just )?arrived\b",
    r"\b(?:unloaded|offloaded) (?:at|into) (?:our|the) (?:warehouse|store|branch|shop)\b",
    r"\b(?:unloaded|offloaded)\b.{0,80}\b(?:pokemon|tcg|warehouse|store|branch|stock|delivery|cartons?|boxes?)\b",
    r"\b(?:we have|we've|we just|our team has) (?:just )?(?:unloaded|offloaded|received)\b",
    r"\b(?:new )?stock (?:has |have )?(?:just )?landed\b",
    r"\breceived (?:at |into )?(?:our|the) (?:warehouse|store|branch|shop)\b",
)


def is_excluded_accessory_product(title: str) -> bool:
    """Exclude standalone sleeves/deck boxes, without dropping sealed boxes that include them."""
    normalized = normalize_text(title or "")
    accessory = EXCLUDED_ACCESSORY_TITLE_RE.search(normalized)
    if not accessory:
        return False
    # A sealed TCG product may mention accessories in its title (for example, “ETB
    # with sleeves”). Only count it as an included accessory when a recognized sealed
    # format appears before the accessory wording. The accessory itself remains excluded.
    return SEALED_PRODUCT_BEFORE_ACCESSORY_RE.search(normalized[:accessory.start()]) is None


def is_tcg_product(title: str, description: str = "", tags: Any = None) -> bool:
    title_text = normalize_text(title or "")
    combined = normalize_text(" ".join([title or "", description or "", clean_text(tags)]))

    # Exclude standalone sleeves/deck-box listings even when branded with Pokémon/TCG terms.
    if is_excluded_accessory_product(title):
        return False

    # Fail closed on an explicit non-Pokémon card-game product title. This avoids
    # letting generic catalogue descriptions or tags make Magic cards appear in
    # the Pokémon-only alert feed.
    if NON_POKEMON_TCG_TITLE_RE.search(title_text):
        return False

    if POKEMON_RE.search(combined) and any(term in combined for term in CARD_TERMS):
        return True
    # Retain known Pokémon-specific sealed product names when retailer titles omit
    # "Pokémon" or "TCG"; do not use generic card-game keywords as exceptions.
    return any(marker in title_text for marker in KNOWN_TCG_PRODUCT_MARKERS)


def is_preorder(text: str) -> bool:
    normalized = normalize_text(text)
    return any(term in normalized for term in (
        "pre-order", "pre order", "preorder", "reserve now", "reservation", "down payment"
    ))


# Tournament/event announcements are not inventory or commercial pre-order signals.
# Apply this to retailer feeds and official news so registration posts don't clutter Discord.
TOURNAMENT_EVENT_TERMS = (
    "tournament", "registration", "register now", "league season",
    "championship series", "world championships", "championship", "regional league",
    "master ball league", "premier ball league", "great ball league", "league cup",
    "league challenge", "roadshow", "academia", "entry fee", "registration fee",
    "participation fee", "event fee", "paid registration", "sign up for",
    "sign-up for", "pre-release event", "prerelease event", "event outline",
)


def is_tournament_announcement(title: str, summary: str = "", url: str = "") -> bool:
    """True for organized-play/event notices, not product listings or stock updates."""
    combined = normalize_text(" ".join([title or "", summary or "", url or ""]))
    return any(term in combined for term in TOURNAMENT_EVENT_TERMS)


def incoming_signal(text: str) -> str:
    """Classify only explicit public wording; never infer private logistics."""
    normalized = normalize_text(text)
    if any(re.search(pattern, normalized, re.I) for pattern in IN_TRANSIT_PATTERNS):
        return "IN_TRANSIT"
    if any(re.search(pattern, normalized, re.I) for pattern in ARRIVED_AT_RETAILER_PATTERNS):
        return "ARRIVED_AT_RETAILER"
    if any(re.search(pattern, normalized, re.I) for pattern in UPCOMING_PATTERNS):
        return "UPCOMING_OR_PREORDER"
    return "NONE"


def identify_set_name(title: str, description: str = "") -> str:
    """Identify a specific expansion or named sealed-product release.

    Retailers often put the actual set in the description while the title is abbreviated.
    Search the title first so navigation/related-product text in a description cannot
    override a clear title. Generic eras such as just “Mega Evolution” or “Scarlet & Violet”
    are not treated as specific sets.
    """
    not_found = "Not explicit in listing title/description"
    broad_names = {"Mega Evolution", "Scarlet & Violet"}

    def normalize_for_set(value: str) -> str:
        value = normalize_text(value or "")
        value = value.replace("—", " ").replace("–", " ").replace("-", " ")
        value = re.sub(r"[^a-z0-9& ]+", " ", value)
        return re.sub(r"\s+", " ", value).strip()

    def known_match(value: str) -> str | None:
        normalized = normalize_for_set(value)
        if not normalized:
            return None
        if re.search(r"\b30th\s+(?:anniversary|celebration)\b", normalized):
            if "classic collection" in normalized:
                return "30th Celebration: Classic Collection"
            return "30th Celebration"
        for phrase, canonical in KNOWN_TCG_SETS:
            if canonical in broad_names:
                continue
            needle = normalize_for_set(phrase)
            if needle and re.search(r"(?<![a-z0-9])" + re.escape(needle) + r"(?![a-z0-9])", normalized):
                return canonical
        if re.search(r"\bsv0?8\b", normalized):
            return "Scarlet & Violet—Surging Sparks (SV8 shorthand)"
        if re.search(r"\bsv10\.5b?\b", normalized):
            return "Scarlet & Violet—Black Bolt (SV10.5B shorthand)"
        if re.search(r"\bsv10\.5w\b", normalized):
            return "Scarlet & Violet—White Flare (SV10.5W shorthand)"
        return None

    # Prefer an explicit match from the name itself; the description is second priority.
    for value in (title, description):
        match = known_match(value)
        if match:
            return match

    # Handle newly announced expansions whose exact title has not yet been added to
    # the maintained list, for example “Mega Evolution—<new name> Booster Box”.
    raw_title = normalize_text(title or "")
    raw_title = raw_title.replace("—", " ").replace("–", " ")
    product_format = re.compile(
        r"\b(?:elite trainer box|etb|booster(?: pack| box| bundle| display)?|bundle|pack|box|"
        r"build and battle(?: box| stadium)?|checklane blister|(?:2|3)[ -]?pack blister|"
        r"blister pack|mini tin|ex tin|tin|ultra premium collection|premium collection|"
        r"premium tournament collection|poster collection|figure collection|binder collection|"
        r"knock out collection|league battle deck|rival battle deck|battle deck|trainer toolkit|"
        r"deck|collection box|collection|single card|promo card|playset)\b",
        re.I,
    )
    series_patterns = (
        (r"\bmega evolution\s+(.+)", "Mega Evolution"),
        (r"\bscarlet\s+(?:&|and)\s+violet\s*[: ]+(.+)", "Scarlet & Violet"),
        (r"\bsword\s+(?:&|and)\s+shield\s*[: ]+(.+)", "Sword & Shield"),
        (r"\bsun\s+(?:&|and)\s+moon\s*[: ]+(.+)", "Sun & Moon"),
    )
    for pattern, prefix in series_patterns:
        match = re.search(pattern, raw_title, re.I)
        if not match:
            continue
        candidate = match.group(1)
        fmt = product_format.search(candidate)
        if fmt:
            candidate = candidate[:fmt.start()]
        candidate = re.sub(r"\[[a-z]{1,5}[- ]?\d+(?:\.\d+)?\]", " ", candidate, flags=re.I)
        candidate = re.sub(r"\b(?:pokemon|pokémon|tcg|trading card game|booster|pack|box|collection|english|japanese)\b", " ", candidate, flags=re.I)
        candidate = re.sub(r"\b(?:me|sv|swsh|sw)\s*[-.]?\d+(?:\.\d+)?\b", " ", candidate, flags=re.I)
        candidate = re.sub(r"[^a-z0-9 &'’]+", " ", candidate, flags=re.I)
        candidate = re.sub(r"\s+", " ", candidate).strip(" &-'’")
        if len(candidate) >= 3 and candidate not in {"mega evolution", "scarlet violet", "sword shield", "sun moon"}:
            return f"{prefix}—{candidate.title()} (name inferred from listing)"

    # Named special collections are identifiable releases even without an expansion
    # name. Generic product formats without any named set/release deliberately fail closed.
    normalized_title = normalize_for_set(title)
    normalized_title = re.sub(r"^(?:pokemon trading card game|pokemon tcg|pokemon)\s*", "", normalized_title, flags=re.I)
    named_collection = re.search(
        r"\b([a-z][a-z0-9'& -]{1,35}?)\s+(premium tournament collection|ultra premium collection|premium collection|figure collection|poster collection|binder collection|illustration collection|special collection)\b",
        normalized_title,
        re.I,
    )
    if named_collection:
        name = re.sub(r"\s+", " ", named_collection.group(0)).strip().title()
        return f"{name} (named product release)"

    # A named character/product deck is a specific sealed-product release, even if it
    # is not tied to one expansion set. Do not accept a generic “Pokémon TCG Battle Deck”.
    named_deck = re.search(
        r"\b([a-z][a-z0-9'& -]{1,40}?)\s+(league battle deck|rival battle deck|battle deck|trainer toolkit|special deck set)\b",
        normalized_title,
        re.I,
    )
    if named_deck:
        product_name = re.sub(r"\s+", " ", named_deck.group(0)).strip().title()
        if product_name.split()[0].casefold() not in {"pokemon", "tcg", "trading", "2026", "2025", "2024"}:
            return f"{product_name} (named product release)"

    if re.search(r"\bworld championships decks?\b", normalized_title) and re.search(r"\b20\d{2}\b", normalized_title):
        year = re.search(r"\b20\d{2}\b", normalized_title).group(0)
        return f"Pokémon TCG World Championships Decks {year} (named product release)"

    # Last-resort support for future/foreign-language set names that are not in the
    # maintained alias list yet. Take the meaningful words before a known product format,
    # after removing brand, stock-state, year/code and packaging boilerplate. This is
    # deliberately conservative: “Pokémon TCG Booster Bundle” still has no candidate.
    cleaned = normalized_title
    cleaned = re.sub(r"\b(?:new|sealed|english|japanese|korean|chinese|simplified chinese|traditional chinese|pokemon|pokémon|tcg|trading|card|game|the pokemon company|pre order|preorder|open|closed|sold out|out of stock|in stock|available|on hand)\b", " ", cleaned, flags=re.I)
    cleaned = re.sub(r"\b(?:me|sv|svsh|swsh|sw)\s*[-.]?\s*\d{1,3}(?:\.\d+)?[a-z]?\b", " ", cleaned, flags=re.I)
    cleaned = re.sub(r"\b20\d{2}\s*(?:q[1-4])?\b", " ", cleaned, flags=re.I)
    cleaned = re.sub(r"\[[^]]{1,18}\]", " ", cleaned)
    fmt = product_format.search(cleaned)
    if fmt:
        candidate = cleaned[:fmt.start()]
    else:
        candidate = ""
    candidate = re.sub(r"[^a-z0-9 &'’]+", " ", candidate, flags=re.I)
    candidate = re.sub(r"\s+", " ", candidate).strip(" &-'’")
    candidate_norm = normalize_for_set(candidate)
    if candidate_norm and candidate_norm not in {
        "mega evolution", "scarlet violet", "sword shield", "sun moon",
        "booster", "booster pack", "booster box", "booster bundle", "elite trainer",
        "elite trainer box", "etb", "blister", "deck", "battle", "collection", "tin",
        "pack", "box", "bundle", "set", "new set", "latest set", "pokemon tcg",
        "special", "premium", "ultra premium", "figure", "poster", "binder",
        "illustration", "world championships", "championships", "new product",
    } and len(candidate_norm) >= 3 and re.search(r"[a-z]", candidate_norm):
        # If the series prefix is present, keep it in the label; otherwise preserve the
        # specific product/set name so this label can be reviewed and corrected later.
        return f"{candidate.title()} (name inferred from listing)"

    return not_found


# Only alert on Pokémon TCG sets/products released from 2025 through the current year.
# Longer names are checked first so a 2026 expansion such as Pitch Black is not
# accidentally classified under the broader 2025 Mega Evolution set.
SET_RELEASE_YEAR_ALIASES: tuple[tuple[str, int], ...] = tuple(sorted([
    # 2026 sets and named TCG product releases
    ("first partner illustration collection", 2026),
    ("first partner collection", 2026),
    ("focused fighters", 2026),
    ("mega greninja ex league battle deck", 2026),
    ("mega lucario ex league battle deck", 2026),
    ("deck crafter's collection", 2026),
    ("deck crafters collection", 2026),
    ("30th celebration", 2026),
    ("30th anniversary", 2026),
    ("ascended heroes", 2026),
    ("perfect order", 2026),
    ("chaos rising", 2026),
    ("pitch black", 2026),
    ("delta reign", 2026),
    ("aura seeker", 2026),
    ("mega evolution", 2025),
    # 2025 English-language sets
    ("phantasmal flames", 2025),
    ("destined rivals", 2025),
    ("journey together", 2025),
    ("prismatic evolutions", 2025),
    ("black bolt", 2025),
    ("white flare", 2025),
    # 2025 Japanese-language sets/products that can appear in Philippine stores
    ("battle partners", 2025),
    ("heat wave arena", 2025),
    ("glory of team rocket", 2025),
    ("mega brave", 2025),
    ("mega symphonia", 2025),
    ("inferno x", 2025),
    ("start deck 100 battle collection", 2025),
], key=lambda pair: (pair[1], len(pair[0])), reverse=True))


def tcg_set_release_year(set_name: str, title: str = "") -> int | None:
    """Return a maintained release year for a named TCG set/product, else None.

    This intentionally filters by the set/product's release year, not the retailer
    listing date. A newly listed old set is still old; a 2025+ set can be restocked
    even if the individual product page was created earlier.
    """
    normalized = normalize_text(f"{set_name or ''} {title or ''}")
    normalized = normalized.replace("—", " ").replace("–", " ").replace("-", " ")
    normalized = re.sub(r"[^a-z0-9& ]+", " ", normalized)
    normalized = re.sub(r"\s+", " ", normalized).strip()
    for phrase, year in SET_RELEASE_YEAR_ALIASES:
        needle = normalize_text(phrase).replace("&", "and")
        needle = re.sub(r"[^a-z0-9 ]+", " ", needle)
        needle = re.sub(r"\s+", " ", needle).strip()
        if re.search(r"(?<![a-z0-9])" + re.escape(needle) + r"(?![a-z0-9])", normalized):
            return year
    if re.search(r"\bworld championships? decks?\b", normalized):
        year_match = re.search(r"\b(20\d{2})\b", normalized)
        if year_match:
            return int(year_match.group(1))
    return None


def is_tcg_release_in_window(title: str, description: str = "", release_date: str = "") -> bool:
    """True only when a named TCG set/product has a 2025-through-current release year."""
    set_name = identify_set_name(title, description)
    year = tcg_set_release_year(set_name, title)
    if year is None and release_date:
        # The official schedule supplies an explicit product release date. Do not use
        # a generic article publication date as a substitute for the set release year.
        date_match = re.search(r"\b(20\d{2})\b", str(release_date))
        if date_match and re.search(r"\b(?:pokemon|pokémon|tcg|trading card|booster|collection|deck|tin|blister)\b", normalize_text(title + " " + description)):
            year = int(date_match.group(1))
    return year is not None and 2025 <= year <= date.today().year


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


def purchase_channel_status(product: dict[str, Any]) -> str:
    """Describe physical vs online availability only when the public page supports it."""
    text_value = normalize_text(str(product.get("signal_text", "")))
    in_store_patterns = (
        r"\bin[- ]?store (?:only|stock|availability|purchase|pickup)\b",
        r"\bavailable (?:at|in) (?:our |the )?(?:physical )?(?:stores?|branches|locations)\b",
        r"\bphysical (?:store|branch) (?:stock|availability|available)\b",
        r"\bstore pickup\b", r"\bpick ?up at (?:our |the )?(?:store|branch)\b",
        r"\bon[- ]hand\b", r"\bon hand\b", r"\bwalk[- ]in purchase\b",
    )
    online_patterns = (
        r"\badd to (?:cart|basket)\b", r"\bbuy now\b", r"\border online\b",
        r"\bonline (?:order|purchase|stock|availability|available)\b",
        r"\bships? nationwide\b", r"\bcheckout\b",
    )
    physical = any(re.search(pattern, text_value, re.I) for pattern in in_store_patterns)
    online = bool(product.get("available") is True) or any(re.search(pattern, text_value, re.I) for pattern in online_patterns)
    if physical and online:
        return "ONLINE ORDERABLE + PHYSICAL/IN-STORE SIGNAL"
    if physical:
        return "PHYSICAL / IN-STORE SIGNAL EXPLICITLY STATED"
    if online:
        return "ONLINE PRODUCT-PAGE ORDERABILITY"
    return "PURCHASE CHANNEL NOT CONFIRMED"


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
    jpstr_status = product.get("jpstr_status")

    if incoming == "ARRIVED_AT_RETAILER":
        return "ARRIVED AT RETAILER / WAREHOUSE", 0x9B59B6, "The public text explicitly says stock arrived, was received, or was unloaded at a retailer/warehouse location."
    if incoming == "IN_TRANSIT":
        return "IN TRANSIT", 0x3498DB, "The public text explicitly mentions shipment/in-transit status."
    if jpstr_status == "ON_HAND_OPEN":
        return "IN STOCK / ON HAND — ORDERABLE", 0x2ECC71, "JPSTR explicitly marks the listing On Hand and Open."
    if jpstr_status == "PREORDER_OPEN":
        return "PRE-ORDER OPEN", 0xF1C40F, "JPSTR explicitly marks the preorder Open."
    if jpstr_status == "ON_HAND_CLOSED":
        return "SOLD OUT / ON-HAND LISTING CLOSED", 0xE74C3C, "JPSTR explicitly marks the on-hand listing Closed."
    if jpstr_status == "PREORDER_CLOSED":
        return "PRE-ORDER CLOSED / SOLD OUT", 0xE74C3C, "JPSTR explicitly marks the preorder Closed."
    if jpstr_status == "AVAILABLE":
        return ("PRE-ORDER OPEN", 0xF1C40F, "JPSTR marks the listing Available and preorder wording is present.") if preorder else ("LIVE / ORDERABLE", 0x3498DB, "JPSTR marks the listing Available.")
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


def preorder_status_bucket(product: dict[str, Any] | None) -> str | None:
    """Classify a public preorder status for transition alerts, conservatively."""
    if not product:
        return None
    text_value = normalize_text(str(product.get("signal_text", "")))
    has_preorder_context = bool(product.get("preorder")) or any(
        term in text_value for term in ("pre-order", "pre order", "preorder", "down payment", "reservation")
    )
    jpstr_status = product.get("jpstr_status")
    if jpstr_status == "ON_HAND_OPEN" or jpstr_status == "ON_HAND_CLOSED":
        return None
    if jpstr_status == "PREORDER_OPEN":
        return "OPEN / ORDERABLE"
    if jpstr_status == "PREORDER_CLOSED":
        return "CLOSED / UNAVAILABLE"
    if jpstr_status == "AVAILABLE":
        return "OPEN / ORDERABLE" if product.get("available") is True else "STATUS UNCLEAR"
    if not has_preorder_context:
        return None
    if any(re.search(pattern, text_value, re.I) for pattern in CLOSED_STATUS_PATTERNS) or product.get("available") is False:
        return "CLOSED / UNAVAILABLE"
    if product.get("available") is True:
        return "OPEN / ORDERABLE"
    return "STATUS UNCLEAR"


def extract_date_from_text(text_value: str) -> date | None:
    """Parse a month/day/year release date when explicitly present in public text."""
    value = clean_text(text_value)
    match = re.search(
        r"\b(January|February|March|April|May|June|July|August|September|October|November|December)\s+([0-9]{1,2}),?\s+([0-9]{4})\b",
        value, re.I,
    )
    if not match:
        return None
    try:
        return datetime.strptime(f"{match.group(1)} {match.group(2)} {match.group(3)}", "%B %d %Y").date()
    except ValueError:
        return None


def extract_arrival_date(text: str) -> str | None:
    """Return a date phrase from a listing, without inventing a year or timezone."""
    value = clean_text(text)
    date_phrase = r"([A-Za-z]{3,9}[ ]+\d{4}|[A-Za-z]{3,9}[ ]+\d{1,2}(?!\d)(?:,?[ ]+\d{4})?|\d{4}-\d{2}-\d{2})"
    patterns = (
        r"\bETA[ ]*[:：-]?[ ]*" + date_phrase,
        r"\bavailable(?: on| from)?[ ]*[:：-]?[ ]*" + date_phrase,
        r"\b(?:expected|estimated|est\.?|estd\.?)[ ]+(?:to arrive|arrival|delivery|release)[ ]*(?:on|by|date)?[ ]*[:：-]?[ ]*" + date_phrase,
        r"\b(?:arrival|delivery|release)[ ]+(?:ETA|date)[ ]*[:：-]?[ ]*(?:estimated?\.?[ ]+|est\.?[ ]+)?" + date_phrase,
        r"\b(?:est\.?[ ]+arrival|est\.?[ ]+release|arriving|release)[ ]*[:：-]?[ ]*" + date_phrase,
        r"\b(\d{4}-\d{2}-\d{2})\b",
    )
    for pattern in patterns:
        match = re.search(pattern, value, re.I)
        if match:
            return re.sub(r"\s+", " ", match.group(1)).strip(" ,.")[:80]
    return None


def is_relevant_announcement(title: str, summary: str = "", url: str = "") -> bool:
    if is_excluded_accessory_product(title) or is_tournament_announcement(title, summary, url):
        return False
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
        if _canonical_host(absolute) == _canonical_host(store["base"]):
            return absolute.split("?")[0]
    if handle:
        return f"{store['base']}/products/{handle}"
    return store["collection_url"]


def is_paid_event_listing(title: str, description: str = "", url: str = "") -> bool:
    """Reject tournament/registration listings while preserving actual sealed products."""
    title_norm = normalize_text(title)
    context = normalize_text(" ".join([title or "", description or "", url or ""]))
    product_markers = (
        "booster", "elite trainer box", " etb", "collection", "battle deck", "battle box",
        "mini tin", "tin ", "tin(", "blister", "deck", "trading card game classic",
        "premium tournament collection", "trainer toolkit", "sleeves", "binder",
        "promo card", "promo pack", "single card", "card set",
    )
    # The phrase "Premium Tournament Collection" is a product name, not an event.
    if any(marker in title_norm for marker in product_markers):
        return False
    event_markers = (
        "registration", "register now", "entry fee", "participation fee", "event fee",
        "paid registration", "sign up", "sign-up", "gym battle", "league challenge",
        "league cup", "tournament", "pre-release event", "prerelease event", "event pass",
    )
    return any(marker in context for marker in event_markers)


def normalize_product(store: dict[str, Any], raw: dict[str, Any], source_name: str) -> dict[str, Any] | None:
    store_domain = urlparse(str(store.get("base", ""))).netloc.casefold()
    if store_domain in EXCLUDED_RETAILER_DOMAINS:
        LOG.debug("Ignoring explicitly excluded retailer domain: %s", store_domain)
        return None
    title = clean_text(raw.get("title") or raw.get("name"))
    description = clean_text(raw.get("body_html") or raw.get("descriptionHtml") or raw.get("description") or raw.get("body"))
    tags = raw.get("tags", [])
    if isinstance(tags, str):
        tags = [part.strip() for part in tags.split(",")]
    if not is_tcg_product(title, description, tags):
        return None
    matched_set_name = identify_set_name(title, description)
    if matched_set_name.startswith("Not explicit"):
        LOG.debug("Ignoring Pokémon TCG product without a specific set/release match: %s", title[:160])
        return None
    release_year = tcg_set_release_year(matched_set_name, title)
    if release_year is None or not (2025 <= release_year <= date.today().year):
        LOG.debug("Ignoring Pokémon TCG listing outside the 2025-present release window (year=%s): %s", release_year, title[:160])
        return None
    if is_paid_event_listing(title, description, str(raw.get("url") or raw.get("onlineStoreUrl") or "")):
        LOG.debug("Ignoring Pokémon TCG event/registration listing: %s", title[:160])
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
    # JPSTR exposes an explicit listing badge (e.g. "On Hand Open") separately from
    # the long product description. Treat that badge as authoritative over generic
    # closed/preorder wording in a title or description.
    jpstr_status = raw.get("jpstr_status") if store.get("key") == "jpstr" else None
    if store.get("key") == "jpstr" and not jpstr_status:
        jpstr_status = extract_jpstr_listing_status(full_text, title)
    # Availability must not be inferred from "pre-order" alone: closed listings often
    # retain preorder text and may still show generic cart controls.
    normalized_full = normalize_text(full_text)
    if jpstr_status == "ON_HAND_OPEN":
        available = True
        preorder_flag = False
        if signal == "UPCOMING_OR_PREORDER":
            signal = "NONE"
    elif jpstr_status == "PREORDER_OPEN":
        available = True
        preorder_flag = True
    elif jpstr_status == "ON_HAND_CLOSED":
        available = False
        preorder_flag = False
    elif jpstr_status == "PREORDER_CLOSED":
        available = False
        preorder_flag = True
    else:
        if re.search(r"\b(closed|expired|sold out|out of stock|unavailable|fully allocated|no longer accepting)\b", normalized_full, re.I):
            available = False
        elif re.search(r"\b(open pre[- ]?order|pre[- ]?order now|add to (?:cart|basket)|buy now|in stock|on hand)\b", normalized_full, re.I):
            if available is None:
                available = True
        preorder_flag = is_preorder(full_text)
    product = {
        "store_key": store["key"],
        "store": store["name"],
        "handle": handle,
        "title": title[:250],
        "url": url,
        "available": available,
        "price": price_text,
        "preorder": preorder_flag,
        "incoming_signal": signal,
        "arrival_date": extract_arrival_date(full_text),
        "signal_text": full_text[:1800],
        "source": source_name,
        "jpstr_status": jpstr_status,
    }
    product["set_name"] = matched_set_name
    product["product_type"] = identify_product_type(title, description)
    product["purchase_channel"] = purchase_channel_status(product)
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
    if incoming.get("jpstr_status") in ("ON_HAND_OPEN", "ON_HAND_CLOSED", "PREORDER_OPEN", "PREORDER_CLOSED", "AVAILABLE"):
        merged["preorder"] = bool(incoming.get("preorder"))
    elif incoming.get("preorder") is True:
        merged["preorder"] = True
    elif "preorder" not in merged:
        merged["preorder"] = incoming.get("preorder", False)
    for field in ("incoming_signal", "arrival_date", "signal_text", "set_name", "product_type", "purchase_channel", "lifecycle_status", "lifecycle_color", "lifecycle_note", "jpstr_status"):
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


def _canonical_host(url: str) -> str:
    host = (urlparse(url).hostname or "").casefold()
    return host[4:] if host.startswith("www.") else host


def close_browser_renderer() -> None:
    """Close the lazily-created Playwright runtime, if this run needed it."""
    global _BROWSER_RUNTIME, _BROWSER_INSTANCE
    try:
        if _BROWSER_INSTANCE is not None:
            _BROWSER_INSTANCE.close()
    except Exception:
        pass
    try:
        if _BROWSER_RUNTIME is not None:
            _BROWSER_RUNTIME.stop()
    except Exception:
        pass
    _BROWSER_INSTANCE = None
    _BROWSER_RUNTIME = None


atexit.register(close_browser_renderer)


def render_public_page(url: str, store_base: str) -> str | None:
    """Render a public page with Chromium when static parsing was insufficient.

    This intentionally does not solve CAPTCHAs, log in, click purchase buttons, or
    retry denied pages. It reads the same publicly accessible page a normal browser
    can visit and returns rendered DOM HTML to the existing product parser.
    """
    global _BROWSER_RUNTIME, _BROWSER_INSTANCE, _BROWSER_PAGES_USED, _BROWSER_IMPORT_WARNING_SHOWN
    if not BROWSER_FALLBACK_ENABLED or BROWSER_FALLBACK_MAX_PAGES <= 0:
        return None
    if _canonical_host(url) != _canonical_host(store_base):
        LOG.warning("Skipping browser fallback for a URL outside the retailer domain: %s", url)
        return None
    if url in _BROWSER_HTML_CACHE:
        return _BROWSER_HTML_CACHE[url]
    if _BROWSER_PAGES_USED >= BROWSER_FALLBACK_MAX_PAGES:
        LOG.info("Browser fallback page limit (%s) reached; remaining pages will use static parsing only", BROWSER_FALLBACK_MAX_PAGES)
        _BROWSER_HTML_CACHE[url] = None
        return None

    try:
        from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
        from playwright.sync_api import sync_playwright
    except ImportError:
        if not _BROWSER_IMPORT_WARNING_SHOWN:
            LOG.warning("Playwright is not installed; JavaScript-rendered fallback is unavailable")
            _BROWSER_IMPORT_WARNING_SHOWN = True
        _BROWSER_HTML_CACHE[url] = None
        return None

    try:
        if _BROWSER_RUNTIME is None or _BROWSER_INSTANCE is None:
            _BROWSER_RUNTIME = sync_playwright().start()
            _BROWSER_INSTANCE = _BROWSER_RUNTIME.chromium.launch(
                headless=True,
                args=["--no-sandbox", "--disable-dev-shm-usage"],
            )
        page = _BROWSER_INSTANCE.new_page(
            user_agent=USER_AGENT,
            locale="en-PH",
            viewport={"width": 1365, "height": 1000},
        )
        # Product text and DOM remain available without downloading nonessential media.
        def route_nonessential_media(route: Any) -> None:
            try:
                if route.request.resource_type in {"image", "font", "media"}:
                    route.abort()
                else:
                    route.continue_()
            except Exception:
                try:
                    route.continue_()
                except Exception:
                    pass
        page.route("**/*", route_nonessential_media)
        LOG.info("JavaScript fallback: rendering public page %s", url)
        _BROWSER_PAGES_USED += 1
        response = page.goto(url, wait_until="domcontentloaded", timeout=BROWSER_FALLBACK_TIMEOUT_MS)
        if response is not None and response.status in (401, 403, 429, 503):
            LOG.warning("Browser fallback stopped for %s after HTTP %s; not attempting to bypass access restrictions", url, response.status)
            page.close()
            _BROWSER_HTML_CACHE[url] = None
            return None
        if response is not None and response.status >= 500:
            LOG.warning("Browser fallback stopped for %s after HTTP %s", url, response.status)
            page.close()
            _BROWSER_HTML_CACHE[url] = None
            return None
        # Give client-side hydration a short window. Network-idle is best-effort because
        # some shops keep analytics or inventory connections open indefinitely.
        try:
            page.wait_for_load_state("networkidle", timeout=1800)
        except PlaywrightTimeoutError:
            pass
        page.wait_for_timeout(700)
        try:
            page.evaluate("window.scrollTo(0, Math.min(document.body.scrollHeight || 0, 2400))")
            page.wait_for_timeout(250)
            # JPSTR and some storefronts paginate with a public “Load More” button.
            # Click only that non-transactional label; never click Pre-order, Add to Cart,
            # checkout, or other purchase/registration controls. Limit extra loads per page.
            for _ in range(3):
                load_more = page.get_by_role("button", name=re.compile(r"^\s*load more\s*$", re.I))
                try:
                    if load_more.count() < 1 or not load_more.first.is_visible():
                        break
                    load_more.first.click(timeout=1000)
                    page.wait_for_timeout(450)
                except Exception:
                    break
            page.evaluate("window.scrollTo(0, 0)")
        except Exception:
            pass
        final_url = page.url or url
        if _canonical_host(final_url) != _canonical_host(store_base):
            LOG.warning("Browser page redirected outside the retailer domain; ignoring rendered HTML from %s", url)
            page.close()
            _BROWSER_HTML_CACHE[url] = None
            return None
        rendered = page.content()
        low = clean_text(BeautifulSoup(rendered, "html.parser").get_text(" ", strip=True)).casefold()
        if any(marker in low for marker in (
            "verify you are human", "complete the captcha", "captcha challenge",
            "access denied", "checking your browser", "unusual traffic detected",
        )):
            LOG.warning("Browser fallback saw an access/challenge page at %s; not attempting to bypass it", url)
            page.close()
            _BROWSER_HTML_CACHE[url] = None
            return None
        page.close()
        _BROWSER_HTML_CACHE[url] = rendered
        return rendered
    except Exception as exc:
        LOG.warning("JavaScript browser fallback failed for %s: %s", url, exc.__class__.__name__)
        try:
            close_browser_renderer()
        finally:
            _BROWSER_HTML_CACHE[url] = None
        return None


def should_use_browser_fallback(store: dict[str, Any], page_url: str) -> bool:
    """Use Chromium only on public pages likely to contain relevant sealed TCG listings."""
    if store.get("key") not in BROWSER_FALLBACK_TARGET_KEYS:
        return False
    path = urlparse(page_url).path.casefold()
    key = store.get("key")
    if key in {"toy_kingdom", "toys_r_us"}:
        # General Pokémon / trading-card collections are dominated by figures or other games.
        # Their dedicated text search results are much more useful for a TCG-only watcher.
        return path.startswith("/search")
    if key == "datablitz":
        return any(part in path for part in ("pokemon-tcg", "pokemon-trading-card", "/trading-card"))
    if key == "green_gate_hobbies":
        return "ptcg-" in path or path.startswith("/search")
    if key == "arken":
        return "pokemon-trading-card-game-en" in path
    if key == "gameline":
        return path.startswith("/search")
    if key == "jpstr":
        return path in {"/", "/preorders"} or path.startswith("/listings/")
    if key == "hobby_arena":
        return path in {"", "/"} or "pokemon" in path or "trading-card" in path
    return False


def browser_fallback_products(store: dict[str, Any], page_url: str, source_name: str, detail: bool = False) -> list[dict[str, Any]]:
    """Render and parse one selected public page after the ordinary parser found none."""
    rendered = render_public_page(page_url, store["base"])
    if not rendered:
        return []
    if detail:
        products = parse_html_product_detail_page(store, page_url, f"{source_name} JavaScript-rendered fallback", rendered)
    else:
        products = parse_html_product_cards(store, page_url, f"{source_name} JavaScript-rendered fallback", rendered)
    if products:
        LOG.info("JavaScript fallback found %s TCG listing(s) at %s", len(products), page_url)
    else:
        LOG.info("JavaScript fallback rendered %s but still found no matching public TCG product links", page_url)
    return products


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


def _jsonld_product_objects(value: Any) -> list[dict[str, Any]]:
    """Recursively find Product nodes in JSON-LD graphs and ItemLists."""
    found: list[dict[str, Any]] = []
    if isinstance(value, dict):
        kind = value.get("@type", [])
        kinds = [kind] if isinstance(kind, str) else kind if isinstance(kind, list) else []
        if any(str(item).casefold() in ("product", "productmodel", "productgroup") for item in kinds):
            found.append(value)
        for key, child in value.items():
            if key in ("@context",):
                continue
            found.extend(_jsonld_product_objects(child))
    elif isinstance(value, list):
        for child in value:
            found.extend(_jsonld_product_objects(child))
    return found


def parse_jsonld_product_records(store: dict[str, Any], page_url: str, source_name: str, content: str) -> list[dict[str, Any]]:
    """Extract public Product structured-data records when product cards omit readable links."""
    soup = BeautifulSoup(content, "html.parser")
    records: dict[str, dict[str, Any]] = {}
    base_domain = _canonical_host(store["base"])
    for script in soup.select('script[type="application/ld+json"]'):
        raw_json = script.string or script.get_text(" ", strip=True)
        if not raw_json:
            continue
        try:
            data = json.loads(raw_json)
        except (ValueError, TypeError):
            continue
        for node in _jsonld_product_objects(data):
            title = clean_text(node.get("name") or node.get("alternateName"))
            if not title:
                continue
            raw_url = node.get("url") or node.get("@id") or ""
            if isinstance(raw_url, dict):
                raw_url = raw_url.get("@id", "")
            absolute_url = urljoin(page_url, str(raw_url)) if raw_url else ""
            if not absolute_url or _canonical_host(absolute_url) != base_domain:
                continue
            if not re.search(r"/(?:products?|listings?)/[^/?#]+", urlparse(absolute_url).path, re.I):
                continue
            offers = node.get("offers", {})
            if isinstance(offers, list):
                offers = offers[0] if offers else {}
            if not isinstance(offers, dict):
                offers = {}
            availability_text = clean_text(offers.get("availability") or node.get("availability"))
            available: bool | None = None
            if re.search(r"instock|limitedavailability|preorder", availability_text, re.I):
                available = True
            elif re.search(r"outofstock|soldout|discontinued", availability_text, re.I):
                available = False
            price = offers.get("price") or offers.get("lowPrice") or node.get("price")
            desc = clean_text(node.get("description"))
            if availability_text:
                desc = f"{desc} Public structured availability: {availability_text}".strip()
            raw = {
                "title": title,
                "description": desc,
                "handle": urlparse(absolute_url).path.rstrip("/").split("/")[-1],
                "url": absolute_url,
                "price": price,
                "available": available,
                "sku": node.get("sku"),
            }
            item = normalize_product(store, raw, source_name)
            if item:
                records[item["handle"]] = merge_product(records.get(item["handle"]), item)
    return list(records.values())


def _card_title(anchor: Any, card: Any, href: str) -> str:
    title = clean_text(anchor.get_text(" ", strip=True))
    if title:
        return title
    for selector in (
        "h2", "h3", "h4", ".product-card__title", ".product-title", ".product__title",
        ".product-name", ".card__heading", "[itemprop='name']", "[data-product-title]",
    ):
        node = card.select_one(selector) if hasattr(card, "select_one") else None
        if node:
            title = clean_text(node.get("content") or node.get("data-product-title") or node.get_text(" ", strip=True))
            if title:
                return title
    img = anchor.find("img") or (card.find("img") if hasattr(card, "find") else None)
    title = clean_text(img.get("alt") if img else "")
    if title:
        return title
    match = re.search(r"/(?:products?|listings?)/([^/?#]+)", href)
    return match.group(1).replace("-", " ") if match else ""


def parse_html_product_cards(store: dict[str, Any], page_url: str, source_name: str, content: str) -> list[dict[str, Any]]:
    soup = BeautifulSoup(content, "html.parser")
    records: dict[str, dict[str, Any]] = {}
    # Structured Product JSON-LD often survives even when the visible storefront uses client rendering.
    for item in parse_jsonld_product_records(store, page_url, source_name, content):
        records[item["handle"]] = merge_product(records.get(item["handle"]), item)

    for anchor in soup.select('a[href]'):
        raw_href = anchor.get("href", "")
        standard_product_path = re.search(r"/(?:products?|listings?)/[^/?#]+", raw_href, re.I)
        arken_tcg_path = (
            store.get("key") == "arken"
            and re.search(r"/shop/cardzone/pokemon-trading-card-game-en/[^/?#]+", raw_href, re.I)
        )
        if not standard_product_path and not arken_tcg_path:
            continue
        href = urljoin(page_url, raw_href)
        if _canonical_host(href) != _canonical_host(store["base"]):
            continue
        handle = urlparse(href).path.rstrip("/").split("/")[-1]
        if not handle:
            continue
        parent = anchor
        for _ in range(7):
            if parent.parent is None:
                break
            parent = parent.parent
            classes = " ".join(str(c) for c in parent.get("class", []))
            if parent.name in ("article", "li") or any(word in classes.casefold() for word in ("product", "card", "listing", "grid-item")):
                break
        title = _card_title(anchor, parent, href)
        card_text = clean_text(parent.get_text(" ", strip=True))
        available: bool | None = None
        if re.search(r"\b(closed|expired|sold out|out of stock|unavailable|fully allocated|no longer accepting)\b", card_text, re.I):
            available = False
        elif re.search(r"\b(open pre[- ]?order|pre[- ]?order now|add to (?:cart|basket)|buy now|in stock|on hand)\b", card_text, re.I):
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
        if store.get("key") == "jpstr":
            raw["jpstr_status"] = extract_jpstr_listing_status(card_text, title)
        item = normalize_product(store, raw, source_name)
        if item:
            records[item["handle"]] = merge_product(records.get(item["handle"]), item)
    return list(records.values())

def parse_html_product_detail_page(store: dict[str, Any], page_url: str, source_name: str, content: str) -> list[dict[str, Any]]:
    """Parse one known public product detail page where category cards are not enough."""
    soup = BeautifulSoup(content, "html.parser")
    title_node = soup.find("h1") or soup.find("meta", property="og:title")
    if title_node is None:
        return []
    title = clean_text(title_node.get("content") if title_node.name == "meta" else title_node.get_text(" ", strip=True))
    if not title:
        return []

    description_parts: list[str] = []
    for selector in (
        ".woocommerce-product-details__short-description",
        ".woocommerce-Tabs-panel--description",
        "#tab-description",
        ".summary",
        "[itemprop='description']",
        "meta[name='description']",
    ):
        for node in soup.select(selector):
            text_value = clean_text(node.get("content") if node.name == "meta" else node.get_text(" ", strip=True))
            if text_value and text_value not in description_parts:
                description_parts.append(text_value)
    description = " ".join(description_parts)
    page_text = clean_text(soup.get_text(" ", strip=True))
    if not description:
        # Restrict fallback text to the product panel where possible, to avoid navigation noise.
        main_node = soup.select_one(".product, .product-template, main, #primary") or soup.body
        description = clean_text(main_node.get_text(" ", strip=True)) if main_node else page_text

    price_value = None
    for selector in (
        "meta[property='product:price:amount']", "meta[itemprop='price']", "[itemprop='price']",
        ".summary .price", ".product .price", ".woocommerce-Price-amount",
    ):
        node = soup.select_one(selector)
        if node:
            price_value = node.get("content") or node.get_text(" ", strip=True)
            if price_value:
                break
    signal_text = f"{title} {description}"
    jpstr_status = extract_jpstr_listing_status(page_text, title) if store.get("key") == "jpstr" else None
    available: bool | None = None
    if jpstr_status in ("ON_HAND_OPEN", "PREORDER_OPEN", "AVAILABLE"):
        available = True
    elif jpstr_status in ("ON_HAND_CLOSED", "PREORDER_CLOSED"):
        available = False
    elif re.search(r"\b(closed|expired|sold out|out of stock|unavailable|fully allocated|no longer accepting)\b", signal_text, re.I):
        available = False
    elif re.search(r"\b(open pre[- ]?order|pre[- ]?order now|add to (?:cart|basket)|buy now|in stock|on hand)\b", signal_text, re.I):
        available = True

    path_parts = [part for part in urlparse(page_url).path.rstrip("/").split("/") if part]
    handle = path_parts[-1] if path_parts else page_url
    raw = {
        "title": title,
        "handle": handle,
        "url": page_url,
        "description": description[:5000],
        "available": available,
        "price": price_value,
        "jpstr_status": jpstr_status,
    }
    item = normalize_product(store, raw, source_name)
    return [item] if item else []


def _catalog_page_url(url: str, page: int) -> str:
    """Return an API URL with a page number while preserving its other parameters."""
    if re.search(r"([?&]page=)\d+", url):
        return re.sub(r"([?&]page=)\d+", lambda m: m.group(1) + str(page), url, count=1)
    return url + ("&" if "?" in url else "?") + f"page={page}"


def scrape_catalog_url(session: requests.Session, store: dict[str, Any], endpoint: tuple[str, str, str]) -> tuple[list[dict[str, Any]], bool]:
    source_name, api_url, fallback_url = endpoint
    all_products: dict[str, dict[str, Any]] = {}
    any_json_ok = False
    json_schema_seen = False
    max_pages = 3

    # Most stores expose Shopify-style product JSON. Keep pagination bounded and only
    # continue when a full first page implies there may be more results to inspect.
    first_page_count: int | None = None
    for page_number in range(1, max_pages + 1):
        current_url = _catalog_page_url(api_url, page_number)
        response = get_response(session, current_url)
        if response is None:
            break
        try:
            data = response.json()
        except (ValueError, TypeError):
            LOG.info("Catalogue endpoint did not return JSON; trying public storefront fallback: %s", current_url)
            break
        raw_products = products_from_json_response(data)
        if raw_products is None:
            LOG.warning("Catalogue endpoint returned JSON, but its product-list schema was not recognized: %s", current_url)
            break
        json_schema_seen = True
        any_json_ok = True
        if page_number == 1:
            first_page_count = len(raw_products)
        elif not raw_products:
            break
        for raw in raw_products:
            item = normalize_product(store, raw, source_name)
            if item:
                all_products[item["handle"]] = merge_product(all_products.get(item["handle"]), item)
        if len(raw_products) < 200:
            break

    if all_products:
        if first_page_count is not None and first_page_count >= 200:
            LOG.info("Catalogue %s supplied a full first page; bounded pagination inspected up to %s page(s)", source_name, max_pages)
        return list(all_products.values()), True
    if any_json_ok:
        LOG.info("Catalogue %s returned valid product JSON but no matching set-specific Pokémon TCG products", source_name)
    elif not json_schema_seen:
        LOG.debug("No usable JSON product records from %s; trying public HTML fallback", api_url)

    # On product-list endpoints, HTML can expose cards that the JSON feed omits or blocks.
    fallback = get_response(session, fallback_url)
    if fallback is None:
        return [], any_json_ok
    products = parse_html_product_cards(store, fallback_url, f"{source_name} HTML fallback", fallback.text)
    if not products and should_use_browser_fallback(store, fallback_url):
        products = browser_fallback_products(store, fallback_url, f"{source_name} HTML fallback")
    if not products:
        LOG.info("Public fallback page was reachable but had no matching set-specific Pokémon TCG links: %s", fallback_url)
    return products, True


def scrape_search_suggestions(session: requests.Session, store: dict[str, Any]) -> tuple[list[dict[str, Any]], bool]:
    queries = store.get("suggest_queries") or ["pokemon tcg", "pokemon trading card game", "30th celebration"]
    normalized: dict[str, dict[str, Any]] = {}
    had_success = False
    for query in queries:
        params = {
            "q": str(query),
            "resources[type]": "product",
            "resources[limit]": "25",
            # Sold-out products must remain visible so the watcher can identify restocks.
            "resources[options][unavailable_products]": "show",
        }
        response = get_response(session, store["suggest_url"], params=params)
        if response is None:
            continue
        try:
            data = response.json()
        except (ValueError, TypeError):
            LOG.info("Search suggestions for %s did not return JSON", query)
            continue
        raw_products = products_from_json_response(data)
        if raw_products is None:
            LOG.debug("Search suggestions for %s returned no recognized product records", query)
            continue
        had_success = True
        matched_count = 0
        for raw in raw_products:
            item = normalize_product(store, raw, f"Store search suggestions ({query})")
            if item:
                matched_count += 1
                normalized[item["handle"]] = merge_product(normalized.get(item["handle"]), item)
        LOG.info("Search suggestions '%s' at %s returned %s raw product(s), %s set-specific TCG match(es)", query, store["name"], len(raw_products), matched_count)

    if normalized:
        return list(normalized.values()), True

    # Last-resort public search HTML. Also try this when suggestions returned valid JSON
    # but only irrelevant items: this catches products indexed differently in storefront search.
    html_success = False
    for query_index, query in enumerate(queries[:4]):
        search_url = f"{store['base']}/search?q={quote(str(query))}&type=product"
        html_response = get_response(session, search_url)
        if html_response is None:
            continue
        html_success = True
        products = parse_html_product_cards(store, search_url, f"Store search HTML fallback ({query})", html_response.text)
        if not products and query_index == 0 and store.get("key") in BROWSER_FALLBACK_TARGET_KEYS:
            products = browser_fallback_products(store, search_url, f"Store search HTML fallback ({query})")
        if products:
            for product in products:
                normalized[product["handle"]] = merge_product(normalized.get(product["handle"]), product)
    if normalized:
        return list(normalized.values()), True
    if had_success or html_success:
        LOG.info("Search completed for %s, but no set-specific Pokémon TCG listings matched", store["name"])
        return [], True
    return [], False


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
    # Shopify-style JSON catalogue. If static HTML yields no cards, try a bounded
    # Playwright/Chromium pass for selected retailers where client-side rendering is common.
    for source_name, page_url in store.get("html_urls", []):
        response = get_response(session, page_url)
        if response is None:
            source_successes.append(False)
            continue
        page_products = parse_html_product_cards(store, page_url, source_name, response.text)
        if not page_products and should_use_browser_fallback(store, page_url):
            page_products = browser_fallback_products(store, page_url, source_name)
        source_successes.append(True)
        for product in page_products:
            found[product["handle"]] = merge_product(found.get(product["handle"]), product)
        if not page_products:
            LOG.info("Reached %s but found no eligible set-specific Pokémon TCG product links; the page may be empty, irrelevant, or rendered differently", page_url)
    # Direct product details are useful for high-priority releases/preorders on stores
    # whose category/product routing isn't compatible with the standard catalog parser.
    for source_name, page_url in store.get("detail_urls", []):
        response = get_response(session, page_url)
        source_successes.append(response is not None)
        if response is None:
            continue
        detail_products = parse_html_product_detail_page(store, page_url, source_name, response.text)
        if not detail_products and should_use_browser_fallback(store, page_url):
            detail_products = browser_fallback_products(store, page_url, source_name, detail=True)
        if not detail_products:
            LOG.warning("Fetched %s but could not parse a matching public TCG product detail after browser fallback", page_url)
        for product in detail_products:
            found[product["handle"]] = merge_product(found.get(product["handle"]), product)
    # A source is healthy if at least one of its configured public paths could be read.
    healthy = any(source_successes)
    LOG.info("Retailer summary — %s: %s set-specific listing(s), %s orderable; public source reachable=%s",
             store["name"], len(found), sum(1 for item in found.values() if item.get("available") is True), healthy)
    return found, healthy


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


def parse_public_facebook_posts(page_html: str, page_url: str, source: dict[str, str]) -> list[dict[str, str]]:
    """Extract relevant post links/text from public Facebook HTML when Facebook exposes it."""
    soup = BeautifulSoup(page_html or "", "html.parser")
    page_text = normalize_text(soup.get_text(" ", strip=True))
    post_anchors: dict[str, dict[str, str]] = {}
    post_path = re.compile(r"/(?:[^/]+/posts/\d+|permalink\.php|photo(?:\.php)?|videos/\d+|reel/\d+)", re.I)

    for anchor in soup.find_all("a", href=True):
        href = html.unescape(str(anchor.get("href", "")).strip())
        absolute = urljoin("https://www.facebook.com/", href)
        parsed = urlparse(absolute)
        if _canonical_host(absolute) != "facebook.com" or not post_path.search(parsed.path):
            continue
        if re.search(r"/posts/\d+", parsed.path, re.I):
            stable_url = f"https://www.facebook.com{parsed.path}"
        else:
            stable_url = absolute
        node = anchor
        context = ""
        for _ in range(6):
            if node is None:
                break
            candidate = clean_text(node.get_text(" ", strip=True))
            if len(candidate) > len(context):
                context = candidate
            if len(candidate) >= 80:
                break
            node = getattr(node, "parent", None)
        anchor_text = clean_text(anchor.get_text(" ", strip=True))
        if len(context) < len(anchor_text):
            context = anchor_text
        generic_link_label = bool(re.fullmatch(
            r"(?:view|see|read|watch|open)?\\s*(?:the )?(?:post|more|video|photo|on facebook)?\\s*",
            anchor_text, re.I,
        )) or anchor_text.casefold() in {"facebook", "view post", "see more", "read more", "watch video"}
        title = (context[:250] if generic_link_label else anchor_text) or context[:180]
        summary = context[:1000]
        if not summary or not is_relevant_announcement(title, summary, stable_url):
            continue
        if not is_tcg_release_in_window(title, summary):
            continue
        post_anchors[stable_url] = {
            "store_key": source["key"], "store": source["name"],
            "title": title[:250] or "Pokémon TCG retailer announcement",
            "url": stable_url, "published": "", "summary": summary, "feed": page_url,
        }

    if not post_anchors and any(term in page_text for term in (
        "log in to facebook", "log into facebook", "you must log in", "temporarily blocked",
        "content isn't available right now", "this content isn't available",
    )):
        LOG.warning("Facebook page %s returned a login/block shell; public posts could not be parsed this run", page_url)
    return list(post_anchors.values())


def scrape_public_facebook_page(session: requests.Session, source: dict[str, str]) -> tuple[list[dict[str, str]], bool]:
    response = get_response(session, source["url"])
    if response is None:
        return [], False
    items = parse_public_facebook_posts(response.text, source["url"], source)
    soup = BeautifulSoup(response.text or "", "html.parser")
    page_text = normalize_text(soup.get_text(" ", strip=True))
    login_shell = any(term in page_text for term in (
        "log in to facebook", "log into facebook", "you must log in", "temporarily blocked",
        "content isn't available right now", "this content isn't available",
    ))
    has_post_links = bool(soup.find("a", href=re.compile(r"/(?:[^/]+/posts/|permalink\.php)", re.I)))
    usable = bool(items) or (not login_shell and has_post_links)
    LOG.info("Facebook source summary — %s: %s relevant post(s) parsed; public HTML usable=%s",
             source["name"], len(items), usable)
    return items, usable


def parse_official_release_schedule(content: str, page_url: str, source: dict[str, str], today: date | None = None) -> list[dict[str, str]]:
    """Read explicitly dated Pokémon TCG entries from the official press release schedule.

    Only a practical release window is returned: seven days in the past through 180 days
    ahead. The resulting notices are global dates and never imply Philippine preorder stock.
    """
    today = today or datetime.now(timezone.utc).date()
    soup = BeautifulSoup(content, "html.parser")
    found: dict[str, dict[str, str]] = {}
    for row in soup.select("tr"):
        cells = row.find_all(["td", "th"], recursive=False) or row.find_all("td")
        if len(cells) < 2:
            continue
        anchors = row.select("a[href]")
        if not anchors:
            continue
        title = clean_text(anchors[0].get_text(" ", strip=True))
        row_text = clean_text(row.get_text(" ", strip=True))
        if not title or not is_tcg_product(title) or is_tournament_announcement(title):
            continue
        release_date = extract_date_from_text(row_text)
        if release_date is None or release_date < today - timedelta(days=7) or release_date > today + timedelta(days=180):
            continue
        link = urljoin(page_url, anchors[0].get("href", ""))
        if not link.startswith("https://press.pokemon.com/"):
            link = page_url
        key_url = link
        item = {
            "store_key": source["key"],
            "store": source["name"],
            "title": title[:250],
            "url": key_url,
            "published": "",
            "release_date": release_date.strftime("%B %-d, %Y"),
            "summary": f"Official global schedule release date: {release_date.strftime('%B %-d, %Y')}. This is not a confirmed Philippine retailer launch or preorder status.",
            "feed": page_url,
            "official_product_news": "true",
            "official_source_type": "global_release_schedule",
            "alert_on_first_scan": "true" if release_date >= today - timedelta(days=7) else "false",
        }
        found[key_url] = item
    return list(found.values())


def _article_date_near_anchor(anchor: Any) -> date | None:
    node = anchor
    for _ in range(5):
        if node is None:
            break
        text_value = clean_text(node.get_text(" ", strip=True)) if hasattr(node, "get_text") else ""
        parsed = extract_date_from_text(text_value)
        if parsed and len(text_value) <= 500:
            return parsed
        node = getattr(node, "parent", None)
    return None


def discover_official_release_articles(content: str, page_url: str, source: dict[str, str], today: date | None = None) -> list[dict[str, str]]:
    """Find recent official TCG product-release articles without following unrelated news."""
    today = today or datetime.now(timezone.utc).date()
    soup = BeautifulSoup(content, "html.parser")
    patterns = (
        "product release", "product showcase", "product lineup", "product line-up",
        "new pokemon trading card game", "new pokemon tcg", "expansion brings", "expansion battle is coming",
    )
    found: dict[str, dict[str, str]] = {}
    for anchor in soup.select("a[href]"):
        title = clean_text(anchor.get_text(" ", strip=True))
        href = urljoin(page_url, anchor.get("href", ""))
        normalized_title = normalize_text(title)
        if not title or "pokemon.com" not in urlparse(href).netloc.casefold():
            continue
        if is_excluded_accessory_product(title):
            continue
        if not any(p in normalized_title for p in patterns):
            continue
        if "pocket" in normalized_title or is_tournament_announcement(title):
            continue
        if not re.search(r"/(?:us/)?(?:news|articles)/", urlparse(href).path, re.I):
            continue
        published_date = _article_date_near_anchor(anchor)
        if published_date and published_date < today - timedelta(days=180):
            continue
        found[href] = {
            "title": title[:250], "url": href,
            "published": published_date.strftime("%B %-d, %Y") if published_date else "",
        }
    # Keep the most recent/relevant six article URLs. The article body is cached in state
    # after its first fetch, so the five-minute stock loop doesn't repeatedly refetch static pages.
    return list(found.values())[:6]


def parse_official_release_article(content: str, article_url: str, source: dict[str, str], today: date | None = None) -> list[dict[str, str]]:
    """Extract product headings and explicitly published dates from official TCG release articles."""
    today = today or datetime.now(timezone.utc).date()
    soup = BeautifulSoup(content, "html.parser")
    root = soup.find("article") or soup.select_one("main") or soup.body or soup
    headings = root.select("h2, h3, h4")
    if not headings:
        return []
    result: dict[str, dict[str, str]] = {}
    article_title = clean_text((soup.find("h1") or {}).get_text(" ", strip=True) if soup.find("h1") else "")
    for heading in headings:
        title = clean_text(heading.get_text(" ", strip=True))
        if not title or normalize_text(title) == normalize_text(article_title):
            continue
        # The product heading itself should establish the TCG connection; nearby text can contain
        # unrelated tournament wording and should not turn an event registration into a product.
        if is_excluded_accessory_product(title) or not is_tcg_product(title) or is_tournament_announcement(title):
            continue
        summary_parts: list[str] = []
        for following in heading.find_all_next():
            if following is not heading and following.name in ("h1", "h2", "h3", "h4"):
                break
            if following.name in ("p", "li"):
                text_value = clean_text(following.get_text(" ", strip=True))
                if text_value and text_value not in summary_parts:
                    summary_parts.append(text_value)
            if sum(len(part) for part in summary_parts) > 1200:
                break
        summary = " ".join(summary_parts)[:1200]
        release_date = extract_date_from_text(summary) or extract_date_from_text(clean_text(heading.parent.get_text(" ", strip=True)))
        if release_date and (release_date < today - timedelta(days=14) or release_date > today + timedelta(days=180)):
            continue

        heading_anchor = heading.find("a", href=True)
        target_url = urljoin(article_url, heading_anchor.get("href", "")) if heading_anchor else article_url + "#" + re.sub(r"[^a-z0-9]+", "-", normalize_text(title)).strip("-")
        full_summary = summary
        if release_date:
            date_text = release_date.strftime("%B %-d, %Y")
            full_summary = (f"Official product article lists availability: {date_text}. " + summary).strip()
        if not full_summary:
            full_summary = "Official Pokémon TCG product information. Philippine preorder and retailer availability are not confirmed by this announcement."
        item = {
            "store_key": source["key"],
            "store": source["name"],
            "title": title[:250],
            "url": target_url,
            "published": "",
            "release_date": release_date.strftime("%B %-d, %Y") if release_date else "",
            "summary": full_summary[:1200],
            "feed": article_url,
            "official_product_news": "true",
            "official_source_type": "global_release_article",
            "alert_on_first_scan": "true" if release_date and release_date >= today else "false",
        }
        result[target_url] = item
    return list(result.values())


def scrape_official_source(session: requests.Session, source: dict[str, str], state: dict[str, Any] | None = None) -> tuple[list[dict[str, str]], bool]:
    response = get_response(session, source["url"])
    if response is None:
        return [], False
    kind = source.get("kind", "asia_news")
    if kind == "release_schedule":
        return parse_official_release_schedule(response.text, source["url"], source), True
    if kind == "product_article":
        items = parse_official_release_article(response.text, source["url"], source)
        # Keep a source-level notice when the official article is valid but its product
        # layout differs from the heading parser. This still links to the official article
        # and explicitly avoids claiming Philippine preorder availability.
        if not items:
            soup = BeautifulSoup(response.text, "html.parser")
            heading = soup.find("h1") or soup.find("title")
            title = clean_text(heading.get_text(" ", strip=True)) if heading else source["name"]
            if title and not is_excluded_accessory_product(title) and (is_tcg_product(title) or "pokemon" in title.casefold()):
                items = [{
                    "store_key": source["key"], "store": source["name"],
                    "title": title[:250], "url": source["url"], "published": "",
                    "summary": "Official Pokémon TCG product announcement. Open the official source for product details and dates; this does not confirm Philippine retailer availability.",
                    "feed": source["url"], "official_product_news": "true",
                    "official_source_type": "global_release_article", "alert_on_first_scan": "false",
                }]
        for item in items:
            item["official_source_type"] = "global_release_article"
            item.setdefault("alert_on_first_scan", "false")
        return items, True
    if kind == "article_archive":
        candidates = discover_official_release_articles(response.text, source["url"], source)
        items: dict[str, dict[str, str]] = {}
        cache: dict[str, list[dict[str, str]]] = {}
        if state is not None:
            existing_cache = state.setdefault("official_release_article_cache", {})
            if isinstance(existing_cache, dict):
                cache = existing_cache
        for candidate in candidates:
            cached = cache.get(candidate["url"])
            if isinstance(cached, list):
                for item in cached:
                    if isinstance(item, dict) and item.get("url"):
                        items[str(item["url"])] = item
                continue
            article_response = get_response(session, candidate["url"])
            if article_response is None:
                continue
            article_items = parse_official_release_article(article_response.text, candidate["url"], source)
            for item in article_items:
                if not item.get("published"):
                    item["published"] = candidate.get("published", "")
            # Preserve an official release article even when the page has no product-level headings.
            if not article_items:
                normalized_title = normalize_text(candidate["title"])
                if ("product release" in normalized_title or "product showcase" in normalized_title) and "pocket" not in normalized_title:
                    item = {
                        "store_key": source["key"], "store": source["name"],
                        "title": candidate["title"], "url": candidate["url"],
                        "published": candidate.get("published", ""),
                        "summary": "Official Pokémon TCG release article. Open the original source for product and date details; this does not confirm Philippine retailer availability.",
                        "feed": candidate["url"], "official_product_news": "true",
                        "official_source_type": "global_release_article", "alert_on_first_scan": "false",
                    }
                    article_items = [item]
            cache[candidate["url"]] = article_items
            for item in article_items:
                items[item["url"]] = item
        return list(items.values()), True
    return parse_official_product_news(response.text, source["url"], source), True


def parse_official_product_news(content: str, page_url: str, source: dict[str, str]) -> list[dict[str, str]]:
    """Parse only title-level official product/release items, excluding organized-play events.

    The official news hub mixes Product, Campaign, Event, and Other items. The previous
    implementation matched against a large ancestor container; on this page that container
    could include many unrelated headlines, causing tournament-registration links to pass the
    product filter. Matching the headline itself avoids that false-positive path.
    """
    soup = BeautifulSoup(content, "html.parser")
    product_terms = (
        "arriving", "new expansion", "release promo", "new promo cards",
        "product availability", "product release", "arriving on",
    )
    results: dict[str, dict[str, str]] = {}
    for anchor in soup.select("a[href]"):
        title = clean_text(anchor.get_text(" ", strip=True))
        href = urljoin(page_url, anchor.get("href", ""))
        if not title or href.rstrip("/") == page_url.rstrip("/"):
            continue
        # Use the headline for classification; page-level/section-level context can contain
        # unrelated keywords from neighboring event cards and must not qualify this link.
        if is_excluded_accessory_product(title) or is_tournament_announcement(title):
            continue
        title_normalized = normalize_text(title)
        if not any(term in title_normalized for term in product_terms):
            continue

        # Retain a short item-local context for the Discord report, never the entire news list.
        context = title
        parent = anchor.parent
        for _ in range(5):
            if parent is None:
                break
            candidate = clean_text(parent.get_text(" ", strip=True))
            if len(title) <= len(candidate) <= 700:
                context = candidate
            if parent.name in ("li", "article"):
                break
            parent = parent.parent
        if is_tournament_announcement(context) and not any(term in title_normalized for term in product_terms):
            continue
        results[href] = {
            "store_key": source["key"],
            "store": source["name"],
            "title": title[:250],
            "url": href,
            "published": "",
            "summary": context[:700],
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
        "official_release_article_cache": {},
        "metrics": {
            "total_restock_signals": 0,
            "total_new_orderable_listings": 0,
            "total_preorder_signals": 0,
            "total_explicit_in_transit_signals": 0,
            "total_explicit_retailer_arrival_signals": 0,
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
        for key in ("initialized_stores", "store_health", "products", "initialized_feeds", "announcements", "official_source_health", "official_release_article_cache"):
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
        "new_unavailable": "🕵️ POSSIBLE STEALTH DROP — NEW LISTING (NOT ORDERABLE YET)",
        "new_unknown": "🕵️ POSSIBLE STEALTH DROP — AVAILABILITY UNKNOWN",
        "restock": "🚨 POSSIBLE STEALTH DROP — RESTOCK SIGNAL",
        "preorder_flag": "📅 PRE-ORDER SIGNAL DETECTED",
        "preorder_opened": "🟡 PRE-ORDER NOW OPEN",
        "preorder_closed": "🔴 PRE-ORDER CLOSED / UNAVAILABLE",
        "preorder_status_changed": "🔄 PRE-ORDER STATUS CHANGED",
        "in_transit": "🚚 RETAILER SAYS STOCK IS IN TRANSIT",
        "arrived_at_retailer": "📦 RETAILER REPORTS ARRIVAL / UNLOADED STOCK",
        "status_unavailable": "🔴 TCG STATUS CHANGED — NOW UNAVAILABLE",
        "status_changed": "🔄 TCG PRODUCT STATUS CHANGED",
        "arrival_eta_changed": "🗓️ TCG ARRIVAL ETA PUBLISHED / CHANGED",
        "purchase_channel_changed": "🏬 TCG PHYSICAL / ONLINE AVAILABILITY UPDATE",
        "upcoming": "📦 UPCOMING TCG STOCK SIGNAL",
        "price_drop": "💸 TCG PRICE DROP",
    }.get(kind, "🔔 POKÉMON TCG UPDATE")

    signal = product.get("incoming_signal")
    if signal == "ARRIVED_AT_RETAILER":
        incoming_text = "Explicit public wording says stock arrived, was received, or was unloaded; verify the linked source"
    elif signal == "IN_TRANSIT":
        incoming_text = "Explicit in-transit/shipment wording found"
    elif signal == "UPCOMING_OR_PREORDER" or product.get("preorder"):
        incoming_text = "Pre-order / upcoming wording found; shipment not confirmed"
    else:
        incoming_text = "No public incoming-stock wording found"

    description_parts = []
    if old and kind == "price_drop":
        description_parts.append(f"**Previous price:** {format_price(old.get('price'))}")
    if old and kind == "arrival_eta_changed":
        previous_eta = str(old.get("arrival_date") or "No ETA previously published")
        current_eta = str(product.get("arrival_date") or "ETA removed/not exposed")
        description_parts.append(f"**Arrival ETA change:** {previous_eta} → {current_eta}")
    if old and kind in ("status_unavailable", "status_changed"):
        description_parts.append(
            f"**Status change:** {old.get('lifecycle_status', stock_status(old.get('available')))} → "
            f"{product.get('lifecycle_status', stock_status(product.get('available')))}"
        )
    if old and kind == "purchase_channel_changed":
        description_parts.append(
            f"**Purchase channel change:** {old.get('purchase_channel', purchase_channel_status(old))} → "
            f"{product.get('purchase_channel', purchase_channel_status(product))}"
        )
    if old and kind in ("preorder_opened", "preorder_closed", "preorder_status_changed"):
        previous_status = preorder_status_bucket(old) or "No prior preorder signal"
        current_status = preorder_status_bucket(product) or "No longer marked as a preorder"
        description_parts.append(f"**Preorder status change:** {previous_status} → {current_status}")
        description_parts.append("Status change was inferred from public listing wording/availability; verify on the linked product page before ordering.")
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
        {"name": "Purchase channel", "value": str(product.get("purchase_channel") or purchase_channel_status(product))[:1024], "inline": True},
        {"name": "Upcoming / shipment signal", "value": incoming_text, "inline": False},
        {"name": "ETA / release date", "value": str(product.get("arrival_date") or "Not published by source")[:1024], "inline": True},
        {"name": "Branch-level stock", "value": "Not published by monitored page", "inline": True},
        {"name": "Total units", "value": "Not published by monitored page", "inline": True},
        {"name": "Source", "value": source_value[:1024], "inline": False},
    ]
    if old and kind in ("preorder_opened", "preorder_closed", "preorder_status_changed"):
        fields.extend([
            {"name": "Previous preorder status", "value": preorder_status_bucket(old) or "No prior preorder signal", "inline": True},
            {"name": "Current preorder status", "value": preorder_status_bucket(product) or "No longer marked as a preorder", "inline": True},
        ])
    if kind == "restock":
        color = 0xE74C3C  # newly orderable transition worth checking immediately
    elif kind == "price_drop":
        color = 0x3498DB
    elif kind in ("in_transit", "arrived_at_retailer", "upcoming", "preorder", "preorder_flag", "arrival_eta_changed", "purchase_channel_changed"):
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
    source_type = item.get("official_source_type", "")
    if source_type == "global_release_schedule":
        note = "Official global release schedule only. It is not confirmation of Philippine launch timing, local preorder availability, or store stock."
    elif is_official:
        note = "Official product/release news; this does not confirm Philippine retail stock or that local preorders are open."
    else:
        note = "A retailer announcement was published. Open the original post to verify date, preorder status, listed price, and availability."
    release_date_label = "Official global release date" if source_type in ("global_release_schedule", "global_release_article") else "Release / ETA stated"
    fields = [
        {"name": "Set / expansion", "value": identify_set_name(item.get("title", "") + " " + item.get("summary", ""))[:1024], "inline": True},
        {"name": "Product format", "value": identify_product_type(item.get("title", ""), item.get("summary", ""))[:1024], "inline": True},
        {"name": "Source", "value": item.get("store", "Unknown source")[:1024], "inline": True},
        {"name": release_date_label if item.get("release_date") else "Published", "value": item.get("release_date") or item.get("published") or "Date not provided in parsed page", "inline": True},
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
    run_metrics = {"restock": 0, "new_orderable": 0, "preorder": 0, "upcoming": 0, "in_transit": 0, "arrived": 0}

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
                if product.get("available") is True or product.get("incoming_signal") in ("ARRIVED_AT_RETAILER", "IN_TRANSIT", "UPCOMING_OR_PREORDER")
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
                if product.get("incoming_signal") == "ARRIVED_AT_RETAILER":
                    product_alerts.append(product_alert("arrived_at_retailer", product))
                    run_metrics["arrived"] += 1
                elif product.get("incoming_signal") == "IN_TRANSIT":
                    product_alerts.append(product_alert("in_transit", product))
                    run_metrics["in_transit"] += 1
                elif product.get("incoming_signal") == "UPCOMING_OR_PREORDER":
                    if product.get("preorder") and product.get("available") is True:
                        product_alerts.append(product_alert("preorder_opened", product))
                    elif product.get("preorder"):
                        product_alerts.append(product_alert("preorder", product))
                    else:
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
            old_preorder_status = preorder_status_bucket(old)
            new_preorder_status = preorder_status_bucket(product)
            if old_preorder_status is not None and new_preorder_status is None and product.get("jpstr_status") not in ("ON_HAND_OPEN", "ON_HAND_CLOSED"):
                current_text = normalize_text(str(product.get("signal_text", "")))
                if product.get("available") is False or any(re.search(pattern, current_text, re.I) for pattern in CLOSED_STATUS_PATTERNS):
                    new_preorder_status = "CLOSED / UNAVAILABLE"
                elif product.get("available") is True and any(re.search(pattern, current_text, re.I) for pattern in IN_STOCK_PATTERNS):
                    new_preorder_status = "PRE-ORDER ENDED / ORDERABLE"
            # Status changes take priority over generic restock signals so the Discord alert says
            # explicitly when a preorder opens or closes instead of calling everything a restock.
            if old_signal != "ARRIVED_AT_RETAILER" and new_signal == "ARRIVED_AT_RETAILER":
                product_alerts.append(product_alert("arrived_at_retailer", product, old))
                run_metrics["arrived"] += 1
            elif old_signal != "IN_TRANSIT" and new_signal == "IN_TRANSIT":
                product_alerts.append(product_alert("in_transit", product, old))
                run_metrics["in_transit"] += 1
            elif new_preorder_status == "OPEN / ORDERABLE" and old_preorder_status != "OPEN / ORDERABLE":
                product_alerts.append(product_alert("preorder_opened", product, old))
                run_metrics["preorder"] += 1
            elif old_preorder_status is not None and new_preorder_status is not None and old_preorder_status != new_preorder_status:
                if new_preorder_status == "OPEN / ORDERABLE":
                    product_alerts.append(product_alert("preorder_opened", product, old))
                elif new_preorder_status == "CLOSED / UNAVAILABLE":
                    product_alerts.append(product_alert("preorder_closed", product, old))
                else:
                    product_alerts.append(product_alert("preorder_status_changed", product, old))
                run_metrics["preorder"] += 1
            elif "incoming_signal" in old and old_signal not in ("IN_TRANSIT", "UPCOMING_OR_PREORDER") and new_signal == "UPCOMING_OR_PREORDER":
                product_alerts.append(product_alert("upcoming", product, old))
                run_metrics["upcoming"] += 1
                if product.get("preorder"):
                    run_metrics["preorder"] += 1
            elif old.get("available") is not True and product.get("available") is True:
                # Public page changed to an orderable state; this could be a restock/stealth drop.
                if new_preorder_status == "OPEN / ORDERABLE":
                    product_alerts.append(product_alert("preorder_opened", product, old))
                    run_metrics["preorder"] += 1
                else:
                    product_alerts.append(product_alert("restock", product, old))
                    run_metrics["restock"] += 1
            elif old.get("available") is True and product.get("available") is False:
                product_alerts.append(product_alert("status_unavailable", product, old))
            elif old.get("preorder") is not True and product.get("preorder") is True:
                product_alerts.append(product_alert("preorder_flag", product, old))
                run_metrics["preorder"] += 1
            elif product.get("arrival_date") and product.get("arrival_date") != old.get("arrival_date") and (
                product.get("incoming_signal") in ("IN_TRANSIT", "ARRIVED_AT_RETAILER", "UPCOMING_OR_PREORDER") or product.get("preorder")
            ):
                product_alerts.append(product_alert("arrival_eta_changed", product, old))
            elif old.get("purchase_channel") and product.get("purchase_channel") != old.get("purchase_channel") and (
                "PHYSICAL/IN-STORE" in str(product.get("purchase_channel", ""))
            ):
                product_alerts.append(product_alert("purchase_channel_changed", product, old))
            elif old.get("lifecycle_status") and product.get("lifecycle_status") != old.get("lifecycle_status"):
                product_alerts.append(product_alert("status_changed", product, old))
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
        items = [
            item for item in items
            if is_tcg_release_in_window(str(item.get("title", "")), str(item.get("summary", "")))
        ]
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

    # Public Facebook page posts from Game One PH and GameLine. Facebook may serve login/block
    # shells despite public visibility; do not pretend those runs had full coverage.
    for source in SOCIAL_PAGE_SOURCES:
        items, usable = scrape_public_facebook_page(session, source)
        items = [
            item for item in items
            if not is_excluded_accessory_product(str(item.get("title", "")))
            and is_tcg_release_in_window(str(item.get("title", "")), str(item.get("summary", "")))
        ]
        source_url = source["url"]
        source_already_initialized = state["initialized_feeds"].get(source_url) is True
        if not usable:
            LOG.warning("Facebook source is not reliably readable this run: %s", source_url)
            continue
        for item in items:
            key = announcement_state_key(source["key"], item["url"])
            if key in state["announcements"]:
                continue
            state["announcements"][key] = {
                "store": source["name"], "title": item["title"], "url": item["url"],
                "published": item.get("published", ""), "first_seen": utc_now_iso(),
            }
            if source_already_initialized:
                announcement_alerts.append(announcement_alert(item))
        state["initialized_feeds"][source_url] = True

    # Official public sources: Asia/Philippines news, global release schedule, and official release articles.
    official_news_counts: dict[str, int] = {}
    for source in OFFICIAL_NEWS_SOURCES:
        items, healthy = scrape_official_source(session, source, state)
        items = [
            item for item in items
            if not is_excluded_accessory_product(str(item.get("title", "")))
            and is_tcg_release_in_window(
                str(item.get("title", "")),
                str(item.get("summary", "")),
                str(item.get("release_date", "")),
            )
        ]
        prior_health = state["official_source_health"].get(source["key"])
        state["official_source_health"][source["key"]] = healthy
        if not healthy:
            LOG.warning("Official release source could not be read: %s", source["url"])
            continue
        official_news_counts[source["key"]] = len(items)
        page_already_initialized = state["initialized_feeds"].get(source["url"]) is True
        LOG.info("Official release source fetched (%s): %s relevant item(s) matched", source["key"], len(items))
        for item in items:
            key = announcement_state_key(source["key"], item["url"])
            if key in state["announcements"]:
                continue
            state["announcements"][key] = {
                "store": source["name"], "title": item["title"], "url": item["url"],
                "published": item.get("published", ""), "release_date": item.get("release_date", ""),
                "first_seen": utc_now_iso(),
            }
            # New sources are baselined to avoid replaying old news. However, explicit future
            # official release dates are useful immediately, so alert once when first discovered.
            if page_already_initialized or item.get("alert_on_first_scan") == "true":
                announcement_alerts.append(announcement_alert(item))
        state["initialized_feeds"][source["url"]] = True

    if _BROWSER_PAGES_USED:
        LOG.info("JavaScript browser fallback rendered %s public page(s) this scan", _BROWSER_PAGES_USED)

    # Keep cumulative counts across GitHub Actions runs. These are detected signals,
    # not physical unit counts and not a branch-by-branch inventory total.
    metrics = state.setdefault("metrics", {})
    for key, value in {
        "total_restock_signals": run_metrics["restock"],
        "total_new_orderable_listings": run_metrics["new_orderable"],
        "total_preorder_signals": run_metrics["preorder"],
        "total_explicit_in_transit_signals": run_metrics["in_transit"],
        "total_explicit_retailer_arrival_signals": run_metrics["arrived"],
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
    article_cache = state.get("official_release_article_cache", {})
    if isinstance(article_cache, dict) and len(article_cache) > 30:
        state["official_release_article_cache"] = dict(list(article_cache.items())[-30:])

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
            if signal == "ARRIVED_AT_RETAILER":
                label = "ARRIVED / UNLOADED AT RETAILER"
            elif signal == "IN_TRANSIT":
                label = "IN TRANSIT CLAIM"
            elif signal == "UPCOMING_OR_PREORDER":
                label = "UPCOMING / PRE-ORDER"
            else:
                label = "ORDERABLE"
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

    # Versioned source report: resend when source coverage/lifecycle tracking is upgraded.
    # Use Discord fields and separate embeds so source health is easy to scan.
    if int(state.get("source_report_version", 0) or 0) < 5:
        scan_products = [p for items in current_products_by_store.values() for p in items]
        scan_tcg_count = len(scan_products)
        scan_orderable = sum(1 for p in scan_products if p.get("available") is True)
        scan_preorder = sum(1 for p in scan_products if str(p.get("lifecycle_status", "")).startswith("PRE-ORDER"))
        scan_transit = sum(1 for p in scan_products if p.get("lifecycle_status") == "IN TRANSIT")
        scan_arrived = sum(1 for p in scan_products if p.get("lifecycle_status") == "ARRIVED AT RETAILER / WAREHOUSE")
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
                {"name": "📦 Arrived/unloaded (explicit)", "value": str(scan_arrived), "inline": True},
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
            arrived_count = sum(1 for p in items if p.get("lifecycle_status") == "ARRIVED AT RETAILER / WAREHOUSE")
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
                    {"name": "📦 Reported retailer/warehouse arrival", "value": str(arrived_count), "inline": True},
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
                "title": f"🟣 {source['name']}",
                "url": source["url"],
                "description": (
                    f"**Status:** {'🟢 Page fetched' if healthy else '🔴 Not reachable this scan'}\n"
                    f"**Product/release news links matched:** {matched}\n\n"
                    "Official release sources can identify sets, formats, and global dates. They do not confirm Philippine retailer stock, local preorder availability, or branch allocation."
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
            "ARRIVED AT RETAILER / WAREHOUSE": 0,
            "IN TRANSIT": 1,
            "PRE-ORDER OPEN": 2,
            "PRE-ORDER — STATUS UNCLEAR": 3,
            "PRE-ORDER — STATUS CLOSED/UNAVAILABLE": 4,
            "INCOMING / UPCOMING": 5,
            "IN STOCK (explicit wording)": 6,
            "LIVE / ORDERABLE": 7,
            "SOLD OUT / CLOSED": 8,
            "PRE-ORDER CLOSED / SOLD OUT": 9,
            "UNKNOWN / NOT EXPOSED": 10,
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
            "ARRIVED AT RETAILER / WAREHOUSE": "📦", "IN TRANSIT": "🚚", "PRE-ORDER OPEN": "🟡", "PRE-ORDER — STATUS UNCLEAR": "🟡",
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
            channel = str(product.get("purchase_channel") or purchase_channel_status(product))
            lifecycle_lines.append(
                f"{emoji} **{status_name}** · {retailer}\n"
                f"{title_link}\n"
                f"**Set:** {set_name} · **Type:** {product_type}\n"
                f"**Price:** {price} · **Purchase:** {channel}\n"
                f"**ETA/release:** {eta}"
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
                if signal == "ARRIVED_AT_RETAILER":
                    label = "📦 ARRIVED / UNLOADED AT RETAILER (explicit wording)"
                elif signal == "IN_TRANSIT":
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
                channel = str(product.get("purchase_channel") or purchase_channel_status(product))
                lines.append(f"**{label}** · {store_name}\n{link_title}\n**Detected status:** {lifecycle}\n**Set:** {set_name} · **Type:** {product_type}\n**Purchase channel:** {channel}\n**ETA/release date:** {eta} · **Branch allocation:** not published")
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
                "**Arrival dates:** displayed only when a retailer page explicitly provides a date phrase (including ETA wording). ‘In transit’ and ‘arrived/unloaded’ statuses are used only when public wording supports them; the watcher does not infer private logistics or invent an ETA."
            ),
            "color": 0x34495E,
        })
        send_discord(session, report_embeds, content="🛰️ **Pokémon TCG Drop Radar — readable source report**\nColor key: 🟢 listings parsed · 🟡 page responded but no TCG listings parsed · 🔴 page unavailable · 🟣 official news only")
        state["source_report_sent"] = True
        state["source_report_version"] = 5

    save_state(state)
    LOG.info("Scan complete: %s product(s) visible, %s currently marked orderable; %s product alert(s), %s announcement alert(s).",
             total_products, total_available, len(product_alerts), len(announcement_alerts))
    return 0


if __name__ == "__main__":
    sys.exit(main())
