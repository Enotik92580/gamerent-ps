

# ---------------------------------------------------------------------------
# Telegram: инфраструктура меню
# ---------------------------------------------------------------------------

def InlineKeyboardButton(text: str, **kwargs: Any) -> _TelegramButton:
    if 'callback_data' in kwargs:
        data = 'autosmm:' + kwargs['callback_data']
        if len(data.encode('utf-8')) > 64:
            logger.warning('[AutoSMM] callback_data too long: %s', data)
            data = data.encode('utf-8')[:64].decode('utf-8', 'ignore')
        kwargs['callback_data'] = data
    return _TelegramButton(text, **kwargs)


def B(text: str, data: str) -> _TelegramButton:
    return InlineKeyboardButton(text, callback_data=data)


def U(text: str, url: str) -> _TelegramButton:
    return _TelegramButton(text, url=url)


def _kb(*rows: Optional[List[_TelegramButton]]) -> InlineKeyboardMarkup:
    kb = InlineKeyboardMarkup()
    for row in rows:
        if row:
            kb.row(*row)
    return kb


def _on(value: Any) -> str:
    return '🟢' if value else '🔴'


ROUTES: Dict[str, Callable[..., None]] = {}
INPUTS: Dict[str, Callable[..., None]] = {}


def route(*names: str) -> Callable:
    def deco(func: Callable) -> Callable:
        for name in names:
            ROUTES[name] = func
        return func
    return deco


def inp(name: str) -> Callable:
    def deco(func: Callable) -> Callable:
        INPUTS[name] = func
        return func
    return deco


def _answer(q: Any, text: Optional[str] = None, alert: bool = False) -> None:
    if getattr(q, '_autosmm_answered', False):
        return
    try:
        bot.answer_callback_query(q.id, text=text, show_alert=alert)
    except Exception:
        pass
    try:
        q._autosmm_answered = True
    except Exception:
        pass


def _edit(q: Any, text: str, kb: Optional[InlineKeyboardMarkup] = None) -> None:
    try:
        bot.edit_message_text(text, q.message.chat.id, q.message.message_id, parse_mode='HTML', reply_markup=kb,
                              disable_web_page_preview=True)
    except Exception as exc:
        if 'message is not modified' in str(exc):
            return
        _show(q.message.chat.id, text, kb)


def _show(chat_id: int, text: str, kb: Optional[InlineKeyboardMarkup] = None) -> Optional[Any]:
    try:
        return bot.send_message(chat_id, text, parse_mode='HTML', reply_markup=kb, disable_web_page_preview=True)
    except Exception as exc:
        logger.warning('[AutoSMM] Telegram send failed: %s', type(exc).__name__)
        try:
            return bot.send_message(chat_id, re.sub(r'<[^>]+>', '', text), reply_markup=kb)
        except Exception:
            return None


def _back_kb(back: str = 'home', label: str = '◀️ Назад') -> InlineKeyboardMarkup:
    return _kb([B(label, back)])


def _ask(target: Any, state: str, prompt: str, data: Optional[Dict[str, Any]] = None, back: str = 'home') -> None:
    """Запрашивает ввод с кнопкой отмены. target — callback или сообщение."""
    kb = _kb([B('❌ Отмена', 'x:' + back)])
    if hasattr(target, 'message') and hasattr(target, 'data'):
        chat_id, user_id = target.message.chat.id, target.from_user.id
        _edit(target, prompt, kb)
        message_id = target.message.message_id
    else:
        chat_id, user_id = target.chat.id, target.from_user.id
        sent = _show(chat_id, prompt, kb)
        message_id = getattr(sent, 'message_id', 0)
    cardinal_instance.telegram.set_state(chat_id, message_id, user_id, 'autosmm:' + state, data or {})


def _clear(message: Any) -> None:
    try:
        cardinal_instance.telegram.clear_state(message.chat.id, message.from_user.id, False)
    except Exception:
        pass


def _progress_job(chat_id: int, title: str, func: Callable[[Callable[[int, int], None]], str],
                  kb_done: Optional[InlineKeyboardMarkup] = None, stop_key: str = '') -> None:
    """Запускает долгую операцию в фоне с прогрессом в одном сообщении."""
    stop_kb = _kb([B('⛔ Остановить', 'stopjob:' + stop_key)]) if stop_key else None
    sent = _show(chat_id, f'⏳ <b>{escape(title)}</b>\n\nПодготовка…', stop_kb)
    message_id = getattr(sent, 'message_id', None)

    def progress(done: int, total: int, extra: str = '') -> None:
        if not message_id:
            return
        try:
            bot.edit_message_text(f'⏳ <b>{escape(title)}</b>\n\n{done}/{total}' + (f'\n{extra}' if extra else ''),
                                  chat_id, message_id, parse_mode='HTML', reply_markup=stop_kb)
        except Exception:
            pass

    def run() -> None:
        try:
            result = func(progress)
        except Exception as exc:
            logger.exception('[AutoSMM] Job %s failed', title)
            result = f'❌ Ошибка: {escape(type(exc).__name__)}'
        try:
            if message_id:
                bot.edit_message_text(result[:4000], chat_id, message_id, parse_mode='HTML', reply_markup=kb_done or _back_kb(),
                                      disable_web_page_preview=True)
            else:
                _show(chat_id, result[:4000], kb_done or _back_kb())
        except Exception:
            _show(chat_id, result[:4000], kb_done or _back_kb())
    _submit(run)


def _page(items: List[Any], page: int, per: int) -> Tuple[List[Any], int, int]:
    pages = max(1, math.ceil(len(items) / per))
    page = max(0, min(_int(page), pages - 1))
    return items[page * per:(page + 1) * per], page, pages


def _nav(prefix: str, page: int, pages: int) -> Optional[List[_TelegramButton]]:
    if pages <= 1:
        return None
    row = []
    if page > 0:
        row.append(B('⬅️', f'{prefix}:{page - 1}'))
    row.append(B(f'{page + 1}/{pages}', 'noop'))
    if page < pages - 1:
        row.append(B('➡️', f'{prefix}:{page + 1}'))
    return row


def _choice_token(choice: str) -> str:
    return 'c-' + choice[7:] if choice.startswith('custom:') else choice


def _token_choice(token: str) -> str:
    return 'custom:' + token[2:] if token.startswith('c-') else token


def _template_buttons(prefix: str, cfg: Dict[str, Any], include_auto: bool = True, include_keep: bool = False) -> List[List[_TelegramButton]]:
    buttons = []
    if include_keep:
        buttons.append(B('📌 Как у лота', f'{prefix}:keep'))
    if include_auto:
        buttons.append(B('🎯 По настройкам', f'{prefix}:auto'))
    buttons += [B(THEMES[key]['title'], f'{prefix}:{key}') for key in THEME_ORDER]
    buttons += [B('🧩 ' + _short(v.get('name', k), 20), f'{prefix}:c-{k}') for k, v in cfg['design']['custom'].items()]
    return [buttons[i:i + 2] for i in range(0, len(buttons), 2)]


# ---------------------------------------------------------------------------
# Главное меню
# ---------------------------------------------------------------------------

def scr_home() -> Tuple[str, InlineKeyboardMarkup]:
    cfg = load_config()
    today = _stats(1)
    enabled = sum(1 for x in cfg['services'].values() if x['enabled'])
    lots = cfg['lot_mapping'].values()
    auto = sum(1 for x in lots if x.get('source') == 'auto')
    with _wlock:
        waiting_count = len(waiting)
    orders = _orders()
    in_work = sum(1 for x in orders if x.get('panel_order_id') and not x.get('is_refunded') and
                  str(x.get('status', '')).lower() not in GOOD_STATUSES | BAD_STATUSES | {'manual', 'refunded', 'partial_resolved'} | REVIEW_STATUSES)
    review = sum(1 for x in orders if x.get('status') in REVIEW_STATUSES)
    sales_off = bool(_state_get('sales_off', []))
    low = [cfg['services'][k]['name'] for k, v in (_state_get('balance_low', {}) or {}).items() if v and k in cfg['services']]
    text = (f'🚀 <b>AutoSMM {VERSION}</b>\n━━━━━━━━━━━━━━━━\n'
            f'Статус: {"✅ АКТИВЕН" if RUNNING else "⏸ ОСТАНОВЛЕН (фоновые процессы)"}\n'
            f'Витрина: {"🛑 лоты сняты с продажи" if sales_off else "🟢 лоты в продаже"}\n'
            f'🔌 Панелей: {enabled}/{len(cfg["services"])} · 🛍 Лотов: {len(cfg["lot_mapping"])} (авто {auto})\n'
            f'⏳ Ждут ссылку: {waiting_count} · ⚙️ В работе: {in_work}'
            + (f' · ⚠️ Решить: {review}' if review else '') +
            f'\n💰 Сегодня: {today["orders"]} зак. · прибыль <b>{today["profit"]:.2f} ₽</b>'
            + (f'\n🔴 Низкий баланс: {escape(", ".join(low))}' if low else '') +
            '\n━━━━━━━━━━━━━━━━')
    kb = _kb([B('⏸ Остановить' if RUNNING else '▶️ Запустить', 'run'),
              B('✅ Вернуть в продажу' if sales_off else '🛑 Снять с продажи', 'sales')],
             [B('🛍 Лоты', 'lots'), B('➕ Новый лот', 'new')],
             [B('📦 Автовыгрузка', 'auto') if CAN_CREATE_LOTS else B('📋 Текст для лота', 'ntx'), B('🎨 Оформление', 'ds')],
             [B('🔌 Панели', 'panels'), B('💰 Цены', 'pr')],
             [B('💬 Сообщения', 'msg'), B('♻️ Рефилл', 'rf')],
             [B('⚙️ Настройки', 'st'), B('🌐 Домены', 'dm')],
             [B(f'⚠️ Проверить заказы ({review})' if review else '⚠️ Проверить заказы', 'review'), B('📊 Статистика', 'stats:today')],
             [U('👤 Автор', AUTHOR_URL), U('📣 Канал', CHANNEL_URL)])
    return text, kb


@route('home')
def r_home(q: Any) -> None:
    _edit(q, *scr_home())


@route('noop')
def r_noop(q: Any) -> None:
    _answer(q)


@route('x')
def r_cancel(q: Any, *back: str) -> None:
    try:
        cardinal_instance.telegram.clear_state(q.message.chat.id, q.from_user.id, False)
    except Exception:
        pass
    _dispatch(q, ':'.join(back) or 'home')


@route('run')
def r_run(q: Any) -> None:
    if RUNNING:
        _stop_workers()
        _answer(q, '⏸ Фоновые процессы остановлены. Приём заказов продолжается.', True)
    else:
        _workers(cardinal_instance)
        _answer(q, '▶️ Запущено: слежение за заказами, автоцена, баланс, вотчер.')
    r_home(q)


@route('sales')
def r_sales(q: Any) -> None:
    if _state_get('sales_off', []):
        _edit(q, '✅ <b>Вернуть лоты в продажу?</b>\n\nБудут включены лоты, которые плагин снял с продажи этой кнопкой '
                 '(кроме выключенных из-за низкого баланса).', _kb([B('✅ Да, включить', 'salesgo:on'), B('Отмена', 'home')]))
    else:
        _edit(q, '🛑 <b>Снять все лоты AutoSMM с продажи?</b>\n\nВсе привязанные лоты будут выключены на FunPay. '
                 'Уже оплаченные заказы продолжат выполняться. Вернуть можно этой же кнопкой.',
              _kb([B('🛑 Да, снять', 'salesgo:off'), B('Отмена', 'home')]))


@route('salesgo')
def r_salesgo(q: Any, mode: str) -> None:
    if not _account_ready():
        _answer(q, 'Аккаунт FunPay ещё не загружен.', True)
        return
    _answer(q)
    def job(progress: Callable) -> str:
        changed, errors = (_sales_on if mode == 'on' else _sales_off)(progress)
        text = (f'✅ Включено лотов: {changed}' if mode == 'on' else f'🛑 Выключено лотов: {changed}')
        if errors:
            text += '\n\nОшибки:\n' + '\n'.join('• ' + escape(x) for x in errors[:15])
        return text
    _progress_job(q.message.chat.id, 'Возвращаю лоты в продажу' if mode == 'on' else 'Снимаю лоты с продажи', job,
                  _back_kb('home', '◀️ Главное меню'))


@route('wdis')
def r_watch_disable(q: Any, panel_id: str, service_id: str) -> None:
    lots = [x['funpay_lot_id'] for x in load_config()['lot_mapping'].values()
            if str(x['panel_id']) == panel_id and str(x['service_id']) == service_id and x.get('funpay_lot_id')]
    _answer(q, f'Выключаю лоты: {len(lots)}…')
    def job(progress: Callable) -> str:
        changed, errors = _bulk_active(lots, False, progress)
        return f'🔴 Выключено лотов: {changed} из {len(lots)}. Включите их сами на FunPay, когда проверите услугу.' + \
               (('\n\nОшибки:\n' + '\n'.join('• ' + escape(x) for x in errors[:10])) if errors else '')
    _progress_job(q.message.chat.id, 'Выключаю лоты по услуге', job)
    try:
        bot.edit_message_reply_markup(q.message.chat.id, q.message.message_id, reply_markup=None)
    except Exception:
        pass


@route('wok')
def r_watch_ok(q: Any) -> None:
    _answer(q, 'Принято.')
    try:
        bot.edit_message_reply_markup(q.message.chat.id, q.message.message_id, reply_markup=None)
    except Exception:
        pass


@route('stopjob')
def r_stopjob(q: Any, key: str = '') -> None:
    job = _jobs.get(key)
    if job:
        job['stop'] = True
        _answer(q, 'Останавливаю после текущего лота…')
    else:
        _answer(q, 'Задача уже завершена.')


# ---------------------------------------------------------------------------
# Проверка заказов
# ---------------------------------------------------------------------------

@route('review')
def r_review(q: Any, page: str = '0') -> None:
    orders = [x for x in _orders() if x.get('status') in REVIEW_STATUSES]
    items, page_n, pages = _page(orders, _int(page), 8)
    labels = {'submission_unknown': '❓ запуск не подтверждён', 'awaiting_refund': '↩️ нужен возврат',
              'refund_unknown': '🛑 возврат не подтверждён', 'partial': '◐ частично выполнен'}
    kb = _kb(*[[B(f'#{x["funpay_order_id"]} · {labels.get(x["status"], x["status"])}', 'ro:' + str(x['funpay_order_id']))] for x in items],
             _nav('review', page_n, pages), [B('◀️ Главное меню', 'home')])
    _edit(q, f'⚠️ <b>Проверить заказы</b>\n\nТребуют решения: {len(orders)}.\n'
             'Перед повтором или возвратом проверьте историю SMM-панели.' if orders else
          '⚠️ <b>Проверить заказы</b>\n\n✅ Всё разобрано — заказов, требующих решения, нет.', kb)


@route('ro')
def r_review_order(q: Any, funpay_id: str) -> None:
    order = _get_order(funpay_id)
    if not order:
        _answer(q, 'Заказ не найден.', True)
        return
    text = (f'⚠️ <b>Заказ #{escape(funpay_id)}</b>\nСостояние: {escape(str(order.get("status")))}\n'
            f'Панель: {escape(str(order.get("panel_id")))} · Услуга: {escape(str(order.get("service_id")))}\n'
            f'Количество: {order.get("quantity")}\nСсылка: {escape(str(order.get("link", "")))}\n'
            f'Покупатель: {escape(str(order.get("buyer_username") or "?"))}\n'
            f'Причина: {escape(str(order.get("error") or order.get("refund_reason") or "нужна ручная проверка"))}\n'
            f'<a href="{_order_url(funpay_id)}">Открыть заказ на FunPay</a>')
    rows = []
    if order['status'] == 'submission_unknown':
        rows.append([B('🔗 Заказ найден в панели', 'rs:attach:' + funpay_id), B('🔄 Повторить', 'rs:retry:' + funpay_id)])
        rows.append([B('↩️ Возврат', 'rs:refund:' + funpay_id)])
    else:
        rows.append([B('✅ Разобрано вручную', 'rs:done:' + funpay_id)])
    rows.append([B('◀️ Список', 'review')])
    _edit(q, text, _kb(*rows))


@route('rs')
def r_resolve(q: Any, action: str, funpay_id: str) -> None:
    prompts = {'attach': 'Найдите заказ в истории панели по ссылке, услуге и количеству. Введите его номер для привязки:',
               'retry': 'Проверьте историю панели. Только если заказа там НЕТ, введите <code>НЕ СОЗДАН</code> — плагин повторит запуск один раз.',
               'refund': 'Проверьте историю панели. Только если заказа там НЕТ, введите <code>НЕ СОЗДАН</code> — будет оформлен возврат.',
               'done': 'Проверьте результат и при необходимости сделайте возврат на FunPay. Затем введите <code>РАЗОБРАНО</code>.'}
    if action in prompts:
        _ask(q, 'review', prompts[action], {'action': action, 'id': funpay_id}, back='ro:' + funpay_id)


@inp('review')
def i_review(message: Any, text: str, data: Dict[str, Any]) -> None:
    action, funpay_id = data['action'], data['id']
    order = _get_order(funpay_id)
    if not order:
        raise ValueError('Заказ уже удалён.')
    c = cardinal_instance
    if action == 'attach':
        if order.get('status') != 'submission_unknown':
            raise ValueError('Запуск этого заказа уже разобран.')
        if not text.isdigit() or _int(text) <= 0:
            raise ValueError('Введите номер заказа SMM-панели (цифры).')
        if any(str(x.get('panel_id')) == str(order['panel_id']) and str(x.get('panel_order_id')) == text for x in _orders()):
            raise ValueError('Этот заказ панели уже привязан к другому заказу FunPay.')
        PanelAPI(load_config(), order['panel_id']).status(text)
        _update_order(funpay_id, panel_order_id=text, status='pending', completion_notified=False, started_at=time.time())
        _remove_waiting(funpay_id)
        _clear(message)
        _show(message.chat.id, '✅ Заказ панели привязан. Слежение за статусом продолжится.', _back_kb('review', '◀️ К списку'))
        values = {'panel_order_id': text, 'order_id': text, 'funpay_order_id': funpay_id, 'link': order.get('link', '')}
        if not _send(c, order['chat_id'], _format(load_config()['messages']['after_confirmation'], values)):
            _show(message.chat.id, '⚠️ Не удалось уведомить покупателя. Заказ уже сохранён.')
    elif action in ('retry', 'refund'):
        if order.get('status') != 'submission_unknown':
            raise ValueError('Запуск этого заказа уже разобран.')
        if text.strip().upper() != 'НЕ СОЗДАН':
            raise ValueError('Сначала проверьте историю панели. Для подтверждения введите: НЕ СОЗДАН')
        _clear(message)
        if action == 'retry':
            _update_order(funpay_id, status='retry_ready')
            _submit(_start_order, c, funpay_id, True)
        else:
            _update_order(funpay_id, cost=0)
            _submit(_refund, c, funpay_id, order['chat_id'], 'продавец подтвердил, что услуга не запущена.')
        _show(message.chat.id, '✅ Решение принято. Результат придёт уведомлением.', _back_kb('review', '◀️ К списку'))
    elif action == 'done':
        if text.strip().upper() != 'РАЗОБРАНО':
            raise ValueError('Для закрытия введите: РАЗОБРАНО')
        if order['status'] == 'partial':
            _update_order(funpay_id, status='partial_resolved')
        else:
            _update_order(funpay_id, status='refunded', is_refunded=True)
        _remove_waiting(funpay_id)
        _clear(message)
        _show(message.chat.id, '✅ Ручное решение сохранено.', _back_kb('review', '◀️ К списку'))


# ---------------------------------------------------------------------------
# Лоты: меню, список, поиск
# ---------------------------------------------------------------------------

LOT_FILTERS = {'all': 'Все', 'auto': 'Автовыгрузка', 'man': 'Ручные', 'nofp': 'Без FunPay ID', 'ref': 'С рефиллом'}


def _filter_lots(cfg: Dict[str, Any], flt: str) -> List[Tuple[str, Dict[str, Any]]]:
    items = list(cfg['lot_mapping'].items())
    if flt == 'auto':
        items = [x for x in items if x[1].get('source') == 'auto']
    elif flt == 'man':
        items = [x for x in items if x[1].get('source') != 'auto']
    elif flt == 'nofp':
        items = [x for x in items if not x[1].get('funpay_lot_id')]
    elif flt == 'ref':
        items = [x for x in items if x[1]['refill']['enabled']]
    elif flt.startswith('b_'):
        items = [x for x in items if x[1].get('batch_id') == flt[2:]]
    elif flt.startswith('p_'):
        items = [x for x in items if str(x[1].get('panel_id')) == flt[2:]]
    return sorted(items, key=lambda x: _int(x[0].split('_')[-1]))


@route('lots')
def r_lots(q: Any) -> None:
    cfg = load_config()
    lots = cfg['lot_mapping'].values()
    no_tag = sum(1 for x in lots if x.get('funpay_lot_id') and not x.get('tag_written'))
    text = (f'🛍 <b>Лоты</b>\n\nВсего: {len(cfg["lot_mapping"])} · автовыгрузка: {sum(1 for x in lots if x.get("source") == "auto")} · '
            f'ручные: {sum(1 for x in lots if x.get("source") != "auto")}\n'
            f'Без FunPay ID: {sum(1 for x in lots if not x.get("funpay_lot_id"))}'
            + (f'\n🏷 Тег ещё не записан: {no_tag} (допишется в фоне)' if no_tag else ''))
    kb = _kb([B('🔍 Поиск лота', 'lsearch'), B('📋 Все лоты', 'll:all:0')],
             [B('🤖 Автовыгрузка', 'll:auto:0'), B('✋ Ручные', 'll:man:0')],
             [B('❗ Без FunPay ID', 'll:nofp:0'), B('📦 Выгрузки', 'bts') if CAN_CREATE_LOTS else B('📋 Текст для лота', 'ntx')],
             [B('🎨 Переоформить лоты', 'dsr') if CAN_CREATE_LOTS else None, B('♻️ Рефилл: массово', 'rfb')],
             [B('📦 Кол-во за 1 шт. всем лотам', 'lqall')],
             [B('◀️ Назад', 'home')])
    _edit(q, text, kb)


@route('ll')
def r_lot_list(q: Any, flt: str = 'all', page: str = '0') -> None:
    cfg = load_config()
    items, page_n, pages = _page(_filter_lots(cfg, flt), _int(page), 10)
    rows = []
    for key, lot in items:
        mark = '' if lot.get('funpay_lot_id') else '❗'
        rows.append([B(f'{mark}{_short(lot["name"], 34)} [ID={lot["service_id"]}, Q={lot["quantity"]}]', 'lot:' + key)])
    rows.append(_nav(f'll:{flt}', page_n, pages))
    rows.append([B('◀️ Лоты', 'lots')])
    title = LOT_FILTERS.get(flt) or ('Выгрузка ' + flt[2:] if flt.startswith('b_') else 'Панель ' + flt[2:])
    total = len(_filter_lots(cfg, flt))
    _edit(q, f'📋 <b>{escape(title)}</b> — {total}\n\nНажмите на лот, чтобы открыть карточку.' if total else
          f'📋 <b>{escape(title)}</b>\n\nЛотов нет.', _kb(*rows))


@route('lsearch')
def r_lot_search(q: Any) -> None:
    _ask(q, 'lot_search', '🔍 Введите FunPay ID лота, номер (lot_3), ID услуги или часть названия:', back='lots')


@inp('lot_search')
def i_lot_search(message: Any, text: str, data: Dict[str, Any]) -> None:
    cfg = load_config()
    needle = text.strip().lower()
    found = []
    for key, lot in cfg['lot_mapping'].items():
        if needle in (key.lower(), str(lot.get('funpay_lot_id') or ''), 'svc' + str(lot['service_id'])) or \
                needle in lot['name'].lower() or needle == str(lot['service_id']):
            found.append((key, lot))
    _clear(message)
    if not found:
        _show(message.chat.id, '🔍 Ничего не найдено.', _kb([B('🔍 Искать снова', 'lsearch'), B('◀️ Лоты', 'lots')]))
        return
    rows = [[B(f'{_short(lot["name"], 40)} [ID={lot["service_id"]}]', 'lot:' + key)] for key, lot in found[:10]]
    rows.append([B('🔍 Искать снова', 'lsearch'), B('◀️ Лоты', 'lots')])
    _show(message.chat.id, f'🔍 Найдено: {len(found)}' + (' (показаны первые 10)' if len(found) > 10 else ''), _kb(*rows))


# ---------------------------------------------------------------------------
# Карточка лота
# ---------------------------------------------------------------------------

def scr_lot(key: str) -> Tuple[str, InlineKeyboardMarkup]:
    cfg = load_config()
    lot = cfg['lot_mapping'].get(key)
    if not lot:
        return '❌ Лот не найден (возможно, удалён).', _back_kb('lots')
    panel = cfg['services'].get(lot['panel_id'])
    mode, value = _effective_markup(cfg, lot)
    refill = lot['refill']
    source = {'auto': '🤖 автовыгрузка', 'manual': '🛠 создан вручную', 'bind': '🔗 привязан', 'legacy': '📁 прежняя версия'}.get(lot['source'], lot['source'])
    platform = _platform_info(lot.get('platform') or 'other')
    kind = KINDS.get(lot.get('kind') or 'generic', KINDS['generic'])
    text = (f'🛍 <b>{escape(lot["name"])}</b>\n<code>{key}</code> · {source}\n━━━━━━━━━━━━━━━━\n'
            f'🔌 Панель: {escape(panel["name"]) if panel else "❌ удалена (#" + escape(lot["panel_id"]) + ")"}\n'
            f'🆔 Услуга: <code>{escape(lot["service_id"])}</code> · 📦 За 1 шт.: <b>{lot["quantity"]}</b>\n'
            f'🏷 FunPay ID: ' + (f'<a href="{LOT_URL.format(lot["funpay_lot_id"])}">{lot["funpay_lot_id"]}</a>'
                               if lot['funpay_lot_id'] else '❗️ не привязан (цена меняться не будет)') + '\n'
            + (f'🔖 Тег: <code>#SMM:{lot["tag"]}</code> — {"✅ записан на FunPay" if lot["tag_written"] else "⚠️ не записан"}\n'
               if HAS_LOT_TAGS else '') +
            f'🧭 Тип: {platform["emoji"]} {escape(platform["name"])} · {kind["emoji"]} {escape(kind["ru"].lower())}\n'
            f'🌐 Домены: {escape(", ".join(lot["domains"]) or "общий список")}\n'
            f'🔢 Опрос: {"ВКЛ" if lot["poll"] else "ВЫКЛ"} · 💰 Автоцена: {"ВКЛ" if lot["autoprice"] else "ВЫКЛ"}\n'
            f'📈 Наценка: {"глобальная " if lot["markup_mode"] == "global" else ""}{_markup_text(mode, value)}'
            + (f' · последняя цена {_money(lot["last_price"])} ₽' if lot.get('last_price') else '') + '\n'
            f'♻️ Рефилл: {"ВКЛ" if refill["enabled"] else "ВЫКЛ"}'
            + (f' ({refill["limit"] if refill["limit_enabled"] else "∞"} раз, {_fmt_hours(refill["hours"]) if refill["period_enabled"] else "бессрочно"})'
               if refill['enabled'] else '') +
            f'\n🎨 Оформление: {escape(_template_name(cfg, lot.get("template") or "auto"))}')
    mm = {'global': '🌍 глоб.', 'fixed': '💵 фикс', 'percent': '➗ %'}[lot['markup_mode']]
    kb = _kb([B('✏️ Название', f'le:name:{key}'), B('🆔 ID услуги', f'le:svc:{key}')],
             [B('📦 Количество', f'le:qty:{key}'), B('🔌 Панель', f'lpanel:{key}')],
             [B('🔗 FunPay ID', f'le:fpid:{key}'), B('🏷 Записать тег', f'ltag:{key}') if HAS_LOT_TAGS else B('📋 Текст для лота', f'ltx:{key}')],
             [B('🌐 Домены', f'le:dom:{key}'), B(f'🔢 Опрос: {"ВКЛ" if lot["poll"] else "ВЫКЛ"}', f'lt:poll:{key}')],
             [B(f'💰 Автоцена: {"ВКЛ" if lot["autoprice"] else "ВЫКЛ"}', f'lt:ap:{key}'), B(f'📈 Наценка: {mm}', f'lt:mm:{key}')],
             [B('✏️ Значение наценки', f'le:mval:{key}'), B('💱 Рассчитать цену', f'lcalc:{key}')],
             [B('💵 Цена на FunPay', f'le:price:{key}'), B(f'♻️ Рефилл: {"ВКЛ" if refill["enabled"] else "ВЫКЛ"}', f'lref:{key}')],
             [B('🎨 Оформление', f'ldes:{key}') if CAN_CREATE_LOTS else None, B('📦 Остаток', f'le:stock:{key}')],
             [B('🟢 Вкл. на FunPay', f'lact:{key}:1'), B('🔴 Выкл. на FunPay', f'lact:{key}:0')],
             [B('❌ Удалить', f'ldel:{key}'), B('◀️ К списку', 'll:all:0')])
    return text, kb


@route('lot')
def r_lot(q: Any, key: str) -> None:
    _edit(q, *scr_lot(key))


LOT_EDIT_PROMPTS = {
    'name': ('✏️ Введите название лота <b>как на FunPay</b> (по нему бот узнаёт заказ, если нет тега):', 'name'),
    'svc': ('🆔 Введите ID услуги в панели (число из раздела Services):', 'svc'),
    'qty': ('📦 Сколько единиц услуги заказывать за 1 шт. лота на FunPay?', 'qty'),
    'fpid': (f'🔗 Введите ID лота FunPay — {LOT_ID_HINT}. Бот проверит, что лот существует:', 'fpid'),
    'dom': ('🌐 Введите домены через запятую (например <code>t.me, telegram.me</code>), <code>auto</code> — по типу услуги, '
            '<code>*</code> — любые, <code>-</code> — общий список:', 'dom'),
    'mval': ('✏️ Введите значение наценки лота (процент или рубли — зависит от режима):', 'mval'),
    'price': ('💵 Введите новую цену лота на FunPay за 1 шт. (₽). Если у лота включена автоцена, '
              'она может изменить цену при следующем пересчёте:', 'price'),
    'stock': ('📦 Сколько штук держать в наличии на FunPay? Плагин будет пополнять остаток, когда он упадёт вдвое. '
              '<code>0</code> — не следить:', 'stock'),
}


@route('le')
def r_lot_edit(q: Any, field: str, key: str) -> None:
    lot = _lot_by_key(key)
    if not lot:
        _answer(q, 'Лот не найден.', True)
        return
    if field == 'mval' and lot['markup_mode'] == 'global':
        _answer(q, 'Сначала переключите «📈 Наценка» на фикс или процент.', True)
        return
    if field in ('price', 'stock') and not lot.get('funpay_lot_id'):
        _answer(q, 'Сначала привяжите FunPay ID лота.', True)
        return
    prompt, _ = LOT_EDIT_PROMPTS[field]
    _ask(q, 'lot_edit', prompt, {'field': field, 'key': key}, back='lot:' + key)


@inp('lot_edit')
def i_lot_edit(message: Any, text: str, data: Dict[str, Any]) -> None:
    field, key = data['field'], data['key']
    cfg = load_config()
    lot = cfg['lot_mapping'].get(key)
    if not lot:
        raise ValueError('Лот уже удалён.')
    if field == 'fpid':
        lot_id = _int(text)
        if lot_id <= 0:
            raise ValueError('Введите числовой ID лота FunPay.')
        other = _lot_key_by_funpay_id(cfg, lot_id)
        if other and other != key:
            raise ValueError(f'Этот лот FunPay уже привязан к {other}.')
        _clear(message)
        _show(message.chat.id, '⏳ Проверяю лот на FunPay…')
        _submit(_bind_funpay_id, message.chat.id, key, lot_id)
        return
    if field == 'price':
        price = _float(text, -1)
        if price <= 0:
            raise ValueError('Цена должна быть больше нуля.')
        _clear(message)
        _show(message.chat.id, '⏳ Сохраняю цену на FunPay…')
        def job() -> None:
            try:
                _edit_lot(lot['funpay_lot_id'], {'price': _money(price)})
                with config_tx() as tx:
                    if key in tx['lot_mapping']:
                        tx['lot_mapping'][key]['last_price'] = price
                _show(message.chat.id, f'✅ Цена {_money(price)} ₽ сохранена на FunPay.', _back_kb('lot:' + key, '◀️ К лоту'))
            except Exception as exc:
                _show(message.chat.id, '❌ FunPay не сохранил цену: ' + escape(_rejection_text(exc)), _back_kb('lot:' + key, '◀️ К лоту'))
        _submit(job)
        return
    service = None
    if field == 'svc':
        if not text.strip().isdigit():
            raise ValueError('ID услуги — это число.')
        service = PanelAPI(cfg, lot['panel_id']).service(text.strip())
    with config_tx() as tx:
        item = tx['lot_mapping'].get(key)
        if not item:
            raise ValueError('Лот уже удалён.')
        if field == 'name':
            if not text.strip():
                raise ValueError('Название не может быть пустым.')
            item['name'] = text.strip()[:200]
        elif field == 'svc' and service is not None:
            item['service_id'] = text.strip()
            profile = _service_profile(service)
            item['platform'], item['kind'] = profile['platform'], profile['kind']
            item['poll'] = item['poll'] or 'poll' in str(service.get('type', '')).lower()
        elif field == 'qty':
            if _int(text) <= 0:
                raise ValueError('Ошибка: нужно число больше нуля.')
            item['quantity'] = _int(text)
        elif field == 'dom':
            value = text.strip().lower()
            if value == 'auto':
                item['domains'] = list(_platform_info(item.get('platform') or 'other').get('domains') or [])
            elif value == '-':
                item['domains'] = []
            else:
                item['domains'] = _domains(value)
                if not item['domains']:
                    raise ValueError('Не удалось распознать домены.')
        elif field == 'mval':
            value = _float(text, float('nan'))
            if not math.isfinite(value) or value < 0 or (item['markup_mode'] == 'percent' and value > 10000):
                raise ValueError('Введите неотрицательное число.')
            item['markup_value'] = value
        elif field == 'stock':
            value = _int(text, -1)
            if value < 0 or value > 99999:
                raise ValueError('Введите число от 0 до 99999.')
            item['stock_target'] = value
    _clear(message)
    _show(message.chat.id, '✅ Сохранено.')
    if field in ('qty', 'svc') and lot.get('funpay_lot_id'):
        _show(message.chat.id, '⏳ Обновляю цену и описание лота на FunPay под новое количество…')
        _submit(_refresh_lots_job, message.chat.id, [key])
        return
    _show(message.chat.id, *scr_lot(key))


def _refresh_lots_job(chat_id: int, keys: List[str]) -> None:
    """После смены количества или услуги: пересчитать цену и пересобрать тексты на FunPay."""
    cfg = load_config()
    keys = [k for k in keys if cfg['lot_mapping'].get(k, {}).get('funpay_lot_id')]
    manual_price = [cfg['lot_mapping'][k]['name'] for k in keys if not cfg['lot_mapping'][k].get('autoprice')]
    report = _pricing_pass(manual=True, only_keys=set(keys), force_price=True) if _account_ready() else \
        {'updated': [], 'errors': ['Аккаунт FunPay ещё не загружен.'], 'skipped': [], 'same': 0}
    done, errors = _restyle_lots(keys, 'keep')
    lines = [f'✅ Обновлено лотов на FunPay: {done} из {len(keys)}']
    if report['updated']:
        lines.append('💰 Цены:\n' + '\n'.join('• ' + escape(x) for x in report['updated'][:15]))
    if manual_price:
        lines.append(f'⚠️ У {len(manual_price)} лотов выключена автоцена — цену проверьте вручную (💵 Цена на FunPay).')
    for item in (report['errors'] + errors)[:10]:
        lines.append('❌ ' + escape(item))
    _show(chat_id, '\n\n'.join(lines), _back_kb('lot:' + keys[0] if len(keys) == 1 else 'lots', '◀️ Назад'))


@route('lqall')
def r_lots_qty_all(q: Any) -> None:
    _ask(q, 'lots_qty', '📦 <b>Количество за 1 шт. для всех лотов</b>\n\nВведите, сколько единиц услуги выдавать за 1 шт. лота '
                        '(обычно <code>1</code>). Бот поменяет количество у всех лотов, пересчитает цены и обновит описания на FunPay.',
         back='lots')


@inp('lots_qty')
def i_lots_qty(message: Any, text: str, data: Dict[str, Any]) -> None:
    value = _int(text)
    if value <= 0:
        raise ValueError('Введите число больше нуля.')
    with config_tx() as tx:
        keys = [k for k, v in tx['lot_mapping'].items() if v['quantity'] != value]
        for key in keys:
            tx['lot_mapping'][key]['quantity'] = value
    _clear(message)
    if not keys:
        _show(message.chat.id, f'ℹ️ У всех лотов уже {value} ед. за 1 шт.', _back_kb('lots', '◀️ Лоты'))
        return
    def job(progress: Callable) -> str:
        cfg = load_config()
        with_fp = [k for k in keys if cfg['lot_mapping'].get(k, {}).get('funpay_lot_id')]
        report = _pricing_pass(manual=True, only_keys=set(with_fp), force_price=True)
        done, errors = _restyle_lots(with_fp, 'keep', progress)
        manual = sum(1 for k in with_fp if not cfg['lot_mapping'][k].get('autoprice'))
        text = (f'📦 Количество {value} ед. за 1 шт. установлено у {len(keys)} лотов.\n'
                f'💰 Цен обновлено: {len(report["updated"])} · 🎨 описаний: {done}')
        if manual:
            text += f'\n⚠️ У {manual} лотов выключена автоцена — проверьте их цену вручную.'
        errs = report['errors'] + errors
        if errs:
            text += '\n\nОшибки:\n' + '\n'.join('• ' + escape(x) for x in errs[:10])
        return text
    _progress_job(message.chat.id, 'Обновляю количество, цены и описания', job, _back_kb('lots', '◀️ Лоты'))


def _bind_funpay_id(chat_id: int, key: str, lot_id: int) -> None:
    if not _account_ready():
        _show(chat_id, '⏳ Аккаунт FunPay ещё не загрузился. Повторите через минуту.', _back_kb('lot:' + key, '◀️ К лоту'))
        return
    try:
        fields = _lot_fields(lot_id)
    except Exception as exc:
        _show(chat_id, f'❌ Лот {lot_id} не найден на вашем аккаунте FunPay ({escape(type(exc).__name__)}).',
              _back_kb('lot:' + key, '◀️ К лоту'))
        return
    name = fields.fields.get('fields[summary][ru]') or fields.fields.get('fields[name][ru]') or ''
    node = getattr(getattr(fields, 'subcategory', None), 'id', None) or _int(fields.fields.get('node_id'))
    with config_tx() as tx:
        if key not in tx['lot_mapping']:
            return
        tx['lot_mapping'][key].update(funpay_lot_id=lot_id, tag_written=False, subcategory=node or None,
                                      **({'name': name} if name else {}))
    ok, message = _write_tag(key)
    _show(chat_id, f'✅ Лот FunPay {lot_id} привязан.\n🏷 {escape(message)}', None)
    _show(chat_id, *scr_lot(key))


@route('lpanel')
def r_lot_panel(q: Any, key: str, panel_id: str = '') -> None:
    cfg = load_config()
    if not panel_id:
        rows = [[B(('✅ ' if cfg['lot_mapping'].get(key, {}).get('panel_id') == pid else '') + p['name'], f'lpanel:{key}:{pid}')]
                for pid, p in cfg['services'].items()]
        rows.append([B('◀️ К лоту', 'lot:' + key)])
        _edit(q, '🔌 Выберите панель для лота:', _kb(*rows))
        return
    with config_tx() as tx:
        if key in tx['lot_mapping'] and panel_id in tx['services']:
            tx['lot_mapping'][key]['panel_id'] = panel_id
    _answer(q, '✅ Панель изменена. Проверьте ID услуги — в другой панели он свой.', True)
    r_lot(q, key)


@route('lt')
def r_lot_toggle(q: Any, what: str, key: str) -> None:
    with config_tx() as tx:
        lot = tx['lot_mapping'].get(key)
        if not lot:
            return
        if what == 'poll':
            lot['poll'] = not lot['poll']
        elif what == 'ap':
            lot['autoprice'] = not lot['autoprice']
            if lot['autoprice'] and not lot.get('funpay_lot_id'):
                _answer(q, '⚠️ У лота нет FunPay ID — цена меняться не сможет, пока вы его не привяжете.', True)
        elif what == 'mm':
            lot['markup_mode'] = {'global': 'fixed', 'fixed': 'percent', 'percent': 'global'}[lot['markup_mode']]
            if lot['markup_mode'] != 'global' and not lot.get('markup_value'):
                lot['markup_value'] = tx['pricing']['fixed' if lot['markup_mode'] == 'fixed' else 'percent']
    r_lot(q, key)


@route('ltag')
def r_lot_tag(q: Any, key: str) -> None:
    _answer(q, '⏳ Записываю тег на FunPay…')
    def job() -> None:
        ok, message = _write_tag(key)
        _show(q.message.chat.id, ('✅ ' if ok else '❌ ') + escape(message) +
              ('' if ok else f'\n\nДобавьте вручную строку <code>#SMM:{escape(load_config()["lot_mapping"].get(key, {}).get("tag", ""))}</code> '
                             'в подробное описание лота на FunPay.'), _back_kb('lot:' + key, '◀️ К лоту'))
    _submit(job)


@route('lcalc')
def r_lot_calc(q: Any, key: str) -> None:
    _answer(q, '⏳ Считаю…')
    def job() -> None:
        cfg = load_config()
        lot = cfg['lot_mapping'].get(key)
        if not lot:
            return
        try:
            info = _lot_price_info(cfg, lot)
        except PanelError as exc:
            _show(q.message.chat.id, f'❌ Посчитать нельзя: {escape(str(exc))}', _back_kb('lot:' + key, '◀️ К лоту'))
            return
        fx = _fx_to_rub(info['currency'], cfg)
        text = (f'💱 <b>Расчёт цены</b> — {escape(_short(lot["name"], 60))}\n\n'
                f'Цена провайдера: {info["rate"]:g} {escape(info["currency"])} за 1000\n'
                + (f'Курс: 1 {escape(info["currency"])} = {fx:.2f} ₽\n' if info['currency'] != 'RUB' else '') +
                f'Количество за 1 шт.: {lot["quantity"]}\n'
                f'Себестоимость 1 шт.: <b>{_money(info["cost_rub"])} ₽</b>\n'
                f'Наценка: {_markup_text(info["mode"], info["value"])}\n'
                f'Цена продажи: <b>{_money(info["price"])} ₽</b>'
                + ('' if lot.get('funpay_lot_id') else '\n\n⚠️ У лота нет FunPay ID — цена на FunPay меняться не будет.'))
        _show(q.message.chat.id, text, _back_kb('lot:' + key, '◀️ К лоту'))
    _submit(job)


@route('lact')
def r_lot_active(q: Any, key: str, value: str) -> None:
    lot = _lot_by_key(key)
    if not lot or not lot.get('funpay_lot_id'):
        _answer(q, 'У лота нет FunPay ID.', True)
        return
    _answer(q, '⏳ Сохраняю на FunPay…')
    def job() -> None:
        try:
            changed = _set_lot_active(lot['funpay_lot_id'], value == '1')
            text = ('🟢 Лот включён на FunPay.' if value == '1' else '🔴 Лот выключен на FunPay.') if changed else 'ℹ️ Лот уже в этом состоянии.'
            if value == '1':
                _state_set('sales_off', [x for x in (_state_get('sales_off', []) or []) if x != lot['funpay_lot_id']])
        except Exception as exc:
            text = '❌ FunPay не сохранил лот: ' + escape(_rejection_text(exc))
        _show(q.message.chat.id, text, _back_kb('lot:' + key, '◀️ К лоту'))
    _submit(job)


@route('ldel')
def r_lot_delete(q: Any, key: str) -> None:
    lot = _lot_by_key(key)
    if not lot:
        _answer(q, 'Лот уже удалён.', True)
        return
    rows = [[B('🗑 Только из плагина', f'ldd:{key}:0')]]
    if lot.get('funpay_lot_id') and CAN_DELETE_LOTS:
        rows.append([B('🔥 Из плагина и с FunPay', f'ldd:{key}:1')])
    rows.append([B('Отмена', 'lot:' + key)])
    _edit(q, f'❌ <b>Удалить лот?</b>\n\n{escape(lot["name"])}\n\nПосле удаления из плагина заказы по этому лоту '
             'перестанут обрабатываться автоматически.', _kb(*rows))


@route('ldd')
def r_lot_delete_do(q: Any, key: str, remote: str) -> None:
    lot = _lot_by_key(key)
    if not lot:
        _answer(q, 'Лот уже удалён.', True)
        return
    def finish(extra: str = '') -> None:
        with config_tx(allow_shrink=True) as tx:
            tx['lot_mapping'].pop(key, None)
        _show(q.message.chat.id, '✅ Лот удалён из плагина.' + extra, _back_kb('lots', '◀️ Лоты'))
    if remote == '1' and lot.get('funpay_lot_id'):
        _answer(q, '⏳ Удаляю на FunPay…')
        def job() -> None:
            try:
                _delete_funpay_lot(lot['funpay_lot_id'])
                finish(' Лот удалён и на FunPay.')
            except Exception as exc:
                _show(q.message.chat.id, '❌ FunPay не удалил лот: ' + escape(_rejection_text(exc)) +
                      '\nЛот в плагине сохранён.', _back_kb('lot:' + key, '◀️ К лоту'))
        _submit(job)
    else:
        _answer(q)
        finish(' Сам лот на FunPay остался.')


@route('ldes')
def r_lot_design(q: Any, key: str) -> None:
    if not CAN_CREATE_LOTS:
        r_lot_text(q, key)
        return
    cfg = load_config()
    lot = cfg['lot_mapping'].get(key)
    if not lot or not lot.get('funpay_lot_id'):
        _answer(q, 'Нужен привязанный FunPay ID.', True)
        return
    rows = _template_buttons(f'ldset:{key}', cfg)
    rows.append([B('◀️ К лоту', 'lot:' + key)])
    _edit(q, f'🎨 <b>Оформление лота</b>\n\nСейчас: {escape(_template_name(cfg, lot.get("template") or "auto"))}\n\n'
             'Выберите тему или свой шаблон — описание лота на FunPay будет переписано (цена и тег сохранятся).', _kb(*rows))


@route('ldset')
def r_lot_design_set(q: Any, key: str, token: str) -> None:
    if not CAN_CREATE_LOTS:
        _answer(q, 'На этой площадке тексты лотов меняются вручную — используйте «📋 Текст для лота».', True)
        return
    choice = _token_choice(token)
    _answer(q, '⏳ Переоформляю лот…')
    with config_tx() as tx:
        if key in tx['lot_mapping']:
            tx['lot_mapping'][key]['template'] = '' if choice == 'auto' else choice
    def job() -> None:
        done, errors = _restyle_lots([key], choice)
        _show(q.message.chat.id, '✅ Лот переоформлен.' if done else '❌ ' + escape('; '.join(errors) or 'Не удалось.'),
              _back_kb('lot:' + key, '◀️ К лоту'))
    _submit(job)


# ---------------------------------------------------------------------------
# Готовый текст лота (для ручного создания или правки на площадке)
# ---------------------------------------------------------------------------

def _text_message(service: Dict[str, Any], quantity: int, price: float, choice: str, header: str) -> List[str]:
    cfg = load_config()
    limits = {'summary_ru': 100, 'summary_en': 100, 'desc_ru': 5000, 'desc_en': 5000}
    texts = render_lot_texts(service, quantity, price, choice, limits, cfg, '')
    ru = (f'{header}\n🎨 {escape(_template_name(cfg, texts["template"]))}\n\n<b>Название (RU):</b>\n<pre>{escape(texts["summary_ru"])}</pre>\n'
          f'<b>Описание (RU):</b>\n<pre>{escape(texts["desc_ru"][:3200])}</pre>')
    en = (f'<b>Title (EN):</b>\n<pre>{escape(texts["summary_en"])}</pre>\n'
          f'<b>Description (EN):</b>\n<pre>{escape(texts["desc_en"][:3400])}</pre>')
    return [ru, en]


@route('ltx', 'ltxt')
def r_lot_text(q: Any, key: str, token: str = '') -> None:
    cfg = load_config()
    lot = cfg['lot_mapping'].get(key)
    if not lot:
        _answer(q, 'Лот не найден.', True)
        return
    _answer(q, '⏳ Готовлю текст…')
    choice = _token_choice(token) if token else (lot.get('template') or 'auto')

    def job() -> None:
        try:
            service = PanelAPI(cfg, lot['panel_id']).service(lot['service_id'])
        except PanelError as exc:
            _show(q.message.chat.id, f'❌ {escape(str(exc))}', _back_kb('lot:' + key, '◀️ К лоту'))
            return
        price = _float(lot.get('last_price'))
        parts = _text_message(service, lot['quantity'], price, choice,
                              f'📋 <b>Текст для лота</b> — нажмите на блок, чтобы скопировать, и вставьте в редактор лота на {MARKET}.')
        _show(q.message.chat.id, parts[0])
        rows = _template_buttons(f'ltxt:{key}', cfg)
        rows.append([B('◀️ К лоту', 'lot:' + key)])
        _show(q.message.chat.id, parts[1], _kb(*rows))
    _submit(job)


# ---------------------------------------------------------------------------
# Рефилл конкретного лота
# ---------------------------------------------------------------------------

@route('lref')
def r_lot_refill(q: Any, key: str) -> None:
    lot = _lot_by_key(key)
    if not lot:
        return
    r = lot['refill']
    text = (f'♻️ <b>Рефилл лота</b>\n{escape(_short(lot["name"], 60))}\n\n'
            f'Рефилл: {"ВКЛ" if r["enabled"] else "ВЫКЛ"}\nЛимит запусков: {"ВКЛ" if r["limit_enabled"] else "ВЫКЛ"} ({r["limit"]})\n'
            f'Срок действия: {"ВКЛ" if r["period_enabled"] else "ВЫКЛ"} ({_fmt_hours(r["hours"])})\n\n'
            'Условия фиксируются в заказе в момент покупки.')
    kb = _kb([B(f'♻️ Рефилл: {"ВКЛ" if r["enabled"] else "ВЫКЛ"}', f'lrt:en:{key}')],
             [B(f'🔁 Лимит: {"ВКЛ" if r["limit_enabled"] else "ВЫКЛ"}', f'lrt:lim:{key}'), B(f'✏️ Запусков ({r["limit"]})', f'lre:limit:{key}')],
             [B(f'⏳ Срок: {"ВКЛ" if r["period_enabled"] else "ВЫКЛ"}', f'lrt:per:{key}'), B(f'✏️ Срок ({r["hours"]} ч)', f'lre:hours:{key}')],
             [B('◀️ К настройкам лота', 'lot:' + key)])
    _edit(q, text, kb)


@route('lrt')
def r_lot_refill_toggle(q: Any, what: str, key: str) -> None:
    with config_tx() as tx:
        lot = tx['lot_mapping'].get(key)
        if lot:
            field = {'en': 'enabled', 'lim': 'limit_enabled', 'per': 'period_enabled'}[what]
            lot['refill'][field] = not lot['refill'][field]
    r_lot_refill(q, key)


@route('lre')
def r_lot_refill_edit(q: Any, what: str, key: str) -> None:
    prompt = '✏️ Сколько раз покупатель может запустить рефилл по одному заказу? (целое, минимум 1)' if what == 'limit' else \
        '✏️ Срок действия рефилла в часах (например 168 = 7 дней):'
    _ask(q, 'lot_refill', prompt, {'what': what, 'key': key}, back='lref:' + key)


@inp('lot_refill')
def i_lot_refill(message: Any, text: str, data: Dict[str, Any]) -> None:
    value = _int(text)
    if value < 1:
        raise ValueError('Нужно целое число не меньше 1.')
    with config_tx() as tx:
        lot = tx['lot_mapping'].get(data['key'])
        if not lot:
            raise ValueError('Лот уже удалён.')
        lot['refill']['limit' if data['what'] == 'limit' else 'hours'] = value
    _clear(message)
    _show(message.chat.id, '✅ Сохранено.', _back_kb('lref:' + data['key'], '◀️ К рефиллу лота'))
