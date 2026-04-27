"""
Model pricing utilities for cost tracking.

Fetches pricing data from LiteLLM's community-maintained database and
calculates costs based on token usage.
"""

import logging
import time
from typing import Optional
import aiohttp

log = logging.getLogger(__name__)

# LiteLLM pricing database URL
LITELLM_PRICING_URL = (
    "https://raw.githubusercontent.com/BerriAI/litellm/main/model_prices_and_context_window.json"
)

# Cache configuration
PRICING_CACHE_TTL_SECONDS = 24 * 60 * 60  # 24 hours
FETCH_TIMEOUT_SECONDS = 30

# In-memory cache
_pricing_cache: dict = {}
_pricing_cache_timestamp: float = 0


async def fetch_litellm_pricing() -> dict:
    """
    Fetch pricing data from LiteLLM's GitHub repository.
    Returns empty dict on failure.
    """
    try:
        timeout = aiohttp.ClientTimeout(total=FETCH_TIMEOUT_SECONDS)
        async with aiohttp.ClientSession(timeout=timeout, trust_env=True) as session:
            async with session.get(LITELLM_PRICING_URL) as response:
                if response.status == 200:
                    data = await response.json()
                    log.info(f"Fetched LiteLLM pricing data: {len(data)} models")
                    return data
                else:
                    log.warning(
                        f"Failed to fetch LiteLLM pricing: HTTP {response.status}"
                    )
                    return {}
    except Exception as e:
        log.error(f"Error fetching LiteLLM pricing: {e}")
        return {}


async def get_pricing_data() -> dict:
    """
    Get pricing data, using cache if available and fresh.
    Refreshes cache if older than TTL.
    """
    global _pricing_cache, _pricing_cache_timestamp

    current_time = time.time()
    cache_age = current_time - _pricing_cache_timestamp

    # Return cache if still valid
    if _pricing_cache and cache_age < PRICING_CACHE_TTL_SECONDS:
        return _pricing_cache

    # Fetch fresh data
    fresh_data = await fetch_litellm_pricing()

    if fresh_data:
        _pricing_cache = fresh_data
        _pricing_cache_timestamp = current_time
        return _pricing_cache

    # If fetch failed but we have stale cache, use it
    if _pricing_cache:
        log.warning("Using stale pricing cache due to fetch failure")
        return _pricing_cache

    return {}


def normalize_model_id(model_id: str) -> list[str]:
    """
    Generate possible LiteLLM model ID variations to try.
    Returns a list of IDs to check, in priority order.

    Examples:
        "gpt-4o" -> ["gpt-4o"]
        "openai/gpt-4o" -> ["openai/gpt-4o", "gpt-4o"]
        "azure/gpt-4" -> ["azure/gpt-4", "gpt-4"]
    """
    variations = [model_id]

    # If model has provider prefix, also try without it
    if "/" in model_id:
        base_model = model_id.split("/", 1)[-1]
        variations.append(base_model)

    # Try lowercase versions
    lowercase_variations = [v.lower() for v in variations if v.lower() not in variations]
    variations.extend(lowercase_variations)

    return variations


async def get_model_pricing(
    model_id: str,
    model_meta: Optional[dict] = None,
) -> Optional[dict]:
    """
    Get pricing for a model.

    First checks for manual override in model_meta.pricing_override,
    then looks up in LiteLLM pricing database.

    Args:
        model_id: The model identifier
        model_meta: Optional model metadata dict that may contain pricing_override

    Returns:
        Dict with input_cost_per_token and output_cost_per_token, or None
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
                    "source": "override",
                }

    # Look up in LiteLLM database
    pricing_data = await get_pricing_data()
    if not pricing_data:
        return None

    # Try different model ID variations
    for variant in normalize_model_id(model_id):
        if variant in pricing_data:
            model_pricing = pricing_data[variant]
            input_cost = model_pricing.get("input_cost_per_token")
            output_cost = model_pricing.get("output_cost_per_token")

            if input_cost is not None and output_cost is not None:
                return {
                    "input_cost_per_token": float(input_cost),
                    "output_cost_per_token": float(output_cost),
                    "source": "litellm",
                    "matched_model": variant,
                }

    return None


async def calculate_cost(
    model_id: str,
    input_tokens: int,
    output_tokens: int,
    model_meta: Optional[dict] = None,
) -> Optional[dict]:
    """
    Calculate cost for a request based on token usage.

    Args:
        model_id: The model identifier
        input_tokens: Number of input/prompt tokens
        output_tokens: Number of output/completion tokens
        model_meta: Optional model metadata for pricing override

    Returns:
        Dict with cost breakdown:
        {
            "input_cost": float,
            "output_cost": float,
            "total_cost": float,
            "currency": "USD"
        }
        Or None if no pricing available
    """
    if not input_tokens and not output_tokens:
        return None

    pricing = await get_model_pricing(model_id, model_meta)
    if not pricing:
        return None

    input_cost = input_tokens * pricing["input_cost_per_token"]
    output_cost = output_tokens * pricing["output_cost_per_token"]
    total_cost = input_cost + output_cost

    return {
        "input_cost": round(input_cost, 8),
        "output_cost": round(output_cost, 8),
        "total_cost": round(total_cost, 8),
        "currency": "USD",
    }


def normalize_usage_with_cost(usage: dict, cost: Optional[dict]) -> dict:
    """
    Merge cost data into a normalized usage dict.

    Args:
        usage: The normalized usage dict from normalize_usage()
        cost: Cost dict from calculate_cost(), or None

    Returns:
        Usage dict with cost field added if cost was provided
    """
    if not cost:
        return usage

    result = dict(usage)
    result["cost"] = cost
    return result


async def refresh_pricing_cache() -> bool:
    """
    Force refresh the pricing cache.
    Returns True if successful, False otherwise.
    """
    global _pricing_cache, _pricing_cache_timestamp

    fresh_data = await fetch_litellm_pricing()
    if fresh_data:
        _pricing_cache = fresh_data
        _pricing_cache_timestamp = time.time()
        return True
    return False


def get_cache_status() -> dict:
    """
    Get current cache status for debugging/admin.
    """
    current_time = time.time()
    cache_age = current_time - _pricing_cache_timestamp if _pricing_cache_timestamp else None

    return {
        "cached_models": len(_pricing_cache),
        "cache_age_seconds": round(cache_age, 1) if cache_age else None,
        "cache_ttl_seconds": PRICING_CACHE_TTL_SECONDS,
        "cache_fresh": cache_age < PRICING_CACHE_TTL_SECONDS if cache_age else False,
    }
