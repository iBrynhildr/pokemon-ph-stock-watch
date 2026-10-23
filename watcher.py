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
