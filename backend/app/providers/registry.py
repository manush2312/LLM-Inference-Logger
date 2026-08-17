"""Provider registry.

Built once at startup from settings. Only providers that are actually
configured are registered, so "you have no Anthropic key" surfaces as a clear
400 at the API boundary rather than an authentication error thrown from inside
a vendor SDK halfway through a stream.
"""

from __future__ import annotations

from app.core.config import Settings
from app.core.errors import ProviderNotConfiguredError
from app.core.logging import get_logger
from app.providers.anthropic_provider import AnthropicProvider
from app.providers.base import BaseProvider
from app.providers.mock import MockProvider
from app.providers.openai_provider import OpenAIProvider

log = get_logger(__name__)


class ProviderRegistry:
    def __init__(self, providers: dict[str, BaseProvider], default: str) -> None:
        self._providers = providers
        self._default = default

    @classmethod
    def from_settings(cls, settings: Settings) -> ProviderRegistry:
        candidates: list[BaseProvider] = [
            # Always available: no key, no network, no cost. Keeps the system
            # demonstrable and testable on a fresh clone.
            MockProvider(),
            AnthropicProvider(settings.anthropic_api_key, settings.default_anthropic_model),
            OpenAIProvider(settings.openai_api_key, settings.default_openai_model),
        ]

        providers = {p.name: p for p in candidates if p.is_configured()}
        skipped = [p.name for p in candidates if not p.is_configured()]

        default = settings.default_provider
        if default not in providers:
            log.warning(
                "default_provider_unavailable",
                requested=default,
                falling_back_to=MockProvider.name,
                available=sorted(providers),
            )
            default = MockProvider.name

        log.info("providers_registered", available=sorted(providers), skipped=skipped)
        return cls(providers, default)

    @property
    def default_name(self) -> str:
        return self._default

    def available(self) -> list[str]:
        return sorted(self._providers)

    def get(self, name: str | None = None) -> BaseProvider:
        resolved = name or self._default
        provider = self._providers.get(resolved)
        if provider is None:
            raise ProviderNotConfiguredError(
                f"Provider '{resolved}' is not available",
                requested=resolved,
                available=self.available(),
            )
        return provider

    def resolve_model(self, provider_name: str | None, model: str | None) -> tuple[str, str]:
        """Resolve (provider, model), applying defaults. Validates the provider."""
        provider = self.get(provider_name)
        return provider.name, model or provider.default_model()
