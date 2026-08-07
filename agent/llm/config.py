####
## LLM Routing Config Loader for Agentic Data Quality Triage
## Author: Mario Caesar // hello@caesarmar.io // https://caesarmar.io/
####

# --- Importing Libraries
from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, Field, field_validator, model_validator


# --- Configuring Project Path
PROJECT_ROOT = Path(__file__).resolve().parents[2]

# Add repo root before importing project packages when this file is executed by path.
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from pipelines.common.logging import logger


# --- Defining Constants
DEFAULT_MODEL_ROUTING_CONFIG_PATH = PROJECT_ROOT / "configs" / "agent" / "model_routing.yml"

ProviderType = Literal["heuristic", "openai_compatible"]
ReasoningTier = Literal["none", "cheap", "mid", "strong"]
RiskTier = Literal["low", "medium", "high"]
StructuredOutputMode = Literal["off", "preferred"]


# --- Defining Classes
class ProviderConfig(BaseModel):
    """
    Runtime provider profile for one LLM-compatible backend.

    Attributes:
        provider_type: Provider implementation type.
        enabled: Whether the provider is allowed for routing.
        default_model: Default model when route and environment do not override it.
        api_key_env: Environment variable that stores the provider API key.
        base_url_env: Environment variable that stores an OpenAI-compatible base URL.
        model_env: Environment variable that can override the selected model.
    """

    provider_type: ProviderType
    enabled: bool              = True
    default_model: str         = "heuristic-v1"
    api_key_env: str           = ""
    base_url_env: str          = ""
    model_env: str             = ""


class RouteConfig(BaseModel):
    """
    Cost-aware model route for one agent task.

    Attributes:
        description: Human-readable purpose of the route.
        provider: Provider key from the providers mapping.
        model: Default model for this route.
        reasoning_tier: Relative reasoning capability needed by the task.
        risk_tier: Operational risk of the task output.
        temperature: Sampling temperature.
        max_output_tokens: Maximum output token budget.
        input_cost_per_1m_tokens: Estimated input token price.
        output_cost_per_1m_tokens: Estimated output token price.
        fallback_route: Route used when the selected provider is unavailable.
        structured_output_mode: Whether this route may prefer caller-supplied JSON schema output.
    """

    description: str                         = ""
    provider: str
    model: str                               = ""
    reasoning_tier: ReasoningTier            = "cheap"
    risk_tier: RiskTier                      = "low"
    temperature: float                       = Field(default=0.0, ge=0.0, le=2.0)
    max_output_tokens: int                   = Field(default=700, ge=1, le=16000)
    input_cost_per_1m_tokens: float          = Field(default=0.0, ge=0.0)
    output_cost_per_1m_tokens: float         = Field(default=0.0, ge=0.0)
    fallback_route: str                      = ""
    structured_output_mode: StructuredOutputMode = "off"


class ResolvedRoute(BaseModel):
    """
    Fully resolved route after environment variables and provider defaults are applied.

    Attributes:
        route_name: Route key from the routing config.
        route: Route configuration.
        provider_name: Provider key.
        provider: Provider configuration.
        model: Final model name.
        api_key: API key value, never persisted or printed.
        base_url: Optional OpenAI-compatible base URL.
        use_heuristic: Whether execution should use local heuristic fallback.
        fallback_reason: Explanation for heuristic fallback.
    """

    route_name: str
    route: RouteConfig
    provider_name: str
    provider: ProviderConfig
    model: str
    api_key: str                 = ""
    base_url: str                = ""
    use_heuristic: bool          = False
    fallback_reason: str         = ""


class ModelRoutingConfig(BaseModel):
    """
    Full LLM routing configuration.

    Attributes:
        default_route: Route used when no explicit route is requested.
        currency: Currency label used for cost estimates.
        providers: Provider profiles keyed by provider name.
        routes: Task routes keyed by route name.
    """

    default_route: str
    currency: str                           = "USD"
    providers: dict[str, ProviderConfig]
    routes: dict[str, RouteConfig]

    @field_validator("providers", "routes")
    @classmethod
    def validate_non_empty_mapping(cls, value: dict[str, object]) -> dict[str, object]:
        """
        Validate that required mappings are not empty.

        Args:
            value: Provider or route mapping.

        Returns:
            Original mapping when non-empty.

        Raises:
            ValueError: If the mapping is empty.
        """
        if not value:
            raise ValueError("providers and routes must not be empty")

        return value

    @model_validator(mode="after")
    def validate_references(self) -> "ModelRoutingConfig":
        """
        Validate route/provider/fallback references.

        Returns:
            Current config when references are valid.

        Raises:
            ValueError: If a route references an unknown provider or fallback route.
        """
        if self.default_route not in self.routes:
            raise ValueError(f"default_route is unknown: {self.default_route}")

        for route_name, route in self.routes.items():
            if route.provider not in self.providers:
                raise ValueError(f"Route {route_name} references unknown provider: {route.provider}")

            if route.fallback_route and route.fallback_route not in self.routes:
                raise ValueError(f"Route {route_name} references unknown fallback_route: {route.fallback_route}")

        return self


# --- Defining Functions
def resolve_model_routing_config_path(config_path: str | Path | None = None) -> Path:
    """
    Resolve the model routing config path from an explicit value or environment variable.

    Args:
        config_path: Optional explicit config path.

    Returns:
        Absolute path to the model routing YAML file.
    """
    raw_path = config_path or os.getenv("LLM_ROUTING_CONFIG_PATH") or DEFAULT_MODEL_ROUTING_CONFIG_PATH
    path     = Path(raw_path)

    if not path.is_absolute():
        path = PROJECT_ROOT / path

    logger.info("Resolved model routing config path | path=%s", path)

    return path


def load_model_routing_config(config_path: str | Path | None = None) -> ModelRoutingConfig:
    """
    Load and validate the model routing YAML file.

    Args:
        config_path: Optional explicit config path.

    Returns:
        Validated ModelRoutingConfig instance.

    Raises:
        FileNotFoundError: If the config file is missing.
        ValueError: If the YAML content is invalid.
    """
    path = resolve_model_routing_config_path(config_path=config_path)

    logger.info("Loading model routing config | path=%s", path)

    with path.open("r", encoding="utf-8") as file:
        raw_config = yaml.safe_load(file) or {}

    config = ModelRoutingConfig.model_validate(raw_config)

    logger.info(
        "Model routing config loaded | providers=%d routes=%d default_route=%s",
        len(config.providers),
        len(config.routes),
        config.default_route,
    )

    return config


def getenv_from_config(env_name: str) -> str:
    """
    Read an environment variable only when the configured variable name is present.

    Args:
        env_name: Environment variable name.

    Returns:
        Environment value or an empty string.
    """
    if not env_name:
        return ""

    return os.getenv(env_name, "").strip()


def resolve_route(
    route_name: str | None = None,
    config: ModelRoutingConfig | None = None,
    config_path: str | Path | None = None,
    force_heuristic: bool = False,
) -> ResolvedRoute:
    """
    Resolve a model route with provider settings and environment overrides.

    Args:
        route_name: Optional route key. Defaults to config.default_route.
        config: Optional already-loaded routing config.
        config_path: Optional YAML path when config is not provided.
        force_heuristic: Force local heuristic execution regardless of provider availability.

    Returns:
        ResolvedRoute ready for the LLM client.

    Raises:
        ValueError: If the route or provider cannot be resolved.
    """
    routing_config = config or load_model_routing_config(config_path=config_path)
    selected_name  = route_name or routing_config.default_route

    if selected_name not in routing_config.routes:
        raise ValueError(f"Unknown model route: {selected_name}")

    route         = routing_config.routes[selected_name]
    provider_name = route.provider
    provider      = routing_config.providers[provider_name]
    model         = getenv_from_config(provider.model_env) or route.model or provider.default_model
    api_key       = getenv_from_config(provider.api_key_env)
    base_url      = getenv_from_config(provider.base_url_env)

    use_heuristic   = force_heuristic or provider.provider_type == "heuristic" or not provider.enabled
    fallback_reason = ""

    if force_heuristic:
        fallback_reason = "forced_heuristic"

    elif provider.provider_type == "heuristic":
        fallback_reason = "heuristic_provider"

    elif not provider.enabled:
        fallback_reason = f"provider_disabled:{provider_name}"

    elif not use_heuristic and provider.provider_type == "openai_compatible" and not api_key:
        use_heuristic   = True
        fallback_reason = f"missing_api_key:{provider.api_key_env}"

    resolved = ResolvedRoute(
        route_name=selected_name,
        route=route,
        provider_name=provider_name,
        provider=provider,
        model=model,
        api_key=api_key,
        base_url=base_url,
        use_heuristic=use_heuristic,
        fallback_reason=fallback_reason,
    )

    logger.info(
        "Resolved model route | route=%s provider=%s model=%s heuristic=%s reason=%s",
        resolved.route_name,
        resolved.provider_name,
        resolved.model,
        resolved.use_heuristic,
        resolved.fallback_reason,
    )

    return resolved


def resolve_executable_route(
    route_name: str | None = None,
    config: ModelRoutingConfig | None = None,
    config_path: str | Path | None = None,
    force_heuristic: bool = False,
    max_hops: int = 5,
) -> ResolvedRoute:
    """
    Resolve the first executable route, following fallback routes when needed.

    Args:
        route_name: Optional route key. Defaults to config.default_route.
        config: Optional already-loaded routing config.
        config_path: Optional YAML path when config is not provided.
        force_heuristic: Force local heuristic execution regardless of provider availability.
        max_hops: Maximum fallback hops before stopping.

    Returns:
        ResolvedRoute for either an available provider or a deterministic heuristic route.

    Raises:
        ValueError: If fallback routing loops or exceeds max_hops.
    """
    routing_config = config or load_model_routing_config(config_path=config_path)
    selected_name  = route_name or routing_config.default_route
    visited        = set()

    for _ in range(max_hops):
        if selected_name in visited:
            raise ValueError(f"Model route fallback loop detected: {selected_name}")

        visited.add(selected_name)

        resolved = resolve_route(
            route_name=selected_name,
            config=routing_config,
            force_heuristic=force_heuristic,
        )

        if not resolved.use_heuristic or resolved.provider.provider_type == "heuristic":
            return resolved

        fallback_route = resolved.route.fallback_route

        if not fallback_route or fallback_route == selected_name:
            logger.info("No further executable fallback route found | route=%s", selected_name)

            return resolved

        logger.info(
            "Following model route fallback | current_route=%s fallback_route=%s reason=%s",
            selected_name,
            fallback_route,
            resolved.fallback_reason,
        )
        selected_name = fallback_route

    raise ValueError(f"Model route fallback exceeded max_hops={max_hops}: {route_name}")
