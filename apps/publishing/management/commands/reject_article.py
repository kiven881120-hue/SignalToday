from apps.publishing.review import reject_article

from ._article_review import ArticleReviewCommand


class Command(ArticleReviewCommand):
    help = "Reject one IN_REVIEW ArticleVersion back to DRAFT."
    service = staticmethod(reject_article)
