# ==================== CONFIG.EXAMPLE.PY ====================
# шаблон без реальных ключей. скопируй:  cp config.example.py config.py
# и впиши свои ключи (config.py в .gitignore и в репозиторий не попадает).


# API настройки Нареза
KEY = "mcb_ВСТАВЬ_СВОЙ_КЛЮЧ"
BASE = "https://chat.narez.xyz/chat/api"

# Публикация в канал (True — от имени linux.c, False — в общую ленту)
POST_TO_CHANNEL = False
CHANNEL_NICK = "linux.c"

# Telegram Bridge Config
TG_BOT_TOKEN = "0000000000:AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
MY_TG_ID = 000000000

# Владельцы бота (юзернеймы в маргичате). К ним — с почтением,
# к остальным — с подколами и по памяти.
OWNER_NICKS = ["linux"]

# Память юзеров: факты через маркер [remember: ...] + история переписок
MEMORY_FILE = "memory.json"
MEMORY_HISTORY_LIMIT = 10     # сколько последних сообщений хранить на юзера
MEMORY_FACTS_LIMIT = 40       # потолок фактов на юзера (дальше не пишем)

# Автопосты: шанс, что нейросети разрешат опрос и картинку
POLL_CHANCE = 0.2             # из автопостов каждый пятый — опрос
IMAGE_CHANCE = 0.35           # у трети автопостов нейросеть сама ищет картинку

# Интервалы постинга (в минутах) с рандомом для естественности (~15 - 25 мин)
MIN_POST_INTERVAL_MIN = 15
MAX_POST_INTERVAL_MIN = 25

# Ночной режим сна (часы по локал времени, с SLEEP_START_HOUR  спит по SLEEP_END_HOUR)
SLEEP_START_HOUR = 2
SLEEP_END_HOUR = 7

# Файлы состояния
STATE_FILE = "bot_state.json"
PROMPTS_FILE = "prompts.txt"
VERBOSE = True

# Ограничения запросов
MAX_RESPONSES_PER_MINUTE = 10

# Модели для конвейера ( OpenRouter + Groq резерв )
AI_MODELS_PIPELINE = [
    {"provider": "groq", "model": "openai/gpt-oss-120b"},
    {"provider": "openrouter", "model": "google/gemma-4-26b-a4b-it:free"},
    {"provider": "openrouter", "model": "google/gemma-4-31b-it:free"},
#   {"provider": "openrouter", "model": "xingchen-agi/xing4.0-29b-a4b:free"},
    {"provider": "groq", "model": "openai/gpt-oss-20b"},
    {"provider": "groq", "model": "llama-3.3-70b-versatile"},
]

OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
OPENROUTER_KEY = "sk-or-v1-ВСТАВЬ"

GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"
GROQ_KEY = "gsk_ВСТАВЬ"

# Системный промпт (злой щитпостер, без персонажей)
PROMPT_SYSTEM = (
    "ты — анонимный злой щитпостер из маргичата. пишешь короткие едкие посты "
    "и комментарии на русском языке. "
    "стиль: сарказм, язвительность, бытовой цинизм, абсурд, мат допустим, "
    "но без угроз, без 18+ и без травли по признакам. "
    "строго запрещено: научпоп, википедиевские факты, анекдоты 2007 года, "
    "нейросетевой клишированный мусор, приветствия, вводные слова, морализаторство. "
    "только сухой едкий тезис или разнос, до 300 символов, с юмором. "
    "разрешено использовать markdown."
)
