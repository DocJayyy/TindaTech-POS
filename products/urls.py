
from django.urls import path
from . import views

urlpatterns = [
    path("", views.product_list_view, name="product_list"),
    path("add/", views.product_create_view, name="product_create"),
    path("<int:pk>/edit/", views.product_edit_view, name="product_edit"),
    path("categories/", views.category_list_view, name="category_list"),
    path("categories/add/", views.category_create_view, name="category_create"),
    path("categories/<int:pk>/edit/",views.category_edit_view,name="category_edit"),
]
