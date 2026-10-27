from django.db import connection
from django.test import TestCase


class DatabaseConnectionTests(TestCase):
    def test_postgresql_connection(self):
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1")
            result = cursor.fetchone()

        self.assertEqual(connection.vendor, "postgresql")
        self.assertEqual(result, (1,))
