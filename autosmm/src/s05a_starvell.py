

# ---------------------------------------------------------------------------
# Starvell: мост между потоками плагина и асинхронным ядром
# ---------------------------------------------------------------------------
# Логика плагина (заказы, панели, меню) выполняется в рабочих потоках, а Starvell
# Cardinal живёт в asyncio. Корутины ядра запускаются в его цикле событий и
# дожидаются из потока — так общий код остаётся одинаковым для FunPay и Starvell.

_SV: Dict[str, Any] = {'service': None, 'loop': None, 'loop_thread': None, 'bot': None, 'me': None, 'me_name': ''}


def _run(coro: Any, timeout: float = 90) -> Any:
    """Выполняет корутину в цикле событий Starvell Cardinal и ждёт результат из рабочего потока."""
    loop = _SV.get('loop')
    if loop is None or loop.is_closed():
        if asyncio.iscoroutine(coro):
            coro.close()
        raise RuntimeError('Starvell Cardinal ещё не запущен.')
    if threading.get_ident() == _SV.get('loop_thread'):
        if asyncio.iscoroutine(coro):
            coro.close()
        raise RuntimeError('Синхронный вызов из потока событий запрещён.')
    return asyncio.run_coroutine_threadsafe(coro, loop).result(timeout)


def _service() -> Any:
    service = _SV.get('service')
    if service is None:
        raise RuntimeError('Сервис Starvell ещё не подключён.')
    return service


def _sv_api_url() -> str:
    api = getattr(_service(), 'api', None)
    return str(getattr(getattr(api, 'config', None), 'API_URL', '') or 'https://starvell.com/api').rstrip('/')


def _session() -> Any:
    api = getattr(_service(), 'api', None)
    session = getattr(api, 'session', None)
    if session is None:
        raise RuntimeError('API Starvell не инициализирован (проверьте session_cookie).')
    return session


def _is_rate_limited(exc: Exception) -> bool:
    text = str(exc).lower()
    return '429' in text or 'too many' in text or 'rate limit' in text or 'слишком много' in text


FP_MIN_INTERVAL = 0.7


def _fp_call(func: Callable, *args: Any, attempts: int = 4, **kwargs: Any) -> Any:
    """Запрос к Starvell с паузой между вызовами и отступлением при лимите запросов."""
    for attempt in range(attempts):
        with _fp_lock:
            delay = FP_MIN_INTERVAL - (time.time() - _last_fp_call[0])
            if delay > 0:
                time.sleep(delay)
            _last_fp_call[0] = time.time()
        try:
            result = func(*args, **kwargs)
            if asyncio.iscoroutine(result):
                result = _run(result)
            return result
        except Exception as exc:
            if _is_rate_limited(exc) and attempt < attempts - 1:
                wait = 4 * (2 ** attempt)
                logger.warning('[AutoSMM] Starvell rate limit, retry in %ss', wait)
                time.sleep(wait)
                continue
            raise


def _account_ready() -> bool:
    return _SV.get('service') is not None and _SV.get('loop') is not None and bool(_SV.get('me'))


def _api_error(result: Any) -> str:
    """Starvell отвечает {message, data:{code}} при ошибке, success: false — в обёртках ядра."""
    if isinstance(result, dict):
        if result.get('success') is False or result.get('error'):
            return str(result.get('error') or result.get('message') or 'ошибка Starvell')
        if result.get('statusCode') and _int(result.get('statusCode')) >= 400:
            message = result.get('message')
            return '; '.join(map(str, message)) if isinstance(message, list) else str(message or result.get('statusCode'))
    return ''


def _funpay_rejection(exc: Exception) -> Dict[str, Any]:
    return {}


def _rejection_text(exc: Exception, cfg: Optional[Dict[str, Any]] = None) -> str:
    return _safe_detail(str(exc) or type(exc).__name__, cfg, 400)


# ---------------------------------------------------------------------------
# Лоты Starvell (offers)
# ---------------------------------------------------------------------------

def _dig(value: Any, *keys: str) -> Any:
    for key in keys:
        if not isinstance(value, dict):
            return None
        value = value.get(key)
    return value


def _unwrap(value: Any) -> Any:
    while isinstance(value, dict) and isinstance(value.get('data'), dict) and len(value) <= 3:
        value = value['data']
    return value


def _offer_info(offer_id: Any) -> Dict[str, Any]:
    """Данные лота: цена, количество, активность, описания."""
    offer_id = str(offer_id).strip()
    getter = getattr(_service(), 'get_offer_info', None) or getattr(getattr(_service(), 'api', None), 'get_offer_info', None)
    if callable(getter):
        info = _fp_call(getter, offer_id)
    else:
        info = _fp_call(_session().get_json, f'{_sv_api_url()}/offers/{offer_id}',
                        referer='https://starvell.com/account/sells', include_sid=True)
    info = _unwrap(info)
    if isinstance(info, dict) and isinstance(info.get('offer'), dict):
        info = info['offer']
    if not isinstance(info, dict) or not info or _api_error(info):
        raise RuntimeError(f'Лот {offer_id} не найден на вашем аккаунте Starvell. {_api_error(info)}'.strip())
    return info


def _offer_title(info: Dict[str, Any]) -> str:
    for path in (('descriptions', 'rus', 'briefDescription'), ('descriptions', 'rus', 'description'),
                 ('descriptions', 'eng', 'briefDescription'), ('briefDescription',), ('title',), ('name',)):
        value = _dig(info, *path)
        if value:
            return re.sub(r'\s+', ' ', str(value)).strip()
    return ''


class _OfferFields:
    """Обёртка лота Starvell с интерфейсом FunPay LotFields, нужным общему коду."""

    def __init__(self, offer_id: Any, info: Dict[str, Any]):
        self.lot_id = _int(offer_id) or offer_id
        self.offer_id = str(offer_id)
        self.info = info
        self.raw_price = info.get('price')
        self.price = _float(self.raw_price)
        self.amount = _int(info.get('availability', info.get('amount')), 0)
        self.active = bool(info.get('isActive', info.get('active', True)))
        self.price_changed = False
        category = info.get('categoryId') or _dig(info, 'category', 'id') or info.get('subCategoryId')
        self.subcategory = SimpleNamespace(id=_int(category))
        title = _offer_title(info)
        self.fields = {'fields[summary][ru]': title, 'fields[name][ru]': title, 'price': str(self.raw_price or ''),
                       'amount': str(self.amount), 'active': 'on' if self.active else '', 'node_id': str(_int(category) or ''),
                       'fields[desc][ru]': str(_dig(info, 'descriptions', 'rus', 'description') or ''),
                       'fields[desc][en]': str(_dig(info, 'descriptions', 'eng', 'description') or '')}


def _lot_fields(lot_id: Any) -> _OfferFields:
    return _OfferFields(lot_id, _offer_info(lot_id))


def _apply_lot_updates(lot_fields: _OfferFields, updates: Dict[str, Any]) -> _OfferFields:
    if 'price' in updates:
        lot_fields.price = _float(updates['price'])
        lot_fields.price_changed = True
    if 'amount' in updates:
        lot_fields.amount = max(0, _int(updates['amount']))
    if 'active' in updates:
        lot_fields.active = updates['active'] in ('on', True, 1, '1')
    return lot_fields


def _price_unit() -> str:
    return (_state_get('starvell', {}) or {}).get('price_unit', 'rub')


def _format_price(value: float, unit: str) -> str:
    if unit == 'kop':
        return str(int(round(value * 100)))
    return f'{value:.2f}' if value >= 1 else _money(round(value, 6))


def _post_offer(offer_id: str, price: str, availability: int, active: bool) -> Any:
    payload = {'price': price, 'availability': int(availability), 'isActive': bool(active)}
    result = _fp_call(_session().post_json, f'{_sv_api_url()}/offers/{offer_id}/partial-update', payload,
                      referer='https://starvell.com/account/sells', include_sid=True)
    error = _api_error(result)
    if error:
        raise RuntimeError(f'Starvell не сохранил лот: {error}')
    return result


def _save_lot_fields(lot_fields: _OfferFields) -> None:
    """partial-update требует цену, количество и активность вместе."""
    if not lot_fields.price_changed:
        raw = lot_fields.raw_price
        price = str(raw) if raw not in (None, '') else _format_price(lot_fields.price, _price_unit())
        _post_offer(lot_fields.offer_id, price, lot_fields.amount, lot_fields.active)
        return
    unit = _price_unit()
    target = lot_fields.price
    _post_offer(lot_fields.offer_id, _format_price(target, unit), lot_fields.amount, lot_fields.active)
    # Сверка: Starvell отдаёт цену лота строкой в рублях. Если записалось в 100 раз
    # больше или меньше — меняем единицы, исправляем и запоминаем.
    try:
        actual = _float(_offer_info(lot_fields.offer_id).get('price'))
    except Exception:
        return
    if target <= 0 or abs(actual - target) <= max(0.011, target * 0.02):
        return
    other = 'kop' if unit == 'rub' else 'rub'
    if abs(actual - target * 100) <= target * 2 or abs(actual - target / 100) <= max(0.011, target * 0.02 / 100):
        _post_offer(lot_fields.offer_id, _format_price(target, other), lot_fields.amount, lot_fields.active)
        state = _state_get('starvell', {}) or {}
        state['price_unit'] = other
        _state_set('starvell', state)
        logger.warning('[AutoSMM] Starvell price unit switched to %s for offer %s', other, lot_fields.offer_id)
        return
    raise RuntimeError(f'Starvell записал цену {actual}, ожидалось {target}. Проверьте лот вручную.')


def _edit_lot(lot_id: Any, updates: Dict[str, Any]) -> _OfferFields:
    lot_fields = _lot_fields(lot_id)
    _apply_lot_updates(lot_fields, updates)
    _save_lot_fields(lot_fields)
    return lot_fields


def _lot_is_active(lot_fields: _OfferFields) -> bool:
    return bool(lot_fields.active)


def _set_lot_active(lot_id: Any, active: bool) -> bool:
    lot_fields = _lot_fields(lot_id)
    if lot_fields.active == active:
        return False
    _apply_lot_updates(lot_fields, {'active': 'on' if active else ''})
    _save_lot_fields(lot_fields)
    return True


def _delete_funpay_lot(lot_id: Any) -> None:
    raise RuntimeError('Starvell не даёт удалять лоты через API — удалите лот на сайте.')


def _write_tag(lot_key: str) -> Tuple[bool, str]:
    """На Starvell лот заказа известен по ID (offerId) — тег в описании не нужен."""
    with config_tx() as tx:
        if lot_key in tx['lot_mapping']:
            tx['lot_mapping'][lot_key]['tag_written'] = True
    return True, 'Starvell сообщает ID лота в заказе — заказы будут распознаваться точно.'


def _node_form(subcategory: int) -> Dict[str, Any]:
    raise RuntimeError('Starvell не даёт создавать лоты через API.')


def _form_limits(form: Dict[str, Any], cfg: Dict[str, Any]) -> Dict[str, int]:
    return {'summary_ru': 100, 'summary_en': 100, 'desc_ru': 5000, 'desc_en': 5000}


def _create_lot(*args: Any, **kwargs: Any) -> Tuple[int, str, List[str]]:
    raise LotPublishError('Starvell не даёт создавать лоты через API. Создайте лот на сайте и привяжите его.')
