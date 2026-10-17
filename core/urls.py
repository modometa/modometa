from django.urls import path

from . import views


app_name = "core"

urlpatterns = [
    # Home & FAQ
    path("", views.home, name="home"),
    path("faq/", views.faq, name="faq"),
    # Open Graph dynamic images
    path("og.png", views.default_og_image, name="default_og_image"),
    # Player views
    path("player/<str:player>/og.png", views.player_og_image, name="player_og_image"),
    path("player/<str:player>/", views.player_detail, name="player_detail"),
    path(
        "player/<str:player>/deck/<str:event>/og.png",
        views.deck_og_image,
        name="deck_og_image",
    ),
    path(
        "player/<str:player>/deck/<str:event>/<int:deck_index>/og.png",
        views.deck_og_image,
        name="deck_og_image_disambiguated",
    ),
    path(
        "player/<str:player>/deck/<str:event>/", views.deck_detail, name="deck_detail"
    ),
    path(
        "player/<str:player>/deck/<str:event>/<int:deck_index>/",
        views.deck_detail,
        name="deck_detail_disambiguated",
    ),
    # Card views
    path("card/<path:card>/og.png", views.card_og_image, name="card_og_image"),
    path("card/<slug:slug>/", views.card_detail, name="card_detail"),
    # Search API
    path("api/search-index/", views.search_index, name="search_index"),
    # Format views
    path("<str:format>/og.png", views.format_og_image, name="format_og_image"),
    path(
        "<str:format>/matrix/og.png",
        views.format_matrix_og_image,
        name="format_matrix_og_image",
    ),
    path(
        "<str:format>/archetype/<str:archetype>/og.png",
        views.archetype_og_image,
        name="archetype_og_image",
    ),
    path("<str:format>/", views.format_overview, name="format_overview"),
    path("<str:format>/tournaments/", views.tournament_list, name="tournament_list"),
    path(
        "<str:format>/tournaments/og.png",
        views.tournament_list_og_image,
        name="tournament_list_og_image",
    ),
    path(
        "<str:format>/tournaments/<str:event>/",
        views.tournament_detail,
        name="tournament_detail",
    ),
    path(
        "<str:format>/tournaments/<str:event>/og.png",
        views.tournament_og_image,
        name="tournament_og_image",
    ),
    path(
        "<str:format>/archetype/<str:archetype>/",
        views.archetype_detail,
        name="archetype_detail",
    ),
    path("<str:format>/cards/", views.cards_list, name="cards_list"),
    path("<str:format>/matrix/", views.archetype_matrix, name="archetype_matrix"),
    path("<str:format>/leaderboard/", views.leaderboard, name="leaderboard"),
]
