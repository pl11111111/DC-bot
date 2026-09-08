import json
import os

# 加载本地化数据
def load_translations():
    try:
        with open('config/translations.json', 'r', encoding='utf-8') as f:
            return json.load(f)
    except FileNotFoundError:
        return {"en": {}}

translations = load_translations()

def get_lang_code(discord_locale):
    lang_map = {
        'en-US': 'en',
        'en-GB': 'en',
        'zh-TW': 'zh-tw',
        'zh-CN': 'zh-cn',
        'ja': 'ja',
        'ko': 'ko'
    }
    return lang_map.get(discord_locale, 'en')

def get_text(key, lang, **kwargs):
    text = translations.get(lang, {}).get(key, translations['en'].get(key, key))
    return text.format(**kwargs)

# 注册新的本地化文件
def register_translations(file_path):
    """
    Register additional translation files 
    """
    global translations
    try:
        with open(file_path, 'r', encoding='utf-8') as f:
            new_translations = json.load(f)
            for lang, trans in new_translations.items():
                if lang in translations:
                    translations[lang].update(trans)
                else:
                    translations[lang] = trans
        return True
    except Exception as e:
        print(f"Error loading translations from {file_path}: {e}")
        return False 