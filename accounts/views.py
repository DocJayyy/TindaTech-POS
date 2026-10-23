
from django.contrib.auth.views import LoginView
from django.shortcuts import redirect
from django.urls import reverse


class POSLoginView(LoginView):
    template_name = "accounts/login.html"
    redirect_authenticated_user = False

    def get_success_url(self):
        user = self.request.user

        if user.is_superuser or user.groups.filter(name="Owner").exists():
            return reverse("dashboard")

        if user.groups.filter(name="Cashier").exists():
            return reverse("cashier_dashboard")

        return reverse("login")
