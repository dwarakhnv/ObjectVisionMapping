from django.urls import path

from . import views

urlpatterns = [
    path("", views.environment_list, name="environment_list"),
    path("<slug:slug>/", views.environment_detail, name="environment_detail"),
]
