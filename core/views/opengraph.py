"""OpenGraph dynamic image generation views and SVG helpers."""

import base64
import urllib.parse
from datetime import date
from pathlib import Path

import httpx
import resvg_py
from django.conf import settings
from django.core.cache import cache
from django.db.models import Count
from django.db.models import Q
from django.http import Http404
from django.http import HttpResponse
from django.shortcuts import get_object_or_404
from django.template.loader import render_to_string

from core.engine.knn import get_card_idf_map
from core.formats import FORMAT_CHOICES
from core.formats import FORMAT_SLUGS
from core.formats import FORMATS
from core.models.card import Card
from core.models.card import CardLookup
from core.models.card import normalize_card_name
from core.models.deck import Deck
from core.models.tournament import Tournament
from core.templatetags.mana_tags import COLOR_ORDER
from core.utils import get_user_agent
from core.views.charts import build_archetype_heatmap
from core.views.charts import build_format_bump_chart
from core.views.charts import build_player_heatmap
from core.views.charts import build_tournament_archetype_chart
from core.views.charts import build_tournament_list_archetype_chart
from core.views.charts import get_archetype_matrix_data
from core.views.charts import tournament_deck_sort_key
from core.views.utils import get_cards_map
from core.views.utils import get_reference_date
from core.views.utils import get_timeframe_cutoff
from core.views.utils import parse_timeframe
from core.views.utils import public_cache


def render_og_png(template_name: str, context: dict, request=None) -> HttpResponse:
    """Render an SVG template and rasterize it to PNG via resvg-py."""
    svg_text = render_to_string(template_name, context, request=request)
    font_dirs = [
        str(d)
        for d in [
            Path("/usr/share/fonts"),
            Path("/usr/local/share/fonts"),
            settings.BASE_DIR / "core" / "static" / "fonts",
        ]
        if d.is_dir()
    ]
    png_bytes = resvg_py.svg_to_bytes(
        svg_string=svg_text,
        font_dirs=font_dirs if font_dirs else None,
        sans_serif_family="DejaVu Sans",
        monospace_family="DejaVu Sans Mono",
        font_family="DejaVu Sans",
    )
    return HttpResponse(png_bytes, content_type="image/png")


@public_cache(cdn_seconds=86400, browser_seconds=3600)
def default_og_image(request):
    """Serve default Open Graph PNG image with site branding and features."""
    return render_og_png(
        "og/default_og.svg",
        {
            "site_name": getattr(settings, "SITE_NAME", "MODOMeta"),
        },
        request=request,
    )


@public_cache(cdn_seconds=86400, browser_seconds=3600)
def format_og_image(request, format):
    """Serve dynamic Open Graph PNG image for a format metagame, including bump chart."""
    fmt_slug = format.lower().strip()
    if fmt_slug not in settings.MODOMETA_FORMATS:
        raise Http404(f"Format '{format}' not found.")

    cutoff, ref_date = get_timeframe_cutoff(365)
    bump_chart = build_format_bump_chart(fmt_slug, ref_date, step_y=23)
    total_decks = Deck.objects.filter(
        format=fmt_slug,
        tournament__date__gte=cutoff,
        tournament__date__lte=ref_date,
    ).count()

    return render_og_png(
        "og/format_og.svg",
        {
            "format_slug": fmt_slug,
            "format_name": fmt_slug.capitalize(),
            "bump_chart": bump_chart,
            "total_decks": f"{total_decks:,}",
        },
        request=request,
    )


def _get_og_matrix_cell_style(
    pct: float | None, is_mirror: bool = False, has_data: bool = True
) -> dict:
    """Return SVG fill, stroke, and text colors for matrix OG image cells."""
    if is_mirror:
        return {
            "bg_fill": "#18181b",
            "bg_opacity": "0.5",
            "border_stroke": "#27272a",
            "border_opacity": "0.4",
            "text_fill": "#52525b",
            "subtext_fill": "#3f3f46",
        }
    if not has_data or pct is None:
        return {
            "bg_fill": "#18181b",
            "bg_opacity": "0.3",
            "border_stroke": "#27272a",
            "border_opacity": "0.2",
            "text_fill": "#3f3f46",
            "subtext_fill": "#27272a",
        }
    if pct > 60.0:
        return {
            "bg_fill": "#064e3b",
            "bg_opacity": "0.75",
            "border_stroke": "#10b981",
            "border_opacity": "0.6",
            "text_fill": "#6ee7b7",
            "subtext_fill": "#a7f3d0",
        }
    elif pct > 55.0:
        return {
            "bg_fill": "#064e3b",
            "bg_opacity": "0.4",
            "border_stroke": "#10b981",
            "border_opacity": "0.4",
            "text_fill": "#34d399",
            "subtext_fill": "#6ee7b7",
        }
    elif pct < 40.0:
        return {
            "bg_fill": "#450a0a",
            "bg_opacity": "0.75",
            "border_stroke": "#ef4444",
            "border_opacity": "0.6",
            "text_fill": "#fca5a5",
            "subtext_fill": "#fecaca",
        }
    elif pct < 45.0:
        return {
            "bg_fill": "#450a0a",
            "bg_opacity": "0.4",
            "border_stroke": "#ef4444",
            "border_opacity": "0.4",
            "text_fill": "#f87171",
            "subtext_fill": "#fca5a5",
        }
    else:
        return {
            "bg_fill": "#27272a",
            "bg_opacity": "0.7",
            "border_stroke": "#3f3f46",
            "border_opacity": "0.5",
            "text_fill": "#e4e4e7",
            "subtext_fill": "#a1a1aa",
        }


def build_matrix_og_data(
    format_slug: str, cutoff_date: date, ref_date: date, max_archetypes: int = 8
) -> dict:
    """Build pre-calculated layout and cell data for the archetype matrix OG image."""
    matrix_raw = get_archetype_matrix_data(
        format_slug, cutoff_date, ref_date, limit=max_archetypes
    )
    if not matrix_raw["has_data"]:
        return {"has_data": False, "total_matches": 0}

    sorted_slugs = matrix_raw["sorted_slugs"]
    archetype_names = matrix_raw["archetype_names"]
    matrix_stats = matrix_raw["matrix_stats"]
    n = len(sorted_slugs)

    # Geometry calculations for SVG
    # Card container width: 1080px (x=60 to 1140), height: 365px (y=200 to 565)
    cols = n
    table_x = 80
    row_label_width = 160
    columns_start_x = table_x + row_label_width + 12
    available_width = 1120 - columns_start_x
    gap_x = 6
    cell_width = min(120, int((available_width - (cols - 1) * gap_x) / cols))

    available_height = 310
    gap_y = 4
    cell_height = min(40, int((available_height - (n - 1) * gap_y) / n))

    def _truncate(name: str, max_chars: int) -> str:
        return (name[: max_chars - 1] + "…") if len(name) > max_chars else name

    col_headers = []
    for j, s in enumerate(sorted_slugs):
        cx = columns_start_x + j * (cell_width + gap_x)
        full_name = archetype_names.get(s, s)
        col_headers.append(
            {
                "x": cx,
                "center_x": cx + cell_width // 2,
                "width": cell_width,
                "name": full_name,
                "short_name": _truncate(full_name, 13),
            }
        )

    rows_data = []
    row_start_y = 242
    for i, s1 in enumerate(sorted_slugs):
        ry = row_start_y + i * (cell_height + gap_y)
        full_name = archetype_names.get(s1, s1)
        row_label_y = ry + cell_height // 2 + 4

        cells = []
        for j, s2 in enumerate(sorted_slugs):
            cx = columns_start_x + j * (cell_width + gap_x)
            is_mirror = s1 == s2
            st = matrix_stats[s1][s2]
            m_tot = st["total_matches"]
            m_won = st["matches_won"]
            has_data = not is_mirror and m_tot > 0
            pct = (m_won / m_tot * 100) if has_data else None

            style = _get_og_matrix_cell_style(
                pct, is_mirror=is_mirror, has_data=has_data
            )
            pct_str = f"{pct:.0f}%" if pct is not None else ""
            rec_str = f"{m_won}/{m_tot}" if has_data else ""

            cells.append(
                {
                    "x": cx,
                    "y": ry,
                    "width": cell_width,
                    "height": cell_height,
                    "center_x": cx + cell_width // 2,
                    "top_text_y": ry + 15,
                    "bottom_text_y": ry + 28,
                    "center_y": ry + cell_height // 2 + 4,
                    "is_mirror": is_mirror,
                    "has_data": has_data,
                    "match_win_pct": pct_str,
                    "record": rec_str,
                    **style,
                }
            )

        rows_data.append(
            {
                "slug": s1,
                "name": full_name,
                "short_name": _truncate(full_name, 18),
                "label_y": row_label_y,
                "cells": cells,
            }
        )

    return {
        "has_data": True,
        "col_headers": col_headers,
        "rows": rows_data,
        "total_matches": matrix_raw["total_matches"],
    }


@public_cache(cdn_seconds=86400, browser_seconds=3600)
def format_matrix_og_image(request, format):
    """Serve dynamic Open Graph PNG image for a format archetype matchup matrix."""
    fmt_slug = format.lower().strip()
    if fmt_slug not in settings.MODOMETA_FORMATS:
        raise Http404(f"Format '{format}' not found.")

    days = parse_timeframe(request, default=90)
    cutoff, ref_date = get_timeframe_cutoff(days)

    matrix_data = build_matrix_og_data(fmt_slug, cutoff, ref_date, max_archetypes=8)

    format_name = (
        FORMATS[fmt_slug].name if fmt_slug in FORMATS else fmt_slug.capitalize()
    )

    return render_og_png(
        "og/matrix_og.svg",
        {
            "format_slug": fmt_slug,
            "format_name": format_name,
            "matrix": matrix_data,
            "total_matches": f"{matrix_data.get('total_matches', 0):,}",
            "days": days,
        },
        request=request,
    )


def build_mana_pill(colors_str: str | None) -> dict | None:
    """Build SVG layout metadata for rendering MTG mana symbols in a badge pill."""
    unique_chars = {
        c.upper() for c in str(colors_str or "") if c.upper() in COLOR_ORDER
    }
    if not unique_chars:
        return None
    sorted_chars = sorted(unique_chars, key=lambda c: COLOR_ORDER.get(c, 99))
    mana_symbols = [c.lower() for c in sorted_chars]
    pill_width = 20 + len(mana_symbols) * 28 + (len(mana_symbols) - 1) * 6
    pill_x = 1140 - pill_width
    return {
        "x": pill_x,
        "y": 115,
        "width": pill_width,
        "height": 40,
        "symbols": [
            {"id": f"mana-{sym}", "x": 10 + idx * 34, "y": 6}
            for idx, sym in enumerate(mana_symbols)
        ],
    }


@public_cache(cdn_seconds=86400, browser_seconds=3600)
def archetype_og_image(request, format, archetype):
    """Serve dynamic Open Graph PNG image for an archetype, including activity heatmap."""
    fmt_slug = format.lower().strip()
    arch_slug = archetype.lower().strip()
    if fmt_slug not in settings.MODOMETA_FORMATS:
        raise Http404(f"Format '{format}' not found.")

    decks_qs = Deck.objects.filter(format=fmt_slug, archetype_slug=arch_slug)
    sample_deck = decks_qs.first()
    if not sample_deck:
        raise Http404(f"Archetype '{archetype}' not found in {fmt_slug.capitalize()}.")

    ref_date = get_reference_date()
    cutoff, _ = get_timeframe_cutoff(90)
    recent_qs = decks_qs.filter(
        tournament__date__gte=cutoff, tournament__date__lte=ref_date
    )
    active_qs = recent_qs if recent_qs.exists() else decks_qs

    stats_agg = active_qs.aggregate(
        top8_count=Count("id", filter=Q(is_top8=True)),
        chall_appearances=Count("id", filter=Q(tournament__event_type="challenge")),
        league_5_0_count=Count(
            "id", filter=Q(tournament__event_type="league", is_5_0=True)
        ),
    )
    top8_count = stats_agg["top8_count"]
    chall_appearances = stats_agg["chall_appearances"]
    league_5_0_count = stats_agg["league_5_0_count"]

    conv_rate = (
        round((top8_count / chall_appearances) * 100, 1)
        if chall_appearances > 0
        else 0.0
    )

    fmt_totals_key = f"format_totals_v1:{fmt_slug}:90"
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

    league_share = (
        round((league_5_0_count / total_5_0s) * 100, 1) if total_5_0s > 0 else 0.0
    )

    heatmap = build_archetype_heatmap(fmt_slug, arch_slug, ref_date)

    deck_colors = sample_deck.colors
    if not deck_colors:
        non_empty = decks_qs.exclude(colors="").values_list("colors", flat=True).first()
        if non_empty:
            deck_colors = non_empty
    mana_pill = build_mana_pill(deck_colors)

    return render_og_png(
        "og/archetype_og.svg",
        {
            "format_slug": fmt_slug,
            "format_name": fmt_slug.capitalize(),
            "archetype_slug": arch_slug,
            "archetype_name": sample_deck.archetype,
            "mana_pill": mana_pill,
            "stats": {
                "league_share": league_share,
                "league_5_0_count": league_5_0_count,
                "challenge_appearances": chall_appearances,
                "top8_count": top8_count,
                "conversion_rate": conv_rate,
            },
            "heatmap": heatmap,
        },
        request=request,
    )


def _format_og_chart_row(r: dict, idx: int) -> dict:
    """Format a single archetype row with SVG coordinate offsets for Challenge OG images."""
    name = r["name"]
    name_display = name[:18] + "…" if len(name) > 19 else name
    units = []
    for u_idx, u in enumerate(r["units"][:8]):
        units.append(
            {
                "x": 160 + u_idx * 19,
                "text_x": 160 + u_idx * 19 + 7.5,
                "rank": u.get("rank"),
                "is_top8": u["is_top8"],
            }
        )
    return {
        "y_offset": idx * 29,
        "name": name_display,
        "units": units,
        "total_count": r["total_count"],
        "top8_count": r["top8_count"],
        "share_pct": r["share_pct"],
    }


@public_cache(cdn_seconds=86400, browser_seconds=3600)
def tournament_og_image(request, format, event):
    """Serve dynamic Open Graph PNG image for a Challenge or League tournament."""
    fmt_slug = format.lower().strip()
    if fmt_slug not in settings.MODOMETA_FORMATS:
        raise Http404(f"Format '{format}' not found.")

    tournament = get_object_or_404(Tournament, id=event, format=fmt_slug)
    event_type = getattr(tournament, "event_type", "").lower()
    if event_type not in ("challenge", "league"):
        raise Http404(
            "OG images are only generated for Challenge and League tournaments."
        )

    is_challenge = event_type == "challenge"
    raw_decks = list(Deck.objects.filter(tournament=tournament))
    raw_decks.sort(key=tournament_deck_sort_key)
    if not raw_decks:
        raise Http404("No deck data recorded for this tournament.")

    chart = build_tournament_archetype_chart(raw_decks, is_challenge=is_challenge)
    if not chart or not chart.get("rows"):
        raise Http404("Could not generate chart data for this tournament.")

    rows = chart["rows"]
    if len(rows) > 20:
        displayed = list(rows[:19])
        remaining = rows[19:]
        rem_total = sum(r["total_count"] for r in remaining)
        rem_top8 = sum(r["top8_count"] for r in remaining)
        rem_share = round(sum(r["share_pct"] for r in remaining), 1)
        rem_units = []
        for r in remaining:
            rem_units.extend(r["units"])
        displayed.append(
            {
                "name": f"Other ({len(remaining)})",
                "total_count": rem_total,
                "top8_count": rem_top8,
                "share_pct": rem_share,
                "units": rem_units[:8],
            }
        )
    else:
        displayed = rows

    half = (len(displayed) + 1) // 2
    col1 = [_format_og_chart_row(r, i) for i, r in enumerate(displayed[:half])]
    col2 = (
        [_format_og_chart_row(r, i) for i, r in enumerate(displayed[half:])]
        if len(displayed) > half
        else []
    )

    podium = []
    if is_challenge:
        podium_ranks = ["1st", "2nd", "3rd"]
        box_w = 348
        gap = 18
        for i in range(min(3, len(raw_decks))):
            d = raw_decks[i]
            p_name = d.player[:14] + "…" if len(d.player) > 15 else d.player
            a_name = d.archetype[:17] + "…" if len(d.archetype) > 18 else d.archetype
            podium.append(
                {
                    "x": 60 + i * (box_w + gap),
                    "width": box_w,
                    "text_right_x": box_w - 10,
                    "label": f"{podium_ranks[i]}: {p_name}",
                    "archetype": a_name,
                    "is_first": (i == 0),
                }
            )

    return render_og_png(
        "og/tournament_og.svg",
        {
            "tournament": tournament,
            "format_name": fmt_slug.capitalize(),
            "chart": chart,
            "podium": podium,
            "col1": col1,
            "col2": col2,
        },
        request=request,
    )


@public_cache(cdn_seconds=86400, browser_seconds=3600)
def tournament_list_og_image(request, format):
    """Serve dynamic Open Graph PNG image for the tournament list view (30-day Challenge Top 8 breakdown)."""
    fmt_slug = format.lower().strip()
    if fmt_slug not in settings.MODOMETA_FORMATS:
        raise Http404(f"Format '{format}' not found.")

    chart = build_tournament_list_archetype_chart(fmt_slug, days=30)
    if not chart or not chart.get("rows"):
        raise Http404(
            f"No Challenge Top 8 data found in {fmt_slug} over the last 30 days."
        )

    format_name = (
        FORMATS[fmt_slug].name if fmt_slug in FORMATS else fmt_slug.capitalize()
    )

    max_display_rows = 10
    og_rows = []
    for i, r in enumerate(chart["rows"][:max_display_rows]):
        raw_units = r["units"]
        overflow_count = max(0, len(raw_units) - 28)
        visible_units = raw_units[:28] if overflow_count > 0 else raw_units

        units_data = []
        for u_idx, u in enumerate(visible_units):
            u_x = 180 + u_idx * 18
            units_data.append(
                {
                    "x": u_x,
                    "text_x": u_x + 7,
                    "rank": u["rank"],
                    "is_win": u["is_win"],
                }
            )

        overflow = None
        if overflow_count > 0:
            overflow = {
                "x": 180 + len(visible_units) * 18,
                "text_x": 180 + len(visible_units) * 18 + 14,
                "label": f"+{overflow_count}",
            }

        name = r["name"]
        if len(name) > 19:
            name = name[:18] + "…"

        og_rows.append(
            {
                "name": name,
                "y_offset": i * 26,
                "units": units_data,
                "overflow": overflow,
                "wins_count": r["wins_count"],
                "total_count": r["total_count"],
                "share_pct": r["share_pct"],
            }
        )

    remaining_archetypes = max(0, chart["total_archetypes"] - len(og_rows))
    remaining_decks = sum(r["total_count"] for r in chart["rows"][len(og_rows) :])
    top_archetype = chart["rows"][0] if chart["rows"] else None

    return render_og_png(
        "og/tournament_list_og.svg",
        {
            "format_name": format_name,
            "format_slug": fmt_slug,
            "chart": chart,
            "rows": og_rows,
            "top_archetype": top_archetype,
            "remaining_archetypes": remaining_archetypes,
            "remaining_decks": remaining_decks,
        },
        request=request,
    )


@public_cache(cdn_seconds=86400, browser_seconds=3600)
def player_og_image(request, player):
    """Serve dynamic Open Graph PNG image for a player, including activity heatmap."""
    player_clean = player.strip()
    player_lower = player_clean.lower()

    decks_qs = (
        Deck.objects.filter(player_lower=player_lower)
        .select_related("tournament")
        .defer("mainboard", "sideboard", "illegal_cards", "tournament__standings")
    )
    all_decks = list(decks_qs)
    total_decks = len(all_decks)
    if total_decks == 0:
        raise Http404(f"No records found for player '{player}'.")

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

    formats_played = sorted(set(d.format.capitalize() for d in all_decks if d.format))
    player_name = all_decks[0].player

    ref_date = get_reference_date()
    heatmap = build_player_heatmap(player_lower, ref_date, player_name=player_name)

    return render_og_png(
        "og/player_og.svg",
        {
            "player_name": player_name,
            "total_decks": total_decks,
            "total_5_0s": total_5_0s,
            "total_top8s": total_top8s,
            "chall_appearances": chall_appearances,
            "conversion_rate": conversion_rate,
            "chall_match_wins": chall_match_wins,
            "chall_matches_count": chall_matches_count,
            "chall_win_rate": chall_win_rate,
            "formats_played": formats_played,
            "heatmap": heatmap,
        },
        request=request,
    )


@public_cache(cdn_seconds=86400, browser_seconds=3600)
def card_og_image(request, card):
    """Serve dynamic Open Graph PNG image for a card, embedding the Scryfall artwork."""
    card_query = urllib.parse.unquote(card).strip()
    norm = normalize_card_name(card_query)

    card_obj = (
        Card.objects.filter(name=card_query).defer("printings").first()
        or Card.objects.filter(name__iexact=card_query).defer("printings").first()
        or Card.objects.filter(normalized_name=norm).defer("printings").first()
    )

    if not card_obj and norm:
        lookup = (
            CardLookup.objects.filter(lookup_name=norm)
            .select_related("card")
            .defer("card__printings")
            .first()
        )
        if lookup:
            card_obj = lookup.card

    if not card_obj and "-" in card_query:
        spaced_norm = normalize_card_name(card_query.replace("-", " "))
        card_obj = (
            Card.objects.filter(normalized_name=spaced_norm).defer("printings").first()
        )
        if not card_obj:
            lookup = (
                CardLookup.objects.filter(lookup_name=spaced_norm)
                .select_related("card")
                .defer("card__printings")
                .first()
            )
            if lookup:
                card_obj = lookup.card

    if not card_obj:
        raise Http404(f"Card '{card_query}' not found.")

    image_data_uri = _fetch_card_image_data_uri(card_obj)

    supported_slugs = getattr(settings, "MODOMETA_FORMATS", FORMAT_SLUGS)
    choices_dict = dict(FORMAT_CHOICES)
    supported_formats = [
        (slug, choices_dict.get(slug, slug.capitalize()))
        for slug in supported_slugs
        if slug in choices_dict
    ]
    legal_formats = [
        f_name for f_slug, f_name in supported_formats if card_obj.is_legal_in(f_slug)
    ]
    legal_slugs = [
        f_slug for f_slug, _ in supported_formats if card_obj.is_legal_in(f_slug)
    ]

    target_names = {card_obj.name}
    if card_obj.card_faces and len(card_obj.card_faces) > 1:
        composite = " // ".join(f["name"] for f in card_obj.card_faces if f.get("name"))
        if composite:
            target_names.add(composite)

    name_q = Q()
    for t_name in target_names:
        quoted = f'"{t_name}"'
        name_q |= Q(mainboard__icontains=quoted) | Q(sideboard__icontains=quoted)

    format_counts = {}
    if legal_slugs:
        cutoff, _ = get_timeframe_cutoff(90)
        tourns_qs = Tournament.objects.filter(format__in=legal_slugs, date__gte=cutoff)
        format_counts_qs = (
            Deck.objects.filter(
                format__in=legal_slugs,
                tournament__in=tourns_qs,
            )
            .filter(name_q)
            .order_by()
            .values("format")
            .annotate(cnt=Count("id"))
        )
        format_counts = {item["format"]: item["cnt"] for item in format_counts_qs}

    total_decks = sum(format_counts.values())

    total_fmts = len(supported_formats)
    row0_count = min(4, total_fmts)
    row1_count = total_fmts - row0_count

    legalities_display = []
    for idx, (f_slug, f_name) in enumerate(supported_formats):
        if idx < row0_count:
            col = idx
            row0_width = row0_count * 154 + max(0, row0_count - 1) * 18
            start_x = (700 - row0_width) // 2
            x = start_x + col * 172
            y = 14
        else:
            col = idx - row0_count
            row1_width = row1_count * 154 + max(0, row1_count - 1) * 18
            start_x = (700 - row1_width) // 2
            x = start_x + col * 172
            y = 100
        status = card_obj.legalities.get(f_slug, "not_legal").lower()
        if f_slug == "vintage" and status in ("legal", "restricted"):
            display_status = "Restricted" if status == "restricted" else "Legal"
            badge_color = "#34d399" if status != "restricted" else "#fbbf24"
            badge_bg = "#064e3b" if status != "restricted" else "#78350f"
        elif status == "legal":
            display_status = "Legal"
            badge_color = "#34d399"
            badge_bg = "#064e3b"
        elif status == "restricted":
            display_status = "Restricted"
            badge_color = "#fbbf24"
            badge_bg = "#78350f"
        elif status == "banned":
            display_status = "Banned"
            badge_color = "#f87171"
            badge_bg = "#881337"
        else:
            display_status = "Not Legal"
            badge_color = "#71717a"
            badge_bg = "#27272a"

        legalities_display.append(
            {
                "name": f_name,
                "status": display_status,
                "badge_color": badge_color,
                "badge_bg": badge_bg,
                "x": x,
                "y": y,
            }
        )

    colors_str = "".join(card_obj.colors or card_obj.color_identity or [])
    mana_pill = build_mana_pill(colors_str)
    if mana_pill:
        mana_pill["y"] = 135

    if format_counts:
        top_slug = max(
            format_counts,
            key=lambda f: (
                format_counts[f],
                -supported_slugs.index(f) if f in supported_slugs else -999,
            ),
        )
        top_format_display = choices_dict.get(top_slug, top_slug.capitalize())
    else:
        top_format_display = "-"

    return render_og_png(
        "og/card_og.svg",
        {
            "card": card_obj,
            "image_data_uri": image_data_uri,
            "total_decks": f"{total_decks:,}",
            "legal_format_count": len(legal_formats),
            "top_format_display": top_format_display,
            "legalities_display": legalities_display,
            "mana_pill": mana_pill,
        },
        request=request,
    )


def _fetch_card_image_data_uri(card_obj: Card | None) -> str | None:
    """Fetch and cache a Scryfall card image as a base64 data URI for SVG embedding."""
    if not card_obj:
        return None
    cache_key = f"card_og_img_data_uri:{card_obj.id}"
    image_data_uri = cache.get(cache_key)
    if not image_data_uri and card_obj.image_uri:
        try:
            headers = {"User-Agent": get_user_agent()}
            with httpx.Client(timeout=5.0, follow_redirects=True) as client:
                resp = client.get(card_obj.image_uri, headers=headers)
                if resp.status_code == 200:
                    content_type = resp.headers.get("content-type", "image/jpeg")
                    b64_img = base64.b64encode(resp.content).decode("ascii")
                    image_data_uri = f"data:{content_type};base64,{b64_img}"
                    cache.set(cache_key, image_data_uri, timeout=604800)
        except Exception:
            image_data_uri = None
    return image_data_uri


def _extract_card_mana_symbols(card_obj: Card | None) -> list[str]:
    """Return ordered mana symbol IDs (e.g. ['mana-u', 'mana-b']) for a non-land card."""
    if not card_obj or card_obj.is_land:
        return []
    raw_colors = card_obj.colors or card_obj.color_identity or []
    unique_chars = {c.upper() for c in raw_colors if c.upper() in COLOR_ORDER}
    if not unique_chars:
        return ["mana-c"]
    sorted_chars = sorted(unique_chars, key=lambda c: COLOR_ORDER.get(c, 99))
    return [f"mana-{c.lower()}" for c in sorted_chars]


@public_cache(cdn_seconds=86400 * 7, browser_seconds=3600)
def deck_og_image(request, player, event, deck_index=1):
    """Serve dynamic Open Graph PNG image for a player's tournament decklist."""
    player_lower = player.strip().lower()
    tournament = get_object_or_404(Tournament, id=event)

    decks = list(
        Deck.objects.filter(tournament=tournament, player_lower=player_lower).order_by(
            "id"
        )
    )
    if not decks:
        raise Http404("Deck not found.")

    idx = max(0, min(deck_index - 1, len(decks) - 1))
    deck = decks[idx]

    mb_counts: dict[str, int] = {}
    for item in deck.mainboard or []:
        c_name = (item.get("card") or "").strip()
        c_count = int(item.get("count", 1))
        if c_name and c_count > 0:
            mb_counts[c_name] = mb_counts.get(c_name, 0) + c_count

    cards_map = get_cards_map(set(mb_counts.keys()))
    idf_map = get_card_idf_map()

    ranked_mainboard = []
    curve_buckets = [0, 0, 0, 0, 0, 0]
    total_nonland_cmc = 0.0
    nonland_count = 0
    land_count = 0

    for c_name, count in mb_counts.items():
        card_obj = cards_map.get(c_name)
        norm = normalize_card_name(c_name)
        idf = idf_map.get(norm, 1.0)

        # Weight by linear copy count capped at 4 so 3-4 ofs beat 1-ofs,
        # while high-IDF 3-ofs or lands can still beat generic staples.
        effective_copies = min(max(count, 1), 4)
        tfidf_weight = effective_copies * idf

        is_land = bool(card_obj and card_obj.is_land)
        if is_land:
            land_count += count
        else:
            cmc_val = float(card_obj.cmc) if card_obj and card_obj.cmc else 0.0
            nonland_count += count
            total_nonland_cmc += cmc_val * count
            bucket_idx = min(5, max(0, int(round(cmc_val))))
            curve_buckets[bucket_idx] += count

        ranked_mainboard.append(
            {
                "name": c_name,
                "count": count,
                "weight": tfidf_weight,
                "idf": idf,
                "card_obj": card_obj,
                "is_land": is_land,
            }
        )

    ranked_mainboard.sort(key=lambda x: (-x["weight"], -x["count"], x["name"]))

    spotlight_entry = ranked_mainboard[0] if ranked_mainboard else None
    spotlight_card_obj = spotlight_entry["card_obj"] if spotlight_entry else None
    spotlight_name = spotlight_entry["name"] if spotlight_entry else deck.archetype
    spotlight_count = spotlight_entry["count"] if spotlight_entry else 0
    image_data_uri = _fetch_card_image_data_uri(spotlight_card_obj)

    avg_cmc = (
        f"{total_nonland_cmc / nonland_count:.2f}" if nonland_count > 0 else "0.00"
    )

    # Build mana curve bar geometry (6 bars inside 220x72 card)
    max_bucket = max(curve_buckets) if any(curve_buckets) else 1
    curve_labels = ["0", "1", "2", "3", "4", "5+"]
    curve_bars = []
    for b_idx, b_cnt in enumerate(curve_buckets):
        bx = 22 + b_idx * 31
        bar_h = int(round((b_cnt / max_bucket) * 24)) if b_cnt > 0 else 2
        by = 54 - bar_h
        curve_bars.append(
            {
                "x": bx,
                "y": by,
                "width": 20,
                "height": bar_h,
                "center_x": bx + 10,
                "count": b_cnt,
                "label": curve_labels[b_idx],
                "fill": "#10b981" if b_cnt > 0 else "#27272a",
            }
        )

    # Format 2 columns of up to 7 cards each (14 total)
    max_rows_per_col = 7
    max_display = max_rows_per_col * 2
    overflow_unique = max(0, len(ranked_mainboard) - max_display)
    visible_entries = (
        ranked_mainboard[: max_display - 1]
        if overflow_unique > 0
        else ranked_mainboard[:max_display]
    )

    def _format_card_row(entry: dict, row_idx: int, is_first: bool) -> dict:
        c_name = entry["name"]
        short_name = c_name[:27] + "…" if len(c_name) > 28 else c_name
        syms = _extract_card_mana_symbols(entry["card_obj"])
        sym_items = []
        total_syms = len(syms)
        for s_idx, sym_id in enumerate(syms):
            # Right-align symbols ending at x=326
            sx = 326 - (total_syms - s_idx) * 18
            sym_items.append({"id": sym_id, "x": sx})
        return {
            "y": 16 + row_idx * 29,
            "count": entry["count"],
            "name": short_name,
            "is_spotlight": is_first,
            "is_land": entry["is_land"],
            "symbols": sym_items,
        }

    col1_cards = [
        _format_card_row(e, i, i == 0)
        for i, e in enumerate(visible_entries[:max_rows_per_col])
    ]
    col2_cards = [
        _format_card_row(e, i, False)
        for i, e in enumerate(visible_entries[max_rows_per_col:])
    ]
    overflow_row = None
    if overflow_unique > 0:
        overflow_all_count = sum(
            e["count"] for e in ranked_mainboard[max_display - 1 :]
        )
        overflow_row = {
            "y": 16 + len(col2_cards) * 29,
            "label": f"+{overflow_unique} more unique cards ({overflow_all_count} copies)",
        }

    fmt_slug = deck.format.lower().strip()
    format_name = (
        FORMATS[fmt_slug].name if fmt_slug in FORMATS else fmt_slug.capitalize()
    )

    mana_pill = build_mana_pill(deck.colors)
    if mana_pill:
        mana_pill["y"] = 122

    # Result styling
    record_str = deck.record_display
    if deck.is_top8:
        result_label = "CHALLENGE TOP 8"
        result_value = deck.result or (f"#{deck.rank}" if deck.rank else "Top 8")
        result_sub = f"({record_str})" if record_str else "Top 8 Finish"
        result_color = "#10b981"
    elif deck.is_5_0:
        result_label = "LEAGUE RESULT"
        result_value = "5-0 Trophy"
        result_sub = "Undefeated"
        result_color = "#38bdf8"
    else:
        result_label = "EVENT RESULT"
        result_value = deck.result or (f"#{deck.rank}" if deck.rank else "Entry")
        result_sub = f"({record_str})" if record_str else ""
        result_color = "#ffffff"

    archetype_display = (
        deck.archetype[:24] + "…" if len(deck.archetype) > 25 else deck.archetype
    )
    tourn_name = tournament.name
    if len(tourn_name) > 36:
        tourn_name = tourn_name[:35] + "…"
    tourn_date = tournament.date.isoformat() if tournament.date else ""

    return render_og_png(
        "og/deck_og.svg",
        {
            "deck": deck,
            "tournament": tournament,
            "format_name": format_name,
            "archetype_display": archetype_display,
            "tourn_name": tourn_name,
            "tourn_date": tourn_date,
            "mana_pill": mana_pill,
            "spotlight_name": spotlight_name,
            "spotlight_count": spotlight_count,
            "image_data_uri": image_data_uri,
            "result_label": result_label,
            "result_value": result_value,
            "result_sub": result_sub,
            "result_color": result_color,
            "mainboard_count": deck.mainboard_card_count,
            "sideboard_count": deck.sideboard_card_count,
            "land_count": land_count,
            "avg_cmc": avg_cmc,
            "curve_bars": curve_bars,
            "col1_cards": col1_cards,
            "col2_cards": col2_cards,
            "overflow_row": overflow_row,
        },
        request=request,
    )
