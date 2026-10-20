

# ---------------------------------------------------------------------------
# Прокси
# ---------------------------------------------------------------------------

def _normalize_proxy(value: str) -> str:
    """Принимает login:pass@ip:port, ip:port, ip:port:login:pass и URL со схемой."""
    value = str(value or '').strip()
    if not value:
        raise ValueError('Прокси не указан.')
    if '://' not in value:
        parts = value.split(':')
        if '@' not in value and len(parts) == 4 and parts[1].isdigit():
            value = f'{parts[2]}:{parts[3]}@{parts[0]}:{parts[1]}'
        value = 'http://' + value
    parsed = urlparse(value)
    if parsed.scheme not in ('http', 'https', 'socks5', 'socks5h', 'socks4'):
        raise ValueError('Поддерживаются схемы http, https, socks5, socks4.')
    try:
        port = parsed.port
    except ValueError:
        raise ValueError('Неверный порт прокси.') from None
    if not parsed.hostname or not port:
        raise ValueError('Укажите прокси в формате логин:пароль@адрес:порт или адрес:порт.')
    return value


def _mask_proxy(value: str) -> str:
    if not value:
        return 'не задан'
    try:
        parsed = urlparse(value)
        auth = f'{parsed.username}:***@' if parsed.username else ''
        return f'{parsed.scheme}://{auth}{parsed.hostname}:{parsed.port}'
    except ValueError:
        return '[неверный прокси]'


def _bot_proxy() -> Optional[str]:
    c = cardinal_instance
    for source in (getattr(getattr(c, 'account', None), 'proxy', None), getattr(c, 'proxy', None)):
        if isinstance(source, dict) and (source.get('https') or source.get('http')):
            return str(source.get('https') or source.get('http'))
    return None


def _resolve_proxy(cfg: Dict[str, Any], panel: Optional[Dict[str, Any]] = None) -> Tuple[Optional[str], str]:
    """Возвращает (url или None, описание способа подключения)."""
    mode, url, origin = 'plugin', '', ''
    if panel is not None:
        mode, url = panel.get('proxy', {}).get('mode', 'plugin'), panel.get('proxy', {}).get('url', '')
    if mode == 'plugin':
        mode, url, origin = cfg['proxy']['mode'], cfg['proxy']['url'], ' (как у плагина)'
    if mode == 'bot':
        proxy = _bot_proxy()
        return proxy, (f'прокси бота {_mask_proxy(proxy)}' if proxy else 'напрямую (у бота нет прокси)') + origin
    if mode == 'custom':
        if not url:
            return None, 'напрямую (свой прокси не задан)' + origin
        return url, f'свой прокси {_mask_proxy(url)}' + origin
    return None, 'напрямую' + origin


def _proxy_kwargs(proxy: Optional[str]) -> Dict[str, Any]:
    return {'proxies': {'http': proxy, 'https': proxy}} if proxy else {}


def _test_proxy(proxy: Optional[str]) -> str:
    try:
        response = requests.get('https://api.ipify.org?format=json', timeout=(8, 12), **_proxy_kwargs(proxy))
        ip = response.json().get('ip')
        return f'✅ Работает. Внешний IP: {ip}'
    except requests.exceptions.ProxyError:
        return '❌ Прокси не отвечает или отклонил авторизацию.'
    except requests.exceptions.InvalidSchema:
        return '❌ Для SOCKS-прокси на сервере нужен пакет PySocks (pip install pysocks).'
    except requests.Timeout:
        return '❌ Таймаут: прокси или сеть не ответили вовремя.'
    except Exception as exc:
        return f'❌ Ошибка проверки: {type(exc).__name__}'


# ---------------------------------------------------------------------------
# Курсы валют
# ---------------------------------------------------------------------------

def _cbr_rates() -> Dict[str, float]:
    fx = _state_get('fx', {}) or {}
    if fx.get('rates') and time.time() - _float(fx.get('ts')) < 4 * 3600:
        return fx['rates']
    cfg = load_config()
    try:
        response = requests.get('https://www.cbr-xml-daily.ru/daily_json.js', timeout=(8, 15),
                                **_proxy_kwargs(_resolve_proxy(cfg)[0]))
        valutes = response.json().get('Valute', {})
        rates = {code: _float(item.get('Value')) / max(1, _int(item.get('Nominal'), 1))
                 for code, item in valutes.items() if _float(item.get('Value')) > 0}
        if rates:
            _state_set('fx', {'ts': time.time(), 'rates': rates})
            return rates
    except Exception as exc:
        logger.warning('[AutoSMM] CBR rates unavailable: %s', type(exc).__name__)
    return fx.get('rates') or {}


def _fx_to_rub(currency: str, cfg: Optional[Dict[str, Any]] = None) -> float:
    currency = str(currency or 'RUB').upper().strip()
    if currency in ('', 'RUB', 'RUR', '₽'):
        return 1.0
    cfg = cfg or load_config()
    if currency == 'USD' and cfg['pricing'].get('usd_mode') == 'manual':
        rate = _float(cfg['pricing'].get('usd_rate'))
        if rate > 0:
            return rate
    rate = _cbr_rates().get(currency)
    if not rate:
        if currency == 'USD' and _float(cfg['pricing'].get('usd_rate')) > 0:
            return _float(cfg['pricing']['usd_rate'])
        raise PanelError(f'Нет курса для валюты {currency}. Проверьте доступ к cbr-xml-daily.ru или задайте ручной курс USD.')
    return rate


def _usd_rate_text(cfg: Dict[str, Any]) -> str:
    try:
        rate = _fx_to_rub('USD', cfg)
        return f'{rate:.2f} ₽' + (' (ручной)' if cfg['pricing'].get('usd_mode') == 'manual' else ' (ЦБ РФ)')
    except PanelError:
        return 'нет данных'


# ---------------------------------------------------------------------------
# SMM Panel API v2
# ---------------------------------------------------------------------------

def _panel_quirks(panel: Dict[str, Any]) -> Dict[str, Any]:
    host = (urlparse(str(panel.get('api_url') or '')).hostname or '').lower()
    if 'smmbind' in host:
        return {'early_minutes': 15, 'confirmations': 2, 'force_method': 'POST'}
    return {'early_minutes': 0, 'confirmations': 1, 'force_method': ''}


class PanelAPI:
    def __init__(self, cfg: Dict[str, Any], panel_id: Any):
        self.id, self.cfg = str(panel_id), cfg
        self.data = cfg['services'].get(self.id)
        if not self.data:
            raise PanelError(f'Панель #{self.id} не найдена. Проверьте сервис в карточке лота.')
        if not self.data.get('enabled', True):
            raise PanelError(f'Панель «{self.data.get("name")}» выключена.')
        try:
            self.url = _api_url(self.data.get('api_url'))
        except ValueError as exc:
            raise PanelError(str(exc)) from None
        if not self.data['api_key'] or self.data['api_key'] == 'YOUR_API_KEY':
            raise PanelError('Для панели не задан API-ключ.')
        self.proxy = _resolve_proxy(cfg, self.data)[0]
        self.quirks = _panel_quirks(self.data)

    def _error(self, message: str, action: str, *, uncertain: bool = False, status_code: Optional[int] = None) -> PanelError:
        message = _safe_detail(message, self.cfg, 650)
        logger.warning('[AutoSMM] Panel #%s action=%s: %s', self.id, action, message)
        return PanelError(message, uncertain=uncertain, status_code=status_code)

    def _api_error(self, value: Any) -> str:
        detail = _safe_detail(value, self.cfg)
        hints = {
            'user_inactive': 'Проверьте API-ключ, активность аккаунта и доступ к API в кабинете панели.',
            'invalid_api_key': 'Проверьте API-ключ в кабинете панели.', 'invalid key': 'Проверьте API-ключ в кабинете панели.',
            'incorrect api key': 'Проверьте API-ключ в кабинете панели.',
            'not enough funds': 'Пополните баланс SMM-панели.', 'insufficient funds': 'Пополните баланс SMM-панели.',
        }
        return detail + (' — ' + hints[detail.lower()] if detail.lower() in hints else '')

    def _request(self, method: str, data: Dict[str, Any], options: Dict[str, Any], read_only: bool) -> requests.Response:
        url = self.url
        for redirect in range(4):
            response = (requests.get(url, params=data, **options) if method == 'GET' else requests.post(url, data=data, **options))
            if response.status_code not in (301, 302, 303, 307, 308):
                return response
            location = response.headers.get('Location', '')
            target = urljoin(url, location)
            old, new = urlparse(url), urlparse(target)
            try:
                target = _api_url(target)
                same_origin = (old.hostname == new.hostname and
                               (old.scheme, old.port or (443 if old.scheme == 'https' else 80)) ==
                               (new.scheme, new.port or (443 if new.scheme == 'https' else 80)))
                upgrade = (old.scheme == 'http' and new.scheme == 'https' and old.hostname == new.hostname
                           and old.port in (None, 80) and new.port in (None, 443))
            except ValueError:
                same_origin = upgrade = False
            if not read_only or not location or response.status_code == 303 or not (same_origin or upgrade) or redirect == 3:
                response.close()
                raise self._error(f'HTTP {response.status_code}: API перенаправляет запрос. '
                                  f'Укажите конечный адрес API из документации панели: {_display_url(target)}.',
                                  str(data['action']), uncertain=not read_only, status_code=response.status_code)
            response.close()
            url = target
        raise PanelError('Слишком много перенаправлений API.')

    def call(self, action: str, **kwargs: Any) -> Any:
        read_only = action in {'services', 'status', 'balance', 'refill_status'}
        data = {**{k: v for k, v in kwargs.items() if v is not None}, 'key': self.data['api_key'], 'action': action}
        timeout = max(5, min(180, _int(self.cfg.get('api_timeout'), 45)))
        options: Dict[str, Any] = {'timeout': (min(10, timeout), timeout), 'allow_redirects': False,
                                   'headers': {'Accept': 'application/json', 'User-Agent': f'AutoSMM/{VERSION}'},
                                   **_proxy_kwargs(self.proxy)}
        method = self.quirks.get('force_method') or str(self.data.get('request_method', 'POST')).upper()
        attempts = 3 if read_only else 1
        for attempt in range(attempts):
            try:
                response = self._request(method, data, options, read_only)
            except requests.RequestException as exc:
                if isinstance(exc, requests.exceptions.SSLError):
                    message, retry = 'Ошибка SSL: проверьте сертификат панели и дату на сервере Cardinal.', False
                elif isinstance(exc, requests.exceptions.ProxyError):
                    message, retry = 'Ошибка прокси: проверьте его адрес, авторизацию и доступность.', False
                elif isinstance(exc, (requests.exceptions.InvalidURL, requests.exceptions.InvalidSchema, requests.exceptions.MissingSchema)):
                    message, retry = 'Неверный URL API или прокси (для SOCKS нужен пакет PySocks).', False
                elif isinstance(exc, requests.Timeout):
                    message, retry = 'Панель не ответила вовремя. Проверьте доступность API с сервера Cardinal.', True
                else:
                    message, retry = 'Не удалось связаться с панелью: проверьте DNS, сеть и доступ с IP сервера.', True
                if read_only and retry and attempt < attempts - 1:
                    time.sleep(1 + attempt * 2)
                    continue
                raise self._error(message, action, uncertain=not read_only) from None
            try:
                code = response.status_code
                try:
                    result = response.json()
                except ValueError:
                    result = None
                api_error = result.get('error') if isinstance(result, dict) else None
                if not 200 <= code < 300:
                    hints = {400: 'Неверные параметры или отказ API. Проверьте ключ и настройки панели.',
                             401: 'Панель не авторизовала запрос. Проверьте API-ключ.',
                             403: 'Доступ запрещён. Проверьте доступ к API, ограничения IP и защиту панели (прокси).',
                             404: 'Адрес API не найден. Скопируйте точный API URL из кабинета панели.',
                             405: f'Метод {method} не поддерживается. Переключите GET / POST в карточке панели.',
                             429: 'Превышен лимит запросов. Подождите и увеличьте интервал проверки.'}
                    message = f'HTTP {code}: ' + hints.get(code, 'Ошибка сервера панели.' if code >= 500 else 'Панель отклонила HTTP-запрос.')
                    if api_error:
                        message += ' Ответ API: ' + self._api_error(api_error)
                    if read_only and (code == 429 or code >= 500) and attempt < attempts - 1:
                        delay = 1 + attempt * 2
                        retry_after = response.headers.get('Retry-After')
                        if retry_after:
                            delay = _float(retry_after, 11)
                        if 0 <= delay <= 10:
                            time.sleep(delay)
                            continue
                    raise self._error(message, action, uncertain=not read_only and code >= 500, status_code=code)
                if api_error:
                    raise self._error('Панель отклонила запрос: ' + self._api_error(api_error), action)
                if result is None:
                    content_type = str(response.headers.get('Content-Type', '')).lower()
                    body = response.text[:1000].lower()
                    kind = ('HTML-страницу (защита, вход в кабинет или неверный URL)'
                            if 'html' in content_type or '<html' in body or '<!doctype' in body else 'неверный JSON или пустой ответ')
                    raise self._error(f'HTTP {code}: панель вернула {kind}. Проверьте API URL и доступ с сервера.',
                                      action, uncertain=not read_only, status_code=code)
                return result
            finally:
                response.close()
        raise PanelError('Панель не ответила.')

    def services(self, refresh: bool = False) -> List[Dict[str, Any]]:
        cached = _service_cache.get(self.id)
        identity = (self.url, self.data['api_key'], self.data.get('request_method', 'POST'), str(self.proxy))
        if cached and not refresh and time.time() - cached[0] < 900 and cached[2] == identity:
            return cached[1]
        result = self.call('services')
        if isinstance(result, dict) and isinstance(result.get('services'), list):
            result = result['services']
        if not isinstance(result, list):
            raise PanelError('API вернул не список услуг.')
        result = [x for x in result if isinstance(x, dict) and str(x.get('service', '')).strip()]
        _service_cache[self.id] = (time.time(), result, identity)
        return result

    def service(self, service_id: Any) -> Dict[str, Any]:
        found = next((x for x in self.services() if str(x.get('service')) == str(service_id).strip()), None)
        if not found:
            found = next((x for x in self.services(refresh=True) if str(x.get('service')) == str(service_id).strip()), None)
        if not found:
            raise PanelError(f'Услуга #{service_id} не найдена в панели «{self.data.get("name")}».')
        return found

    def balance(self) -> Dict[str, Any]:
        result = self.call('balance')
        if not isinstance(result, dict) or 'balance' not in result:
            raise PanelError('API вернул неверный баланс.')
        currency = str(result.get('currency') or '').upper().strip() or 'RUB'
        balances = _state_get('balances', {}) or {}
        balances[self.id] = {'balance': _float(result.get('balance')), 'currency': currency, 'ts': time.time()}
        _state_set('balances', balances)
        return {**result, 'currency': currency}

    def currency(self) -> str:
        info = (_state_get('balances', {}) or {}).get(self.id) or {}
        if info.get('currency') and time.time() - _float(info.get('ts')) < 24 * 3600:
            return str(info['currency'])
        return self.balance()['currency']

    def add(self, service_id: Any, link: str, quantity: int, **extra: Any) -> str:
        result = self.call('add', service=str(service_id), link=link, quantity=quantity, **extra)
        if not isinstance(result, dict) or not str(result.get('order') or '').isdigit() or _int(result.get('order')) <= 0:
            raise PanelError('Панель не вернула корректный номер заказа. Проверьте историю панели.', uncertain=True)
        return str(result['order'])

    def status(self, order_id: Any) -> Dict[str, Any]:
        result = self.call('status', order=str(order_id))
        if not isinstance(result, dict) or not str(result.get('status') or '').strip():
            raise PanelError('API вернул неверный статус.')
        return result

    def statuses(self, order_ids: List[str]) -> Dict[str, Dict[str, Any]]:
        """Пакетная проверка (orders=1,2,3); при неподдержке — по одному."""
        result: Dict[str, Dict[str, Any]] = {}
        ids = [str(x) for x in order_ids if str(x).isdigit()]
        for start in range(0, len(ids), 100):
            chunk = ids[start:start + 100]
            data = None
            if len(chunk) > 1:
                try:
                    data = self.call('status', orders=','.join(chunk))
                except PanelError:
                    data = None
            if isinstance(data, dict) and any(isinstance(data.get(x), dict) for x in chunk):
                for order_id in chunk:
                    item = data.get(order_id)
                    if isinstance(item, dict) and str(item.get('status') or '').strip():
                        result[order_id] = item
                continue
            for order_id in chunk:
                try:
                    result[order_id] = self.status(order_id)
                except PanelError:
                    continue
        return result

    def refill(self, order_id: Any) -> str:
        result = self.call('refill', order=str(order_id))
        refill_id = result.get('refill') if isinstance(result, dict) else None
        if isinstance(refill_id, dict):
            refill_id = refill_id.get('refill') or refill_id.get('id')
        if refill_id in (None, '', 0, '0', False):
            raise PanelError('Панель не подтвердила запуск рефилла.')
        return str(refill_id)


def _service_cost(panel: Dict[str, Any], service: Dict[str, Any], quantity: int) -> float:
    """Себестоимость в валюте панели."""
    return max(0.0, _float(service.get('rate')) * (1 - max(-100, min(100, _float(panel.get('discount')))) / 100) * quantity / 1000)


def _cost_rub(cfg: Dict[str, Any], panel_id: str, service: Dict[str, Any], quantity: int,
              api: Optional[PanelAPI] = None) -> Tuple[float, float, str]:
    """(себестоимость в рублях, себестоимость в валюте панели, валюта панели)."""
    panel = cfg['services'][str(panel_id)]
    native = _service_cost(panel, service, quantity)
    currency = (api or PanelAPI(cfg, panel_id)).currency()
    return native * _fx_to_rub(currency, cfg), native, currency


def _effective_markup(cfg: Dict[str, Any], lot: Optional[Dict[str, Any]]) -> Tuple[str, float]:
    if lot and lot.get('markup_mode') in ('percent', 'fixed'):
        return lot['markup_mode'], _float(lot.get('markup_value'))
    pricing = cfg['pricing']
    return ('fixed', _float(pricing['fixed'])) if pricing.get('mode') == 'fixed' else ('percent', _float(pricing['percent']))


def _price_from_cost(cost_rub: float, mode: str, value: float) -> float:
    price = cost_rub + value if mode == 'fixed' else cost_rub * (1 + value / 100)
    if price >= 1:
        return math.ceil(price * 100 - 1e-9) / 100
    return max(0.000001, math.ceil(price * 1_000_000 - 1e-9) / 1_000_000)


def _markup_text(mode: str, value: float) -> str:
    return f'+{value:g} ₽' if mode == 'fixed' else f'{value:+g}%'
