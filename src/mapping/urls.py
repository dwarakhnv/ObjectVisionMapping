from django.urls import path

from . import views

urlpatterns = [
    path("", views.index, name="mapping_index"),
    path("<slug:slug>/", views.calibrate, name="mapping_calibrate"),
    path("<slug:slug>/live/", views.live, name="mapping_live"),
]
