"""Build the configured search provider without coupling the research core to a vendor."""

from collections.abc import Callable

from backend.core.config import ConfigError, Settings
from backend.research.repository import ResearchRepository
from backend.research.search import SearchProvider
from backend.research.search_budget import BudgetedSearchProvider, repository_usage_counter


def create_search_provider(
    settings: Settings,
    *,
    repository: ResearchRepository | None = None,
    usage_counter: Callable[[], int] | None = None,
) -> SearchProvider | None:
    """Return the budget-guarded provider, or ``None`` when search is off (the default).

    A paid provider is never returned without a usage counter (given directly or derived
    from ``repository``): the factory fails closed rather than skip the monthly guard.
    """
    if settings.search_provider == "none":
        return None
    if settings.search_provider == "tavily":
        if usage_counter is None:
            if repository is None:
                raise ConfigError("A search usage counter or research repository is required")
            usage_counter = repository_usage_counter(repository)
        try:
            from backend.research.tavily import TavilySearchProvider
        except ImportError as exc:
            raise ConfigError("Install httpx to use the Tavily search provider") from exc
        return BudgetedSearchProvider(
            TavilySearchProvider(settings.search_key or ""),
            monthly_limit=settings.search_monthly_limit,
            usage_counter=usage_counter,
        )
    raise ConfigError(f"Unsupported search provider: {settings.search_provider}")
