import logging
from pathlib import Path
from aiogram.types import CallbackQuery, Message, InlineKeyboardMarkup, InputMediaPhoto, FSInputFile
from aiogram.enums import ParseMode


# Папка с изображениями
PICTURES_DIR = Path(__file__).parent.parent / "pictures"

# Маппинг названий сообщений на файлы изображений
IMAGE_MAPPING = {
    "Главное меню": "Main_menu.JPG",
    "Как подключиться": "Connection.JPG",
    "Реферальная программа": "Referral_program.jpg",
    "Моя подписка": "My_subscription.jpg",
    "My-not_subscription": "My-not_subscription.jpg",
    "Выбери срок подписки": "Add_a_subscription.JPG",
    "Выбери способ оплаты": "Add_a_subscription.JPG",
    "Оплати": "Add_a_subscription.JPG",
}


def get_image_path(message_key: str) -> Path | None:
    """
    Получить путь к изображению по названию сообщения
    
    Args:
        message_key: Ключ из IMAGE_MAPPING
        
    Returns:
        Path объект или None если изображение не найдено
    """
    filename = IMAGE_MAPPING.get(message_key)
    if not filename:
        return None
    
    image_path = PICTURES_DIR / filename
    
    if not image_path.exists():
        logging.warning(f"Image file not found: {image_path}")
        return None
    
    return image_path


async def edit_text_with_photo(
    callback: CallbackQuery,
    text: str,
    reply_markup: InlineKeyboardMarkup,
    message_key: str,
    parse_mode: ParseMode = ParseMode.HTML
):
    """
    Отредактировать сообщение текстом и изображением (используется edit_media)

    Args:
        callback: CallbackQuery объект
        text: Текст сообщения
        reply_markup: Клавиатура с кнопками
        message_key: Ключ для получения пути к изображению
        parse_mode: Режим парсинга текста
    """
    image_path = get_image_path(message_key)

    if image_path:
        try:
            # Проверяем, содержит ли текущее сообщение медиа
            if callback.message.photo:
                # Сообщение уже содержит фото - редактируем медиа
                await callback.message.edit_media(
                    media=InputMediaPhoto(
                        media=FSInputFile(image_path),
                        caption=text,
                        parse_mode=parse_mode
                    ),
                    reply_markup=reply_markup
                )
            else:
                # Сообщение содержит только текст - удаляем и отправляем новое с фото
                await callback.message.delete()
                await callback.message.answer_photo(
                    photo=FSInputFile(image_path),
                    caption=text,
                    reply_markup=reply_markup,
                    parse_mode=parse_mode
                )
        except Exception as e:
            logging.error(f"Error editing media with photo: {e}")
            # Если ошибка, просто редактируем текст без фото
            try:
                await callback.message.edit_text(text, reply_markup=reply_markup, parse_mode=parse_mode)
            except Exception as e2:
                logging.error(f"Error editing text fallback: {e2}")
    else:
        # Изображение не найдено, редактируем только текст
        try:
            await callback.message.edit_text(text, reply_markup=reply_markup, parse_mode=parse_mode)
        except Exception as e:
            logging.error(f"Error editing text: {e}")


async def send_text_with_photo(
    message: Message,
    text: str,
    reply_markup: InlineKeyboardMarkup,
    message_key: str,
    parse_mode: ParseMode = ParseMode.HTML
):
    """
    Отправить новое сообщение с текстом и изображением

    Args:
        message: Message объект
        text: Текст сообщения
        reply_markup: Клавиатура с кнопками
        message_key: Ключ для получения пути к изображению
        parse_mode: Режим парсинга текста
    """
    image_path = get_image_path(message_key)

    if image_path:
        try:
            # Отправить фото с подписью
            await message.answer_photo(
                photo=FSInputFile(image_path),
                caption=text,
                reply_markup=reply_markup,
                parse_mode=parse_mode
            )
        except Exception as e:
            logging.error(f"Error sending photo: {e}")
            # Если ошибка с фото, отправляем просто текст
            await message.answer(text, reply_markup=reply_markup, parse_mode=parse_mode)
    else:
        # Изображение не найдено, отправляем только текст
        await message.answer(text, reply_markup=reply_markup, parse_mode=parse_mode)


async def send_text_with_photo_callback(
    callback: CallbackQuery,
    text: str,
    reply_markup: InlineKeyboardMarkup,
    message_key: str,
    parse_mode: ParseMode = ParseMode.HTML
):
    """
    Отправить новое сообщение через callback с текстом и изображением

    Args:
        callback: CallbackQuery объект
        text: Текст сообщения
        reply_markup: Клавиатура с кнопками
        message_key: Ключ для получения пути к изображению
        parse_mode: Режим парсинга текста
    """
    image_path = get_image_path(message_key)

    if image_path:
        try:
            # Отправить фото с подписью
            await callback.message.answer_photo(
                photo=FSInputFile(image_path),
                caption=text,
                reply_markup=reply_markup,
                parse_mode=parse_mode
            )
        except Exception as e:
            logging.error(f"Error sending photo via callback: {e}")
            # Если ошибка с фото, отправляем просто текст
            await callback.message.answer(text, reply_markup=reply_markup, parse_mode=parse_mode)
    else:
        # Изображение не найдено, отправляем только текст
        await callback.message.answer(text, reply_markup=reply_markup, parse_mode=parse_mode)
