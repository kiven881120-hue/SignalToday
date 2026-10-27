from apps.publishing.review import correct_article

from ._article_review import ArticleReviewCommand


class Command(ArticleReviewCommand):
    help = "Correct one PUBLISHED ArticleVersion by opening a new DRAFT revision."
    service = staticmethod(correct_article)
