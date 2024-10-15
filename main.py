import os
import logging
from telebot.async_telebot import AsyncTeleBot
import asyncio
import psycopg2
import openai
from langdetect import detect, LangDetectException

# Настройка логирования
logging.basicConfig(level=logging.DEBUG, format='%(asctime)s - %(levelname)s - %(message)s')

# Токены и ключи API
TELEGRAM_TOKEN = os.getenv('TELEGRAM_TOKEN')
OPENAI_API_KEY = os.getenv('OPENAI_API_KEY')
TIMESCALE_CONNECTION_STRING = os.getenv('TIMESCALE_CONNECTION_STRING')

if not TELEGRAM_TOKEN or not OPENAI_API_KEY or not TIMESCALE_CONNECTION_STRING:
    logging.error("Не все необходимые переменные окружения установлены.")
    exit(1)

openai.api_key = OPENAI_API_KEY

bot = AsyncTeleBot(TELEGRAM_TOKEN)
async def create_tables():
    conn = psycopg2.connect(TIMESCALE_CONNECTION_STRING)
    c = conn.cursor()

    query_create_chat_history_table = """
    CREATE TABLE IF NOT EXISTS chat_history (
        id SERIAL PRIMARY KEY,
        user_id INTEGER,
        message_role TEXT,
        message_content TEXT,
        timestamp TIMESTAMPTZ DEFAULT NOW()
    );
    """
    c.execute(query_create_chat_history_table)

    query_create_hypertable = """
    SELECT create_hypertable('chat_history', 'timestamp');
    """
    c.execute(query_create_hypertable)

    query_create_users_table = """
    CREATE TABLE IF NOT EXISTS users (
        id SERIAL PRIMARY KEY,
        telegram_id INTEGER UNIQUE,
        name TEXT,
        language TEXT DEFAULT 'en'
    );
    """
    c.execute(query_create_users_table)

    conn.commit()
    c.close()
    conn.close()

async def save_message(user_id, message_role, message_content):
    conn = psycopg2.connect(TIMESCALE_CONNECTION_STRING)
    c = conn.cursor()

    query_insert_message = """
    INSERT INTO chat_history (user_id, message_role, message_content)
    VALUES (%s, %s, %s);
    """
    c.execute(query_insert_message, (user_id, message_role, message_content))

    conn.commit()
    c.close()
    conn.close()

async def get_chat_history(user_id):
    conn = psycopg2.connect(TIMESCALE_CONNECTION_STRING)
    c = conn.cursor()

    query_get_history = """
    SELECT message_role, message_content FROM chat_history
    WHERE user_id = %s ORDER BY timestamp DESC LIMIT 10;
    """
    c.execute(query_get_history, (user_id,))

    chat_history = c.fetchall()
    c.close()
    conn.close()
    return chat_history[::-1]

async def check_user_exists(user_id):
    conn = psycopg2.connect(TIMESCALE_CONNECTION_STRING)
    c = conn.cursor()

    query_check_user = """
    SELECT * FROM users WHERE telegram_id = %s;
    """
    c.execute(query_check_user, (user_id,))

    user_exists = bool(c.fetchone())
    c.close()
    conn.close()
    return user_exists

async def save_user(user_id, name):
    conn = psycopg2.connect(TIMESCALE_CONNECTION_STRING)
    c = conn.cursor()

    query_insert_user = """
    INSERT INTO users (telegram_id, name)
    VALUES (%s, %s);
    """
    c.execute(query_insert_user, (user_id, name))

    conn.commit()
    c.close()
    conn.close()

async def update_user_language(user_id, language):
    conn = psycopg2.connect(TIMESCALE_CONNECTION_STRING)
    c = conn.cursor()

    query_update_language = """
    UPDATE users SET language = %s WHERE telegram_id = %s;
    """
    c.execute(query_update_language, (language, user_id))

    conn.commit()
    c.close()
    conn.close()
async def get_ai_response(user_id, message):
    chat_history = await get_chat_history(user_id)

    messages = [
        {"role": role, "content": content} for role, content in chat_history
    ]

    user_info_query = """
    SELECT name, language FROM users WHERE telegram_id = %s;
    """
    conn = psycopg2.connect(TIMESCALE_CONNECTION_STRING)
    c = conn.cursor()
    c.execute(user_info_query, (user_id,))
    user_info = c.fetchone()
    c.close()
    conn.close()

    system_messages = {
        'ru': f"Вы помощник-продавец. Помните историю разговора и отвечайте осознанно. Отвечайте на том же языке, на котором задают вопрос. Пользователь: {user_info[0]}",
        'en': f"You are a sales assistant. Remember the conversation and answer thoughtfully. Reply in the same language as the user's message. User: {user_info[0]}"
    }
    messages.insert(0, {"role": "system", "content": system_messages[user_info[1]]})

    response = openai.ChatCompletion.create(
        model="gpt-3.5-turbo",
        messages=messages,
        max_tokens=500,
        temperature=0.7,
    )
    return response.choices[0].message.content

async def save_ai_response(user_id, ai_response):
    await save_message(user_id, 'ai', ai_response)


async def process_message(message):
    try:
        # Сохранение сообщения пользователя
        await save_message(message.chat.id, 'user', message.text)

        # Проверка существования пользователя
        if not await check_user_exists(message.chat.id):
            await save_user(message.chat.id, message.from_user.first_name)

        # Определение языка пользователя с использованием значения по умолчанию
        user_language = 'en'  # Значение по умолчанию

        try:
            detected_language = detect(message.text)
            if detected_language == 'ru':
                user_language = 'ru'
            elif detected_language != 'en':
                logging.warning(f"Detected language '{detected_language}' is not supported. Using default 'en'.")
        except LangDetectException:
            logging.warning(f"Failed to detect language for message: {message.text}")

        # Обновление языка пользователя в базе данных
        await update_user_language(message.chat.id, user_language)

        # Получение ответа от ИИ-помощника
        ai_response = await get_ai_response(message.chat.id, message.text)

        # Сохранение ответа ИИ-помощника в базу данных
        await save_ai_response(message.chat.id, ai_response)

        # Отправка ответа пользователю
        await bot.send_message(message.chat.id, ai_response)

    except Exception as e:
        logging.error(f"Error processing message: {e}")
        error_reply = {
            'ru': "Извините, произошла ошибка при обработке вашего запроса.",
            'en': "Sorry, there was an error processing your request."
        }

        # Используем значение по умолчанию для user_language
        default_user_language = 'en'

        # Пытаемся получить язык пользователя из базы данных
        try:
            conn = psycopg2.connect(TIMESCALE_CONNECTION_STRING)
            c = conn.cursor()

            query_get_language = """
            SELECT language FROM users WHERE telegram_id = %s;
            """
            c.execute(query_get_language, (message.chat.id,))
            user_language = c.fetchone()

            if user_language:
                default_user_language = user_language[0]

            c.close()
            conn.close()
        except Exception as db_e:
            logging.error(f"Failed to retrieve user language from database: {db_e}")

        await bot.reply_to(message, error_reply[default_user_language])


# Асинхронный обработчик команды /start
@bot.message_handler(commands=['start'])
async def handle_start(message):
    logging.debug("Handling /start command")
    await save_message(message.chat.id, 'system', "/start command received")
    greeting_text = {
        'ru': "Привет! Я ваш бот-продавец. Чем могу помочь?",
        'en': "Hello! I'm your sales bot. How can I assist you?"
    }
    await bot.reply_to(message, greeting_text['en'])  # Используем английский по умолчанию

# Асинхронный обработчик личных сообщений
@bot.message_handler(func=lambda message: True)
async def handle_private_message(message):
    logging.debug("Handling private message")
    await process_message(message)

# Основная функция для запуска бота
async def main():
    await create_tables()
    logging.info("Запуск бота")
    await bot.polling(none_stop=True)

if __name__ == "__main__":
    asyncio.run(main())
