from django.test import TestCase

from apps.settings.models import Category, LLMProviderPreset, SiteSettings


class SiteSettingsSingletonTests(TestCase):
    def test_load_returns_the_single_row_seeded_by_migration(self):
        settings_obj = SiteSettings.load()
        self.assertEqual(settings_obj.pk, 1)
        self.assertEqual(SiteSettings.objects.count(), 1)

    def test_load_creates_singleton_row_if_missing(self):
        SiteSettings.objects.all().delete()
        settings_obj = SiteSettings.load()
        self.assertEqual(settings_obj.pk, 1)
        self.assertEqual(SiteSettings.objects.count(), 1)

    def test_load_returns_existing_singleton(self):
        first = SiteSettings.load()
        first.site_name = "Changed"
        first.save(update_fields=["site_name"])
        second = SiteSettings.load()
        self.assertEqual(second.pk, 1)
        self.assertEqual(second.site_name, "Changed")

    def test_save_always_forces_pk_1(self):
        settings_obj = SiteSettings(site_name="Other")
        settings_obj.save()
        self.assertEqual(settings_obj.pk, 1)
        self.assertEqual(SiteSettings.objects.count(), 1)


class CategorySlugTests(TestCase):
    def test_chinese_name_autoslugs_with_unicode(self):
        category = Category.objects.create(name="人工智能")
        self.assertEqual(category.slug, "人工智能")

    def test_explicit_slug_is_preserved(self):
        category = Category.objects.create(name="汽车", slug="auto")
        self.assertEqual(category.slug, "auto")

    def test_ascii_name_autoslugs_normally(self):
        category = Category.objects.create(name="Climate Change")
        self.assertEqual(category.slug, "climate-change")


class LLMProviderPresetTests(TestCase):
    def test_seed_migration_creates_builtin_presets(self):
        names = set(LLMProviderPreset.objects.filter(is_builtin=True).values_list("name", flat=True))
        self.assertIn("DeepSeek", names)
        self.assertIn("OpenAI", names)

    def test_seed_migration_creates_default_site_settings_row(self):
        self.assertTrue(SiteSettings.objects.filter(pk=1).exists())
