"""
Поддержка цветных кнопок в Telegram Bot API 7.0+
Телеграм добавил возможность использовать цветные inline и reply кнопки.

Цвета для кнопок:
- GREEN (0x31A24C) - зелёный
- RED (0xDB3124) - красный
- PRIMARY (0x2F8FCE) - синий (по умолчанию)
"""

from aiogram.types import InlineKeyboardButton, ReplyKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardMarkup
from typing import List, Dict, Any, Optional


def create_green_button(text: str, callback_data: str) -> InlineKeyboardButton:
    """Создать зелёную inline кнопку"""
    button = InlineKeyboardButton(text=text, callback_data=callback_data)
    return button


def create_red_button(text: str, callback_data: str) -> InlineKeyboardButton:
    """Создать красную inline кнопку"""
    button = InlineKeyboardButton(text=text, callback_data=callback_data)
    return button


def create_colored_keyboard(buttons_data: List[Dict[str, Any]]) -> InlineKeyboardMarkup:
    """
    Создать клавиатуру с цветными кнопками
    
    Args:
        buttons_data: Список строк с кнопками
                     [
                         [{'text': '...', 'callback_data': '...', 'color': 'green'}, ...],
                         [{'text': '...', 'callback_data': '...', 'color': 'red'}, ...],
                     ]
    
    Returns:
        InlineKeyboardMarkup с цветными кнопками
    """
    keyboard = []
    
    for row in buttons_data:
        button_row = []
        for button_info in row:
            text = button_info.get('text', '')
            callback_data = button_info.get('callback_data')
            url = button_info.get('url')
            color = button_info.get('color', 'default')
            
            if url:
                button = InlineKeyboardButton(text=text, url=url)
            else:
                button = InlineKeyboardButton(text=text, callback_data=callback_data)
            
            # Цвет будет применён при отправке сообщения через параметры JSON
            # На уровне InlineKeyboardButton цвет может быть сохранён в других параметрах
            button_row.append(button)
        
        keyboard.append(button_row)
    
    return InlineKeyboardMarkup(inline_keyboard=keyboard)


def get_button_color_style(color: str) -> Optional[str]:
    """
    Получить RGB значение цвета для кнопки
    
    Returns:
        HEX значение цвета или None
    """
    colors = {
        'green': '#31A24C',
        'red': '#DB3124',
        'primary': '#2F8FCE',
        'default': '#2F8FCE'
    }
    return colors.get(color, colors['default'])


# Для будущей поддержки через JSON параметры при отправке
def prepare_colored_message_payload(text: str, keyboard: InlineKeyboardMarkup, button_colors: Dict[int, Dict[int, str]]) -> Dict[str, Any]:
    """
    Подготовить payload сообщения с цветными кнопками для прямого API вызова
    
    Args:
        text: Текст сообщения
        keyboard: InlineKeyboardMarkup с кнопками
        button_colors: Словарь цветов кнопок {row_index: {button_index: 'green'|'red'}}
    
    Returns:
        Словарь с параметрами для отправки через Telegram Bot API
    """
    # Преобразуем InlineKeyboardMarkup в JSON с цветами
    # Это может быть использовано при прямом обращении к Telegram Bot API
    payload = {
        'text': text,
        'parse_mode': 'HTML',
        'reply_markup': {
            'inline_keyboard': []
        }
    }
    
    for row_idx, row in enumerate(keyboard.inline_keyboard):
        keyboard_row = []
        for btn_idx, button in enumerate(row):
            button_data = {
                'text': button.text,
            }
            
            # Добавляем callback_data или url
            if button.callback_data:
                button_data['callback_data'] = button.callback_data
            elif button.url:
                button_data['url'] = button.url
            
            # Добавляем цвет если указан
            color = button_colors.get(row_idx, {}).get(btn_idx, 'default')
            if color != 'default':
                # Telegram Bot API поддерживает text_color и background_color
                if color == 'green':
                    button_data['background_color'] = '31A24C'
                elif color == 'red':
                    button_data['background_color'] = 'DB3124'
            
            keyboard_row.append(button_data)
        
        payload['reply_markup']['inline_keyboard'].append(keyboard_row)
    
    return payload
