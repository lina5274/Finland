import logging
import os
import sqlite3
from telebot import TeleBot, types
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from langdetect import detect, LangDetectException
import openai

# Настройка детального логирования
logging.basicConfig(level=logging.DEBUG, format='%(asctime)s - %(levelname)s - %(message)s')

# Токены API (используйте переменные окружения)
TELEGRAM_TOKEN = os.getenv('TELEGRAM_TOKEN')
OPENAI_API_KEY = os.getenv('OPENAI_API_KEY')

# Проверяем наличие токенов
if not TELEGRAM_TOKEN or not OPENAI_API_KEY:
    logging.error("TELEGRAM_TOKEN or OPENAI_API_KEY не заданы.")
    exit(1)

# Инициализация OpenAI API
openai.api_key = OPENAI_API_KEY

# Инициализация бота Telegram
bot = TeleBot(TELEGRAM_TOKEN)


# Создание таблиц для хранения истории сообщений и заказов
def create_tables():
    conn = sqlite3.connect('chat_history.db')
    c = conn.cursor()

    # Таблица для истории чата
    c.execute('''CREATE TABLE IF NOT EXISTS chat_history
                 (user_id INTEGER, message_role TEXT, message_content TEXT, timestamp TEXT)''')

    # Таблица для заказов
    c.execute('''CREATE TABLE IF NOT EXISTS orders
                 (order_id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, product_name TEXT, quantity INTEGER, total_cost REAL, order_date TEXT)''')

    conn.commit()
    conn.close()


# Сохранение сообщения в базу данных
def save_message(user_id, message_role, message_content):
    timestamp = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    conn = sqlite3.connect('chat_history.db')
    c = conn.cursor()
    c.execute("INSERT INTO chat_history (user_id, message_role, message_content, timestamp) VALUES (?, ?, ?, ?)",
              (user_id, message_role, message_content, timestamp))
    conn.commit()
    conn.close()


# Получение истории сообщений для определенного пользователя
def get_chat_history(user_id):
    conn = sqlite3.connect('chat_history.db')
    c = conn.cursor()
    c.execute("SELECT message_role, message_content FROM chat_history WHERE user_id=? ORDER BY rowid DESC LIMIT 10",
              (user_id,))
    chat_history = c.fetchall()
    conn.close()
    return chat_history[::-1]


# Функция для получения информации о товаре из базы данных
def get_product_info(product_name):
    conn = sqlite3.connect('products.db')
    c = conn.cursor()
    c.execute("SELECT * FROM products WHERE name=?", (product_name,))
    product_data = c.fetchone()
    conn.close()
    return product_data


# Определение языка пользователя по последнему сообщению
def get_user_language(user_id):
    conn = sqlite3.connect('chat_history.db')
    c = conn.cursor()
    c.execute(
        "SELECT message_content FROM chat_history WHERE user_id=? AND message_role='user' ORDER BY rowid DESC LIMIT 1",
        (user_id,))
    result = c.fetchone()
    conn.close()
    if result:
        last_message = result[0]
        try:
            language = detect(last_message)
        except LangDetectException:
            language = 'en'
    else:
        language = 'en'
    return language


# Функция для обработки заказа
def process_order(message):
    try:
        # Сохранение сообщения пользователя
        save_message(message.chat.id, 'user', message.text)

        # Определение языка сообщения
        try:
            user_language = detect(message.text)
        except LangDetectException:
            user_language = 'en'

        # Получение истории сообщений
        chat_history = get_chat_history(message.chat.id)
        messages = [
            {"role": role, "content": content} for role, content in chat_history
        ]

        # Добавляем системное сообщение
        system_messages = {
            'ru': "Вы помощник-продавец. Помните историю разговора и отвечайте осознанно. Отвечайте на том же языке, на котором задают вопрос.",
            'en': "You are a sales assistant. Remember the conversation and answer thoughtfully. Reply in the same language as the user's message."
        }
        messages.insert(0, {"role": "system", "content": system_messages[user_language]})

        # Получаем информацию о товаре из базы данных
        product_name = message.text.split()[-1]
        product_info = get_product_info(product_name)

        # Добавляем информацию о товаре в контекст
        if product_info:
            product_context = f"""
            Товар: {product_info[1]}
            Описание: {product_info[2]}
            Цена: {product_info[3]} руб.
            """
            messages.append({"role": "system", "content": product_context})

        response = openai.ChatCompletion.create(
            model="gpt-3.5-turbo",
            messages=messages,
            max_tokens=500,
            temperature=0.7,
        )
        message_text = response.choices[0].message.content

        # Сохранение ответа бота
        save_message(message.chat.id, 'assistant', message_text)

        bot.send_message(message.chat.id, message_text)

        # Предложение сделать заказ
        keyboard = types.InlineKeyboardMarkup()
        buy_button = types.InlineKeyboardButton(text="Купить", callback_data=f"buy_{product_info[1]}")
        keyboard.add(buy_button)
        bot.send_message(message.chat.id, "Хотите купить этот товар?", reply_markup=keyboard)

    except Exception as e:
        logging.error(f"Error processing message: {e}")
        error_reply = {
            'ru': "Извините, произошла ошибка при обработке вашего запроса.",
            'en': "Sorry, there was an error processing your request."
        }
        bot.reply_to(message, error_reply[get_user_language(message.chat.id)])


# Обработчик команды /start
@bot.message_handler(commands=['start'])
def handle_start(message):
    logging.debug("Handling /start command")
    user_language = get_user_language(message.chat.id)
    greeting_text = {
        'ru': "Привет! Я ваш бот-продавец. Чем могу помочь?",
        'en': "Hello! I'm your sales bot. How can I assist you?"
    }
    bot.reply_to(message, greeting_text[user_language])


# Обработчик личных сообщений
@bot.message_handler(func=lambda message: True)
def handle_private_message(message):
    logging.debug("Handling private message")

    # Создаем пул потоков для параллельной обработки сообщений
    with ThreadPoolExecutor(max_workers=20) as executor:
        future = executor.submit(process_order, message)
        future.result()


# Обработчик callback запросов
@bot.callback_query_handler(func=lambda call: True)
def handle_callback(call):
    if call.data.startswith('buy_'):
        product_name = call.data.split('_')[1]
        product_info = get_product_info(product_name)

        if product_info:
            order_id = save_order(call.from_user.id, product_name, 1, product_info[3])
            bot.answer_callback_query(call.id, text="Заказ успешно оформлен!")
            bot.send_message(call.message.chat.id, f"Ваш заказ #{order_id} на товар '{product_name}' успешно оформлен.")
        else:
            bot.answer_callback_query(call.id, text="Извините, произошла ошибка при оформлении заказа.")


# Функция для сохранения заказа в базу данных
def save_order(user_id, product_name, quantity, total_cost):
    timestamp = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    conn = sqlite3.connect('chat_history.db')
    c = conn.cursor()
    c.execute("INSERT INTO orders (user_id, product_name, quantity, total_cost, order_date) VALUES (?, ?, ?, ?, ?)",
              (user_id, product_name, quantity, total_cost, timestamp))
    order_id = c.lastrowid
    conn.commit()
    conn.close()
    return order_id


if __name__ == "__main__":
    create_tables()  # Создание таблиц для хранения истории сообщений и заказов

    # Запуск бота
    logging.info("Запуск бота")
    bot.infinity_polling()
