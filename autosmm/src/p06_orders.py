

# ---------------------------------------------------------------------------
# Уведомления продавцу
# ---------------------------------------------------------------------------

def _admin_ids() -> List[int]:
    raw = getattr(getattr(cardinal_instance, 'telegram', None), 'authorized_users', None) or {}
    values = raw.keys() if isinstance(raw, dict) else raw if isinstance(raw, (list, tuple, set)) else []
    return [_int(value) for value in values if _int(value)]


def _is_admin(event: Any) -> bool:
    return _int(getattr(getattr(event, 'from_user', None), 'id', None)) in _admin_ids()


def _admin_later(text: str) -> None:
    if bot and _READY:
        _notify_admins(text)
    else:
        _pending_admin.append(text)


def _notify_admins(text: str, kb: Optional[InlineKeyboardMarkup] = None, html: bool = False) -> None:
    if not bot:
        _pending_admin.append(text)
        return
    try:
        cfg = load_config()
    except Exception:
        cfg = _default_config()
    targets = [cfg['notification_chat_id']] if cfg.get('notification_chat_id') else _admin_ids()
    body = text if html else _safe_detail(text, cfg, 3500)
    for target in targets:
        try:
            bot.send_message(target, body, reply_markup=kb, parse_mode='HTML' if html else None,
                             disable_web_page_preview=True)
        except Exception as exc:
            logger.warning('[AutoSMM] Admin notification failed: %s', type(exc).__name__)


def _send(c: Any, chat_id: Any, text: str) -> bool:
    """Cardinal.send_message возвращает None, если сообщение не доставлено."""
    if not chat_id or not text:
        return False
    try:
        result = c.send_message(chat_id, _plain(text))
        return result is not None and result is not False
    except Exception as exc:
        logger.warning('[AutoSMM] FunPay message to %s failed: %s', chat_id, type(exc).__name__)
        return False


def _order_url(funpay_id: Any) -> str:
    return ORDER_URL.format(funpay_id)


# ---------------------------------------------------------------------------
# Сопоставление собеседника и заказа
# ---------------------------------------------------------------------------

def _message_values(message: Any, field: str) -> List[str]:
    values: List[str] = []
    value = message.get(field) if isinstance(message, dict) else getattr(message, field, None)
    if value not in (None, '') and not isinstance(value, (dict, list, tuple, set)):
        values.append(str(value))
    chat = message.get('chat') if isinstance(message, dict) else getattr(message, 'chat', None)
    if field == 'chat_id' and chat is not None:
        value = chat.get('id') if isinstance(chat, dict) else getattr(chat, 'id', None)
        if value not in (None, ''):
            values.append(str(value))
    return list(dict.fromkeys(values))


def _chat_key(value: Any) -> Optional[Tuple[str, str]]:
    if value in (None, ''):
        return None
    value = str(value).strip()
    if value.isdigit():
        return ('cid', str(int(value)))
    alias = re.fullmatch(r'users-(\d+)-(\d+)', value)
    if alias:
        return ('users', '-'.join(str(x) for x in sorted(map(int, alias.groups()))))
    return ('opaque', value)


def _sender_matches_buyer(message: Any, data: Dict[str, Any]) -> bool:
    author_ids = _message_values(message, 'author_id') + _message_values(message, 'user_id')
    if data.get('buyer_id') not in (None, '') and str(data['buyer_id']) in author_ids:
        return True
    expected = str(data.get('buyer_username') or '').strip().lstrip('@').lower()
    names = _message_values(message, 'author') + _message_values(message, 'author_username') + _message_values(message, 'username')
    return bool(expected and any(name.strip().lstrip('@').lower() == expected for name in names))


def _message_matches_waiting(message: Any, data: Dict[str, Any]) -> bool:
    author_ids = _message_values(message, 'author_id') + _message_values(message, 'user_id')
    if '0' in author_ids:
        return False
    actual = [key for key in (_chat_key(x) for x in _message_values(message, 'chat_id')) if key]
    saved = [_chat_key(data.get('chat_id'))]
    aliases = data.get('chat_aliases', [])
    if isinstance(aliases, list):
        saved.extend(_chat_key(x) for x in aliases)
    saved = [key for key in saved if key]
    if actual and saved:
        if set(actual) & set(saved):
            return True
        if {key[0] for key in actual} & {key[0] for key in saved}:
            return False
        if any(key[0] == 'opaque' for key in actual + saved):
            return False
    return _sender_matches_buyer(message, data)


def _remember_message_chat(message: Any, data: Dict[str, Any], *, saved_order: bool = False) -> Any:
    values = _message_values(message, 'chat_id')
    if not values:
        return data.get('chat_id')
    current, previous = values[0], str(data.get('chat_id') or '')
    if current == previous:
        return data.get('chat_id')
    aliases = list(data.get('chat_aliases') or []) if isinstance(data.get('chat_aliases'), list) else []
    if previous and previous not in aliases:
        aliases.append(previous)
    data.update(chat_id=int(current) if current.isdigit() else current, chat_aliases=aliases)
    if saved_order:
        _update_order(data['funpay_order_id'], chat_id=data['chat_id'], chat_aliases=aliases)
    else:
        with _wlock:
            if str(data.get('funpay_order_id')) in waiting:
                waiting[str(data['funpay_order_id'])].update(chat_id=data['chat_id'], chat_aliases=aliases)
        _save_waiting()
    return data['chat_id']


# ---------------------------------------------------------------------------
# Определение лота по заказу: тег → точное название → название + параметры
# ---------------------------------------------------------------------------

def _norm_title(value: Any) -> str:
    text = unescape(str(value or '')).replace('\xa0', ' ').lower()
    text = re.sub(r'\s+', ' ', text).strip()
    return re.sub(r',\s*\d+\s*(шт|pcs|ед)\.?$', '', text).strip(' ,')


def _order_texts(order: Any) -> List[str]:
    values = []
    for field in ('description', 'short_description', 'title', 'summary', 'offer_name', 'lot_name'):
        try:
            value = order.get(field) if isinstance(order, dict) else getattr(order, field, None)
        except Exception:
            value = None
        if value:
            values.append(_norm_title(value))
    return list(dict.fromkeys(x for x in values if x))


def _match_by_name(order: Any, lots: Dict[str, Dict[str, Any]]) -> Tuple[Optional[str], int]:
    """(ключ лота, количество кандидатов). Только полное название или название + «, параметр»."""
    texts = _order_texts(order)
    exact, prefix = [], []
    for key, item in lots.items():
        name = _norm_title(item.get('name'))
        if not name:
            continue
        if any(text == name for text in texts):
            exact.append(key)
        elif any(text.startswith(name + ',') for text in texts):
            prefix.append(key)
    candidates = exact or prefix
    return (candidates[0] if len(candidates) == 1 else None), len(candidates)


def _match_by_tag(full_description: str, lots: Dict[str, Dict[str, Any]]) -> Tuple[Optional[str], List[str]]:
    tags = [x.lower() for x in TAG_RE.findall(str(full_description or ''))]
    for tag in tags:
        key = next((k for k, v in lots.items() if v.get('tag') == tag), None)
        if key:
            return key, tags
    return None, tags


def _find_lot(order: Any, c: Any) -> Tuple[Optional[str], str]:
    """Возвращает (ключ лота, причина при неудаче)."""
    lots = load_config()['lot_mapping']
    if not lots:
        return None, ''
    lot_id = _int(getattr(order, 'lot_id', 0))
    if lot_id:
        # Площадка сообщила ID лота (Starvell) — это точное совпадение.
        exact = next((k for k, v in lots.items() if v.get('funpay_lot_id') and _int(v['funpay_lot_id']) == lot_id), None)
        if exact:
            return exact, ''
    key, count = _match_by_name(order, lots)
    if key and count == 1:
        return key, ''
    full_description, error = None, ''
    fetch = getattr(c.account, 'get_order', None)
    if callable(fetch):
        for attempt in range(3):
            try:
                full = _fp_call(fetch, str(order.id), attempts=2)
                full_description = getattr(full, 'full_description', None) or ''
                break
            except Exception as exc:
                error = type(exc).__name__
                time.sleep(2 + attempt * 3)
    if full_description is not None:
        tagged, tags = _match_by_tag(full_description, lots)
        if tagged:
            return tagged, ''
        if tags:
            return None, f'в заказе есть тег #SMM:{tags[0]}, но такого лота нет в плагине'
    if count > 1:
        return None, f'название подходит к {count} лотам — запишите теги в карточках этих лотов'
    if error and not full_description:
        # Без кандидатов по названию это, скорее всего, чужой заказ: не шумим.
        logger.warning('[AutoSMM] Order %s: FunPay order details unavailable (%s), no name match.', getattr(order, 'id', '?'), error)
    return None, ''


# ---------------------------------------------------------------------------
# Возвраты и ручная проверка
# ---------------------------------------------------------------------------

def _refund(c: Any, funpay_id: Any, chat_id: Any, reason: str, *, notify_reason: str = '') -> None:
    funpay_id = str(funpay_id)
    with _orders_lock:
        item = _get_order(funpay_id)
        if item and (item.get('is_refunded') or item.get('refund_attempted')):
            return
        if not item:
            data = waiting.get(funpay_id, {})
            _add_order({**data, 'funpay_order_id': funpay_id, 'status': 'awaiting_refund', 'panel_order_id': '',
                        'created_at': time.time(), 'is_refunded': False, 'cost': 0, 'chat_id': chat_id})
        _update_order(funpay_id, status='awaiting_refund', refund_reason=reason)
    cfg = load_config()
    if not cfg['auto_refunds']:
        _remove_waiting(funpay_id)
        _notify_admins(f'⚠️ Требуется ручной возврат по заказу #{funpay_id}\nПричина: {notify_reason or reason}\n{_order_url(funpay_id)}')
        _advance_queue(c, funpay_id)
        return
    with _orders_lock:
        current = _get_order(funpay_id) or {}
        if current.get('refund_attempted') or current.get('is_refunded'):
            return
        _update_order(funpay_id, refund_attempted=True, status='refunding')
    try:
        _fp_call(c.account.refund, funpay_id, attempts=2)
    except Exception as exc:
        _update_order(funpay_id, status='refund_unknown', refund_error=type(exc).__name__)
        with _wlock:
            if funpay_id in waiting:
                waiting[funpay_id]['step'] = 'review'
        _save_waiting()
        _notify_admins(f'🛑 Не удалось выполнить автовозврат #{funpay_id} ({type(exc).__name__}).\n'
                       f'Причина возврата: {notify_reason or reason}\nВерните деньги вручную: {_order_url(funpay_id)}')
        return
    _update_order(funpay_id, is_refunded=True, status='refunded', cost=0)
    _remove_waiting(funpay_id)
    _notify_admins(f'⚠️ Авто-возврат по заказу #{funpay_id}\nПричина: {notify_reason or reason}')
    _send(c, chat_id, f'❌ Ваши средства возвращены по причине: {reason}')
    _advance_queue(c, funpay_id)


def _manual_review(c: Any, data: Dict[str, Any], reason: str) -> None:
    funpay_id = str(data['funpay_order_id'])
    _update_order(funpay_id, status='submission_unknown', error=reason)
    with _wlock:
        if funpay_id in waiting:
            waiting[funpay_id]['step'] = 'review'
    _save_waiting()
    _notify_admins(f'⚠️ Заказ FunPay #{funpay_id}: запуск в панели #{data["panel_id"]} не подтверждён. {reason}\n'
                   f'Проверьте историю панели по услуге {data["service_id"]}, ссылке {data.get("link", "")} и количеству '
                   f'{data["quantity"]}. Откройте «⚠️ Проверить заказы» в AutoSMM.')
    _send(c, data['chat_id'], '⏳ Ответ SMM-сервиса задержался. Продавец проверит запуск заказа. Повторно отправлять ссылку не нужно.')
    _advance_queue(c, funpay_id, data)


def _recover_submissions() -> int:
    """Перезапуск не может безопасно повторить прерванный платный запрос."""
    count = 0
    with _wlock:
        for data in list(waiting.values()):
            if data.get('step') == 'starting':
                _add_order({**data, 'status': 'submission_unknown', 'panel_order_id': '', 'created_at': time.time(),
                            'is_refunded': False, 'cost': 0})
                data['step'] = 'review'
                count += 1
    with _orders_lock:
        for order in _orders_raw():
            if order.get('status') in {'submitting', 'retry_ready'}:
                order.update(status='submission_unknown', error='Перезапуск во время передачи заказа. Проверьте историю панели.')
                count += 1
            if order.get('status') == 'refunding':
                order.update(status='refund_unknown', refund_error='Перезапуск во время возврата.')
                count += 1
            if order.get('panel_order_id') and str(order.get('funpay_order_id')) in waiting:
                with _wlock:
                    waiting.pop(str(order['funpay_order_id']), None)
        _persist_orders()
    _save_waiting()
    return count


# ---------------------------------------------------------------------------
# Очередь заказов покупателя
# ---------------------------------------------------------------------------

def _buyer_key(data: Dict[str, Any]) -> str:
    if data.get('buyer_id') not in (None, ''):
        return 'id:' + str(data['buyer_id'])
    return 'name:' + str(data.get('buyer_username') or '').lower()


def _buyer_entries(key: str) -> List[Dict[str, Any]]:
    with _wlock:
        items = [v for v in waiting.values() if _buyer_key(v) == key]
    return sorted(items, key=lambda x: (_float(x.get('created_at')), str(x.get('funpay_order_id'))))


def _remove_waiting(funpay_id: Any) -> Optional[Dict[str, Any]]:
    with _wlock:
        data = waiting.pop(str(funpay_id), None)
    _save_waiting()
    return data


def _advance_queue(c: Any, finished_id: Any, finished: Optional[Dict[str, Any]] = None) -> None:
    """Когда активный заказ ушёл в работу или отменён, просим ссылку для следующего."""
    finished = finished or _get_order(finished_id) or {}
    if not finished:
        return
    key = _buyer_key(finished)
    entries = [x for x in _buyer_entries(key) if x.get('step') in ('link', 'poll', 'confirm')]
    if not entries:
        return
    nxt = entries[0]
    if not nxt.get('queued'):
        return
    with _wlock:
        if str(nxt['funpay_order_id']) in waiting:
            waiting[str(nxt['funpay_order_id'])]['queued'] = False
            waiting[str(nxt['funpay_order_id'])]['prompted_at'] = time.time()
    _save_waiting()
    cfg = load_config()
    _send(c, nxt['chat_id'], _format(cfg['messages']['next_in_queue'], {
        'funpay_order_id': nxt['funpay_order_id'], 'remaining': len(entries) - 1,
        'example_link': _example(nxt.get('domains', []), nxt.get('platform', ''), nxt.get('kind', ''))}))


# ---------------------------------------------------------------------------
# Пауза покупателей (/stop, /start) и чёрный список
# ---------------------------------------------------------------------------

def _is_paused(cfg: Dict[str, Any], buyer_id: Any, username: Any) -> bool:
    username = str(username or '').lower()
    for item in cfg.get('paused_buyers', []):
        if buyer_id not in (None, '') and str(item.get('id') or '') == str(buyer_id):
            return True
        if username and str(item.get('username') or '').lower() == username:
            return True
    return False


def _is_blacklisted(c: Any, username: Any) -> bool:
    check = getattr(c, 'is_blacklisted', None)
    if callable(check):
        try:
            return bool(check(username))
        except Exception:
            return False
    names = getattr(c, 'blacklist', None) or []
    return bool(username) and str(username).lower() in {str(x).lower() for x in names}


def _pause_buyer(c: Any, buyer_id: Any, username: str, chat_id: Any) -> None:
    with config_tx() as cfg:
        if not _is_paused(cfg, buyer_id, username):
            cfg['paused_buyers'].append({'id': str(buyer_id or ''), 'username': username, 'since': time.time()})
    key_id, key_name = 'id:' + str(buyer_id or ''), 'name:' + str(username or '').lower()
    dropped = []
    with _wlock:
        for funpay_id, data in list(waiting.items()):
            if _buyer_key(data) in (key_id, key_name) and data.get('step') in ('link', 'poll', 'confirm'):
                waiting.pop(funpay_id, None)
                dropped.append(funpay_id)
    _save_waiting()
    manual = []
    with _orders_lock:
        for order in _orders_raw():
            if _buyer_key(order) in (key_id, key_name) and order.get('panel_order_id') and not order.get('is_refunded') \
                    and str(order.get('status', '')).lower() not in GOOD_STATUSES | BAD_STATUSES | {'manual', 'refunded'}:
                order.update(status='manual', manual_since=time.time(), completion_notified=True)
                manual.append(str(order['funpay_order_id']))
        if manual:
            _persist_orders()
    _notify_admins(f'⏸ Автообработка приостановлена для покупателя {username or "?"} (ID {buyer_id or "?"}).\n'
                   f'Снято ожидание ссылки: {", ".join("#" + x for x in dropped) or "нет"}\n'
                   f'Переведено в ручной режим: {", ".join("#" + x for x in manual) or "нет"}\n'
                   'Чтобы вернуть — отправьте /start в этот чат или нажмите кнопку в «⚙️ Настройки → ⏸ На паузе».')


def _resume_buyer(buyer_id: Any, username: str) -> bool:
    with config_tx() as cfg:
        before = len(cfg['paused_buyers'])
        cfg['paused_buyers'] = [x for x in cfg['paused_buyers'] if not (
            (buyer_id not in (None, '') and str(x.get('id') or '') == str(buyer_id)) or
            (username and str(x.get('username') or '').lower() == str(username).lower()))]
        return len(cfg['paused_buyers']) != before


def _seller_command(c: Any, msg: Any, command: str) -> None:
    chat_id = getattr(msg, 'chat_id', None)
    buyer_id = getattr(msg, 'interlocutor_id', None)
    username = ''
    with _wlock:
        known = [x for x in waiting.values() if _message_matches_waiting({'chat_id': chat_id}, x)]
    known += [x for x in _orders() if _chat_key(x.get('chat_id')) == _chat_key(chat_id)]
    if known:
        buyer_id = buyer_id or known[-1].get('buyer_id')
        username = str(known[-1].get('buyer_username') or '')
    if not username:
        username = str(getattr(msg, 'chat_name', '') or '')
    if not username and not buyer_id:
        try:
            chat = _fp_call(c.account.get_chat, int(chat_id), False)
            username = str(getattr(chat, 'name', '') or '')
        except Exception:
            pass
    if not username and not buyer_id:
        _notify_admins(f'⚠️ Не удалось определить покупателя для команды {command} в чате {chat_id}. Повторите позже.')
        return
    if command == '/stop':
        _pause_buyer(c, buyer_id, username, chat_id)
    elif _resume_buyer(buyer_id, username):
        _notify_admins(f'▶️ Автообработка возобновлена для покупателя {username or "?"} (ID {buyer_id or "?"}).')
    else:
        _notify_admins(f'ℹ️ Покупатель {username or "?"} не был на паузе.')


# ---------------------------------------------------------------------------
# Новый заказ
# ---------------------------------------------------------------------------

def _resolve_chat(c: Any, order: Any) -> Any:
    buyer = getattr(order, 'buyer_id', None)
    username = str(getattr(order, 'buyer_username', '') or '')
    chat = getattr(order, 'chat_id', None)
    lookup = getattr(c.account, 'get_chat_by_name', None)
    if username and callable(lookup):
        try:
            cached = lookup(username, False)
            if getattr(cached, 'id', None) and str(getattr(cached, 'name', '') or '').lower() == username.lower():
                chat = cached.id
        except Exception:
            pass
    if not chat and buyer and getattr(c.account, 'id', None):
        chat = f'users-{buyer}-{c.account.id}'
    return chat


def _handle_new_order(c: Any, order: Any) -> None:
    funpay_id = str(order.id)
    with _order_lock(funpay_id):
        with _wlock:
            if funpay_id in waiting:
                return
        if _get_order(funpay_id):
            return
        cfg = load_config()
        if not cfg['lot_mapping']:
            return
        key, reason = _find_lot(order, c)
        if not key:
            if reason:
                _notify_admins(f'⚠️ AutoSMM не определил лот заказа #{funpay_id}: {reason}.\n'
                               f'Обработайте заказ вручную: {_order_url(funpay_id)}')
            else:
                logger.info('[AutoSMM] Order %s is not an AutoSMM lot.', funpay_id)
            return
        cfg = load_config()
        lot = cfg['lot_mapping'][key]
        buyer = getattr(order, 'buyer_id', None)
        username = str(getattr(order, 'buyer_username', '') or '')
        if cfg.get('respect_blacklist') and _is_blacklisted(c, username):
            _notify_admins(f'🚫 Заказ #{funpay_id} от {username} из чёрного списка — автонакрутка не запущена.\n{_order_url(funpay_id)}')
            return
        if _is_paused(cfg, buyer, username):
            _notify_admins(f'⏸ Новый заказ #{funpay_id} от покупателя {username}, который сейчас на паузе. '
                           f'Накрутка не запущена — обработайте вручную.\n{_order_url(funpay_id)}')
            return
        chat = _resolve_chat(c, order)
        if not chat or not buyer:
            _notify_admins(f'⚠️ Заказ #{funpay_id}: не удалось определить чат покупателя. Обработайте вручную.\n{_order_url(funpay_id)}')
            return
        amount = max(1, _int(getattr(order, 'amount', 1), 1))
        price = _float(getattr(order, 'price', 0))
        refill = lot['refill']
        now = time.time()
        try:
            paid_at = getattr(order, 'date', None).timestamp()
            paid_at = paid_at if abs(paid_at - now) < 86400 else now
        except Exception:
            paid_at = now
        entry = {'buyer_id': buyer, 'buyer_username': username, 'chat_id': chat, 'funpay_order_id': funpay_id,
                 'lot_key': key, 'lot_name': lot['name'], 'panel_id': lot['panel_id'], 'service_id': lot['service_id'],
                 'quantity': amount * lot['quantity'], 'amount': amount, 'price': price, 'domains': lot['domains'],
                 'platform': lot.get('platform', ''), 'kind': lot.get('kind', ''), 'poll': bool(lot.get('poll')),
                 'step': 'link', 'created_at': paid_at, 'queued': False, 'prompted_at': now,
                 'refill_terms': {'enabled': refill['enabled'], 'left': refill['limit'] if refill['limit_enabled'] else None,
                                  'hours': refill['hours'] if refill['period_enabled'] else None}}
        with _wlock:
            # Проверка очереди и вставка — атомарно: два одновременных заказа
            # одного покупателя не станут активными оба.
            if funpay_id in waiting:
                return
            active = [x for x in _buyer_entries(_buyer_key(entry)) if x.get('step') in ('link', 'poll', 'confirm', 'starting')]
            entry['queued'] = bool(active)
            waiting[funpay_id] = entry
        _save_waiting()
    if entry['queued']:
        _send(c, chat, _format(cfg['messages']['queued'], {'funpay_id': funpay_id, 'funpay_order_id': funpay_id,
                                                         'position': len(active) + 1}))
    else:
        _send(c, chat, _format(cfg['messages']['after_payment'], {
            'example_link': _example(lot['domains'], lot.get('platform', ''), lot.get('kind', '')), 'buyer_username': username,
            'funpay_order_id': funpay_id, 'service_title': lot['name']}))
    if refill['enabled'] and cfg['refill'].get('send_moment') == 'after_payment':
        _send_refill_info(c, entry, None)
    if cfg.get('notify_new_order'):
        _notify_admins(f'🛍 Новый SMM заказ #{funpay_id}\nЛот: {lot["name"]}\nПокупатель: {username}\n'
                       f'Количество: {amount} шт. → {entry["quantity"]} ед.\nСумма: {price:.2f} ₽'
                       + ('\n📥 Поставлен в очередь' if entry['queued'] else ''))
    logger.info('[AutoSMM] Order %s accepted (lot %s), waiting for a link.', funpay_id, key)


# ---------------------------------------------------------------------------
# Сообщения покупателя
# ---------------------------------------------------------------------------

_LINK_RE = re.compile(r'(?:https?://[^\s<>"\']+|(?:www\.)?(?:[a-z0-9-]+\.)+[a-z]{2,}(?:/[^\s<>"\']*)?|@[a-z0-9_]{4,32})', re.I)


def _extract_link(text: str, data: Dict[str, Any]) -> str:
    for match in _LINK_RE.finditer(text):
        value = match.group(0).rstrip('.,;!?)]»"\'')
        if value.startswith('@'):
            if data.get('platform') == 'telegram' or any(d in ('t.me', 'telegram.me') for d in data.get('domains', [])):
                return 'https://t.me/' + value[1:]
            continue
        if not re.match(r'https?://', value, re.I):
            host = value.split('/')[0].lower().removeprefix('www.')
            known = set(data.get('domains') or []) | set(DEFAULT_DOMAINS)
            if '*' in (data.get('domains') or []) or host in known or any(host.endswith('.' + x) for x in known):
                value = 'https://' + value
            else:
                continue
        return value
    return ''


def _valid_link(link: str, domains: List[str], check: bool = True) -> Tuple[bool, str]:
    parsed = urlparse(link.strip())
    if parsed.scheme not in ('https', 'http') or not parsed.netloc:
        return False, '❌ Нужна полная ссылка, например https://...'
    if not check:
        return True, ''
    host = (parsed.hostname or '').lower().removeprefix('www.')
    allowed = _domains(domains) or _global_domains()
    if '*' in allowed or host in allowed or any(host.endswith('.' + x) for x in allowed):
        return True, ''
    return False, f'❌ Домен {host} не подходит для этого заказа. Разрешены: {", ".join(allowed[:8])}.'


def _confirm_word(text: str) -> Optional[bool]:
    value = text.strip().lower().rstrip('!.')
    if value in YES_WORDS or value.startswith('+'):
        return True
    if value in NO_WORDS or value.startswith('-'):
        return False
    return None


def _set_waiting(funpay_id: str, **updates: Any) -> Dict[str, Any]:
    with _wlock:
        if funpay_id in waiting:
            waiting[funpay_id].update(updates)
            data = deepcopy(waiting[funpay_id])
        else:
            data = {}
    _save_waiting()
    return data


def _ask_confirmation(c: Any, chat: Any, data: Dict[str, Any]) -> None:
    lines = [f'✅ Ссылка для заказа #{data["funpay_order_id"]} принята: {data["link"]}']
    if data.get('answer_number'):
        lines.append(f'🔢 Вариант ответа: {data["answer_number"]}')
    lines.append(f'📦 Количество: {data["quantity"]}')
    lines.append('\nОтправьте «+», чтобы запустить, или «-», чтобы ввести ссылку заново.')
    _send(c, chat, '\n'.join(lines))


def _process_buyer_input(c: Any, msg: Any, text: str) -> bool:
    """Возвращает True, если сообщение относилось к ожидающему заказу."""
    with _wlock:
        matches = [deepcopy(d) for d in waiting.values() if _message_matches_waiting(msg, d)]
    if not matches:
        return False
    addressed = re.fullmatch(r'!smm\s+#?([A-Za-z0-9]+)\s+(.+)', text, re.I | re.S)
    if addressed:
        requested, text = addressed.group(1).upper(), addressed.group(2).strip()
        matches = [d for d in matches if str(d['funpay_order_id']).upper() == requested]
        if not matches:
            _send(c, getattr(msg, 'chat_id', None), '❌ Не найден ожидающий заказ с таким номером.')
            return True
    matches.sort(key=lambda x: (_float(x.get('created_at')), str(x.get('funpay_order_id'))))
    actionable = [d for d in matches if d.get('step') in ('link', 'poll', 'confirm') and (addressed or not d.get('queued'))]
    starting = [d for d in matches if d.get('step') == 'starting']
    if not actionable and not starting:
        queued = [d for d in matches if d.get('step') in ('link', 'poll', 'confirm')]
        if queued:
            # Самовосстановление очереди: активного заказа нет — берём первый из очереди.
            queued[0] = _set_waiting(str(queued[0]['funpay_order_id']), queued=False) or queued[0]
            actionable = [queued[0]]
    if not actionable:
        chat = _remember_message_chat(msg, matches[0])
        if any(d.get('step') == 'starting' for d in matches):
            if _extract_link(text, matches[0]) or _confirm_word(text) is not None:
                _send(c, chat, '⏳ Заказ уже передаётся в SMM-сервис. Пожалуйста, подождите.')
        elif any(d.get('step') == 'review' for d in matches):
            if _extract_link(text, matches[0]) or _confirm_word(text) is not None:
                _send(c, chat, '⏳ Продавец проверяет запуск заказа. Повторный запуск не нужен.')
        return True
    data = actionable[0]
    funpay_id = str(data['funpay_order_id'])
    chat = _remember_message_chat(msg, data)
    cfg = load_config()
    if data['step'] == 'link' or (data['step'] == 'confirm' and _extract_link(text, data)):
        link = _extract_link(text, data)
        if not link:
            if time.time() - _float(data.get('hint_at')) >= max(1, cfg.get('link_hint_minutes', 10)) * 60:
                _set_waiting(funpay_id, hint_at=time.time())
                example = _example(data.get('domains', []), data.get('platform', ''), data.get('kind', ''))
                _send(c, chat, f'🔗 Не вижу ссылки. Отправьте полную ссылку для заказа #{funpay_id}.\n📎 Пример: {example}')
            return True
        ok, reason = _valid_link(link, data.get('domains', []), cfg.get('domain_check', True))
        if not ok:
            _send(c, chat, reason)
            return True
        if data.get('poll') and not data.get('answer_number'):
            _set_waiting(funpay_id, link=link, step='poll')
            _send(c, chat, cfg['messages']['poll_request'])
            return True
        data = _set_waiting(funpay_id, link=link, step='confirm' if cfg['confirm_link'] else 'starting')
        if cfg['confirm_link']:
            _ask_confirmation(c, chat, data)
        else:
            _send(c, chat, '⏳ Ссылка принята. Запускаю заказ…')
            _submit(_start_order, c, funpay_id)
        return True
    if data['step'] == 'poll':
        number = re.fullmatch(r'#?\s*(\d{1,3})', text.strip())
        if not number or _int(number.group(1)) <= 0:
            _send(c, chat, '🔢 Нужно число — номер варианта ответа (например: 1).')
            return True
        data = _set_waiting(funpay_id, answer_number=_int(number.group(1)), step='confirm' if cfg['confirm_link'] else 'starting')
        if cfg['confirm_link']:
            _ask_confirmation(c, chat, data)
        else:
            _send(c, chat, '⏳ Принято. Запускаю заказ…')
            _submit(_start_order, c, funpay_id)
        return True
    if data['step'] == 'confirm':
        answer = _confirm_word(text)
        if answer is True:
            _set_waiting(funpay_id, step='starting')
            _send(c, chat, '⏳ Подтверждено. Запускаю заказ в SMM-сервисе…')
            _submit(_start_order, c, funpay_id)
        elif answer is False:
            with _wlock:
                if funpay_id in waiting:
                    waiting[funpay_id].pop('link', None)
                    waiting[funpay_id].pop('answer_number', None)
                    waiting[funpay_id]['step'] = 'link'
            _save_waiting()
            _send(c, chat, '✏️ Хорошо, отправьте другую ссылку.')
        elif time.time() - _float(data.get('hint_at')) >= 60:
            _set_waiting(funpay_id, hint_at=time.time())
            _send(c, chat, 'Отправьте «+», чтобы запустить заказ, или «-», чтобы изменить ссылку.')
        return True
    return True


def _status_message(result: Dict[str, Any]) -> str:
    lines = ['📊 Статус заказа', f"• Состояние: {_plain_status(result.get('status'))}"]
    for key, label in (('start_count', 'Стартовое значение'), ('remains', 'Осталось')):
        value = result.get(key)
        if value not in (None, ''):
            lines.append(f'• {label}: {_plain(value)}')
    return '\n'.join(lines)


def _plain_status(value: Any) -> str:
    text = _plain(value)
    labels = {'in progress': 'В процессе', 'processing': 'В процессе', 'pending': 'В очереди', 'completed': 'Выполнен',
              'complete': 'Выполнен', 'partial': 'Частично выполнен', 'canceled': 'Отменён', 'cancelled': 'Отменён',
              'failed': 'Ошибка выполнения', 'manual': 'Ведёт продавец'}
    return labels.get(text.lower(), text or 'Неизвестно')


def _check_command(c: Any, msg: Any, panel_order_id: str) -> None:
    chat = getattr(msg, 'chat_id', None)
    owned = [x for x in _orders() if str(x.get('panel_order_id')) == panel_order_id and _message_matches_waiting(msg, x)]
    if len(owned) != 1:
        _send(c, chat, '❌ Заказ не найден в базе бота. Проверьте номер из сообщения о запуске.')
        return
    order = owned[0]
    chat = _remember_message_chat(msg, order, saved_order=True)
    if str(order.get('status', '')).lower() == 'manual':
        _send(c, chat, 'ℹ️ Этот заказ ведёт продавец вручную.')
        return
    try:
        result = PanelAPI(load_config(), order['panel_id']).status(order['panel_order_id'])
        _send(c, chat, _status_message(result))
    except PanelError:
        _send(c, chat, '❌ SMM-сервис сейчас не ответил. Попробуйте позже.')


def auto_smm_handler(c: Any, event: Any, *args: Any) -> None:
    try:
        if isinstance(event, NewMessageEvent):
            _on_message(c, event.message)
        elif isinstance(event, NewOrderEvent):
            order = event.order
            status = getattr(order, 'status', None)
            paid = getattr(getattr(enums, 'OrderStatuses', None), 'PAID', None)
            if status is not None and paid is not None and status != paid:
                return
            _submit(_handle_new_order, c, order)
    except Exception:
        logger.exception('[AutoSMM] Event handler failed')


def _on_message(c: Any, msg: Any) -> None:
    author = str(getattr(msg, 'author_id', None))
    text = str(getattr(msg, 'text', '') or '').strip()
    if not text or author == '0':
        return
    if author == str(getattr(c.account, 'id', '')):
        if text.lower() in ('/stop', '/start'):
            _submit(_seller_command, c, msg, text.lower())
        return
    cfg = load_config()
    username = str(getattr(msg, 'author', '') or getattr(msg, 'chat_name', '') or '')
    if _is_paused(cfg, getattr(msg, 'author_id', None), username):
        return
    if cfg.get('respect_blacklist') and _is_blacklisted(c, username):
        return
    command = re.fullmatch(r'(?:чек|check|статус|status)\s+#?(\d+)', text.lower())
    if command:
        _submit(_check_command, c, msg, command.group(1))
        return
    if _handle_refill_input(c, msg, text):
        return
    _process_buyer_input(c, msg, text)


# ---------------------------------------------------------------------------
# Запуск заказа в панели
# ---------------------------------------------------------------------------

def _start_order(c: Any, funpay_id: str, retry: bool = False) -> None:
    funpay_id = str(funpay_id)
    with _order_lock(funpay_id):
        _start_order_locked(c, funpay_id, retry)


def _start_order_locked(c: Any, funpay_id: str, retry: bool) -> None:
    with _wlock:
        data = deepcopy(waiting.get(funpay_id))
    existing = _get_order(funpay_id)
    if data is None:
        if not (retry and existing):
            return
        data = deepcopy(existing)
    if existing and (existing.get('is_refunded') or existing.get('status') not in {'retry_ready', 'preflight_failed'}):
        return
    record = {**data, 'funpay_order_id': funpay_id, 'panel_order_id': '', 'status': 'submitting', 'cost': 0,
              'created_at': _float(data.get('created_at')) or time.time(), 'submitted_at': time.time(),
              'is_refunded': False, 'completion_notified': False}
    record.pop('step', None)
    if existing:
        _update_order(funpay_id, **record)
    else:
        _add_order(record)
    _set_waiting(funpay_id, step='starting')
    chat = data['chat_id']
    cfg = load_config()
    # Покупатель мог получить возврат вне плагина: проверяем до платного запроса.
    fetch = getattr(c.account, 'get_order', None)
    if callable(fetch):
        try:
            actual = _fp_call(fetch, funpay_id, attempts=3)
            status = getattr(actual, 'status', None)
            status_name = str(getattr(status, 'name', status or '')).lower().split('.')[-1]
            if status_name not in {'paid', 'closed', 'refunded', 'partially_refunded'}:
                raise RuntimeError('status ' + status_name)
        except Exception as exc:
            _update_order(funpay_id, status='preflight_failed', error='Не удалось подтвердить оплату на FunPay.')
            _set_waiting(funpay_id, step='confirm')
            _notify_admins(f'⚠️ Заказ #{funpay_id}: не удалось проверить оплату на FunPay ({type(exc).__name__}). '
                           'Запрос в панель не отправлялся.')
            _send(c, chat, '⏳ Не удалось проверить оплату на FunPay. Заказ пока не отправлен. Повторите «+» через минуту.')
            return
        if status_name in ('refunded', 'partially_refunded'):
            _update_order(funpay_id, status='refunded', is_refunded=True, cost=0)
            _remove_waiting(funpay_id)
            _send(c, chat, 'ℹ️ Этот заказ отменён/возвращён на FunPay — накрутка не запускается.')
            _advance_queue(c, funpay_id)
            return
    request_started = False
    try:
        api = PanelAPI(cfg, data['panel_id'])
        service = api.service(data['service_id'])
        quantity = _int(data['quantity'])
        if quantity < max(1, _int(service.get('min'), 1)) or (_int(service.get('max')) > 0 and quantity > _int(service.get('max'))):
            raise PanelError(f'Количество {quantity} вне лимитов услуги ({service.get("min")}–{service.get("max")}).')
        if str(service.get('type', 'Default')).strip().lower() not in ('default', 'poll'):
            raise PanelError('Автозапуск поддерживает только услуги типа Default и Poll.')
        cost_rub, cost_native, currency = _cost_rub(cfg, data['panel_id'], service, quantity, api)
        if not math.isfinite(cost_rub) or cost_rub <= 0:
            raise PanelError('Панель вернула некорректную стоимость услуги.')
        balance = api.balance()
        funds = _float(balance.get('balance'), float('nan'))
        if not math.isfinite(funds) or funds < cost_native:
            raise PanelError(f'На балансе панели недостаточно средств ({funds:g} {currency}, нужно {cost_native:.4f}).')
        price = _float(data.get('price'))
        if cfg['anti_dumping'] and price > 0 and price - cost_rub < _float(cfg['min_margin']):
            _update_order(funpay_id, cost=0, error='Антидемпинг')
            _refund(c, funpay_id, chat, 'стоимость услуги изменилась, заказ не может быть выполнен по этой цене.',
                    notify_reason=f'антидемпинг: цена {price:.2f} ₽, себестоимость сейчас {cost_rub:.2f} ₽, '
                                  f'мин. прибыль {cfg["min_margin"]} ₽. Обновите цену лота.')
            return
        if cfg['anti_dumping'] and price <= 0:
            _notify_admins(f'⚠️ Заказ #{funpay_id}: FunPay не передал цену заказа — антидемпинг пропущен.')
        _update_order(funpay_id, cost=round(cost_rub, 6), cost_native=cost_native, currency=currency)
        if (_get_order(funpay_id) or {}).get('is_refunded'):
            return
        extra = {'answer_number': data.get('answer_number')} if data.get('answer_number') else {}
        request_started = True
        panel_order = api.add(data['service_id'], data['link'], quantity, **extra)
    except Exception as exc:
        reason = _safe_detail(str(exc) if isinstance(exc, PanelError) else type(exc).__name__, cfg, 650)
        logger.error('[AutoSMM] Start order %s: %s', funpay_id, reason)
        if request_started and (not isinstance(exc, PanelError) or exc.uncertain):
            _manual_review(c, data, reason)
        else:
            _update_order(funpay_id, cost=0, error=reason)
            critical = 'не найдена' in reason and 'Панель' in reason
            _notify_admins(('❌ Критическая ошибка AutoSMM' if critical else '❌ Ошибка провайдера накрутки') +
                           f'\nЗаказ #{funpay_id} не запущен: {reason}')
            _send(c, chat, cfg['messages']['order_error'])
            _refund(c, funpay_id, chat, 'не удалось запустить услугу.', notify_reason=reason)
        return
    current = _get_order(funpay_id) or {}
    refunded_during_send = bool(current.get('is_refunded'))
    terms = data.get('refill_terms') or {}
    refill = {'enabled': bool(terms.get('enabled')), 'left': terms.get('left'), 'used': 0,
              'deadline': (time.time() + _int(terms['hours']) * 3600) if terms.get('hours') else None}
    _update_order(funpay_id, panel_order_id=panel_order, status='refunded' if refunded_during_send else 'pending',
                  started_at=time.time(), refill=refill)
    _remove_waiting(funpay_id)
    if refunded_during_send:
        _notify_admins(f'⚠️ Заказ #{funpay_id} возвращён на FunPay во время запуска, но панель создала #{panel_order}.')
        return
    values = {'panel_order_id': panel_order, 'order_id': panel_order, 'funpay_order_id': funpay_id, 'link': data['link']}
    if not _send(c, chat, _format(cfg['messages']['after_confirmation'], values)):
        _notify_admins(f'⚠️ Заказ #{funpay_id} запущен в панели (#{panel_order}), но сообщение покупателю не доставлено.')
    if refill['enabled'] and cfg['refill'].get('send_moment') == 'after_start':
        _send_refill_info(c, {**data, 'panel_order_id': panel_order}, refill)
    _advance_queue(c, funpay_id, {**data, 'funpay_order_id': funpay_id})
    _submit(_refresh_balance_quiet, data['panel_id'])


def _refresh_balance_quiet(panel_id: str) -> None:
    try:
        PanelAPI(load_config(), panel_id).balance()
    except PanelError:
        pass


def on_funpay_order_status_changed(c: Any, event: Any, *args: Any) -> None:
    order = getattr(event, 'order', None)
    status = getattr(order, 'status', None)
    name = str(getattr(status, 'name', status or '')).lower().split('.')[-1]
    if name not in ('refunded', 'partially_refunded'):
        return
    funpay_id = str(getattr(order, 'id', ''))
    data = _remove_waiting(funpay_id) if funpay_id in waiting else None
    current = _get_order(funpay_id)
    if data and not current:
        _add_order({**data, 'status': 'refunded', 'is_refunded': True, 'panel_order_id': '', 'cost': 0,
                    'created_at': time.time(), 'refund_reason': 'Возврат подтверждён событием FunPay.'})
    elif current and not current.get('is_refunded'):
        status_now = str(current.get('status') or '').lower()
        _update_order(funpay_id, status='refunded' if not current.get('panel_order_id') or status_now in BAD_STATUSES else status_now,
                      is_refunded=True)
    if data:
        _send(c, data.get('chat_id'), 'ℹ️ Заказ отменён/возвращён — накрутка по нему не запускается.')
        _advance_queue(c, funpay_id, data)
