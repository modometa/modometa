"""Tests for SEO features: meta tags, Open Graph, Twitter cards, JSON-LD, and dynamic PNG OG images."""

import json
import re
from datetime import date

import pytest
from django.template.loader import render_to_string
from django.test import Client

from core.models import Deck
from core.models import Match
from core.models import Tournament
from core.views import _get_og_matrix_cell_style
from core.views import build_mana_pill
from core.views import build_matrix_og_data
from core.views import render_og_png


@pytest.fixture
def client():
    return Client()


@pytest.fixture
def sample_data(db):
    """Seed sample tournament and deck for SEO tests."""
    tourn = Tournament.objects.create(
        id="legacy_challenge_test_01",
        format="legacy",
        event_type="challenge",
        name="Legacy Challenge 32",
        date=date(2026, 1, 15),
    )
    deck = Deck.objects.create(
        id="legacy_challenge_test_01_deck_1",
        tournament=tourn,
        format="legacy",
        player="LoggySD",
        player_lower="loggysd",
        archetype="Dimir Murktide",
        archetype_slug="dimir-murktide",
        color_name="Dimir",
        colors="UB",
        mainboard=[{"card": "Murktide Regent", "count": 4}],
        sideboard=[],
        is_top8=True,
        rank=1,
    )
    return tourn, deck


@pytest.mark.django_db
def test_homepage_seo_and_open_graph(client):
    """Test that homepage contains meta description, canonical, OG tags, Twitter cards, and JSON-LD."""
    response = client.get("/")
    assert response.status_code == 200
    content = response.content.decode("utf-8")

    # Meta description & canonical
    assert '<meta name="description" content=' in content
    assert '<link rel="canonical" href=' in content
    assert '<meta name="robots" content="index, follow">' in content

    # Open Graph
    assert '<meta property="og:site_name" content="MODOMeta">' in content
    assert '<meta property="og:type" content="website">' in content
    assert (
        '<meta property="og:title" content="MTGO Metagame Overview — MODOMeta">'
        in content
    )
    assert '<meta property="og:image" content=' in content
    assert '<meta property="og:image:type" content="image/png">' in content
    assert "/og.png" in content

    # Twitter card
    assert '<meta name="twitter:card" content="summary_large_image">' in content
    assert (
        '<meta name="twitter:title" content="MTGO Metagame Overview — MODOMeta">'
        in content
    )
    assert '<meta name="twitter:image" content=' in content

    # JSON-LD WebSite
    json_ld_matches = re.findall(
        r'<script type="application/ld\+json">\s*(\{.*?\})\s*</script>',
        content,
        re.DOTALL,
    )
    assert len(json_ld_matches) >= 1
    data = json.loads(json_ld_matches[0])
    assert data["@context"] == "https://schema.org"
    assert data["@type"] == "WebSite"
    assert data["name"] == "MODOMeta"


@pytest.mark.django_db
def test_format_overview_seo_and_open_graph(client, sample_data):
    """Test that format overview contains dynamic OG image, meta description, and BreadcrumbList JSON-LD."""
    response = client.get("/legacy/")
    assert response.status_code == 200
    content = response.content.decode("utf-8")

    assert "Explore the Legacy metagame on Magic Online" in content
    assert (
        '<meta property="og:title" content="Legacy Metagame Overview — MODOMeta">'
        in content
    )
    assert "/legacy/og.png" in content
    assert "Legacy Metagame 52-week bump chart on MODOMeta" in content

    # JSON-LD BreadcrumbList
    json_ld_matches = re.findall(
        r'<script type="application/ld\+json">\s*(\{.*?\})\s*</script>',
        content,
        re.DOTALL,
    )
    assert len(json_ld_matches) >= 1
    data = json.loads(json_ld_matches[0])
    assert data["@type"] == "BreadcrumbList"
    assert len(data["itemListElement"]) == 2
    assert data["itemListElement"][1]["name"] == "Legacy Metagame"


@pytest.mark.django_db
def test_archetype_detail_seo_and_open_graph(client, sample_data):
    """Test that archetype detail contains dynamic OG image, meta tags, and BreadcrumbList."""
    _, deck = sample_data
    response = client.get(f"/legacy/archetype/{deck.archetype_slug}/")
    assert response.status_code == 200
    content = response.content.decode("utf-8")

    assert deck.archetype in content
    assert f"/legacy/archetype/{deck.archetype_slug}/og.png" in content

    json_ld_matches = re.findall(
        r'<script type="application/ld\+json">\s*(\{.*?\})\s*</script>',
        content,
        re.DOTALL,
    )
    assert len(json_ld_matches) >= 1
    data = json.loads(json_ld_matches[0])
    assert data["@type"] == "BreadcrumbList"
    assert len(data["itemListElement"]) == 3
    assert data["itemListElement"][2]["name"] == deck.archetype


@pytest.mark.django_db
def test_player_detail_seo_and_open_graph(client, sample_data):
    """Test that player detail contains profile og:type, player OG image, and ProfilePage JSON-LD."""
    _, deck = sample_data
    response = client.get(f"/player/{deck.player}/")
    assert response.status_code == 200
    content = response.content.decode("utf-8")

    assert '<meta property="og:type" content="profile">' in content
    assert f"/player/{deck.player}/og.png" in content

    json_ld_matches = re.findall(
        r'<script type="application/ld\+json">\s*(\[.*?\])\s*</script>',
        content,
        re.DOTALL,
    )
    assert len(json_ld_matches) >= 1
    data = json.loads(json_ld_matches[0])
    assert isinstance(data, list)
    types = [item["@type"] for item in data]
    assert "BreadcrumbList" in types
    assert "ProfilePage" in types


@pytest.mark.django_db
def test_tournament_detail_seo_and_json_ld(client, sample_data):
    """Test Event schema and BreadcrumbList on tournament detail."""
    tourn, _ = sample_data
    response = client.get(f"/legacy/tournaments/{tourn.id}/")
    assert response.status_code == 200
    content = response.content.decode("utf-8")

    assert tourn.name in content
    json_ld_matches = re.findall(
        r'<script type="application/ld\+json">\s*(\[.*?\])\s*</script>',
        content,
        re.DOTALL,
    )
    assert len(json_ld_matches) >= 1
    data = json.loads(json_ld_matches[0])
    assert isinstance(data, list)
    types = [item["@type"] for item in data]
    assert "BreadcrumbList" in types
    assert "Event" in types


@pytest.mark.django_db
def test_faq_seo_and_json_ld(client):
    """Test FAQPage schema on /faq/."""
    response = client.get("/faq/")
    assert response.status_code == 200
    content = response.content.decode("utf-8")

    assert "Frequently asked questions about MODOMeta" in content
    json_ld_matches = re.findall(
        r'<script type="application/ld\+json">\s*(\{.*?\})\s*</script>',
        content,
        re.DOTALL,
    )
    assert len(json_ld_matches) >= 1
    data = json.loads(json_ld_matches[0])
    assert data["@type"] == "FAQPage"
    assert len(data["mainEntity"]) >= 3


@pytest.mark.django_db
def test_default_og_image_view(client):
    """Test default PNG OG image view."""
    response = client.get("/og.png")
    assert response.status_code == 200
    assert response["Content-Type"] == "image/png"
    assert response.content[:4] == b"\x89PNG"
    assert len(response.content) > 1000


@pytest.mark.django_db
def test_format_og_image_view(client, sample_data):
    """Test format PNG OG image view with bump chart and 1-year deck count."""
    # Add a deck from 2 years ago (outside 365-day cutoff)
    older_tourn = Tournament.objects.create(
        id="legacy_challenge_old_test",
        format="legacy",
        event_type="challenge",
        name="Legacy Challenge 2024",
        date=date(2024, 1, 1),
    )
    Deck.objects.create(
        id="legacy_challenge_old_deck_1",
        tournament=older_tourn,
        format="legacy",
        player="OldPlayer",
        player_lower="oldplayer",
        archetype="Storm",
        archetype_slug="storm",
        mainboard=[],
        sideboard=[],
    )

    response = client.get("/legacy/og.png")
    assert response.status_code == 200
    assert response["Content-Type"] == "image/png"
    assert response.content[:4] == b"\x89PNG"
    assert len(response.content) > 1000

    # Verify template renders 1-year total copy
    svg_content = render_to_string(
        "og/format_og.svg",
        {
            "format_name": "Legacy",
            "total_decks": "1",
            "bump_chart": {"has_data": False},
        },
    )
    assert (
        "1 league and challenge decks over the previous year across competitive Legacy events on MTGO"
        in svg_content
    )
    assert '<use href="#shield-logo"' in svg_content

    # Test invalid format returns 404
    resp_invalid = client.get("/nonexistent_format/og.png")
    assert resp_invalid.status_code == 404

    # Verify template renders top archetypes when bump chart has data
    svg_with_data = render_to_string(
        "og/format_og.svg",
        {
            "format_name": "Legacy",
            "total_decks": "100",
            "bump_chart": {
                "has_data": True,
                "month_labels": [],
                "rank_lines": [],
                "tracks": [],
                "top_archetypes": [
                    {
                        "rank": 1,
                        "slug": "storm",
                        "name": "Storm",
                        "display_name": "Storm",
                        "count": 50,
                        "count_formatted": "50",
                        "share": 50.0,
                        "color": "#f97316",
                        "y_offset": 0,
                    }
                ],
            },
        },
    )
    assert "TOP ARCHETYPES" in svg_with_data
    assert "Storm" in svg_with_data
    assert "50 Top 8s" in svg_with_data


@pytest.mark.django_db
def test_archetype_og_image_view(client, sample_data):
    """Test archetype PNG OG image view with activity heatmap and mana symbols."""
    _, deck = sample_data
    response = client.get(f"/legacy/archetype/{deck.archetype_slug}/og.png")
    assert response.status_code == 200
    assert response["Content-Type"] == "image/png"
    assert response.content[:4] == b"\x89PNG"
    assert len(response.content) > 1000

    # Test non-existent archetype returns 404
    resp_invalid = client.get("/legacy/archetype/nonexistent-archetype-xyz/og.png")
    assert resp_invalid.status_code == 404

    # Verify build_mana_pill helper
    assert build_mana_pill(None) is None
    assert build_mana_pill("") is None
    assert build_mana_pill("XYZ") is None
    pill = build_mana_pill("BU")  # Should sort canonically to U, B
    assert pill is not None
    assert [s["id"] for s in pill["symbols"]] == ["mana-u", "mana-b"]
    assert pill["width"] == 82
    assert pill["x"] == 1140 - 82

    # Verify template renders 90-day copy and mana symbol vectors
    svg_content = render_to_string(
        "og/archetype_og.svg",
        {
            "format_name": "Legacy",
            "archetype_name": "Dimir Murktide",
            "mana_pill": pill,
            "stats": {},
            "heatmap": {"weeks": []},
        },
    )
    assert (
        "Legacy Archetype Performance &amp; Metagame Share Over The Previous 90 Days"
        in svg_content
    )
    assert "• Dimir" not in svg_content
    assert '<use href="#mana-u"' in svg_content
    assert '<use href="#mana-b"' in svg_content
    assert '<use href="#shield-logo"' in svg_content


@pytest.mark.django_db
def test_player_og_image_view(client, sample_data):
    """Test player PNG OG image view with activity heatmap."""
    _, deck = sample_data
    deck.wins = 7
    deck.losses = 2
    deck.save()

    response = client.get(f"/player/{deck.player}/og.png")
    assert response.status_code == 200
    assert response["Content-Type"] == "image/png"
    assert response.content[:4] == b"\x89PNG"
    assert len(response.content) > 1000

    svg = render_to_string(
        "og/player_og.svg",
        {
            "player_name": deck.player,
            "total_decks": 1,
            "total_5_0s": 0,
            "total_top8s": 1,
            "chall_appearances": 1,
            "conversion_rate": 100.0,
            "chall_match_wins": 7,
            "chall_matches_count": 9,
            "chall_win_rate": 77.8,
            "formats_played": ["Legacy"],
            "heatmap": {"month_labels": [], "day_labels": [], "weeks": []},
        },
    )
    assert "MATCH WIN %" in svg
    assert "77.8%" in svg

    # Test non-existent player returns 404
    resp_invalid = client.get("/player/nonexistent_player_12345/og.png")
    assert resp_invalid.status_code == 404


def test_render_og_png_font_resolution():
    """Test that render_og_png rasterizes SVGs with font directory configuration."""
    response = render_og_png("og/default_og.svg", {"site_name": "MODOMeta"})
    assert response.status_code == 200
    assert response["Content-Type"] == "image/png"
    assert response.content[:4] == b"\x89PNG"
    assert len(response.content) > 5000


@pytest.mark.django_db
def test_format_matrix_og_image_view(client):
    """Test format archetype matrix PNG OG image view and meta tags."""
    today = date.today()
    tourn = Tournament.objects.create(
        id="modern_matrix_og_tourn",
        format="modern",
        event_type="challenge",
        name="Modern Challenge 32",
        date=today,
    )
    d_burn = Deck.objects.create(
        id="d_burn_og",
        tournament=tourn,
        format="modern",
        player="BurnPlayer",
        player_lower="burnplayer",
        archetype="Burn",
        archetype_slug="burn",
    )
    d_delver = Deck.objects.create(
        id="d_delver_og",
        tournament=tourn,
        format="modern",
        player="DelverPlayer",
        player_lower="delverplayer",
        archetype="Delver",
        archetype_slug="delver",
    )
    Match.objects.create(
        id="match_og_1",
        tournament=tourn,
        round_name="Finals",
        round_slug="finals",
        player1="BurnPlayer",
        player2="DelverPlayer",
        player1_deck=d_burn,
        player2_deck=d_delver,
        player1_wins=2,
        player2_wins=1,
    )

    # 1. Test matrix OG PNG response
    response = client.get("/modern/matrix/og.png")
    assert response.status_code == 200
    assert response["Content-Type"] == "image/png"
    assert response.content[:4] == b"\x89PNG"
    assert len(response.content) > 1000

    # 2. Test invalid format returns 404
    resp_invalid = client.get("/nonexistent_format/matrix/og.png")
    assert resp_invalid.status_code == 404

    # 3. Test matrix HTML page meta tags point to matrix og.png
    matrix_resp = client.get("/modern/matrix/")
    assert matrix_resp.status_code == 200
    assert (
        'property="og:image" content="http://testserver/modern/matrix/og.png"'
        in matrix_resp.content.decode()
    )
    assert (
        'name="twitter:image" content="http://testserver/modern/matrix/og.png"'
        in matrix_resp.content.decode()
    )


@pytest.mark.django_db
def test_format_matrix_og_image_empty_data(client):
    """Test matrix OG image renders empty placeholder when no matches exist."""
    response = client.get("/vintage/matrix/og.png")
    assert response.status_code == 200
    assert response["Content-Type"] == "image/png"
    assert response.content[:4] == b"\x89PNG"


def test_og_matrix_cell_style_tiers():
    """Verify SVG color and opacity tiers for OG matrix cells."""
    # Mirror cell
    m_style = _get_og_matrix_cell_style(None, is_mirror=True)
    assert m_style["text_fill"] == "#52525b"

    # No data cell
    nd_style = _get_og_matrix_cell_style(None, is_mirror=False, has_data=False)
    assert nd_style["text_fill"] == "#3f3f46"

    # Deep green (> 60%)
    dg = _get_og_matrix_cell_style(65.0)
    assert dg["text_fill"] == "#6ee7b7"
    assert dg["bg_fill"] == "#064e3b"

    # Green (55% - 60%)
    g = _get_og_matrix_cell_style(58.0)
    assert g["text_fill"] == "#34d399"

    # Neutral (45% - 55%)
    neu = _get_og_matrix_cell_style(50.0)
    assert neu["text_fill"] == "#e4e4e7"
    assert neu["bg_fill"] == "#27272a"

    # Red (40% - 45%)
    r = _get_og_matrix_cell_style(42.0)
    assert r["text_fill"] == "#f87171"

    # Deep red (< 40%)
    dr = _get_og_matrix_cell_style(30.0)
    assert dr["text_fill"] == "#fca5a5"
    assert dr["bg_fill"] == "#450a0a"


@pytest.mark.django_db
def test_build_matrix_og_data_eight_archetypes():
    """Verify build_matrix_og_data defaults to 8 archetypes and formats an 8x8 grid."""
    tourn = Tournament.objects.create(
        id="modern_tourn_eight",
        format="modern",
        event_type="challenge",
        name="Modern Challenge",
        date=date(2026, 2, 1),
    )
    # Create 9 distinct archetypes
    decks = []
    for i in range(9):
        name = f"Archetype {chr(65 + i)}"
        d = Deck.objects.create(
            id=f"deck_arch_{i}",
            tournament=tourn,
            format="modern",
            player=f"Player{i}",
            player_lower=f"player{i}",
            archetype=name,
            archetype_slug=f"archetype-{chr(97 + i)}",
        )
        decks.append(d)

    # Create matches between consecutive decks so each has matches
    for i in range(8):
        Match.objects.create(
            id=f"match_eight_{i}",
            tournament=tourn,
            round_name="Quarterfinals",
            round_slug="quarterfinals",
            player1=decks[i].player,
            player2=decks[i + 1].player,
            player1_deck=decks[i],
            player2_deck=decks[i + 1],
            player1_wins=2,
            player2_wins=1,
        )

    data = build_matrix_og_data("modern", date(2026, 1, 1), date(2026, 3, 1))
    assert data["has_data"] is True
    assert len(data["col_headers"]) == 8
    assert len(data["rows"]) == 8
    for row in data["rows"]:
        assert len(row["cells"]) == 8


@pytest.mark.django_db
def test_tournament_og_image_view(client, sample_data):
    """Test tournament PNG OG image view for Challenge and League events, and 404 for other events."""
    tourn, deck = sample_data
    # 1. Challenge event
    response = client.get(f"/legacy/tournaments/{tourn.id}/og.png")
    assert response.status_code == 200
    assert response["Content-Type"] == "image/png"
    assert response.content[:4] == b"\x89PNG"
    assert len(response.content) > 1000

    html_resp = client.get(f"/legacy/tournaments/{tourn.id}/")
    assert html_resp.status_code == 200
    assert f"/legacy/tournaments/{tourn.id}/og.png" in html_resp.content.decode("utf-8")

    # 2. League event
    league_tourn = Tournament.objects.create(
        id="legacy_league_seo_test",
        format="legacy",
        event_type="league",
        name="Legacy League",
        date=date(2026, 1, 15),
    )
    Deck.objects.create(
        id="league_deck_seo_test",
        tournament=league_tourn,
        format="legacy",
        player="LeaguePlayer",
        player_lower="leagueplayer",
        archetype="Delver",
        archetype_slug="delver",
        result="5-0",
        is_5_0=True,
    )
    league_og_resp = client.get(f"/legacy/tournaments/{league_tourn.id}/og.png")
    assert league_og_resp.status_code == 200
    assert league_og_resp["Content-Type"] == "image/png"
    assert league_og_resp.content[:4] == b"\x89PNG"

    league_html = client.get(f"/legacy/tournaments/{league_tourn.id}/").content.decode(
        "utf-8"
    )
    assert f"/legacy/tournaments/{league_tourn.id}/og.png" in league_html

    # 3. Other unranked event returns 404
    other_tourn = Tournament.objects.create(
        id="legacy_other_seo_test",
        format="legacy",
        event_type="other",
        name="Legacy LCQ",
        date=date(2026, 1, 15),
    )
    Deck.objects.create(
        id="other_deck_seo_test",
        tournament=other_tourn,
        format="legacy",
        player="OtherPlayer",
        player_lower="otherplayer",
        archetype="Delver",
        archetype_slug="delver",
        result="3-1",
    )
    other_og_resp = client.get(f"/legacy/tournaments/{other_tourn.id}/og.png")
    assert other_og_resp.status_code == 404

    other_html = client.get(f"/legacy/tournaments/{other_tourn.id}/").content.decode(
        "utf-8"
    )
    assert f"/legacy/tournaments/{other_tourn.id}/og.png" not in other_html
    assert "/og.png" in other_html

    # 4. Test invalid format returns 404
    assert (
        client.get(f"/invalidformat/tournaments/{tourn.id}/og.png").status_code == 404
    )


@pytest.mark.django_db
def test_tournament_list_og_image_view(client, sample_data):
    """Test tournament list PNG OG image view for formats with recent Challenge data."""
    tourn, deck = sample_data
    response = client.get("/legacy/tournaments/og.png")
    assert response.status_code == 200
    assert response["Content-Type"] == "image/png"
    assert response.content[:4] == b"\x89PNG"
    assert len(response.content) > 1000

    # Test HTML includes tournament list og.png link
    html_resp = client.get("/legacy/tournaments/")
    assert html_resp.status_code == 200
    assert "/legacy/tournaments/og.png" in html_resp.content.decode("utf-8")

    # Test invalid format returns 404
    assert client.get("/invalidformat/tournaments/og.png").status_code == 404


@pytest.mark.django_db
def test_sitemap_endpoint(client, sample_data):
    """Test sitemap.xml returns valid XML with core pages, format hubs, and archetypes."""
    response = client.get("/sitemap.xml")
    assert response.status_code == 200
    assert "xml" in response["Content-Type"]
    content = response.content.decode("utf-8")

    # Structure
    assert '<?xml version="1.0" encoding="UTF-8"?>' in content
    assert "<urlset" in content

    # Core pages
    assert "<loc>https://testserver/</loc>" in content
    assert "<loc>https://testserver/faq/</loc>" in content

    # Format hub, matrix, leaderboard
    assert "<loc>https://testserver/legacy/</loc>" in content
    assert "<loc>https://testserver/legacy/matrix/</loc>" in content
    assert "<loc>https://testserver/legacy/leaderboard/</loc>" in content

    # Seeded archetype from sample_data
    assert "<loc>https://testserver/legacy/archetype/dimir-murktide/</loc>" in content
    assert "<changefreq>weekly</changefreq>" in content

    # Disallowed deep routes should NOT be in the sitemap
    assert "/player/" not in content
    assert "/tournaments/" not in content
    assert "/card/" not in content


@pytest.mark.django_db
def test_robots_txt_sitemap_reference(client):
    """Test robots.txt includes reference to sitemap.xml."""
    response = client.get("/robots.txt")
    assert response.status_code == 200
    content = response.content.decode("utf-8")
    assert "Sitemap: https://modometa.com/sitemap.xml" in content
