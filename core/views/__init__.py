"""Views package for Modometa metagame analyzer."""

from core.views.charts import ARCHETYPE_COLOR_AFFINITIES
from core.views.charts import FALLBACK_PALETTE
from core.views.charts import _get_matrix_color_class
from core.views.charts import assign_archetype_colors
from core.views.charts import build_activity_heatmap_grid
from core.views.charts import build_archetype_heatmap
from core.views.charts import build_challenge_archetype_chart
from core.views.charts import build_format_bump_chart
from core.views.charts import build_player_heatmap
from core.views.charts import build_tournament_archetype_chart
from core.views.charts import build_tournament_list_archetype_chart
from core.views.charts import get_activity_heatmap_date_range
from core.views.charts import get_archetype_matrix_data
from core.views.charts import tournament_deck_sort_key
from core.views.opengraph import _format_og_chart_row
from core.views.opengraph import _get_og_matrix_cell_style
from core.views.opengraph import archetype_og_image
from core.views.opengraph import build_mana_pill
from core.views.opengraph import build_matrix_og_data
from core.views.opengraph import card_og_image
from core.views.opengraph import deck_og_image
from core.views.opengraph import default_og_image
from core.views.opengraph import format_matrix_og_image
from core.views.opengraph import format_og_image
from core.views.opengraph import player_og_image
from core.views.opengraph import render_og_png
from core.views.opengraph import tournament_list_og_image
from core.views.opengraph import tournament_og_image
from core.views.utils import CARD_TYPE_CATEGORIES
from core.views.utils import DEFAULT_TIMEFRAME
from core.views.utils import VALID_TIMEFRAMES
from core.views.utils import build_deck_mainboard_sections
from core.views.utils import classify_card_type
from core.views.utils import get_card_legality_badge
from core.views.utils import get_cards_map
from core.views.utils import get_dataset_min_date
from core.views.utils import get_dataset_start_year
from core.views.utils import get_format_card_stats
from core.views.utils import get_reference_date
from core.views.utils import get_timeframe_cutoff
from core.views.utils import parse_timeframe
from core.views.utils import public_cache
from core.views.views import archetype_detail
from core.views.views import archetype_matrix
from core.views.views import card_detail
from core.views.views import cards_list
from core.views.views import deck_detail
from core.views.views import faq
from core.views.views import format_overview
from core.views.views import home
from core.views.views import leaderboard
from core.views.views import player_detail
from core.views.views import search_index
from core.views.views import tournament_detail
from core.views.views import tournament_list


__all__ = [
    "ARCHETYPE_COLOR_AFFINITIES",
    "CARD_TYPE_CATEGORIES",
    "DEFAULT_TIMEFRAME",
    "FALLBACK_PALETTE",
    "VALID_TIMEFRAMES",
    "_format_og_chart_row",
    "_get_matrix_color_class",
    "_get_og_matrix_cell_style",
    "archetype_detail",
    "archetype_matrix",
    "archetype_og_image",
    "assign_archetype_colors",
    "build_activity_heatmap_grid",
    "build_archetype_heatmap",
    "build_challenge_archetype_chart",
    "build_deck_mainboard_sections",
    "build_format_bump_chart",
    "build_mana_pill",
    "build_matrix_og_data",
    "build_player_heatmap",
    "build_tournament_archetype_chart",
    "build_tournament_list_archetype_chart",
    "card_detail",
    "card_og_image",
    "cards_list",
    "classify_card_type",
    "deck_detail",
    "deck_og_image",
    "default_og_image",
    "faq",
    "format_matrix_og_image",
    "format_og_image",
    "format_overview",
    "get_activity_heatmap_date_range",
    "get_archetype_matrix_data",
    "get_card_legality_badge",
    "get_cards_map",
    "get_dataset_min_date",
    "get_dataset_start_year",
    "get_format_card_stats",
    "get_reference_date",
    "get_timeframe_cutoff",
    "home",
    "leaderboard",
    "parse_timeframe",
    "player_detail",
    "player_og_image",
    "public_cache",
    "render_og_png",
    "search_index",
    "tournament_deck_sort_key",
    "tournament_detail",
    "tournament_list",
    "tournament_list_og_image",
    "tournament_og_image",
]
