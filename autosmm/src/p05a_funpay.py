

# ---------------------------------------------------------------------------
# FunPay: лоты
# ---------------------------------------------------------------------------

def _is_rate_limited(exc: Exception) -> bool:
    response = getattr(exc, 'response', None)
    code = getattr(response, 'status_code', None)
    return code == 429 or '429' in str(exc) or 'too many' in str(exc).lower()


FP_MIN_INTERVAL = 1.1


def _fp_call(func: Callable, *args: Any, attempts: int = 4, **kwargs: Any) -> Any:
    """Запрос к FunPay с паузой между вызовами и отступлением при 429."""
    for attempt in range(attempts):
        with _fp_lock:
            delay = FP_MIN_INTERVAL - (time.time() - _last_fp_call[0])
            if delay > 0:
                time.sleep(delay)
            _last_fp_call[0] = time.time()
        try:
            return func(*args, **kwargs)
        except Exception as exc:
            if _is_rate_limited(exc) and attempt < attempts - 1:
                wait = 4 * (2 ** attempt)
                logger.warning('[AutoSMM] FunPay rate limit, retry in %ss', wait)
                time.sleep(wait)
                continue
            raise


def _account_ready() -> bool:
    account = getattr(cardinal_instance, 'account', None)
    return bool(account is not None and getattr(account, 'is_initiated', True) and getattr(account, 'id', None))


def _funpay_rejection(exc: Exception) -> Dict[str, Any]:
    """Читает только структурированный отказ FunPay, без тела запроса и cookies."""
    response = getattr(exc, 'response', None)
    try:
        value = response.json() if response is not None else None
    except (ValueError, AttributeError):
        value = None
    if not isinstance(value, dict):
        tail = str(exc).rsplit('Текст ответа:', 1)
        try:
            value = json.loads(tail[-1].strip()) if len(tail) == 2 else None
        except ValueError:
            value = None
    if isinstance(value, dict) and (value.get('error') or value.get('errors')):
        return value
    errors = getattr(exc, 'errors', None)
    if isinstance(errors, dict) and errors:
        return {'error': getattr(exc, 'error_message', '') or '', 'errors': list(errors.items())}
    return {}


def _rejection_text(exc: Exception, cfg: Optional[Dict[str, Any]] = None) -> str:
    refusal = _funpay_rejection(exc)
    parts = [str(refusal.get('error') or '')]
    errors = refusal.get('errors')
    if isinstance(errors, list):
        parts += [f'{x[0]}: {x[1]}' for x in errors if isinstance(x, (list, tuple)) and len(x) >= 2]
    text = '; '.join(x for x in parts if x)
    return _safe_detail(text or type(exc).__name__, cfg, 400)


def _node_form(subcategory: int) -> Dict[str, Any]:
    if BeautifulSoup is None:
        raise RuntimeError('Не установлен beautifulsoup4, необходимый для создания лотов.')
    form: Dict[str, Any] = {'defaults': {}, 'selects': {}, 'labels': {}, 'required': set(), 'maxlength': {}, 'hidden': set()}
    try:
        response = _fp_call(cardinal_instance.account.method, 'get', f'lots/offerEdit?node={subcategory}', {}, {}, raise_not_200=True)
        soup = BeautifulSoup(response.content.decode('utf-8', 'replace'), 'lxml')
        editor = soup.find('form', class_='form-offer-editor') or soup
        for el in editor.find_all(['input', 'textarea']):
            name = el.get('name')
            if not name or name == 'query':
                continue
            if el.name == 'textarea':
                form['defaults'][name] = el.text or ''
            elif el.get('type') == 'checkbox':
                if el.has_attr('checked'):
                    form['defaults'][name] = 'on'
            else:
                form['defaults'][name] = el.get('value') or ''
            if el.has_attr('required'):
                form['required'].add(name)
            if el.get('maxlength'):
                form['maxlength'][name] = _int(el['maxlength'])
        for el in editor.find_all('select'):
            name = el.get('name')
            if not name:
                continue
            group = el.find_parent(class_='form-group')
            if group is not None and 'hidden' in (group.get('class') or []):
                form['hidden'].add(name)
            label = group.find('label') if group is not None else None
            form['labels'][name] = label.get_text(' ', strip=True) if label else name
            options = [(x.get('value') or '', x.get_text(' ', strip=True)) for x in el.find_all('option')]
            form['selects'][name] = options
            selected = el.find('option', selected=True)
            form['defaults'][name] = selected.get('value') if selected and selected.get('value') else ''
            if el.has_attr('required'):
                form['required'].add(name)
        token = editor.find('input', {'name': 'csrf_token'})
        if token and token.get('value'):
            cardinal_instance.account.csrf_token = token['value']
    except Exception as exc:
        raise RuntimeError('Не удалось получить форму раздела FunPay: проверьте ID раздела.') from exc
    if not form['defaults'] and not form['selects']:
        raise RuntimeError('FunPay не вернул форму раздела: проверьте ID раздела и вход в аккаунт.')
    return form


def _form_limits(form: Dict[str, Any], cfg: Dict[str, Any]) -> Dict[str, int]:
    fallback = max(1, min(100, _int(cfg.get('summary_limit'), 80)))
    def pick(*keys: str, default: int) -> int:
        values = [_int(form['maxlength'].get(k)) for k in keys if _int(form['maxlength'].get(k)) > 0]
        return min(values) if values else default
    desc = pick('fields[desc][ru]', default=_int(cfg.get('description_limit'), 5000))
    return {'summary_ru': pick('fields[summary][ru]', 'fields[name][ru]', default=fallback),
            'summary_en': pick('fields[summary][en]', 'fields[name][en]', default=fallback),
            'desc_ru': desc, 'desc_en': pick('fields[desc][en]', default=desc)}


def _pick_select(options: List[Tuple[str, str]], kind: str, platform: str) -> Tuple[str, bool]:
    """Подбирает вариант по синонимам типа услуги. (значение, уверенно ли)."""
    entries = [(v, t.lower()) for v, t in options if v]
    if not entries:
        return '', False
    for word in KINDS.get(kind, KINDS['generic'])['select']:
        found = next((v for v, t in entries if word in t), None)
        if found:
            return found, True
    pinfo = _platform_info(platform)
    for word in (pinfo.get('name', '').lower(), pinfo.get('short', '').lower()):
        if word:
            found = next((v for v, t in entries if word in t), None)
            if found:
                return found, True
    for word in ('друг', 'проч', 'other'):
        found = next((v for v, t in entries if word in t), None)
        if found:
            return found, False
    return entries[0][0], False


def _lot_fields(lot_id: int) -> Any:
    return _fp_call(cardinal_instance.account.get_lot_fields, int(lot_id))


_ATTR_MAP = {'fields[summary][ru]': 'title_ru', 'fields[summary][en]': 'title_en', 'fields[desc][ru]': 'description_ru',
             'fields[desc][en]': 'description_en', 'price': 'price', 'active': 'active', 'amount': 'amount'}


def _apply_lot_updates(lot_fields: Any, updates: Dict[str, Any]) -> Any:
    """FunPayAPI перед сохранением пишет поля из атрибутов (renew_fields),
    поэтому меняем и словарь, и соответствующие атрибуты."""
    raw = {k: v for k, v in updates.items()}
    if hasattr(lot_fields, 'edit_fields'):
        lot_fields.edit_fields(raw)
    else:
        lot_fields.fields.update(raw)
    for key, attr in _ATTR_MAP.items():
        if key not in updates or not hasattr(lot_fields, attr):
            continue
        value = updates[key]
        try:
            if attr == 'price':
                value = _float(value)
            elif attr == 'active':
                value = value in ('on', True, 1, '1')
            elif attr == 'amount':
                value = _int(value) if str(value).strip() else None
            setattr(lot_fields, attr, value)
        except Exception:
            pass
    if 'fields[summary][ru]' in updates and 'fields[name][ru]' in lot_fields.fields:
        lot_fields.fields['fields[name][ru]'] = updates['fields[summary][ru]']
    if 'fields[summary][en]' in updates and 'fields[name][en]' in lot_fields.fields:
        lot_fields.fields['fields[name][en]'] = updates['fields[summary][en]']
    if getattr(cardinal_instance.account, 'csrf_token', None):
        if hasattr(lot_fields, 'csrf_token'):
            lot_fields.csrf_token = cardinal_instance.account.csrf_token
        lot_fields.fields['csrf_token'] = cardinal_instance.account.csrf_token
    return lot_fields


def _save_lot_fields(lot_fields: Any) -> None:
    _fp_call(cardinal_instance.account.save_lot, lot_fields, attempts=3)


def _edit_lot(lot_id: int, updates: Dict[str, Any]) -> Any:
    lot_fields = _lot_fields(lot_id)
    _apply_lot_updates(lot_fields, updates)
    _save_lot_fields(lot_fields)
    return lot_fields


def _lot_is_active(lot_fields: Any) -> bool:
    if hasattr(lot_fields, 'active'):
        return bool(lot_fields.active)
    return lot_fields.fields.get('active') == 'on'


def _set_lot_active(lot_id: int, active: bool) -> bool:
    """Возвращает True, если состояние изменилось."""
    lot_fields = _lot_fields(lot_id)
    if _lot_is_active(lot_fields) == active:
        return False
    _apply_lot_updates(lot_fields, {'active': 'on' if active else ''})
    _save_lot_fields(lot_fields)
    return True


def _delete_funpay_lot(lot_id: int) -> None:
    account = cardinal_instance.account
    if hasattr(account, 'delete_lot'):
        _fp_call(account.delete_lot, int(lot_id))
        return
    _fp_call(account.save_lot, LotFields(int(lot_id), {'csrf_token': account.csrf_token, 'offer_id': str(lot_id), 'deleted': '1'}))


def _write_tag(lot_key: str) -> Tuple[bool, str]:
    """Дописывает #SMM:тег в подробное описание лота (RU и EN)."""
    cfg = load_config()
    lot = cfg['lot_mapping'].get(lot_key)
    if not lot or not lot.get('funpay_lot_id'):
        return False, 'У лота не привязан FunPay ID.'
    if not _account_ready():
        return False, 'Аккаунт FunPay ещё не загружен.'
    tag = lot['tag']
    try:
        lot_fields = _lot_fields(lot['funpay_lot_id'])
        ru = lot_fields.fields.get('fields[desc][ru]', '')
        en = lot_fields.fields.get('fields[desc][en]', '')
        if f'#SMM:{tag}' in ru and (not en or f'#SMM:{tag}' in en):
            result = True, 'Тег уже записан на FunPay.'
        else:
            updates = {'fields[desc][ru]': _with_tag(ru, tag)}
            if en.strip():
                updates['fields[desc][en]'] = _with_tag(en, tag)
            _apply_lot_updates(lot_fields, updates)
            _save_lot_fields(lot_fields)
            result = True, 'Тег записан в описание лота на FunPay.'
        subcategory = getattr(getattr(lot_fields, 'subcategory', None), 'id', None) or _int(lot_fields.fields.get('node_id'))
    except Exception as exc:
        return False, 'FunPay не сохранил лот: ' + _rejection_text(exc, cfg)
    with config_tx() as tx:
        if lot_key in tx['lot_mapping']:
            tx['lot_mapping'][lot_key]['tag_written'] = True
            if subcategory and not tx['lot_mapping'][lot_key].get('subcategory'):
                tx['lot_mapping'][lot_key]['subcategory'] = subcategory
    return result


def _create_lot(subcategory: int, panel_id: str, service: Dict[str, Any], quantity: int, price: float, choice: str,
                tag: str, *, form: Optional[Dict[str, Any]] = None, select_overrides: Optional[Dict[str, str]] = None,
                stock: int = 0, summary_override: str = '') -> Tuple[int, str, List[str]]:
    """Создаёт лот на FunPay. Возвращает (ID лота или 0, название, предупреждения)."""
    cfg = load_config()
    service_type = str(service.get('type', 'Default')).strip().lower()
    if service_type not in ('default', 'poll'):
        raise PanelError('Поддерживаются услуги типа Default и Poll (ссылка + количество).')
    if price <= 0 or not math.isfinite(price):
        raise PanelError('Некорректная цена лота.')
    form = form or _node_form(subcategory)
    limits = _form_limits(form, cfg)
    texts = render_lot_texts(service, quantity, price, choice, limits, cfg, tag)
    if summary_override:
        texts['summary_ru'] = _summary_field(summary_override, limits['summary_ru'])
    warnings: List[str] = []
    account = cardinal_instance.account
    fields = dict(form['defaults'])
    min_pcs, max_pcs = _pieces_range(service, quantity)
    amount = stock or max_pcs
    fields.update({'node_id': str(subcategory), 'offer_id': '0', 'price': _money(price), 'amount': str(max(1, min(99999, amount))),
                   'active': 'on', 'fields[summary][ru]': texts['summary_ru'], 'fields[summary][en]': texts['summary_en'],
                   'fields[desc][ru]': texts['desc_ru'], 'fields[desc][en]': texts['desc_en'],
                   'csrf_token': account.csrf_token})
    if 'fields[name][ru]' in form['defaults']:
        fields['fields[name][ru]'] = texts['summary_ru']
    if 'fields[name][en]' in form['defaults']:
        fields['fields[name][en]'] = texts['summary_en']
    for name, options in form['selects'].items():
        if name in form['hidden']:
            continue
        valid = {v for v, _ in options if v}
        override = (select_overrides or {}).get(name)
        if override and override in valid:
            fields[name] = override
            continue
        value, sure = _pick_select(options, texts['kind'], texts['platform'])
        if value:
            fields[name] = value
            if not sure and len(valid) > 1:
                label = next((t for v, t in options if v == value), value)
                warnings.append(f'«{form["labels"].get(name, name)}» = «{label}» (выбрано по умолчанию)')
    missing = [form['labels'].get(x, x) for x in form['required'] if x not in form['hidden'] and not str(fields.get(x, '')).strip()]
    if missing:
        raise LotPublishError('В разделе FunPay остались обязательные поля: ' + ', '.join(missing[:4]))
    try:
        before = {_funpay_lot_id(x) for x in _fp_call(account.get_my_subcategory_lots, int(subcategory))}
    except Exception as exc:
        raise LotPublishError('Не удалось прочитать текущие лоты FunPay. Создание отменено до отправки запроса.') from exc
    subcategory_obj = account.get_subcategory(enums.SubCategoryTypes.COMMON, int(subcategory))
    result = None
    for validation_attempt in range(4):
        try:
            payload = {k: v for k, v in fields.items() if not k.startswith('_autosmm')}
            try:
                lot_obj = LotFields(0, payload, subcategory_obj, account.currency)
            except TypeError:
                lot_obj = LotFields(0, payload)
            result = _fp_call(account.save_lot, lot_obj, attempts=2)
            break
        except Exception as exc:
            refusal = _funpay_rejection(exc)
            error = str(refusal.get('error') or '')
            if 'много предложений' in error.lower():
                raise LotPublishError('Достигнут лимит предложений FunPay в этом разделе.', stop_batch=True) from None
            raw_errors = refusal.get('errors', [])
            en_fields = {str(item[0]) for item in raw_errors if isinstance(item, (list, tuple)) and len(item) >= 2
                         and str(item[0]) in {'fields[summary][en]', 'fields[name][en]', 'fields[desc][en]'}} \
                if isinstance(raw_errors, list) else set()
            if en_fields and not fields.get('_autosmm_plain_en'):
                # FunPay не принял английский текст — повторяем с простым латинским вариантом.
                fields['_autosmm_plain_en'] = '1'
                plain = _plain_english(TAG_RE.sub('', fields.get('fields[desc][en]', ''))).strip()
                fields['fields[desc][en]'] = (plain + f'\n\n#SMM:{tag}' if tag else plain)
                summary = _summary_field(_plain_english(fields.get('fields[summary][en]', '')) or 'SMM service', limits['summary_en'])
                fields['fields[summary][en]'] = summary
                if 'fields[name][en]' in fields:
                    fields['fields[name][en]'] = summary
                warnings.append('FunPay не принял английское описание — использован упрощённый латинский текст.')
                continue
            long_fields = [item[0] for item in raw_errors if isinstance(item, (list, tuple)) and len(item) >= 2
                           and str(item[0]) in {'fields[summary][ru]', 'fields[summary][en]', 'fields[name][ru]', 'fields[name][en]'}
                           and 'длинн' in str(item[1]).lower()] if isinstance(raw_errors, list) else []
            if long_fields and validation_attempt < 2:
                for key in long_fields:
                    language = 'ru' if key.endswith('[ru]') else 'en'
                    old = fields[f'fields[summary][{language}]']
                    new_limit = max(20, min(70 if validation_attempt == 0 else 55, _utf16_len(old) - 5))
                    shortened = _summary_field(old, new_limit)
                    fields[f'fields[summary][{language}]'] = shortened
                    if f'fields[name][{language}]' in fields:
                        fields[f'fields[name][{language}]'] = shortened
                continue
            if refusal:
                raise LotPublishError('FunPay отклонил сохранение лота: ' + _rejection_text(exc, cfg)) from None
            raise LotPublishError('Сохранение лота не подтверждено. Проверьте список лотов FunPay перед повтором.',
                                  stop_batch=True) from None
    summary = fields['fields[summary][ru]']
    direct_id = _funpay_lot_id(result)
    if direct_id:
        return direct_id, summary, warnings
    key = re.sub(r'\s+', ' ', summary).strip().lower()
    for attempt in range(5):
        try:
            lots = list(_fp_call(account.get_my_subcategory_lots, int(subcategory)))
            created = [lot for lot in lots if _funpay_lot_id(lot) not in before]
            exact = next((lot for lot in created if _lot_text(lot) == key), None)
            candidate = exact or (created[0] if len(created) == 1 else None)
            if candidate and _funpay_lot_id(candidate):
                return _funpay_lot_id(candidate), summary, warnings
        except Exception as exc:
            logger.warning('[AutoSMM] Cannot read created FunPay lot ID: %s', type(exc).__name__)
        time.sleep(1.0)
    warnings.append('FunPay ID созданного лота не определён — привяжите его в карточке лота.')
    return 0, summary, warnings


