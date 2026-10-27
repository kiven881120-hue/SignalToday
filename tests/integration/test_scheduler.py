from django.core.management import call_command
from django.test import TransactionTestCase


class SchedulerCommandTests(TransactionTestCase):
    def test_scheduler_once_uses_database_lock(self):
        call_command("run_scheduler", once=True)
