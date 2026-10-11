"""Standard HTML and JSON views for Modometa."""

from collections import Counter
from datetime import date
from datetime import timedelta

from django.conf import settings
from django.core.cache import cache
from django.core.paginator import Paginator
from django.db.models import Count
from django.db.models import Q
from django.db.models import Sum
from django.http import Http404
from django.http import HttpResponse
from django.http import JsonResponse
from django.shortcuts import get_object_or_404
from django.shortcuts import render

from core.engine.knn import get_global_knn_index
from core.engine.search_index import build_search_index_data
from core.formats import FORMAT_CHOICES
from core.formats import FORMAT_SLUGS
from core.formats import FORMATS
from core.models.card import Card
from core.models.card import generate_card_slug
from core.models.card import get_gatherer_url
from core.models.card import get_scryfall_url
from core.models.deck import Deck
from core.models.tournament import Tournament
from core.views.charts import _get_matrix_color_class
from core.views.charts import build_archetype_heatmap
from core.views.charts import build_format_bump_chart
from core.views.charts import build_player_heatmap
from core.views.charts import build_tournament_archetype_chart
from core.views.charts import build_tournament_list_archetype_chart
from core.views.charts import get_archetype_matrix_data
from core.views.charts import tournament_deck_sort_key
from core.views.utils import build_deck_mainboard_sections
from core.views.utils import get_card_legality_badge
from core.views.utils import get_cards_map
from core.views.utils import get_dataset_start_year
from core.views.utils import get_format_card_stats
from core.views.utils import get_reference_date
from core.views.utils import get_timeframe_cutoff
from core.views.utils import parse_timeframe
from core.views.utils import public_cache


@public_cache(cdn_seconds=3600, browser_seconds=180)
def home(request):
    """Home view: Overview of active formats with top 3-5 archetypes each."""
    days = parse_timeframe(request)
    cutoff, ref_date = get_timeframe_cutoff(days)

    active_slugs = getattr(settings, "ACTIVE_FORMAT_SLUGS", settings.MODOMETA_FORMATS)
    format_metas = []
    for fmt_slug in active_slugs:
        fmt_name = (
            FORMATS[fmt_slug].name if fmt_slug in FORMATS else fmt_slug.capitalize()
        )
        # Tournaments in window
        tourn_qs = Tournament.objects.filter(
            format=fmt_slug, date__gte=cutoff, date__lte=ref_date
        )
        challenge_count = tourn_qs.filter(event_type="challenge").count()
        total_top8_slots = max(1, challenge_count * 8)

        # Query decks in window
        decks_qs = Deck.objects.filter(
            format=fmt_slug,
            tournament__in=tourn_qs,
        )
        total_decks = decks_qs.count()

        # Top archetypes
        arch_counts = (
            decks_qs.values("archetype", "archetype_slug")
            .annotate(
                count=Count("id"),
                top8_count=Count("id", filter=Q(is_top8=True)),
            )
            .order_by("-top8_count", "-count")[:5]
        )

        top_slugs = [a["archetype_slug"] for a in arch_counts]
        arch_colors = (
            decks_qs.filter(archetype_slug__in=top_slugs)
            .values("archetype_slug", "colors", "color_name")
            .annotate(cnt=Count("id"))
            .order_by("archetype_slug", "-cnt")
        )
        colors_map = {}
        for ac in arch_colors:
            slug = ac["archetype_slug"]
            if slug not in colors_map:
                colors_map[slug] = (ac["colors"], ac["color_name"])

        top_archetypes = []
        for a in arch_counts:
            top8_share = round((a["top8_count"] / total_top8_slots) * 100, 1)
            colors, color_name = colors_map.get(a["archetype_slug"], ("", ""))
            top_archetypes.append(
                {
                    "name": a["archetype"],
                    "slug": a["archetype_slug"],
                    "colors": colors,
                    "color_name": color_name,
                    "count": a["count"],
                    "top8_count": a["top8_count"],
                    "top8_share": top8_share,
                }
            )

        format_metas.append(
            {
                "format_slug": fmt_slug,
                "format_name": fmt_name,
                "total_decks": total_decks,
                "challenge_count": challenge_count,
                "total_top8_slots": total_top8_slots,
                "top_archetypes": top_archetypes,
            }
        )

    recent_tournaments = Tournament.objects.filter(format__in=active_slugs).order_by(
        "-date", "-id"
    )[:10]

    return render(
        request,
        "home.html",
        {
            "format_metas": format_metas,
            "recent_tournaments": recent_tournaments,
        },
    )


@public_cache(cdn_seconds=3600, browser_seconds=180)
def format_overview(request, format):
    """Format overview view: Top archetypes, cards, recent leagues and challenges."""
    fmt_slug = format.lower().strip()
    if fmt_slug not in settings.MODOMETA_FORMATS:
        raise Http404("Format not supported")

    days = parse_timeframe(request)
    cutoff, ref_date = get_timeframe_cutoff(days)

    tourns_qs = Tournament.objects.filter(
        format=fmt_slug, date__gte=cutoff, date__lte=ref_date
    )
    challenge_count = tourns_qs.filter(event_type="challenge").count()
    league_count = tourns_qs.filter(event_type="league").count()

    decks_qs = Deck.objects.filter(
        format=fmt_slug,
        tournament__in=tourns_qs,
    )
    total_decks = decks_qs.count()

    league_tourns = tourns_qs.filter(event_type="league")
    league_5_0_count = Deck.objects.filter(
        format=fmt_slug, tournament__in=league_tourns, is_5_0=True
    ).count()
    player_count = decks_qs.values("player_lower").distinct().count()

    chall_tourns = tourns_qs.filter(event_type="challenge")
    raw_chall_decks = Deck.objects.filter(
        format=fmt_slug, tournament__in=chall_tourns
    ).count()
    total_chall_decks = max(1, raw_chall_decks)
    raw_top8_slots = challenge_count * 8
    total_top8_slots = max(1, raw_top8_slots)
    total_5_0s = max(1, league_5_0_count)

    # Aggregate Archetypes
    arch_aggregates = (
        decks_qs.values("archetype", "archetype_slug")
        .annotate(
            total_count=Count("id"),
            top8_count=Count("id", filter=Q(is_top8=True)),
            chall_appearances=Count("id", filter=Q(tournament__event_type="challenge")),
            league_5_0_count=Count(
                "id", filter=Q(tournament__event_type="league", is_5_0=True)
            ),
        )
        .order_by("-top8_count", "-total_count")
    )

    # Calculate previous period for T8 Momentum (e.g. recent 90d vs previous 90d)
    prev_cutoff = cutoff - timedelta(days=days)
    prev_tourns = Tournament.objects.filter(
        format=fmt_slug, date__gte=prev_cutoff, date__lt=cutoff
    )
    prev_t8_qs = (
        Deck.objects.filter(
            format=fmt_slug,
            tournament__in=prev_tourns,
            is_top8=True,
        )
        .values("archetype_slug")
        .annotate(prev_t8_count=Count("id"))
    )
    prev_t8_map = {row["archetype_slug"]: row["prev_t8_count"] for row in prev_t8_qs}

    archetypes = []
    for a in arch_aggregates:
        chall_share = round((a["chall_appearances"] / total_chall_decks) * 100, 1)
        top8_share = round((a["top8_count"] / total_top8_slots) * 100, 1)
        conv_rate = (
            round((a["top8_count"] / a["chall_appearances"]) * 100, 1)
            if a["chall_appearances"] > 0
            else 0.0
        )
        league_share = round((a["league_5_0_count"] / total_5_0s) * 100, 1)
        prev_top8 = prev_t8_map.get(a["archetype_slug"], 0)
        t8_momentum = a["top8_count"] - prev_top8

        archetypes.append(
            {
                "name": a["archetype"],
                "slug": a["archetype_slug"],
                "total_count": a["total_count"],
                "top8_count": a["top8_count"],
                "prev_top8_count": prev_top8,
                "t8_momentum": t8_momentum,
                "chall_appearances": a["chall_appearances"],
                "league_5_0_count": a["league_5_0_count"],
                "challenge_share": chall_share,
                "top8_share": top8_share,
                "conversion_rate": conv_rate,
                "league_share": league_share,
            }
        )

    paginator = Paginator(archetypes, 25)
    page_number = request.GET.get("page")
    page_obj = paginator.get_page(page_number)

    # Fetch colors only for the 25 archetypes on the current page
    page_slugs = [a["slug"] for a in page_obj]
    arch_colors = (
        decks_qs.filter(archetype_slug__in=page_slugs)
        .values("archetype_slug", "colors", "color_name")
        .annotate(cnt=Count("id"))
        .order_by("archetype_slug", "-cnt")
    )
    colors_map = {}
    for ac in arch_colors:
        slug = ac["archetype_slug"]
        if slug not in colors_map:
            colors_map[slug] = (ac["colors"], ac["color_name"])

    for a in page_obj:
        colors, color_name = colors_map.get(a["slug"], ("", ""))
        a["colors"] = colors
        a["color_name"] = color_name

    card_stats = get_format_card_stats(fmt_slug, days=days, active_type="")
    common_cards = card_stats["card_rows"][:10]

    recent_tournaments = Tournament.objects.filter(format=fmt_slug).order_by(
        "-date", "-id"
    )[:6]

    bump_chart = build_format_bump_chart(fmt_slug, ref_date)

    active_slugs = getattr(settings, "ACTIVE_FORMAT_SLUGS", settings.MODOMETA_FORMATS)
    is_supported_format = fmt_slug in active_slugs

    return render(
        request,
        "format_overview.html",
        {
            "format_slug": fmt_slug,
            "format_name": fmt_slug.capitalize(),
            "is_supported_format": is_supported_format,
            "stats": {
                "total_decks": total_decks,
                "challenge_count": challenge_count,
                "league_count": league_count,
                "league_5_0_count": league_5_0_count,
                "player_count": player_count,
                "total_chall_decks": raw_chall_decks,
                "total_top8_slots": raw_top8_slots,
                "total_5_0s": league_5_0_count,
            },
            "archetypes": page_obj.object_list,
            "page_obj": page_obj,
            "common_cards": common_cards,
            "recent_tournaments": recent_tournaments,
            "bump_chart": bump_chart,
        },
    )


@public_cache(cdn_seconds=3600, browser_seconds=180)
def tournament_list(request, format):
    """Tournament list view: Filterable by challenge or league."""
    fmt_slug = format.lower().strip()
    if fmt_slug not in settings.MODOMETA_FORMATS:
        raise Http404("Format not supported")

    active_type = request.GET.get("type", "").lower()
    qs = Tournament.objects.filter(format=fmt_slug)
    if active_type in ("challenge", "league", "preliminary"):
        qs = qs.filter(event_type=active_type)

    qs = qs.order_by("-date", "-id")
    paginator = Paginator(qs, 100)
    page_number = request.GET.get("page")
    page_obj = paginator.get_page(page_number)

    archetype_chart = None
    if active_type != "league":
        archetype_chart = build_tournament_list_archetype_chart(fmt_slug, days=30)

    return render(
        request,
        "tournament_list.html",
        {
            "format_slug": fmt_slug,
            "format_name": fmt_slug.capitalize(),
            "active_type": active_type,
            "tournaments": page_obj.object_list,
            "page_obj": page_obj,
            "archetype_chart": archetype_chart,
        },
    )


# These basically never change unless it's today's leagues
@public_cache(cdn_seconds=3600 * 4, browser_seconds=300)
def tournament_detail(request, format, event):
    """Tournament detail view: Displays standings and decklinks for an event."""
    tournament = get_object_or_404(Tournament, id=event)
    raw_decks = list(Deck.objects.filter(tournament=tournament))
    raw_decks.sort(key=tournament_deck_sort_key)

    # Disambiguate players with multiple decks
    player_counts = Counter()
    decks = []
    for d in raw_decks:
        player_counts[d.player_lower] += 1
        d.deck_index = player_counts[d.player_lower]
        decks.append(d)

    archetype_chart = None
    event_type = getattr(tournament, "event_type", "").lower()
    if event_type in ("challenge", "league"):
        archetype_chart = build_tournament_archetype_chart(
            decks, is_challenge=(event_type == "challenge")
        )

    response = render(
        request,
        "tournament_detail.html",
        {
            "tournament": tournament,
            "decks": decks,
            "archetype_chart": archetype_chart,
        },
    )

    # Tournaments older than 30 days are static historical records; cache for 7 days on CDN
    if tournament.date and (date.today() - tournament.date).days > 30:
        response._cdn_seconds = 86400 * 7

    return response


@public_cache(cdn_seconds=86400, browser_seconds=300)
def player_detail(request, player):
    """Player detail view: Lifetime tournament history across formats."""
    player_clean = player.strip()
    player_lower = player_clean.lower()

    decks_qs = (
        Deck.objects.filter(player_lower=player_lower)
        .select_related("tournament")
        .defer("mainboard", "sideboard", "illegal_cards", "tournament__standings")
        .order_by("-tournament__date")
    )
    all_decks = list(decks_qs)
    total_decks = len(all_decks)
    if total_decks == 0:
        raise Http404(f"No records found for player '{player}'")

    total_top8s = sum(1 for d in all_decks if d.is_top8)
    total_5_0s = sum(
        1 for d in all_decks if d.is_5_0 and d.tournament.event_type == "league"
    )
    chall_appearances = sum(
        1 for d in all_decks if d.tournament.event_type == "challenge"
    )
    conversion_rate = (
        round((total_top8s / chall_appearances) * 100, 1)
        if chall_appearances > 0
        else 0.0
    )

    formats_played = sorted(list({d.format for d in all_decks}))

    chall_match_wins = 0
    chall_match_losses = 0
    for d in all_decks:
        if d.tournament.event_type == "challenge" and d.wins is not None:
            chall_match_wins += d.wins
            chall_match_losses += d.losses or 0

    chall_matches_count = chall_match_wins + chall_match_losses
    chall_win_rate = (
        round((chall_match_wins / chall_matches_count) * 100, 1)
        if chall_matches_count > 0
        else 0.0
    )

    # Add disambiguator index for player decks (chronological numbering per event)
    event_counts = Counter()
    for d in reversed(all_decks):
        event_counts[d.tournament_id] += 1
        d.deck_index = event_counts[d.tournament_id]

    paginator = Paginator(all_decks, 100)
    page_number = request.GET.get("page")
    page_obj = paginator.get_page(page_number)

    ref_date = get_reference_date()
    player_name = all_decks[0].player
    heatmap = build_player_heatmap(
        player_lower=player_lower,
        ref_date=ref_date,
        player_name=player_name,
    )

    return render(
        request,
        "player_detail.html",
        {
            "player_name": player_name,
            "total_decks": total_decks,
            "total_top8s": total_top8s,
            "total_5_0s": total_5_0s,
            "chall_appearances": chall_appearances,
            "conversion_rate": conversion_rate,
            "chall_match_wins": chall_match_wins,
            "chall_matches_count": chall_matches_count,
            "chall_win_rate": chall_win_rate,
            "formats_played": [f.capitalize() for f in formats_played],
            "decks": page_obj.object_list,
            "page_obj": page_obj,
            "heatmap": heatmap,
            "start_year": get_dataset_start_year(),
        },
    )


@public_cache(cdn_seconds=3600, browser_seconds=300)
def card_detail(request, slug):
    """Card detail view: Card stats, printings, external links, and recent tournament decks."""
    card_obj = Card.objects.filter(slug=slug).first()
    if not card_obj:
        raise Http404(f"Card with slug '{slug}' not found")

    # Display top 25 printings from JSONField (populated via sync_scryfall)
    raw_printings = (card_obj.printings or [])[:25]
    printings = []
    for p in raw_printings:
        p_dict = dict(p)
        tcg_id = p.get("tcgplayer_id")
        mtgo_id = p.get("mtgo_id") or p.get("cardhoarder_id")
        mkt_id = p.get("cardmarket_id")

        p_dict["tcgplayer_url"] = (
            f"https://www.tcgplayer.com/product/{tcg_id}" if tcg_id else None
        )
        p_dict["cardhoarder_url"] = (
            f"https://www.cardhoarder.com/cards/{mtgo_id}" if mtgo_id else None
        )
        p_dict["cardmarket_url"] = (
            f"https://www.cardmarket.com/en/Magic/Products?idProduct={mkt_id}"
            if mkt_id
            else None
        )
        printings.append(p_dict)

    # Formats we support where the card is legal
    active_slugs = getattr(settings, "ACTIVE_FORMAT_SLUGS", settings.MODOMETA_FORMATS)
    legal_active = [slug for slug in active_slugs if card_obj.is_legal_in(slug)]

    target_names = {card_obj.name}
    if card_obj.card_faces and len(card_obj.card_faces) > 1:
        composite = " // ".join(f["name"] for f in card_obj.card_faces if f.get("name"))
        if composite:
            target_names.add(composite)

    name_q = Q()
    for t_name in target_names:
        quoted = f'"{t_name}"'
        name_q |= Q(mainboard__icontains=quoted) | Q(sideboard__icontains=quoted)

    format_decks = []
    decks_to_show = 50
    cutoff, _ = get_timeframe_cutoff(365)
    for fmt_slug in legal_active:
        fmt_info = FORMATS.get(fmt_slug)
        decks = list(
            # Tournament has a composite index on (format, date).
            # By filtering on `tournament__format` instead of `format`,
            # that index will be used properly and this will be much faster.
            # The date cutoff here is necessary for performance,
            # otherwise all decks need to be scanned where a card is legal but unplayed
            Deck.objects.filter(tournament__format=fmt_slug)
            .filter(
                name_q,
                tournament__date__gte=cutoff,
            )
            .select_related("tournament")
            .order_by("-tournament__date", "rank", "id")[:decks_to_show]
        )

        for d in decks:
            mb_cnt = sum(
                item.get("count", 1)
                for item in d.mainboard
                if item.get("card") in target_names
            )
            sb_cnt = sum(
                item.get("count", 1)
                for item in d.sideboard
                if item.get("card") in target_names
            )
            parts = []
            if mb_cnt > 0:
                parts.append(f"{mb_cnt}x MB")
            if sb_cnt > 0:
                parts.append(f"{sb_cnt}x SB")
            d.card_copies_display = ", ".join(parts) if parts else ""

        format_decks.append(
            {
                "format_slug": fmt_slug,
                "format_name": fmt_info.name if fmt_info else fmt_slug.capitalize(),
                "decks": decks,
                "count": len(decks),
            }
        )

    # Legality status across supported formats for display
    supported_slugs = getattr(settings, "MODOMETA_FORMATS", FORMAT_SLUGS)
    choices_dict = dict(FORMAT_CHOICES)
    supported_formats = [
        (slug, choices_dict.get(slug, slug.capitalize()))
        for slug in supported_slugs
        if slug in choices_dict
    ]
    legalities_display = []
    for f_slug, f_name in supported_formats:
        status = card_obj.legalities.get(f_slug, "not_legal").lower()
        if f_slug == "vintage" and status in ("legal", "restricted"):
            display_status = "Restricted" if status == "restricted" else "Legal"
            badge_type = "restricted" if status == "restricted" else "legal"
        elif status == "legal":
            display_status = "Legal"
            badge_type = "legal"
        elif status == "banned":
            display_status = "Banned"
            badge_type = "banned"
        elif status == "restricted":
            display_status = "Restricted"
            badge_type = "restricted"
        else:
            display_status = "Not Legal"
            badge_type = "not_legal"

        legalities_display.append(
            {
                "slug": f_slug,
                "name": f_name,
                "status": display_status,
                "badge_type": badge_type,
            }
        )

    return render(
        request,
        "card_detail.html",
        {
            "card": card_obj,
            "printings": printings,
            "format_decks": format_decks,
            "legalities_display": legalities_display,
        },
    )


@public_cache(cdn_seconds=86400 * 7, browser_seconds=300)
def deck_detail(request, player, event, deck_index=1):
    """Deck detail view: Displays mainboard, sideboard, Scryfall previews, and kNN similarities."""
    player_lower = player.strip().lower()
    tournament = get_object_or_404(Tournament, id=event)

    decks = list(
        Deck.objects.filter(tournament=tournament, player_lower=player_lower).order_by(
            "id"
        )
    )
    if not decks:
        raise Http404("Deck not found")

    idx = max(0, min(deck_index - 1, len(decks) - 1))
    deck = decks[idx]

    # Preload image URIs for cards in deck
    card_names = {item["card"] for item in deck.mainboard} | {
        item["card"] for item in deck.sideboard
    }
    cards_map = get_cards_map(card_names)
    illegal_set = set(deck.illegal_cards or [])

    annotated_mainboard = []
    for item in deck.mainboard:
        card = cards_map.get(item["card"])
        slug = card.slug if card and card.slug else generate_card_slug(item["card"])
        badge = get_card_legality_badge(item["card"], card, deck.format, illegal_set)
        annotated_mainboard.append(
            {
                "card": item["card"],
                "slug": slug,
                "card_obj": card,
                "count": item["count"],
                "cmc": card.cmc if card else 0.0,
                "image_uri": card.image_uri if card else None,
                "mana_cost": card.mana_cost if card else None,
                "scryfall_url": card.scryfall_url
                if card
                else get_scryfall_url(name=item["card"]),
                "gatherer_url": card.gatherer_url
                if card
                else get_gatherer_url(name=item["card"]),
                "is_banned": badge is not None,
                "legality_badge": badge,
            }
        )

    annotated_sideboard = []
    for item in deck.sideboard:
        card = cards_map.get(item["card"])
        slug = card.slug if card and card.slug else generate_card_slug(item["card"])
        badge = get_card_legality_badge(item["card"], card, deck.format, illegal_set)
        annotated_sideboard.append(
            {
                "card": item["card"],
                "slug": slug,
                "card_obj": card,
                "count": item["count"],
                "cmc": card.cmc if card else 0.0,
                "image_uri": card.image_uri if card else None,
                "mana_cost": card.mana_cost if card else None,
                "scryfall_url": card.scryfall_url
                if card
                else get_scryfall_url(name=item["card"]),
                "gatherer_url": card.gatherer_url
                if card
                else get_gatherer_url(name=item["card"]),
                "is_banned": badge is not None,
                "legality_badge": badge,
            }
        )
    annotated_sideboard.sort(key=lambda x: (x.get("cmc", 0.0), x.get("card", "")))

    mainboard_sections = build_deck_mainboard_sections(annotated_mainboard)
    deck.mainboard = annotated_mainboard
    deck.sideboard = annotated_sideboard
    deck.mainboard_sections = mainboard_sections

    # Generate text for copy to clipboard
    deck_text = deck.decklist_text

    # Format & Cross-Format kNN neighbors (cached per deck to avoid recomputing 300k matrix ops)
    format_neighbors = []
    cross_format_neighbors = []
    global_index = get_global_knn_index()
    if global_index is not None:
        knn_cache_key = f"deck_knn_v2_{deck.id}"
        cached_neighbors = cache.get(knn_cache_key)
        if cached_neighbors is not None:
            format_neighbors, cross_format_neighbors = cached_neighbors
        else:
            query_vec = global_index.vector_from_decklist(
                deck.mainboard, deck.sideboard
            )
            format_raw_neighbors = global_index.query(
                query_vec,
                reference_date=tournament.date,
                top_k=5,
                exclude_deck_id=deck.id,
                format_filter=deck.format,
            )
            cross_raw_neighbors = global_index.query(
                query_vec,
                reference_date=tournament.date,
                top_k=5,
                exclude_deck_id=deck.id,
                exclude_format=deck.format,
            )

            all_raw_neighbors = list(format_raw_neighbors) + list(cross_raw_neighbors)
            neighbor_ids = [str(n["deck_id"]) for n in all_raw_neighbors]
            deck_map = {}
            if neighbor_ids:
                deck_map = {
                    d["id"]: d
                    for d in Deck.objects.filter(id__in=neighbor_ids).values(
                        "id",
                        "player",
                        "archetype",
                        "colors",
                        "color_name",
                        "tournament_id",
                    )
                }

            for n in format_raw_neighbors:
                parts = str(n["deck_id"]).rsplit("_", 1)
                d_idx = parts[-1] if len(parts) == 2 and parts[-1].isdigit() else 1
                db_deck = deck_map.get(str(n["deck_id"]))
                tourn_id = (
                    db_deck["tournament_id"]
                    if db_deck
                    else str(n["deck_id"]).split("_")[0]
                )
                player = db_deck["player"] if db_deck else n["player"]
                archetype = db_deck["archetype"] if db_deck else n["archetype"]
                colors = (
                    (db_deck["colors"] if db_deck else None) or n.get("colors") or ""
                )
                color_name = (
                    (db_deck["color_name"] if db_deck else None)
                    or n.get("color_name")
                    or ""
                )
                format_neighbors.append(
                    {
                        "player": player,
                        "archetype": archetype,
                        "colors": colors,
                        "color_name": color_name,
                        "similarity_pct": round(n["raw_similarity"] * 100, 1),
                        "date": n["date"],
                        "tournament_id": tourn_id,
                        "deck_index": d_idx,
                    }
                )

            for n in cross_raw_neighbors:
                parts = str(n["deck_id"]).rsplit("_", 1)
                d_idx = parts[-1] if len(parts) == 2 and parts[-1].isdigit() else 1
                db_deck = deck_map.get(str(n["deck_id"]))
                tourn_id = (
                    db_deck["tournament_id"]
                    if db_deck
                    else str(n["deck_id"]).split("_")[0]
                )
                player = db_deck["player"] if db_deck else n["player"]
                archetype = db_deck["archetype"] if db_deck else n["archetype"]
                colors = (
                    (db_deck["colors"] if db_deck else None) or n.get("colors") or ""
                )
                color_name = (
                    (db_deck["color_name"] if db_deck else None)
                    or n.get("color_name")
                    or ""
                )
                cross_format_neighbors.append(
                    {
                        "player": player,
                        "archetype": archetype,
                        "format": n["format"].capitalize(),
                        "colors": colors,
                        "color_name": color_name,
                        "similarity_pct": round(n["raw_similarity"] * 100, 1),
                        "date": n["date"],
                        "tournament_id": tourn_id,
                        "deck_index": d_idx,
                    }
                )
            cache.set(
                knn_cache_key,
                (format_neighbors, cross_format_neighbors),
                timeout=86400,
            )

    return render(
        request,
        "deck_detail.html",
        {
            "deck": deck,
            "tournament": tournament,
            "deck_text": deck_text,
            "mainboard_sections": mainboard_sections,
            "format_neighbors": format_neighbors,
            "cross_format_neighbors": cross_format_neighbors,
        },
    )


@public_cache(cdn_seconds=3600, browser_seconds=180)
def archetype_detail(request, format, archetype):
    """Archetype detail view: 30d/90d stats, core cards, recent event finishes."""
    fmt_slug = format.lower().strip()
    arch_slug = archetype.lower().strip()
    if fmt_slug not in settings.MODOMETA_FORMATS:
        raise Http404("Format not supported")

    days = parse_timeframe(request)
    cutoff, ref_date = get_timeframe_cutoff(days)

    decks_qs = Deck.objects.filter(
        format=fmt_slug,
        archetype_slug=arch_slug,
        tournament__date__gte=cutoff,
        tournament__date__lte=ref_date,
    ).select_related("tournament")

    total_decks = decks_qs.count()
    if total_decks == 0:
        # Check if archetype exists historically
        hist = Deck.objects.filter(format=fmt_slug, archetype_slug=arch_slug).first()
        if not hist:
            raise Http404(
                f"Archetype '{archetype}' not found in {fmt_slug.capitalize()}"
            )
        sample_deck = hist
    else:
        sample_deck = decks_qs.first()

    # Format-wide totals for share calculations (cached per format/timeframe to avoid repeated full-table queries)
    fmt_totals_key = f"format_totals_v1:{fmt_slug}:{days}"
    fmt_totals = cache.get(fmt_totals_key)
    if fmt_totals is None:
        fmt_decks = Deck.objects.filter(
            format=fmt_slug,
            tournament__date__gte=cutoff,
            tournament__date__lte=ref_date,
        )
        total_chall_decks = max(
            1, fmt_decks.filter(tournament__event_type="challenge").count()
        )
        chall_count = Tournament.objects.filter(
            format=fmt_slug,
            event_type="challenge",
            date__gte=cutoff,
            date__lte=ref_date,
        ).count()
        total_top8_slots = max(1, chall_count * 8)
        total_5_0s = max(
            1, fmt_decks.filter(tournament__event_type="league", is_5_0=True).count()
        )
        fmt_totals = (total_chall_decks, total_top8_slots, total_5_0s)
        cache.set(fmt_totals_key, fmt_totals, timeout=3600)
    else:
        total_chall_decks, total_top8_slots, total_5_0s = fmt_totals
    stats_agg = decks_qs.aggregate(
        top8_count=Count("id", filter=Q(is_top8=True)),
        chall_appearances=Count("id", filter=Q(tournament__event_type="challenge")),
        league_5_0_count=Count(
            "id", filter=Q(tournament__event_type="league", is_5_0=True)
        ),
    )
    top8_count = stats_agg["top8_count"]
    chall_appearances = stats_agg["chall_appearances"]
    league_5_0_count = stats_agg["league_5_0_count"]

    top8_share = round((top8_count / total_top8_slots) * 100, 1)
    chall_share = round((chall_appearances / total_chall_decks) * 100, 1)
    conv_rate = (
        round((top8_count / chall_appearances) * 100, 1)
        if chall_appearances > 0
        else 0.0
    )
    league_share = round((league_5_0_count / total_5_0s) * 100, 1)

    # Core cards & archetype colors calculation
    card_counts = Counter()
    card_total_copies = Counter()
    color_counts = Counter()
    for d in decks_qs.values("mainboard", "colors", "color_name").iterator(
        chunk_size=2000
    ):
        d_cards = set()
        for item in d.get("mainboard") or []:
            name = item.get("card", "")
            cnt = item.get("count", 1)
            if name:
                d_cards.add(name)
                card_total_copies[name] += cnt
        for c in d_cards:
            card_counts[c] += 1
        if d.get("colors") or d.get("color_name"):
            color_counts[(d.get("colors", ""), d.get("color_name", ""))] += 1

    most_common_color = color_counts.most_common(1)
    if most_common_color:
        colors, color_name = most_common_color[0][0]
    else:
        colors = sample_deck.colors if sample_deck else ""
        color_name = sample_deck.color_name if sample_deck else ""

    card_stats = []
    for name, cnt in card_counts.items():
        adopt_pct = round((cnt / max(1, total_decks)) * 100, 1)
        avg_cp = round(card_total_copies[name] / max(1, cnt), 1)
        card_stats.append((adopt_pct, avg_cp, cnt, name))

    # Sort primarily by adoption_pct (descending), secondarily by avg_copies (descending), then name (ascending)
    card_stats.sort(key=lambda x: (-x[0], -x[1], -x[2], x[3]))
    top_cards = card_stats[:12]
    cards_map = get_cards_map([c[3] for c in top_cards])

    core_cards = []
    for adopt_pct, avg_cp, _cnt, name in top_cards:
        card = cards_map.get(name)
        slug = card.slug if card and card.slug else generate_card_slug(name)
        core_cards.append(
            {
                "name": name,
                "slug": slug,
                "card": card,
                "mana_cost": card.mana_cost if card and card.mana_cost else "",
                "adoption_pct": adopt_pct,
                "avg_copies": avg_cp,
                "image_uri": card.image_uri if card else None,
                "scryfall_url": card.scryfall_url
                if card
                else get_scryfall_url(name=name),
                "gatherer_url": card.gatherer_url
                if card
                else get_gatherer_url(name=name),
            }
        )

    all_finishes_qs = (
        Deck.objects.filter(
            format=fmt_slug,
            archetype_slug=arch_slug,
        )
        .select_related("tournament")
        .defer("mainboard", "sideboard", "illegal_cards")
        .order_by("-tournament__date", "rank", "id")
    )
    paginator = Paginator(all_finishes_qs, 25)
    page_number = request.GET.get("page")
    page_obj = paginator.get_page(page_number)

    for f in page_obj.object_list:
        parts = f.id.rsplit("_", 1)
        f.deck_index = int(parts[-1]) if len(parts) == 2 and parts[-1].isdigit() else 1

    heatmap = build_archetype_heatmap(fmt_slug, arch_slug, ref_date)

    return render(
        request,
        "archetype_detail.html",
        {
            "archetype_name": sample_deck.archetype,
            "archetype_slug": arch_slug,
            "format_name": fmt_slug.capitalize(),
            "format_slug": fmt_slug,
            "colors": colors,
            "color_name": color_name,
            "total_decks": total_decks,
            "days": days,
            "stats": {
                "total_decks": total_decks,
                "top8_count": top8_count,
                "top8_share": top8_share,
                "total_top8_slots": total_top8_slots,
                "challenge_appearances": chall_appearances,
                "challenge_share": chall_share,
                "total_chall_decks": total_chall_decks,
                "conversion_rate": conv_rate,
                "league_5_0_count": league_5_0_count,
                "league_share": league_share,
                "total_5_0s": total_5_0s,
            },
            "heatmap": heatmap,
            "core_cards": core_cards,
            "finishes": page_obj.object_list,
            "page_obj": page_obj,
        },
    )


@public_cache(cdn_seconds=3600, browser_seconds=180)
def cards_list(request, format):
    """Cards view: Paginated list of most commonly played cards in format."""
    fmt_slug = format.lower().strip()
    if fmt_slug not in settings.MODOMETA_FORMATS:
        raise Http404("Format not supported")

    days = parse_timeframe(request)
    active_type = request.GET.get("type", "").lower().strip()
    if active_type not in ("challenge", "league"):
        active_type = ""

    card_stats = get_format_card_stats(fmt_slug, days=days, active_type=active_type)
    card_rows = card_stats["card_rows"]
    total_decks = card_stats["total_decks"]

    paginator = Paginator(card_rows, 100)
    page_number = request.GET.get("page")
    page_obj = paginator.get_page(page_number)

    return render(
        request,
        "cards.html",
        {
            "format_slug": fmt_slug,
            "format_name": fmt_slug.capitalize(),
            "active_type": active_type,
            "total_decks": total_decks,
            "cards": page_obj.object_list,
            "page_obj": page_obj,
        },
    )


@public_cache(cdn_seconds=3600, browser_seconds=180)
def leaderboard(request, format):
    """Leaderboard view: Top 100 competitors in format over timeframe."""
    fmt_slug = format.lower().strip()
    if fmt_slug not in settings.MODOMETA_FORMATS:
        raise Http404("Format not supported")

    days = parse_timeframe(request)
    cutoff, ref_date = get_timeframe_cutoff(days)

    decks_qs = Deck.objects.filter(
        format=fmt_slug,
        tournament__date__gte=cutoff,
        tournament__date__lte=ref_date,
    )

    player_stats = list(
        decks_qs.values("player", "player_lower")
        .annotate(
            total_finishes=Count("id"),
            top8_count=Count("id", filter=Q(is_top8=True)),
            chall_appearances=Count("id", filter=Q(tournament__event_type="challenge")),
            league_count=Count(
                "id", filter=Q(tournament__event_type="league", is_5_0=True)
            ),
            chall_wins=Sum("wins", filter=Q(tournament__event_type="challenge")),
            chall_losses=Sum("losses", filter=Q(tournament__event_type="challenge")),
            chall_draws=Sum("draws", filter=Q(tournament__event_type="challenge")),
        )
        .order_by("-top8_count", "-league_count", "-total_finishes")[:100]
    )

    # Batch query top archetypes for all top players to eliminate N+1 queries
    top_lowers = [p["player_lower"] for p in player_stats]
    fav_arch_map = {}
    if top_lowers:
        arch_counts_qs = (
            decks_qs.filter(player_lower__in=top_lowers)
            .values("player_lower", "archetype", "archetype_slug")
            .annotate(cnt=Count("id"))
            .order_by("player_lower", "-cnt")
        )
        for row in arch_counts_qs:
            pl = row["player_lower"]
            if pl not in fav_arch_map:
                fav_arch_map[pl] = (row["archetype"], row["archetype_slug"])

    players = []
    for p in player_stats:
        top8s = p["top8_count"]
        challs = p["chall_appearances"]
        conv_rate = round((top8s / challs) * 100, 1) if challs > 0 else 0.0

        chall_wins = p["chall_wins"] or 0
        chall_losses = p["chall_losses"] or 0
        chall_draws = p["chall_draws"] or 0
        total_chall_matches = chall_wins + chall_losses
        win_rate = (
            round((chall_wins / total_chall_matches) * 100, 1)
            if total_chall_matches > 0
            else None
        )
        chall_record = (
            f"{chall_wins}-{chall_losses}-{chall_draws}"
            if chall_draws
            else f"{chall_wins}-{chall_losses}"
        )

        fav_arch_tuple = fav_arch_map.get(p["player_lower"])
        fav_arch_name = fav_arch_tuple[0] if fav_arch_tuple else None
        fav_arch_slug = fav_arch_tuple[1] if fav_arch_tuple else None

        players.append(
            {
                "player": p["player"],
                "top8_count": top8s,
                "challenge_appearances": challs,
                "conversion_rate": conv_rate,
                "match_win_rate": win_rate,
                "challenge_record": chall_record,
                "challenge_match_wins": chall_wins,
                "challenge_match_losses": chall_losses,
                "challenge_match_draws": chall_draws,
                "league_count": p["league_count"],
                "top_archetype": fav_arch_name,
                "top_archetype_slug": fav_arch_slug,
            }
        )

    return render(
        request,
        "leaderboard.html",
        {
            "format_slug": fmt_slug,
            "format_name": fmt_slug.capitalize(),
            "players": players,
        },
    )


@public_cache(cdn_seconds=3600, browser_seconds=180)
def archetype_matrix(request, format):
    """Archetype Matchup Matrix view: Head-to-head win rates for top 20 archetypes."""
    fmt_slug = format.lower().strip()
    if fmt_slug not in settings.MODOMETA_FORMATS:
        raise Http404("Format not supported")

    days = parse_timeframe(request)
    cutoff, ref_date = get_timeframe_cutoff(days)

    matrix_data = get_archetype_matrix_data(fmt_slug, cutoff, ref_date, limit=20)
    sorted_archetype_slugs = matrix_data["sorted_slugs"]
    archetype_names = matrix_data["archetype_names"]
    matrix_stats = matrix_data["matrix_stats"]
    overall_stats = matrix_data["overall_stats"]

    archetypes = [
        {"slug": slug, "name": archetype_names.get(slug, slug)}
        for slug in sorted_archetype_slugs
    ]

    # Construct rows and cells for the template
    rows = []
    for s1 in sorted_archetype_slugs:
        cells = []
        for s2 in sorted_archetype_slugs:
            if s1 == s2:
                mirror_matches = matrix_stats[s1][s2]["total_matches"]
                cells.append(
                    {
                        "is_mirror": True,
                        "has_data": False,
                        "mirror_matches": mirror_matches,
                        "color_class": "bg-zinc-950/60 text-zinc-600",
                        "tooltip": (
                            f"{mirror_matches} mirror matches"
                            if mirror_matches > 0
                            else "Mirror match"
                        ),
                    }
                )
            else:
                st = matrix_stats[s1][s2]
                m_tot = st["total_matches"]
                m_won = st["matches_won"]
                g_won = st["games_won"]
                g_lost = st["games_lost"]
                g_tot = g_won + g_lost

                if m_tot > 0:
                    m_pct = (m_won / m_tot) * 100
                    g_pct = (g_won / g_tot) * 100 if g_tot > 0 else 0.0

                    color_class = _get_matrix_color_class(m_pct)

                    cells.append(
                        {
                            "is_mirror": False,
                            "has_data": True,
                            "match_win_pct": (
                                f"{m_pct:.0f}%"
                                if m_pct.is_integer()
                                else f"{m_pct:.1f}%"
                            ),
                            "game_win_pct": (
                                f"{g_pct:.0f}%"
                                if g_pct.is_integer()
                                else f"{g_pct:.1f}%"
                            ),
                            "matches_won": m_won,
                            "total_matches": m_tot,
                            "games_won": g_won,
                            "total_games": g_tot,
                            "color_class": color_class,
                            "match_tooltip": f"{m_won}/{m_tot}",
                            "game_tooltip": f"{g_won}/{g_tot}",
                            "tooltip": f"{m_won}/{m_tot}",
                        }
                    )
                else:
                    cells.append(
                        {
                            "is_mirror": False,
                            "has_data": False,
                            "color_class": "bg-zinc-900/30 text-zinc-600",
                            "tooltip": "No matches",
                        }
                    )

        # Calculate overall record for this archetype
        ov = overall_stats[s1]
        ov_m_tot = ov["total_matches"]
        ov_m_won = ov["matches_won"]
        ov_g_tot = ov["total_games"]
        ov_g_won = ov["games_won"]

        if ov_m_tot > 0:
            ov_m_pct = (ov_m_won / ov_m_tot) * 100
            ov_g_pct = (ov_g_won / ov_g_tot) * 100 if ov_g_tot > 0 else 0.0
            ov_color = _get_matrix_color_class(ov_m_pct)

            overall_cell = {
                "has_data": True,
                "match_win_pct": (
                    f"{ov_m_pct:.0f}%" if ov_m_pct.is_integer() else f"{ov_m_pct:.1f}%"
                ),
                "game_win_pct": (
                    f"{ov_g_pct:.0f}%" if ov_g_pct.is_integer() else f"{ov_g_pct:.1f}%"
                ),
                "matches_won": ov_m_won,
                "total_matches": ov_m_tot,
                "games_won": ov_g_won,
                "total_games": ov_g_tot,
                "color_class": ov_color,
                "match_tooltip": f"{ov_m_won}/{ov_m_tot}",
                "game_tooltip": f"{ov_g_won}/{ov_g_tot}",
                "tooltip": f"{ov_m_won}/{ov_m_tot}",
            }
        else:
            overall_cell = {
                "has_data": False,
                "color_class": "bg-zinc-900/30 text-zinc-600",
                "tooltip": "No matches",
            }

        rows.append(
            {
                "archetype": archetype_names.get(s1, s1),
                "archetype_slug": s1,
                "cells": cells,
                "overall": overall_cell,
            }
        )

    format_name = (
        FORMATS[fmt_slug].name if fmt_slug in FORMATS else fmt_slug.capitalize()
    )

    return render(
        request,
        "archetype_matrix.html",
        {
            "format_slug": fmt_slug,
            "format_name": format_name,
            "archetypes": archetypes,
            "rows": rows,
            "total_matches": matrix_data["total_matches"],
            "has_ldcp_data": matrix_data.get("has_ldcp_data", False),
            "has_vmc_data": matrix_data.get("has_vmc_data", False),
            "has_community_data": matrix_data.get("has_community_data", False),
            "days": days,
        },
    )


@public_cache(cdn_seconds=3600, browser_seconds=3600)
def faq(request):
    """FAQ view explaining data sources, tournament coverage, and acknowledgments."""
    return render(request, "faq.html")


# It's OK to cache this for a long time since it's read off disk
@public_cache(cdn_seconds=86400, browser_seconds=3600)
def search_index(request):
    """Return cached JSON search index from disk, in-memory cache, or database fallback."""
    is_testing = getattr(settings, "IS_TESTING", False)
    search_index_path = getattr(
        settings,
        "SEARCH_INDEX_PATH",
        settings.BASE_DIR / "data" / "search_index.json",
    )

    if not is_testing and search_index_path.is_file():
        return HttpResponse(
            search_index_path.read_bytes(), content_type="application/json"
        )

    cache_key = "search_index_v1"
    data = cache.get(cache_key)
    if data is None:
        active_slugs = getattr(
            settings, "ACTIVE_FORMAT_SLUGS", settings.MODOMETA_FORMATS
        )
        data = build_search_index_data(formats=active_slugs)
        cache.set(cache_key, data, timeout=86400)

    return JsonResponse(data)
