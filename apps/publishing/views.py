from datetime import date, timedelta

from django.db.models import Count, F, OuterRef, Prefetch, Q, Subquery
from django.http import Http404, HttpResponse
from django.shortcuts import get_object_or_404, render
from django.utils import timezone
from django.views.decorators.http import require_safe

from apps.claims.models import ClaimEvidence

from .models import (
    Article,
    ArticleStatus,
    ArticleVersionClaim,
    Digest,
    DigestCategory,
    DigestItem,
    DigestStatus,
)


TOPIC_GROUPS = (
    (DigestCategory.WORLD, "🌍", "世界"),
    (DigestCategory.AI_TECH, "🤖", "AI / 科技"),
    (DigestCategory.FINANCE, "💰", "财经"),
    (DigestCategory.AUTO, "🚗", "汽车"),
    (DigestCategory.SCIENCE, "🔬", "科学"),
    (DigestCategory.EDUCATION, "🎓", "教育"),
    (DigestCategory.HEALTH, "🏥", "健康"),
    (DigestCategory.GENERAL, "◇", "更多线索"),
)

CATEGORY_GROUPS = (
    (DigestCategory.PERSONALIZED, "👀", "我想看的"),
    (DigestCategory.DISCOVER, "🧭", "你可能不知道"),
    *TOPIC_GROUPS,
)


def _card_summary(title, summary):
    compact_title = "".join(character.casefold() for character in title if character.isalnum())
    compact_summary = "".join(
        character.casefold() for character in summary if character.isalnum()
    )
    if compact_title in compact_summary and len(compact_summary) <= len(compact_title) + 12:
        return ""
    return summary

PUBLIC_ARTICLE_FILTER = Q(
    article__status=ArticleStatus.PUBLISHED,
    article__published_version__status=ArticleStatus.PUBLISHED,
    article__published_version__isnull=False,
    article__current_version_id=F("article__published_version_id"),
    article_version__article_id=F("article_id"),
    article_version__status__in=(ArticleStatus.PUBLISHED, ArticleStatus.CORRECTED),
)


def _public_items_prefetch():
    source_name = (
        ClaimEvidence.objects.filter(
            claim__article_links__article_version_id=OuterRef("article_version_id")
        )
        .order_by("captured_at", "pk")
        .values("source__name")[:1]
    )
    return Prefetch(
        "items",
        queryset=DigestItem.objects.filter(PUBLIC_ARTICLE_FILTER)
        .annotate(source_name=Subquery(source_name))
        .select_related("article", "article_version")
        .order_by("rank"),
        to_attr="public_items",
    )


def _digest_context(digest):
    items = digest.public_items if digest else []
    category_labels = {
        value: label for value, _icon, label in CATEGORY_GROUPS
    }
    category_labels[DigestCategory.MUST_KNOW] = "今日必须知道"
    for item in items:
        item.category_label = category_labels[item.category]
        item.card_summary = _card_summary(
            item.article_version.title, item.article_version.one_sentence_summary
        )
    must_know = [item for item in items if item.category == DigestCategory.MUST_KNOW][:5]
    discover = [item for item in items if item.category == DigestCategory.DISCOVER][:5]
    personalized = [
        item for item in items if item.category == DigestCategory.PERSONALIZED
    ][:5]
    topic_sections = []
    for category, icon, label in TOPIC_GROUPS:
        category_items = [item for item in items if item.category == category]
        if category_items:
            topic_sections.append(
                {"icon": icon, "label": label, "items": category_items[:5]}
            )
    return {
        "digest": digest,
        "digest_date": digest.digest_date if digest else timezone.localdate(),
        "selected_count": len(items),
        "must_know": must_know,
        "discover": discover,
        "personalized": personalized,
        "topic_sections": topic_sections,
    }


def _canonical(request, path=None):
    return request.build_absolute_uri(path or request.path)


@require_safe
def home(request):
    digest = (
        Digest.objects.filter(
            status=DigestStatus.READY, digest_date__lte=timezone.localdate()
        )
        .order_by("-digest_date")
        .prefetch_related(_public_items_prefetch())
        .first()
    )
    context = _digest_context(digest)
    context.update(
        {
            "page_title": "SignalToday｜少刷一点，多知道一点",
            "page_description": "看见你关心的，也发现平时容易错过的新信号。",
            "canonical_url": _canonical(request, "/"),
            "is_home": True,
        }
    )
    return render(request, "publishing/home.html", context)


@require_safe
def digest_detail(request, digest_date):
    try:
        parsed_date = date.fromisoformat(digest_date)
    except ValueError as exc:
        raise Http404 from exc
    digest = get_object_or_404(
        Digest.objects.filter(status=DigestStatus.READY).prefetch_related(
            _public_items_prefetch()
        ),
        digest_date=parsed_date,
    )
    context = _digest_context(digest)
    context.update(
        {
            "page_title": f"{digest.digest_date.isoformat()} 日报｜SignalToday",
            "page_description": f"SignalToday {digest.digest_date.isoformat()} 精选信息。",
            "canonical_url": _canonical(request),
            "is_home": False,
        }
    )
    return render(request, "publishing/digest_detail.html", context)


def _article_queryset():
    evidence = ClaimEvidence.objects.select_related("source", "raw_item").order_by(
        "captured_at", "pk"
    )
    links = ArticleVersionClaim.objects.select_related("claim").prefetch_related(
        Prefetch("claim__evidence", queryset=evidence, to_attr="public_source_evidence")
    )
    return Article.objects.filter(
        status=ArticleStatus.PUBLISHED,
        published_version__status=ArticleStatus.PUBLISHED,
        published_version__isnull=False,
        current_version_id=F("published_version_id"),
    ).select_related("published_version").prefetch_related(
        Prefetch("published_version__claim_links", queryset=links, to_attr="public_claim_links")
    )


@require_safe
def article_detail(request, article_id):
    article = get_object_or_404(_article_queryset(), pk=article_id)
    version = article.published_version
    digest_item = (
        article.digest_items.filter(digest__status=DigestStatus.READY)
        .select_related("digest")
        .order_by("-digest__digest_date", "rank")
        .first()
    )
    sources = []
    seen = set()
    for link in version.public_claim_links:
        for evidence in link.claim.public_source_evidence:
            key = (evidence.source_id, evidence.raw_item_id)
            if key in seen:
                continue
            seen.add(key)
            sources.append(
                {
                    "name": evidence.source.name,
                    "url": evidence.raw_item.url or evidence.source.url,
                    "title": evidence.raw_item.title,
                }
            )
    category = None
    if digest_item:
        labels = {value: (icon, label) for value, icon, label in CATEGORY_GROUPS}
        labels[DigestCategory.MUST_KNOW] = ("🔥", "今日必须知道")
        category = labels.get(digest_item.category)
    context = {
        "article": article,
        "version": version,
        "digest_item": digest_item,
        "category": category,
        "sources": sources,
        "page_title": f"{version.title}｜SignalToday",
        "page_description": version.one_sentence_summary,
        "canonical_url": _canonical(request),
    }
    return render(request, "publishing/article_detail.html", context)


@require_safe
def digest_article_detail(request, digest_date, article_id):
    try:
        parsed_date = date.fromisoformat(digest_date)
    except ValueError as exc:
        raise Http404 from exc
    item = get_object_or_404(
        DigestItem.objects.filter(
            PUBLIC_ARTICLE_FILTER,
            digest__digest_date=parsed_date,
            digest__status=DigestStatus.READY,
        ).select_related("digest", "article", "article_version"),
        article_id=article_id,
    )
    article = item.article
    version = item.article_version
    links = list(
        ArticleVersionClaim.objects.filter(article_version=version)
        .select_related("claim")
        .prefetch_related(
            Prefetch(
                "claim__evidence",
                queryset=ClaimEvidence.objects.select_related("source", "raw_item").order_by(
                    "captured_at", "pk"
                ),
                to_attr="public_source_evidence",
            )
        )
    )
    sources = []
    seen = set()
    for link in links:
        for evidence in link.claim.public_source_evidence:
            key = (evidence.source_id, evidence.raw_item_id)
            if key in seen:
                continue
            seen.add(key)
            sources.append({
                "name": evidence.source.name,
                "url": evidence.raw_item.url or evidence.source.url,
                "title": evidence.raw_item.title,
            })
    labels = {value: (icon, label) for value, icon, label in CATEGORY_GROUPS}
    labels[DigestCategory.MUST_KNOW] = ("🔥", "今日必须知道")
    return render(request, "publishing/article_detail.html", {
        "article": article,
        "version": version,
        "digest_item": item,
        "category": labels.get(item.category),
        "sources": sources,
        "page_title": f"{version.title}｜SignalToday",
        "page_description": version.one_sentence_summary,
        "canonical_url": _canonical(request),
    })


@require_safe
def history(request):
    current_date = timezone.localdate()
    start_date = current_date - timedelta(days=29)
    public_items = Q(
        items__article__status=ArticleStatus.PUBLISHED,
        items__article__published_version__status=ArticleStatus.PUBLISHED,
        items__article__current_version_id=F("items__article__published_version_id"),
        items__article_version__article_id=F("items__article_id"),
    )
    digests = (
        Digest.objects.filter(
            status=DigestStatus.READY,
            digest_date__gte=start_date,
            digest_date__lte=current_date,
        )
        .annotate(public_item_count=Count("items", filter=public_items))
        .order_by("-digest_date")
    )
    return render(
        request,
        "publishing/history.html",
        {
            "digests": digests,
            "page_title": "历史日报｜SignalToday",
            "page_description": "查看 SignalToday 最近 30 天的精选日报。",
            "canonical_url": _canonical(request),
        },
    )


@require_safe
def robots(request):
    return HttpResponse(
        "User-agent: *\nAllow: /\nDisallow: /admin/\nDisallow: /health/\n",
        content_type="text/plain; charset=utf-8",
    )
