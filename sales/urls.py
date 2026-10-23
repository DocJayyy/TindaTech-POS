
from django.urls import path
from django.contrib.auth import views as auth_views
from . import views

urlpatterns = [
    path("", views.checkout_view, name="checkout"),
    path("complete/", views.complete_sale_view, name="complete_sale"),
    path("receipt/<int:sale_id>/", views.sale_receipt_view, name="sale_receipt"),
    path("history/", views.sales_history_view, name="sales_history"),
    path("logout/", auth_views.LogoutView.as_view(), name="logout"),
    path("cashier/", views.cashier_dashboard_view, name="cashier_dashboard"),
    path("history/<int:sale_id>/adjustment/",views.sale_adjustment_view,name="sale_adjustment",),
    path("shift/", views.open_shift_page_view, name="open_shift_page"),
    path("shift/open/", views.open_shift_view, name="open_shift"),
    path("x-reading/", views.x_reading_view, name="x_reading"),
    path("x-reading/<int:reading_id>/",views.x_reading_detail_view,name="x_reading_detail"),
    path("x-reading/<int:reading_id>/export/",views.x_reading_export_pdf_view,name="x_reading_export_pdf"),
    path("z-reading/", views.z_reading_view, name="z_reading"),
    path("close-shift/", views.close_shift_view, name="close_shift"),
    path("z-reading/<int:session_id>/",views.z_reading_detail_view,name="z_reading_detail"),
    path("z-reading/<int:session_id>/export/",views.z_reading_export_pdf_view,name="z_reading_export_pdf"),
    path("electronic-journal/export/",views.electronic_journal_export,name="electronic_journal_export"),
    path("electronic-journal/",views.electronic_journal_view,name="electronic_journal"),
]
