"""Лимиты магазинов.

Вынесены отдельно, чтобы мок и тесты не тянули боевые SDK
(google-auth, pyjwt, http-клиент) ради двух чисел.
"""

from app.models import Store

#: Apple обрезает ответ разработчика на 5970 символах.
APPSTORE_REPLY_CHAR_LIMIT = 5970

#: Google Play — на 350. Это другой жанр письма, а не тот же текст покороче.
GOOGLEPLAY_REPLY_CHAR_LIMIT = 350

#: Окно доступности отзывов в Google Play Developer API, в днях.
#: Всё, что не забрали за это время, потеряно безвозвратно.
GOOGLEPLAY_RETENTION_WINDOW_DAYS = 7

REPLY_CHAR_LIMITS = {
    Store.APP_STORE: APPSTORE_REPLY_CHAR_LIMIT,
    Store.GOOGLE_PLAY: GOOGLEPLAY_REPLY_CHAR_LIMIT,
}
