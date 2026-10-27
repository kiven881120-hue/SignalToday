from apps.publishing.review import submit_article_for_review

from ._article_review import ArticleReviewCommand


class Command(ArticleReviewCommand):
    help = "Submit one DRAFT ArticleVersion for human review."
    service = staticmethod(submit_article_for_review)
