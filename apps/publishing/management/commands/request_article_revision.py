from apps.publishing.review import request_article_revision

from ._article_review import ArticleReviewCommand


class Command(ArticleReviewCommand):
    help = "Return one IN_REVIEW ArticleVersion to DRAFT for human-requested revision."
    service = staticmethod(request_article_revision)
