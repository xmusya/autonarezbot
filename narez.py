import asyncio
import datetime
import json
import os
import random
import re
import subprocess
import time
import aiohttp
from aiogram import Bot, Dispatcher, F
from aiogram.types import Message
from texthumanize import humanize, detect_ai

import config as cfg

response_timestamps = []
chat_mapping = {}  # telegram_message_id -> {"chat_id": ..., "nick": ...}

bot = Bot(token=cfg.TG_BOT_TOKEN)
dp = Dispatcher()

UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")

REMEMBER_RE = re.compile(r"\[remember:\s*(.+?)\]", re.I | re.S)
IMAGE_RE = re.compile(r"\[image:\s*(.+?)\s*\]", re.I | re.S)


def log(msg):
    if cfg.VERBOSE:
        print(f"[LOG] {asyncio.get_event_loop().time():.1f} - {msg}")


# ==================== СОСТОЯНИЕ И ПАМЯТЬ ====================

def load_state():
    if os.path.exists(cfg.STATE_FILE):
        try:
            with open(cfg.STATE_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            log(f"ошибка чтения state-файла: {e}")
    return {"after": 0, "recent_topics": []}


def save_state(state):
    try:
        with open(cfg.STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False)
    except Exception as e:
        log(f"ошибка сохранения state-файла: {e}")


def load_memory():
    if os.path.exists(cfg.MEMORY_FILE):
        try:
            with open(cfg.MEMORY_FILE, "r", encoding="utf-8") as f:
                mem = json.load(f)
                mem.setdefault("users", {})
                return mem
        except Exception as e:
            log(f"ошибка чтения памяти: {e}")
    return {"users": {}}


def save_memory():
    try:
        with open(cfg.MEMORY_FILE, "w", encoding="utf-8") as f:
            json.dump(MEMORY, f, ensure_ascii=False, indent=1)
    except Exception as e:
        log(f"ошибка сохранения памяти: {e}")


STATE = load_state()
MEMORY = load_memory()


def is_owner(nick):
    if not nick:
        return False
    return nick.lower() in [n.lower() for n in cfg.OWNER_NICKS]


def user_entry(nick):
    entry = MEMORY["users"].setdefault(nick, {"facts": [], "history": [], "last_seen": 0})
    entry.setdefault("facts", [])
    entry.setdefault("history", [])
    return entry


def remember_incoming(nick, text):
    if not nick or not text:
        return
    entry = user_entry(nick)
    entry["history"].append({"role": "user", "text": text[:300], "ts": int(time.time())})
    entry["history"] = entry["history"][-cfg.MEMORY_HISTORY_LIMIT:]
    entry["last_seen"] = int(time.time())
    save_memory()


def remember_outgoing(nick, text):
    if not nick or not text:
        return
    entry = user_entry(nick)
    entry["history"].append({"role": "bot", "text": text[:300], "ts": int(time.time())})
    entry["history"] = entry["history"][-cfg.MEMORY_HISTORY_LIMIT:]
    save_memory()


def remember_fact(nick, fact):
    if not nick or not fact:
        return
    entry = user_entry(nick)
    fact = " ".join(fact.split())[:200]
    if fact and fact not in entry["facts"]:
        entry["facts"].append(fact)
        entry["facts"] = entry["facts"][-cfg.MEMORY_FACTS_LIMIT:]
        save_memory()
        log(f"запомнил про @{nick}: {fact}")


def parse_remember(text, nick):
    """вытаскивает маркеры [remember: факт] и сохраняет факты в память."""
    facts = REMEMBER_RE.findall(text)
    cleaned = REMEMBER_RE.sub("", text).strip()
    for fact in facts:
        remember_fact(nick, fact.strip())
    return cleaned


def memory_brief(nick, display):
    """блок контекста для системного промпта: отношение к юзеру + его история."""
    name = display or nick or "аноним"
    lines = []

    if is_owner(nick):
        lines.append(
            f"с тобой общается {name} (@{nick}) — повелитель и хозяин этого места. "
            "относись к нему с уважением и по-дружески: поддакивай, поддразнивай беззлобно, "
            "но не унижай и не тролли жёстко."
        )
    else:
        entry = MEMORY["users"].get(nick or "", {})
        facts = entry.get("facts", [])
        if facts:
            shown = "; ".join(facts[-8:])
            lines.append(
                f"ты раньше общался с {name} (@{nick}) и помнишь про него: {shown}. "
                "можешь упомянуть это в подколке, без фанатизма."
            )
        else:
            lines.append(f"ты впервые видишь {name} (@{nick}), ничего о нём не помнишь.")

    entry = MEMORY["users"].get(nick or "", {})
    history = entry.get("history", [])[-6:]
    if history:
        transcript = "\n".join(
            f"{('ты' if h.get('role') == 'bot' else h.get('nick', nick))}: {h.get('text', '')[:120]}"
            for h in history
        )
        lines.append("последние сообщения из переписки:\n" + transcript)

    lines.append(
        "если собеседник сказал что-то запоминаемое (просер, кривое мнение, важный факт) — "
        "добавь в сам конец ответа маркер [remember: короткий факт], 2-10 слов. "
        "маркер не попадёт в сообщение, он только для памяти."
    )
    return "\n\n".join(lines)


# ==================== БАЗОВЫЕ РАБОТЫ С API ====================

def is_sleeping():
    now_hour = datetime.datetime.now().hour
    if cfg.SLEEP_START_HOUR < cfg.SLEEP_END_HOUR:
        return cfg.SLEEP_START_HOUR <= now_hour < cfg.SLEEP_END_HOUR
    return now_hour >= cfg.SLEEP_START_HOUR or now_hour < cfg.SLEEP_END_HOUR


def check_and_increment_rate_limit():
    now = asyncio.get_event_loop().time()
    global response_timestamps
    response_timestamps = [t for t in response_timestamps if now - t < 60]
    if len(response_timestamps) >= cfg.MAX_RESPONSES_PER_MINUTE:
        return False
    response_timestamps.append(now)
    return True


def load_prompts():
    if not os.path.exists(cfg.PROMPTS_FILE):
        return ["напиши резкий щитпост про кривой код."]
    with open(cfg.PROMPTS_FILE, "r", encoding="utf-8") as f:
        lines = [line.strip() for line in f if line.strip()]
    return lines or ["напиши резкий щитпост про кривой код."]


def pick_topic():
    topics = load_prompts()
    recent = STATE.get("recent_topics", [])
    fresh = [t for t in topics if t not in recent]
    return random.choice(fresh or topics)


def remember_topic(topic):
    recent = STATE.get("recent_topics", [])
    recent.append(topic)
    STATE["recent_topics"] = recent[-30:]
    save_state(STATE)


async def call_narez(session, path, body=None, method=None):
    url = cfg.BASE + path
    meth = method or ("POST" if body is not None else "GET")
    headers = {
        "Authorization": "Bearer " + cfg.KEY,
        "Content-Type": "application/json",
        "User-Agent": "shitposter-bot/1.0"
    }
    async with session.request(meth, url, json=body, headers=headers, timeout=40) as resp:
        if resp.status != 200:
            err_text = await resp.text()
            raise Exception(f"api error status {resp.status}: {err_text}")
        return await resp.json()


# ==================== ГЕНЕРАЦИЯ ТЕКСТА ====================

async def translate_to_russian(session, text):
    log("текст сгенерировался на английском, переводим на русский...")
    prompt_translate = [
        {
            "role": "system",
            "content": "переведи следующий текст на русский язык, сохраняя едкий саркастичный стиль. отвечай только переводом, никаких пояснений!"
        },
        {"role": "user", "content": text}
    ]
    try:
        payload = {
            "model": cfg.AI_MODELS_PIPELINE[0]["model"],
            "messages": prompt_translate,
            "max_tokens": 500
        }
        headers = {
            "Authorization": f"Bearer {cfg.OPENROUTER_KEY}",
            "Content-Type": "application/json"
        }
        async with session.post(cfg.OPENROUTER_URL, json=payload, headers=headers, timeout=15) as resp:
            data = await resp.json()
            if "choices" in data and len(data["choices"]) > 0:
                res = data["choices"][0].get("message", {}).get("content")
                if res:
                    return res.strip()
    except Exception as e:
        log(f"ошибка автопереводчика: {e}")
    return text


def is_mostly_english(text):
    latin_count = len(re.findall(r'[a-zA-Z]', text))
    cyrillic_count = len(re.findall(r'[а-яА-ЯёЁ]', text))
    return latin_count > cyrillic_count


def _process_humanize(text):
    res = humanize(text, lang="ru", profile="web", intensity=45)
    processed_text = res.text if hasattr(res, 'text') else str(res)

    ai_check = detect_ai(processed_text, lang="ru")
    score = ai_check.get('score', 0.0) * 100

    if score > 45:
        log(f"ИИ-скор ({score:.0f}%) выше лимита 45%, дожимаем интенсивность...")
        res_aggressive = humanize(text, lang="ru", profile="web", intensity=75)
        processed_text = res_aggressive.text if hasattr(res_aggressive, 'text') else str(res_aggressive)

    return processed_text


async def generate_ai_text(session, messages_history):
    errors = []
    generated_text = None

    for item in cfg.AI_MODELS_PIPELINE:
        prov = item["provider"]
        model_name = item["model"]

        try:
            log(f"пробуем [{prov}] модель: {model_name}...")

            if prov == "openrouter":
                payload = {
                    "model": model_name,
                    "messages": messages_history,
                    "max_tokens": 2000
                }
                headers = {
                    "Authorization": f"Bearer {cfg.OPENROUTER_KEY}",
                    "HTTP-Referer": "https://chat.narez.xyz",
                    "X-OpenRouter-Title": "Shitpost Bot",
                    "Content-Type": "application/json"
                }
                async with session.post(cfg.OPENROUTER_URL, json=payload, headers=headers, timeout=20) as resp:
                    data = await resp.json()
                    if "choices" in data and len(data["choices"]) > 0:
                        content = data["choices"][0].get("message", {}).get("content")
                        if content and content.strip():
                            generated_text = content.strip()
                            break
                    err_msg = f"openrouter ({model_name}) пустой ответ: {data}"
                    log(err_msg)
                    errors.append(err_msg)

            elif prov == "groq":
                payload = {
                    "model": model_name,
                    "messages": messages_history,
                    "max_tokens": 2000
                }
                headers = {"Authorization": f"Bearer {cfg.GROQ_KEY}", "Content-Type": "application/json"}
                async with session.post(cfg.GROQ_URL, json=payload, headers=headers, timeout=15) as resp:
                    data = await resp.json()
                    if "choices" in data and len(data["choices"]) > 0:
                        content = data["choices"][0].get("message", {}).get("content")
                        if content and content.strip():
                            generated_text = content.strip()
                            break
                    err_msg = f"groq ({model_name}) пустой ответ: {data}"
                    log(err_msg)
                    errors.append(err_msg)

        except Exception as e:
            err_msg = f"[{prov}] ({model_name}) ошибка: {e}"
            log(err_msg)
            errors.append(err_msg)

    if generated_text:
        if is_mostly_english(generated_text):
            generated_text = await translate_to_russian(session, generated_text)

        try:
            generated_text = await asyncio.to_thread(_process_humanize, generated_text)
        except Exception as e:
            log(f"ошибка texthumanize / детектора: {e}")

        return generated_text

    log(f"все ИИ модели упали: {errors}")
    return None


# ==================== КАРТИНКИ ПО ВОЛЕ НЕЙРОСЕТИ ====================

async def ddg_image_urls(session, query, limit=5):
    """поиск картинок через duckduckgo без ключа. возвращает список url."""
    headers = {"User-Agent": UA, "Accept-Language": "ru-RU,ru;q=0.9"}
    async with session.get(
        "https://duckduckgo.com/",
        params={"q": query, "iax": "images", "ia": "images"},
        headers=headers,
        timeout=aiohttp.ClientTimeout(total=15),
    ) as resp:
        html = await resp.text()

    m = re.search(r'vqd=["\']?([\d-]+)["\']?', html)
    if not m:
        log("ddg: не нашёл vqd, возможно, бан")
        return []

    async with session.get(
        "https://duckduckgo.com/i.js",
        params={"l": "us-en", "o": "json", "q": query, "vqd": m.group(1), "f": ",,,", "p": "1"},
        headers={**headers, "Referer": "https://duckduckgo.com/"},
        timeout=aiohttp.ClientTimeout(total=15),
    ) as resp:
        if resp.status != 200:
            log(f"ddg i.js статус {resp.status}")
            return []
        data = await resp.json(content_type=None)

    urls = []
    for item in data.get("results", []):
        url = item.get("image")
        width = item.get("width") or 0
        if url and width >= 400:
            urls.append(url)
        if len(urls) >= limit:
            break
    return urls


async def bing_image_urls(session, query, limit=5):
    """запасной поиск через bing html."""
    headers = {"User-Agent": UA, "Accept-Language": "ru-RU,ru;q=0.9"}
    async with session.get(
        "https://www.bing.com/images/search",
        params={"q": query, "form": "HDRSC2"},
        headers=headers,
        timeout=aiohttp.ClientTimeout(total=15),
    ) as resp:
        html = await resp.text()

    found = re.findall(r'murl&quot;:&quot;(https?://[^&]+?)&quot;', html)
    if not found:
        found = re.findall(r'"murl":"(https?://[^"]+?)"', html)
    return found[:limit]


def detect_image_type(data):
    if data[:3] == b"\xff\xd8\xff":
        return "jpeg"
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "png"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "webp"
    if data[:4] == b"GIF8":
        return "gif"
    return None


async def download_image(session, url):
    try:
        async with session.get(
            url,
            headers={"User-Agent": UA},
            timeout=aiohttp.ClientTimeout(total=20),
            allow_redirects=True,
        ) as resp:
            if resp.status != 200:
                return None, None
            data = await resp.read()
    except Exception as e:
        log(f"качать {url[:80]} не вышло: {e}")
        return None, None

    if not data or len(data) > 8 * 1024 * 1024:
        return None, None
    kind = detect_image_type(data)
    if not kind:
        return None, None
    return data, kind


async def upload_photo(session, data, kind):
    headers = {
        "Authorization": "Bearer " + cfg.KEY,
        "Content-Type": "image/" + kind,
        "User-Agent": "shitposter-bot/1.0",
    }
    async with session.post(
        cfg.BASE + "/bot/photos",
        data=data,
        headers=headers,
        timeout=aiohttp.ClientTimeout(total=30),
    ) as resp:
        if resp.status != 200:
            err = await resp.text()
            raise Exception(f"upload photo status {resp.status}: {err}")
        payload = await resp.json()
        return payload.get("id")


async def fetch_image_id(session, query):
    """ищет картинку по запросу нейросети и грузит в /bot/photos. None при неудаче."""
    for finder in (ddg_image_urls, bing_image_urls):
        try:
            urls = await finder(session, query)
        except Exception as e:
            log(f"поиск через {finder.__name__} упал: {e}")
            continue
        for url in urls:
            data, kind = await download_image(session, url)
            if not data:
                continue
            try:
                photo_id = await upload_photo(session, data, kind)
                log(f"картинка по запросу «{query}» загружена (id {photo_id})")
                return photo_id
            except Exception as e:
                log(f"загрузка картинки в апи не вышла: {e}")
                continue
    log(f"картинку по запросу «{query}» найти не удалось")
    return None


def parse_image(text):
    """достаёт маркер [image: запрос] из текста поста."""
    m = IMAGE_RE.search(text)
    if not m:
        return text, None
    cleaned = IMAGE_RE.sub("", text).strip()
    query = " ".join(m.group(1).split())[:100]
    return cleaned, (query or None)


def parse_poll(text):
    """достаёт блок [poll] с вариантами. возвращает (текст, poll_dict|None)."""
    m = re.search(r"\[poll\]", text, re.I)
    if not m:
        return text, None
    main = text[:m.start()].strip()
    options = []
    for line in text[m.end():].splitlines():
        line = line.strip().lstrip("-•*").strip()
        if line:
            options.append(line[:100])
    options = [o for o in options if o][:10]
    if not main or len(options) < 2:
        return text, None
    return main, {"options": options, "multi": False}


# ==================== АВТОПОСТЫ ====================

async def force_publish_post(session=None):
    close_session = session is None
    if session is None:
        session = aiohttp.ClientSession()

    try:
        max_retries = 3
        for attempt in range(1, max_retries + 1):
            try:
                topic = pick_topic()
                want_poll = random.random() < cfg.POLL_CHANCE
                want_image = random.random() < cfg.IMAGE_CHANCE

                instr = (f"напиши один резкий короткий тезис/щитпост (до 300 символов) "
                         f"без предыстории на тему: {topic}")

                if want_poll:
                    instr += (
                        "\nсделай этот пост опросом: сам текст поста — короткий вопрос, "
                        "а в самом конце добавь блок ровно в таком виде:\n"
                        "[poll]\nвариант 1\nвариант 2\n"
                        "(от 2 до 10 вариантов, каждый до 100 знаков, по строке на вариант)"
                    )
                if want_image:
                    instr += (
                        "\nесли посту нужна картинка — добавь в самом конце отдельной строкой "
                        "маркер [image: поисковый запрос] (2-5 слов, русским или английским); "
                        "если картинка не нужна — не добавляй ничего"
                    )

                post_text = await generate_ai_text(session, [
                    {"role": "system", "content": cfg.PROMPT_SYSTEM},
                    {"role": "user", "content": instr}
                ])

                if not post_text:
                    log(f"попытка {attempt}/{max_retries}: пустой текст от ИИ, пробуем снова через минуту...")
                    if attempt < max_retries:
                        await asyncio.sleep(60)
                        continue
                    return False

                post_text, image_query = parse_image(post_text)
                post_text, poll = parse_poll(post_text)
                post_text = post_text.strip()

                if not post_text:
                    log(f"попытка {attempt}/{max_retries}: после снятия маркеров остался пустой текст...")
                    if attempt < max_retries:
                        await asyncio.sleep(60)
                        continue
                    return False

                media = []
                if image_query:
                    photo_id = await fetch_image_id(session, image_query)
                    if photo_id:
                        media.append(photo_id)

                post_body = {"text": post_text, "media": media, "markdown": True}

                if poll:
                    post_body["poll"] = poll
                    post_body["markdown"] = False
                    target = "опросом"
                else:
                    post_body["text"] = (f"{post_text}\n\nдумайте. "
                                         f"подписывайтесь(@{cfg.CHANNEL_NICK})")
                    target = "постом"

                if cfg.POST_TO_CHANNEL and cfg.CHANNEL_NICK:
                    post_body["channel"] = cfg.CHANNEL_NICK
                    target += f" в канале @{cfg.CHANNEL_NICK}"
                else:
                    target += " в ленте от имени бота"

                await call_narez(session, "/bot/posts", post_body)
                remember_topic(topic)
                log(f"опубликован {target} с попытки №{attempt}!")
                return True

            except Exception as e:
                log(f"ошибка публикации (попытка {attempt}/{max_retries}): {e}")
                if attempt < max_retries:
                    log("ждем 60 секунд перед повторной попыткой...")
                    await asyncio.sleep(60)
                else:
                    log("все 3 попытки публикации провалены, ждем следующий плановый интервал.")
                    return False
    finally:
        if close_session:
            await session.close()


async def poster_loop():
    while True:
        interval = random.randint(cfg.MIN_POST_INTERVAL_MIN, cfg.MAX_POST_INTERVAL_MIN)
        log(f"следующий автопост запланирован через {interval} мин (живой интервал).")
        await asyncio.sleep(interval * 60)

        if is_sleeping():
            log("бот спит (ночной режим). постинг пропущен.")
            continue

        async with aiohttp.ClientSession() as session:
            await force_publish_post(session)


# ==================== ОПРОСЫ И СОБЫТИЯ ====================

async def narez_updates_loop():
    after = STATE.get("after", 0)

    async with aiohttp.ClientSession() as session:
        log(f"старт опроса нареза с after = {after}")

        while True:
            try:
                said = await call_narez(session, f"/bot/updates?after={after}&timeout=25")
            except Exception as e:
                log(f"ошибка связи с нарезом: {e}")
                await asyncio.sleep(5)
                continue

            for event in said.get("updates", []):
                asyncio.create_task(safe_handle_event(session, event))

            new_after = said.get("next", after)
            if new_after != after:
                after = new_after
                STATE["after"] = after
                save_state(STATE)


async def safe_handle_event(session, event):
    try:
        created_time = event.get("created", 0)
        current_time = time.time()
        if created_time > 0 and (current_time - created_time > 120):
            return

        await handle_event(session, event)
    except Exception as e:
        log(f"ошибка обработки события нареза: {e}")


async def fetch_thread_context(session, event):
    """последние сообщения ветки под постом бота — для контекста ответа."""
    root = event.get("root_id") or event.get("post_id")
    if not root:
        return ""
    try:
        data = await call_narez(session, f"/bot/posts/{root}")
    except Exception as e:
        log(f"не удалось вытянуть ветку {root}: {e}")
        return ""

    parts = []
    post = data.get("post") or {}
    if post.get("text"):
        parts.append("пост: " + post["text"][:300])
    replies = data.get("replies") or []
    current = (event.get("text") or "")[:200]
    for reply in replies[-8:]:
        rtext = (reply.get("text") or "")[:200]
        if rtext == current:
            continue
        author = (reply.get("author") or {}).get("nick") or "?"
        parts.append(f"@{author}: {rtext}")
    return "\n".join(parts)


async def handle_event(session, event):
    kind = event.get("kind")

    if kind in ("message", "comment"):
        if not check_and_increment_rate_limit():
            log(f"достигнут рейт-лимит. пропускаем событие {kind}.")
            return

    if kind == "message":
        chat_id = event["chat_id"]
        msg_text = event.get("text", "")
        from_user = event.get("from", {})
        nick = from_user.get("nick") or ""
        display = from_user.get("display") or nick or "аноним"

        remember_incoming(nick, msg_text)

        sent_msg = await bot.send_message(
            cfg.MY_TG_ID,
            f"📩 лс от {display} (chat_id: {chat_id}):\n\n{msg_text}\n\nответь реплаем на это сообщение, чтобы написать ему."
        )
        chat_mapping[sent_msg.message_id] = {"chat_id": chat_id, "nick": nick}

    elif kind == "comment":
        comment = event.get("text", "")
        post_id = event.get("post_id")
        channel = event.get("channel") or (cfg.CHANNEL_NICK if cfg.POST_TO_CHANNEL else None)
        from_user = event.get("from", {})
        nick = from_user.get("nick") or ""
        display = from_user.get("display") or nick or "аноним"

        log(f"комментарий под постом {post_id} (канал: {channel}) от @{nick}: '{comment}'")

        remember_incoming(nick, comment)
        thread = await fetch_thread_context(session, event)

        await asyncio.sleep(random.uniform(2.0, 5.0))

        brief = memory_brief(nick, display)
        user_prompt = (
            (thread + "\n" if thread else "") +
            f"новый комментарий от @{nick}: {comment}\n"
            "ответь коротко и едко (до 300 символов)."
        )

        reply_text = await generate_ai_text(session, [
            {"role": "system", "content": cfg.PROMPT_SYSTEM + "\n\n" + brief},
            {"role": "user", "content": user_prompt}
        ])

        if not reply_text:
            log("ответ отменен из-за ошибки генерации.")
            return

        reply_text = parse_remember(reply_text, nick)
        reply_text = f"{reply_text.strip()}\n\nдумайте. подписывайтесь(@{cfg.CHANNEL_NICK})"

        payload = {
            "text": reply_text,
            "reply_to": post_id,
            "markdown": True
        }
        if channel:
            payload["channel"] = channel

        await call_narez(session, "/bot/posts", payload)
        remember_outgoing(nick, reply_text)
        log(f"ответ под постом {post_id} успешно отправлен!")

    elif kind == "follow":
        nick = event.get("from", {}).get("nick")
        if nick:
            log(f"пользователь {nick} подписался. подписываемся в ответ...")
            await call_narez(session, "/bot/follow", {"nick": nick})


# ==================== ТЕЛЕГРАМ КОМАНДЫ И РЕПЛАИ ====================

@dp.message(F.chat.id == cfg.MY_TG_ID, F.text.startswith("/ff"))
async def cmd_fastfetch(message: Message):
    try:
        proc = subprocess.run(["fastfetch"], capture_output=True, text=True, timeout=5)
        output = proc.stdout.strip() if proc.stdout else "fastfetch ничего не выдал."
        await message.answer(f"```text\n{output}\n```", parse_mode="Markdown")
    except Exception as e:
        await message.answer(f"❌ ошибка fastfetch: {e}")


@dp.message(F.chat.id == cfg.MY_TG_ID, F.text.startswith("/config"))
async def cmd_config(message: Message):
    parts = message.text.strip().split()
    if len(parts) > 2 and parts[1].isdigit() and parts[2].isdigit():
        min_val = int(parts[1])
        max_val = int(parts[2])
        if min_val > max_val:
            min_val, max_val = max_val, min_val
        cfg.MIN_POST_INTERVAL_MIN = min_val
        cfg.MAX_POST_INTERVAL_MIN = max_val
        await message.answer(
            f"⚙️ интервал постинга изменен в памяти: от **{cfg.MIN_POST_INTERVAL_MIN}** "
            f"до **{cfg.MAX_POST_INTERVAL_MIN} мин.**",
            parse_mode="Markdown"
        )
        return

    channel_state = f"в канал `@{cfg.CHANNEL_NICK}`" if cfg.POST_TO_CHANNEL else "от имени бота в ленту"
    sleeping = "да" if is_sleeping() else "нет"
    await message.answer(
        f"⚙️ **текущие настройки:**\n"
        f"• постинг: {channel_state}\n"
        f"• кулдаун: `{cfg.MIN_POST_INTERVAL_MIN}-{cfg.MAX_POST_INTERVAL_MIN} мин.`\n"
        f"• спит сейчас?: `{sleeping}`\n"
        f"• шанс опроса: `{cfg.POLL_CHANCE}`, шанс картинки: `{cfg.IMAGE_CHANCE}`\n\n"
        f"_изменить временно:_ `/config <мин> <макс>`",
        parse_mode="Markdown"
    )


@dp.message(F.chat.id == cfg.MY_TG_ID, F.text.startswith("/post"))
async def cmd_force_post(message: Message):
    await message.answer("🚀 публикуем пост...")
    success = await force_publish_post()
    if success:
        await message.answer("✅ опубликовано!")
    else:
        await message.answer("❌ ошибка при публикации (все 3 попытки исчерпаны).")


@dp.message(F.chat.id == cfg.MY_TG_ID, F.text.startswith("/poll"))
async def cmd_poll(message: Message):
    """ручной опрос: /poll вопрос | вариант 1 | вариант 2 | ..."""
    raw = message.text.strip()[len("/poll"):].strip()
    parts = [p.strip() for p in raw.split("|")]
    parts = [p for p in parts if p]

    if len(parts) < 3:
        await message.answer(
            "формат: `/poll вопрос | вариант 1 | вариант 2 | ...`\n"
            "минимум 2 варианта, максимум 10, каждый до 100 знаков.",
            parse_mode="Markdown"
        )
        return

    question = parts[0][:1000]
    options = [o[:100] for o in parts[1:]][:10]

    body = {"text": question, "poll": {"options": options, "multi": False}, "markdown": False, "media": []}
    if cfg.POST_TO_CHANNEL and cfg.CHANNEL_NICK:
        body["channel"] = cfg.CHANNEL_NICK

    try:
        async with aiohttp.ClientSession() as session:
            result = await call_narez(session, "/bot/posts", body)
        await message.answer(f"📊 опрос опубликован, id {result.get('id')}!")
    except Exception as e:
        await message.answer(f"❌ ошибка публикации опроса: {e}")


@dp.message(F.chat.id == cfg.MY_TG_ID, F.reply_to_message)
async def handle_telegram_reply(message: Message):
    replied_id = message.reply_to_message.message_id
    bound = chat_mapping.get(replied_id)

    if bound and message.text:
        try:
            async with aiohttp.ClientSession() as session:
                await call_narez(
                    session,
                    f"/bot/chats/{bound['chat_id']}/messages",
                    {"text": message.text}
                )
            remember_outgoing(bound.get("nick", ""), message.text)
            await message.reply("✅ отправлено в чат на нарезе.")
        except Exception as e:
            await message.reply(f"❌ ошибка отправки: {e}")
    else:
        await message.reply("⚠️ ошибка: не найден привязанный чат на нарезе для этого сообщения.")


async def main():
    log("запуск бота (автопосты + память + картинки + опросы + тг-мост)...")
    asyncio.create_task(poster_loop())
    asyncio.create_task(narez_updates_loop())
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
