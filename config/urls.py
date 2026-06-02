from django.urls import include, path

urlpatterns = [
    path("", include("reconcile.urls")),
]
