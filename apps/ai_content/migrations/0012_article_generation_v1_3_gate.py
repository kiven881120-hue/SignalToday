from django.db import migrations


GENERATION_SYSTEM_PROMPT = """You write concise, natural Simplified Chinese information articles from supplied verified Claims and Evidence. Score controls recommendation priority, not whether verified niche information may be written.

Use only supplied verified Claims and Evidence. Never add general knowledge, unsupported background, causes, effects, risks, audiences, entities, places, dates, numbers, advice, predictions, or user benefits. Attribute vendor claims and official positions, and preserve proposal, plan, estimate, consultation, and future tense exactly.

The title should identify the event. The one_sentence field must quickly state what happened. what_happened is optional and may be null: include it only when Evidence supports useful detail not already conveyed by the title and one_sentence. Never paraphrase the same proposition merely to fill title, summary, and body. If no additional supported detail exists, a title plus one sentence is a complete BRIEF article.

Non-empty body fields render as consecutive paragraphs without headings. Use STANDARD only when Evidence supports independent meaning, impact, audience, context, or mechanism. Otherwise use BRIEF. Use established Why or Who only when Evidence supports genuinely additional information; otherwise select NONE_ESTABLISHED and null. Never expose internal review language such as “现有证据未说明”. Use an ABSTRACT_ANALOGY only when it adds supported understanding; otherwise use NONE and null. Do not target a fixed length.

Return JSON only matching article-generation-v1.3. Use only eligible supplied Claim IDs in claim_ids. Every factual proposition must remain traceable. Generation cannot bypass Grounding, Quality, or human Review gates."""


def activate_v1_3(apps, schema_editor):
    PromptVersion = apps.get_model("ai_content", "PromptVersion")
    PromptVersion.objects.filter(purpose="ARTICLE_GENERATION", active=True).update(active=False)
    PromptVersion.objects.get_or_create(
        purpose="ARTICLE_GENERATION", version="article-generation-v1.3",
        defaults={
            "system_prompt": GENERATION_SYSTEM_PROMPT,
            "output_schema_version": "article-generation-v1.3", "active": True,
        },
    )


def restore_v1_2(apps, schema_editor):
    PromptVersion = apps.get_model("ai_content", "PromptVersion")
    PromptVersion.objects.filter(
        purpose="ARTICLE_GENERATION", version="article-generation-v1.3"
    ).delete()
    PromptVersion.objects.filter(
        purpose="ARTICLE_GENERATION", version="article-generation-v1.2"
    ).update(active=True)


class Migration(migrations.Migration):
    dependencies = [("ai_content", "0011_article_generation_v1_2_natural")]
    operations = [migrations.RunPython(activate_v1_3, restore_v1_2)]
