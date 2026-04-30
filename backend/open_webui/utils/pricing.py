"""
Model pricing utilities for cost tracking.

Fetches pricing data from models.dev and calculates costs based on token usage.
Uses fuzzy model ID matching to handle different naming conventions across providers.
"""

import json
import logging
import re
import time
from decimal import Decimal, ROUND_HALF_UP
from typing import Optional
from urllib.request import Request, urlopen
from urllib.error import URLError, HTTPError

log = logging.getLogger(__name__)

# Pricing source
MODELS_DEV_URL = "https://models.dev/api.json"

# Cache configuration
PRICING_CACHE_TTL_SECONDS = 6 * 60 * 60  # 6 hours
FETCH_TIMEOUT_SECONDS = 15

# Constants
ONE_MILLION = Decimal("1000000")
DISPLAY_QUANTIZE = Decimal("0.00000001")

# In-memory cache
_pricing_cache: dict = {}
_pricing_cache_timestamp: float = 0
# Fuzzy lookup index: maps stripped model name -> list of (provider_id, model_id, cost)
_fuzzy_index: dict[str, list[dict]] = {}


def _to_alphanum(value: str) -> str:
    """Strip everything except lowercase alphanumeric chars for fuzzy matching."""
    return re.sub(r'[^a-z0-9]', '', value.lower())


def _extract_model_name(model_id: str) -> str:
    """
    Extract the bare model name from an OpenWebUI model ID.

    Handles formats like:
        "openrouter.anthropic/claude-opus-4.5" -> "claude-opus-4.5"
        "openai/gpt-4o" -> "gpt-4o"
        "gpt-4o" -> "gpt-4o"
        "connection:model-name" -> "model-name"
    """
    # Strip connection.provider/ prefix
    if "/" in model_id:
        model_id = model_id.split("/")[-1]
    # Strip colon prefix
    if ":" in model_id:
        model_id = model_id.split(":")[-1]
    return model_id.strip()


def _extract_provider_hint(model_id: str, owned_by: Optional[str] = None) -> Optional[str]:
    """
    Extract provider hint from model ID or owned_by field.

    "openrouter.anthropic/claude-opus-4.5" -> "anthropic"
    "openai/gpt-4o" -> "openai"
    """
    if owned_by:
        return owned_by.strip().lower()

    if "/" in model_id:
        prefix = model_id.split("/")[0]
        # Handle "connection.provider" format
        if "." in prefix:
            return prefix.split(".")[-1].strip().lower()
        return prefix.strip().lower()

    return None


def _build_fuzzy_index(catalog: dict) -> dict[str, list[dict]]:
    """
    Build a fuzzy lookup index from the models.dev catalog.

    Maps stripped alphanumeric model names to their catalog entries.
    This makes matching resilient to dots vs hyphens vs underscores etc.
    """
    index: dict[str, list[dict]] = {}

    for provider_id, provider in catalog.items():
        if not isinstance(provider, dict):
            continue
        models = provider.get("models")
        if not isinstance(models, dict):
            continue

        for model_id, model in models.items():
            if not isinstance(model, dict):
                continue
            cost = model.get("cost")
            if not cost:
                continue

            entry = {
                "provider_id": provider_id,
                "model_id": model_id,
                "cost": cost,
            }

            # Index by stripped model name
            key = _to_alphanum(model_id)
            if key:
                index.setdefault(key, []).append(entry)

    return index


def fetch_models_dev_pricing() -> dict:
    """Fetch pricing data from models.dev. Returns empty dict on failure."""
    try:
        request = Request(
            MODELS_DEV_URL,
            headers={"User-Agent": "OpenWebUI-Cost-Tracker/1.0"}
        )
        with urlopen(request, timeout=FETCH_TIMEOUT_SECONDS) as response:
            data = json.loads(response.read().decode("utf-8"))
            log.info(f"Fetched models.dev pricing data: {len(data)} providers")
            return data
    except (URLError, HTTPError, TimeoutError, ValueError) as e:
        log.warning(f"Failed to fetch models.dev pricing: {e}")
        return {}


def get_pricing_data() -> dict:
    """Get pricing data, using cache if available and fresh."""
    global _pricing_cache, _pricing_cache_timestamp, _fuzzy_index

    current_time = time.time()
    cache_age = current_time - _pricing_cache_timestamp

    if _pricing_cache and cache_age < PRICING_CACHE_TTL_SECONDS:
        return _pricing_cache

    fresh_data = fetch_models_dev_pricing()

    if fresh_data:
        _pricing_cache = fresh_data
        _pricing_cache_timestamp = current_time
        _fuzzy_index = _build_fuzzy_index(fresh_data)
        return _pricing_cache

    if _pricing_cache:
        log.warning("Using stale pricing cache due to fetch failure")
        return _pricing_cache

    return {}


def _get_fuzzy_index() -> dict[str, list[dict]]:
    """Get the fuzzy index, building it if needed."""
    global _fuzzy_index
    if not _fuzzy_index and _pricing_cache:
        _fuzzy_index = _build_fuzzy_index(_pricing_cache)
    return _fuzzy_index


def resolve_model_pricing(
    catalog: dict,
    requested_model: Optional[str],
    base_model_id: Optional[str] = None,
    owned_by: Optional[str] = None,
) -> dict:
    """
    Resolve pricing for a model using models.dev catalog.

    Strategy:
    1. Try exact provider/model lookup if format allows
    2. Fuzzy match: strip both the incoming model name and all catalog model names
       to alphanumeric-only, then match. This handles dots vs hyphens, underscores,
       etc. without any special-case code.
    3. If multiple fuzzy matches, prefer the one matching the provider hint.

    Returns dict with provider_id, model_id, cost, and source.
    """
    if not requested_model:
        return {"provider_id": None, "model_id": None, "cost": {}, "source": "unresolved"}

    provider_hint = _extract_provider_hint(requested_model, owned_by)
    model_name = _extract_model_name(requested_model)

    # Also try base_model_id if provided
    base_model_name = _extract_model_name(base_model_id) if base_model_id else None

    # --- Strategy 1: Exact lookup with provider/model ---
    def exact_lookup(provider_id: str, model_id: str, source: str) -> Optional[dict]:
        provider = catalog.get(provider_id)
        if not isinstance(provider, dict):
            return None
        model = (provider.get("models") or {}).get(model_id)
        if not isinstance(model, dict):
            return None
        return {
            "provider_id": provider_id,
            "model_id": model_id,
            "cost": model.get("cost") or {},
            "source": source,
        }

    # Try provider_hint + model_name as exact lookup
    if provider_hint and model_name:
        match = exact_lookup(provider_hint, model_name, "exact")
        if match:
            return match

    # Try base model with provider hint
    if provider_hint and base_model_name:
        match = exact_lookup(provider_hint, base_model_name, "exact_base")
        if match:
            return match

    # --- Strategy 2: Fuzzy index lookup ---
    index = _get_fuzzy_index()

    # Try model names in priority order
    names_to_try = [model_name]
    if base_model_name and base_model_name != model_name:
        names_to_try.append(base_model_name)

    for name in names_to_try:
        key = _to_alphanum(name)
        if not key:
            continue

        hits = index.get(key)
        if not hits:
            continue

        # If we have a provider hint, prefer that provider
        if provider_hint:
            preferred = [h for h in hits if h["provider_id"].lower() == provider_hint]
            if preferred:
                h = preferred[0]
                return {**h, "source": "fuzzy"}

        # Otherwise pick the first match (prefer openai as default)
        sorted_hits = sorted(hits, key=lambda h: (h["provider_id"] != "openai", h["provider_id"]))
        h = sorted_hits[0]
        return {**h, "source": "fuzzy"}

    return {
        "provider_id": None,
        "model_id": base_model_id or requested_model,
        "cost": {},
        "source": "unresolved",
    }


def get_model_pricing(
    model_id: str,
    base_model_id: Optional[str] = None,
    owned_by: Optional[str] = None,
    model_meta: Optional[dict] = None,
) -> Optional[dict]:
    """
    Get pricing for a model.

    Args:
        model_id: The requested model identifier
        base_model_id: Optional base model ID (from model info)
        owned_by: Optional provider hint
        model_meta: Optional model metadata with pricing_override

    Returns:
        Dict with input/output costs per token, or None if not found
    """
    # Check for manual override first
    if model_meta:
        override = model_meta.get("pricing_override")
        if override and isinstance(override, dict):
            input_cost = override.get("input_cost_per_token")
            output_cost = override.get("output_cost_per_token")
            if input_cost is not None and output_cost is not None:
                return {
                    "input_cost_per_token": float(input_cost),
                    "output_cost_per_token": float(output_cost),
                    "cache_read_cost_per_token": float(override.get("cache_read_cost_per_token", 0)),
                    "cache_write_cost_per_token": float(override.get("cache_write_cost_per_token", 0)),
                    "source": "override",
                }

    catalog = get_pricing_data()
    if not catalog:
        return None

    resolved = resolve_model_pricing(catalog, model_id, base_model_id, owned_by)

    if resolved.get("source") == "unresolved":
        return None

    cost = resolved.get("cost") or {}

    # models.dev uses cost per 1M tokens
    input_per_million = Decimal(str(cost.get("input", 0)))
    output_per_million = Decimal(str(cost.get("output", 0)))
    cache_read_per_million = Decimal(str(cost.get("cache_read", 0)))
    cache_write_per_million = Decimal(str(cost.get("cache_write", 0)))

    if input_per_million == 0 and output_per_million == 0:
        return None

    return {
        "input_cost_per_token": float(input_per_million / ONE_MILLION),
        "output_cost_per_token": float(output_per_million / ONE_MILLION),
        "cache_read_cost_per_token": float(cache_read_per_million / ONE_MILLION),
        "cache_write_cost_per_token": float(cache_write_per_million / ONE_MILLION),
        "source": resolved.get("source"),
        "matched_provider": resolved.get("provider_id"),
        "matched_model": resolved.get("model_id"),
    }


def extract_token_breakdown(usage: dict) -> dict:
    """
    Extract detailed token breakdown from usage data.
    Handles various API formats (OpenAI, Anthropic, Ollama, etc.)
    """
    def safe_int(value) -> int:
        try:
            if value in (None, ""):
                return 0
            return max(0, int(float(value)))
        except (TypeError, ValueError):
            return 0

    def first_present(data: dict, paths: list) -> int:
        for path in paths:
            current = data
            for key in path:
                if isinstance(current, dict) and key in current:
                    current = current[key]
                else:
                    current = None
                    break
            if current is not None:
                return safe_int(current)
        return 0

    input_tokens = first_present(usage, [
        ("input_tokens",),
        ("prompt_tokens",),
        ("prompt_eval_count",),
        ("prompt_n",),
    ])

    output_total = first_present(usage, [
        ("output_tokens",),
        ("completion_tokens",),
        ("eval_count",),
        ("predicted_n",),
    ])

    reasoning_tokens = first_present(usage, [
        ("completion_tokens_details", "reasoning_tokens"),
        ("outputTokenDetails", "reasoningTokens"),
        ("reasoning_tokens",),
    ])

    cache_read_tokens = first_present(usage, [
        ("prompt_tokens_details", "cached_tokens"),
        ("input_tokens_details", "cache_read_tokens"),
        ("cache_read_input_tokens",),
        ("cache_read_tokens",),
    ])

    cache_write_tokens = first_present(usage, [
        ("input_tokens_details", "cache_write_tokens"),
        ("cache_creation_input_tokens",),
        ("cache_write_tokens",),
    ])

    # Adjust for cache/reasoning to avoid double counting
    adjusted_input = max(0, input_tokens - cache_read_tokens - cache_write_tokens)
    adjusted_output = max(0, output_total - reasoning_tokens)

    total = first_present(usage, [("total_tokens",)])
    if total <= 0:
        total = adjusted_input + adjusted_output + reasoning_tokens + cache_read_tokens + cache_write_tokens

    return {
        "input_tokens": adjusted_input,
        "output_tokens": adjusted_output,
        "reasoning_tokens": reasoning_tokens,
        "cache_read_tokens": cache_read_tokens,
        "cache_write_tokens": cache_write_tokens,
        "total_tokens": total,
    }


def calculate_cost(
    model_id: str,
    input_tokens: int,
    output_tokens: int,
    base_model_id: Optional[str] = None,
    owned_by: Optional[str] = None,
    model_meta: Optional[dict] = None,
    reasoning_tokens: int = 0,
    cache_read_tokens: int = 0,
    cache_write_tokens: int = 0,
) -> Optional[dict]:
    """
    Calculate cost for a request based on token usage.

    Returns:
        Dict with cost breakdown, or None if no pricing available
    """
    if not input_tokens and not output_tokens:
        return None

    pricing = get_model_pricing(model_id, base_model_id, owned_by, model_meta)
    if not pricing:
        return None

    input_cost = Decimal(input_tokens) * Decimal(str(pricing["input_cost_per_token"]))
    output_cost = Decimal(output_tokens) * Decimal(str(pricing["output_cost_per_token"]))

    # Add reasoning tokens at output rate
    reasoning_cost = Decimal(reasoning_tokens) * Decimal(str(pricing["output_cost_per_token"]))

    # Add cache costs
    cache_read_cost = Decimal(cache_read_tokens) * Decimal(str(pricing.get("cache_read_cost_per_token", 0)))
    cache_write_cost = Decimal(cache_write_tokens) * Decimal(str(pricing.get("cache_write_cost_per_token", 0)))

    total_cost = input_cost + output_cost + reasoning_cost + cache_read_cost + cache_write_cost

    return {
        "input_cost": float(input_cost.quantize(DISPLAY_QUANTIZE, rounding=ROUND_HALF_UP)),
        "output_cost": float(output_cost.quantize(DISPLAY_QUANTIZE, rounding=ROUND_HALF_UP)),
        "reasoning_cost": float(reasoning_cost.quantize(DISPLAY_QUANTIZE, rounding=ROUND_HALF_UP)),
        "cache_read_cost": float(cache_read_cost.quantize(DISPLAY_QUANTIZE, rounding=ROUND_HALF_UP)),
        "cache_write_cost": float(cache_write_cost.quantize(DISPLAY_QUANTIZE, rounding=ROUND_HALF_UP)),
        "total_cost": float(total_cost.quantize(DISPLAY_QUANTIZE, rounding=ROUND_HALF_UP)),
        "currency": "USD",
        "pricing_source": pricing.get("source"),
        "matched_provider": pricing.get("matched_provider"),
        "matched_model": pricing.get("matched_model"),
    }


def normalize_usage_with_cost(usage: dict, cost: Optional[dict]) -> dict:
    """Merge cost data into a normalized usage dict."""
    if not cost:
        return usage

    result = dict(usage)
    result["cost"] = cost
    return result


def refresh_pricing_cache() -> bool:
    """Force refresh the pricing cache."""
    global _pricing_cache, _pricing_cache_timestamp, _fuzzy_index

    fresh_data = fetch_models_dev_pricing()
    if fresh_data:
        _pricing_cache = fresh_data
        _pricing_cache_timestamp = time.time()
        _fuzzy_index = _build_fuzzy_index(fresh_data)
        return True
    return False


def get_cache_status() -> dict:
    """Get current cache status."""
    current_time = time.time()
    cache_age = current_time - _pricing_cache_timestamp if _pricing_cache_timestamp else None

    return {
        "cached_providers": len(_pricing_cache),
        "fuzzy_index_entries": len(_fuzzy_index),
        "cache_age_seconds": round(cache_age, 1) if cache_age else None,
        "cache_ttl_seconds": PRICING_CACHE_TTL_SECONDS,
        "cache_fresh": cache_age < PRICING_CACHE_TTL_SECONDS if cache_age else False,
    }
