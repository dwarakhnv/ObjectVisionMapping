from django.urls import include, path

urlpatterns = [
    path("", include("simulation.urls")),
    path("", include("remote.urls")),
    path("detect/", include("vision.urls")),
    path("legacy/environments/", include("environments.urls")),
    path("mapping/", include("mapping.urls")),
    path("api/", include("environments.api_urls")),
    path("api/", include("vision.api_urls")),
    path("api/", include("mapping.api_urls")),
]


