"""App routes (mounted at site root via config.urls)."""

from django.urls import path

from .views import UploadView

urlpatterns = [
    path("", UploadView.as_view(), name="upload"),
]
