from django.urls import path

from . import views

urlpatterns = [
    path("environments", views.api_environments),
    path("environments/preview", views.api_preview),
    path("environments/convert", views.api_convert),
    path("environments/<slug:slug>", views.api_environment),
]
