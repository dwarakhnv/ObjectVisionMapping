from django.urls import path, re_path

from . import views

CAM = r"^mapping/(?P<slug>[a-z0-9][a-z0-9_-]*)/cameras/(?P<cam_id>[A-Za-z0-9][A-Za-z0-9_.-]*)"

urlpatterns = [
    re_path(CAM + r"$", views.api_camera),
    re_path(CAM + r"/points$", views.api_points),
    re_path(CAM + r"/solve$", views.api_solve),
    re_path(CAM + r"/reset$", views.api_reset),
    re_path(CAM + r"/image$", views.api_image),
    path("mapping/<slug:slug>/objects", views.api_world_objects),
]
