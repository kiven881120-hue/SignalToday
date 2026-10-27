from apps.publishing.review import publish_article

from ._article_review import ArticleReviewCommand


class Command(ArticleReviewCommand):
    help = "Publish one explicitly APPROVED ArticleVersion after revalidation."
    service = staticmethod(publish_article)
