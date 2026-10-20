

# ---------------------------------------------------------------------------
# Диспетчеры Telegram
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
    except (ValueError, PanelError, ConfigGuardError) as exc:
        _answer(q, f'❌ {exc}', True)
    except Exception:
        logger.exception('[AutoSMM] Callback %s failed', data)
        _answer(q, '❌ Ошибка. Подробности в журнале Cardinal.', True)
    finally:
        _answer(local)


def _on_callback(q: Any) -> None:
    if not _is_admin(q):
        return
    _dispatch(q, str(q.data)[len('autosmm:'):])


def _callback_filter(q: Any) -> bool:
    return str(getattr(q, 'data', '') or '').startswith('autosmm:')


def _state(message: Any) -> Tuple[str, Dict[str, Any]]:
    data = cardinal_instance.telegram.get_state(message.chat.id, message.from_user.id)
    if not isinstance(data, dict):
        return '', {}
    return str(data.get('state') or ''), data.get('data') if isinstance(data.get('data'), dict) else {}


def _autosmm_state_filter(message: Any) -> bool:
    try:
        return _is_admin(message) and _state(message)[0].startswith('autosmm:')
    except Exception:
        return False


def _input(message: Any) -> None:
    if not _is_admin(message):
        return
    state, data = _state(message)
    if not state.startswith('autosmm:'):
        return
    text = str(getattr(message, 'text', '') or '').strip()
    if text.startswith('/'):
        _clear(message)
        if _command_name(message) in PLUGIN_COMMANDS:
            _command_dispatch(message)
        else:
            _show(message.chat.id, 'ℹ️ Ввод отменён. Повторите команду.')
        return
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
        _show(message.chat.id, '❌ Ошибка сохранения. Подробности в журнале Cardinal.')


PLUGIN_COMMANDS = {'/start_smm', '/stop_smm', '/auto_smm_settings'}


def _command_name(message: Any) -> str:
    text = str(getattr(message, 'text', '') or '').strip().lower()
    return text.split(maxsplit=1)[0].split('@', 1)[0] if text.startswith('/') else ''


def auto_smm_settings(message: Any) -> None:
    if not _is_admin(message):
        return
    _show(message.chat.id, *scr_home())


def start_smm(message: Any) -> None:
    if not _is_admin(message):
        return
    _workers(cardinal_instance)
    _show(message.chat.id, '▶️ Фоновые процессы AutoSMM запущены.')


def stop_smm(message: Any) -> None:
    if not _is_admin(message):
        return
    _stop_workers()
    _show(message.chat.id, '⏸ Фоновые процессы остановлены. Приём заказов продолжается — чтобы перестать продавать, '
                           'нажмите «🛑 Снять с продажи» в /auto_smm_settings.')


def _command_dispatch(message: Any) -> None:
    if not _is_admin(message):
        return
    command = _command_name(message)
    try:
        {'/start_smm': start_smm, '/stop_smm': stop_smm, '/auto_smm_settings': auto_smm_settings}[command](message)
    except KeyError:
        return
    except Exception:
        logger.exception('[AutoSMM] Command %s failed', command)
        try:
            bot.send_message(message.chat.id, '❌ Ошибка AutoSMM. Причина записана в журнал Cardinal.')
        except Exception:
            pass


def _is_plugin_command(message: Any) -> bool:
    return _command_name(message) in PLUGIN_COMMANDS


def _promote_message_handlers(raw_bot: Any, functions: List[Any]) -> bool:
    """Ставит узкие обработчики AutoSMM перед общими обработчиками Cardinal."""
    handlers = getattr(raw_bot, 'message_handlers', None)
    if not isinstance(handlers, list):
        return False
    selected = [item for item in handlers if isinstance(item, dict) and item.get('function') in functions]
    if len(selected) != len(functions):
        return False
    for item in selected:
        handlers.remove(item)
    handlers[0:0] = selected
    return True


def _register_telegram(c: Any) -> None:
    raw_bot = c.telegram.bot
    register = getattr(raw_bot, 'register_message_handler', None)
    if callable(register):
        register(_command_dispatch, commands=['start_smm', 'stop_smm', 'auto_smm_settings'], func=_is_admin)
        register(_input, func=_autosmm_state_filter)
        if not _promote_message_handlers(raw_bot, [_command_dispatch, _input]):
            logger.warning('[AutoSMM] Handlers installed, but priority could not be verified.')
    else:
        c.telegram.msg_handler(start_smm, commands=['start_smm'])
        c.telegram.msg_handler(stop_smm, commands=['stop_smm'])
        c.telegram.msg_handler(auto_smm_settings, commands=['auto_smm_settings'])
        c.telegram.msg_handler(_input, func=_autosmm_state_filter)
    raw_bot.callback_query_handler(func=_callback_filter)(_on_callback)


# ---------------------------------------------------------------------------
# Запуск плагина
# ---------------------------------------------------------------------------

def init_commands(c: Any) -> None:
    """PRE_INIT: загружаем данные и меню. Аккаунт FunPay в этот момент ещё не загружен."""
    global bot, cardinal_instance, waiting
    try:
        logger.info('[AutoSMM] Initialising plugin %s', VERSION)
        cardinal_instance = c
        load_config()
        with _wlock:
            waiting = _load_waiting()
        _orders_raw()
    except Exception:
        logger.exception('[AutoSMM] CRITICAL: plugin data initialisation failed')
        return
    telegram = getattr(c, 'telegram', None)
    if telegram is None or getattr(telegram, 'bot', None) is None:
        logger.warning('[AutoSMM] Telegram is disabled in Cardinal: orders are processed, the settings menu is unavailable.')
    else:
        try:
            bot = telegram.bot
            c.add_telegram_commands(UUID, [('start_smm', 'Запустить фоновые процессы AutoSMM', True),
                                           ('stop_smm', 'Остановить фоновые процессы AutoSMM', True),
                                           ('auto_smm_settings', 'Настройки AutoSMM', True)])
            _register_telegram(c)
            logger.info('[AutoSMM] Telegram handlers registered: /auto_smm_settings /start_smm /stop_smm')
        except Exception:
            logger.exception('[AutoSMM] Telegram registration failed')
    threading.Thread(target=_post_init_fallback, args=(c,), daemon=True, name='autosmm-postinit').start()


def _post_init_fallback(c: Any) -> None:
    """Если сборка Cardinal не вызывает POST_INIT, стартуем сами после входа в аккаунт."""
    for _ in range(600):
        if _READY:
            return
        if _account_ready() and getattr(c, 'runner', None) is not None:
            time.sleep(5)
            if not _READY:
                logger.warning('[AutoSMM] POST_INIT was not called — starting from fallback.')
                post_init(c)
            return
        time.sleep(1)


_post_init_lock = threading.Lock()


def post_init(c: Any) -> None:
    """POST_INIT: аккаунт FunPay готов — восстанавливаем состояние и запускаем фон."""
    global _READY
    with _post_init_lock:
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
        logger.info('[AutoSMM] Started. Lots: %s, waiting: %s.', len(load_config()['lot_mapping']), len(waiting))
    except Exception:
        logger.exception('[AutoSMM] Post-init failed')


BIND_TO_PRE_INIT = [init_commands]
BIND_TO_POST_INIT = [post_init]
BIND_TO_NEW_MESSAGE = [auto_smm_handler]
BIND_TO_NEW_ORDER = [auto_smm_handler]
BIND_TO_ORDER_STATUS_CHANGED = [on_funpay_order_status_changed]
BIND_TO_DELETE = None
