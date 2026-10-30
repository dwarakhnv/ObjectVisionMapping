from django.urls import path, re_path

from . import views

REC = r"^api/recordings/(?P<env>[a-z0-9_]+)/(?P<rec_id>[0-9]{8}-[0-9]{6}(?:-[0-9]+)?_[a-z0-9_]+)"

urlpatterns = [
    path("remote/", views.remote_page, name="remote"),
    path("recordings/", views.recordings_page, name="recordings"),
    # Settings, used by the Remote access page
    path("api/remote/subscriptions/<str:sub_id>", views.api_subscription),
    path("api/remote/<str:env>", views.api_remote),
    path("api/remote/<str:env>/subscriptions", views.api_subscriptions),
    path("api/remote/<str:env>/recording", views.api_recording_settings),
    # Public API for other apps and services (API key required)
    path("api/v1/environments/<str:env>", views.v1_environment),
    path("api/v1/environments/<str:env>/objects", views.v1_objects),
    path("api/v1/environments/<str:env>/stream", views.v1_stream),
    # Saved recordings
    path("api/recordings/<str:env>", views.api_recordings),
    re_path(REC + r"$", views.api_recording),
    re_path(REC + r"\.(?:mp4|webm)$", views.api_recording_video),
    re_path(REC + r"\.jpg$", views.api_recording_thumb),
]
