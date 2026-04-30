"""
Model pricing utilities for cost tracking.

Fetches pricing data from models.dev and calculates costs based on token usage.
Uses the same pricing resolution approach as OpenCode cost tracker.
"""

import json
import logging
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


def _safe_decimal(value) -> Decimal:
    try:
        return Decimal(str(value))
    except Exception:
        return Decimal("0")


def _normalize_identifier(value: Optional[str]) -> str:
    if not value:
        return ""
    return "".join(ch for ch in value.strip().lower() if ch.isalnum() or ch in "-._/:")


def _split_model_candidates(model_id: Optional[str]) -> list[str]:
    """Generate candidate model IDs to try for lookup.

    OpenWebUI uses dot-separated connection prefixes, e.g.:
        "openrouter.anthropic/claude-opus-4.5" -> connection=openrouter, provider=anthropic, model=claude-opus-4.5
        "myapi.openai/gpt-4o" -> connection=myapi, provider=openai, model=gpt-4o
    """
    if not model_id:
        return []

    raw = model_id.strip()
    candidates = [raw]

    # Handle OpenWebUI dot-separated connection prefix (e.g., "openrouter.anthropic/claude-opus-4.5")
    # Split on first "/" to get the prefix and model name
    if "/" in raw:
        prefix, model_name = raw.split("/", 1)
        # Check if prefix has a dot (connection.provider format)
        if "." in prefix:
            dot_parts = prefix.split(".")
            # Last segment after dot is the actual provider
            provider = dot_parts[-1]
            # Add provider/model (e.g., "anthropic/claude-opus-4.5")
            candidates.append(f"{provider}/{model_name}")
            # Add just the model name
            candidates.append(model_name)
        else:
            # Simple prefix/model format
            candidates.append(model_name)

    # Handle colon separator (e.g., "provider:model")
    if ":" in raw:
        candidates.append(raw.split(":")[-1])

    # Handle slash separator - try progressively shorter paths
    parts = raw.split("/")
    for i in range(1, len(parts)):
        remaining = "/".join(parts[i:])
        if remaining not in candidates:
            candidates.append(remaining)

    # Also add just the last segment
    if len(parts) > 1:
        last = parts[-1]
        if last not in candidates:
            candidates.append(last)

    # Handle -latest suffix
    if raw.endswith("-latest"):
        candidates.append(raw[:-len("-latest")])

    # Version dot-to-hyphen normalization (e.g., "claude-opus-4.5" -> "claude-opus-4-5")
    # Models.dev uses hyphens, but OpenRouter/OpenWebUI may use dots for versions
    extra = []
    for c in candidates:
        normalized = c.replace(".", "-")
        if normalized != c:
            extra.append(normalized)
    candidates.extend(extra)

    # Deduplicate while preserving order
    seen = set()
    deduped = []
    for c in candidates:
        key = _normalize_identifier(c)
        if key and key not in seen:
            seen.add(key)
            deduped.append(c)

    return deduped


def fetch_models_dev_pricing() -> dict:
    """
    Fetch pricing data from models.dev.
    Returns empty dict on failure.
    """
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
    """
    Get pricing data, using cache if available and fresh.
    """
    global _pricing_cache, _pricing_cache_timestamp

    current_time = time.time()
    cache_age = current_time - _pricing_cache_timestamp

    if _pricing_cache and cache_age < PRICING_CACHE_TTL_SECONDS:
        return _pricing_cache

    fresh_data = fetch_models_dev_pricing()

    if fresh_data:
        _pricing_cache = fresh_data
        _pricing_cache_timestamp = current_time
        return _pricing_cache

    if _pricing_cache:
        log.warning("Using stale pricing cache due to fetch failure")
        return _pricing_cache

    return {}


def resolve_model_pricing(
    catalog: dict,
    requested_model: Optional[str],
    base_model_id: Optional[str] = None,
    owned_by: Optional[str] = None,
) -> dict:
    """
    Resolve pricing for a model using models.dev catalog.

    Tries multiple strategies:
    1. Explicit provider/model format (e.g., "openai/gpt-4o")
    2. Exact model ID match across all providers
    3. Normalized model ID match

    Returns dict with provider_id, model_id, cost, and source.
    """
    # Extract provider hint from owned_by or from dot-prefix in model ID
    provider_hint = _normalize_identifier(owned_by)
    if not provider_hint and requested_model and "/" in requested_model:
        prefix = requested_model.split("/", 1)[0]
        if "." in prefix:
            # "openrouter.anthropic/model" -> provider hint is "anthropic"
            provider_hint = _normalize_identifier(prefix.split(".")[-1])

    requested_candidates = _split_model_candidates(requested_model)
    base_candidates = _split_model_candidates(base_model_id)

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

    # Try explicit provider/model format from requested_model
    if requested_model and "/" in requested_model:
        parts = requested_model.split("/")
        if len(parts) >= 2:
            # Could be "openrouter/openai/gpt-4o" or "openai/gpt-4o"
            # Try last two segments first, then first two
            provider_id = parts[-2]
            model_id = parts[-1]
            match = exact_lookup(provider_id, model_id, "explicit")
            if match:
                return match

            if len(parts) > 2:
                provider_id = parts[0]
                model_id = "/".join(parts[1:])
                match = exact_lookup(provider_id, model_id, "explicit")
                if match:
                    return match

    # Try explicit format from base_model_id
    if base_model_id and "/" in base_model_id:
        parts = base_model_id.split("/")
        if len(parts) >= 2:
            provider_id = parts[-2] if len(parts) > 2 else parts[0]
            model_id = parts[-1]
            match = exact_lookup(provider_id, model_id, "explicit_base")
            if match:
                return match

    # Search for exact model ID matches across all providers
    search_candidates = requested_candidates + [c for c in base_candidates if c not in requested_candidates]

    exact_hits = []
    for candidate in search_candidates:
        for provider_id, provider in catalog.items():
            if not isinstance(provider, dict):
                continue
            model = (provider.get("models") or {}).get(candidate)
            if isinstance(model, dict):
                exact_hits.append({
                    "provider_id": provider_id,
                    "model_id": candidate,
                    "cost": model.get("cost") or {},
                    "source": "exact_id",
                })

    if exact_hits:
        # Prefer provider that matches hint
        if provider_hint:
            preferred = [h for h in exact_hits if _normalize_identifier(h["provider_id"]) == provider_hint]
            if preferred:
                return preferred[0]
        # Default to openai if available, then sort
        exact_hits.sort(key=lambda h: (h["provider_id"] != "openai", h["provider_id"], h["model_id"]))
        return exact_hits[0]

    # Try normalized matching
    normalized_candidates = [_normalize_identifier(c) for c in search_candidates if c]
    normalized_candidates = list(dict.fromkeys(normalized_candidates))  # Dedupe

    normalized_hits = []
    for norm_candidate in normalized_candidates:
        if not norm_candidate:
            continue
        for provider_id, provider in catalog.items():
            if not isinstance(provider, dict):
                continue
            for model_id, model in (provider.get("models") or {}).items():
                if _normalize_identifier(model_id) == norm_candidate:
                    normalized_hits.append({
                        "provider_id": provider_id,
                        "model_id": model_id,
                        "cost": model.get("cost") or {},
                        "source": "normalized_id",
                    })

    if normalized_hits:
        if provider_hint:
            preferred = [h for h in normalized_hits if _normalize_identifier(h["provider_id"]) == provider_hint]
            if preferred:
                return preferred[0]
        normalized_hits.sort(key=lambda h: (h["provider_id"] != "openai", h["provider_id"], h["model_id"]))
        return normalized_hits[0]

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
    input_per_million = _safe_decimal(cost.get("input", 0))
    output_per_million = _safe_decimal(cost.get("output", 0))
    cache_read_per_million = _safe_decimal(cost.get("cache_read", 0))
    cache_write_per_million = _safe_decimal(cost.get("cache_write", 0))

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
    """
    Merge cost data into a normalized usage dict.
    """
    if not cost:
        return usage

    result = dict(usage)
    result["cost"] = cost
    return result


def refresh_pricing_cache() -> bool:
    """Force refresh the pricing cache."""
    global _pricing_cache, _pricing_cache_timestamp

    fresh_data = fetch_models_dev_pricing()
    if fresh_data:
        _pricing_cache = fresh_data
        _pricing_cache_timestamp = time.time()
        return True
    return False


def get_cache_status() -> dict:
    """Get current cache status."""
    current_time = time.time()
    cache_age = current_time - _pricing_cache_timestamp if _pricing_cache_timestamp else None

    return {
        "cached_providers": len(_pricing_cache),
        "cache_age_seconds": round(cache_age, 1) if cache_age else None,
        "cache_ttl_seconds": PRICING_CACHE_TTL_SECONDS,
        "cache_fresh": cache_age < PRICING_CACHE_TTL_SECONDS if cache_age else False,
    }
