from apps.publishing.review import approve_article

from ._article_review import ArticleReviewCommand


class Command(ArticleReviewCommand):
    help = "Approve one IN_REVIEW ArticleVersion."
    service = staticmethod(approve_article)

    def add_arguments(self, parser):
        super().add_arguments(parser)
        parser.add_argument(
            "--classification", choices=("MUST_KNOW", "DISCOVER"),
            help="Optional human-confirmed publication classification.",
        )

    def run_service(self, options, actor):
        return self.service(
            options["article_version"], options["reason"], actor,
            classification=options["classification"],
        )
