"""Provider registry.

Built once at startup from settings. Only providers that are actually
configured are registered, so "you have no Anthropic key" surfaces as a clear
400 at the API boundary rather than an authentication error thrown from inside
a vendor SDK halfway through a stream.
"""

from __future__ import annotations

from app.core.config import Settings
from app.core.errors import ModelNotSupportedError, ProviderNotConfiguredError
from app.core.logging import get_logger
from app.providers.anthropic_provider import AnthropicProvider
from app.providers.base import BaseProvider
from app.providers.mock import MockProvider
from app.providers.openai_compatible import (
    GeminiProvider,
    GroqProvider,
    OllamaProvider,
)
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
            # OpenAI-compatible backends. Each keeps its own registry name so
            # the dashboard can tell their traffic apart -- sharing `openai`
            # would collapse them in every panel, which is the one thing a
            # multi-provider observability tool must not do.
            GroqProvider(
                api_key=settings.groq_api_key,
                base_url=settings.groq_base_url,
                default_model=settings.default_groq_model,
            ),
            GeminiProvider(
                api_key=settings.gemini_api_key,
                base_url=settings.gemini_base_url,
                default_model=settings.default_gemini_model,
            ),
        ]

        if settings.ollama_enabled:
            candidates.append(
                OllamaProvider(
                    base_url=settings.ollama_base_url,
                    default_model=settings.default_ollama_model,
                )
            )

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
        """Resolve (provider, model), applying defaults and validating both."""
        provider = self.get(provider_name)
        resolved = model or provider.default_model()

        if not provider.supports_model(resolved):
            # Caught here rather than left to the provider, so the error names
            # what is actually available instead of surfacing as a confusingly
            # normal-looking response from whichever provider was defaulted to.
            raise ModelNotSupportedError(
                f"Provider {provider.name!r} does not serve model {resolved!r}",
                provider=provider.name,
                requested_model=resolved,
                available_models=provider.supported_models(),
            )

        return provider.name, resolved
