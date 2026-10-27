from dataclasses import dataclass
from decimal import Decimal

from django.conf import settings
from django.db.models import DecimalField, Sum
from django.db.models.functions import Coalesce
from django.utils import timezone

from .models import ModelCall, ModelCallStatus, ModelPricing


MILLION = Decimal("1000000")


@dataclass(frozen=True)
class BudgetDecision:
    allowed: bool
    warning: bool
    status: str
    spent: Decimal
    projected: Decimal


def estimate_tokens(text):
    return max(1, (len(text) + 3) // 4)


def current_pricing(provider, model):
    return ModelPricing.objects.filter(
        provider=provider,
        model=model,
        currency="CNY",
        active=True,
        effective_from__lte=timezone.now(),
    ).order_by("-effective_from", "-pk").first()


def calculate_cost(pricing, input_tokens, output_tokens):
    return (
        (Decimal(input_tokens) * pricing.input_price + Decimal(output_tokens) * pricing.output_price)
        / MILLION
    ).quantize(Decimal("0.000001"))


def daily_spend(now=None):
    now = now or timezone.now()
    output = ModelCall.objects.filter(
        created_at__date=timezone.localdate(now),
    ).exclude(
        status=ModelCallStatus.PENDING,
    ).aggregate(total=Sum(
        Coalesce("actual_cost", "estimated_cost"),
        output_field=DecimalField(max_digits=14, decimal_places=6),
    ))["total"]
    return output or Decimal("0")


def check_budget(estimated_cost, now=None, *, essential=True):
    spent = daily_spend(now)
    budget = Decimal(str(settings.LLM_DAILY_BUDGET_CNY))
    projected = spent + estimated_cost
    if projected >= budget:
        return BudgetDecision(False, True, ModelCallStatus.PAUSED_BUDGET_EXCEEDED, spent, projected)
    warning = projected >= budget * Decimal("0.8")
    if warning and not essential:
        return BudgetDecision(False, True, ModelCallStatus.PAUSED, spent, projected)
    return BudgetDecision(True, warning, "WARNING_80_PERCENT" if warning else "OK", spent, projected)
