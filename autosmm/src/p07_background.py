

# ---------------------------------------------------------------------------
# Рефилл
# ---------------------------------------------------------------------------

def _fmt_deadline(ts: Any) -> str:
    return datetime.fromtimestamp(_float(ts)).strftime('%d.%m.%Y %H:%M') if ts else 'бессрочно'


def _fmt_hours(hours: Any) -> str:
    hours = _int(hours)
    if not hours:
        return 'бессрочно'
    return f'{hours} ч' + (f' ({hours // 24} {_plural(hours // 24, ("день", "дня", "дней"))})' if hours % 24 == 0 and hours >= 24 else '')


def _send_refill_info(c: Any, data: Dict[str, Any], refill: Optional[Dict[str, Any]]) -> None:
    cfg = load_config()
    terms = data.get('refill_terms') or {}
    left = refill.get('left') if refill else terms.get('left')
    deadline = refill.get('deadline') if refill else ((time.time() + _int(terms['hours']) * 3600) if terms.get('hours') else None)
    _send(c, data.get('chat_id'), _format(cfg['messages']['refill_info'], {
        'command': '!рефилл', 'order_id': data.get('panel_order_id', ''), 'funpay_order_id': data.get('funpay_order_id', ''),
        'link': data.get('link', ''), 'refill_limit': 'без ограничений' if left is None else left,
        'refill_period': _fmt_hours(terms.get('hours')), 'refill_deadline': _fmt_deadline(deadline)}))


def _refill_eligible(order: Dict[str, Any]) -> bool:
    refill = order.get('refill') or {}
    if not refill.get('enabled') or not order.get('panel_order_id') or order.get('is_refunded'):
        return False
    if str(order.get('status', '')).lower() in BAD_STATUSES | {'manual', 'refunded', 'submission_unknown'}:
        return False
    if refill.get('left') is not None and _int(refill['left']) <= 0:
        return False
    if refill.get('deadline') and _float(refill['deadline']) < time.time():
        return False
    return True


def _refill_key(msg: Any) -> str:
    return '|'.join(_message_values(msg, 'chat_id')) + '|' + '|'.join(_message_values(msg, 'author_id'))


def _handle_refill_input(c: Any, msg: Any, text: str) -> bool:
    chat = getattr(msg, 'chat_id', None)
    key = _refill_key(msg)
    pending = _refill_choices.get(key)
    if pending and pending['expires'] < time.time():
        _refill_choices.pop(key, None)
        pending = None
    if pending:
        value = text.strip().lower()
        if value in NO_WORDS:
            _refill_choices.pop(key, None)
            _send(c, chat, 'Хорошо, рефилл не запускаем.')
            return True
        if value.isdigit() and 1 <= _int(value) <= len(pending['orders']):
            _refill_choices.pop(key, None)
            _submit(_run_refill, c, msg, pending['orders'][_int(value) - 1])
            return True
    match = REFILL_RE.fullmatch(text.strip())
    if not match:
        return False
    owned = [x for x in _orders() if _message_matches_waiting(msg, x) and _refill_eligible(x)]
    if match.group(1):
        owned = [x for x in owned if str(x.get('panel_order_id')) == match.group(1) or str(x.get('funpay_order_id')) == match.group(1)]
    if not owned:
        _send(c, chat, '♻️ Сейчас нет заказов, для которых доступен рефилл (не включён, истёк срок или исчерпан лимит).')
        return True
    owned.sort(key=lambda x: _float(x.get('started_at') or x.get('created_at')), reverse=True)
    owned = owned[:10]
    if len(owned) == 1:
        _submit(_run_refill, c, msg, str(owned[0]['funpay_order_id']))
        return True
    lines = ['♻️ Выберите заказ для рефилла — отправьте его номер в списке:']
    for index, order in enumerate(owned, 1):
        refill = order.get('refill') or {}
        left = 'без лимита' if refill.get('left') is None else f'осталось {refill["left"]}'
        lines.append(f'{index}. Заказ {order["panel_order_id"]} · {left} · до {_fmt_deadline(refill.get("deadline"))}\n   {order.get("link", "")}')
    lines.append('\nДля отмены отправьте «-».')
    _refill_choices[key] = {'orders': [str(x['funpay_order_id']) for x in owned], 'expires': time.time() + 900}
    _send(c, chat, '\n'.join(lines))
    return True


def _run_refill(c: Any, msg: Any, funpay_id: str) -> None:
    chat = getattr(msg, 'chat_id', None)
    with _order_lock('refill:' + funpay_id):
        order = _get_order(funpay_id)
        if not order or not _refill_eligible(order):
            _send(c, chat, '♻️ Рефилл для этого заказа сейчас недоступен.')
            return
        if time.time() - _float(order.get('last_refill_at')) < 120:
            _send(c, chat, '⏳ Рефилл по этому заказу уже запущен. Подождите.')
            return
        refill = dict(order.get('refill') or {})
        if refill.get('left') is not None:
            refill['left'] = _int(refill['left']) - 1
        refill['used'] = _int(refill.get('used')) + 1
        _update_order(funpay_id, refill=refill, last_refill_at=time.time())
        try:
            refill_id = PanelAPI(load_config(), order['panel_id']).refill(order['panel_order_id'])
        except PanelError as exc:
            restored = dict(refill)
            if restored.get('left') is not None:
                restored['left'] = _int(restored['left']) + 1
            restored['used'] = max(0, _int(restored.get('used')) - 1)
            _update_order(funpay_id, refill=restored, last_refill_at=0)
            _send(c, chat, '⚠️ Сервис сейчас не принял запрос на рефилл. Попытка не списана — продавец уже уведомлён.')
            _notify_admins(f'⚠️ Рефилл не создан\nЗаказ #{funpay_id} (панель {order["panel_order_id"]})\nОтвет: {exc}')
            return
    left = '' if refill.get('left') is None else f'\n🔁 Осталось запусков: {refill["left"]}'
    _send(c, chat, f'♻️ Рефилл запущен (№ {refill_id}). Восстановление занимает от нескольких часов до суток.{left}')
    if load_config()['refill'].get('notify', True):
        _notify_admins(f'♻️ Запущен рефилл по заказу #{funpay_id} (панель {order["panel_order_id"]}, рефилл {refill_id}).')


# ---------------------------------------------------------------------------
# Слежение за заказами
# ---------------------------------------------------------------------------

def _monitor_pass(c: Any) -> None:
    cfg = load_config()
    by_panel: Dict[str, List[Dict[str, Any]]] = {}
    for order in _orders():
        status = str(order.get('status', '')).strip().lower()
        if order.get('is_refunded') or not order.get('panel_order_id'):
            continue
        if status in {'partial', 'partial_resolved', 'awaiting_refund', 'refunding', 'refund_unknown', 'submission_unknown',
                      'manual', 'refunded'} | BAD_STATUSES:
            continue
        if status in GOOD_STATUSES and order.get('completion_notified') is not False:
            continue
        by_panel.setdefault(str(order['panel_id']), []).append(order)
    for panel_id, orders in by_panel.items():
        try:
            api = PanelAPI(cfg, panel_id)
            results = api.statuses([str(x['panel_order_id']) for x in orders])
        except PanelError:
            continue
        for order in orders:
            result = results.get(str(order['panel_order_id']))
            if result:
                try:
                    _apply_status(c, cfg, api, order, result)
                except Exception:
                    logger.exception('[AutoSMM] Status update %s failed', order.get('funpay_order_id'))


def _apply_status(c: Any, cfg: Dict[str, Any], api: PanelAPI, order: Dict[str, Any], result: Dict[str, Any]) -> None:
    funpay_id = order['funpay_order_id']
    previous = str(order.get('status', '')).strip().lower()
    status = str(result.get('status', 'pending')).strip().lower()
    charge = _float(result.get('charge'), -1)
    if status in BAD_STATUSES:
        _update_order(funpay_id, status=status, cost=0, panel_charge=charge)
        _refund(c, funpay_id, order['chat_id'], 'SMM-сервис не смог выполнить заказ.',
                notify_reason=f'панель вернула статус «{status}» по заказу {order["panel_order_id"]}')
        return
    if status == 'partial':
        updates: Dict[str, Any] = {'status': 'partial', 'remains': result.get('remains')}
        if charge >= 0:
            try:
                updates['cost'] = round(charge * _fx_to_rub(order.get('currency') or api.currency(), cfg), 6)
            except PanelError:
                pass
        _update_order(funpay_id, **updates)
        _send(c, order['chat_id'], f'ℹ️ Заказ {order["panel_order_id"]} выполнен частично (осталось {result.get("remains")}). '
                                   'Продавец свяжется с вами по оставшейся части.')
        _notify_admins(f'⚠️ Заказ #{funpay_id} выполнен частично. Остаток: {result.get("remains")}. '
                       f'Решите вопрос с покупателем вручную.\n{_order_url(funpay_id)}')
        return
    if status in GOOD_STATUSES:
        started = _float(order.get('started_at') or order.get('submitted_at') or order.get('created_at'))
        guard = max(_int(cfg.get('early_complete_minutes'), 5), api.quirks['early_minutes']) * 60
        if previous not in GOOD_STATUSES and time.time() - started < guard:
            return
        seen = _int(order.get('complete_seen')) + 1
        if previous not in GOOD_STATUSES and seen < api.quirks['confirmations']:
            _update_order(funpay_id, complete_seen=seen)
            return
        if previous not in GOOD_STATUSES:
            updates = {'status': 'completed', 'completion_notified': False, 'complete_seen': seen, 'completed_at': time.time()}
            if charge >= 0:
                try:
                    updates['cost'] = round(charge * _fx_to_rub(order.get('currency') or api.currency(), cfg), 6)
                except PanelError:
                    pass
            _update_order(funpay_id, **updates)
        if order.get('completion_notified') is not True:
            values = {'panel_order_id': order['panel_order_id'], 'order_id': order['panel_order_id'],
                      'funpay_order_id': funpay_id, 'link': order.get('link', '')}
            if not _send(c, order['chat_id'], _format(cfg['messages']['after_completion'], values)):
                return
            _update_order(funpay_id, completion_notified=True)
            refill = order.get('refill') or {}
            if refill.get('enabled') and cfg['refill'].get('send_moment') == 'after_complete':
                _send_refill_info(c, order, refill)
        return
    _update_order(funpay_id, status=status, remains=result.get('remains'), complete_seen=0)


def _reminder_pass(c: Any) -> None:
    cfg = load_config()
    now = time.time()
    with _wlock:
        items = [deepcopy(x) for x in waiting.values()]
    for data in items:
        if data.get('step') != 'link' or data.get('queued'):
            continue
        age = now - _float(data.get('prompted_at') or data.get('created_at'))
        funpay_id = str(data['funpay_order_id'])
        if cfg.get('remind_hours') and age >= cfg['remind_hours'] * 3600 and not data.get('reminded'):
            _set_waiting(funpay_id, reminded=True)
            _send(c, data['chat_id'], _format(cfg['messages']['link_reminder'], {
                'funpay_order_id': funpay_id,
                'example_link': _example(data.get('domains', []), data.get('platform', ''), data.get('kind', ''))}))
        if cfg.get('stale_hours') and age >= cfg['stale_hours'] * 3600 and not data.get('stale_notified'):
            _set_waiting(funpay_id, stale_notified=True)
            _notify_admins(f'⏰ Заказ #{funpay_id} уже {int(age // 3600)} ч ждёт ссылку от покупателя '
                           f'{data.get("buyer_username") or "?"}.\n{_order_url(funpay_id)}')


# ---------------------------------------------------------------------------
# Автоцена и синхронизация остатков
# ---------------------------------------------------------------------------

def _lot_price_info(cfg: Dict[str, Any], lot: Dict[str, Any], api: Optional[PanelAPI] = None) -> Dict[str, Any]:
    api = api or PanelAPI(cfg, lot['panel_id'])
    service = api.service(lot['service_id'])
    cost_rub, native, currency = _cost_rub(cfg, lot['panel_id'], service, lot['quantity'], api)
    mode, value = _effective_markup(cfg, lot)
    price = _price_from_cost(cost_rub, mode, value)
    return {'service': service, 'rate': _float(service.get('rate')), 'currency': currency, 'cost_rub': cost_rub,
            'cost_native': native, 'mode': mode, 'value': value, 'price': price}


def _pricing_pass(force: bool = False, manual: bool = False, only_keys: Optional[set] = None,
                  force_price: bool = False) -> Dict[str, Any]:
    cfg = load_config()
    report = {'updated': [], 'same': 0, 'skipped': [], 'errors': [], 'stock': 0}
    if not _account_ready():
        report['errors'].append('Аккаунт FunPay ещё не загружен.')
        return report
    full = force or time.time() - _float(_state_get('last_full_sync', 0)) > 6 * 3600
    apis: Dict[str, PanelAPI] = {}
    for key, lot in cfg['lot_mapping'].items():
        if only_keys is not None and key not in only_keys:
            continue
        label = _short(lot['name'], 40)
        if not lot.get('funpay_lot_id'):
            if lot.get('autoprice') and cfg['pricing']['enabled']:
                report['skipped'].append(f'{label}: не привязан FunPay ID')
            continue
        want_price = (cfg['pricing']['enabled'] or force_price) and lot.get('autoprice')
        want_stock = full and lot.get('stock_target') and only_keys is None
        if not want_price and not want_stock:
            continue
        try:
            api = apis.get(lot['panel_id']) or PanelAPI(cfg, lot['panel_id'])
            apis[lot['panel_id']] = api
            info = _lot_price_info(cfg, lot, api) if want_price else None
        except PanelError as exc:
            report['errors'].append(f'{label}: {exc}')
            continue
        new_price = info['price'] if info else None
        if new_price and not full and lot.get('last_price'):
            change = abs(new_price - lot['last_price']) / max(lot['last_price'], 1e-9) * 100
            if change < _float(cfg['pricing'].get('min_change'), 0.5):
                report['same'] += 1
                continue
        try:
            fields = _lot_fields(lot['funpay_lot_id'])
            updates: Dict[str, Any] = {}
            old_price = _float(getattr(fields, 'price', None) or fields.fields.get('price'))
            if new_price and abs(new_price - old_price) / max(old_price, 1e-9) * 100 >= _float(cfg['pricing'].get('min_change'), 0.5):
                updates['price'] = _money(new_price)
            if want_stock:
                amount = _int(getattr(fields, 'amount', None) if getattr(fields, 'amount', None) is not None else fields.fields.get('amount'))
                if amount < lot['stock_target'] // 2:
                    updates['amount'] = str(lot['stock_target'])
            if updates:
                _apply_lot_updates(fields, updates)
                _save_lot_fields(fields)
                if 'amount' in updates:
                    report['stock'] += 1
            if 'price' in updates:
                report['updated'].append(f'{label}: {_money(old_price)} → {_money(new_price)} ₽ (себест. {_money(info["cost_rub"])} ₽)')
            elif new_price:
                report['same'] += 1
            if new_price:
                with config_tx() as tx:
                    if key in tx['lot_mapping']:
                        tx['lot_mapping'][key]['last_price'] = new_price
        except Exception as exc:
            report['errors'].append(f'{label}: {_rejection_text(exc, cfg)}')
    if full and only_keys is None:
        _state_set('last_full_sync', time.time())
    if not manual and cfg['pricing'].get('notify') and (report['updated'] or report['errors']):
        _notify_admins(_pricing_report_text(report))
    return report


def _pricing_report_text(report: Dict[str, Any]) -> str:
    lines = ['💰 AutoSMM: пересчёт цен', f'Обновлено: {len(report["updated"])} · Без изменений: {report["same"]} · '
             f'Пропущено: {len(report["skipped"])} · Ошибок: {len(report["errors"])}']
    if report.get('stock'):
        lines.append(f'Пополнен остаток у лотов: {report["stock"]}')
    for title, items in (('Изменения', report['updated']), ('Пропущено', report['skipped']), ('Ошибки', report['errors'])):
        if items:
            lines.append(f'\n{title}:')
            lines += ['• ' + x for x in items[:25]]
            if len(items) > 25:
                lines.append(f'… и ещё {len(items) - 25}')
    return '\n'.join(lines)


# ---------------------------------------------------------------------------
# Защита от низкого баланса
# ---------------------------------------------------------------------------

def _balance_pass() -> None:
    cfg = load_config()
    if not _account_ready():
        return
    off = _state_get('balance_off', {}) or {}
    low = _state_get('balance_low', {}) or {}
    for panel_id, panel in cfg['services'].items():
        threshold = _float(panel.get('min_balance'))
        if threshold <= 0 or not panel.get('enabled', True):
            if off.get(panel_id) and threshold <= 0:
                _restore_lots(off.pop(panel_id), cfg)
                low.pop(panel_id, None)
            continue
        try:
            api = PanelAPI(cfg, panel_id)
            balance = api.balance()
            rub = _float(balance.get('balance')) * _fx_to_rub(balance['currency'], cfg)
        except PanelError:
            continue
        lots = [x for x in cfg['lot_mapping'].values() if str(x['panel_id']) == panel_id and x.get('funpay_lot_id')]
        if rub < threshold and not low.get(panel_id):
            disabled = []
            for lot in lots:
                try:
                    if _set_lot_active(lot['funpay_lot_id'], False):
                        disabled.append(lot['funpay_lot_id'])
                except Exception as exc:
                    logger.warning('[AutoSMM] Cannot disable lot %s: %s', lot['funpay_lot_id'], type(exc).__name__)
            off[panel_id] = sorted(set(off.get(panel_id, []) + disabled))
            low[panel_id] = True
            _notify_admins(f'🔴 AutoSMM: низкий баланс\nПанель «{panel["name"]}»: {rub:.2f} ₽ (порог {threshold:g} ₽).\n'
                           f'Выключено лотов на FunPay: {len(disabled)}. Пополните баланс — лоты включатся автоматически.')
        elif rub >= threshold and low.get(panel_id):
            restored = _restore_lots(off.pop(panel_id, []), cfg)
            low.pop(panel_id, None)
            _notify_admins(f'🟢 AutoSMM: баланс восстановлен\nПанель «{panel["name"]}»: {rub:.2f} ₽. Включено лотов: {restored}.')
    _state_set('balance_off', off)
    _state_set('balance_low', low)


def _restore_lots(lot_ids: List[int], cfg: Dict[str, Any]) -> int:
    restored = 0
    sales_off = set(_state_get('sales_off', []) or [])
    for lot_id in lot_ids:
        if lot_id in sales_off:
            continue
        try:
            if _set_lot_active(lot_id, True):
                restored += 1
        except Exception as exc:
            logger.warning('[AutoSMM] Cannot enable lot %s: %s', lot_id, type(exc).__name__)
    return restored


# ---------------------------------------------------------------------------
# Вотчер изменений услуг у провайдера
# ---------------------------------------------------------------------------

WATCH_MODES = ['important', 'all', 'off']
WATCH_MODE_LABELS = {'important': 'только важные', 'all': 'все', 'off': 'выключены'}


def _watcher_pass() -> None:
    """Сверяет используемые услуги с каталогом провайдера.
    Цена изменилась — цены лотов пересчитываются сразу; уведомление — одно на проверку."""
    cfg = load_config()
    if not cfg.get('watcher_enabled'):
        return
    used: Dict[str, set] = {}
    for lot in cfg['lot_mapping'].values():
        used.setdefault(str(lot['panel_id']), set()).add(str(lot['service_id']))
    snapshots = _state_get('watch', {}) or {}
    missing = _state_get('watch_missing', {}) or {}
    threshold = max(0.0, _float(cfg.get('watcher_threshold'), 5.0))
    events: List[Dict[str, Any]] = []
    reprice: set = set()
    for panel_id, service_ids in used.items():
        try:
            api = PanelAPI(cfg, panel_id)
            catalog = {str(x.get('service')): x for x in api.services(refresh=True)}
        except PanelError:
            continue
        panel_snap = snapshots.setdefault(panel_id, {})
        for service_id in service_ids:
            key = f'{panel_id}:{service_id}'
            lots = [(k, x) for k, x in cfg['lot_mapping'].items() if str(x['panel_id']) == panel_id and str(x['service_id']) == service_id]
            service = catalog.get(service_id)
            if not service:
                if service_id in panel_snap and not missing.get(key):
                    missing[key] = time.time()
                    events.append({'panel': panel_id, 'service': service_id, 'lots': lots, 'important': True,
                                   'changes': ['Услуга пропала из каталога провайдера']})
                continue
            missing.pop(key, None)
            current = {'rate': _float(service.get('rate')), 'name': str(service.get('name') or ''),
                       'min': _int(service.get('min')), 'max': _int(service.get('max'))}
            old = panel_snap.get(service_id)
            panel_snap[service_id] = current
            if not old:
                continue
            changes, important = [], False
            if abs(old['rate'] - current['rate']) > 1e-9:
                pct = (current['rate'] - old['rate']) / max(old['rate'], 1e-9) * 100
                changes.append(f'Цена за 1000: {old["rate"]:g} → {current["rate"]:g} ({pct:+.2f}%)')
                important = important or abs(pct) >= threshold
                if cfg.get('watcher_autoprice', True):
                    reprice.update(k for k, x in lots if x.get('autoprice') and x.get('funpay_lot_id'))
            if old['name'] != current['name']:
                changes.append(f'Название: «{_short(old["name"], 60)}» → «{_short(current["name"], 60)}»')
                important = True
            if old['min'] != current['min']:
                changes.append(f'Минимум: {old["min"]} → {current["min"]}')
                important = True
            if old['max'] != current['max']:
                changes.append(f'Максимум: {old["max"]} → {current["max"]}')
                important = True
            if changes:
                events.append({'panel': panel_id, 'service': service_id, 'lots': lots, 'important': important, 'changes': changes})
    _state_set('watch', snapshots)
    _state_set('watch_missing', missing)
    report = None
    if reprice and _account_ready():
        report = _pricing_pass(manual=True, only_keys=reprice, force_price=True)
    mode = cfg.get('watcher_notify', 'important')
    shown = [] if mode == 'off' else [e for e in events if mode == 'all' or e['important']]
    if shown:
        _watch_alert(cfg, shown, report)
    elif report and report['errors']:
        logger.warning('[AutoSMM] Watcher reprice errors: %s', '; '.join(report['errors'][:5]))


def _watch_alert(cfg: Dict[str, Any], events: List[Dict[str, Any]], report: Optional[Dict[str, Any]] = None) -> None:
    parts = [f'⚠️ <b>AutoSMM: провайдер изменил услуги ({len(events)})</b>']
    for event in events[:15]:
        panel = cfg['services'].get(event['panel'], {})
        lots = ', '.join(escape(_short(x['name'], 45)) for _, x in event['lots'][:3])
        more = f' и ещё {len(event["lots"]) - 3}' if len(event['lots']) > 3 else ''
        parts.append(f'\n<b>{escape(panel.get("name", event["panel"]))}</b> · услуга <code>{escape(event["service"])}</code>\n'
                     + '\n'.join('• ' + escape(x) for x in event['changes']) + f'\nЛоты: {lots}{more}')
    if len(events) > 15:
        parts.append(f'\n… и ещё {len(events) - 15} услуг')
    if report is not None:
        if report['updated']:
            parts.append('\n💰 <b>Цены лотов обновлены автоматически:</b>\n' + '\n'.join('• ' + escape(x) for x in report['updated'][:10]))
        elif cfg.get('watcher_autoprice', True):
            parts.append('\n💰 Цены лотов пересчитаны — изменение слишком мало, на FunPay цены оставлены.')
        if report['errors']:
            parts.append('\n❌ Ошибки пересчёта:\n' + '\n'.join('• ' + escape(x) for x in report['errors'][:5]))
    kb = InlineKeyboardMarkup(row_width=1)
    for event in [e for e in events if e['important']][:4]:
        count = sum(1 for _, x in event['lots'] if x.get('funpay_lot_id'))
        if count:
            kb.add(InlineKeyboardButton(f'🔴 Выключить лоты услуги {event["service"]} ({count})',
                                        callback_data=f'wdis:{event["panel"]}:{event["service"]}'))
    mode = cfg.get('watcher_notify', 'important')
    kb.row(*([InlineKeyboardButton('🔕 Только важные', callback_data='wnot:important')] if mode == 'all' else []),
           InlineKeyboardButton('🔇 Выключить', callback_data='wnot:off'), InlineKeyboardButton('✅ Ок', callback_data='wok'))
    _notify_admins('\n'.join(parts)[:4000], kb, html=True)


# ---------------------------------------------------------------------------
# Планировщик фоновых задач
# ---------------------------------------------------------------------------

def _job_intervals(cfg: Dict[str, Any]) -> Dict[str, int]:
    return {'monitor': cfg['poll_interval'], 'reminders': 300, 'pricing': cfg['pricing']['interval'] * 60,
            'balance': 300, 'watcher': 1800, 'tags': 3600}


def _scheduler(c: Any) -> None:
    next_run: Dict[str, float] = {'monitor': time.time() + 5, 'reminders': time.time() + 60, 'pricing': time.time() + 120,
                                  'balance': time.time() + 30, 'watcher': time.time() + 90, 'tags': time.time() + 20}
    while RUNNING:
        now = time.time()
        try:
            cfg = load_config()
            intervals = _job_intervals(cfg)
            for name in list(next_run):
                if now < next_run[name] or not RUNNING:
                    continue
                next_run[name] = now + intervals[name]
                try:
                    if name == 'monitor':
                        _monitor_pass(c)
                    elif name == 'reminders':
                        _reminder_pass(c)
                    elif name == 'pricing' and (cfg['pricing']['enabled'] or any(x.get('stock_target') for x in cfg['lot_mapping'].values())):
                        _pricing_pass()
                    elif name == 'balance':
                        _balance_pass()
                    elif name == 'watcher':
                        _watcher_pass()
                    elif name == 'tags':
                        _tags_pass()
                except Exception:
                    logger.exception('[AutoSMM] Job %s failed', name)
        except Exception:
            logger.exception('[AutoSMM] Scheduler failed')
        for _ in range(5):
            if not RUNNING:
                return
            time.sleep(1)


def _tags_pass() -> None:
    """Дописывает теги лотам, привязанным в прошлых версиях."""
    if not _account_ready():
        return
    cfg = load_config()
    # Лотам из прошлых версий определяем соцсеть и тип по каталогу панели.
    unknown = [(k, v) for k, v in cfg['lot_mapping'].items() if not v.get('platform') or not v.get('kind')]
    if unknown:
        found: Dict[str, Tuple[str, str]] = {}
        for key, lot in unknown:
            try:
                profile = _service_profile(PanelAPI(cfg, lot['panel_id']).service(lot['service_id']))
                found[key] = (profile['platform'], profile['kind'])
            except PanelError:
                continue
        if found:
            with config_tx() as tx:
                for key, (platform, kind) in found.items():
                    if key in tx['lot_mapping']:
                        tx['lot_mapping'][key]['platform'], tx['lot_mapping'][key]['kind'] = platform, kind
    pending = [k for k, v in cfg['lot_mapping'].items() if v.get('funpay_lot_id') and not v.get('tag_written')]
    failures = []
    for key in pending[:40]:
        ok, message = _write_tag(key)
        if not ok:
            failures.append(f'{key}: {message}')
    if failures:
        logger.warning('[AutoSMM] Tag backfill failures: %s', '; '.join(failures[:5]))


def _workers(c: Any) -> None:
    global RUNNING
    RUNNING = True
    thread = _threads.get('scheduler')
    if not thread or not thread.is_alive():
        _threads['scheduler'] = threading.Thread(target=_scheduler, args=(c,), daemon=True, name='autosmm-scheduler')
        _threads['scheduler'].start()


def _stop_workers() -> None:
    global RUNNING
    RUNNING = False


# ---------------------------------------------------------------------------
# Массовые операции с лотами FunPay
# ---------------------------------------------------------------------------

def _bulk_active(lot_ids: List[int], active: bool, progress: Optional[Callable[[int, int], None]] = None) -> Tuple[int, List[str]]:
    changed, errors = 0, []
    for index, lot_id in enumerate(lot_ids, 1):
        try:
            if _set_lot_active(lot_id, active):
                changed += 1
        except Exception as exc:
            errors.append(f'{lot_id}: {_rejection_text(exc)}')
        if progress and (index % 5 == 0 or index == len(lot_ids)):
            progress(index, len(lot_ids))
    return changed, errors


def _sales_off(progress: Optional[Callable[[int, int], None]] = None) -> Tuple[int, List[str]]:
    cfg = load_config()
    lot_ids = [x['funpay_lot_id'] for x in cfg['lot_mapping'].values() if x.get('funpay_lot_id')]
    disabled, errors = [], []
    for index, lot_id in enumerate(lot_ids, 1):
        try:
            if _set_lot_active(lot_id, False):
                disabled.append(lot_id)
        except Exception as exc:
            errors.append(f'{lot_id}: {_rejection_text(exc, cfg)}')
        if progress and (index % 5 == 0 or index == len(lot_ids)):
            progress(index, len(lot_ids))
    _state_set('sales_off', sorted(set((_state_get('sales_off', []) or []) + disabled)))
    return len(disabled), errors


def _sales_on(progress: Optional[Callable[[int, int], None]] = None) -> Tuple[int, List[str]]:
    lot_ids = _state_get('sales_off', []) or []
    blocked = {x for ids in (_state_get('balance_off', {}) or {}).values() for x in ids}
    _state_set('sales_off', [])
    changed, errors = _bulk_active([x for x in lot_ids if x not in blocked], True, progress)
    return changed, errors


def _restyle_lots(keys: List[str], choice: str, progress: Optional[Callable[[int, int], None]] = None) -> Tuple[int, List[str]]:
    """Переоформляет выставленные лоты: тексты меняются, цена и тег сохраняются."""
    cfg = load_config()
    done, errors = 0, []
    apis: Dict[str, PanelAPI] = {}
    for index, key in enumerate(keys, 1):
        lot = cfg['lot_mapping'].get(key)
        if not lot or not lot.get('funpay_lot_id'):
            continue
        try:
            api = apis.get(lot['panel_id']) or PanelAPI(cfg, lot['panel_id'])
            apis[lot['panel_id']] = api
            service = api.service(lot['service_id'])
            fields = _lot_fields(lot['funpay_lot_id'])
            limits = {'summary_ru': 80, 'summary_en': 80, 'desc_ru': _int(cfg.get('description_limit'), 5000),
                      'desc_en': _int(cfg.get('description_limit'), 5000)}
            node = getattr(getattr(fields, 'subcategory', None), 'id', None) or _int(fields.fields.get('node_id'))
            if node:
                try:
                    limits = _form_limits(_node_form(node), cfg)
                except Exception:
                    pass
            price = _float(getattr(fields, 'price', None) or fields.fields.get('price'))
            texts = render_lot_texts(service, lot['quantity'], price, choice if choice != 'keep' else (lot.get('template') or 'auto'),
                                     limits, cfg, lot['tag'])
            updates = {'fields[summary][ru]': texts['summary_ru'], 'fields[summary][en]': texts['summary_en'],
                       'fields[desc][ru]': texts['desc_ru'], 'fields[desc][en]': texts['desc_en']}
            _apply_lot_updates(fields, updates)
            try:
                _save_lot_fields(fields)
            except Exception as exc:
                bad = {str(x[0]) for x in (_funpay_rejection(exc).get('errors') or []) if isinstance(x, (list, tuple)) and x}
                if not bad & {'fields[summary][en]', 'fields[desc][en]', 'fields[name][en]'}:
                    raise
                plain = _plain_english(TAG_RE.sub('', texts['desc_en'])) + f'\n\n#SMM:{lot["tag"]}'
                _apply_lot_updates(fields, {'fields[summary][en]': _summary_field(_plain_english(texts['summary_en']), limits['summary_en']),
                                            'fields[desc][en]': plain})
                _save_lot_fields(fields)
            with config_tx() as tx:
                if key in tx['lot_mapping']:
                    item = tx['lot_mapping'][key]
                    item.update(name=texts['summary_ru'], tag_written=True, platform=texts['platform'], kind=texts['kind'])
                    if choice not in ('keep', 'auto'):
                        item['template'] = choice
            done += 1
        except Exception as exc:
            errors.append(f'{_short(lot["name"], 40)}: {exc if isinstance(exc, PanelError) else _rejection_text(exc, cfg)}')
        if progress and (index % 3 == 0 or index == len(keys)):
            progress(index, len(keys))
    return done, errors


# ---------------------------------------------------------------------------
# Статистика
# ---------------------------------------------------------------------------

def _stats(days: Optional[int]) -> Dict[str, Any]:
    cutoff = time.time() - days * 86400 if days else None
    out = {'revenue': 0.0, 'cost': 0.0, 'orders': 0, 'done': 0, 'refunded': 0, 'active': 0}
    for order in _orders():
        if cutoff and _float(order.get('created_at'), time.time()) < cutoff:
            continue
        status = str(order.get('status', '')).lower()
        refunded = bool(order.get('is_refunded')) or status == 'refunded'
        out['orders'] += 1
        if refunded:
            out['refunded'] += 1
            continue
        out['revenue'] += _float(order.get('price'))
        out['cost'] += _float(order.get('cost'))
        out['done'] += status in GOOD_STATUSES
        out['active'] += status not in GOOD_STATUSES | BAD_STATUSES and bool(order.get('panel_order_id'))
    out['profit'] = out['revenue'] - out['cost']
    return out
