

# ---------------------------------------------------------------------------
# Starvell Cardinal: адаптеры Telegram (aiogram) и сервиса Starvell
# ---------------------------------------------------------------------------

from aiogram import F  # noqa: E402
from aiogram.filters import Command  # noqa: E402

try:  # aiogram ≥ 3.4
    from aiogram.types import LinkPreviewOptions as _LinkPreviewOptions
except ImportError:  # pragma: no cover
    _LinkPreviewOptions = None


def _aio_markup(kb: Optional[InlineKeyboardMarkup]) -> Any:
    if kb is None:
        return None
    from aiogram.types import InlineKeyboardButton as AioButton, InlineKeyboardMarkup as AioMarkup
    rows = []
    for row in kb.keyboard:
        items = [AioButton(text=b.text, url=b.url) if b.url else AioButton(text=b.text, callback_data=b.callback_data)
                 for b in row if b is not None]
        if items:
            rows.append(items)
    return AioMarkup(inline_keyboard=rows)


def _preview_kwargs(disabled: Any) -> Dict[str, Any]:
    if not disabled:
        return {}
    if _LinkPreviewOptions is not None:
        return {'link_preview_options': _LinkPreviewOptions(is_disabled=True)}
    return {'disable_web_page_preview': True}


class _BotBridge:
    """Интерфейс telebot поверх aiogram Bot: общий код меню вызывает его из рабочих потоков."""

    def __init__(self, aio_bot: Any):
        self.aio = aio_bot

    def send_message(self, chat_id: Any, text: str, parse_mode: Optional[str] = None,
                     reply_markup: Optional[InlineKeyboardMarkup] = None, disable_web_page_preview: Any = None, **_: Any) -> Any:
        return _run(self.aio.send_message(chat_id=chat_id, text=text, parse_mode=parse_mode, reply_markup=_aio_markup(reply_markup),
                                          **_preview_kwargs(disable_web_page_preview)))

    def edit_message_text(self, text: str, chat_id: Any, message_id: int, parse_mode: Optional[str] = None,
                          reply_markup: Optional[InlineKeyboardMarkup] = None, disable_web_page_preview: Any = None, **_: Any) -> Any:
        return _run(self.aio.edit_message_text(text=text, chat_id=chat_id, message_id=message_id, parse_mode=parse_mode,
                                               reply_markup=_aio_markup(reply_markup), **_preview_kwargs(disable_web_page_preview)))

    def edit_message_reply_markup(self, chat_id: Any, message_id: int, reply_markup: Optional[InlineKeyboardMarkup] = None, **_: Any) -> Any:
        return _run(self.aio.edit_message_reply_markup(chat_id=chat_id, message_id=message_id, reply_markup=_aio_markup(reply_markup)))

    def answer_callback_query(self, callback_id: str, text: Optional[str] = None, show_alert: bool = False, **_: Any) -> Any:
        return _run(self.aio.answer_callback_query(callback_query_id=callback_id, text=text, show_alert=show_alert), timeout=15)

    def delete_message(self, chat_id: Any, message_id: int, **_: Any) -> Any:
        return _run(self.aio.delete_message(chat_id=chat_id, message_id=message_id), timeout=15)


def _config_admin_ids() -> List[int]:
    try:
        from bot.core.config import BotConfig  # type: ignore
        return [_int(x) for x in (BotConfig.ADMIN_IDS() or []) if _int(x)]
    except Exception:
        return []


class _TelegramAdapter:
    def __init__(self, bridge: _BotBridge):
        self.bot = bridge
        self._states: Dict[Tuple[int, int], Dict[str, Any]] = {}

    @property
    def authorized_users(self) -> Dict[int, Dict[str, Any]]:
        known = (_state_get('starvell', {}) or {}).get('admins', [])
        return {x: {} for x in dict.fromkeys(_config_admin_ids() + [_int(x) for x in known if _int(x)])}

    def get_state(self, chat_id: int, user_id: int) -> Optional[Dict[str, Any]]:
        return self._states.get((_int(chat_id), _int(user_id)))

    def set_state(self, chat_id: int, message_id: int, user_id: int, state: str, data: Optional[Dict[str, Any]] = None) -> None:
        self._states[(_int(chat_id), _int(user_id))] = {'state': state, 'mid': message_id, 'data': data or {}}

    def clear_state(self, chat_id: int, user_id: int, del_msg: bool = False) -> Optional[int]:
        item = self._states.pop((_int(chat_id), _int(user_id)), None)
        return item.get('mid') if item else None


def _remember_admin(user_id: Any) -> None:
    """Сообщения и кнопки плагина пропускает AuthMiddleware ядра — значит, это админ."""
    user_id = _int(user_id)
    if not user_id:
        return
    state = _state_get('starvell', {}) or {}
    admins = [_int(x) for x in state.get('admins', [])]
    if user_id not in admins:
        state['admins'] = (admins + [user_id])[-20:]
        _state_set('starvell', state)


_ORDER_STATUS = {'CREATED': 'PAID', 'PAID': 'PAID', 'COMPLETED': 'CLOSED', 'CLOSED': 'CLOSED', 'REFUND': 'REFUNDED',
                 'REFUNDED': 'REFUNDED', 'FAILED': 'FAILED', 'PRE_CREATED': 'UNPAID'}


def _find_dict(value: Any, key: str, depth: int = 0) -> Optional[Dict[str, Any]]:
    if depth > 4 or not isinstance(value, dict):
        return None
    if isinstance(value.get(key), dict):
        return value[key]
    for item in value.values():
        found = _find_dict(item, key, depth + 1)
        if found:
            return found
    return None


def _sv_order(order_id: Any) -> SimpleNamespace:
    """Заказ Starvell в виде, который понимает общий код (как FunPay Order)."""
    service = _service()
    raw = _fp_call(service.get_order_details, str(order_id))
    if not isinstance(raw, dict):
        raise RuntimeError('Starvell не вернул данные заказа.')
    page = raw.get('pageProps') if isinstance(raw.get('pageProps'), dict) else raw
    order = page.get('order') if isinstance(page.get('order'), dict) else (_find_dict(raw, 'order') or {})
    chat = page.get('chat') if isinstance(page.get('chat'), dict) else (_find_dict(raw, 'chat') or {})
    if not order:
        raise RuntimeError('В ответе Starvell нет данных заказа.')
    buyer = order.get('buyer') if isinstance(order.get('buyer'), dict) else {}
    buyer_id = order.get('buyerId') or buyer.get('id')
    rus = _dig(order, 'offerDetails', 'descriptions', 'rus') or {}
    brief = str(rus.get('briefDescription') or rus.get('description') or '')
    chat_id = chat.get('id')
    if not chat_id and buyer_id:
        finder = getattr(service, 'find_chat_by_user_id', None)
        if callable(finder):
            try:
                chat_id = _fp_call(finder, str(buyer_id))
            except Exception:
                chat_id = None
    status = str(order.get('status') or '').upper()
    return SimpleNamespace(
        id=str(order.get('id') or order_id), status=SimpleNamespace(name=_ORDER_STATUS.get(status, status or 'UNKNOWN')),
        full_description=str(rus.get('description') or ''), short_description=brief, description=brief,
        lot_id=_int(order.get('offerId') or _dig(order, 'offer', 'id')), buyer_id=buyer_id,
        buyer_username=str(buyer.get('username') or buyer.get('nickname') or ''),
        amount=max(1, _int(order.get('quantity'), 1)), price=_float(order.get('basePrice') or order.get('totalPrice')) / 100,
        chat_id=chat_id, date=None)


class _StarvellAccount:
    is_initiated = True
    csrf_token = None
    currency = 'RUB'

    @property
    def proxy(self) -> Optional[Dict[str, str]]:
        """Прокси из настроек Starvell Cardinal — режим «прокси бота» в AutoSMM."""
        try:
            url = getattr(getattr(getattr(_service(), 'api', None), 'config', None), 'proxy_url', None)
        except RuntimeError:
            url = None
        return {'http': url, 'https': url} if url else None

    @property
    def id(self) -> Any:
        return _SV.get('me')

    def get_order(self, order_id: Any, *args: Any, **kwargs: Any) -> SimpleNamespace:
        return _sv_order(order_id)

    def refund(self, order_id: Any) -> None:
        result = _fp_call(_service().refund_order, str(order_id), attempts=2)
        error = _api_error(result)
        if error:
            raise RuntimeError(error)


class _StarvellCardinal:
    """Объект с интерфейсом FunPay Cardinal, нужным общему коду заказов."""
    blacklist: List[str] = []
    proxy: Dict[str, str] = {}
    runner = True

    def __init__(self, telegram: Optional[_TelegramAdapter]):
        self.account = _StarvellAccount()
        self.telegram = telegram

    def send_message(self, chat_id: Any, text: str, *args: Any, **kwargs: Any) -> Any:
        """Сообщение в чат Starvell; длинный текст делится на части по MAX_MESSAGE_LEN."""
        if not chat_id or not text:
            return None
        parts, rest = [], str(text)
        while rest:
            if len(rest) <= MAX_MESSAGE_LEN:
                parts.append(rest)
                break
            cut = rest.rfind('\n', 0, MAX_MESSAGE_LEN)
            cut = cut if cut > MAX_MESSAGE_LEN // 2 else MAX_MESSAGE_LEN
            parts.append(rest[:cut])
            rest = rest[cut:].lstrip('\n')
        result = None
        for part in parts:
            try:
                result = _fp_call(_service().send_message, str(chat_id), part, attempts=3)
            except Exception as exc:
                logger.warning('[AutoSMM] Starvell message to %s failed: %s', chat_id, type(exc).__name__)
                return None
            if _api_error(result):
                logger.warning('[AutoSMM] Starvell refused message to %s: %s', chat_id, _api_error(result))
                return None
        return result if result is not None else True

    def is_blacklisted(self, user: Any) -> bool:
        try:
            from bot.core.config import get_config_manager  # type: ignore
            return bool(user) and bool(get_config_manager().is_blacklisted(str(user)))
        except Exception:
            return False


# ---------------------------------------------------------------------------
# Диспетчеры меню (общие с FunPay-версией)
# ---------------------------------------------------------------------------

def _dispatch(q: Any, data: str) -> None:
    parts = data.split(':')
    handler = ROUTES.get(parts[0])
    if not handler:
        _answer(q, 'Меню устарело — откройте /auto_smm_settings заново.', True)
        return
    local = copy(q)
    local.data = data
    try:
        handler(local, *parts[1:])
    except TypeError as exc:
        logger.warning('[AutoSMM] Bad callback %s: %s', data, exc)
        _answer(q, 'Меню устарело — откройте /auto_smm_settings заново.', True)
    except (ValueError, PanelError, ConfigGuardError, LotPublishError) as exc:
        _answer(q, f'❌ {exc}', True)
    except Exception:
        logger.exception('[AutoSMM] Callback %s failed', data)
        _answer(q, '❌ Ошибка. Подробности в журнале Starvell Cardinal.', True)
    finally:
        _answer(local)


def _on_callback(q: Any) -> None:
    if not _is_admin(q):
        return
    _dispatch(q, str(q.data)[len('autosmm:'):])


def _state(message: Any) -> Tuple[str, Dict[str, Any]]:
    data = cardinal_instance.telegram.get_state(message.chat.id, message.from_user.id)
    if not isinstance(data, dict):
        return '', {}
    return str(data.get('state') or ''), data.get('data') if isinstance(data.get('data'), dict) else {}


def _input(message: Any) -> None:
    if not _is_admin(message):
        return
    state, data = _state(message)
    if not state.startswith('autosmm:'):
        return
    text = str(getattr(message, 'text', '') or '').strip()
    handler = INPUTS.get(state[len('autosmm:'):])
    if not handler:
        _clear(message)
        return
    if not text:
        _show(message.chat.id, '❌ Нужен текст. Попробуйте ещё раз или нажмите «Отмена».')
        return
    try:
        handler(message, text, data)
    except (ValueError, PanelError, LotPublishError, ConfigGuardError) as exc:
        _show(message.chat.id, f'❌ {escape(str(exc))}\n\nПопробуйте ещё раз или нажмите «Отмена».')
    except Exception:
        logger.exception('[AutoSMM] Input %s failed', state)
        _show(message.chat.id, '❌ Ошибка сохранения. Подробности в журнале Starvell Cardinal.')


PLUGIN_COMMANDS = {'/start_smm', '/stop_smm', '/auto_smm_settings'}


def _command_name(message: Any) -> str:
    text = str(getattr(message, 'text', '') or '').strip().lower()
    return text.split(maxsplit=1)[0].split('@', 1)[0] if text.startswith('/') else ''


def auto_smm_settings(message: Any) -> None:
    if _is_admin(message):
        _show(message.chat.id, *scr_home())


def start_smm(message: Any) -> None:
    if _is_admin(message):
        _workers(cardinal_instance)
        _show(message.chat.id, '▶️ Фоновые процессы AutoSMM запущены.')


def stop_smm(message: Any) -> None:
    if _is_admin(message):
        _stop_workers()
        _show(message.chat.id, '⏸ Фоновые процессы остановлены. Приём заказов продолжается — чтобы перестать продавать, '
                               'нажмите «🛑 Снять с продажи» в /auto_smm_settings.')


def _command_dispatch(message: Any) -> None:
    command = _command_name(message)
    handler = {'/start_smm': start_smm, '/stop_smm': stop_smm, '/auto_smm_settings': auto_smm_settings}.get(command)
    if not handler:
        return
    try:
        handler(message)
    except Exception:
        logger.exception('[AutoSMM] Command %s failed', command)
        _show(message.chat.id, '❌ Ошибка AutoSMM. Причина записана в журнал Starvell Cardinal.')


def post_init(c: Any) -> None:
    global _READY
    if _READY:
        return
    _READY = True
    try:
        recovered = _recover_submissions()
        for text in list(_pending_admin):
            _notify_admins(text)
        _pending_admin.clear()
        if recovered:
            _notify_admins(f'⚠️ AutoSMM: после перезапуска {recovered} заказ(ов) требуют проверки — откройте «⚠️ Проверить заказы».')
        if load_config()['auto_start']:
            _workers(c)
        logger.info('[AutoSMM] Started on Starvell. Lots: %s, waiting: %s.', len(load_config()['lot_mapping']), len(waiting))
    except Exception:
        logger.exception('[AutoSMM] Post-init failed')


# ---------------------------------------------------------------------------
# Привязка к Starvell Cardinal
# ---------------------------------------------------------------------------

def _attach(service: Any = None, aio_bot: Any = None) -> None:
    global bot, cardinal_instance
    if service is not None:
        _SV['service'] = service
    if aio_bot is not None and (_SV.get('bot') is not aio_bot or bot is None):
        _SV['bot'] = aio_bot
        bot = _BotBridge(aio_bot)
        if cardinal_instance is None:
            cardinal_instance = _StarvellCardinal(_TelegramAdapter(bot))
        else:
            cardinal_instance.telegram = cardinal_instance.telegram or _TelegramAdapter(bot)
            cardinal_instance.telegram.bot = bot
    if cardinal_instance is None:
        cardinal_instance = _StarvellCardinal(None)
    try:
        loop = asyncio.get_running_loop()
        if _SV.get('loop') is None:
            _SV['loop'], _SV['loop_thread'] = loop, threading.get_ident()
    except RuntimeError:
        pass


def _bootstrap() -> None:
    global waiting
    load_config()
    with _wlock:
        waiting = _load_waiting()
    _orders_raw()
    for attempt in range(5):
        try:
            info = _run(_service().get_user_info())
            user = (info or {}).get('user') or {}
            if user.get('id'):
                _SV['me'], _SV['me_name'] = str(user['id']), str(user.get('username') or '')
                break
        except Exception as exc:
            logger.warning('[AutoSMM] Starvell user info unavailable: %s', type(exc).__name__)
        time.sleep(2 + attempt * 2)
    if not _SV.get('me'):
        _admin_later('⚠️ AutoSMM: не удалось получить профиль Starvell — проверьте session_cookie. Заказы начнут обрабатываться после входа.')
        threading.Thread(target=_retry_bootstrap, daemon=True, name='autosmm-login').start()
        return
    post_init(cardinal_instance)


def _retry_bootstrap() -> None:
    for _ in range(120):
        time.sleep(30)
        try:
            info = _run(_service().get_user_info())
            user = (info or {}).get('user') or {}
            if user.get('id'):
                _SV['me'], _SV['me_name'] = str(user['id']), str(user.get('username') or '')
                post_init(cardinal_instance)
                return
        except Exception:
            continue


SETTINGS_BUTTON_TEXT = '⚙️ Настройки AutoSMM'


def _settings_markup(markup: Any) -> Any:
    """Добавляет кнопку настроек первой строкой в aiogram-клавиатуру карточки плагина."""
    from aiogram.types import InlineKeyboardButton as AioButton, InlineKeyboardMarkup as AioMarkup
    rows = list(getattr(markup, 'inline_keyboard', None) or [])
    if any(getattr(b, 'callback_data', None) == 'autosmm:home' for row in rows for b in row):
        return markup
    return AioMarkup(inline_keyboard=[[AioButton(text=SETTINGS_BUTTON_TEXT, callback_data='autosmm:home')]] + rows)


def _install_plugin_card_button() -> int:
    """В карточке плагина Starvell Cardinal нет кнопки настроек — добавляем её только для AutoSMM.
    Сборки рисуют карточку разными функциями, поэтому подменяем все известные."""
    import importlib
    patched = 0
    targets = [('bot.plugins.cp', 'edit_plugin'), ('bot.handlers.plugins_handlers', 'get_plugin_info_menu'),
               ('bot.keyboards', 'get_plugin_info_menu'), ('bot.keyboards.keyboards', 'get_plugin_info_menu'),
               ('bot.keyboards.plugins', 'edit_plugin')]
    for module_name, name in targets:
        try:
            module = importlib.import_module(module_name)
        except Exception:
            continue
        original = getattr(module, name, None)
        if not callable(original):
            continue
        if getattr(original, '_autosmm', False):
            patched += 1
            continue

        def wrapper(*args: Any, __original: Callable = original, **kwargs: Any) -> Any:
            keyboard = __original(*args, **kwargs)
            uuid = kwargs.get('uuid', args[2] if __original.__name__ == 'edit_plugin' and len(args) > 2 else (args[0] if args else ''))
            if str(uuid) != UUID or kwargs.get('ask_delete') or (len(args) > 4 and args[4] is True):
                return keyboard
            try:
                return _settings_markup(keyboard)
            except Exception:
                return keyboard

        wrapper._autosmm = True
        wrapper.__name__ = name
        setattr(module, name, wrapper)
        patched += 1
    return patched


async def _card_request_middleware(make_request: Any, bot_obj: Any, method: Any) -> Any:
    """Подстраховка для любых сборок: если бот отправляет карточку AutoSMM (в тексте и кнопках есть его UUID),
    добавляем кнопку настроек прямо в запрос к Telegram."""
    try:
        text = str(getattr(method, 'text', '') or '')
        markup = getattr(method, 'reply_markup', None)
        rows = getattr(markup, 'inline_keyboard', None)
        if UUID in text and rows:
            datas = [str(getattr(b, 'callback_data', '') or '') for row in rows for b in row]
            texts = [str(getattr(b, 'text', '') or '') for row in rows for b in row]
            is_card = any(UUID in d for d in datas) and not any(t.startswith('✅ Да') for t in texts)
            if is_card and 'autosmm:home' not in datas:
                method = method.model_copy(update={'reply_markup': _settings_markup(markup)})
    except Exception as exc:
        logger.debug('[AutoSMM] Card middleware skipped: %s', exc)
    return await make_request(bot_obj, method)


def _install_card_middleware(aio_bot: Any) -> bool:
    try:
        manager = aio_bot.session.middleware
        if getattr(aio_bot, '_autosmm_card_mw', False):
            return True
        manager(_card_request_middleware)
        try:
            aio_bot._autosmm_card_mw = True
        except Exception:
            pass
        return True
    except Exception as exc:
        logger.warning('[AutoSMM] Cannot install card middleware: %s', type(exc).__name__)
        return False


async def _add_bot_commands(aio_bot: Any) -> None:
    """Ядро задаёт свой список команд — дописываем в него команду AutoSMM, чтобы она была в подсказках."""
    if aio_bot is None:
        return
    try:
        from aiogram.types import BotCommand
        current = list(await aio_bot.get_my_commands())
        if not any(getattr(c, 'command', '') == 'auto_smm_settings' for c in current):
            await aio_bot.set_my_commands(current + [BotCommand(command='auto_smm_settings', description='🚀 AutoSMM: настройки')])
    except Exception as exc:
        logger.warning('[AutoSMM] Cannot add bot command: %s', type(exc).__name__)


async def on_init(aio_bot: Any = None, starvell: Any = None, db: Any = None, plugin_manager: Any = None, *args: Any, **kwargs: Any) -> None:
    """BIND_TO_INIT: ядро и сервис Starvell готовы."""
    logger.info('[AutoSMM] Initialising plugin %s for Starvell Cardinal', VERSION)
    _SV['loop'], _SV['loop_thread'] = asyncio.get_running_loop(), threading.get_ident()
    _attach(starvell, aio_bot)
    patched = _install_plugin_card_button()
    middleware = aio_bot is not None and _install_card_middleware(aio_bot)
    logger.info('[AutoSMM] Settings button: card functions patched=%s, request middleware=%s', patched, middleware)
    await _add_bot_commands(aio_bot)
    await asyncio.to_thread(_bootstrap)


async def on_stop(*args: Any, **kwargs: Any) -> None:
    _stop_workers()


async def on_new_order(order_data: Any, starvell_service: Any = None, *args: Any, **kwargs: Any) -> None:
    """BIND_TO_NEW_ORDER: новый заказ или смена статуса."""
    _attach(starvell_service, kwargs.get('bot'))
    if not isinstance(order_data, dict):
        return
    order_id = str(order_data.get('id') or '').strip()
    status = str(order_data.get('status') or 'CREATED').upper()
    if not order_id:
        return
    if status in ('REFUND', 'REFUNDED'):
        _submit(_sv_status_changed, order_id)
    elif status in ('CREATED', 'PAID'):
        _submit(_sv_new_order, order_id)


def _sv_new_order(order_id: str) -> None:
    for attempt in range(60):
        if _READY:
            break
        time.sleep(2)
    order = None
    for attempt in range(3):
        try:
            order = _sv_order(order_id)
            break
        except Exception as exc:
            logger.warning('[AutoSMM] Order %s details unavailable: %s', order_id, exc)
            time.sleep(2 + attempt * 3)
    if order is None:
        if load_config()['lot_mapping']:
            _notify_admins(f'⚠️ AutoSMM: не удалось получить данные заказа #{order_id} со Starvell. Если это SMM-заказ — '
                           f'обработайте его вручную.\n{_order_url(order_id)}')
        return
    if order.status.name != 'PAID':
        return
    _handle_new_order(cardinal_instance, order)


def _sv_status_changed(order_id: str) -> None:
    event = SimpleNamespace(order=SimpleNamespace(id=order_id, status=SimpleNamespace(name='REFUNDED')))
    on_funpay_order_status_changed(cardinal_instance, event)


async def on_new_message(message_data: Any, starvell_service: Any = None, *args: Any, **kwargs: Any) -> None:
    """BIND_TO_NEW_MESSAGE: сообщение покупателя (свои сообщения ядро не передаёт)."""
    _attach(starvell_service, kwargs.get('bot'))
    if not isinstance(message_data, dict) or not _READY:
        return
    content = str(message_data.get('content') or message_data.get('text') or '').strip()
    if not content:
        return
    author = str(message_data.get('author') or message_data.get('author_id') or '').strip()
    username = str(message_data.get('author_username') or message_data.get('author_nickname') or '')
    if not author.isdigit():
        username, author = username or author, ''
    msg = SimpleNamespace(text=content, chat_id=message_data.get('chat_id'), author_id=author or None, author=username or None,
                          chat_name=None, interlocutor_id=None)
    await asyncio.to_thread(_on_message, cardinal_instance, msg)


def _wrap_message(message: Any) -> SimpleNamespace:
    return SimpleNamespace(text=getattr(message, 'text', '') or '', message_id=getattr(message, 'message_id', 0),
                           chat=SimpleNamespace(id=message.chat.id), from_user=SimpleNamespace(id=message.from_user.id))


def _wrap_callback(callback: Any) -> SimpleNamespace:
    msg = callback.message
    return SimpleNamespace(id=callback.id, data=callback.data, from_user=SimpleNamespace(id=callback.from_user.id),
                           message=SimpleNamespace(chat=SimpleNamespace(id=msg.chat.id), message_id=msg.message_id))


async def _tg_command(message: Any, **kwargs: Any) -> None:
    _attach(None, kwargs.get('bot'))
    _remember_admin(message.from_user.id)
    await asyncio.to_thread(_command_dispatch, _wrap_message(message))


async def _tg_callback(callback: Any, **kwargs: Any) -> None:
    _attach(None, kwargs.get('bot'))
    _remember_admin(callback.from_user.id)
    if not callback.message:
        await callback.answer('Сообщение устарело — откройте /auto_smm_settings.', show_alert=True)
        return
    await asyncio.to_thread(_on_callback, _wrap_callback(callback))


def _tg_text_filter(message: Any, **kwargs: Any) -> bool:
    """Ловим текст только когда плагин ждёт ввод; команды бота проходят дальше."""
    adapter = getattr(cardinal_instance, 'telegram', None)
    if adapter is None or getattr(message, 'from_user', None) is None:
        return False
    state = adapter.get_state(message.chat.id, message.from_user.id)
    if not state or not str(state.get('state', '')).startswith('autosmm:'):
        return False
    text = str(getattr(message, 'text', '') or '')
    if text.startswith('/'):
        adapter.clear_state(message.chat.id, message.from_user.id)
        return False
    return True


async def _tg_text(message: Any, **kwargs: Any) -> None:
    _remember_admin(message.from_user.id)
    await asyncio.to_thread(_input, _wrap_message(message))


COMMANDS = {
    'auto_smm_settings': {'handler': _tg_command, 'description': '🚀 AutoSMM: настройки', 'filters': [Command('auto_smm_settings')]},
    'start_smm': {'handler': _tg_command, 'description': '▶️ AutoSMM: запустить фон', 'filters': [Command('start_smm')]},
    'stop_smm': {'handler': _tg_command, 'description': '⏸ AutoSMM: остановить фон', 'filters': [Command('stop_smm')]},
}
CALLBACKS = {'autosmm': {'handler': _tg_callback, 'filter': F.data.startswith('autosmm:')}}
TEXT_HANDLERS = {'autosmm_input': {'handler': _tg_text, 'filter': _tg_text_filter}}

BIND_TO_INIT = [on_init]
BIND_TO_STOP = [on_stop]
BIND_TO_NEW_ORDER = [on_new_order]
BIND_TO_NEW_MESSAGE = [on_new_message]
BIND_TO_DELETE = None
