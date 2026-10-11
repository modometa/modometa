"""Integration tests for Modometa routes and views."""

import json
import re
import warnings
from datetime import date
from datetime import timedelta
from io import StringIO
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from django.conf import settings
from django.core.cache import cache
from django.core.management import call_command
from django.http import HttpResponse
from django.template.loader import render_to_string
from django.test import Client
from django.test import RequestFactory
from django.test import override_settings

from core.engine.knn import set_global_knn_index
from core.models import Card
from core.models import CardLookup
from core.models import Deck
from core.models import Match
from core.models import Tournament
from core.pipeline.scryfall import _extract_mana_cost
from core.views import _get_matrix_color_class
from core.views import build_challenge_archetype_chart
from core.views import build_format_bump_chart
from core.views import classify_card_type
from core.views import get_archetype_matrix_data
from core.views import get_dataset_min_date
from core.views import get_dataset_start_year
from core.views import get_reference_date
from core.views import public_cache


@pytest.fixture
def client():
    return Client()


@pytest.mark.django_db
def test_home_view(client):
    response = client.get("/")
    assert response.status_code == 200
    assert b"MTGO Metagame Analyzer" in response.content
    assert len(response.context["format_metas"]) > 0
    assert "total_top8_slots" in response.context["format_metas"][0]
    assert b"Fan Content Policy" in response.content


@pytest.mark.django_db
def test_home_view_30d_toggle(client):
    response = client.get("/?days=30")
    assert response.status_code == 200


@pytest.mark.django_db
def test_format_overview_view(client):
    response = client.get("/legacy/")
    assert response.status_code == 200
    assert b"Legacy Metagame" in response.content
    assert "total_chall_decks" in response.context["stats"]
    assert "total_top8_slots" in response.context["stats"]
    assert "total_5_0s" in response.context["stats"]

    # Verify column header order: League Share before Challenge Share before Top 8 Share before T8 Momentum before Conversion
    content = response.content
    pos_ls = content.find(b">League Share</span>")
    pos_cs = content.find(b">Challenge Share</span>")
    pos_t8s = content.find(b">Top 8 Share</span>")
    pos_t8m = content.find(b">T8 Momentum</th>")
    pos_conv = content.find(b">Conversion</span>")
    assert (
        pos_ls != -1
        and pos_cs != -1
        and pos_t8s != -1
        and pos_t8m != -1
        and pos_conv != -1
    )
    assert pos_ls < pos_cs < pos_t8s < pos_t8m < pos_conv
    assert b">Colors</th>" not in content

    # Verify each archetype slug appears at most once in format overview
    archetypes = response.context["archetypes"]
    slugs = [a["slug"] for a in archetypes]
    assert len(slugs) == len(set(slugs)), "Each archetype must appear at most once"

    # Verify that metrics for an archetype match on archetype_detail
    if archetypes:
        first_arch = archetypes[0]
        arch_resp = client.get(f"/legacy/archetype/{first_arch['slug']}/")
        assert arch_resp.status_code == 200
        arch_stats = arch_resp.context["stats"]
        assert first_arch["top8_count"] == arch_stats["top8_count"]
        assert first_arch["chall_appearances"] == arch_stats["challenge_appearances"]
        assert first_arch["league_5_0_count"] == arch_stats["league_5_0_count"]
        assert first_arch["top8_share"] == arch_stats["top8_share"]
        assert first_arch["challenge_share"] == arch_stats["challenge_share"]
        assert first_arch["conversion_rate"] == arch_stats["conversion_rate"]
        assert first_arch["league_share"] == arch_stats["league_share"]

    cards_resp = client.get("/legacy/cards/?days=90")
    assert cards_resp.status_code == 200
    if response.context["common_cards"]:
        assert (
            response.context["common_cards"][0]["name"]
            == cards_resp.context["cards"][0]["name"]
        )
        assert (
            response.context["common_cards"][0]["adoption_pct"]
            == cards_resp.context["cards"][0]["adoption_pct"]
        )


@pytest.mark.django_db
def test_tournament_list_view(client):
    Tournament.objects.create(
        id="legacy-challenge-test",
        name="Legacy Challenge 64",
        format="legacy",
        event_type="challenge",
        date=date(2024, 1, 1),
        player_count=64,
        deck_count=32,
    )
    Tournament.objects.create(
        id="legacy-league-test",
        name="Legacy League",
        format="legacy",
        event_type="league",
        date=date(2024, 1, 2),
        player_count=None,
        deck_count=15,
    )
    response = client.get("/legacy/tournaments/")
    assert response.status_code == 200
    content = response.content.decode()
    assert "Legacy Tournaments" in content
    assert ">Players</th>" in content
    assert "64" in content
    assert "—" in content


@pytest.mark.django_db
def test_tournament_detail_view(client):
    t = Tournament.objects.create(
        id="legacy-challenge-32-test",
        name="Legacy Challenge 32",
        format="legacy",
        event_type="challenge",
        date=date(2024, 1, 1),
        player_count=42,
    )
    Deck.objects.create(
        id="legacy-challenge-32-test_testplayer_1",
        tournament=t,
        format="legacy",
        player="TestPlayer",
        player_lower="testplayer",
        archetype="Dimir Tempo",
        archetype_slug="dimir-tempo",
        colors="UB",
        color_name="Dimir",
        result="1st Place",
        is_top8=True,
    )
    response = client.get(f"/legacy/tournaments/{t.id}/")
    assert response.status_code == 200
    assert b">Colors</th>" not in response.content
    assert b">Place / Result</th>" in response.content
    assert b">Deck</th>" in response.content
    assert b">Player</th>" in response.content
    assert b'title="Dimir"' in response.content
    assert b"42 Players" in response.content


@pytest.mark.django_db
def test_tournament_detail_sort_order(client):
    t = Tournament.objects.create(
        id="legacy-swiss-test",
        name="Legacy LCQ",
        format="legacy",
        event_type="other",
        date=date(2024, 1, 1),
    )
    decks_data = [
        ("p_13", "1-3", None),
        ("p_50", "5-0", None),
        ("p_41", "4-1", None),
        ("p_31", "3-1", None),
        ("p_32", "3-2", None),
        ("p_1st", "1st Place", 1),
    ]
    for player, res, rank in decks_data:
        Deck.objects.create(
            id=f"legacy-swiss-test_{player}",
            tournament=t,
            format="legacy",
            player=player,
            player_lower=player.lower(),
            archetype="Delver",
            archetype_slug="delver",
            result=res,
            rank=rank,
        )
    response = client.get(f"/legacy/tournaments/{t.id}/")
    assert response.status_code == 200
    ordered_players = [d.player for d in response.context["decks"]]
    assert ordered_players == ["p_1st", "p_50", "p_41", "p_31", "p_32", "p_13"]


@pytest.mark.django_db
def test_build_challenge_archetype_chart_empty():
    """Test that build_challenge_archetype_chart returns None on empty list."""
    assert build_challenge_archetype_chart([]) is None


@pytest.mark.django_db
def test_tournament_detail_challenge_archetype_chart(client):
    """Test that challenge tournaments render the archetype breakdown chart with correct data."""
    t = Tournament.objects.create(
        id="legacy-challenge-chart-test",
        name="Legacy Challenge 32",
        format="legacy",
        event_type="challenge",
        date=date(2024, 1, 1),
    )
    # Archetype A (Doomsday): 2 decks (1 Top 8, 1 Swiss)
    Deck.objects.create(
        id="deck_1",
        tournament=t,
        format="legacy",
        player="PlayerA",
        player_lower="playera",
        archetype="Doomsday",
        archetype_slug="doomsday",
        colors="UB",
        color_name="Dimir",
        result="1st Place",
        rank=1,
        is_top8=True,
    )
    Deck.objects.create(
        id="deck_2",
        tournament=t,
        format="legacy",
        player="PlayerB",
        player_lower="playerb",
        archetype="Doomsday",
        archetype_slug="doomsday",
        colors="UB",
        color_name="Dimir",
        result="9th Place",
        rank=9,
        is_top8=False,
    )
    # Archetype B (Omni-Tell): 3 decks (0 Top 8, 3 Swiss)
    for i in range(3):
        Deck.objects.create(
            id=f"deck_omni_{i}",
            tournament=t,
            format="legacy",
            player=f"PlayerOmni_{i}",
            player_lower=f"playeromni_{i}",
            archetype="Omni-Tell",
            archetype_slug="omni-tell",
            colors="UR",
            color_name="Izzet",
            result=f"{10 + i}th Place",
            rank=10 + i,
            is_top8=False,
        )

    # Archetype C (Mardu Energy): 1 deck (1 Top 8)
    Deck.objects.create(
        id="deck_mardu",
        tournament=t,
        format="legacy",
        player="PlayerMardu",
        player_lower="playermardu",
        archetype="Mardu Energy",
        archetype_slug="mardu-energy",
        colors="WBR",
        color_name="Mardu",
        result="2nd Place",
        rank=2,
        is_top8=True,
    )

    response = client.get(f"/legacy/tournaments/{t.id}/")
    assert response.status_code == 200
    assert "archetype_chart" in response.context
    chart = response.context["archetype_chart"]
    assert chart is not None
    assert chart["total_decks"] == 6
    assert chart["total_archetypes"] == 3
    assert chart["total_top8_decks"] == 2
    assert chart["total_swiss_decks"] == 4
    assert chart["max_count"] == 3

    # Ordering: Omni-Tell (3 decks), Doomsday (2 decks, 1 Top 8), Mardu Energy (1 deck, 1 Top 8)
    row_names = [r["name"] for r in chart["rows"]]
    assert row_names == ["Omni-Tell", "Doomsday", "Mardu Energy"]

    # Verify Doomsday units
    dd_row = chart["rows"][1]
    assert dd_row["total_count"] == 2
    assert dd_row["top8_count"] == 1
    assert dd_row["swiss_count"] == 1
    assert len(dd_row["units"]) == 2
    assert dd_row["units"][0]["is_top8"] is True
    assert dd_row["units"][0]["rank"] == 1
    assert dd_row["units"][1]["is_top8"] is False
    assert dd_row["units"][1]["rank"] == 9

    # Verify HTML template contains chart elements
    content = response.content.decode("utf-8")
    assert "Archetype Breakdown" in content
    assert "challenge-unit-top8" in content
    assert "challenge-unit-swiss" in content
    assert "Omni-Tell" in content
    assert "Doomsday" in content
    assert "Mardu Energy" in content


@pytest.mark.django_db
def test_tournament_detail_league_archetype_chart(client):
    """Test that league tournaments render the simplified archetype chart without green squares."""
    t = Tournament.objects.create(
        id="legacy-league-chart-test",
        name="Legacy League",
        format="legacy",
        event_type="league",
        date=date(2024, 1, 1),
    )
    # 2 Delver decks
    for i in range(2):
        Deck.objects.create(
            id=f"league_delver_{i}",
            tournament=t,
            format="legacy",
            player=f"DelverPlayer_{i}",
            player_lower=f"delverplayer_{i}",
            archetype="Delver",
            archetype_slug="delver",
            colors="UR",
            color_name="Izzet",
            result="5-0",
            is_5_0=True,
        )
    # 1 Reanimator deck
    Deck.objects.create(
        id="league_reanimator",
        tournament=t,
        format="legacy",
        player="ReanimatorPlayer",
        player_lower="reanimatorplayer",
        archetype="Reanimator",
        archetype_slug="reanimator",
        colors="UB",
        color_name="Dimir",
        result="5-0",
        is_5_0=True,
    )

    response = client.get(f"/legacy/tournaments/{t.id}/")
    assert response.status_code == 200
    assert "archetype_chart" in response.context
    chart = response.context["archetype_chart"]
    assert chart is not None
    assert chart["is_challenge"] is False
    assert chart["total_decks"] == 3
    assert chart["total_archetypes"] == 2
    assert chart["total_top8_decks"] == 0

    content = response.content.decode("utf-8")
    assert "Archetype Breakdown" in content
    assert "published 5-0 decks" in content
    assert "🏆 3 5-0 Decks" in content
    assert "challenge-unit-swiss" in content
    assert "challenge-unit-top8" not in content  # No green squares
    assert ">Top 8<" not in content  # No Top 8 column
    assert ">5-0s<" in content


@pytest.mark.django_db
def test_tournament_detail_other_event_no_chart(client):
    """Test that non-challenge and non-league events do not render the archetype chart."""
    t = Tournament.objects.create(
        id="legacy-other-no-chart-test",
        name="Legacy Other Event",
        format="legacy",
        event_type="other",
        date=date(2024, 1, 1),
    )
    Deck.objects.create(
        id="other_deck_1",
        tournament=t,
        format="legacy",
        player="OtherPlayer",
        player_lower="otherplayer",
        archetype="Delver",
        archetype_slug="delver",
        colors="UR",
        color_name="Izzet",
        result="3-1",
    )
    response = client.get(f"/legacy/tournaments/{t.id}/")
    assert response.status_code == 200
    assert response.context.get("archetype_chart") is None
    content = response.content.decode("utf-8")
    assert "challenge-unit-top8" not in content
    assert "challenge-unit-swiss" not in content


@pytest.mark.django_db
def test_tournament_list_challenge_top8_chart(client):
    """Test that tournament list renders 30-day challenge Top 8 breakdown chart."""
    ref_d = date(2024, 6, 15)
    t = Tournament.objects.create(
        id="legacy-challenge-list-test",
        name="Legacy Challenge 32",
        format="legacy",
        event_type="challenge",
        date=ref_d,
    )
    # 1st place Delver deck
    Deck.objects.create(
        id="tlist_deck_1",
        tournament=t,
        format="legacy",
        player="WinnerPlayer",
        player_lower="winnerplayer",
        archetype="Delver",
        archetype_slug="delver",
        colors="UR",
        color_name="Izzet",
        rank=1,
        result="1st",
        is_top8=True,
    )
    # 2nd place Reanimator deck
    Deck.objects.create(
        id="tlist_deck_2",
        tournament=t,
        format="legacy",
        player="RunnerUpPlayer",
        player_lower="runnerupplayer",
        archetype="Reanimator",
        archetype_slug="reanimator",
        colors="UB",
        color_name="Dimir",
        rank=2,
        result="2nd",
        is_top8=True,
    )

    # 1. Default (All) shows the chart
    response = client.get("/legacy/tournaments/")
    assert response.status_code == 200
    assert "archetype_chart" in response.context
    chart = response.context["archetype_chart"]
    assert chart is not None
    assert chart["total_decks"] >= 2
    assert chart["total_tournaments"] >= 1

    content = response.content.decode("utf-8")
    assert "Archetype Breakdown" in content
    assert "challenge-unit-winner" in content
    assert "challenge-unit-top8" in content
    assert "/legacy/tournaments/og.png" in content
    assert "/player/WinnerPlayer/deck/legacy-challenge-list-test/" in content
    assert "/player/RunnerUpPlayer/deck/legacy-challenge-list-test/" in content

    # 2. Challenge filter shows the chart
    chall_resp = client.get("/legacy/tournaments/?type=challenge")
    assert chall_resp.status_code == 200
    assert chall_resp.context.get("archetype_chart") is not None

    # 3. League filter hides the chart
    league_resp = client.get("/legacy/tournaments/?type=league")
    assert league_resp.status_code == 200
    assert league_resp.context.get("archetype_chart") is None
    league_content = league_resp.content.decode("utf-8")
    assert "challenge-unit-winner" not in league_content


@pytest.mark.django_db
def test_tournament_list_chart_groups_others(client):
    """Test that single top8 archetypes without a win are grouped into Others on tournament list, but not tournament detail."""
    ref_d = date(2024, 6, 15)
    t = Tournament.objects.create(
        id="legacy-challenge-others-test",
        name="Legacy Challenge 32",
        format="legacy",
        event_type="challenge",
        date=ref_d,
    )
    # Archetype 1: Multi top 8 (Delver: 2 finishes, 1 win)
    for i in range(2):
        Deck.objects.create(
            id=f"others_delver_{i}",
            tournament=t,
            format="legacy",
            player=f"DelverPlayer_{i}",
            player_lower=f"delverplayer_{i}",
            archetype="Delver",
            archetype_slug="delver",
            colors="UR",
            color_name="Izzet",
            rank=i + 1,
            result="1st" if i == 0 else "2nd",
            is_top8=True,
        )
    # Archetype 2: Single top 8 with a win (Painter: 1 finish, 1 win) -> should STAY separate
    Deck.objects.create(
        id="others_painter_1",
        tournament=t,
        format="legacy",
        player="PainterPlayer",
        player_lower="painterplayer",
        archetype="Painter",
        archetype_slug="painter",
        colors="R",
        color_name="Mono-Red",
        rank=1,
        result="1st",
        is_top8=True,
    )
    # Archetype 3: Single top 8 without a win (Dredge: 1 finish, 0 wins) -> should be grouped into Others
    Deck.objects.create(
        id="others_dredge_1",
        tournament=t,
        format="legacy",
        player="DredgePlayer",
        player_lower="dredgeplayer",
        archetype="Dredge",
        archetype_slug="dredge",
        colors="UBG",
        color_name="Sultai",
        rank=4,
        result="4th",
        is_top8=True,
    )
    # Archetype 4: Single top 8 without a win (Maverick: 1 finish, 0 wins) -> should be grouped into Others
    Deck.objects.create(
        id="others_maverick_1",
        tournament=t,
        format="legacy",
        player="MaverickPlayer",
        player_lower="maverickplayer",
        archetype="Maverick",
        archetype_slug="maverick",
        colors="WG",
        color_name="Selesnya",
        rank=7,
        result="7th",
        is_top8=True,
    )

    # 1. Tournament list view groups Dredge and Maverick into Others
    response = client.get("/legacy/tournaments/")
    assert response.status_code == 200
    chart = response.context["archetype_chart"]
    row_names = [r["name"] for r in chart["rows"]]
    assert "Delver" in row_names
    assert "Painter" in row_names  # Kept separate because it has 1st place!
    assert "Dredge" not in row_names  # Grouped into Others
    assert "Maverick" not in row_names  # Grouped into Others
    assert "Others" in row_names
    assert row_names[-1] == "Others"  # At the bottom

    others_row = next(r for r in chart["rows"] if r["name"] == "Others")
    assert others_row["total_count"] == 2
    assert others_row["wins_count"] == 0
    assert len(others_row["units"]) == 2
    assert others_row["is_others"] is True

    content = response.content.decode("utf-8")
    assert "Others" in content
    # Pips in Others link to their respective deck detail pages
    assert "/player/DredgePlayer/deck/legacy-challenge-others-test/" in content
    assert "/player/MaverickPlayer/deck/legacy-challenge-others-test/" in content

    # 2. Tournament detail view keeps all archetypes individual (no Others grouping)
    detail_resp = client.get(f"/legacy/tournaments/{t.id}/")
    assert detail_resp.status_code == 200
    detail_chart = detail_resp.context["archetype_chart"]
    detail_row_names = [r["name"] for r in detail_chart["rows"]]
    assert "Others" not in detail_row_names
    assert "Dredge" in detail_row_names
    assert "Maverick" in detail_row_names


@pytest.mark.django_db
def test_player_detail_view(client):
    deck = Deck.objects.first()
    if not deck:
        t = Tournament.objects.create(
            id="legacy-challenge-2023-05",
            name="Legacy Challenge 32",
            format="legacy",
            date=date(2023, 5, 10),
            event_type="challenge",
        )
        deck = Deck.objects.create(
            tournament=t,
            player="Ark4n",
            player_lower="ark4n",
            format="legacy",
            archetype="Dimir Ninjas",
            archetype_slug="dimir-ninjas",
            result="1st Place",
            is_top8=True,
            mainboard=[{"card": "Brainstorm", "count": 4}],
            sideboard=[],
        )
    response = client.get(f"/player/{deck.player}/")
    assert response.status_code == 200
    assert deck.player.encode() in response.content
    assert b"League 5-0" in response.content
    assert b"all time" in response.content
    assert "start_year" in response.context
    if response.context["start_year"]:
        assert f"(since {response.context['start_year']})".encode() in response.content
    assert "chall_appearances" in response.context
    assert "chall_match_wins" in response.context
    assert "chall_matches_count" in response.context
    assert "chall_win_rate" in response.context
    assert "page_obj" in response.context
    assert response.context["page_obj"].paginator.per_page == 100

    # Verify League 5-0 Trophies stat card comes before Challenge T8 stat card
    l50_pos = response.content.find(b"League 5-0")
    ct8_pos = response.content.find(b"Challenge T8")
    cwin_pos = response.content.find(b"Challenge MW%")
    assert l50_pos != -1 and ct8_pos != -1 and cwin_pos != -1
    assert l50_pos < ct8_pos < cwin_pos

    # Verify Action column is gone and Date links to deck detail
    assert b">Action</th>" not in response.content
    assert b"View Deck" not in response.content
    deck_link = f"/player/{deck.player}/deck/{deck.tournament_id}/".encode()
    assert deck_link in response.content


@pytest.mark.django_db
def test_player_detail_challenge_match_win_rate(client):
    """Test challenge match win rate calculation, tooltip formatting, and edge cases."""
    tourn_chall = Tournament.objects.create(
        id="vintage-challenge-test-winrate",
        name="Vintage Challenge 32",
        format="vintage",
        date=date(2024, 6, 1),
        event_type="challenge",
    )
    tourn_league = Tournament.objects.create(
        id="vintage-league-test-winrate",
        name="Vintage League",
        format="vintage",
        date=date(2024, 6, 2),
        event_type="league",
    )

    Deck.objects.create(
        id="deck_test_a",
        tournament=tourn_chall,
        player="AliceWins",
        player_lower="alicewins",
        format="vintage",
        archetype="Oath",
        archetype_slug="oath",
        result="2nd Place",
        is_top8=True,
        wins=2,
        losses=3,
    )
    Deck.objects.create(
        id="deck_test_b",
        tournament=tourn_chall,
        player="BobPlays",
        player_lower="bobplays",
        format="vintage",
        archetype="Dredge",
        archetype_slug="dredge",
        result="3rd Place",
        is_top8=True,
        wins=1,
        losses=2,
    )
    Deck.objects.create(
        id="deck_test_c",
        tournament=tourn_chall,
        player="CharlieLoses",
        player_lower="charlieloses",
        format="vintage",
        archetype="MUD",
        archetype_slug="mud",
        result="4th Place",
        is_top8=True,
        wins=0,
        losses=3,
    )
    # League deck for Alice
    Deck.objects.create(
        id="deck_test_a_league",
        tournament=tourn_league,
        player="AliceWins",
        player_lower="alicewins",
        format="vintage",
        archetype="Oath",
        archetype_slug="oath",
        result="5-0",
        is_5_0=True,
    )
    # League-only deck for LeagueHero
    Deck.objects.create(
        id="deck_test_league_hero",
        tournament=tourn_league,
        player="LeagueHero",
        player_lower="leaguehero",
        format="vintage",
        archetype="Oath",
        archetype_slug="oath",
        result="5-0",
        is_5_0=True,
    )

    # Test Alice: 2 wins / 5 challenge matches = 40.0%
    resp_a = client.get("/player/AliceWins/")
    assert resp_a.status_code == 200
    assert resp_a.context["chall_match_wins"] == 2
    assert resp_a.context["chall_matches_count"] == 5
    assert resp_a.context["chall_win_rate"] == 40.0
    content_a = resp_a.content.decode()
    assert 'title="2/5"' in content_a
    assert "40.0%" in content_a
    assert "Challenge MW%" in content_a

    # Test Bob: 1 win / 3 matches = 33.3%
    resp_b = client.get("/player/BobPlays/")
    assert resp_b.status_code == 200
    assert resp_b.context["chall_match_wins"] == 1
    assert resp_b.context["chall_matches_count"] == 3
    assert resp_b.context["chall_win_rate"] == 33.3
    content_b = resp_b.content.decode()
    assert 'title="1/3"' in content_b
    assert "33.3%" in content_b

    # Test LeagueHero (0 challenge matches): 0/0 and 0.0%
    resp_l = client.get("/player/LeagueHero/")
    assert resp_l.status_code == 200
    assert resp_l.context["chall_match_wins"] == 0
    assert resp_l.context["chall_matches_count"] == 0
    assert resp_l.context["chall_win_rate"] == 0.0
    content_l = resp_l.content.decode()
    assert 'title="0/0"' in content_l


@pytest.mark.django_db
def test_get_dataset_start_year():
    Tournament.objects.all().delete()
    assert get_dataset_min_date() is None
    assert get_dataset_start_year() is None

    Tournament.objects.create(
        id="tourn-other-2021",
        name="Other Event",
        format="legacy",
        date=date(2021, 6, 1),
        event_type="other",
    )
    assert get_dataset_min_date() == date(2021, 6, 1)
    assert get_dataset_start_year() == 2021

    Tournament.objects.create(
        id="tourn-league-2023",
        name="Legacy League",
        format="legacy",
        date=date(2023, 1, 15),
        event_type="league",
    )
    Tournament.objects.create(
        id="tourn-challenge-2024",
        name="Legacy Challenge",
        format="legacy",
        date=date(2024, 2, 20),
        event_type="challenge",
    )
    assert get_dataset_min_date() == date(2023, 1, 15)
    assert get_dataset_start_year() == 2023

    # Verify 4-hour caching behavior when not in test mode
    cache.clear()
    with override_settings(IS_TESTING=False):
        assert cache.get("dataset_min_date_v1") is None
        cached_val = get_dataset_min_date()
        assert cached_val == date(2023, 1, 15)
        assert cache.get("dataset_min_date_v1") == date(2023, 1, 15)
    cache.clear()


@pytest.mark.django_db
def test_player_detail_pagination(client):
    tourn = Tournament.objects.create(
        id="tourn_pagination_test",
        name="Legacy League",
        format="legacy",
        date=date.today(),
        event_type="league",
    )
    player_name = "PaginationTestPlayer"
    decks = [
        Deck(
            id=f"page_deck_{i}",
            tournament=tourn,
            player=player_name,
            player_lower=player_name.lower(),
            format="legacy",
            archetype="Delver",
            archetype_slug="delver",
            result="5-0",
            is_5_0=True,
            is_top8=False,
            mainboard=[{"card": "Brainstorm", "count": 4}],
            sideboard=[],
        )
        for i in range(105)
    ]
    Deck.objects.bulk_create(decks)

    resp_p1 = client.get(f"/player/{player_name}/")
    assert resp_p1.status_code == 200
    assert len(resp_p1.context["decks"]) == 100
    assert resp_p1.context["page_obj"].paginator.num_pages == 2
    assert "1–100 of 105 events".encode() in resp_p1.content
    assert "Next →".encode() in resp_p1.content

    resp_p2 = client.get(f"/player/{player_name}/?page=2")
    assert resp_p2.status_code == 200
    assert len(resp_p2.context["decks"]) == 5
    assert "← Previous".encode() in resp_p2.content


@pytest.mark.django_db
def test_deck_detail_view(client):
    t = Tournament.objects.create(
        id="tourn-deck-detail-test",
        name="Vintage Challenge",
        format="vintage",
        date=date(2024, 6, 1),
    )
    deck = Deck.objects.create(
        id="deck-detail-test-1",
        tournament=t,
        format="vintage",
        player="DetailPlayer",
        player_lower="detailplayer",
        archetype="Oath of Druids",
        archetype_slug="oath-of-druids",
        result="1st",
        mainboard=[{"card": "Oath of Druids", "count": 4}],
        sideboard=[],
    )

    response = client.get(f"/player/{deck.player}/deck/{deck.tournament_id}/")
    assert response.status_code == 200
    assert b"Mainboard" in response.content
    assert b"Similar" in response.content

    # Archetype link in title and action button
    content = response.content.decode()
    arch_url = f"/{deck.format}/archetype/{deck.archetype_slug}/"
    assert arch_url in content
    assert "Archetype Overview" in content


@pytest.mark.django_db
def test_classify_card_type_precedence():
    # 1. Land creature -> land (Dryad Arbor)
    dryad = Card(name="Dryad Arbor", type_line="Land Creature — Forest Dryad")
    assert classify_card_type(dryad) == "land"

    # 2. Enchantment creature -> creature (Overlord of the Balemurk)
    overlord = Card(
        name="Overlord of the Balemurk",
        type_line="Enchantment Creature — Avatar Horror",
    )
    assert classify_card_type(overlord) == "creature"

    # 3. Artifact creature -> creature (Walking Ballista)
    ballista = Card(name="Walking Ballista", type_line="Artifact Creature — Construct")
    assert classify_card_type(ballista) == "creature"

    # 4. MDFC with Instant front face and Land back face -> instant (Sink into Stupor)
    sink = Card(
        name="Sink into Stupor // Soporific Springs",
        type_line="Instant // Land",
    )
    assert classify_card_type(sink) == "instant"

    # 5. MDFC with Sorcery front face and Land back face -> sorcery (Bala Ged Recovery)
    bala = Card(
        name="Bala Ged Recovery // Bala Ged Sanctuary",
        type_line="Sorcery // Land",
    )
    assert classify_card_type(bala) == "sorcery"

    # 6. Enchantment Land -> land (Urza's Saga)
    saga = Card(name="Urza's Saga", type_line="Enchantment Land — Urza's Saga")
    assert classify_card_type(saga) == "land"

    # 7. Battle MDFC -> battle (Invasion of Ikoria)
    ikoria = Card(
        name="Invasion of Ikoria // Zilortha, Apex of Ikoria",
        type_line="Battle — Siege // Legendary Creature — Dinosaur",
    )
    assert classify_card_type(ikoria) == "battle"

    # 8. None or empty type_line -> other
    assert classify_card_type(None) == "other"
    assert classify_card_type(Card(name="Mystery Card", type_line="")) == "other"


@pytest.mark.django_db
def test_deck_detail_mainboard_sections(client):
    t = Tournament.objects.create(
        id="tourn-deck-sections-test",
        name="Legacy Challenge 32",
        format="legacy",
        date=date(2024, 7, 1),
    )
    Card.objects.create(
        id="c-tamiyo",
        oracle_id="o-tamiyo",
        name="Tamiyo, Inquisitive Student",
        normalized_name="tamiyo, inquisitive student",
        type_line="Legendary Creature — Moonfolk Wizard // Legendary Planeswalker — Tamiyo",
        cmc=1.0,
    )
    Card.objects.create(
        id="c-bilbo",
        oracle_id="o-bilbo",
        name="Bilbo, Thief in the Night",
        normalized_name="bilbo, thief in the night",
        type_line="Legendary Creature — Halfling Rogue",
        cmc=2.0,
    )
    Card.objects.create(
        id="c-bowmasters",
        oracle_id="o-bowmasters",
        name="Orcish Bowmasters",
        normalized_name="orcish bowmasters",
        type_line="Creature — Orc Archer",
        cmc=2.0,
    )
    Card.objects.create(
        id="c-force",
        oracle_id="o-force",
        name="Force of Will",
        normalized_name="force of will",
        type_line="Instant",
        cmc=5.0,
    )
    Card.objects.create(
        id="c-brainstorm",
        oracle_id="o-brainstorm",
        name="Brainstorm",
        normalized_name="brainstorm",
        type_line="Instant",
        cmc=1.0,
    )
    Card.objects.create(
        id="c-island",
        oracle_id="o-island",
        name="Island",
        normalized_name="island",
        type_line="Basic Land — Island",
        cmc=0.0,
    )
    Card.objects.create(
        id="c-ee",
        oracle_id="o-ee",
        name="Engineered Explosives",
        normalized_name="engineered explosives",
        type_line="Artifact",
        cmc=0.0,
    )
    Card.objects.create(
        id="c-hydro",
        oracle_id="o-hydro",
        name="Hydroblast",
        normalized_name="hydroblast",
        type_line="Instant",
        cmc=1.0,
    )
    Card.objects.create(
        id="c-massacre",
        oracle_id="o-massacre",
        name="Massacre",
        normalized_name="massacre",
        type_line="Sorcery",
        cmc=4.0,
    )

    deck = Deck.objects.create(
        id="deck-sections-test-1",
        tournament=t,
        format="legacy",
        player="Ark4n",
        player_lower="ark4n",
        archetype="Dimir Midrange",
        archetype_slug="dimir-midrange",
        result="1st Place",
        mainboard=[
            {"card": "Force of Will", "count": 4},
            {"card": "Brainstorm", "count": 4},
            {"card": "Tamiyo, Inquisitive Student", "count": 4},
            {"card": "Orcish Bowmasters", "count": 2},
            {"card": "Bilbo, Thief in the Night", "count": 4},
            {"card": "Island", "count": 4},
        ],
        sideboard=[
            {"card": "Massacre", "count": 2},
            {"card": "Engineered Explosives", "count": 1},
            {"card": "Hydroblast", "count": 2},
        ],
    )

    response = client.get(f"/player/{deck.player}/deck/{deck.tournament_id}/")
    assert response.status_code == 200
    sections = response.context["mainboard_sections"]
    section_names = [s["name"] for s in sections]
    assert section_names == ["Creatures", "Instants", "Lands"]

    # Verify Creatures section: count=10, sorted by cmc then name
    creatures_sec = sections[0]
    assert creatures_sec["count"] == 10
    assert creatures_sec["icon"] == "creature"
    c_names = [c["card"] for c in creatures_sec["cards"]]
    # Tamiyo (1.0) < Bilbo (2.0) < Bowmasters (2.0)
    assert c_names == [
        "Tamiyo, Inquisitive Student",
        "Bilbo, Thief in the Night",
        "Orcish Bowmasters",
    ]

    # Verify Instants section: count=8, Brainstorm (1.0) < Force of Will (5.0)
    instants_sec = sections[1]
    assert instants_sec["count"] == 8
    assert [c["card"] for c in instants_sec["cards"]] == [
        "Brainstorm",
        "Force of Will",
    ]

    # Verify rendered HTML header has icon and count
    content = response.content.decode()
    assert "Creatures (10)" in content
    assert "ms-creature" in content
    assert "Instants (8)" in content
    assert "ms-instant" in content
    assert "Lands (4)" in content
    assert "ms-land" in content
    # Sideboard should be sorted by CMC ascending (EE: 0.0 < Hydroblast: 1.0 < Massacre: 4.0)
    assert "Sideboard" in content
    sb_cards = [c["card"] for c in response.context["deck"].sideboard]
    assert sb_cards == ["Engineered Explosives", "Hydroblast", "Massacre"]


@pytest.mark.django_db
def test_archetype_detail_view(client):
    t_chall = Tournament.objects.create(
        id="legacy_chall_detail_test",
        format="legacy",
        name="Legacy Challenge",
        event_type="challenge",
        date=date.today(),
    )
    t_league = Tournament.objects.create(
        id="legacy_league_detail_test",
        format="legacy",
        name="Legacy League",
        event_type="league",
        date=date.today(),
    )
    Card.objects.create(name="Brainstorm", mana_cost="{U}")
    deck = Deck.objects.create(
        id="legacy_deck_detail_test_1",
        tournament=t_chall,
        format="legacy",
        player="DetailTester",
        player_lower="detailtester",
        archetype="Dimir Tempo",
        archetype_slug="dimir-tempo",
        result="1st Place",
        is_top8=True,
        mainboard=[{"card": "Brainstorm", "count": 4}],
        sideboard=[],
    )
    Deck.objects.create(
        id="legacy_deck_detail_test_2",
        tournament=t_league,
        format="legacy",
        player="DetailTester2",
        player_lower="detailtester2",
        archetype="Dimir Tempo",
        archetype_slug="dimir-tempo",
        result="5-0",
        is_5_0=True,
        mainboard=[{"card": "Brainstorm", "count": 4}],
        sideboard=[],
    )

    response = client.get(f"/legacy/archetype/{deck.archetype_slug}/")
    assert response.status_code == 200
    stats = response.context["stats"]
    assert "total_top8_slots" in stats
    assert "total_chall_decks" in stats
    assert "total_5_0s" in stats

    content = response.content
    assert b"League Share" in content
    assert b"League 5-0 Share" not in content
    assert b"Challenge Share" in content
    assert b"Challenge Top 8 Share" in content
    assert b"Challenge Conversion" in content

    # Check stat card ordering
    pos_ls = content.find(b"League Share")
    pos_cs = content.find(b"Challenge Share")
    pos_t8 = content.find(b"Challenge Top 8 Share")
    pos_cc = content.find(b"Challenge Conversion")
    assert pos_ls < pos_cs < pos_t8 < pos_cc

    # Check tooltips
    ls_tt = f'title="{stats["league_5_0_count"]}/{stats["total_5_0s"]}"'.encode()
    cs_tt = f'title="{stats["challenge_appearances"]}/{stats["total_chall_decks"]}"'.encode()
    t8_tt = f'title="{stats["top8_count"]}/{stats["total_top8_slots"]}"'.encode()
    cc_tt = f'title="{stats["top8_count"]}/{stats["challenge_appearances"]}"'.encode()
    assert ls_tt in content
    assert cs_tt in content
    assert t8_tt in content
    assert cc_tt in content

    # Verify Deck column is present with deck link and date is plain text
    assert b">Deck</th>" in content
    assert b">Date</th>" in content
    assert b"View \xe2\x86\x92" not in content
    if response.context["finishes"]:
        finish = response.context["finishes"][0]
        finish_link = f"/player/{finish.player}/deck/{finish.tournament_id}/{finish.deck_index}/".encode()
        assert finish_link in content
        date_str = finish.tournament.date.strftime("%Y-%m-%d").encode()
        assert f">{date_str.decode()}</a>".encode() not in content

    # Verify number of finishes in timeframe is shown
    assert "total_decks" in response.context
    assert (
        f"{response.context['total_decks']} finishes last {response.context['days']} days".encode()
        in content
    )

    # Verify Recent Tournament Finishes appears before Core Cards
    pos_finishes = content.find(b"Recent Tournament Finishes")
    pos_core = content.find(b"Core Cards")
    assert pos_finishes != -1 and pos_core != -1
    assert pos_finishes < pos_core

    # Verify Activity Heatmap appears after Challenge Conversion and before Recent Tournament Finishes
    pos_heatmap = content.find(b"Activity Heatmap")
    assert pos_heatmap != -1
    assert pos_cc < pos_heatmap < pos_finishes

    # Verify heatmap context
    assert "heatmap" in response.context
    heatmap = response.context["heatmap"]
    assert len(heatmap["weeks"]) == 53
    assert len(heatmap["weeks"][0]["days"]) == 7
    assert heatmap["weeks"][0]["days"][0]["date"].weekday() == 0  # Monday
    assert heatmap["weeks"][0]["days"][6]["date"].weekday() == 6  # Sunday
    assert "active_days_count" in heatmap
    assert "total_leagues" in heatmap
    assert "total_challenges" in heatmap

    # Verify Core Cards has mana_cost
    if response.context["core_cards"]:
        first_core = response.context["core_cards"][0]
        assert "mana_cost" in first_core


@pytest.mark.django_db
def test_archetype_detail_core_cards_sorting(client):
    """Core cards sort primarily by adoption % (desc) and secondarily by avg quantity (desc)."""
    t = Tournament.objects.create(
        id="tourn_core_cards_sort",
        name="Vintage Challenge",
        format="vintage",
        date=date(2024, 6, 1),
    )

    # 10 decks total:
    # - "Oath of Druids": in 10 decks, 4 copies each (100%, avg 4.0)
    # - "Black Lotus": in 10 decks, 1 copy each (100%, avg 1.0)
    # - "Force of Will": in 9 decks, 4 copies each (90%, avg 4.0)
    # - "Ancestral Recall": in 9 decks, 1 copy each (90%, avg 1.0)
    for i in range(10):
        mb = [
            {"card": "Oath of Druids", "count": 4},
            {"card": "Black Lotus", "count": 1},
        ]
        if i < 9:
            mb.append({"card": "Force of Will", "count": 4})
            mb.append({"card": "Ancestral Recall", "count": 1})

        Deck.objects.create(
            id=f"deck_sort_test_{i}",
            tournament=t,
            format="vintage",
            player=f"Player_{i}",
            player_lower=f"player_{i}",
            archetype="Oath of Druids",
            archetype_slug="oath-of-druids",
            result=f"{i + 1}th Place",
            mainboard=mb,
            sideboard=[],
        )

    response = client.get("/vintage/archetype/oath-of-druids/?days=90")
    assert response.status_code == 200
    core_cards = response.context["core_cards"]
    card_names = [c["name"] for c in core_cards]

    # 4-of at 100% must precede 1-of at 100%, which must precede 4-of at 90%, which precedes 1-of at 90%
    assert card_names == [
        "Oath of Druids",
        "Black Lotus",
        "Force of Will",
        "Ancestral Recall",
    ]


@pytest.mark.django_db
def test_archetype_detail_all_time_pagination_and_zero_timeframe(client):
    # Create anchor tournament so reference date is 2024-06-01 (making 2024-01-01 > 90d ago)
    Tournament.objects.create(
        id="tourn_anchor_arch",
        name="Legacy Anchor",
        format="legacy",
        date=date(2024, 6, 1),
    )

    # Create an archetype with finishes from 2024-01-01 (outside 90d window)
    t = Tournament.objects.create(
        id="tourn_old_arch",
        name="Legacy Challenge",
        format="legacy",
        date=date(2024, 1, 1),
    )
    for i in range(30):
        Deck.objects.create(
            id=f"tourn_old_arch_p_{i}_1",
            tournament=t,
            format="legacy",
            player=f"Player_{i}",
            player_lower=f"player_{i}",
            archetype="Orzhov Midrange",
            archetype_slug="orzhov-midrange",
            result="1st",
            mainboard=[{"card": "Swords to Plowshares", "count": 4}],
            sideboard=[],
        )

    response = client.get("/legacy/archetype/orzhov-midrange/?days=90")
    assert response.status_code == 200
    content = response.content.decode()

    # Upper right label
    assert "0 finishes last 90 days" in content

    # All-time finishes paginated (25 per page -> 30 finishes = 2 pages)
    assert "page_obj" in response.context
    page_obj = response.context["page_obj"]
    assert page_obj.paginator.count == 30
    assert len(response.context["finishes"]) == 25
    assert "1–25 of 30 finishes" in content
    assert "Next →" in content

    # Empty core cards section explanation
    assert (
        "No card statistics available for this archetype because there's no recent league or challenge decks."
        in content
    )


@pytest.mark.django_db
def test_archetype_activity_heatmap(client):
    ref = get_reference_date()

    # Create anchor tournament on ref date
    Tournament.objects.create(
        id="heatmap_tourn_anchor",
        name="Modern Anchor",
        format="modern",
        event_type="league",
        date=ref,
    )

    # 1. Day with 1x League 5-0
    t_league1 = Tournament.objects.create(
        id="heatmap_tourn_league_1",
        name="Modern League",
        format="modern",
        event_type="league",
        date=ref - timedelta(days=5),
    )
    Deck.objects.create(
        id="heatmap_deck_league_1",
        tournament=t_league1,
        format="modern",
        player="HeatmapPlayer1",
        player_lower="heatmapplayer1",
        archetype="Grixis Shadow",
        archetype_slug="grixis-shadow",
        is_5_0=True,
        result="5-0",
    )

    # 2. Day with 2x League 5-0s
    t_league2 = Tournament.objects.create(
        id="heatmap_tourn_league_2",
        name="Modern League 2",
        format="modern",
        event_type="league",
        date=ref - timedelta(days=10),
    )
    Deck.objects.create(
        id="heatmap_deck_league_2a",
        tournament=t_league2,
        format="modern",
        player="HeatmapPlayer2",
        player_lower="heatmapplayer2",
        archetype="Grixis Shadow",
        archetype_slug="grixis-shadow",
        is_5_0=True,
        result="5-0",
    )
    Deck.objects.create(
        id="heatmap_deck_league_2b",
        tournament=t_league2,
        format="modern",
        player="HeatmapPlayer3",
        player_lower="heatmapplayer3",
        archetype="Grixis Shadow",
        archetype_slug="grixis-shadow",
        is_5_0=True,
        result="5-0",
    )

    # 3. Day with Challenge entry (non-top 8)
    t_chall_entry = Tournament.objects.create(
        id="heatmap_tourn_chall_entry",
        name="Modern Challenge 32",
        format="modern",
        event_type="challenge",
        date=ref - timedelta(days=15),
    )
    Deck.objects.create(
        id="heatmap_deck_chall_entry",
        tournament=t_chall_entry,
        format="modern",
        player="HeatmapPlayer4",
        player_lower="heatmapplayer4",
        archetype="Grixis Shadow",
        archetype_slug="grixis-shadow",
        is_top8=False,
        rank=12,
        result="12th Place",
    )

    # 4. Day with both Challenge Top 8 AND League 5-0 (Priority test: Top 8 > League)
    same_day = ref - timedelta(days=20)
    t_same_chall = Tournament.objects.create(
        id="heatmap_tourn_same_chall",
        name="Modern Challenge 64",
        format="modern",
        event_type="challenge",
        date=same_day,
    )
    Deck.objects.create(
        id="heatmap_deck_same_chall",
        tournament=t_same_chall,
        format="modern",
        player="HeatmapPlayer5",
        player_lower="heatmapplayer5",
        archetype="Grixis Shadow",
        archetype_slug="grixis-shadow",
        is_top8=True,
        rank=3,
        result="3rd Place",
    )
    t_same_league = Tournament.objects.create(
        id="heatmap_tourn_same_league",
        name="Modern League Same Day",
        format="modern",
        event_type="league",
        date=same_day,
    )
    Deck.objects.create(
        id="heatmap_deck_same_league",
        tournament=t_same_league,
        format="modern",
        player="HeatmapPlayer6",
        player_lower="heatmapplayer6",
        archetype="Grixis Shadow",
        archetype_slug="grixis-shadow",
        is_5_0=True,
        result="5-0",
    )

    # 5. Day with Challenge 1st Place (Winner)
    t_winner = Tournament.objects.create(
        id="heatmap_tourn_winner",
        name="Modern Challenge Winner",
        format="modern",
        event_type="challenge",
        date=ref - timedelta(days=25),
    )
    Deck.objects.create(
        id="heatmap_deck_winner",
        tournament=t_winner,
        format="modern",
        player="HeatmapPlayer7",
        player_lower="heatmapplayer7",
        archetype="Grixis Shadow",
        archetype_slug="grixis-shadow",
        is_top8=True,
        rank=1,
        result="1st Place",
    )

    response = client.get("/modern/archetype/grixis-shadow/")
    assert response.status_code == 200
    heatmap = response.context["heatmap"]

    assert heatmap["active_days_count"] == 5
    assert heatmap["total_leagues"] == 4
    assert heatmap["total_challenges"] == 3
    assert heatmap["total_top8s"] == 2

    # Verify cell days map
    days_by_date = {}
    for w in heatmap["weeks"]:
        for d in w["days"]:
            days_by_date[d["date_str"]] = d

    # 1. League 1 day
    d1_date = ref - timedelta(days=5)
    d1_date_str = d1_date.strftime("%Y-%m-%d")
    d1 = days_by_date[d1_date_str]
    assert d1["color_class"] == "activity-league-1"
    assert d1["tooltip"] == f"{d1_date.strftime('%b')} {d1_date.day}: 1x League 5-0"
    assert d1["url"] == "/modern/tournaments/heatmap_tourn_league_1/"

    # 2. League 2 day
    d2_date = ref - timedelta(days=10)
    d2_date_str = d2_date.strftime("%Y-%m-%d")
    d2 = days_by_date[d2_date_str]
    assert d2["color_class"] == "activity-league-2"
    assert d2["tooltip"] == f"{d2_date.strftime('%b')} {d2_date.day}: 2x League 5-0s"
    assert d2["url"] == "/modern/tournaments/heatmap_tourn_league_2/"

    # 3. Challenge entry day
    d3_date = ref - timedelta(days=15)
    d3_date_str = d3_date.strftime("%Y-%m-%d")
    d3 = days_by_date[d3_date_str]
    assert d3["color_class"] == "activity-challenge-entry"
    assert (
        d3["tooltip"] == f"{d3_date.strftime('%b')} {d3_date.day}: 1x Challenge entry"
    )
    assert d3["url"] == "/modern/tournaments/heatmap_tourn_chall_entry/"

    # 4. Same day: Challenge Top 8 + League 5-0 (Challenge priority for color and link)
    d4_date_str = same_day.strftime("%Y-%m-%d")
    d4 = days_by_date[d4_date_str]
    assert d4["color_class"] == "activity-challenge-top8"
    assert (
        d4["tooltip"]
        == f"{same_day.strftime('%b')} {same_day.day}: 1x Challenge Top 8, 1x League 5-0"
    )
    assert d4["url"] == "/modern/tournaments/heatmap_tourn_same_chall/"

    # 5. Challenge winner day
    d5_date = ref - timedelta(days=25)
    d5_date_str = d5_date.strftime("%Y-%m-%d")
    d5 = days_by_date[d5_date_str]
    assert d5["color_class"] == "activity-challenge-winner"
    assert (
        d5["tooltip"] == f"{d5_date.strftime('%b')} {d5_date.day}: 1x Challenge Top 8"
    )
    assert d5["url"] == "/modern/tournaments/heatmap_tourn_winner/"

    # 6. Inactive day (no results -> no url link)
    d_inactive_date = ref - timedelta(days=3)
    d_inactive_date_str = d_inactive_date.strftime("%Y-%m-%d")
    d_inactive = days_by_date[d_inactive_date_str]
    assert d_inactive["color_class"] == "activity-level-0"
    assert (
        d_inactive["tooltip"]
        == f"{d_inactive_date.strftime('%b')} {d_inactive_date.day}: No tournament finishes"
    )
    assert d_inactive["url"] == ""

    # Verify link appears in rendered HTML
    assert b'href="/modern/tournaments/heatmap_tourn_same_chall/"' in response.content

    # 7. Check 30d toggle keeps 53 weeks in heatmap
    resp_30 = client.get("/modern/archetype/grixis-shadow/?days=30")
    assert resp_30.status_code == 200
    assert len(resp_30.context["heatmap"]["weeks"]) == 53


@pytest.mark.django_db
def test_player_activity_heatmap(client):
    ref = get_reference_date()

    # Create anchor tournament on ref date
    Tournament.objects.create(
        id="player_tourn_anchor",
        name="Modern Anchor",
        format="modern",
        event_type="league",
        date=ref,
    )

    player_name = "HeatmapHero"
    player_lower = player_name.lower()

    # 1. Day with 1x League 5-0 (Modern)
    t_league = Tournament.objects.create(
        id="player_tourn_league",
        name="Modern League",
        format="modern",
        event_type="league",
        date=ref - timedelta(days=5),
    )
    Deck.objects.create(
        id="player_deck_league",
        tournament=t_league,
        format="modern",
        player=player_name,
        player_lower=player_lower,
        archetype="Murktide",
        archetype_slug="murktide",
        is_5_0=True,
        result="5-0",
    )

    # 2. Day with 1x Challenge Top 8 (Legacy)
    t_chall_t8 = Tournament.objects.create(
        id="player_tourn_chall_t8",
        name="Legacy Challenge",
        format="legacy",
        event_type="challenge",
        date=ref - timedelta(days=12),
    )
    Deck.objects.create(
        id="player_deck_chall_t8",
        tournament=t_chall_t8,
        format="legacy",
        player=player_name,
        player_lower=player_lower,
        archetype="Delver",
        archetype_slug="delver",
        is_top8=True,
        rank=4,
        result="4th Place",
    )

    # 3. Day with 1x Challenge Winner (Pauper)
    t_chall_win = Tournament.objects.create(
        id="player_tourn_chall_win",
        name="Pauper Challenge",
        format="pauper",
        event_type="challenge",
        date=ref - timedelta(days=20),
    )
    Deck.objects.create(
        id="player_deck_chall_win",
        tournament=t_chall_win,
        format="pauper",
        player=player_name,
        player_lower=player_lower,
        archetype="Burn",
        archetype_slug="burn",
        is_top8=True,
        rank=1,
        result="1st Place",
    )

    # 4. Day with 1x Challenge Entry non-T8 (Pioneer)
    t_chall_entry = Tournament.objects.create(
        id="player_tourn_chall_entry",
        name="Pioneer Challenge",
        format="pioneer",
        event_type="challenge",
        date=ref - timedelta(days=25),
    )
    Deck.objects.create(
        id="player_deck_chall_entry",
        tournament=t_chall_entry,
        format="pioneer",
        player=player_name,
        player_lower=player_lower,
        archetype="Phoenix",
        archetype_slug="phoenix",
        is_top8=False,
        rank=15,
        result="15th Place",
    )

    response = client.get(f"/player/{player_name}/")
    assert response.status_code == 200
    assert "heatmap" in response.context
    heatmap = response.context["heatmap"]

    assert len(heatmap["weeks"]) == 53
    assert heatmap["active_days_count"] == 4
    assert heatmap["total_leagues"] == 1
    assert heatmap["total_challenges"] == 3
    assert heatmap["total_top8s"] == 2
    assert heatmap["aria_label"] == f"{player_name}'s 52-week activity heatmap"

    # Verify cell days map
    days_by_date = {}
    for w in heatmap["weeks"]:
        for d in w["days"]:
            days_by_date[d["date_str"]] = d

    # 1. League day
    d_lg = days_by_date[(ref - timedelta(days=5)).strftime("%Y-%m-%d")]
    assert d_lg["color_class"] == "activity-league-1"
    assert d_lg["url"] == "/modern/tournaments/player_tourn_league/"

    # 2. Challenge T8 day (cross-format: legacy)
    d_t8 = days_by_date[(ref - timedelta(days=12)).strftime("%Y-%m-%d")]
    assert d_t8["color_class"] == "activity-challenge-top8"
    assert d_t8["url"] == "/legacy/tournaments/player_tourn_chall_t8/"

    # 3. Challenge winner day (pauper)
    d_win = days_by_date[(ref - timedelta(days=20)).strftime("%Y-%m-%d")]
    assert d_win["color_class"] == "activity-challenge-winner"
    assert d_win["url"] == "/pauper/tournaments/player_tourn_chall_win/"

    # 4. Challenge entry day (pioneer)
    d_entry = days_by_date[(ref - timedelta(days=25)).strftime("%Y-%m-%d")]
    assert d_entry["color_class"] == "activity-challenge-entry"
    assert d_entry["url"] == "/pioneer/tournaments/player_tourn_chall_entry/"

    # Verify SVG markup in rendered HTML
    assert b"52-week activity heatmap" in response.content
    assert b'href="/legacy/tournaments/player_tourn_chall_t8/"' in response.content
    assert b"activity-challenge-winner" in response.content


@pytest.mark.django_db
def test_cards_view(client):
    cache.clear()

    # 1. 90d All (Leagues + Challenges)
    resp_90_all = client.get("/legacy/cards/?days=90")
    assert resp_90_all.status_code == 200
    assert b"Cards" in resp_90_all.content
    assert b"Challenges" in resp_90_all.content
    assert b"Leagues" in resp_90_all.content
    assert resp_90_all.context["page_obj"].paginator.per_page == 100
    assert cache.get("cards_list_v4:legacy:90:all") is not None

    # 2. 30d All
    resp_30_all = client.get("/legacy/cards/?days=30")
    assert resp_30_all.status_code == 200
    assert cache.get("cards_list_v4:legacy:30:all") is not None

    # 3. 90d Challenges only
    resp_90_chall = client.get("/legacy/cards/?type=challenge&days=90")
    assert resp_90_chall.status_code == 200
    assert cache.get("cards_list_v4:legacy:90:challenge") is not None

    # 4. 90d Leagues only
    resp_90_league = client.get("/legacy/cards/?type=league&days=90")
    assert resp_90_league.status_code == 200
    assert cache.get("cards_list_v4:legacy:90:league") is not None

    # Verify they are all distinct cached items
    k_90_all = cache.get("cards_list_v4:legacy:90:all")
    k_30_all = cache.get("cards_list_v4:legacy:30:all")
    k_90_chall = cache.get("cards_list_v4:legacy:90:challenge")
    k_90_league = cache.get("cards_list_v4:legacy:90:league")

    assert k_90_all is not None
    assert k_30_all is not None
    assert k_90_chall is not None
    assert k_90_league is not None


@pytest.mark.django_db
def test_leaderboard_view(client):
    response = client.get("/legacy/leaderboard/")
    assert response.status_code == 200
    assert b"Leaderboard" in response.content


@pytest.mark.django_db
def test_leaderboard_view_match_win_rate(client):
    today = date.today()
    tourn = Tournament.objects.create(
        id="legacy-challenge-leaderboard-test",
        name="Legacy Challenge 32",
        format="legacy",
        event_type="challenge",
        date=today,
    )
    Deck.objects.create(
        id="deck_lb_1",
        tournament=tourn,
        format="legacy",
        player="ChampPlayer",
        player_lower="champplayer",
        rank=1,
        is_top8=True,
        wins=8,
        losses=1,
        draws=0,
        archetype="Delver",
        archetype_slug="delver",
    )
    Deck.objects.create(
        id="deck_lb_2",
        tournament=tourn,
        format="legacy",
        player="RunnerUp",
        player_lower="runnerup",
        rank=2,
        is_top8=True,
        wins=7,
        losses=2,
        draws=1,
        archetype="Reanimator",
        archetype_slug="reanimator",
    )

    response = client.get("/legacy/leaderboard/")
    assert response.status_code == 200
    players = response.context["players"]
    assert len(players) >= 2
    p1 = next(p for p in players if p["player"] == "ChampPlayer")
    assert p1["match_win_rate"] == 88.9
    assert p1["challenge_record"] == "8-1"

    p2 = next(p for p in players if p["player"] == "RunnerUp")
    assert p2["match_win_rate"] == 77.8
    assert p2["challenge_record"] == "7-2-1"

    content = response.content.decode()
    assert "88.9%" in content
    assert "Match Win %" in content


@pytest.mark.django_db
def test_player_detail_win_rate_from_deck_standings(client):
    today = date.today()
    tourn = Tournament.objects.create(
        id="modern-challenge-player-test",
        name="Modern Challenge 32",
        format="modern",
        event_type="challenge",
        date=today,
    )
    Deck.objects.create(
        id="deck_p_standings",
        tournament=tourn,
        format="modern",
        player="StandingsAce",
        player_lower="standingsace",
        rank=1,
        is_top8=True,
        wins=9,
        losses=1,
        draws=0,
        points=27,
        omwp=0.67,
        archetype="Murktide",
        archetype_slug="murktide",
    )
    response = client.get("/player/StandingsAce/")
    assert response.status_code == 200
    ctx = response.context
    assert ctx["chall_match_wins"] == 9
    assert ctx["chall_matches_count"] == 10
    assert ctx["chall_win_rate"] == 90.0
    content = response.content.decode()
    assert "90.0%" in content


@pytest.mark.django_db
def test_deck_detail_missing_knn_index_warning(client, settings):
    set_global_knn_index(None, loaded=False)
    settings.KNN_INDEX_PATH = "/tmp/non_existent_knn_index.npz"

    t = Tournament.objects.create(
        id="tourn_knn_warn",
        name="Modern Event",
        format="modern",
        date=date.today(),
    )
    deck = Deck.objects.create(
        id="deck_knn_warn",
        tournament=t,
        format="modern",
        player="KnnTester",
        player_lower="knntester",
        mainboard=[{"card": "Lightning Bolt", "count": 4}],
        sideboard=[],
    )
    with warnings.catch_warnings(record=True) as recorded:
        warnings.simplefilter("always")
        response = client.get(f"/player/{deck.player}/deck/{deck.tournament_id}/")
        assert response.status_code == 200
        assert any("kNN index file not found" in str(w.message) for w in recorded)
        assert b"Mainboard" in response.content


@pytest.mark.django_db
def test_deck_detail_banned_card_badge(client):
    t = Tournament.objects.create(
        id="tourn_banned_badge",
        name="Modern Event",
        format="modern",
        date=date.today(),
    )
    deck = Deck.objects.create(
        id="deck_banned_badge",
        tournament=t,
        format="modern",
        player="Univerce",
        player_lower="univerce",
        illegal_cards=["Fury"],
        mainboard=[{"card": "Fury", "count": 4}],
        sideboard=[],
    )
    response = client.get(f"/player/{deck.player}/deck/{deck.tournament_id}/1/")
    assert response.status_code == 200
    content = response.content.decode()
    assert "Banned" in content
    assert "Historical / Not Legal Today" not in content
    assert "Banned / Illegal cards in current" not in content


@pytest.mark.django_db
def test_deck_detail_restricted_badge(client):
    Card.objects.create(
        id="test-card-fantasticar",
        oracle_id="test-oracle-fantasticar",
        name="The Fantasticar",
        normalized_name="the fantasticar",
        slug="the-fantasticar",
        legalities={"vintage": "restricted", "legacy": "banned"},
    )
    t = Tournament.objects.create(
        id="tourn_restricted_badge",
        name="Vintage Event",
        format="vintage",
        date=date.today(),
    )
    deck = Deck.objects.create(
        id="deck_restricted_badge",
        tournament=t,
        format="vintage",
        player="Capitano_CL",
        player_lower="capitano_cl",
        illegal_cards=["The Fantasticar"],
        mainboard=[{"card": "The Fantasticar", "count": 4}],
        sideboard=[],
    )
    response = client.get(f"/player/{deck.player}/deck/{deck.tournament_id}/1/")
    assert response.status_code == 200
    content = response.content.decode()
    assert "Restricted" in content
    assert "Banned" not in content
    assert "bg-amber-950/40" in content


@pytest.mark.django_db
def test_deck_detail_similar_decks_mana_symbols(client):
    t = Tournament.objects.create(
        id="tourn_similar_mana",
        name="Legacy Event",
        format="legacy",
        date=date.today(),
    )
    d1 = Deck.objects.create(
        id="deck_similar_mana_1",
        tournament=t,
        format="legacy",
        player="Rexplosion",
        player_lower="rexplosion",
        archetype="Storm",
        archetype_slug="storm",
        mainboard=[{"card": "Brainstorm", "count": 4}],
        sideboard=[],
    )
    d2 = Deck.objects.create(
        id="deck_similar_mana_2",
        tournament=t,
        format="legacy",
        player="NeighborPlayer",
        player_lower="neighborplayer",
        archetype="Delver",
        archetype_slug="delver",
        colors="UR",
        color_name="Izzet",
        mainboard=[{"card": "Lightning Bolt", "count": 4}],
        sideboard=[],
    )
    mock_index = MagicMock()
    mock_index.vector_from_decklist.return_value = MagicMock()
    mock_index.query.side_effect = [
        [
            {
                "deck_id": d2.id,
                "player": "NeighborPlayer",
                "archetype": "Delver",
                "colors": "UR",
                "color_name": "Izzet",
                "format": "legacy",
                "date": date.today(),
                "raw_similarity": 0.99,
                "recency_weight": 1.0,
                "score": 0.99,
            }
        ],
        [],
    ]
    set_global_knn_index(mock_index, loaded=True)
    try:
        response = client.get(f"/player/{d1.player}/deck/{t.id}/1/")
        assert response.status_code == 200
        format_neighbors = response.context.get("format_neighbors", [])
        assert len(format_neighbors) == 1
        assert format_neighbors[0]["colors"] == "UR"
        assert format_neighbors[0]["color_name"] == "Izzet"
        assert "ms ms-u" in response.content.decode()
        assert "ms ms-r" in response.content.decode()
    finally:
        set_global_knn_index(None, loaded=False)


@pytest.mark.django_db
def test_deck_detail_similar_decks_uses_db_archetype_over_stale_knn_index(client):
    t = Tournament.objects.create(
        id="tourn_stale_test",
        name="Legacy Challenge",
        format="legacy",
        date=date(2026, 1, 1),
    )
    d1 = Deck.objects.create(
        id="tourn_stale_test_p1_1",
        tournament=t,
        format="legacy",
        player="Player1",
        player_lower="player1",
        archetype="Storm",
        archetype_slug="storm",
        mainboard=[{"card": "Brainstorm", "count": 4}],
        sideboard=[],
    )
    d2 = Deck.objects.create(
        id="tourn_stale_test_p2_1",
        tournament=t,
        format="legacy",
        player="Player2",
        player_lower="player2",
        archetype="Saga Storm",  # Updated in DB!
        archetype_slug="saga-storm",
        colors="BRG",
        color_name="Jund",
        mainboard=[{"card": "Brainstorm", "count": 4}],
        sideboard=[],
    )

    # Mock kNN index returning stale archetype in raw results
    mock_index = MagicMock()
    mock_index.vector_from_decklist.return_value = MagicMock()
    mock_index.query.side_effect = [
        [
            {
                "deck_id": d2.id,
                "player": "Player2",
                "archetype": "Old Stale Storm",  # Stale in index!
                "colors": "UB",
                "color_name": "Dimir",
                "format": "legacy",
                "date": date(2026, 1, 1),
                "raw_similarity": 0.99,
                "recency_weight": 1.0,
                "score": 0.99,
            }
        ],
        [],  # cross format
    ]

    set_global_knn_index(mock_index, loaded=True)
    try:
        response = client.get(f"/player/{d1.player}/deck/{t.id}/1/")
        assert response.status_code == 200
        format_neighbors = response.context.get("format_neighbors", [])
        assert len(format_neighbors) == 1
        # Must reflect the canonical database archetype, not the stale index value!
        assert format_neighbors[0]["archetype"] == "Saga Storm"
        assert format_neighbors[0]["colors"] == "BRG"
        assert format_neighbors[0]["color_name"] == "Jund"
    finally:
        set_global_knn_index(None, loaded=False)


@pytest.mark.django_db
def test_faq_view(client):
    response = client.get("/faq/")
    assert response.status_code == 200
    content = response.content.decode()
    assert "Frequently Asked Questions" in content
    assert "Scryfall" in content
    assert "https://github.com/andrewgioia/Mana" in content
    assert "MTG_decklistcache" in content
    assert "modometa-mtgo-data" in content
    assert "top 32 decks" in content
    assert "Last Chance Qualifiers" in content
    assert "Preliminaries" in content
    assert "Admin Panel" not in content
    assert 'id="credits-and-acknowledgments"' in content
    assert 'href="#credits-and-acknowledgments"' in content
    assert 'id="tournament-data"' in content
    assert 'id="metagame-metrics"' in content


@pytest.mark.django_db
def test_cards_list_view_external_links(client):
    response = client.get("/legacy/cards/")
    assert response.status_code == 200
    assert "cards" in response.context
    cards = response.context["cards"]
    if cards:
        first_card = cards[0]
        assert "scryfall_url" in first_card
        assert "gatherer_url" in first_card
        assert "mana_cost" in first_card
        assert "scryfall.com" in first_card["scryfall_url"]
        assert (
            "gatherer.wizards.com/Pages/Card/Details.aspx" in first_card["gatherer_url"]
        )

        content = response.content.decode()
        assert first_card["scryfall_url"] in content
        assert first_card["gatherer_url"] in content
        assert "Gatherer" in content
        assert "Scryfall" in content
        assert "cursor-help" not in content

        # Verify mana icons render when cards have a mana cost
        cards_with_cost = [c for c in cards if c.get("mana_cost")]
        if cards_with_cost:
            assert "ms ms-" in content


@pytest.mark.django_db
def test_mdfc_mana_cost_display(client):
    # 1. Test _extract_mana_cost on MDFC structure
    mdfc_item = {
        "name": "Sink into Stupor // Soporific Springs",
        "mana_cost": None,
        "card_faces": [
            {"name": "Sink into Stupor", "mana_cost": "{1}{U}{U}"},
            {"name": "Soporific Springs", "mana_cost": ""},
        ],
    }
    assert _extract_mana_cost(mdfc_item) == "{1}{U}{U}"

    tamiyo_item = {
        "name": "Tamiyo, Inquisitive Student // Tamiyo, Seasoned Scholar",
        "mana_cost": None,
        "card_faces": [
            {"name": "Tamiyo, Inquisitive Student", "mana_cost": "{U}"},
            {"name": "Tamiyo, Seasoned Scholar", "mana_cost": ""},
        ],
    }
    assert _extract_mana_cost(tamiyo_item) == "{U}"

    split_item = {
        "name": "Fire // Ice",
        "mana_cost": "{1}{R} // {1}{U}",
        "card_faces": [
            {"name": "Fire", "mana_cost": "{1}{R}"},
            {"name": "Ice", "mana_cost": "{1}{U}"},
        ],
    }
    assert _extract_mana_cost(split_item) == "{1}{R} // {1}{U}"

    dual_spell_mdfc = {
        "name": "Shaile, Dean of Radiance // Embrose, Dean of Shadow",
        "mana_cost": None,
        "card_faces": [
            {"name": "Shaile, Dean of Radiance", "mana_cost": "{1}{W}"},
            {"name": "Embrose, Dean of Shadow", "mana_cost": "{2}{B}{B}"},
        ],
    }
    assert _extract_mana_cost(dual_spell_mdfc) == "{1}{W} // {2}{B}{B}"


@pytest.mark.django_db
def test_format_overview_archetype_colors_and_card_mana_symbols(client):
    cache.clear()

    Card.objects.create(name="Force of Will", mana_cost="{3}{U}{U}")
    t = Tournament.objects.create(
        id="legacy-challenge-overview-test",
        name="Legacy Challenge Test",
        format="legacy",
        event_type="challenge",
        date=date(2024, 1, 1),
    )
    Deck.objects.create(
        id="legacy-deck-overview-test-1",
        tournament=t,
        format="legacy",
        player="PlayerOne",
        player_lower="playerone",
        archetype="Dimir Tempo",
        archetype_slug="dimir-tempo",
        colors="UB",
        color_name="Dimir",
        result="1st Place",
        is_top8=True,
        mainboard=[{"card": "Force of Will", "count": 4}],
        sideboard=[],
    )
    response = client.get("/legacy/?days=90")
    assert response.status_code == 200
    content = response.content
    assert b">Colors</th>" not in content
    # Archetype color icons
    assert b'title="Dimir"' in content
    assert b"ms-u" in content
    assert b"ms-b" in content
    # Card mana cost icons in Most Played Cards
    assert b"ms-3" in content


@pytest.mark.django_db
def test_search_index_api(client):
    t = Tournament.objects.create(
        id="tourn-search-test",
        name="Legacy Challenge",
        format="legacy",
        date=date(2024, 1, 1),
    )
    Deck.objects.create(
        id="deck-search-test",
        tournament=t,
        format="legacy",
        player="SearchHero",
        player_lower="searchhero",
        archetype="Dimir Tempo",
        archetype_slug="dimir-tempo",
        result="1st",
    )

    response = client.get("/api/search-index/")
    assert response.status_code == 200
    assert "public, max-age=3600" in response.headers.get("Cache-Control", "")

    data = response.json()
    assert "archetypes" in data
    assert "players" in data

    arch_names = [a["name"] for a in data["archetypes"]]
    assert "Dimir Tempo" in arch_names

    player_names = [p["name"] for p in data["players"]]
    assert "SearchHero" in player_names


@pytest.mark.django_db
def test_build_search_index_command(tmp_path):
    """build_search_index command generates a valid JSON file."""
    t = Tournament.objects.create(
        id="tourn-cmd-test",
        name="Vintage Challenge",
        format="vintage",
        date=date(2024, 1, 1),
    )
    Deck.objects.create(
        id="deck-cmd-test",
        tournament=t,
        format="vintage",
        player="CmdHero",
        player_lower="cmdhero",
        archetype="Oath of Druids",
        archetype_slug="oath-of-druids",
        result="1st",
    )
    dest = tmp_path / "search_index.json"
    out = StringIO()
    call_command("build_search_index", output=str(dest), stdout=out)
    assert dest.is_file()
    data = json.loads(dest.read_text())
    assert any(a["name"] == "Oath of Druids" for a in data["archetypes"])
    assert any(p["name"] == "CmdHero" for p in data["players"])


def test_search_index_serves_static_file(client, tmp_path, settings):
    """search_index view serves static file directly when available outside tests."""
    dest = tmp_path / "search_index.json"
    dest.write_text('{"archetypes":[{"name":"StaticArch"}],"players":[]}')
    settings.SEARCH_INDEX_PATH = dest
    settings.IS_TESTING = False
    response = client.get("/api/search-index/")
    assert response.status_code == 200
    assert response.json()["archetypes"][0]["name"] == "StaticArch"


@pytest.mark.django_db
def test_search_box_in_header_replaces_window_switcher(client):
    response = client.get("/legacy/")
    assert response.status_code == 200
    content = response.content.decode("utf-8")

    # Global search input must exist
    assert 'id="global-search-input"' in content
    assert 'placeholder="Search cards, archetypes &amp; players..."' in content

    # Header should not contain Window: timeframe switcher
    header_start = content.find("<header")
    header_end = content.find("</header>")
    assert header_start != -1 and header_end != -1
    header_html = content[header_start:header_end]
    assert "Window:" not in header_html
    assert "data-timeframe-toggle" not in header_html

    # Timeframe toggle is removed from sidebar and header, available inline on page
    aside_start = content.find("<aside")
    aside_end = content.find("</aside>")
    assert aside_start != -1 and aside_end != -1
    aside_html = content[aside_start:aside_end]
    assert "data-timeframe-toggle" not in aside_html
    assert "data-timeframe-select" in content


@pytest.mark.django_db
def test_active_formats_hidden_from_navigation_and_homepage(client):
    """Only active formats should appear in the left navigation and homepage format cards."""
    active_slugs = getattr(settings, "ACTIVE_FORMAT_SLUGS", settings.MODOMETA_FORMATS)
    hidden_slugs = [
        slug for slug in settings.MODOMETA_FORMATS if slug not in active_slugs
    ]

    response = client.get("/")
    assert response.status_code == 200

    # Homepage format cards
    format_slugs = [item["format_slug"] for item in response.context["format_metas"]]
    assert format_slugs == active_slugs

    # Navigation sidebar items
    nav_formats = [fmt["slug"] for fmt in response.context["MODOMETA_FORMATS"]]
    assert nav_formats == active_slugs

    # Verify HTML sidebar lists active formats and excludes hidden formats
    content = response.content.decode("utf-8")
    aside_start = content.find("<aside")
    aside_end = content.find("</aside>")
    sidebar_html = content[aside_start:aside_end]

    for slug in active_slugs:
        assert f'href="/{slug}/"' in sidebar_html

    for slug in hidden_slugs:
        assert f'href="/{slug}/"' not in sidebar_html


@pytest.mark.django_db
def test_hidden_formats_still_accessible_via_direct_url_for_search(client):
    """Formats outside active formats are accessible directly (e.g. from search results)."""
    active_slugs = getattr(settings, "ACTIVE_FORMAT_SLUGS", settings.MODOMETA_FORMATS)
    hidden_slugs = [
        slug for slug in settings.MODOMETA_FORMATS if slug not in active_slugs
    ]
    test_slug = (
        "modern"
        if "modern" in hidden_slugs
        else (hidden_slugs[0] if hidden_slugs else "modern")
    )

    # Inactive/hidden format overview should return 200 OK (not 404)
    response = client.get(f"/{test_slug}/")
    assert response.status_code == 200
    assert f"{test_slug.capitalize()} Metagame".encode() in response.content

    # The sidebar navigation on the page still only displays active formats
    nav_formats = [fmt["slug"] for fmt in response.context["MODOMETA_FORMATS"]]
    assert nav_formats == active_slugs


def test_healthz_endpoint(client):
    """Health check endpoint should return 200 OK."""
    response = client.get("/healthz")
    assert response.status_code == 200
    assert response.content == b"OK"


def test_robots_txt_endpoint(client):
    """Robots.txt endpoint should return 200 OK with plain text from template."""
    response = client.get("/robots.txt")
    assert response.status_code == 200
    assert "text/plain" in response.headers["content-type"]
    assert b"User-agent: *" in response.content
    assert b"Crawl-delay: 2" in response.content
    assert b"Disallow: /player/" in response.content
    assert b"Disallow: /*/tournaments/" in response.content
    assert b"Disallow: /card/" in response.content
    assert b"Sitemap: https://modometa.com/sitemap.xml" in response.content


def test_favicon_endpoint(client):
    """Favicon endpoint should redirect to static favicon."""
    response = client.get("/favicon.ico")
    assert response.status_code == 301
    assert "/static/img/favicon.ico" in response.headers["Location"]


def test_public_cache_decorator():
    """public_cache decorator should emit public, browser max-age, matching CDN max-age, and stale-if-error."""
    rf = RequestFactory()

    @public_cache(cdn_seconds=7200, browser_seconds=120)
    def dummy_view(request):
        return HttpResponse("ok")

    response = dummy_view(rf.get("/test-dummy/"))
    assert response.status_code == 200
    assert "public" in response.headers["Cache-Control"]
    assert "max-age=120" in response.headers["Cache-Control"]
    assert "s-maxage=7200" in response.headers["Cache-Control"]
    assert "stale-if-error=259200" in response.headers["Cache-Control"]
    assert (
        response.headers["Cloudflare-CDN-Cache-Control"]
        == "max-age=7200, stale-if-error=259200"
    )


@pytest.mark.django_db
def test_cache_headers_emitted(client):
    """Production views should emit valid Cache-Control and Cloudflare-CDN-Cache-Control headers."""
    for path in ["/", "/faq/"]:
        resp = client.get(path)
        assert resp.status_code == 200

        cache_control = resp.headers.get("Cache-Control", "")
        cf_cache = resp.headers.get("Cloudflare-CDN-Cache-Control", "")

        assert "public" in cache_control

        s_max = re.search(r"s-maxage=(\d+)", cache_control)
        max_age = re.search(r"max-age=(\d+)", cache_control)
        cf_max = re.search(r"max-age=(\d+)", cf_cache)
        stale_err = re.search(r"stale-if-error=(\d+)", cache_control)
        cf_stale = re.search(r"stale-if-error=(\d+)", cf_cache)

        assert s_max is not None, f"s-maxage missing in Cache-Control for {path}"
        assert max_age is not None, f"max-age missing in Cache-Control for {path}"
        assert cf_max is not None, (
            f"max-age missing in Cloudflare-CDN-Cache-Control for {path}"
        )
        assert stale_err is not None, (
            f"stale-if-error missing in Cache-Control for {path}"
        )
        assert cf_stale is not None, (
            f"stale-if-error missing in Cloudflare-CDN-Cache-Control for {path}"
        )

        # Cloudflare CDN TTL should mirror origin directives
        assert s_max.group(1) == cf_max.group(1)
        assert stale_err.group(1) == cf_stale.group(1)
        assert int(s_max.group(1)) > 0
        assert int(max_age.group(1)) > 0
        assert int(stale_err.group(1)) > 0


@pytest.mark.django_db
def test_tournament_detail_dynamic_cache_ttl(client):
    """Old tournaments (>30d) should have 7-day CDN TTL; recent (<=30d) should have 4h."""
    old_date = date.today() - timedelta(days=45)
    t_old = Tournament.objects.create(
        id="legacy-challenge-old-ttl",
        name="Legacy Challenge Old",
        format="legacy",
        event_type="challenge",
        date=old_date,
    )
    resp_old = client.get(f"/legacy/tournaments/{t_old.id}/")
    assert resp_old.status_code == 200
    assert "s-maxage=604800" in resp_old.headers["Cache-Control"]
    assert "stale-if-error=259200" in resp_old.headers["Cache-Control"]
    assert (
        resp_old.headers["Cloudflare-CDN-Cache-Control"]
        == "max-age=604800, stale-if-error=259200"
    )

    recent_date = date.today() - timedelta(days=5)
    t_recent = Tournament.objects.create(
        id="legacy-challenge-recent-ttl",
        name="Legacy Challenge Recent",
        format="legacy",
        event_type="challenge",
        date=recent_date,
    )
    resp_recent = client.get(f"/legacy/tournaments/{t_recent.id}/")
    assert resp_recent.status_code == 200
    assert "s-maxage=14400" in resp_recent.headers["Cache-Control"]
    assert "stale-if-error=259200" in resp_recent.headers["Cache-Control"]
    assert (
        resp_recent.headers["Cloudflare-CDN-Cache-Control"]
        == "max-age=14400, stale-if-error=259200"
    )


@pytest.mark.django_db
def test_player_detail_cache_ttl(client):
    """Player detail view should emit 24h CDN TTL (86400s)."""
    t = Tournament.objects.create(
        id="legacy-challenge-player-ttl",
        name="Legacy Challenge",
        format="legacy",
        event_type="challenge",
        date=date(2024, 1, 1),
    )
    Deck.objects.create(
        id="legacy-challenge-player-ttl_deck_1",
        tournament=t,
        format="legacy",
        player="CachingPro",
        player_lower="cachingpro",
        archetype="Burn",
        archetype_slug="burn",
    )
    resp = client.get("/player/cachingpro/")
    assert resp.status_code == 200
    assert "s-maxage=86400" in resp.headers["Cache-Control"]
    assert "stale-if-error=259200" in resp.headers["Cache-Control"]
    assert (
        resp.headers["Cloudflare-CDN-Cache-Control"]
        == "max-age=86400, stale-if-error=259200"
    )


@pytest.mark.django_db
def test_deck_detail_cache_ttl(client):
    """Deck detail view should emit 7-day CDN TTL (604800s)."""
    t = Tournament.objects.create(
        id="legacy-challenge-deck-ttl",
        name="Legacy Challenge",
        format="legacy",
        event_type="challenge",
        date=date(2024, 1, 1),
    )
    Deck.objects.create(
        id="legacy-challenge-deck-ttl_deck_1",
        tournament=t,
        format="legacy",
        player="DeckMaster",
        player_lower="deckmaster",
        archetype="Burn",
        archetype_slug="burn",
        mainboard=[{"card": "Lightning Bolt", "count": 4}],
    )
    resp = client.get(f"/player/DeckMaster/deck/{t.id}/")
    assert resp.status_code == 200
    assert "s-maxage=604800" in resp.headers["Cache-Control"]
    assert "stale-if-error=259200" in resp.headers["Cache-Control"]
    assert (
        resp.headers["Cloudflare-CDN-Cache-Control"]
        == "max-age=604800, stale-if-error=259200"
    )


@pytest.mark.django_db
def test_deck_detail_resolves_mdfc_image_uri(client):
    """Deck detail view resolves image_uri for DFC/MDFC cards listed by front face name."""
    card = Card.objects.create(
        id="test-outland-liberator-uuid",
        name="Outland Liberator // Frenzied Trapbreaker",
        normalized_name="outland liberator // frenzied trapbreaker",
        mana_cost="{1}{G}",
        cmc=2.0,
        type_line="Creature — Human Werewolf",
        image_uri="https://cards.scryfall.io/test-outland-liberator.jpg",
    )
    CardLookup.objects.create(
        lookup_name="outland liberator",
        canonical_name="Outland Liberator // Frenzied Trapbreaker",
        card=card,
        priority=10,
    )

    t = Tournament.objects.create(
        id="legacy-league-mdfc-test",
        name="Legacy League",
        format="legacy",
        event_type="league",
        date=date(2024, 7, 7),
    )
    deck = Deck.objects.create(
        id="legacy-league-mdfc-test_player_1",
        tournament=t,
        format="legacy",
        player="Moonmadness-_-",
        player_lower="moonmadness-_-",
        archetype="Maverick",
        mainboard=[{"card": "Outland Liberator", "count": 1}],
        sideboard=[],
    )

    response = client.get(f"/player/{deck.player}/deck/{t.id}/1/")
    assert response.status_code == 200

    mb_items = response.context["deck"].mainboard
    assert len(mb_items) == 1
    assert mb_items[0]["card"] == "Outland Liberator"
    assert (
        mb_items[0]["image_uri"]
        == "https://cards.scryfall.io/test-outland-liberator.jpg"
    )
    assert mb_items[0]["mana_cost"] == "{1}{G}"
    assert (
        b'data-card-image="https://cards.scryfall.io/test-outland-liberator.jpg"'
        in response.content
    )


@pytest.mark.django_db
def test_site_name_context_processor_and_title(client):
    """SITE_NAME is provided by modometa_globals and rendered in title tags."""
    response = client.get("/")
    assert response.status_code == 200
    assert response.context["SITE_NAME"] == "MODOMeta"
    # Ensure title contains the page title and SITE_NAME
    assert (
        "<title>MTGO Metagame Overview — MODOMeta</title>"
        in response.content.decode("utf-8")
    )

    # Ensure footer uses SITE_NAME
    assert "MODOMeta is unofficial Fan Content" in response.content.decode("utf-8")


def test_no_hardcoded_internal_links_in_templates():
    """Verify that templates use Django's {% url %} rather than hardcoded root paths in hrefs."""
    template_dir = Path(__file__).resolve().parent.parent / "core" / "templates"
    pattern = re.compile(r'href="/(?!/|\s)')

    violations = []
    for html_file in template_dir.glob("*.html"):
        with open(html_file, "r", encoding="utf-8") as f:
            for idx, line in enumerate(f, 1):
                if pattern.search(line):
                    violations.append(f"{html_file.name}:{idx}: {line.strip()}")

    assert not violations, "Found hardcoded internal links in templates:\n" + "\n".join(
        violations
    )


@pytest.mark.django_db
def test_format_overview_bump_chart_and_momentum(client):
    """Test 52-week bump chart SSR SVG generation and T8 Momentum calculation."""
    ref = get_reference_date()

    # Create tournaments across the last 52 weeks and prior 90-day window
    # Archetype A: Surging in recent 90d (3 T8s in last 90d, 0 in prior 90d) -> momentum +3
    # Archetype B: Declining (1 T8 in last 90d, 4 in prior 90d) -> momentum -3
    # Archetype C: Stable (2 T8s in last 90d, 2 in prior 90d) -> momentum 0

    t_recent_1 = Tournament.objects.create(
        id="t_recent_1",
        name="Pauper Challenge 1",
        format="pauper",
        event_type="challenge",
        date=ref - timedelta(days=5),
    )
    Deck.objects.create(
        id="d_recent_a1",
        tournament=t_recent_1,
        format="pauper",
        player="PlayerA",
        player_lower="playera",
        archetype="Kuldotha Red",
        archetype_slug="kuldotha-red",
        is_top8=True,
        rank=1,
        result="1st Place",
    )
    Deck.objects.create(
        id="d_recent_b1",
        tournament=t_recent_1,
        format="pauper",
        player="PlayerB",
        player_lower="playerb",
        archetype="Dimir Terror",
        archetype_slug="dimir-terror",
        is_top8=True,
        rank=2,
        result="2nd Place",
    )
    Deck.objects.create(
        id="d_recent_c1",
        tournament=t_recent_1,
        format="pauper",
        player="PlayerC",
        player_lower="playerc",
        archetype="Golgari Gardens",
        archetype_slug="golgari-gardens",
        is_top8=True,
        rank=3,
        result="3rd Place",
    )

    t_recent_2 = Tournament.objects.create(
        id="t_recent_2",
        name="Pauper Challenge 2",
        format="pauper",
        event_type="challenge",
        date=ref - timedelta(days=12),
    )
    Deck.objects.create(
        id="d_recent_a2",
        tournament=t_recent_2,
        format="pauper",
        player="PlayerA2",
        player_lower="playera2",
        archetype="Kuldotha Red",
        archetype_slug="kuldotha-red",
        is_top8=True,
        rank=1,
        result="1st Place",
    )
    Deck.objects.create(
        id="d_recent_c2",
        tournament=t_recent_2,
        format="pauper",
        player="PlayerC2",
        player_lower="playerc2",
        archetype="Golgari Gardens",
        archetype_slug="golgari-gardens",
        is_top8=True,
        rank=2,
        result="2nd Place",
    )

    t_recent_3 = Tournament.objects.create(
        id="t_recent_3",
        name="Pauper Challenge 3",
        format="pauper",
        event_type="challenge",
        date=ref - timedelta(days=20),
    )
    Deck.objects.create(
        id="d_recent_a3",
        tournament=t_recent_3,
        format="pauper",
        player="PlayerA3",
        player_lower="playera3",
        archetype="Kuldotha Red",
        archetype_slug="kuldotha-red",
        is_top8=True,
        rank=1,
        result="1st Place",
    )

    # Tournaments in prior 90-day window [ref - 180d, ref - 90d)
    t_prior_1 = Tournament.objects.create(
        id="t_prior_1",
        name="Pauper Challenge Prior 1",
        format="pauper",
        event_type="challenge",
        date=ref - timedelta(days=110),
    )
    for i in range(4):
        Deck.objects.create(
            id=f"d_prior_b{i}",
            tournament=t_prior_1,
            format="pauper",
            player=f"PlayerB_Prior_{i}",
            player_lower=f"playerb_prior_{i}",
            archetype="Dimir Terror",
            archetype_slug="dimir-terror",
            is_top8=True,
            rank=i + 1,
            result=f"{i + 1}th Place",
        )
    for i in range(2):
        Deck.objects.create(
            id=f"d_prior_c{i}",
            tournament=t_prior_1,
            format="pauper",
            player=f"PlayerC_Prior_{i}",
            player_lower=f"playerc_prior_{i}",
            archetype="Golgari Gardens",
            archetype_slug="golgari-gardens",
            is_top8=True,
            rank=i + 5,
            result=f"{i + 5}th Place",
        )

    # 1. Request format overview for pauper
    response = client.get("/pauper/?days=90")
    assert response.status_code == 200

    # 2. Check Bump Chart context
    assert "bump_chart" in response.context
    bump_chart = response.context["bump_chart"]
    assert bump_chart["has_data"] is True
    assert bump_chart["svg_width"] == 801
    assert bump_chart["svg_height"] == 184
    assert len(bump_chart["rank_lines"]) == 10
    assert len(bump_chart["month_labels"]) >= 1
    assert len(bump_chart["tracks"]) >= 1

    # Check SVG rendered in response
    content = response.content.decode("utf-8")
    assert 'class="bump-track"' in content
    assert 'class="bump-node"' in content
    assert 'viewBox="0 0 801 184"' in content
    assert ">#1</text>" not in content
    assert ">#5</text>" not in content
    if any(m.get("anchor") == "end" for m in bump_chart["month_labels"]):
        assert 'text-anchor="end"' in content
    for m in bump_chart["month_labels"]:
        if m.get("anchor") == "end":
            assert m["x"] <= bump_chart["x_end"]
        else:
            assert m["x"] + 24 <= bump_chart["svg_width"]

    # 3. Check T8 Momentum calculations in table
    arch_dict = {a["slug"]: a for a in response.context["archetypes"]}
    assert "kuldotha-red" in arch_dict
    assert "dimir-terror" in arch_dict
    assert "golgari-gardens" in arch_dict

    # Kuldotha Red: 3 recent, 0 prior => momentum +3
    assert arch_dict["kuldotha-red"]["top8_count"] == 3
    assert arch_dict["kuldotha-red"]["prev_top8_count"] == 0
    assert arch_dict["kuldotha-red"]["t8_momentum"] == 3
    assert "+3 ▲" in content

    # Dimir Terror: 1 recent, 4 prior => momentum -3
    assert arch_dict["dimir-terror"]["top8_count"] == 1
    assert arch_dict["dimir-terror"]["prev_top8_count"] == 4
    assert arch_dict["dimir-terror"]["t8_momentum"] == -3
    assert "-3 ▼" in content

    # Golgari Gardens: 2 recent, 2 prior => momentum 0
    assert arch_dict["golgari-gardens"]["top8_count"] == 2
    assert arch_dict["golgari-gardens"]["prev_top8_count"] == 2
    assert arch_dict["golgari-gardens"]["t8_momentum"] == 0
    assert "0 ▬" in content

    # 4. Check that 30d toggle recalculates momentum against prior 30d
    resp_30 = client.get("/pauper/?days=30")
    assert resp_30.status_code == 200
    arch_dict_30 = {a["slug"]: a for a in resp_30.context["archetypes"]}
    # In prior 30d window [ref-60, ref-30), there were 0 tournaments, so prior=0 for all
    assert arch_dict_30["kuldotha-red"]["prev_top8_count"] == 0
    assert arch_dict_30["kuldotha-red"]["t8_momentum"] == 3
    # Bump chart retains 52 weeks of data regardless of ?days=30
    assert resp_30.context["bump_chart"]["has_data"] is True


@pytest.mark.django_db
def test_format_bump_chart_right_edge_anchor():
    """Test that bump chart sets anchor='end' and SVG renders text-anchor='end' when month boundary is near right edge."""
    # On 2026-09-03, Sep 1 falls in week 51, where col_x + 24 > x_end
    chart = build_format_bump_chart("pauper", date(2026, 9, 3))
    end_labels = [m for m in chart["month_labels"] if m.get("anchor") == "end"]
    assert len(end_labels) == 1
    assert end_labels[0]["name"] == "Sep"
    assert end_labels[0]["x"] == chart["x_end"]

    rendered = render_to_string(
        "includes/format_bump_chart.html",
        {"bump_chart": chart, "format_name": "Pauper"},
    )
    assert 'text-anchor="end"' in rendered


@pytest.mark.django_db
def test_format_overview_unsupported_format_notification(client):
    """Unsupported formats (not in ACTIVE_FORMAT_SLUGS) display a work-in-progress banner."""
    # 1. Supported format (legacy) should NOT display notification
    resp_supported = client.get("/legacy/")
    assert resp_supported.status_code == 200
    assert resp_supported.context["is_supported_format"] is True
    assert (
        "https://github.com/davidfischer/mtg-archetypes"
        not in resp_supported.content.decode()
    )

    # 2. Unsupported format (standard) SHOULD display notification
    resp_unsupported = client.get("/standard/")
    assert resp_unsupported.status_code == 200
    assert resp_unsupported.context["is_supported_format"] is False
    content = resp_unsupported.content.decode()
    assert "https://github.com/davidfischer/mtg-archetypes" in content
    assert "Work in progress:" in content
    assert "Archetypes for Standard are a work in progress" in content

    # 3. If settings.ACTIVE_FORMAT_SLUGS is overridden to include pauper, notification disappears
    with override_settings(ACTIVE_FORMAT_SLUGS=["standard"]):
        resp_overridden = client.get("/standard/")
        assert resp_overridden.status_code == 200
        assert resp_overridden.context["is_supported_format"] is True
        assert (
            "https://github.com/davidfischer/mtg-archetypes"
            not in resp_overridden.content.decode()
        )


@pytest.mark.django_db
def test_timeframe_select_dropdown_renders_inline(client):
    """Verify inline timeframe select dropdown renders with 90d and 30d options."""
    # 1. Homepage
    resp_home = client.get("/")
    assert resp_home.status_code == 200
    home_html = resp_home.content.decode()
    assert "data-timeframe-select" in home_html
    assert 'value="30"' in home_html
    assert 'value="90"' in home_html
    assert 'value="180"' in home_html
    assert 'value="365"' in home_html
    assert "selected" in home_html

    # 2. Format overview
    resp_format = client.get("/legacy/")
    assert resp_format.status_code == 200
    format_html = resp_format.content.decode()
    assert "data-timeframe-select" in format_html

    # 3. Timeframe selection reflects in select options
    resp_30 = client.get("/legacy/?days=30")
    assert resp_30.status_code == 200
    assert (
        'value="30" class="bg-zinc-900 text-zinc-100 py-1" selected'
        in resp_30.content.decode()
    )

    resp_180 = client.get("/legacy/?days=180")
    assert resp_180.status_code == 200
    assert resp_180.context["TIMEFRAME"] == "180"
    assert resp_180.context["TIMEFRAME_QUERY_PARAM"] == "180"
    assert (
        'value="180" class="bg-zinc-900 text-zinc-100 py-1" selected'
        in resp_180.content.decode()
    )

    resp_365 = client.get("/legacy/?days=365")
    assert resp_365.status_code == 200
    assert resp_365.context["TIMEFRAME"] == "365"
    assert resp_365.context["TIMEFRAME_QUERY_PARAM"] == "365"
    assert (
        'value="365" class="bg-zinc-900 text-zinc-100 py-1" selected'
        in resp_365.content.decode()
    )


@pytest.mark.django_db
def test_timeframe_navigation_clean_urls_and_preservation(client):
    """Clean URLs by default (90d), with ?days=30/180/365 preserved when active."""
    # When visiting /legacy/ (default 90d):
    resp_default = client.get("/legacy/")
    assert resp_default.status_code == 200
    html_default = resp_default.content.decode()

    # Sidebar links should be clean
    assert 'href="/legacy/"' in html_default
    assert 'href="/legacy/tournaments/"' in html_default
    assert 'href="/legacy/cards/"' in html_default
    assert 'href="/legacy/leaderboard/"' in html_default
    assert "?days=90" not in html_default

    # When visiting /legacy/?days=30 (explicit 30d):
    resp_30 = client.get("/legacy/?days=30")
    assert resp_30.status_code == 200
    html_30 = resp_30.content.decode()

    # Sidebar and subnavigation links preserve ?days=30 (except tournaments which is all-time)
    assert 'href="/legacy/?days=30"' in html_30
    assert 'href="/legacy/tournaments/"' in html_30
    assert 'href="/legacy/cards/?days=30"' in html_30
    assert 'href="/legacy/matrix/?days=30"' in html_30
    assert 'href="/legacy/leaderboard/?days=30"' in html_30

    # When visiting /legacy/?days=180 (explicit 180d):
    resp_180 = client.get("/legacy/?days=180")
    assert resp_180.status_code == 200
    html_180 = resp_180.content.decode()
    assert 'href="/legacy/?days=180"' in html_180
    assert 'href="/legacy/matrix/?days=180"' in html_180
    assert 'href="/legacy/cards/?days=180"' in html_180

    # Cards filter pills and querystring tag behavior:
    cards_default = client.get("/legacy/cards/")
    assert cards_default.context["TIMEFRAME_QUERY_PARAM"] is None
    cards_default_html = cards_default.content.decode()
    assert 'href="/legacy/cards/"' in cards_default_html
    assert 'href="?type=challenge"' in cards_default_html
    assert 'href="?type=league"' in cards_default_html

    cards_30 = client.get("/legacy/cards/?days=30")
    assert cards_30.context["TIMEFRAME_QUERY_PARAM"] == "30"
    cards_30_html = cards_30.content.decode()
    assert 'href="/legacy/cards/?days=30"' in cards_30_html
    assert 'href="?days=30&amp;type=challenge"' in cards_30_html
    assert 'href="?days=30&amp;type=league"' in cards_30_html

    cards_180 = client.get("/legacy/cards/?days=180")
    assert cards_180.context["TIMEFRAME_QUERY_PARAM"] == "180"
    cards_180_html = cards_180.content.decode()
    assert 'href="/legacy/cards/?days=180"' in cards_180_html
    assert 'href="?days=180&amp;type=challenge"' in cards_180_html
    assert 'href="?days=180&amp;type=league"' in cards_180_html


@pytest.mark.django_db
def test_archetype_matrix_view_basic(client):
    """Verify archetype matrix route renders with breadcrumbs and template context."""
    response = client.get("/modern/matrix/")
    assert response.status_code == 200
    assert b"Modern Archetype Matrix" in response.content
    assert "archetypes" in response.context
    assert "rows" in response.context
    assert "total_matches" in response.context


@pytest.mark.django_db
def test_archetype_matrix_invalid_format(client):
    """Verify requesting matrix for invalid format returns 404."""
    response = client.get("/not-a-format/matrix/")
    assert response.status_code == 404


@pytest.mark.django_db
def test_archetype_matrix_calculations_and_coloring(client):
    """Verify pairwise match win %, game win %, coloring, and tooltips."""
    today = date.today()
    tourn = Tournament.objects.create(
        id="modern-matrix-test-event",
        name="Modern Test Challenge",
        format="modern",
        event_type="challenge",
        date=today,
    )

    deck_burn = Deck.objects.create(
        id="deck_burn_1",
        tournament=tourn,
        format="modern",
        player="BurnPlayer",
        player_lower="burnplayer",
        result="1st Place",
        archetype="Burn",
        archetype_slug="burn",
    )
    deck_delver = Deck.objects.create(
        id="deck_delver_1",
        tournament=tourn,
        format="modern",
        player="DelverPlayer",
        player_lower="delverplayer",
        result="2nd Place",
        archetype="Delver",
        archetype_slug="delver",
    )

    # Match 1: Burn wins 2-0
    Match.objects.create(
        id="match_test_1",
        tournament=tourn,
        round_name="Quarterfinals",
        round_slug="quarterfinals",
        player1="BurnPlayer",
        player2="DelverPlayer",
        player1_deck=deck_burn,
        player2_deck=deck_delver,
        player1_wins=2,
        player2_wins=0,
        draws=0,
    )
    # Match 2: Delver wins 2-1
    Match.objects.create(
        id="match_test_2",
        tournament=tourn,
        round_name="Semifinals",
        round_slug="semifinals",
        player1="BurnPlayer",
        player2="DelverPlayer",
        player1_deck=deck_burn,
        player2_deck=deck_delver,
        player1_wins=1,
        player2_wins=2,
        draws=0,
    )
    # Match 3: Burn wins 2-1
    Match.objects.create(
        id="match_test_3",
        tournament=tourn,
        round_name="Finals",
        round_slug="finals",
        player1="BurnPlayer",
        player2="DelverPlayer",
        player1_deck=deck_burn,
        player2_deck=deck_delver,
        player1_wins=2,
        player2_wins=1,
        draws=0,
    )

    resp = client.get("/modern/matrix/?days=30")
    assert resp.status_code == 200
    rows = {r["archetype_slug"]: r for r in resp.context["rows"]}
    assert "burn" in rows
    assert "delver" in rows

    burn_row = rows["burn"]
    delver_row = rows["delver"]

    # Burn cells: mirror cell against Burn, matchup cell against Delver
    burn_cells = {
        resp.context["archetypes"][idx]["slug"]: c
        for idx, c in enumerate(burn_row["cells"])
    }
    assert burn_cells["burn"]["is_mirror"] is True

    burn_vs_delver = burn_cells["delver"]
    assert burn_vs_delver["has_data"] is True
    # 2 out of 3 matches = 66.7% (green), 5 out of 8 games
    assert burn_vs_delver["matches_won"] == 2
    assert burn_vs_delver["total_matches"] == 3
    assert burn_vs_delver["match_tooltip"] == "2/3"
    assert burn_vs_delver["game_tooltip"] == "5/8"
    assert burn_vs_delver["tooltip"] == "2/3"
    assert burn_vs_delver["color_class"] == (
        "bg-emerald-500/25 text-emerald-300 border border-emerald-500/35"
    )
    assert burn_row["overall"]["color_class"] == (
        "bg-emerald-500/25 text-emerald-300 border border-emerald-500/35"
    )

    # Delver vs Burn: 1 out of 3 matches = 33.3% (deep red), 3 out of 8 games
    delver_cells = {
        resp.context["archetypes"][idx]["slug"]: c
        for idx, c in enumerate(delver_row["cells"])
    }
    delver_vs_burn = delver_cells["burn"]
    assert delver_vs_burn["has_data"] is True
    assert delver_vs_burn["matches_won"] == 1
    assert delver_vs_burn["total_matches"] == 3
    assert delver_vs_burn["match_tooltip"] == "1/3"
    assert delver_vs_burn["game_tooltip"] == "3/8"
    assert delver_vs_burn["tooltip"] == "1/3"
    assert delver_vs_burn["color_class"] == (
        "bg-red-500/25 text-red-300 border border-red-500/35"
    )
    assert delver_row["overall"]["color_class"] == (
        "bg-red-500/25 text-red-300 border border-red-500/35"
    )


def test_archetype_matrix_color_tiers():
    """Verify 5 color gradation tiers for matchup matrix win rates."""
    # > 60%: Deep green
    assert _get_matrix_color_class(60.1) == (
        "bg-emerald-500/25 text-emerald-300 border border-emerald-500/35"
    )
    assert _get_matrix_color_class(75.0) == (
        "bg-emerald-500/25 text-emerald-300 border border-emerald-500/35"
    )
    assert _get_matrix_color_class(100.0) == (
        "bg-emerald-500/25 text-emerald-300 border border-emerald-500/35"
    )

    # 55% to 60%: Green
    assert _get_matrix_color_class(60.0) == (
        "bg-emerald-500/15 text-emerald-400 border border-emerald-500/20"
    )
    assert _get_matrix_color_class(58.5) == (
        "bg-emerald-500/15 text-emerald-400 border border-emerald-500/20"
    )
    assert _get_matrix_color_class(55.1) == (
        "bg-emerald-500/15 text-emerald-400 border border-emerald-500/20"
    )

    # 45% to 55%: Neutral
    assert _get_matrix_color_class(55.0) == (
        "bg-zinc-800/40 text-zinc-300 border border-zinc-700/30"
    )
    assert _get_matrix_color_class(50.0) == (
        "bg-zinc-800/40 text-zinc-300 border border-zinc-700/30"
    )
    assert _get_matrix_color_class(45.0) == (
        "bg-zinc-800/40 text-zinc-300 border border-zinc-700/30"
    )

    # 40% to 45%: Red
    assert _get_matrix_color_class(44.9) == (
        "bg-red-500/15 text-red-400 border border-red-500/20"
    )
    assert _get_matrix_color_class(42.0) == (
        "bg-red-500/15 text-red-400 border border-red-500/20"
    )
    assert _get_matrix_color_class(40.0) == (
        "bg-red-500/15 text-red-400 border border-red-500/20"
    )

    # < 40%: Deep red
    assert _get_matrix_color_class(39.9) == (
        "bg-red-500/25 text-red-300 border border-red-500/35"
    )
    assert _get_matrix_color_class(25.0) == (
        "bg-red-500/25 text-red-300 border border-red-500/35"
    )
    assert _get_matrix_color_class(0.0) == (
        "bg-red-500/25 text-red-300 border border-red-500/35"
    )


@pytest.mark.django_db
def test_get_archetype_matrix_data_helper():
    """Verify get_archetype_matrix_data computes shared matrix stats."""
    today = date.today()
    tourn = Tournament.objects.create(
        id="test-matrix-helper-tourn",
        name="Helper Challenge",
        format="modern",
        event_type="challenge",
        date=today,
    )
    d1 = Deck.objects.create(
        id="helper_deck_1",
        tournament=tourn,
        format="modern",
        player="P1",
        player_lower="p1",
        archetype="Affinity",
        archetype_slug="affinity",
    )
    d2 = Deck.objects.create(
        id="helper_deck_2",
        tournament=tourn,
        format="modern",
        player="P2",
        player_lower="p2",
        archetype="Burn",
        archetype_slug="burn",
    )
    Match.objects.create(
        id="helper_match_1",
        tournament=tourn,
        round_name="Finals",
        round_slug="finals",
        player1="P1",
        player2="P2",
        player1_deck=d1,
        player2_deck=d2,
        player1_wins=2,
        player2_wins=1,
    )
    data = get_archetype_matrix_data("modern", today - timedelta(days=7), today)
    assert data["has_data"] is True
    assert data["total_matches"] == 1
    assert data["sorted_slugs"] == ["affinity", "burn"]
    assert data["archetype_names"] == {"affinity": "Affinity", "burn": "Burn"}
    assert data["matrix_stats"]["affinity"]["burn"]["matches_won"] == 1
    assert data["matrix_stats"]["affinity"]["burn"]["total_matches"] == 1
    assert data["matrix_stats"]["burn"]["affinity"]["matches_won"] == 0
    assert data["matrix_stats"]["burn"]["affinity"]["total_matches"] == 1


@pytest.mark.django_db
def test_archetype_matrix_alphabetical_sorting(client):
    """Verify that selected top archetypes are sorted alphabetically in rows and columns."""
    today = date.today()
    tourn = Tournament.objects.create(
        id="modern-matrix-sort-test",
        name="Modern Sort Challenge",
        format="modern",
        event_type="challenge",
        date=today,
    )
    d_zoo = Deck.objects.create(
        id="deck_zoo_1",
        tournament=tourn,
        format="modern",
        player="ZooPlayer",
        player_lower="zooplayer",
        result="1st Place",
        archetype="Zoo",
        archetype_slug="zoo",
    )
    d_burn = Deck.objects.create(
        id="deck_burn_sort",
        tournament=tourn,
        format="modern",
        player="BurnPlayer",
        player_lower="burnplayer",
        result="2nd Place",
        archetype="Burn",
        archetype_slug="burn",
    )
    d_affinity = Deck.objects.create(
        id="deck_affinity_sort",
        tournament=tourn,
        format="modern",
        player="AffinityPlayer",
        player_lower="affinityplayer",
        result="3rd Place",
        archetype="Affinity",
        archetype_slug="affinity",
    )

    # Zoo plays 2 matches against Burn and 1 against Affinity
    # Matches count: Zoo (3), Burn (2), Affinity (1)
    # Alphabetical order: Affinity, Burn, Zoo
    Match.objects.create(
        id="match_sort_1",
        tournament=tourn,
        round_name="Quarterfinals",
        round_slug="quarterfinals",
        player1="ZooPlayer",
        player2="BurnPlayer",
        player1_deck=d_zoo,
        player2_deck=d_burn,
        player1_wins=2,
        player2_wins=1,
    )
    Match.objects.create(
        id="match_sort_2",
        tournament=tourn,
        round_name="Semifinals",
        round_slug="semifinals",
        player1="ZooPlayer",
        player2="BurnPlayer",
        player1_deck=d_zoo,
        player2_deck=d_burn,
        player1_wins=2,
        player2_wins=0,
    )
    Match.objects.create(
        id="match_sort_3",
        tournament=tourn,
        round_name="Finals",
        round_slug="finals",
        player1="ZooPlayer",
        player2="AffinityPlayer",
        player1_deck=d_zoo,
        player2_deck=d_affinity,
        player1_wins=2,
        player2_wins=1,
    )

    resp = client.get("/modern/matrix/?days=30")
    assert resp.status_code == 200

    col_names = [a["name"] for a in resp.context["archetypes"]]
    row_names = [r["archetype"] for r in resp.context["rows"]]

    # Both columns and rows must be alphabetically sorted (A-Z)
    assert col_names == ["Affinity", "Burn", "Zoo"]
    assert row_names == ["Affinity", "Burn", "Zoo"]


@pytest.mark.django_db
def test_archetype_matrix_timeframe_filtering(client):
    """Verify matches outside timeframe cutoff are excluded."""
    today = date.today()
    old_date = today - timedelta(days=120)

    # Recent tournament establishes today as the reference date
    recent_tourn = Tournament.objects.create(
        id="modern-matrix-recent-event",
        name="Modern Recent Challenge",
        format="modern",
        event_type="challenge",
        date=today,
    )
    dr1 = Deck.objects.create(
        id="deck_recent_1",
        tournament=recent_tourn,
        format="modern",
        player="RecentP1",
        player_lower="recentp1",
        result="1st Place",
        archetype="RecentArch",
        archetype_slug="recent-arch",
    )
    dr2 = Deck.objects.create(
        id="deck_recent_2",
        tournament=recent_tourn,
        format="modern",
        player="RecentP2",
        player_lower="recentp2",
        result="2nd Place",
        archetype="RecentArch2",
        archetype_slug="recent-arch-2",
    )
    Match.objects.create(
        id="match_recent_1",
        tournament=recent_tourn,
        round_name="Finals",
        round_slug="finals",
        player1="RecentP1",
        player2="RecentP2",
        player1_deck=dr1,
        player2_deck=dr2,
        player1_wins=2,
        player2_wins=1,
    )

    old_tourn = Tournament.objects.create(
        id="modern-matrix-old-event",
        name="Modern Old Challenge",
        format="modern",
        event_type="challenge",
        date=old_date,
    )
    d1 = Deck.objects.create(
        id="deck_old_1",
        tournament=old_tourn,
        format="modern",
        player="OldPlayer1",
        player_lower="oldplayer1",
        result="1st Place",
        archetype="ArchetypeA",
        archetype_slug="archetype-a",
    )
    d2 = Deck.objects.create(
        id="deck_old_2",
        tournament=old_tourn,
        format="modern",
        player="OldPlayer2",
        player_lower="oldplayer2",
        result="2nd Place",
        archetype="ArchetypeB",
        archetype_slug="archetype-b",
    )
    Match.objects.create(
        id="match_old_1",
        tournament=old_tourn,
        round_name="Finals",
        round_slug="finals",
        player1="OldPlayer1",
        player2="OldPlayer2",
        player1_deck=d1,
        player2_deck=d2,
        player1_wins=2,
        player2_wins=0,
    )

    # 30 days window: should not include this match
    resp_30 = client.get("/modern/matrix/?days=30")
    assert resp_30.status_code == 200
    arch_slugs_30 = [a["slug"] for a in resp_30.context["archetypes"]]
    assert "archetype-a" not in arch_slugs_30

    # 180 days window: should include this match
    resp_180 = client.get("/modern/matrix/?days=180")
    assert resp_180.status_code == 200
    arch_slugs_180 = [a["slug"] for a in resp_180.context["archetypes"]]
    assert "archetype-a" in arch_slugs_180


@pytest.mark.django_db
def test_intcomma_formatting_in_views(client):
    """Verify that intcomma filter formats counts with thousands separators."""
    t = Tournament.objects.create(
        id="tourn_intcomma",
        format="modern",
        name="Modern Challenge 32",
        event_type="challenge",
        date=date.today(),
        player_count=1234,
    )
    Deck.objects.create(
        id="deck_intcomma",
        tournament=t,
        format="modern",
        player="CommaPlayer",
        player_lower="commaplayer",
        result="1st Place",
        archetype="BigDeck",
        archetype_slug="bigdeck",
    )

    # Tournament list: player_count and deck_count
    resp = client.get("/modern/tournaments/")
    assert resp.status_code == 200
    html = resp.content.decode()
    assert "1,234" in html

    # Tournament detail: player_count
    resp = client.get(f"/modern/tournaments/{t.id}/")
    assert resp.status_code == 200
    assert "1,234 Players" in resp.content.decode()


@pytest.mark.django_db
def test_custom_404_page(client):
    """Verify that 404 responses render the custom 404 page with MTG humor and site guides."""
    with override_settings(DEBUG=False):
        response = client.get("/this-route-does-not-exist-anywhere/")
        assert response.status_code == 404
        assert "404.html" in [t.name for t in response.templates]
        content = response.content.decode()

        # Base template derivation & cheeky MTG theme
        assert "Mulligan to Zero" in content
        assert "Not Found" in content
        assert "The page you are looking for is gone" in content
        assert "A mulligan won't help" in content

        # Invalid format path also renders the custom 404
        resp_fmt = client.get("/not-a-format/")
        assert resp_fmt.status_code == 404
        assert "404.html" in [t.name for t in resp_fmt.templates]
        assert "/not-a-format/" in resp_fmt.content.decode()
