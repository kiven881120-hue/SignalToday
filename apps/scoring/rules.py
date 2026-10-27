from decimal import Decimal, InvalidOperation

from django.core.exceptions import ValidationError


DIMENSIONS = (
    "importance",
    "credibility",
    "user_relevance",
    "information_gain",
    "freshness",
    "long_term_impact",
)

DEFAULT_RULE_CONFIG = {
    "weights": {
        "importance": "0.25",
        "credibility": "0.25",
        "user_relevance": "0.15",
        "information_gain": "0.15",
        "freshness": "0.10",
        "long_term_impact": "0.10",
    },
    "thresholds": {"FOCUS": "8.0", "NORMAL": "6.5", "CANDIDATE": "5.0"},
}


def default_rule_config():
    return {key: value.copy() for key, value in DEFAULT_RULE_CONFIG.items()}


def decimal_value(value, label):
    try:
        return Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        raise ValidationError(f"{label} must be a decimal number.") from None


def validate_rule_config(config):
    if not isinstance(config, dict):
        raise ValidationError("rule_config must be an object.")
    weights = config.get("weights")
    thresholds = config.get("thresholds")
    if not isinstance(weights, dict) or set(weights) != set(DIMENSIONS):
        raise ValidationError({"rule_config": f"weights must contain exactly: {', '.join(DIMENSIONS)}"})
    parsed_weights = {key: decimal_value(value, f"weight {key}") for key, value in weights.items()}
    if any(value < 0 for value in parsed_weights.values()):
        raise ValidationError({"rule_config": "All weights must be non-negative."})
    if sum(parsed_weights.values(), Decimal("0")) != Decimal("1"):
        raise ValidationError({"rule_config": "Weights must sum exactly to 1."})
    required_thresholds = {"FOCUS", "NORMAL", "CANDIDATE"}
    if not isinstance(thresholds, dict) or set(thresholds) != required_thresholds:
        raise ValidationError({"rule_config": "thresholds must contain FOCUS, NORMAL, and CANDIDATE."})
    parsed_thresholds = {key: decimal_value(value, f"threshold {key}") for key, value in thresholds.items()}
    if not all(Decimal("0") <= value <= Decimal("10") for value in parsed_thresholds.values()):
        raise ValidationError({"rule_config": "Thresholds must be between 0 and 10."})
    if not parsed_thresholds["FOCUS"] > parsed_thresholds["NORMAL"] > parsed_thresholds["CANDIDATE"]:
        raise ValidationError({"rule_config": "Thresholds must descend FOCUS > NORMAL > CANDIDATE."})
    return parsed_weights, parsed_thresholds


def weighted_score(values, config):
    weights, _ = validate_rule_config(config)
    total = sum((decimal_value(values[key], key) * weights[key] for key in DIMENSIONS), Decimal("0"))
    return min(Decimal("10"), max(Decimal("0"), total)).quantize(Decimal("0.001"))


def recommendation_level(final_score, config):
    _, thresholds = validate_rule_config(config)
    score = decimal_value(final_score, "final_score")
    if score >= thresholds["FOCUS"]:
        return "FOCUS"
    if score >= thresholds["NORMAL"]:
        return "NORMAL"
    if score >= thresholds["CANDIDATE"]:
        return "CANDIDATE"
    return "REJECT"
