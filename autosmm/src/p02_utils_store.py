

# ---------------------------------------------------------------------------
# Общие утилиты
# ---------------------------------------------------------------------------

class PanelError(RuntimeError):
    def __init__(self, message: str, *, uncertain: bool = False, status_code: Optional[int] = None):
        super().__init__(message)
        self.uncertain = uncertain
        self.status_code = status_code


class LotPublishError(RuntimeError):
    def __init__(self, message: str, *, stop_batch: bool = False):
        super().__init__(message)
        self.stop_batch = stop_batch


class ConfigGuardError(RuntimeError):
    """Запись отменена: она стёрла бы все панели или лоты."""


def _float(value: Any, default: float = 0.0) -> float:
    try:
        result = float(str(value).replace(",", ".").replace(' ', ''))
        return result if math.isfinite(result) else default
    except (ValueError, TypeError):
        return default


def _int(value: Any, default: int = 0) -> int:
    try:
        return int(str(value).strip())
    except (ValueError, TypeError):
        try:
            number = float(str(value).replace(',', '.'))
            return int(number) if math.isfinite(number) else default
        except (ValueError, TypeError):
            return default


def _markup(value: Any) -> float:
    """Валидирует наценку в процентах до сохранения."""
    number = _float(value, float("nan"))
    if not math.isfinite(number) or number < -99 or number > 10000:
        raise ValueError('Наценка должна быть числом от -99 до 10 000.')
    return number


def _money(value: float) -> str:
    if value >= 1 or value == 0:
        return f'{value:.2f}'
    return f'{value:.6f}'.rstrip('0').rstrip('.')


def _plural(number: Any, forms: Tuple[str, str, str]) -> str:
    n = abs(_int(number)) % 100
    if 11 <= n <= 19:
        return forms[2]
    n %= 10
    return forms[0] if n == 1 else forms[1] if 2 <= n <= 4 else forms[2]


def _plural_en(number: Any, forms: Tuple[str, str]) -> str:
    return forms[0] if _int(number) == 1 else forms[1]


def _human_number(value: Any) -> str:
    number = _int(value)
    return f'{number:,}'.replace(',', ' ') if number else str(value)


def _clip_units(text: str, limit: int) -> str:
    """Обрезает по UTF-16 единицам, как считает браузер FunPay."""
    return text.encode('utf-16-le')[:max(0, limit) * 2].decode('utf-16-le', errors='ignore')


def _utf16_len(text: str) -> int:
    return len(text.encode('utf-16-le')) // 2


def _summary_field(text: str, limit: int) -> str:
    text = re.sub(r'\s+', ' ', text).strip()
    if _utf16_len(text) <= limit:
        return text
    clipped = _clip_units(text, limit - 1)
    if ' ' in clipped:
        clipped = clipped.rsplit(' ', 1)[0]
    return clipped.rstrip('.,;:•|✦ ') + '…'


def _short(text: str, limit: int) -> str:
    text = re.sub(r"\s+", " ", str(text)).strip()
    if len(text) <= limit:
        return text
    cut = text[:max(1, limit - 1)].rsplit(" ", 1)[0].strip()
    return (cut or text[:limit - 1]).rstrip(".,;: ") + "…"


def _plain(text: Any) -> str:
    """FunPay показывает обычный текст: HTML-теги покупателю не отправляем."""
    return unescape(re.sub(r'<[^>]*>', '', str(text or ''))).strip()


def _format(template: str, values: Dict[str, Any]) -> str:
    class Safe(dict):
        def __missing__(self, key):
            return '{' + key + '}'
    try:
        return str(template).format_map(Safe({k: '' if v is None else str(v) for k, v in values.items()}))
    except (ValueError, AttributeError, IndexError, KeyError):
        result = str(template)
        for key, value in values.items():
            result = result.replace('{' + key + '}', '' if value is None else str(value))
        return result


def _translit(text: str) -> str:
    pairs = {"а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "е": "e", "ё": "e", "ж": "zh", "з": "z", "и": "i", "й": "y",
             "к": "k", "л": "l", "м": "m", "н": "n", "о": "o", "п": "p", "р": "r", "с": "s", "т": "t", "у": "u", "ф": "f",
             "х": "h", "ц": "ts", "ч": "ch", "ш": "sh", "щ": "sch", "ъ": "", "ы": "y", "ь": "", "э": "e", "ю": "yu", "я": "ya"}
    pairs.update({key.upper(): value.title() for key, value in list(pairs.items())})
    return re.sub(r"[ \t]+", " ", "".join(pairs.get(char, char) for char in text)).strip() or "SMM service"


def _api_url(value: Any) -> str:
    """Проверяет адрес API без попыток угадать путь панели."""
    value = str(value or '').strip().rstrip('?')
    try:
        parsed = urlparse(value)
        valid = (parsed.scheme in ('http', 'https') and parsed.hostname and not parsed.username and
                 not parsed.password and not parsed.fragment and not re.search(r'\s', value))
        parsed.port
        if any(key.lower() in {'key', 'api_key', 'token', 'action'} for key, _ in parse_qsl(parsed.query)):
            raise ValueError('Укажите адрес API без ключа и action в URL. Ключ вводится отдельно.')
    except (ValueError, TypeError) as exc:
        if str(exc).startswith('Укажите адрес API'):
            raise
        valid = False
    if not valid:
        raise ValueError('Укажите полный API URL из документации панели, например https://example.com/api/v2.')
    return value


def _api_url_warning(value: str) -> str:
    path = urlparse(value).path.strip('/')
    if not path:
        return ('⚠️ В адресе нет пути до API. Обычно он выглядит так: https://example.com/api/v2. '
                'Если у вашей панели другой формат — проигнорируйте предупреждение.')
    return ''


def _display_url(value: Any) -> str:
    try:
        parsed = urlparse(str(value))
        host = parsed.hostname or ''
        if ':' in host:
            host = '[' + host + ']'
        if parsed.port:
            host += ':' + str(parsed.port)
        return urlunparse((parsed.scheme, host, parsed.path, '', '', ''))
    except ValueError:
        return '[неверный URL]'


def _secret_values(cfg: Dict[str, Any]) -> List[str]:
    values = []
    for panel in cfg.get('services', {}).values():
        if not isinstance(panel, dict):
            continue
        values.append(str(panel.get('api_key') or ''))
        values.append(str((panel.get('proxy') or {}).get('url') or ''))
    values.append(str((cfg.get('proxy') or {}).get('url') or ''))
    result = []
    for value in values:
        if not value:
            continue
        result.append(value)
        try:
            parsed = urlparse(value if '://' in value else 'http://' + value)
            result.extend([parsed.username or '', parsed.password or ''])
        except ValueError:
            pass
    return [x for x in result if len(x) >= 3]


def _safe_detail(value: Any, cfg: Optional[Dict[str, Any]] = None, limit: int = 240) -> str:
    text = unescape(str(value))
    cfg = cfg if cfg is not None else load_config()
    for secret in sorted(set(_secret_values(cfg)), key=len, reverse=True):
        for variant in {secret, quote(secret, safe=''), quote_plus(secret)}:
            text = text.replace(variant, '[скрыто]')
    text = re.sub(r'https?://[^\s<>"\']+', lambda match: _display_url(match.group(0)), text)
    text = re.sub(r'(?i)\b(key|api_key|token|password)\s*[=:]\s*[^\s,;]+', r'\1=[скрыто]', text)
    text = re.sub(r'<[^>]*>', '', text)
    return re.sub(r'[ \t]+', ' ', text).strip()[:limit]


def _domains(values: Any) -> List[str]:
    if isinstance(values, str):
        values = re.split(r"[,\s]+", values)
    if not isinstance(values, list):
        return []
    result = []
    for value in values:
        item = str(value).lower().strip().replace("https://", "").replace("http://", "").split("/")[0].split('?')[0]
        if item == '*':
            return ['*']
        item = item.split(':')[0]
        if item.startswith("www."):
            item = item[4:]
        if item and '.' in item and re.fullmatch(r"[a-z0-9.-]+", item) and item not in result:
            result.append(item)
    return result[:40]


def _order_lock(funpay_id: Any) -> threading.Lock:
    with _order_locks_guard:
        key = str(funpay_id)
        if key not in _order_locks:
            _order_locks[key] = threading.Lock()
        return _order_locks[key]


def _submit(func: Callable, *args: Any, **kwargs: Any) -> None:
    """Сетевые операции выполняются вне потока событий FunPay и Telegram."""
    def run():
        try:
            func(*args, **kwargs)
        except Exception:
            logger.exception('[AutoSMM] Background task %s failed', getattr(func, '__name__', func))
    try:
        _executor.submit(run)
    except RuntimeError:
        threading.Thread(target=run, daemon=True).start()


# ---------------------------------------------------------------------------
# Хранилище: атомарная запись, бэкапы, восстановление
# ---------------------------------------------------------------------------

_last_backup: Dict[str, float] = {}


def _json_load(path: str) -> Tuple[str, Any]:
    if not os.path.exists(path):
        return 'missing', None
    with _lock:
        try:
            with open(path, encoding='utf-8') as file:
                return 'ok', json.load(file)
        except (OSError, ValueError, TypeError, UnicodeDecodeError):
            return 'corrupt', None


def _atomic_write(path: str, data: Any) -> None:
    with _lock:
        temporary = path + '.tmp'
        with open(temporary, 'w', encoding='utf-8') as file:
            json.dump(data, file, ensure_ascii=False, indent=2)
            file.flush()
            try:
                os.fsync(file.fileno())
            except OSError:
                pass
        os.replace(temporary, path)


def _backup(path: str, force: bool = False) -> None:
    """Хранит до 15 копий каждого файла не чаще раза в 10 минут."""
    if not os.path.exists(path):
        return
    now = time.time()
    if not force and now - _last_backup.get(path, 0) < 600:
        return
    try:
        os.makedirs(BACKUP_DIR, exist_ok=True)
        base = os.path.splitext(os.path.basename(path))[0]
        target = os.path.join(BACKUP_DIR, f'{base}-{datetime.now().strftime("%Y%m%d-%H%M%S")}.json')
        shutil.copy2(path, target)
        _last_backup[path] = now
        copies = sorted(x for x in os.listdir(BACKUP_DIR) if x.startswith(base + '-') and x.endswith('.json'))
        for name in copies[:-15]:
            os.remove(os.path.join(BACKUP_DIR, name))
    except OSError as exc:
        logger.warning('[AutoSMM] Backup of %s failed: %s', path, exc)


def _backups(path: str) -> List[str]:
    if not os.path.isdir(BACKUP_DIR):
        return []
    base = os.path.splitext(os.path.basename(path))[0]
    names = sorted((x for x in os.listdir(BACKUP_DIR) if x.startswith(base + '-') and x.endswith('.json')), reverse=True)
    return [os.path.join(BACKUP_DIR, x) for x in names]


def _load_recovering(path: str, default: Any, expected: type, label: str) -> Any:
    """Повреждённый файл не перезаписывается дефолтом: он сохраняется рядом,
    а данные восстанавливаются из последнего рабочего бэкапа."""
    status, data = _json_load(path)
    if status == 'ok' and isinstance(data, expected):
        return data
    if status == 'missing':
        return deepcopy(default)
    corrupt = f'{path}.corrupt-{datetime.now().strftime("%Y%m%d-%H%M%S")}'
    try:
        shutil.copy2(path, corrupt)
    except OSError:
        corrupt = path
    for candidate in _backups(path):
        state, value = _json_load(candidate)
        if state == 'ok' and isinstance(value, expected):
            _atomic_write(path, value)
            _admin_later(f'♻️ AutoSMM: {label} повреждён и восстановлен из бэкапа '
                         f'{os.path.basename(candidate)}. Повреждённый файл сохранён: {os.path.basename(corrupt)}.')
            return value
    _admin_later(f'🛑 AutoSMM: {label} повреждён, а рабочего бэкапа нет. Файл сохранён как '
                 f'{os.path.basename(corrupt)} — данные из него можно восстановить вручную.')
    return deepcopy(default)


# ---------------------------------------------------------------------------
# Настройки: схема 14
# ---------------------------------------------------------------------------

def _default_pricing() -> Dict[str, Any]:
    return {'enabled': False, 'mode': 'percent', 'percent': 30.0, 'fixed': 10.0, 'interval': 60,
            'usd_mode': 'auto', 'usd_rate': 95.0, 'notify': True, 'min_change': 0.5}


def _default_config() -> Dict[str, Any]:
    return {
        'schema_version': SCHEMA_VERSION, 'services': {}, 'lot_mapping': {},
        'auto_refunds': True, 'confirm_link': True, 'domain_check': True, 'anti_dumping': True, 'min_margin': 0.0,
        'api_timeout': 45, 'poll_interval': 120, 'notification_chat_id': None, 'auto_start': True,
        'notify_new_order': False, 'watcher_enabled': True, 'respect_blacklist': True,
        'watcher_notify': 'important', 'watcher_threshold': 5.0, 'watcher_autoprice': True,
        'early_complete_minutes': 5, 'link_hint_minutes': 10, 'remind_hours': 6, 'stale_hours': 24,
        'summary_limit': 80, 'description_limit': 5000,
        'proxy': {'mode': 'bot', 'url': ''},
        'pricing': _default_pricing(),
        'refill': {'send_moment': 'after_start', 'notify': True, 'auto_enable': False},
        'messages': deepcopy(DEFAULT_MESSAGES), 'message_template_version': 6,
        'paused_buyers': [],
        'design': {'theme': 'premium', 'custom': {}, 'assign': {}, 'sections': {}, 'english': True, 'title_source': 'smart'},
        'batches': {},
    }


def _new_tag(existing: Iterable[str] = ()) -> str:
    used = set(existing)
    while True:
        tag = secrets.token_hex(4)
        if tag not in used:
            return tag


def _panel(panel_id: str, value: Any) -> Dict[str, Any]:
    """Нормализует панель, сохраняя все дополнительные поля."""
    value = dict(value) if isinstance(value, dict) else {}
    method = str(value.get("request_method", "POST")).upper()
    proxy = value.get('proxy') if isinstance(value.get('proxy'), dict) else {}
    mode = str(proxy.get('mode') or 'plugin')
    value.update({
        "name": str(value.get("name") or f"Панель #{panel_id}")[:48],
        "api_url": str(value.get("api_url", "")).strip().rstrip("?"),
        "api_key": str(value.get("api_key", "")).strip(),
        "discount": max(-100.0, min(100.0, _float(value.get("discount")))),
        "request_method": method if method in ("GET", "POST") else "POST",
        "enabled": bool(value.get("enabled", True)),
        "min_balance": max(0.0, _float(value.get('min_balance'))),
        "proxy": {'mode': mode if mode in ('plugin', 'bot', 'custom', 'direct') else 'plugin',
                  'url': str(proxy.get('url') or '').strip()},
    })
    return value


def _refill_settings(value: Any) -> Dict[str, Any]:
    value = value if isinstance(value, dict) else {}
    return {'enabled': bool(value.get('enabled', False)), 'limit_enabled': bool(value.get('limit_enabled', True)),
            'limit': max(1, _int(value.get('limit'), 1)), 'period_enabled': bool(value.get('period_enabled', True)),
            'hours': max(1, _int(value.get('hours'), 168))}


def _lot(key: str, value: Any) -> Dict[str, Any]:
    """Нормализует лот, сохраняя все дополнительные поля."""
    value = dict(value) if isinstance(value, dict) else {}
    markup_mode = str(value.get('markup_mode') or 'global')
    value.update({
        "name": str(value.get("name") or key)[:200],
        "funpay_lot_id": _int(value.get("funpay_lot_id", value.get("lot_id"))) or None,
        "service_id": str(value.get("service_id", "")).strip(),
        "quantity": max(1, _int(value.get("quantity"), 1)),
        "panel_id": str(value.get("panel_id", value.get("service_number", "1"))),
        "domains": _domains(value.get("domains", value.get("allowed_domains", []))),
        "created_at": _float(value.get("created_at")) or 0.0,
        "source": str(value.get('source') or 'legacy'),
        "subcategory": _int(value.get('subcategory')) or None,
        "batch_id": str(value.get('batch_id') or ''),
        "tag": str(value.get('tag') or '').lower(),
        "tag_written": bool(value.get('tag_written', False)),
        "poll": bool(value.get('poll', False)),
        "autoprice": bool(value.get('autoprice', True)),
        "markup_mode": markup_mode if markup_mode in ('global', 'percent', 'fixed') else 'global',
        "markup_value": _float(value.get('markup_value')),
        "refill": _refill_settings(value.get('refill')),
        "template": str(value.get('template') or ''),
        "platform": str(value.get('platform') or ''),
        "kind": str(value.get('kind') or ''),
        "last_price": _float(value.get('last_price')) or None,
        "stock_target": max(0, _int(value.get('stock_target'))),
    })
    return value


def _normalize_config(cfg: Any) -> Tuple[Dict[str, Any], bool]:
    """Миграция со всех прошлых версий. Возвращает (настройки, изменились ли)."""
    if not isinstance(cfg, dict):
        cfg = {}
    original = deepcopy(cfg)
    defaults = _default_config()
    old_version = _int(cfg.get('schema_version'))
    message_version = _int(cfg.get('message_template_version'))
    for key, value in defaults.items():
        if key not in cfg:
            cfg[key] = deepcopy(value)
    if old_version < 13 and _int(cfg.get('summary_limit')) in (80, 120):
        cfg['summary_limit'] = 80
    # proxies {enabled, url} -> proxy {mode, url}: прежнее «выключено» значило «напрямую».
    old_proxy = cfg.pop('proxies', None)
    if isinstance(old_proxy, dict) and old_version < 14:
        url = str(old_proxy.get('url') or '').strip()
        cfg['proxy'] = {'mode': 'custom' if old_proxy.get('enabled') and url else 'direct', 'url': url}
    if old_version and old_version < 14 and 'poll_interval' in original:
        cfg['poll_interval'] = original['poll_interval']
    proxy = cfg.get('proxy') if isinstance(cfg.get('proxy'), dict) else {}
    mode = str(proxy.get('mode') or 'bot')
    cfg['proxy'] = {'mode': mode if mode in ('bot', 'custom', 'direct') else 'bot', 'url': str(proxy.get('url') or '').strip()}
    pricing = cfg.get('pricing') if isinstance(cfg.get('pricing'), dict) else {}
    cfg['pricing'] = {**_default_pricing(), **pricing}
    cfg['pricing']['interval'] = max(15, _int(cfg['pricing'].get('interval'), 60))
    refill = cfg.get('refill') if isinstance(cfg.get('refill'), dict) else {}
    cfg['refill'] = {**defaults['refill'], **refill}
    design = cfg.get('design') if isinstance(cfg.get('design'), dict) else {}
    cfg['design'] = {**defaults['design'], **design}
    for key in ('custom', 'assign', 'sections'):
        if not isinstance(cfg['design'].get(key), dict):
            cfg['design'][key] = {}
    if cfg['design'].get('theme') not in THEMES and not str(cfg['design'].get('theme', '')).startswith('custom:'):
        cfg['design']['theme'] = 'premium'
    if not isinstance(cfg.get('services'), dict):
        cfg['services'] = {}
    if not isinstance(cfg.get('lot_mapping'), dict):
        cfg['lot_mapping'] = {}
    if not isinstance(cfg.get('batches'), dict):
        cfg['batches'] = {}
    if not isinstance(cfg.get('paused_buyers'), list):
        cfg['paused_buyers'] = []
    cfg['services'] = {str(k): _panel(str(k), v) for k, v in cfg['services'].items()}
    lots = {str(k): _lot(str(k), v) for k, v in cfg['lot_mapping'].items()}
    used_tags = {x['tag'] for x in lots.values() if re.fullmatch(r'[0-9a-f]{8}', x['tag'])}
    for item in lots.values():
        if not re.fullmatch(r'[0-9a-f]{8}', item['tag']):
            item['tag'] = _new_tag(used_tags)
            item['tag_written'] = False
            used_tags.add(item['tag'])
        if not item['created_at']:
            item['created_at'] = time.time()
    cfg['lot_mapping'] = lots
    if not isinstance(cfg.get('messages'), dict):
        cfg['messages'] = deepcopy(DEFAULT_MESSAGES)
    for key, value in DEFAULT_MESSAGES.items():
        if not isinstance(cfg['messages'].get(key), str) or not cfg['messages'][key].strip():
            cfg['messages'][key] = value
    if message_version < 2:
        for key, old_value in _PREVIOUS_DEFAULT_MESSAGES.items():
            if cfg['messages'].get(key) == old_value:
                cfg['messages'][key] = DEFAULT_MESSAGES[key]
    if message_version < 6 and cfg['messages'].get('after_confirmation') in _PREVIOUS_CONFIRMATION_TEXTS:
        cfg['messages']['after_confirmation'] = DEFAULT_MESSAGES['after_confirmation']
    cfg['message_template_version'] = 6
    for key in ('auto_refunds', 'confirm_link', 'domain_check', 'anti_dumping', 'auto_start', 'notify_new_order',
                'watcher_enabled', 'respect_blacklist', 'watcher_autoprice'):
        cfg[key] = bool(cfg.get(key))
    if cfg.get('watcher_notify') not in ('important', 'all', 'off'):
        cfg['watcher_notify'] = 'important'
    cfg['watcher_threshold'] = max(0.0, min(1000.0, _float(cfg.get('watcher_threshold'), 5.0)))
    cfg['poll_interval'] = max(20, min(3600, _int(cfg.get('poll_interval'), 120)))
    cfg['api_timeout'] = max(5, min(180, _int(cfg.get('api_timeout'), 45)))
    cfg['early_complete_minutes'] = max(0, min(1440, _int(cfg.get('early_complete_minutes'), 5)))
    cfg['schema_version'] = SCHEMA_VERSION
    return cfg, cfg != original


_cfg_cache: Optional[Dict[str, Any]] = None


def load_config() -> Dict[str, Any]:
    """Возвращает копию настроек. Изменять настройки — только через config_tx()."""
    global _cfg_cache
    with _cfg_lock:
        if _cfg_cache is None:
            raw = _load_recovering(CONFIG_PATH, {}, dict, 'Файл настроек AutoSMM')
            cfg, changed = _normalize_config(raw)
            if changed or not os.path.exists(CONFIG_PATH):
                if os.path.exists(CONFIG_PATH):
                    _backup(CONFIG_PATH, force=True)
                _atomic_write(CONFIG_PATH, cfg)
            _cfg_cache = cfg
        return deepcopy(_cfg_cache)


def _commit_config(cfg: Dict[str, Any], allow_shrink: bool = False) -> None:
    global _cfg_cache
    with _cfg_lock:
        cfg, _ = _normalize_config(cfg)
        old = _cfg_cache or {}
        if not allow_shrink:
            for key, label in (('lot_mapping', 'лоты'), ('services', 'панели')):
                if old.get(key) and not cfg.get(key):
                    _notify_admins(f'🛑 AutoSMM: предотвращена потеря настроек — запись удалила бы все {label}. '
                                   'Изменение отменено, ничего не потеряно.')
                    raise ConfigGuardError(f'Запись удалила бы все {label}.')
        _backup(CONFIG_PATH)
        _atomic_write(CONFIG_PATH, cfg)
        _cfg_cache = cfg


@contextmanager
def config_tx(allow_shrink: bool = False):
    """Атомарная транзакция «прочитать → изменить → записать»."""
    with _cfg_lock:
        cfg = load_config()
        yield cfg
        _commit_config(cfg, allow_shrink)


def _save_config(cfg: Dict[str, Any], allow_shrink: bool = False) -> None:
    _commit_config(cfg, allow_shrink)


def _lot_by_key(key: str) -> Optional[Dict[str, Any]]:
    item = load_config()['lot_mapping'].get(key)
    return item


def _lot_key_by_funpay_id(cfg: Dict[str, Any], funpay_id: Any) -> Optional[str]:
    return next((k for k, v in cfg['lot_mapping'].items() if v.get('funpay_lot_id') and _int(v['funpay_lot_id']) == _int(funpay_id)), None)


# ---------------------------------------------------------------------------
# История заказов, ожидающие заказы и служебное состояние
# ---------------------------------------------------------------------------

_orders_cache: Optional[List[Dict[str, Any]]] = None


def _orders_raw() -> List[Dict[str, Any]]:
    global _orders_cache
    with _orders_lock:
        if _orders_cache is None:
            value = _load_recovering(ORDERS_PATH, [], list, 'История заказов AutoSMM')
            if not os.path.exists(ORDERS_PATH) and os.path.exists(LEGACY_ORDERS_PATH):
                status, legacy = _json_load(LEGACY_ORDERS_PATH)
                if status == 'ok' and isinstance(legacy, list):
                    value = [{'funpay_order_id': str(x.get('order_id', '')), 'chat_id': x.get('chat_id'),
                              'panel_id': str(x.get('service_number', 1)), 'service_id': str(x.get('service_id', '')),
                              'panel_order_id': str(x.get('id_zakaz', '')), 'status': x.get('status', 'pending'),
                              'price': _float(x.get('summa')), 'cost': _float(x.get('spent')),
                              'quantity': _int(x.get('quantity'), 1), 'link': x.get('customer_url', ''),
                              'created_at': _float(x.get('created_at'), time.time()),
                              'is_refunded': bool(x.get('is_refunded', False))}
                             for x in legacy if isinstance(x, dict) and x.get('order_id')]
                    _atomic_write(ORDERS_PATH, value)
                    logger.info('[AutoSMM] Migrated %s legacy orders.', len(value))
            _orders_cache = [x for x in value if isinstance(x, dict)]
        return _orders_cache


def _persist_orders() -> None:
    with _orders_lock:
        _backup(ORDERS_PATH)
        _atomic_write(ORDERS_PATH, _orders_raw())


def _orders() -> List[Dict[str, Any]]:
    with _orders_lock:
        return deepcopy(_orders_raw())


def _get_order(funpay_id: Any) -> Optional[Dict[str, Any]]:
    with _orders_lock:
        item = next((x for x in _orders_raw() if str(x.get('funpay_order_id')) == str(funpay_id)), None)
        return deepcopy(item) if item else None


def _add_order(order: Dict[str, Any]) -> bool:
    with _orders_lock:
        if any(str(x.get('funpay_order_id')) == str(order['funpay_order_id']) for x in _orders_raw()):
            return False
        _orders_raw().append(deepcopy(order))
        _persist_orders()
        return True


def _update_order(funpay_id: Any, **updates: Any) -> Optional[Dict[str, Any]]:
    with _orders_lock:
        item = next((x for x in _orders_raw() if str(x.get('funpay_order_id')) == str(funpay_id)), None)
        if item is None:
            return None
        changed = any(item.get(k) != v for k, v in updates.items())
        if changed:
            item.update(deepcopy(updates))
            _persist_orders()
        return deepcopy(item)


def _save_waiting() -> None:
    with _wlock:
        snapshot = deepcopy(waiting)
    _atomic_write(WAITING_PATH, snapshot)


def _load_waiting() -> Dict[str, Dict[str, Any]]:
    value = _load_recovering(WAITING_PATH, {}, dict, 'Список ожидающих заказов AutoSMM')
    if not os.path.exists(WAITING_PATH) and os.path.exists(LEGACY_WAITING_PATH):
        status, legacy = _json_load(LEGACY_WAITING_PATH)
        migrated: Dict[str, Dict[str, Any]] = {}
        if status == 'ok' and isinstance(legacy, dict):
            for key, item in legacy.items():
                if not isinstance(item, dict):
                    continue
                step = {'await_link': 'link', 'await_confirm': 'confirm'}.get(str(item.get('step')), str(item.get('step') or 'link'))
                migrated[str(key)] = {'buyer_id': item.get('buyer_id'), 'chat_id': item.get('chat_id'),
                                      'funpay_order_id': str(item.get('order_id_funpay', key)),
                                      'panel_id': str(item.get('service_number', 1)), 'service_id': str(item.get('service_id', '')),
                                      'quantity': _int(item.get('real_amount'), 1), 'price': _float(item.get('price')),
                                      'domains': _domains(item.get('allowed_domains', [])), 'step': step,
                                      **({'link': item['link']} if item.get('link') else {})}
        value = migrated
        _atomic_write(WAITING_PATH, value)
    now = time.time()
    for item in value.values():
        if isinstance(item, dict):
            item.setdefault('created_at', now)
            item.setdefault('queued', False)
    return {str(k): v for k, v in value.items() if isinstance(v, dict)}


_state_cache: Optional[Dict[str, Any]] = None


def _state_data() -> Dict[str, Any]:
    global _state_cache
    with _state_lock:
        if _state_cache is None:
            value = _load_recovering(STATE_PATH, {}, dict, 'Служебное состояние AutoSMM')
            for key, default in (('sales_off', []), ('balance_off', {}), ('balance_low', {}), ('watch', {}),
                                 ('watch_missing', {}), ('fx', {}), ('last_full_sync', 0), ('balances', {})):
                value.setdefault(key, deepcopy(default))
            _state_cache = value
        return _state_cache


def _state_get(key: str, default: Any = None) -> Any:
    with _state_lock:
        return deepcopy(_state_data().get(key, default))


def _state_set(key: str, value: Any) -> None:
    with _state_lock:
        _state_data()[key] = deepcopy(value)
        _atomic_write(STATE_PATH, _state_data())


def _global_domains() -> List[str]:
    status, value = _json_load(DOMAINS_PATH)
    result = _domains(value) if status == 'ok' else []
    return result or list(DEFAULT_DOMAINS)


def _save_global_domains(values: List[str]) -> None:
    _atomic_write(DOMAINS_PATH, _domains(values) or list(DEFAULT_DOMAINS))
