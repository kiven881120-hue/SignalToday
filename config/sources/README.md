# Source Configuration

Each JSON file is validated and synchronized with `python manage.py sync_sources`.
Files contain public metadata only; credentials are forbidden. The filename is not an identity:
the required `identifier` field is the stable database key.
