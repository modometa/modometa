"""Utility functions, caching decorators, and data helpers for views."""

import functools
from collections import Counter
from collections.abc import Iterable
from datetime import date
from datetime import timedelta

from django.conf import settings
from django.core.cache import cache
from django.db.models import Max
from django.db.models import Min
from django.utils.cache import patch_cache_control

from core.models.card import Card
from core.models.card import CardLookup
from core.models.card import generate_card_slug
from core.models.card import get_gatherer_url
from core.models.card import get_scryfall_url
from core.models.card import normalize_card_name
from core.models.deck import Deck
from core.models.tournament import Tournament


VALID_TIMEFRAMES = {30, 90, 180, 365}
DEFAULT_TIMEFRAME = 90

CARD_TYPE_CATEGORIES: list[tuple[str, str, str]] = [
    ("creature", "Creatures", "creature"),
    ("planeswalker", "Planeswalkers", "planeswalker"),
    ("instant", "Instants", "instant"),
    ("sorcery", "Sorceries", "sorcery"),
    ("artifact", "Artifacts", "artifact"),
    ("enchantment", "Enchantments", "enchantment"),
    ("battle", "Battles", "battle"),
    ("land", "Lands", "land"),
    ("other", "Other", "multiple"),
]


def public_cache(
    cdn_seconds: int = 3600,
    browser_seconds: int = 300,
    stale_error_seconds: int = 86400 * 3,
):
    """Cache view output in LocMemCache and emit Cloudflare & browser caching headers.

    - Server: Caches full rendered response in LocMemCache (unless IS_TESTING is True).
    - Browser: Cache-Control: max-age=<browser_seconds>
    - CDN (Cloudflare): Cache-Control: s-maxage=<cdn_seconds>, stale-if-error=<stale_error_seconds>
      and Cloudflare-CDN-Cache-Control.
    """

    def decorator(view_func):
        @functools.wraps(view_func)
        def _wrapped_view(request, *args, **kwargs):
            if request.method != "GET" or settings.DEBUG:
                return view_func(request, *args, **kwargs)

            is_testing = getattr(settings, "IS_TESTING", False)
            cache_key = f"page_cache_v2:{request.get_full_path()}"

            if not is_testing:
                cached_response = cache.get(cache_key)
                if cached_response is not None:
                    return cached_response

            response = view_func(request, *args, **kwargs)
            if hasattr(response, "render") and callable(response.render):
                response.render()

            if response.status_code == 200:
                # Allow views to override cache durations by setting attributes on the response object
                # This allows us to set very long cache durations on old tournaments/decks
                # that will never change again.
                actual_cdn_seconds = getattr(response, "_cdn_seconds", cdn_seconds)
                actual_browser_seconds = getattr(
                    response, "_browser_seconds", browser_seconds
                )
                actual_stale_error_seconds = getattr(
                    response, "_stale_error_seconds", stale_error_seconds
                )
                cache_control_kwargs = {
                    "public": True,
                    "max_age": actual_browser_seconds,
                    "s_maxage": actual_cdn_seconds,
                }
                cf_directives = [f"max-age={actual_cdn_seconds}"]
                if actual_stale_error_seconds:
                    cache_control_kwargs["stale_if_error"] = actual_stale_error_seconds
                    cf_directives.append(f"stale-if-error={actual_stale_error_seconds}")

                patch_cache_control(response, **cache_control_kwargs)
                response["Cloudflare-CDN-Cache-Control"] = ", ".join(cf_directives)

                if not is_testing:
                    cache.set(cache_key, response, timeout=actual_cdn_seconds)

            return response

        return _wrapped_view

    return decorator


def get_cards_map(card_names: Iterable[str]) -> dict[str, Card]:
    """Resolve a collection of card names to Card model instances.

    Uses CardLookup (ordered by priority ascending so highest priority wins)
    first to map canonical names, DFCs, split cards, and aliases to their
    canonical Card instance. Falls back to direct Card matching by exact name
    and normalized name, prioritizing playable cards over placeholder/art cards.
    Defers the heavy printings JSONField to avoid unnecessary payload/memory overhead.
    Returns a dict mapping original card names to Card instances.
    """
    names_set = set(card_names)
    if not names_set:
        return {}

    cards_map: dict[str, Card] = {}

    # Map normalized names to original name strings (preserving user case/spelling)
    norm_to_origs: dict[str, list[str]] = {}
    for n in names_set:
        norm = normalize_card_name(n)
        if norm:
            norm_to_origs.setdefault(norm, []).append(n)

    # 1. Resolve via CardLookup ordered by priority ascending (highest priority wins)
    if norm_to_origs:
        lookups = (
            CardLookup.objects.filter(lookup_name__in=list(norm_to_origs.keys()))
            .select_related("card")
            .defer("card__printings")
            .order_by("priority")
        )
        for cl in lookups:
            if cl.card and cl.card.type_line not in ("Card", "Card // Card", ""):
                for orig in norm_to_origs.get(cl.lookup_name, []):
                    cards_map[orig] = cl.card

    # 2. For any names still missing, try exact Card.name
    missing = [n for n in names_set if n not in cards_map]
    if missing:
        cards = Card.objects.filter(name__in=missing).defer("printings")
        for c in cards:
            if c.name not in cards_map or (
                cards_map[c.name].type_line in ("Card", "Card // Card", "")
                and c.type_line not in ("Card", "Card // Card", "")
            ):
                cards_map[c.name] = c

    # 3. For any names still missing, try Card.normalized_name
    still_missing = [n for n in names_set if n not in cards_map]
    if still_missing:
        missing_norms: dict[str, list[str]] = {}
        for n in still_missing:
            norm = normalize_card_name(n)
            if norm:
                missing_norms.setdefault(norm, []).append(n)

        cards = Card.objects.filter(
            normalized_name__in=list(missing_norms.keys())
        ).defer("printings")
        for c in cards:
            for orig in missing_norms.get(c.normalized_name, []):
                if orig not in cards_map or (
                    cards_map[orig].type_line in ("Card", "Card // Card", "")
                    and c.type_line not in ("Card", "Card // Card", "")
                ):
                    cards_map[orig] = c

    return cards_map


def get_reference_date() -> date:
    """Get latest tournament date in database, or today if empty."""
    latest = Tournament.objects.aggregate(max_d=Max("date"))["max_d"]
    return latest or date.today()


def parse_timeframe(request, default: int = DEFAULT_TIMEFRAME) -> int:
    """Parse and validate timeframe in days from request GET parameters."""
    val = request.GET.get("days")
    try:
        days = int(val)
        return days if days in VALID_TIMEFRAMES else default
    except ValueError, TypeError:
        return default


def get_timeframe_cutoff(days: int = DEFAULT_TIMEFRAME) -> tuple[date, date]:
    """Return (cutoff_date, reference_date)."""
    ref = get_reference_date()
    cutoff = ref - timedelta(days=days)
    return cutoff, ref


def get_dataset_min_date() -> date | None:
    """Get the earliest date of leagues or challenges in the dataset, cached for 4 hours."""
    is_testing = getattr(settings, "IS_TESTING", False)
    key = "dataset_min_date_v1"
    min_date = None if is_testing else cache.get(key)
    if min_date is None:
        min_date = Tournament.objects.filter(
            event_type__in=["league", "challenge"]
        ).aggregate(min_d=Min("date"))["min_d"]
        if not min_date:
            min_date = Tournament.objects.aggregate(min_d=Min("date"))["min_d"]
        if min_date and not is_testing:
            cache.set(key, min_date, timeout=4 * 3600)
    return min_date


def get_dataset_start_year() -> int | None:
    """Get the earliest year of leagues or challenges in the dataset."""
    min_date = get_dataset_min_date()
    return min_date.year if min_date else None


def get_format_card_stats(fmt_slug: str, days: int = 90, active_type: str = "") -> dict:
    """Retrieve or compute cached card statistics for a format, timeframe, and event type."""
    cache_key = f"cards_list_v4:{fmt_slug}:{days}:{active_type or 'all'}"
    cached_data = cache.get(cache_key)
    if cached_data is not None:
        return cached_data

    cutoff, ref_date = get_timeframe_cutoff(days)
    tourns_qs = Tournament.objects.filter(
        format=fmt_slug, date__gte=cutoff, date__lte=ref_date
    )
    if active_type:
        tourns_qs = tourns_qs.filter(event_type=active_type)

    decks_qs = Deck.objects.filter(
        format=fmt_slug,
        tournament__in=tourns_qs,
    )

    mb_counts = Counter()
    sb_counts = Counter()
    total_copies = Counter()
    any_counts = Counter()
    total_decks = 0

    # Chunking by 2k avoids issues on large formats over longer timeframes
    for d in decks_qs.values("mainboard", "sideboard").iterator(chunk_size=2000):
        total_decks += 1
        deck_mb = set()
        for item in d.get("mainboard") or []:
            name = item.get("card", "")
            cnt = item.get("count", 1)
            if name:
                deck_mb.add(name)
                total_copies[name] += cnt
        for c in deck_mb:
            mb_counts[c] += 1
            any_counts[c] += 1

        deck_sb = set()
        for item in d.get("sideboard") or []:
            name = item.get("card", "")
            cnt = item.get("count", 1)
            if name:
                deck_sb.add(name)
                total_copies[name] += cnt
        for c in deck_sb:
            sb_counts[c] += 1
            if c not in deck_mb:
                any_counts[c] += 1

    denom = max(1, total_decks)
    sorted_cards = any_counts.most_common()
    card_names = [c[0] for c in sorted_cards]
    cards_map = get_cards_map(card_names)

    card_rows = []
    for name, cnt in sorted_cards:
        card = cards_map.get(name)
        slug = card.slug if card and card.slug else generate_card_slug(name)
        card_rows.append(
            {
                "name": name,
                "slug": slug,
                "mana_cost": card.mana_cost if card and card.mana_cost else "",
                "type_line": card.type_line if card else "Card",
                "image_uri": card.image_uri if card else None,
                "scryfall_url": card.scryfall_url
                if card
                else get_scryfall_url(name=name),
                "gatherer_url": card.gatherer_url if card else get_gatherer_url(name),
                "any_count": cnt,
                "mb_count": mb_counts[name],
                "sb_count": sb_counts[name],
                "adoption_pct": round((cnt / denom) * 100, 1),
                "mb_pct": round((mb_counts[name] / denom) * 100, 1),
                "sb_pct": round((sb_counts[name] / denom) * 100, 1),
                "avg_copies": round(total_copies[name] / max(1, cnt), 1),
            }
        )

    result = {"card_rows": card_rows, "total_decks": total_decks}
    cache.set(cache_key, result, timeout=3600)
    return result


def classify_card_type(card: Card | None) -> str:
    """Classify a card into a type category key.

    Precedence:
    1. If it's a MDFC, use the front face for the type.
    2. If it's a land, always put in 'land' (e.g. Dryad Arbor is a land creature -> land).
    3. If not land, put in 'creature' if it's a creature (e.g. Overlord of the Balemurk -> creature).
    4. Otherwise choose one of the remaining categories:
       planeswalker, instant, sorcery, artifact, enchantment, battle.
    """
    if not card or not card.type_line:
        return "other"

    front_face = card.type_line.split(" // ")[0].strip().lower()
    main_type = front_face.split("—")[0].split("-")[0]
    words = set(main_type.split())

    if "land" in words:
        return "land"
    if "creature" in words:
        return "creature"
    if "planeswalker" in words:
        return "planeswalker"
    if "instant" in words:
        return "instant"
    if "sorcery" in words:
        return "sorcery"
    if "artifact" in words:
        return "artifact"
    if "enchantment" in words:
        return "enchantment"
    if "battle" in words:
        return "battle"
    return "other"


def build_deck_mainboard_sections(annotated_mainboard: list[dict]) -> list[dict]:
    """Group and sort mainboard cards into sections by card type.

    Within each type section, cards are sorted by converted mana cost followed by name.
    """
    buckets: dict[str, list[dict]] = {k: [] for k, _, _ in CARD_TYPE_CATEGORIES}
    for item in annotated_mainboard:
        card = item.get("card_obj")
        cat = classify_card_type(card)
        buckets[cat].append(item)

    sections = []
    for cat_key, cat_name, icon in CARD_TYPE_CATEGORIES:
        cards = buckets.get(cat_key, [])
        if not cards:
            continue
        cards.sort(key=lambda x: (x.get("cmc", 0.0), x.get("card", "")))
        count = sum(x.get("count", 0) for x in cards)
        sections.append(
            {
                "key": cat_key,
                "name": cat_name,
                "icon": icon,
                "count": count,
                "cards": cards,
            }
        )
    return sections


def get_card_legality_badge(
    card_name: str,
    card_obj: Card | None,
    fmt: str,
    illegal_set: set[str],
) -> dict[str, str] | None:
    """Return badge metadata for a card in a deck if it is illegal in the format."""
    norm_name = normalize_card_name(card_name)
    norm_illegal = {normalize_card_name(c) for c in illegal_set}
    if norm_name not in norm_illegal and card_name not in illegal_set:
        return None

    fmt_slug = fmt.lower().strip()
    status = (
        card_obj.legalities.get(fmt_slug, "not_legal").lower()
        if card_obj and card_obj.legalities
        else "banned"
    )

    if status == "restricted":
        return {
            "label": "Restricted",
            "badge_type": "restricted",
        }
    elif status == "banned":
        return {
            "label": "Banned",
            "badge_type": "banned",
        }
    elif status == "not_legal":
        return {
            "label": "Not Legal",
            "badge_type": "not_legal",
        }
    else:
        # Legal in format, but illegal in deck (e.g. exceeded 4-of limit)
        return {
            "label": "Illegal",
            "badge_type": "banned",
        }
