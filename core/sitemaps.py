"""Sitemaps for search engine discovery of high-value metagame pages."""

from django.conf import settings
from django.contrib.sitemaps import Sitemap
from django.urls import reverse

from core.models.deck import Deck


class StaticViewSitemap(Sitemap):
    """Sitemap for static core pages."""

    protocol = "https"

    def items(self) -> list[str]:
        return ["core:home", "core:faq"]

    def location(self, item: str) -> str:
        return reverse(item)

    def priority(self, item: str) -> float:
        return 1.0 if item == "core:home" else 0.5

    def changefreq(self, item: str) -> str:
        return "daily" if item == "core:home" else "monthly"


class FormatOverviewSitemap(Sitemap):
    """Sitemap for active format hubs, matchup matrices, and leaderboards."""

    protocol = "https"

    def items(self) -> list[tuple[str, str]]:
        active_slugs = getattr(
            settings, "ACTIVE_FORMAT_SLUGS", settings.MODOMETA_FORMATS
        )
        items = []
        for fmt in active_slugs:
            items.append(("core:format_overview", fmt))
            items.append(("core:archetype_matrix", fmt))
            items.append(("core:leaderboard", fmt))
        return items

    def location(self, item: tuple[str, str]) -> str:
        view_name, fmt = item
        return reverse(view_name, kwargs={"format": fmt})

    def priority(self, item: tuple[str, str]) -> float:
        view_name, _ = item
        if view_name == "core:format_overview":
            return 0.9
        elif view_name == "core:archetype_matrix":
            return 0.8
        return 0.7

    def changefreq(self, item: tuple[str, str]) -> str:
        return "daily"


class ArchetypeSitemap(Sitemap):
    """Sitemap for active metagame archetypes across formats."""

    protocol = "https"
    priority = 0.7
    changefreq = "weekly"

    def items(self) -> list[tuple[str, str]]:
        active_slugs = getattr(
            settings, "ACTIVE_FORMAT_SLUGS", settings.MODOMETA_FORMATS
        )
        return list(
            Deck.objects.filter(format__in=active_slugs)
            .exclude(archetype_slug="")
            .order_by()
            .values_list("format", "archetype_slug")
            .distinct()
        )

    def location(self, item: tuple[str, str]) -> str:
        fmt, slug = item
        return reverse(
            "core:archetype_detail", kwargs={"format": fmt, "archetype": slug}
        )


sitemaps = {
    "static": StaticViewSitemap,
    "formats": FormatOverviewSitemap,
    "archetypes": ArchetypeSitemap,
}
