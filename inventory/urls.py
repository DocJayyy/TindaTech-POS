from django.urls import path
from . import views

urlpatterns = [
    path("receive/", views.receive_stock_view, name="receive_stock"),
    path("stock/", views.stock_list_view, name="stock_list"),
    path("expiration-monitoring/",views.expiration_monitoring_view,name="expiration_monitoring"),
    path("damaged-stock/",views.damaged_stock_view,name="damaged_stock"),
    path("expired-stock/",views.expired_stock_view,name="expired_stock"),
    path("damaged-stock/history/",views.damaged_stock_history_view,name="damaged_stock_history"),
]

