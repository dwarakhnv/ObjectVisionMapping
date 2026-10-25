from django.urls import path, re_path

from . import views

FEED = r"(?P<feed_id>[~@]?[A-Za-z0-9][A-Za-z0-9_.-]*)"

urlpatterns = [
    path("feeds", views.api_feeds),
    re_path(r"^feeds/" + FEED + r"/latest$", views.api_feed_latest),
    re_path(r"^feeds/" + FEED + r"/video$", views.api_feed_video),
    re_path(r"^feeds/" + FEED + r"/frame/(?P<frame_id>[0-9]+)\.jpg$", views.api_feed_frame),
    re_path(r"^feeds/" + FEED + r"/snapshot\.jpg$", views.api_feed_snapshot),
    re_path(r"^feeds/" + FEED + r"/stop$", views.api_feed_stop),
    path("sources", views.api_sources),
    path("object-classes", views.api_object_classes),
    path("detector", views.api_detector),
]
