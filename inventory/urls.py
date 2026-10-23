from django.urls import path
from . import views

urlpatterns = [
    path("receive/", views.receive_stock_view, name="receive_stock"),
    path("stock/", views.stock_list_view, name="stock_list"),
]
