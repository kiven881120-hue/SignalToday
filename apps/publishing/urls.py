from django.urls import path

from . import views


app_name = "publishing"

urlpatterns = [
    path("", views.home, name="home"),
    path("today/", views.home, name="today"),
    path("digest/<str:digest_date>/", views.digest_detail, name="digest-detail"),
    path(
        "digest/<str:digest_date>/article/<int:article_id>/",
        views.digest_article_detail,
        name="digest-article-detail",
    ),
    path("article/<int:article_id>/", views.article_detail, name="article-detail"),
    path("history/", views.history, name="history"),
    path("robots.txt", views.robots, name="robots"),
]
