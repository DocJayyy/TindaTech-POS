from django.urls import path
from . import views

urlpatterns = [
    path("", views.dashboard_view, name="dashboard"),
    path("sales/", views.sales_report_view, name="sales_report"),   
    path("sales/export-csv/",views.export_sales_report_csv,name="export_sales_report_csv"),
    path("settings/", views.account_settings_view, name="account_settings"),
    path("manage-accounts/",views.manage_accounts_view,name="manage_accounts"),
    path("sales-analytics/",views.sales_analytics_view,name="sales_analytics"),
    path("receipt-settings/",views.receipt_settings_view,name="receipt_settings"),
    path("activity-log/",views.activity_log_view,name="activity_log"),
]
