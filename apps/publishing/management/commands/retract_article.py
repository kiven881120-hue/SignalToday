from apps.publishing.review import retract_article

from ._article_review import ArticleReviewCommand


class Command(ArticleReviewCommand):
    help = "Retract one PUBLISHED ArticleVersion."
    service = staticmethod(retract_article)
