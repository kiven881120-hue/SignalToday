from django.db import migrations


PRESETS = [
    {
        "name": "DeepSeek",
        "provider": "openai-compatible",
        "base_url": "https://api.deepseek.com/v1",
        "default_model": "deepseek-chat",
        "notes": "需要在 .env 里配置 LLM_API_KEY。",
        "is_builtin": True,
    },
    {
        "name": "通义千问",
        "provider": "openai-compatible",
        "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
        "default_model": "qwen-plus",
        "notes": "需要在 .env 里配置 LLM_API_KEY（阿里云百炼 API Key）。",
        "is_builtin": True,
    },
    {
        "name": "Kimi",
        "provider": "openai-compatible",
        "base_url": "https://api.moonshot.cn/v1",
        "default_model": "moonshot-v1-8k",
        "notes": "需要在 .env 里配置 LLM_API_KEY（Moonshot API Key）。",
        "is_builtin": True,
    },
    {
        "name": "OpenAI",
        "provider": "openai-compatible",
        "base_url": "https://api.openai.com/v1",
        "default_model": "gpt-4o-mini",
        "notes": "需要在 .env 里配置 LLM_API_KEY。",
        "is_builtin": True,
    },
    {
        "name": "Ollama (本地)",
        "provider": "openai-compatible",
        "base_url": "http://host.docker.internal:11434/v1",
        "default_model": "llama3.1",
        "notes": "需要在 Ollama 中启用 OpenAI 兼容模式；LLM_API_KEY 可留空或填任意占位字符串。",
        "is_builtin": True,
    },
]


def seed_presets(apps, schema_editor):
    LLMProviderPreset = apps.get_model("settings", "LLMProviderPreset")
    SiteSettings = apps.get_model("settings", "SiteSettings")
    for preset in PRESETS:
        LLMProviderPreset.objects.get_or_create(name=preset["name"], defaults=preset)
    SiteSettings.objects.get_or_create(pk=1)


def remove_presets(apps, schema_editor):
    LLMProviderPreset = apps.get_model("settings", "LLMProviderPreset")
    SiteSettings = apps.get_model("settings", "SiteSettings")
    LLMProviderPreset.objects.filter(name__in=[preset["name"] for preset in PRESETS]).delete()
    SiteSettings.objects.filter(pk=1).delete()


class Migration(migrations.Migration):
    dependencies = [("settings", "0001_initial")]

    operations = [migrations.RunPython(seed_presets, remove_presets)]
