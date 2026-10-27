from django.contrib.auth import get_user_model
from django.test import Client, TestCase
from django.urls import reverse


class AdminLoginTests(TestCase):
    def test_superuser_login_with_csrf(self):
        username = "stage1-admin"
        password = "Stage1-test-password-only"
        get_user_model().objects.create_superuser(username=username, password=password)
        client = Client(enforce_csrf_checks=True)

        login_url = reverse("admin:login")
        login_page = client.get(login_url)
        csrf_token = login_page.cookies["csrftoken"].value
        response = client.post(
            login_url,
            {
                "username": username,
                "password": password,
                "csrfmiddlewaretoken": csrf_token,
                "next": reverse("admin:index"),
            },
        )

        self.assertEqual(response.status_code, 302)
        self.assertEqual(client.get(reverse("admin:index")).status_code, 200)
