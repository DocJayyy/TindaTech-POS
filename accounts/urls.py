
from django.urls import path
from .views import POSLoginView

urlpatterns = [
    path("login/", POSLoginView.as_view(), name="login"),
]
