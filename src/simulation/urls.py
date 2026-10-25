from django.urls import path

from . import views

urlpatterns = [
    path("", views.home, name="home"),
    path("cameras/", views.cameras_page, name="cameras"),
    path("api/cameras", views.api_cameras),
    path("api/cameras/<str:camera_id>", views.api_camera),
    path("api/cameras/<str:camera_id>/stream.mjpg", views.api_stream),
    path("api/cameras/<str:camera_id>/snapshot.jpg", views.api_snapshot),
    path("api/cameras/<str:camera_id>/resolution", views.api_resolution),
    path("api/cameras/<str:camera_id>/check", views.api_check),
    path("environments/", views.environments_page, name="environments"),
    path("api/envs", views.api_envs),
    path("api/envs/<str:slug>", views.api_env),
    path("plans/", views.plans_page, name="plans"),
    path("api/plans", views.api_plans),
    path("api/plans/images", views.api_plan_image_upload),
    path("api/plans/images/<str:name>", views.api_plan_image),
    path("api/plans/<str:slug>", views.api_plan),
]
