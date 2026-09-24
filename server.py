# /// script
# requires-python = ">=3.12"
# dependencies = ["mcp>=1.2.0,<2", "httpx>=0.27"]
# ///
"""MCP-сервер: где смотреть фильмы и сериалы в российских стримингах.

Данные — JustWatch (неофициальный GraphQL API), регион RU.
Инструменты:
  - where_to_watch(query)  — найти фильм/сериал и показать, где он доступен
  - popular_on(service)    — популярное сейчас на конкретном сервисе

Особенности API, выясненные опытным путём:
  - Кинопоиск = "kpk" (не "kpsk"), ivi в RU-провайдерах JustWatch отсутствует
  - searchTitles требует аргумент source, у popularTitles его нет
  - retailPrice требует аргумент language
"""

import re
import time

import httpx
from urllib.parse import quote
from mcp.server.fastmcp import FastMCP
from typing import Literal

API = "https://apis.justwatch.com/graphql"
COUNTRY = "RU"
LANGUAGE = "ru"
TIMEOUT = 25.0

# short_name JustWatch -> человекочитаемое название
SERVICES = {
    "kpk": "Кинопоиск",
    "okk": "Okko",
    "mre": "more.tv",
    "pre": "Premier",
    "ama": "Amediateka",
    "ivi": "ivi",
    "itu": "iTunes",
    "atp": "Apple TV+",
    "mbi": "MUBI",
    "tvg": "Tvigle",
    "nfx": "Netflix",
    "wink": "Wink",
    "start": "Start",
    "kion": "Кион",
}

# алиасы для popular_on -> short_name
SERVICE_ALIASES = {
    "кинопоиск": "kpk", "kinopoisk": "kpk", "кп": "kpk", "kp": "kpk",
    "окко": "okk", "okko": "okk",
    "more": "mre", "more.tv": "mre", "moretv": "mre", "мортв": "mre",
    "premier": "pre", "премьер": "pre",
    "amediateka": "ama", "амедиатека": "ama",
    "ivi": "ivi", "иви": "ivi",
    "itunes": "itu", "айтаюнс": "itu",
    "apple tv": "atp", "appletv": "atp", "apple": "atp",
    "mubi": "mbi", "муби": "mbi",
    "tvigle": "tvg", "твигл": "tvg",
}

KIND_MAP = {"movie": ["MOVIE"], "tv": ["SHOW"], "any": ["MOVIE", "SHOW"]}

# порядок группировок в выдаче
GROUPS = [
    ("FLATRATE", "подписка"),
    ("FREE", "бесплатно"),
    ("RENT", "аренда"),
    ("BUY", "покупка"),
]

SEARCH_QUERY = """
query ($country: Country!, $first: Int!, $filter: TitleFilter!, $language: Language!, $source: String!) {
  searchTitles(country: $country, first: $first, filter: $filter, language: $language, source: $source) {
    totalCount
    edges {
      node {
        objectType
        content(country: $country, language: $language) {
          title
          originalTitle
          originalReleaseYear
          scoring { imdbScore }
        }
        offers(country: $country, platform: WEB) {
          monetizationType
          retailPrice(language: $language)
          currency
          standardWebURL
          package { shortName }
        }
      }
    }
  }
}
"""

POPULAR_QUERY = """
query ($country: Country!, $first: Int!, $filter: TitleFilter!, $language: Language!) {
  popularTitles(country: $country, first: $first, filter: $filter, language: $language) {
    edges {
      node {
        objectType
        content(country: $country, language: $language) {
          title
          originalTitle
          originalReleaseYear
          scoring { imdbScore }
        }
        offers(country: $country, platform: WEB) {
          monetizationType
          retailPrice(language: $language)
          currency
          standardWebURL
          package { shortName }
        }
      }
    }
  }
}
"""

mcp = FastMCP("where-to-watch")

# --- Живая проверка Амедиатеки (каталог из sitemap: поиск + страница тайтла) ---

AMED_SITEMAPS = [
    "https://www.amediateka.ru/sitemap-content.xml",
    "https://www.amediateka.ru/sitemap-seasons.xml",
]
AMED_TTL = 6 * 3600
_amed_cache = [0.0, []]  # [timestamp, [(kind, url), ...]]

# транслит, совместимый со слагами Амедиатки (poymat-meri, goryachiy-kofe...)
AMED_TRANS = {
    "а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "е": "e", "ё": "e",
    "ж": "zh", "з": "z", "и": "i", "й": "y", "к": "k", "л": "l", "м": "m",
    "н": "n", "о": "o", "п": "p", "р": "r", "с": "s", "т": "t", "у": "u",
    "ф": "f", "х": "h", "ц": "ts", "ч": "ch", "ш": "sh", "щ": "shch",
    "ъ": "", "ы": "y", "ь": "", "э": "e", "ю": "yu", "я": "ya",
}


async def amed_catalog() -> list[tuple[str, str]]:
    """Каталог Амедиатки из sitemap-ов, с кэшем на 6 часов."""
    if _amed_cache[0] and time.time() - _amed_cache[0] < AMED_TTL:
        return _amed_cache[1]
    items: list[tuple[str, str]] = []
    async with httpx.AsyncClient(timeout=TIMEOUT, headers={"User-Agent": KP_UA}, follow_redirects=True) as client:
        for sm in AMED_SITEMAPS:
            try:
                resp = await client.get(sm)
                for loc in re.findall(r"<loc>([^<]+)</loc>", resp.text):
                    m = re.search(r"/watch/([a-z]+)_\d+_([a-z0-9-]+)$", loc)
                    if m:
                        items.append((m.group(1), loc))
            except httpx.HTTPError:
                continue
    _amed_cache[0], _amed_cache[1] = time.time(), items
    return items


def amed_candidates(query: str, items: list[tuple[str, str]], cap: int = 8) -> list[tuple[float, str, str]]:
    q = "".join(AMED_TRANS.get(c, c) for c in query.lower().replace("ё", "е"))
    toks = [t for t in re.split(r"[^a-z0-9]+", q) if len(t) >= 3]
    if not toks:
        return []
    scored = []
    for kind, url in items:
        words = [w for w in re.split(r"[-_0-9]+", url.rsplit("/", 1)[-1]) if w]
        # сравниваем по словам слага, иначе "мэри" влезает в "american",
        # а короткое "la" — в "лалаленд"
        matched = sum(
            1 for t in toks
            if any(w.startswith(t[:3]) or (len(w) >= 3 and t.startswith(w[:3])) for w in words)
        )
        if matched == len(toks) or (len(toks) >= 3 and matched >= len(toks) - 1):
            scored.append((matched / len(toks), kind, url))
    scored.sort(reverse=True)
    return scored[:cap]

# --- Живая проверка Кинопоиска (первоисточник, в отличие от JustWatch) ---

KP_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
         "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")
KP_FILM = "https://www.kinopoisk.ru/film/{id}/"

# Кнопки онлайн-кинотеатра: матчим только HTML-разметку (>текст<), не JSON-конфиги
# UI вида "buttonText":"Смотреть сериал", которые лежат на каждой странице
KP_WATCH_RE = re.compile(r">\s*Смотреть (фильм|по подписке)\s*<")
KP_PRICE_RE = re.compile(r"Смотреть за(?:\s|<[^>]+>)*(\d[\d\s]*)", re.S)
KP_TITLE_RE = re.compile(r'<meta property="og:title" content="([^"]+)"')
KP_MOST_WANTED_RE = re.compile(r'<div class="element most_wanted"')
KP_ID_RE = re.compile(r'data-id="(\d+)" data-type="film"')


async def kp_get(client: httpx.AsyncClient, url: str) -> str:
    """Первый запрос Кинопоиск 302-ит на SSO, но выставляет куку
    disable_server_sso_redirect — повторный запрос с куками отдаёт страницу."""
    resp = await client.get(url)
    if resp.status_code in (301, 302, 303):
        resp = await client.get(url)
    resp.raise_for_status()
    return resp.text


def kp_status(html: str) -> str:
    if KP_WATCH_RE.search(html):
        return "✅ доступен (по подписке)"
    m = KP_PRICE_RE.search(html)
    if m:
        price = re.sub(r"<[^>]+>|\s+", "", m.group(1))
        return f"ℹ️ доступен за {price}₽ (аренда/покупка)"
    if "Буду смотреть" in html:
        return "❌ в онлайн-кинотеатре недоступен"
    if "captcha" in html.lower():
        return "❓ Кинопоиск показал капчу"
    return "❓ не удалось разобрать страницу"


def kp_title(html: str) -> str:
    m = KP_TITLE_RE.search(html)
    return m.group(1) if m else ""


async def kp_search_ids(client: httpx.AsyncClient, query: str, cap: int = 8) -> list[str]:
    html = await kp_get(client, "https://www.kinopoisk.ru/index.php?kp_query=" + quote(query))
    # приоритет — блок «Скорее всего, вы ищете», остальное — просто кандидаты
    ids = [m.group(1) for m in KP_ID_RE.finditer(html)]
    blocks = re.split(r'<div class="element ', html)
    priority = [KP_ID_RE.search(b).group(1) for b in blocks
                if b.startswith("most_wanted") and KP_ID_RE.search(b)]
    seen, ordered = set(), priority + ids
    return [i for i in ordered if not (i in seen or seen.add(i))][:cap]


async def gql(query: str, variables: dict) -> dict:
    async with httpx.AsyncClient(timeout=TIMEOUT, headers={"User-Agent": "justwatch-mcp/0.1"}) as client:
        resp = await client.post(API, json={"query": query, "variables": variables})
        # JustWatch отдаёт GraphQL-ошибки с не-200 статусом — читаем тело всегда
        try:
            data = resp.json()
        except Exception:
            resp.raise_for_status()
            raise RuntimeError(f"HTTP {resp.status_code}, в ответе нет JSON")
    if data.get("errors"):
        raise RuntimeError("; ".join(e.get("message", "?") for e in data["errors"]))
    return data["data"]


def format_price(offer: dict) -> str:
    # retailPrice(language: "ru") приходит готовой строкой вида "99,00₽"
    price = offer.get("retailPrice")
    if not price:
        return ""
    return f" ({str(price).replace(',00', '')})"


def format_offers(offers: list[dict]) -> str:
    seen: set[tuple[str, str]] = set()
    parts = []
    for mtype, label in GROUPS:
        names = []
        for o in offers:
            if o["monetizationType"] != mtype:
                continue
            short = o["package"]["shortName"]
            if (short, mtype) in seen:
                continue
            seen.add((short, mtype))
            name = SERVICES.get(short, short)
            url = o.get("standardWebURL")
            if url and not url.startswith("https://www.justwatch.com"):
                if "tv.apple.com" in url:  # отрезаем трекинг-параметры Apple
                    url = url.split("?")[0]
                name = f"[{name}]({url})"
            if mtype in ("RENT", "BUY"):
                name += format_price(o)
            names.append(name)
        if names:
            parts.append(f"{label}: " + ", ".join(names))
    return " · ".join(parts) if parts else "— нигде нет"


def format_title(node: dict, with_offers: bool = True) -> str:
    c = node["content"]
    title = c["title"]
    if c.get("originalTitle") and c["originalTitle"] != title:
        title += f" / {c['originalTitle']}"
    if c.get("originalReleaseYear"):
        title += f" ({c['originalReleaseYear']})"
    if node.get("objectType") == "SHOW":
        title += " [сериал]"
    score = (c.get("scoring") or {}).get("imdbScore")
    if score:
        title += f" — IMDb {score:g}"
    if with_offers:
        title += "\n    " + format_offers(node.get("offers") or [])
    return title


@mcp.tool()
async def where_to_watch(
    query: str,
    kind: Literal["movie", "tv", "any"] = "any",
    limit: int = 5,
) -> str:
    """Найти фильм/сериал по названию (рус. или англ.) и показать, где он доступен в РФ: подписка, аренда, покупка.

    Данные JustWatch. Надёжность по сервисам разная: Okko/more.tv/Premier — хорошо,
    Кинопоиск (kpk) — бывают и ложные «есть по подписке», и пропуски: результат
    по Кинопоиску проверяй инструментом check_kinopoisk (парсит первоисточник).
    Используй ПЕРЕД тем, как рекомендовать фильм, чтобы не советовать то, чего нет
    на сервисах.
    """
    data = await gql(
        SEARCH_QUERY,
        {
            "country": COUNTRY,
            "first": 20,
            "filter": {"searchQuery": query, "objectTypes": KIND_MAP[kind]},
            "language": LANGUAGE,
            "source": "ALL",
        },
    )
    result = data["searchTitles"]
    edges = result["edges"]
    if not edges:
        return f"По запросу «{query}» ничего не нашлось в JustWatch (регион RU)."

    # сначала — то, что доступно по подписке; внутри — по рейтингу
    def key(e: dict) -> tuple:
        offers = e["node"].get("offers") or []
        has_flat = any(o["monetizationType"] == "FLATRATE" for o in offers)
        score = ((e["node"]["content"].get("scoring") or {}).get("imdbScore") or 0)
        return (not has_flat, -score)

    edges.sort(key=key)
    shown = edges[:limit]
    lines = [f"«{query}» — найдено {result['totalCount']}, показываю топ {len(shown)}:"]
    for e in shown:
        lines.append("- " + format_title(e["node"]))

    offers = [o for e in shown for o in (e["node"].get("offers") or [])]
    if any(o["package"]["shortName"] == "kpk" for o in offers):
        lines.append("⚠️ Данные JustWatch по Кинопоиску бывают неточны — перепроверь на странице фильма.")
    elif not any(o["monetizationType"] == "FLATRATE" for o in offers):
        lines.append(
            "⚠️ По подписке у JustWatch не нашлось, но их данные по Кинопоиску неполны — "
            f"проверь поиск: https://www.kinopoisk.ru/index.php?kp_query={quote(query)}"
        )
    return "\n".join(lines)


@mcp.tool()
async def check_kinopoisk(query: str, limit: int = 3) -> str:
    """Живая проверка на kinopoisk.ru: поиск по названию → статус в онлайн-кинотеатре.

    Данные первоисточника (точно, в отличие от JustWatch). Используй чтобы:
    1) подтвердить «подписка: Кинопоиск» от where_to_watch (их данные бывают ложными);
    2) опровергнуть «нигде нет» — Кинопоиск может быть упущен в JustWatch.
    Статусы: доступен (подписка) / за N₽ / недоступен.
    """
    headers = {"User-Agent": KP_UA, "Accept-Language": "ru-RU,ru;q=0.9"}
    async with httpx.AsyncClient(timeout=TIMEOUT, headers=headers, follow_redirects=False) as client:
        ids = await kp_search_ids(client, query)
        if not ids:
            return f"Кинопоиск: по запросу «{query}» ничего не нашлось."
        lines = []
        for kp_id in ids[:limit]:
            html = await kp_get(client, KP_FILM.format(id=kp_id))
            title = kp_title(html) or f"film/{kp_id}"  # og:title уже с «кавычками»
            lines.append(f"- {title} — {kp_status(html)} (id {kp_id})")
    return "Кинопоиск (живая проверка):\n" + "\n".join(lines)


@mcp.tool()
async def check_amediateka(query: str, limit: int = 3) -> str:
    """Живая проверка Амедиатеки: поиск по каталогу сайта + открытие страницы тайтла.

    Амедиатека почти весь каталог держит в подписке, поэтому «есть в каталоге»
    обычно значит «доступен по подписке» (единичные позиции — покупка/аренда,
    это видно только на самой странице).
    """
    items = await amed_catalog()
    if not items:
        return "Амедиатека: не удалось получить каталог (sitemap недоступен)."
    cands = amed_candidates(query, items)
    if not cands:
        return f"❌ В каталоге Амедиатеки «{query}» не нашлось (всего тайтлов: {len(items)})."
    headers = {"User-Agent": KP_UA, "Accept-Language": "ru-RU,ru;q=0.9"}
    async with httpx.AsyncClient(timeout=TIMEOUT, headers=headers, follow_redirects=True) as client:
        lines = []
        for _, kind, url in cands[:limit]:
            try:
                resp = await client.get(url)
                title = kp_title(resp.text) or url.rsplit("/", 1)[-1]
                kind_ru = "сериал" if kind.startswith(("serial", "series", "season")) else "фильм"
                lines.append(f"- {title} — ✅ есть в Амедиатеке ({kind_ru}) {url}")
            except httpx.HTTPError:
                lines.append(f"- {url} — страница не открылась")
    return ("Амедиатека (живая проверка, каталог " + str(len(items)) + " тайтлов):\n"
            + "\n".join(lines)
            + "\n⚠️ «есть в каталоге» = почти всегда по подписке; отдельные позиции могут быть за N₽.")


@mcp.tool()
async def popular_on(
    service: str,
    kind: Literal["movie", "tv", "any"] = "movie",
    limit: int = 10,
) -> str:
    """Популярные сейчас фильмы или сериалы на конкретном сервисе (кинопоиск, okko, premier, more.tv, амедиатека...)."""
    short = SERVICE_ALIASES.get(service.strip().lower())
    if not short:
        known = ", ".join(sorted(set(SERVICE_ALIASES.values())))
        return f"Неизвестный сервис «{service}». Доступны (short_name): {known}."
    data = await gql(
        POPULAR_QUERY,
        {
            "country": COUNTRY,
            "first": 100,
            "filter": {"objectTypes": KIND_MAP[kind]},
            "language": LANGUAGE,
        },
    )
    picked = [
        e["node"]
        for e in data["popularTitles"]["edges"]
        if any(o["package"]["shortName"] == short for o in (e["node"].get("offers") or []))
    ][:limit]
    if not picked:
        return f"Среди 100 популярных сейчас не нашлось доступных на {SERVICES.get(short, short)}."
    lines = [f"Популярное на {SERVICES.get(short, short)} ({'фильмы' if kind == 'movie' else kind if kind != 'any' else 'фильмы и сериалы'}):"]
    for i, node in enumerate(picked, 1):
        lines.append(f"{i}. " + format_title(node))
    if short == "kpk":
        lines.append("⚠️ Данные JustWatch по Кинопоиску неполны — список может быть не полным.")
    return "\n".join(lines)


if __name__ == "__main__":
    mcp.run()
