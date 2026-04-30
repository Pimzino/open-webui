from typing import Optional
from datetime import datetime, timedelta
from collections import defaultdict
import logging
from fastapi import APIRouter, Depends, Query, Response
from fastapi.responses import StreamingResponse
import csv
import io
import json as json_lib
import time
from pydantic import BaseModel

from open_webui.models.chat_messages import ChatMessages, ChatMessageModel
from open_webui.models.chats import Chats
from open_webui.models.groups import Groups
from open_webui.models.users import Users
from open_webui.models.feedbacks import Feedbacks
from open_webui.utils.auth import get_admin_user, get_verified_user
from open_webui.internal.db import get_async_session
from sqlalchemy.ext.asyncio import AsyncSession

log = logging.getLogger(__name__)


router = APIRouter()


####################
# Response Models
####################


class ModelAnalyticsEntry(BaseModel):
    model_id: str
    count: int


class ModelAnalyticsResponse(BaseModel):
    models: list[ModelAnalyticsEntry]


class UserAnalyticsEntry(BaseModel):
    user_id: str
    name: Optional[str] = None
    email: Optional[str] = None
    count: int
    input_tokens: int = 0
    output_tokens: int = 0
    total_tokens: int = 0


class UserAnalyticsResponse(BaseModel):
    users: list[UserAnalyticsEntry]


####################
# Endpoints
####################


@router.get('/models', response_model=ModelAnalyticsResponse)
async def get_model_analytics(
    start_date: Optional[int] = Query(None, description='Start timestamp (epoch)'),
    end_date: Optional[int] = Query(None, description='End timestamp (epoch)'),
    group_id: Optional[str] = Query(None, description='Filter by user group ID'),
    user=Depends(get_admin_user),
    db: AsyncSession = Depends(get_async_session),
):
    """Get message counts per model."""
    counts = await ChatMessages.get_message_count_by_model(
        start_date=start_date, end_date=end_date, group_id=group_id, db=db
    )
    models = [
        ModelAnalyticsEntry(model_id=model_id, count=count)
        for model_id, count in sorted(counts.items(), key=lambda x: -x[1])
    ]
    return ModelAnalyticsResponse(models=models)


@router.get('/users', response_model=UserAnalyticsResponse)
async def get_user_analytics(
    start_date: Optional[int] = Query(None, description='Start timestamp (epoch)'),
    end_date: Optional[int] = Query(None, description='End timestamp (epoch)'),
    group_id: Optional[str] = Query(None, description='Filter by user group ID'),
    limit: int = Query(50, description='Max users to return'),
    user=Depends(get_admin_user),
    db: AsyncSession = Depends(get_async_session),
):
    """Get message counts and token usage per user with user info."""
    counts = await ChatMessages.get_message_count_by_user(
        start_date=start_date, end_date=end_date, group_id=group_id, db=db
    )
    token_usage = await ChatMessages.get_token_usage_by_user(
        start_date=start_date, end_date=end_date, group_id=group_id, db=db
    )

    # Get user info for top users
    top_user_ids = [uid for uid, _ in sorted(counts.items(), key=lambda x: -x[1])[:limit]]
    user_info = {u.id: u for u in await Users.get_users_by_user_ids(top_user_ids, db=db)}

    users = []
    for user_id in top_user_ids:
        u = user_info.get(user_id)
        tokens = token_usage.get(user_id, {})
        users.append(
            UserAnalyticsEntry(
                user_id=user_id,
                name=u.name if u else None,
                email=u.email if u else None,
                count=counts[user_id],
                input_tokens=tokens.get('input_tokens', 0),
                output_tokens=tokens.get('output_tokens', 0),
                total_tokens=tokens.get('total_tokens', 0),
            )
        )

    return UserAnalyticsResponse(users=users)


@router.get('/messages', response_model=list[ChatMessageModel])
async def get_messages(
    model_id: Optional[str] = Query(None, description='Filter by model ID'),
    user_id: Optional[str] = Query(None, description='Filter by user ID'),
    chat_id: Optional[str] = Query(None, description='Filter by chat ID'),
    start_date: Optional[int] = Query(None, description='Start timestamp (epoch)'),
    end_date: Optional[int] = Query(None, description='End timestamp (epoch)'),
    skip: int = Query(0),
    limit: int = Query(50, le=100),
    user=Depends(get_admin_user),
    db: AsyncSession = Depends(get_async_session),
):
    """Query messages with filters."""
    if chat_id:
        return await ChatMessages.get_messages_by_chat_id(chat_id=chat_id, db=db)
    elif model_id:
        return await ChatMessages.get_messages_by_model_id(
            model_id=model_id,
            start_date=start_date,
            end_date=end_date,
            skip=skip,
            limit=limit,
            db=db,
        )
    elif user_id:
        return await ChatMessages.get_messages_by_user_id(user_id=user_id, skip=skip, limit=limit, db=db)
    else:
        # Return empty if no filter specified
        return []


class SummaryResponse(BaseModel):
    total_messages: int
    total_chats: int
    total_models: int
    total_users: int


@router.get('/summary', response_model=SummaryResponse)
async def get_summary(
    start_date: Optional[int] = Query(None, description='Start timestamp (epoch)'),
    end_date: Optional[int] = Query(None, description='End timestamp (epoch)'),
    group_id: Optional[str] = Query(None, description='Filter by user group ID'),
    user=Depends(get_admin_user),
    db: AsyncSession = Depends(get_async_session),
):
    """Get summary statistics for the dashboard."""
    model_counts = await ChatMessages.get_message_count_by_model(
        start_date=start_date, end_date=end_date, group_id=group_id, db=db
    )
    user_counts = await ChatMessages.get_message_count_by_user(
        start_date=start_date, end_date=end_date, group_id=group_id, db=db
    )
    chat_counts = await ChatMessages.get_message_count_by_chat(
        start_date=start_date, end_date=end_date, group_id=group_id, db=db
    )

    return SummaryResponse(
        total_messages=sum(model_counts.values()),
        total_chats=len(chat_counts),
        total_models=len(model_counts),
        total_users=len(user_counts),
    )


class DailyStatsEntry(BaseModel):
    date: str
    models: dict[str, int]


class DailyStatsResponse(BaseModel):
    data: list[DailyStatsEntry]


@router.get('/daily', response_model=DailyStatsResponse)
async def get_daily_stats(
    start_date: Optional[int] = Query(None, description='Start timestamp (epoch)'),
    end_date: Optional[int] = Query(None, description='End timestamp (epoch)'),
    group_id: Optional[str] = Query(None, description='Filter by user group ID'),
    granularity: str = Query('daily', description="Granularity: 'hourly' or 'daily'"),
    user=Depends(get_admin_user),
    db: AsyncSession = Depends(get_async_session),
):
    """Get message counts grouped by model for time-series chart."""
    if granularity == 'hourly':
        counts = await ChatMessages.get_hourly_message_counts_by_model(start_date=start_date, end_date=end_date, db=db)
    else:
        counts = await ChatMessages.get_daily_message_counts_by_model(
            start_date=start_date, end_date=end_date, group_id=group_id, db=db
        )
    return DailyStatsResponse(
        data=[DailyStatsEntry(date=date, models=models) for date, models in sorted(counts.items())]
    )


class TokenUsageEntry(BaseModel):
    model_id: str
    input_tokens: int
    output_tokens: int
    total_tokens: int
    message_count: int


class TokenUsageResponse(BaseModel):
    models: list[TokenUsageEntry]
    total_input_tokens: int
    total_output_tokens: int
    total_tokens: int


@router.get('/tokens', response_model=TokenUsageResponse)
async def get_token_usage(
    start_date: Optional[int] = Query(None),
    end_date: Optional[int] = Query(None),
    group_id: Optional[str] = Query(None, description='Filter by user group ID'),
    user=Depends(get_admin_user),
    db: AsyncSession = Depends(get_async_session),
):
    """Get token usage aggregated by model."""
    usage = await ChatMessages.get_token_usage_by_model(
        start_date=start_date, end_date=end_date, group_id=group_id, db=db
    )

    models = [
        TokenUsageEntry(model_id=model_id, **data)
        for model_id, data in sorted(usage.items(), key=lambda x: -x[1]['total_tokens'])
    ]

    total_input = sum(m.input_tokens for m in models)
    total_output = sum(m.output_tokens for m in models)

    return TokenUsageResponse(
        models=models,
        total_input_tokens=total_input,
        total_output_tokens=total_output,
        total_tokens=total_input + total_output,
    )


####################
# Model Chats Browser
####################


class ModelChatEntry(BaseModel):
    chat_id: str
    user_id: Optional[str] = None
    user_name: Optional[str] = None
    first_message: Optional[str] = None
    updated_at: int


class ModelChatsResponse(BaseModel):
    chats: list[ModelChatEntry]
    total: int


@router.get('/models/{model_id:path}/chats', response_model=ModelChatsResponse)
async def get_model_chats(
    model_id: str,
    start_date: Optional[int] = Query(None),
    end_date: Optional[int] = Query(None),
    skip: int = Query(0),
    limit: int = Query(50, le=100),
    user=Depends(get_admin_user),
    db: AsyncSession = Depends(get_async_session),
):
    """Get chats that used a specific model, with preview and feedback info."""

    # Get chat IDs that used this model
    chat_ids = await ChatMessages.get_chat_ids_by_model_id(
        model_id=model_id,
        start_date=start_date,
        end_date=end_date,
        skip=skip,
        limit=limit,
        db=db,
    )

    if not chat_ids:
        return ModelChatsResponse(chats=[], total=0)

    # Get chat details from messages only
    chats_data = []
    for chat_id in chat_ids:
        messages = await ChatMessages.get_messages_by_chat_id(chat_id, db=db)
        if not messages:
            continue

        # Get user_id from first user message
        first_user_msg = next((m for m in messages if m.role == 'user'), None)
        user_id = first_user_msg.user_id if first_user_msg else None

        # Extract first message content as preview
        first_message = None
        if first_user_msg and first_user_msg.content:
            content = first_user_msg.content
            if isinstance(content, str):
                first_message = content[:200]
            elif isinstance(content, list):
                text_parts = [b.get('text', '') for b in content if isinstance(b, dict)]
                first_message = ' '.join(text_parts)[:200]

        # Get user info
        user_name = None
        if user_id:
            user_info = await Users.get_user_by_id(user_id, db=db)
            user_name = user_info.name if user_info else None

        # Timestamps from messages
        updated_at = max(m.created_at for m in messages) if messages else 0

        chats_data.append(
            ModelChatEntry(
                chat_id=chat_id,
                user_id=user_id,
                user_name=user_name,
                first_message=first_message,
                updated_at=updated_at,
            )
        )

    return ModelChatsResponse(chats=chats_data, total=len(chats_data))


####################
# Model Overview
####################


class HistoryEntry(BaseModel):
    date: str
    won: int = 0
    lost: int = 0


class TagEntry(BaseModel):
    tag: str
    count: int


class ModelOverviewResponse(BaseModel):
    history: list[HistoryEntry]
    tags: list[TagEntry]


@router.get('/models/{model_id:path}/overview', response_model=ModelOverviewResponse)
async def get_model_overview(
    model_id: str,
    days: int = Query(30, description='Number of days of history (0 for all)'),
    user=Depends(get_admin_user),
    db: AsyncSession = Depends(get_async_session),
):
    """Get model overview with feedback history and chat tags."""

    # Get chat IDs that used this model
    chat_ids = await ChatMessages.get_chat_ids_by_model_id(
        model_id=model_id,
        start_date=None,
        end_date=None,
        skip=0,
        limit=10000,  # Get all chats
        db=db,
    )

    # Get feedback history per day
    history_counts: dict[str, dict] = defaultdict(lambda: {'won': 0, 'lost': 0})

    # Calculate start date for history
    now = datetime.now()
    start_dt = None
    if days > 0:
        start_dt = now - timedelta(days=days)

    for chat_id in chat_ids:
        feedbacks = await Feedbacks.get_feedbacks_by_chat_id(chat_id, db=db)
        for fb in feedbacks:
            if fb.data and 'rating' in fb.data:
                rating = fb.data['rating']
                fb_date = datetime.fromtimestamp(fb.created_at)

                # Filter by date range
                if start_dt and fb_date < start_dt:
                    continue

                date_str = fb_date.strftime('%Y-%m-%d')
                if rating == 1:
                    history_counts[date_str]['won'] += 1
                elif rating == -1:
                    history_counts[date_str]['lost'] += 1

    # Fill in missing days
    history = []
    if history_counts or days > 0:
        end_dt = now
        if days > 0:
            current = start_dt
        elif history_counts:
            # Find earliest date
            min_date = min(history_counts.keys())
            current = datetime.strptime(min_date, '%Y-%m-%d')
        else:
            current = now

        while current <= end_dt:
            date_str = current.strftime('%Y-%m-%d')
            counts = history_counts.get(date_str, {'won': 0, 'lost': 0})
            history.append(
                HistoryEntry(
                    date=date_str,
                    won=counts['won'],
                    lost=counts['lost'],
                )
            )
            current += timedelta(days=1)

    # Get chat tags
    tag_counts: dict[str, int] = defaultdict(int)
    for chat_id in chat_ids:
        chat = await Chats.get_chat_by_id(chat_id, db=db)
        if chat and chat.meta:
            for tag in chat.meta.get('tags', []):
                tag_counts[tag] += 1

    # Sort by count and take top 10
    tags = [TagEntry(tag=tag, count=count) for tag, count in sorted(tag_counts.items(), key=lambda x: -x[1])[:10]]

    return ModelOverviewResponse(history=history, tags=tags)


####################
# Cost Analytics
####################


class CostByModelEntry(BaseModel):
    model_id: str
    input_cost: float
    output_cost: float
    total_cost: float
    message_count: int


class CostByModelResponse(BaseModel):
    models: list[CostByModelEntry]
    total_input_cost: float
    total_output_cost: float
    total_cost: float
    currency: str = 'USD'


class CostByUserEntry(BaseModel):
    user_id: str
    name: Optional[str] = None
    email: Optional[str] = None
    input_cost: float
    output_cost: float
    total_cost: float
    message_count: int


class CostByUserResponse(BaseModel):
    users: list[CostByUserEntry]
    total_cost: float
    currency: str = 'USD'


class UserCostSummary(BaseModel):
    today: float
    this_month: float
    all_time: float
    currency: str = 'USD'


@router.get('/costs', response_model=CostByModelResponse)
async def get_cost_by_model(
    start_date: Optional[int] = Query(None, description='Start timestamp (epoch)'),
    end_date: Optional[int] = Query(None, description='End timestamp (epoch)'),
    group_id: Optional[str] = Query(None, description='Filter by user group ID'),
    user=Depends(get_admin_user),
    db: AsyncSession = Depends(get_async_session),
):
    """Get cost aggregated by model (admin only)."""
    cost_data = await ChatMessages.get_cost_by_model(
        start_date=start_date, end_date=end_date, group_id=group_id, db=db
    )

    models = [
        CostByModelEntry(model_id=model_id, **data)
        for model_id, data in sorted(cost_data.items(), key=lambda x: -x[1]['total_cost'])
    ]

    total_input = sum(m.input_cost for m in models)
    total_output = sum(m.output_cost for m in models)

    return CostByModelResponse(
        models=models,
        total_input_cost=total_input,
        total_output_cost=total_output,
        total_cost=total_input + total_output,
    )


@router.get('/costs/users', response_model=CostByUserResponse)
async def get_cost_by_user(
    start_date: Optional[int] = Query(None, description='Start timestamp (epoch)'),
    end_date: Optional[int] = Query(None, description='End timestamp (epoch)'),
    group_id: Optional[str] = Query(None, description='Filter by user group ID'),
    limit: int = Query(50, description='Max users to return'),
    user=Depends(get_admin_user),
    db: AsyncSession = Depends(get_async_session),
):
    """Get cost aggregated by user (admin only)."""
    cost_data = await ChatMessages.get_cost_by_user(
        start_date=start_date, end_date=end_date, group_id=group_id, db=db
    )

    # Get top users by cost
    top_user_ids = [
        uid for uid, _ in sorted(cost_data.items(), key=lambda x: -x[1]['total_cost'])[:limit]
    ]
    user_info = {u.id: u for u in await Users.get_users_by_user_ids(top_user_ids, db=db)}

    users = []
    for user_id in top_user_ids:
        u = user_info.get(user_id)
        data = cost_data[user_id]
        users.append(
            CostByUserEntry(
                user_id=user_id,
                name=u.name if u else None,
                email=u.email if u else None,
                **data,
            )
        )

    total_cost = sum(u.total_cost for u in users)

    return CostByUserResponse(users=users, total_cost=total_cost)


@router.get('/costs/export')
async def export_cost_analytics(
    format: str = Query('csv', description="Export format: 'csv' or 'json'"),
    start_date: Optional[int] = Query(None, description='Start timestamp (epoch)'),
    end_date: Optional[int] = Query(None, description='End timestamp (epoch)'),
    group_id: Optional[str] = Query(None, description='Filter by user group ID'),
    user=Depends(get_admin_user),
    db: AsyncSession = Depends(get_async_session),
):
    """Export cost analytics data (admin only)."""
    # Get cost data by model and user
    cost_by_model = await ChatMessages.get_cost_by_model(
        start_date=start_date, end_date=end_date, group_id=group_id, db=db
    )
    cost_by_user = await ChatMessages.get_cost_by_user(
        start_date=start_date, end_date=end_date, group_id=group_id, db=db
    )

    # Get user info
    user_ids = list(cost_by_user.keys())
    user_info = {u.id: u for u in await Users.get_users_by_user_ids(user_ids, db=db)}

    if format == 'json':
        export_data = {
            'exported_at': datetime.utcnow().isoformat(),
            'filters': {
                'start_date': start_date,
                'end_date': end_date,
                'group_id': group_id,
            },
            'by_model': [
                {'model_id': model_id, **data}
                for model_id, data in sorted(cost_by_model.items(), key=lambda x: -x[1]['total_cost'])
            ],
            'by_user': [
                {
                    'user_id': user_id,
                    'name': user_info.get(user_id).name if user_info.get(user_id) else None,
                    'email': user_info.get(user_id).email if user_info.get(user_id) else None,
                    **data,
                }
                for user_id, data in sorted(cost_by_user.items(), key=lambda x: -x[1]['total_cost'])
            ],
            'totals': {
                'total_cost': sum(d['total_cost'] for d in cost_by_model.values()),
                'total_input_cost': sum(d['input_cost'] for d in cost_by_model.values()),
                'total_output_cost': sum(d['output_cost'] for d in cost_by_model.values()),
                'currency': 'USD',
            },
        }
        content = json_lib.dumps(export_data, indent=2)
        return Response(
            content=content,
            media_type='application/json',
            headers={'Content-Disposition': f'attachment; filename=cost-analytics-{int(time.time())}.json'},
        )
    else:
        # CSV format
        output = io.StringIO()
        writer = csv.writer(output)

        # Write by-model section
        writer.writerow(['=== Cost by Model ==='])
        writer.writerow(['model_id', 'input_cost', 'output_cost', 'total_cost', 'message_count'])
        for model_id, data in sorted(cost_by_model.items(), key=lambda x: -x[1]['total_cost']):
            writer.writerow([
                model_id,
                f"{data['input_cost']:.6f}",
                f"{data['output_cost']:.6f}",
                f"{data['total_cost']:.6f}",
                data['message_count'],
            ])

        writer.writerow([])
        writer.writerow(['=== Cost by User ==='])
        writer.writerow(['user_id', 'name', 'email', 'input_cost', 'output_cost', 'total_cost', 'message_count'])
        for user_id, data in sorted(cost_by_user.items(), key=lambda x: -x[1]['total_cost']):
            u = user_info.get(user_id)
            writer.writerow([
                user_id,
                u.name if u else '',
                u.email if u else '',
                f"{data['input_cost']:.6f}",
                f"{data['output_cost']:.6f}",
                f"{data['total_cost']:.6f}",
                data['message_count'],
            ])

        content = output.getvalue()
        return Response(
            content=content,
            media_type='text/csv',
            headers={'Content-Disposition': f'attachment; filename=cost-analytics-{int(time.time())}.csv'},
        )


@router.get('/user/cost', response_model=UserCostSummary)
async def get_user_cost(
    user=Depends(get_verified_user),
    db: AsyncSession = Depends(get_async_session),
):
    """Get cost summary for the current user."""
    now = int(time.time())

    # Calculate time boundaries
    today_start = int(datetime.now().replace(hour=0, minute=0, second=0, microsecond=0).timestamp())
    month_start = int(datetime.now().replace(day=1, hour=0, minute=0, second=0, microsecond=0).timestamp())

    # Get user's cost reset timestamp (if any)
    cost_reset_at = await Users.get_user_cost_reset_at(user.id, db=db)

    # Apply reset timestamp to boundaries
    effective_today_start = max(today_start, cost_reset_at) if cost_reset_at else today_start
    effective_month_start = max(month_start, cost_reset_at) if cost_reset_at else month_start
    effective_all_time_start = cost_reset_at if cost_reset_at else None

    # Get costs for different periods
    today_data = await ChatMessages.get_user_cost_summary(
        user_id=user.id, start_date=effective_today_start, end_date=now, db=db
    )
    month_data = await ChatMessages.get_user_cost_summary(
        user_id=user.id, start_date=effective_month_start, end_date=now, db=db
    )
    all_time_data = await ChatMessages.get_user_cost_summary(
        user_id=user.id, start_date=effective_all_time_start, db=db
    )

    return UserCostSummary(
        today=today_data['total_cost'],
        this_month=month_data['total_cost'],
        all_time=all_time_data['total_cost'],
    )


####################
# Pricing Diagnostics
####################


class PricingStatusResponse(BaseModel):
    cache_status: dict
    sample_models: list[str]


class PricingLookupResponse(BaseModel):
    model_id: str
    matched: bool
    matched_provider: Optional[str] = None
    matched_model: Optional[str] = None
    pricing: Optional[dict] = None


@router.get('/pricing/status', response_model=PricingStatusResponse)
async def get_pricing_status(
    user=Depends(get_admin_user),
):
    """Get pricing cache status and sample of available models (admin only)."""
    from open_webui.utils.pricing import get_cache_status, get_pricing_data

    status = get_cache_status()
    pricing_data = get_pricing_data()

    # Get sample of provider/model pairs (first 20 that have pricing)
    sample = []
    for provider_id, provider in list(pricing_data.items())[:20]:
        if not isinstance(provider, dict):
            continue
        models = provider.get('models') or {}
        for model_id, model in list(models.items())[:3]:
            if isinstance(model, dict) and model.get('cost'):
                sample.append(f"{provider_id}/{model_id}")
                if len(sample) >= 20:
                    break
        if len(sample) >= 20:
            break

    return PricingStatusResponse(cache_status=status, sample_models=sample)


@router.get('/pricing/lookup')
async def lookup_model_pricing(
    model_id: str = Query(..., description='Model ID to look up'),
    base_model_id: Optional[str] = Query(None, description='Optional base model ID'),
    owned_by: Optional[str] = Query(None, description='Optional provider hint'),
    user=Depends(get_admin_user),
):
    """Test pricing lookup for a model ID (admin only)."""
    from open_webui.utils.pricing import get_model_pricing

    pricing = get_model_pricing(model_id, base_model_id, owned_by)

    return PricingLookupResponse(
        model_id=model_id,
        matched=pricing is not None,
        matched_provider=pricing.get('matched_provider') if pricing else None,
        matched_model=pricing.get('matched_model') if pricing else None,
        pricing=pricing,
    )


@router.get('/pricing/models-in-use')
async def get_models_in_use(
    user=Depends(get_admin_user),
    db: AsyncSession = Depends(get_async_session),
):
    """Get list of model IDs actually used in chats with their pricing status (admin only)."""
    from open_webui.utils.pricing import get_model_pricing

    # Get distinct model IDs from messages
    model_counts = await ChatMessages.get_message_count_by_model(db=db)

    results = []
    for model_id, count in sorted(model_counts.items(), key=lambda x: -x[1])[:50]:
        pricing = get_model_pricing(model_id)
        results.append({
            'model_id': model_id,
            'message_count': count,
            'has_pricing': pricing is not None,
            'pricing_source': pricing.get('source') if pricing else None,
            'matched_provider': pricing.get('matched_provider') if pricing else None,
            'matched_model': pricing.get('matched_model') if pricing else None,
        })

    return results


@router.post('/pricing/recalculate')
async def recalculate_costs(
    limit: int = Query(1000, description='Max messages to process'),
    user=Depends(get_admin_user),
    db: AsyncSession = Depends(get_async_session),
):
    """Recalculate costs for messages that have tokens but no cost data (admin only)."""
    from open_webui.utils.pricing import calculate_cost, extract_token_breakdown, extract_upstream_cost

    # Get messages with usage but no cost
    messages = await ChatMessages.get_messages_without_cost(limit=limit, db=db)

    updated = 0
    skipped = 0
    no_pricing = 0

    for msg in messages:
        usage = msg.usage or {}
        token_breakdown = extract_token_breakdown(usage)

        if not token_breakdown['input_tokens'] and not token_breakdown['output_tokens']:
            skipped += 1
            continue

        # Prefer upstream provider cost (e.g. OpenRouter includes actual charged cost)
        cost = extract_upstream_cost(usage)

        # Fall back to models.dev calculation when provider doesn't include cost
        if not cost:
            cost = calculate_cost(
                msg.model_id or '',
                token_breakdown['input_tokens'],
                token_breakdown['output_tokens'],
                reasoning_tokens=token_breakdown.get('reasoning_tokens', 0),
                cache_read_tokens=token_breakdown.get('cache_read_tokens', 0),
                cache_write_tokens=token_breakdown.get('cache_write_tokens', 0),
            )

        if cost:
            new_usage = dict(usage)
            new_usage['cost'] = cost
            await ChatMessages.update_message_usage(msg.id, new_usage, db=db)
            updated += 1
        else:
            no_pricing += 1

    return {
        'processed': len(messages),
        'updated': updated,
        'skipped_no_tokens': skipped,
        'skipped_no_pricing': no_pricing,
    }
