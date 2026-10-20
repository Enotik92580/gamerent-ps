

# ---------------------------------------------------------------------------
# Панели и прокси
# ---------------------------------------------------------------------------

def _balance_line(panel_id: str) -> str:
    info = (_state_get('balances', {}) or {}).get(panel_id)
    if not info:
        return 'неизвестно'
    age = int((time.time() - _float(info.get('ts'))) // 60)
    return f'{info["balance"]:g} {info["currency"]} ({age} мин назад)'


@route('panels')
def r_panels(q: Any) -> None:
    cfg = load_config()
    low = _state_get('balance_low', {}) or {}
    lines = ['🔌 <b>SMM-панели</b>', '']
    rows = []
    for pid, p in cfg['services'].items():
        lines.append(f'{"🟢" if p["enabled"] else "⚫"} <b>{escape(p["name"])}</b> — {escape(_display_url(p["api_url"]))}\n'
                     f'   💰 {escape(_balance_line(pid))} · 💸 мин. {p["min_balance"]:g} ₽'
                     + (' · 🔴 лоты выключены (низкий баланс)' if low.get(pid) else '') +
                     f'\n   🌐 {escape(_resolve_proxy(cfg, p)[1])}')
        rows.append([B(f'⚙️ {p["name"]}', f'pn:{pid}')])
    if not cfg['services']:
        lines.append('Панелей пока нет. Добавьте первую — понадобятся API URL и ключ из кабинета панели.')
    rows += [[B('➕ Добавить панель', 'padd'), B('🔄 Обновить балансы', 'pbal')],
             [B('🧪 Проверить все', 'ptall'), B('🌐 Общий прокси', 'px:g')], [B('◀️ Назад', 'home')]]
    _edit(q, '\n'.join(lines)[:4000], _kb(*rows))


@route('pn')
def r_panel(q: Any, pid: str) -> None:
    cfg = load_config()
    p = cfg['services'].get(pid)
    if not p:
        _edit(q, '❌ Панель не найдена.', _back_kb('panels'))
        return
    lots = sum(1 for x in cfg['lot_mapping'].values() if x['panel_id'] == pid)
    text = (f'🔌 <b>{escape(p["name"])}</b> (#{pid})\nURL: <code>{escape(_display_url(p["api_url"]))}</code>\n'
            f'Ключ: {"задан (скрыт)" if p["api_key"] else "❗ не задан"}\nМетод: {p["request_method"]} · Скидка: {p["discount"]:g}%\n'
            f'💰 Баланс: {escape(_balance_line(pid))}\n💸 Мин. баланс: {p["min_balance"]:g} ₽ {"(выкл)" if not p["min_balance"] else ""}\n'
            f'🌐 Прокси: {escape(_resolve_proxy(cfg, p)[1])}\n🛍 Лотов: {lots}')
    kb = _kb([B('🧪 Проверить', f'pt:{pid}'), B(f'Метод: {p["request_method"]}', f'pm:{pid}')],
             [B('✏️ URL', f'pe:url:{pid}'), B('🔑 Ключ', f'pe:key:{pid}')],
             [B('✏️ Название', f'pe:name:{pid}'), B('🎁 Скидка', f'pe:disc:{pid}')],
             [B('💸 Мин. баланс', f'pe:minb:{pid}'), B('🌐 Прокси', f'px:{pid}')],
             [B('⚫ Выключить' if p['enabled'] else '🟢 Включить', f'ptg:{pid}'), B('🗑 Удалить', f'pdel:{pid}')],
             [B('📋 Лоты панели', f'll:p_{pid}:0'), B('◀️ Панели', 'panels')])
    _edit(q, text, kb)


@route('pm', 'ptg')
def r_panel_toggle(q: Any, pid: str) -> None:
    with config_tx() as tx:
        p = tx['services'].get(pid)
        if not p:
            return
        if q.data.startswith('pm'):
            p['request_method'] = 'GET' if p['request_method'] == 'POST' else 'POST'
        else:
            p['enabled'] = not p['enabled']
    _service_cache.pop(pid, None)
    r_panel(q, pid)


@route('pdel')
def r_panel_delete(q: Any, pid: str) -> None:
    cfg = load_config()
    lots = sum(1 for x in cfg['lot_mapping'].values() if x['panel_id'] == pid)
    if lots:
        _answer(q, f'К панели привязано лотов: {lots}. Сначала перепривяжите или удалите их.', True)
        return
    _edit(q, f'🗑 Удалить панель «{escape(cfg["services"].get(pid, {}).get("name", pid))}»?',
          _kb([B('✅ Да, удалить', f'pdd:{pid}'), B('❌ Отмена', f'pn:{pid}')]))


@route('pdd')
def r_panel_delete_do(q: Any, pid: str) -> None:
    with config_tx(allow_shrink=True) as tx:
        if any(x['panel_id'] == pid for x in tx['lot_mapping'].values()):
            raise ValueError('К панели привязаны лоты.')
        tx['services'].pop(pid, None)
    _service_cache.pop(pid, None)
    _answer(q, '✅ Панель удалена.')
    r_panels(q)


PANEL_PROMPTS = {'url': '✏️ Введите API URL панели, например <code>https://example.com/api/v2</code>\n'
                        '(без <code>?action=</code> и ключа — их бот добавит сам):',
                 'key': '🔑 Отправьте API-ключ. Сообщение с ключом бот сразу удалит из чата.',
                 'name': '✏️ Введите название панели:', 'disc': '🎁 Скидка панели в процентах (от -100 до 100):',
                 'minb': '💸 Порог низкого баланса в рублях. Ниже порога бот выключит лоты этой панели, после пополнения — включит. '
                         '<code>0</code> — выключить защиту:'}


@route('pe')
def r_panel_edit(q: Any, field: str, pid: str) -> None:
    _ask(q, 'panel_edit', PANEL_PROMPTS[field], {'field': field, 'pid': pid}, back=f'pn:{pid}')


@inp('panel_edit')
def i_panel_edit(message: Any, text: str, data: Dict[str, Any]) -> None:
    field, pid = data['field'], data['pid']
    note = ''
    if field == 'key':
        try:
            bot.delete_message(message.chat.id, message.message_id)
        except Exception:
            note = '\n⚠️ Не удалось удалить сообщение с ключом — удалите его вручную.'
    with config_tx() as tx:
        p = tx['services'].get(pid)
        if not p:
            raise ValueError('Панель уже удалена.')
        if field == 'url':
            p['api_url'] = _api_url(text)
            note = ('\n' + _api_url_warning(p['api_url'])) if _api_url_warning(p['api_url']) else ''
        elif field == 'key':
            if len(text.strip()) < 3:
                raise ValueError('Ключ выглядит слишком коротким.')
            p['api_key'] = text.strip()
        elif field == 'name':
            if not text.strip():
                raise ValueError('Название не может быть пустым.')
            p['name'] = text.strip()[:48]
        elif field == 'disc':
            value = _float(text, float('nan'))
            if not math.isfinite(value) or not -100 <= value <= 100:
                raise ValueError('Скидка — число от -100 до 100.')
            p['discount'] = value
        elif field == 'minb':
            value = _float(text, float('nan'))
            if not math.isfinite(value) or value < 0:
                raise ValueError('Введите неотрицательное число.')
            p['min_balance'] = value
    _service_cache.pop(pid, None)
    _clear(message)
    _show(message.chat.id, '✅ Сохранено.' + escape(note), _back_kb(f'pn:{pid}', '◀️ К панели'))


@route('padd')
def r_panel_add(q: Any) -> None:
    _wizard(q.from_user.id, 'panel', reset=True)
    _ask(q, 'panel_url', '➕ <b>Новая панель</b>\n\nШаг 1/3. Введите API URL, например <code>https://example.com/api/v2</code>:', back='panels')


@inp('panel_url')
def i_panel_url(message: Any, text: str, data: Dict[str, Any]) -> None:
    wizard = _wizard(message.from_user.id, 'panel')
    wizard['url'] = _api_url(text)
    _clear(message)
    warning = _api_url_warning(wizard['url'])
    _ask(message, 'panel_key', (escape(warning) + '\n\n' if warning else '') +
         'Шаг 2/3. Отправьте API-ключ (из профиля или раздела API на сайте панели). Сообщение с ключом бот удалит.', back='panels')


@inp('panel_key')
def i_panel_key(message: Any, text: str, data: Dict[str, Any]) -> None:
    try:
        bot.delete_message(message.chat.id, message.message_id)
    except Exception:
        pass
    if len(text.strip()) < 3:
        raise ValueError('Ключ выглядит слишком коротким.')
    _wizard(message.from_user.id, 'panel')['key'] = text.strip()
    _clear(message)
    _ask(message, 'panel_name', 'Шаг 3/3. Введите название панели (например, SoxyLiker):', back='panels')


@inp('panel_name')
def i_panel_name(message: Any, text: str, data: Dict[str, Any]) -> None:
    wizard = _wizard(message.from_user.id, 'panel')
    if not wizard.get('url') or not wizard.get('key'):
        raise ValueError('Мастер устарел — начните заново.')
    with config_tx() as tx:
        pid = str(max([_int(x) for x in tx['services']] + [0]) + 1)
        tx['services'][pid] = _panel(pid, {'name': text.strip() or f'Панель #{pid}', 'api_url': wizard['url'], 'api_key': wizard['key']})
    _wizards.pop(message.from_user.id, None)
    _clear(message)
    _show(message.chat.id, '✅ Панель добавлена. ⏳ Проверяю подключение…')
    _submit(lambda: _show(message.chat.id, _panel_diagnostics(load_config(), pid), _back_kb(f'pn:{pid}', '◀️ К панели')))


def _panel_diagnostics(cfg: Dict[str, Any], panel_id: str) -> str:
    panel = cfg['services'].get(panel_id, {})
    name = escape(str(panel.get('name') or panel_id))
    if not panel.get('enabled', True):
        return f'⚫ {name}: панель выключена.'
    try:
        api = PanelAPI(cfg, panel_id)
        services = api.services(True)
    except PanelError as exc:
        return f'❌ {name}: {escape(str(exc))}'
    try:
        balance = api.balance()
    except PanelError as exc:
        return f'⚠️ {name}: услуг {len(services)}, баланс: {escape(str(exc))}'
    return f'✅ {name}: {len(services)} услуг, баланс {escape(str(balance.get("balance")))} {escape(balance["currency"])}'


@route('pt', 'ptall', 'pbal')
def r_panel_test(q: Any, pid: str = '') -> None:
    cfg = load_config()
    pids = [pid] if pid else list(cfg['services'])
    _answer(q, '⏳ Проверяю…')
    def job() -> None:
        lines = ['🧪 <b>Проверка панелей</b>', ''] + [_panel_diagnostics(cfg, x) for x in pids]
        _show(q.message.chat.id, '\n\n'.join(lines)[:4000], _back_kb(f'pn:{pid}' if pid else 'panels', '◀️ Назад'))
    _submit(job)


def _proxy_target(cfg: Dict[str, Any], target: str) -> Dict[str, Any]:
    return cfg['proxy'] if target == 'g' else cfg['services'][target]['proxy']


PROXY_MODES_GLOBAL = ['bot', 'custom', 'direct']
PROXY_MODES_PANEL = ['plugin', 'bot', 'custom', 'direct']
PROXY_LABELS = {'plugin': 'как у плагина', 'bot': 'прокси бота', 'custom': 'свой прокси', 'direct': 'напрямую'}


@route('px')
def r_proxy(q: Any, target: str) -> None:
    cfg = load_config()
    if target != 'g' and target not in cfg['services']:
        _edit(q, '❌ Панель не найдена.', _back_kb('panels'))
        return
    proxy = _proxy_target(cfg, target)
    title = 'Общий прокси плагина' if target == 'g' else f'Прокси панели «{cfg["services"][target]["name"]}»'
    current = _resolve_proxy(cfg, None if target == 'g' else cfg['services'][target])[1]
    text = (f'🌐 <b>{escape(title)}</b>\n\nРежим: <b>{PROXY_LABELS[proxy["mode"]]}</b>\nПрокси бота: {escape(_mask_proxy(_bot_proxy() or ""))}\n'
            f'Свой прокси: {escape(_mask_proxy(proxy["url"]))}\nСейчас используется: <b>{escape(current)}</b>\n\n'
            'Касается только запросов к SMM-панелям и курса валют. FunPay всегда работает через прокси бота.\n'
            'Форматы: <code>логин:пароль@ip:порт</code>, <code>ip:порт</code>, <code>socks5://логин:пароль@ip:порт</code>.')
    rows = [[B(f'Режим: {PROXY_LABELS[proxy["mode"]]}', f'pxm:{target}')],
            [B('✍️ Свой прокси: ' + ('задан' if proxy['url'] else 'не задан'), f'pxs:{target}')]]
    if proxy['url']:
        rows.append([B('🗑 Удалить свой прокси', f'pxd:{target}')])
    rows.append([B('🧪 Проверить прокси', f'pxt:{target}')])
    rows.append([B('◀️ Назад', 'panels' if target == 'g' else f'pn:{target}')])
    _edit(q, text, _kb(*rows))


@route('pxm')
def r_proxy_mode(q: Any, target: str) -> None:
    with config_tx() as tx:
        proxy = _proxy_target(tx, target)
        modes = PROXY_MODES_GLOBAL if target == 'g' else PROXY_MODES_PANEL
        nxt = modes[(modes.index(proxy['mode']) + 1) % len(modes)] if proxy['mode'] in modes else modes[0]
        if nxt == 'custom' and not proxy['url']:
            nxt = modes[(modes.index('custom') + 1) % len(modes)]
            _answer(q, 'Режим «свой прокси» недоступен, пока прокси не задан.', True)
        proxy['mode'] = nxt
    _service_cache.clear()
    r_proxy(q, target)


@route('pxd')
def r_proxy_delete(q: Any, target: str) -> None:
    with config_tx() as tx:
        proxy = _proxy_target(tx, target)
        proxy['url'] = ''
        proxy['mode'] = 'bot' if target == 'g' else 'plugin'
    _service_cache.clear()
    r_proxy(q, target)


@route('pxs')
def r_proxy_set(q: Any, target: str) -> None:
    _ask(q, 'proxy_set', '✍️ Отправьте прокси одним сообщением (<code>логин:пароль@ip:порт</code> или <code>ip:порт</code>). '
                         'Сообщение будет удалено из чата.', {'target': target}, back=f'px:{target}')


@inp('proxy_set')
def i_proxy_set(message: Any, text: str, data: Dict[str, Any]) -> None:
    value = _normalize_proxy(text)
    try:
        bot.delete_message(message.chat.id, message.message_id)
    except Exception:
        pass
    with config_tx() as tx:
        proxy = _proxy_target(tx, data['target'])
        proxy['url'], proxy['mode'] = value, 'custom'
    _service_cache.clear()
    _clear(message)
    _show(message.chat.id, '✅ Прокси сохранён и включён. ⏳ Проверяю…')
    _submit(lambda: _show(message.chat.id, escape(_test_proxy(value)), _back_kb(f'px:{data["target"]}', '◀️ К прокси')))


@route('pxt')
def r_proxy_test(q: Any, target: str) -> None:
    cfg = load_config()
    proxy, label = _resolve_proxy(cfg, None if target == 'g' else cfg['services'].get(target))
    _answer(q, '⏳ Проверяю…')
    _submit(lambda: _show(q.message.chat.id, f'🧪 {escape(label)}\n{escape(_test_proxy(proxy))}', _back_kb(f'px:{target}', '◀️ К прокси')))


# ---------------------------------------------------------------------------
# Цены
# ---------------------------------------------------------------------------

@route('pr')
def r_pricing(q: Any) -> None:
    cfg = load_config()
    p = cfg['pricing']
    lots = cfg['lot_mapping'].values()
    active = sum(1 for x in lots if x['autoprice'] and x.get('funpay_lot_id'))
    text = (f'💰 <b>Ценообразование</b>\n\nАвтопересчёт: {"✅ ВКЛ" if p["enabled"] else "❌ ВЫКЛ"}\n'
            f'Наценка: {"💵 фикс " + format(p["fixed"], "g") + " ₽" if p["mode"] == "fixed" else "➗ " + format(p["percent"], "g") + " %"}\n'
            f'Интервал: {p["interval"]} мин · мин. изменение: {p["min_change"]:g}%\nКурс USD: {escape(_usd_rate_text(cfg))}\n'
            f'Лотов с автоценой: {active} из {len(cfg["lot_mapping"])}\n\n'
            'Цена = себестоимость × (1 + %) или себестоимость + фикс. Цены от 1 ₽ округляются вверх до копеек, '
            'дешёвые — до 6 знаков. Учитывайте комиссию FunPay в наценке.')
    kb = _kb([B(f'Автопересчёт: {"ВКЛ" if p["enabled"] else "ВЫКЛ"}', 'prt:en'), B(f'Режим: {"💵 фикс" if p["mode"] == "fixed" else "➗ %"}', 'prt:mode')],
             [B(f'✏️ Процент ({p["percent"]:g}%)', 'pre:percent'), B(f'✏️ Фикс ({p["fixed"]:g} ₽)', 'pre:fixed')],
             [B(f'⏱ Интервал ({p["interval"]} мин)', 'pre:interval'), B(f'📉 Мин. изменение ({p["min_change"]:g}%)', 'pre:minch')],
             [B(f'💱 Курс USD: {"ручной" if p["usd_mode"] == "manual" else "авто"}', 'prt:usd'), B(f'✏️ Курс ({p["usd_rate"]:g} ₽)', 'pre:usd')],
             [B(f'{"🔔" if p["notify"] else "🔕"} Уведомления', 'prt:notify'), B('🔄 Пересчитать сейчас', 'prnow')],
             [B('◀️ Назад', 'home')])
    _edit(q, text, kb)


@route('prt')
def r_pricing_toggle(q: Any, what: str) -> None:
    with config_tx() as tx:
        p = tx['pricing']
        if what == 'en':
            p['enabled'] = not p['enabled']
        elif what == 'mode':
            p['mode'] = 'percent' if p['mode'] == 'fixed' else 'fixed'
        elif what == 'usd':
            p['usd_mode'] = 'auto' if p['usd_mode'] == 'manual' else 'manual'
        elif what == 'notify':
            p['notify'] = not p['notify']
    r_pricing(q)


PRICING_PROMPTS = {'percent': ('✏️ Наценка в процентах (не меньше 0):', 'percent'), 'fixed': ('✏️ Фиксированная наценка в рублях:', 'fixed'),
                   'interval': ('⏱ Интервал пересчёта в минутах (минимум 15):', 'interval'),
                   'minch': ('📉 Минимальное изменение цены в процентах, при котором цена обновляется на FunPay:', 'min_change'),
                   'usd': ('✏️ Курс USD → RUB для ручного режима:', 'usd_rate')}


@route('pre')
def r_pricing_edit(q: Any, what: str) -> None:
    _ask(q, 'pricing_edit', PRICING_PROMPTS[what][0], {'what': what}, back='pr')


@inp('pricing_edit')
def i_pricing_edit(message: Any, text: str, data: Dict[str, Any]) -> None:
    value = _float(text, float('nan'))
    if not math.isfinite(value) or value < 0:
        raise ValueError('Нужно неотрицательное число.')
    key = PRICING_PROMPTS[data['what']][1]
    if key == 'interval':
        value = max(15, int(value))
    if key == 'usd_rate' and value <= 0:
        raise ValueError('Курс должен быть больше нуля.')
    if key == 'percent' and value > 10000:
        raise ValueError('Слишком большая наценка.')
    with config_tx() as tx:
        tx['pricing'][key] = value
    _clear(message)
    _show(message.chat.id, '✅ Сохранено.', _back_kb('pr', '◀️ К ценообразованию'))


@route('prnow')
def r_pricing_now(q: Any) -> None:
    if not _account_ready():
        _answer(q, 'Аккаунт FunPay ещё не загружен.', True)
        return
    _answer(q)
    def job(progress: Callable) -> str:
        report = _pricing_pass(force=True, manual=True)
        return escape(_pricing_report_text(report))
    _progress_job(q.message.chat.id, 'Пересчитываю цены', job, _back_kb('pr', '◀️ К ценообразованию'))


# ---------------------------------------------------------------------------
# Оформление
# ---------------------------------------------------------------------------

@route('ds')
def r_design(q: Any) -> None:
    cfg = load_config()
    d = cfg['design']
    text = (f'🎨 <b>Оформление лотов</b>\n\nТема по умолчанию: <b>{escape(_template_name(cfg, d["theme"]))}</b>\n'
            f'Свои шаблоны: {len(d["custom"])} · Назначений: {len(d["assign"])} · Разделов в карте: {len(d["sections"])}\n'
            f'Английская версия: {"полноценная" if d.get("english", True) else "транслит"}\n'
            f'Название лота: {"тип + соцсеть + вариант услуги" if d.get("title_source") != "panel" else "название услуги из панели"}\n\n'
            'Порядок выбора оформления: шаблон лота → назначение «соцсеть + тип» → «соцсеть» → «тип» → тема по умолчанию.')
    kb = _kb([B('🎨 Тема по умолчанию', 'dsth'), B('👁 Предпросмотр', 'dsp')],
             [B('🧩 Свои шаблоны', 'dsc'), B('📌 Назначения', 'dsa')],
             [B('🗺 Карта разделов FunPay', 'dsm'), B('🔄 Переоформить лоты', 'dsr')] if CAN_CREATE_LOTS else [B('📋 Текст для лота', 'ntx')],
             [B(f'🏷 Название: {"тип + вариант" if d.get("title_source") != "panel" else "как в панели"}', 'dstn')],
             [B(f'🇬🇧 EN: {"перевод" if d.get("english", True) else "транслит"}', 'dse'), B('◀️ Назад', 'home')])
    _edit(q, text, kb)


@route('dstn')
def r_design_title_source(q: Any) -> None:
    with config_tx() as tx:
        tx['design']['title_source'] = 'panel' if tx['design'].get('title_source') != 'panel' else 'smart'
    _answer(q, 'Новое название применится к новым лотам. Уже выставленные — через «🔄 Переоформить лоты».', True)
    r_design(q)


@route('dse')
def r_design_english(q: Any) -> None:
    with config_tx() as tx:
        tx['design']['english'] = not tx['design'].get('english', True)
    r_design(q)


@route('dsth')
def r_design_theme(q: Any) -> None:
    cfg = load_config()
    rows = [[B(('✅ ' if cfg['design']['theme'] == key else '') + THEMES[key]['title'], f'dss:{key}'), B('👁', f'dsv:{key}')]
            for key in THEME_ORDER]
    rows += [[B(('✅ ' if cfg['design']['theme'] == 'custom:' + k else '') + '🧩 ' + _short(v.get('name', k), 24), f'dss:c-{k}'),
              B('👁', f'dsv:c-{k}')] for k, v in cfg['design']['custom'].items()]
    rows.append([B('◀️ Назад', 'ds')])
    _edit(q, '🎨 <b>Тема по умолчанию</b>\n\nНажмите на тему, чтобы выбрать, или 👁 — чтобы посмотреть пример.', _kb(*rows))


@route('dss')
def r_design_theme_set(q: Any, token: str) -> None:
    with config_tx() as tx:
        tx['design']['theme'] = _token_choice(token)
    _answer(q, '✅ Тема по умолчанию изменена. Уже выставленные лоты можно переоформить в «🔄 Переоформить лоты».', True)
    r_design_theme(q)


def _preview_text(choice: str, platform: str = 'telegram', kind: str = 'followers') -> str:
    cfg = load_config()
    service = _preview_sample(platform, kind)
    texts = render_lot_texts(service, 100, 49.9, choice, {'summary_ru': 80, 'summary_en': 80, 'desc_ru': 5000, 'desc_en': 5000},
                             cfg, 'a1b2c3d4')
    return (f'👁 <b>{escape(_template_name(cfg, texts["template"]))}</b> · {escape(_platform_info(platform)["name"])} · '
            f'{escape(KINDS[kind]["ru"].lower())}\n\n<b>{escape(texts["summary_ru"])}</b>\n\n'
            f'<blockquote expandable>{escape(texts["desc_ru"][:2800])}</blockquote>\n\n🇬🇧 <b>{escape(texts["summary_en"])}</b>')


@route('dsv')
def r_design_view(q: Any, token: str) -> None:
    _answer(q)
    _show(q.message.chat.id, _preview_text(_token_choice(token)), _kb([B('✅ Сделать темой по умолчанию', f'dss:{token}')]))


@route('dsp')
def r_design_preview(q: Any, platform: str = '', kind: str = '') -> None:
    if not platform:
        rows = [[B(f'{PLATFORMS[k]["emoji"]} {PLATFORMS[k]["name"]}', f'dsp:{k}') for k in PLATFORM_ORDER[i:i + 3]]
                for i in range(0, len(PLATFORM_ORDER), 3)]
        rows.append([B('◀️ Назад', 'ds')])
        _edit(q, '👁 Выберите соцсеть для примера:', _kb(*rows))
        return
    if not kind:
        rows = [[B(f'{KINDS[k]["emoji"]} {KINDS[k]["ru"].capitalize()}', f'dsp:{platform}:{k}') for k in KIND_ORDER[i:i + 2]]
                for i in range(0, len(KIND_ORDER), 2)]
        rows.append([B('◀️ Назад', 'dsp')])
        _edit(q, '👁 Выберите тип услуги:', _kb(*rows))
        return
    _answer(q)
    cfg = load_config()
    choice = _template_choice(cfg, {'platform': platform, 'kind': kind}, 'auto')
    _show(q.message.chat.id, _preview_text(choice, platform, kind),
          _kb(*_template_buttons(f'dspt:{platform}:{kind}', cfg, include_auto=False)))


@route('dspt')
def r_design_preview_theme(q: Any, platform: str, kind: str, token: str) -> None:
    _answer(q)
    _edit(q, _preview_text(_token_choice(token), platform, kind),
          _kb(*_template_buttons(f'dspt:{platform}:{kind}', load_config(), include_auto=False)))


@route('dsc')
def r_design_custom(q: Any) -> None:
    cfg = load_config()
    rows = [[B('🧩 ' + _short(v.get('name', k), 40), f'dsci:{k}')] for k, v in cfg['design']['custom'].items()]
    rows.append([B('➕ Создать шаблон', 'dscn'), B('📥 Импорт', 'dscim')])
    rows.append([B('◀️ Назад', 'ds')])
    _edit(q, '🧩 <b>Свои шаблоны</b>\n\nШаблон — это ваш текст названия и описания с переменными. '
             'Подходит, если хочется полностью своё оформление.\n\nПеременные:\n<code>' + escape(CUSTOM_TEMPLATE_VARS) + '</code>', _kb(*rows))


@route('dscn')
def r_design_custom_new(q: Any) -> None:
    _ask(q, 'custom_new', '➕ Введите название нового шаблона (для себя):', back='dsc')


@inp('custom_new')
def i_custom_new(message: Any, text: str, data: Dict[str, Any]) -> None:
    if not text.strip():
        raise ValueError('Название не может быть пустым.')
    template_id = secrets.token_hex(3)
    with config_tx() as tx:
        tx['design']['custom'][template_id] = {
            'name': text.strip()[:40],
            'summary_ru': '⭐ {title} ⭐ {badges}',
            'desc_ru': ('━━━━━━━━━━━━━━━━━━━━\n{emoji} {title}\n━━━━━━━━━━━━━━━━━━━━\n\n⚡ Автовыдача 24/7\n\n'
                        '📦 1 шт. = {per_piece} {unit}\n🧾 Услуга: {clean_name}\n\n📝 Как заказать:\n'
                        '1️⃣ Оплатите от {min_pcs} до {max_pcs} шт.\n2️⃣ Отправьте {link_hint}: {example_link}\n'
                        '3️⃣ Подтвердите запуск «+»\n\n⚠️ Важно:\n{requirements}'),
            'summary_en': '', 'desc_en': ''}
    _clear(message)
    _show(message.chat.id, '✅ Шаблон создан с примером текста — отредактируйте его.', _kb([B('🧩 Открыть шаблон', f'dsci:{template_id}')]))


CUSTOM_FIELDS = {'sr': ('summary_ru', 'Название (RU)'), 'dr': ('desc_ru', 'Описание (RU)'),
                 'se': ('summary_en', 'Название (EN)'), 'de': ('desc_en', 'Описание (EN)')}


@route('dsci')
def r_design_custom_item(q: Any, template_id: str) -> None:
    cfg = load_config()
    item = cfg['design']['custom'].get(template_id)
    if not item:
        _edit(q, '❌ Шаблон не найден.', _back_kb('dsc'))
        return
    text = (f'🧩 <b>{escape(item["name"])}</b>\n\n<b>Название RU:</b>\n<code>{escape(item.get("summary_ru") or "—")}</code>\n\n'
            f'<b>Описание RU:</b>\n<blockquote expandable>{escape((item.get("desc_ru") or "—")[:1500])}</blockquote>\n\n'
            f'<b>EN:</b> {"свои тексты" if item.get("summary_en") or item.get("desc_en") else "автоматический перевод темы"}')
    kb = _kb([B('✏️ Название RU', f'dsce:sr:{template_id}'), B('✏️ Описание RU', f'dsce:dr:{template_id}')],
             [B('✏️ Название EN', f'dsce:se:{template_id}'), B('✏️ Описание EN', f'dsce:de:{template_id}')],
             [B('👁 Предпросмотр', f'dsv:c-{template_id}'), B('📤 Экспорт', f'dscx:{template_id}')],
             [B('🗑 Удалить', f'dscd:{template_id}'), B('◀️ Шаблоны', 'dsc')])
    _edit(q, text[:4000], kb)


@route('dsce')
def r_design_custom_edit(q: Any, field: str, template_id: str) -> None:
    _ask(q, 'custom_edit', f'✏️ Отправьте новый текст: {CUSTOM_FIELDS[field][1]}.\nПеременные: <code>{escape(CUSTOM_TEMPLATE_VARS)}</code>\n'
                           '<code>-</code> — очистить (для EN — использовать автоматический перевод).',
         {'field': field, 'id': template_id}, back=f'dsci:{template_id}')


@inp('custom_edit')
def i_custom_edit(message: Any, text: str, data: Dict[str, Any]) -> None:
    field = CUSTOM_FIELDS[data['field']][0]
    with config_tx() as tx:
        item = tx['design']['custom'].get(data['id'])
        if not item:
            raise ValueError('Шаблон удалён.')
        value = '' if text.strip() == '-' else text
        if field == 'summary_ru' and not value.strip():
            raise ValueError('Название RU обязательно.')
        item[field] = value[:5000]
    _clear(message)
    _show(message.chat.id, '✅ Сохранено.', _kb([B('🧩 К шаблону', f'dsci:{data["id"]}'), B('👁 Предпросмотр', f'dsv:c-{data["id"]}')]))


@route('dscd')
def r_design_custom_delete(q: Any, template_id: str) -> None:
    with config_tx() as tx:
        tx['design']['custom'].pop(template_id, None)
        choice = 'custom:' + template_id
        if tx['design']['theme'] == choice:
            tx['design']['theme'] = 'premium'
        tx['design']['assign'] = {k: v for k, v in tx['design']['assign'].items() if v != choice}
        for lot in tx['lot_mapping'].values():
            if lot.get('template') == choice:
                lot['template'] = ''
    _answer(q, '🗑 Шаблон удалён.')
    r_design_custom(q)


@route('dscx')
def r_design_custom_export(q: Any, template_id: str) -> None:
    item = load_config()['design']['custom'].get(template_id)
    _answer(q)
    if item:
        _show(q.message.chat.id, '📤 Скопируйте JSON шаблона:\n<code>' + escape(json.dumps(item, ensure_ascii=False)) + '</code>')


@route('dscim')
def r_design_custom_import(q: Any) -> None:
    _ask(q, 'custom_import', '📥 Отправьте JSON шаблона (из «📤 Экспорт»):', back='dsc')


@inp('custom_import')
def i_custom_import(message: Any, text: str, data: Dict[str, Any]) -> None:
    try:
        item = json.loads(text)
    except ValueError:
        raise ValueError('Это не JSON.') from None
    if not isinstance(item, dict) or not str(item.get('summary_ru') or '').strip():
        raise ValueError('В шаблоне нет поля summary_ru.')
    template_id = secrets.token_hex(3)
    with config_tx() as tx:
        tx['design']['custom'][template_id] = {'name': str(item.get('name') or 'Импорт')[:40],
                                               **{k: str(item.get(k) or '')[:5000] for k in ('summary_ru', 'desc_ru', 'summary_en', 'desc_en')}}
    _clear(message)
    _show(message.chat.id, '✅ Шаблон импортирован.', _kb([B('🧩 Открыть', f'dsci:{template_id}')]))


@route('dsa')
def r_design_assign(q: Any) -> None:
    cfg = load_config()
    items = list(cfg['design']['assign'].items())
    rows = []
    for index, (key, choice) in enumerate(items[:30]):
        platform, kind = key.split(':')
        label = (f'{"все соцсети" if platform == "*" else _platform_info(platform)["name"]} · '
                 f'{"все типы" if kind == "*" else KINDS.get(kind, KINDS["generic"])["ru"].lower()} → {_template_name(cfg, choice)}')
        rows.append([B('❌ ' + _short(label, 55), f'dsad:{index}')])
    rows.append([B('➕ Назначить', 'dsan'), B('◀️ Назад', 'ds')])
    _edit(q, '📌 <b>Назначения шаблонов</b>\n\nНапример: «Telegram · реакции → 🔥 Яркий». Нажмите ❌, чтобы удалить назначение.', _kb(*rows))


@route('dsad')
def r_design_assign_delete(q: Any, index: str) -> None:
    with config_tx() as tx:
        keys = list(tx['design']['assign'])
        if _int(index) < len(keys):
            tx['design']['assign'].pop(keys[_int(index)], None)
    r_design_assign(q)


@route('dsan')
def r_design_assign_new(q: Any, platform: str = '', kind: str = '') -> None:
    if not platform:
        keys = ['*'] + PLATFORM_ORDER
        rows = [[B('🌍 Все' if k == '*' else f'{PLATFORMS[k]["emoji"]} {PLATFORMS[k]["name"]}', f'dsan:{k}') for k in keys[i:i + 3]]
                for i in range(0, len(keys), 3)]
        rows.append([B('◀️ Назад', 'dsa')])
        _edit(q, '📌 Шаг 1/3. Для какой соцсети?', _kb(*rows))
        return
    if not kind:
        keys = ['*'] + KIND_ORDER
        rows = [[B('🧩 Все типы' if k == '*' else f'{KINDS[k]["emoji"]} {KINDS[k]["ru"].capitalize()}', f'dsan:{platform}:{k}')
                 for k in keys[i:i + 2]] for i in range(0, len(keys), 2)]
        rows.append([B('◀️ Назад', 'dsan')])
        _edit(q, '📌 Шаг 2/3. Для какого типа услуги?', _kb(*rows))
        return
    rows = _template_buttons(f'dsat:{platform}:{kind}', load_config(), include_auto=False)
    rows.append([B('◀️ Назад', f'dsan:{platform}')])
    _edit(q, '📌 Шаг 3/3. Какой шаблон или тему использовать?', _kb(*rows))


@route('dsat')
def r_design_assign_set(q: Any, platform: str, kind: str, token: str) -> None:
    if platform == '*' and kind == '*':
        with config_tx() as tx:
            tx['design']['theme'] = _token_choice(token)
    else:
        with config_tx() as tx:
            tx['design']['assign'][f'{platform}:{kind}'] = _token_choice(token)
    _answer(q, '✅ Назначено.')
    r_design_assign(q)


@route('dsm')
def r_design_sections(q: Any) -> None:
    cfg = load_config()
    sections = cfg['design']['sections']
    rows = [[B(f'{PLATFORMS[k]["emoji"]} {PLATFORMS[k]["name"]}: {sections.get(k) or "—"}', f'dsmp:{k}') for k in PLATFORM_ORDER[i:i + 2]]
            for i in range(0, len(PLATFORM_ORDER), 2)]
    extra = [k for k in sections if ':' in k]
    if extra:
        rows.append([B(f'Особых правил «соцсеть+тип»: {len(extra)}', 'noop')])
    rows.append([B('◀️ Назад', 'ds')])
    _edit(q, '🗺 <b>Карта разделов FunPay</b>\n\nУкажите раздел FunPay для каждой соцсети — тогда автовыгрузка сама разложит '
             'услуги по разделам. ID раздела — число из ссылки <code>funpay.com/lots/ЧИСЛО/</code>.', _kb(*rows))


@route('dsmp')
def r_design_section_set(q: Any, platform: str) -> None:
    _ask(q, 'section_set', f'🗺 Введите ID раздела FunPay для {escape(_platform_info(platform)["name"])} (<code>0</code> — убрать).\n'
                           'Чтобы задать отдельный раздел для типа услуги, введите: <code>ID тип</code>, например <code>1234 views</code>.\n'
                           'Типы: ' + ', '.join(KIND_ORDER), {'platform': platform}, back='dsm')


@inp('section_set')
def i_section_set(message: Any, text: str, data: Dict[str, Any]) -> None:
    parts = text.strip().split()
    node = _int(parts[0], -1) if parts else -1
    kind = parts[1].lower() if len(parts) > 1 else ''
    if node < 0 or (kind and kind not in KINDS):
        raise ValueError('Формат: ID или «ID тип», например 1234 views.')
    key = f'{data["platform"]}:{kind}' if kind else data['platform']
    with config_tx() as tx:
        if node:
            tx['design']['sections'][key] = node
        else:
            tx['design']['sections'].pop(key, None)
    _clear(message)
    _show(message.chat.id, '✅ Сохранено.', _back_kb('dsm', '◀️ К карте разделов'))


@route('dsr')
def r_design_restyle(q: Any) -> None:
    cfg = load_config()
    rows = [[B('Все лоты', 'dsrs:all'), B('Автовыгрузка', 'dsrs:auto'), B('Ручные', 'dsrs:man')]]
    rows += [[B(f'🔌 {p["name"]}', f'dsrs:p_{pid}')] for pid, p in cfg['services'].items()]
    rows.append([B('◀️ Назад', 'ds')])
    _edit(q, '🔄 <b>Переоформить лоты</b>\n\nТексты лотов на FunPay будут собраны заново. Цена, остаток и тег сохраняются.\n\n'
             'Какие лоты переоформить?', _kb(*rows))


@route('dsrs')
def r_design_restyle_scope(q: Any, scope: str) -> None:
    rows = _template_buttons(f'dsrt:{scope}', load_config(), include_keep=True)
    rows.append([B('◀️ Назад', 'dsr')])
    _edit(q, '🎨 Каким оформлением?\n\n«📌 Как у лота» — шаблон, сохранённый у каждого лота (или по настройкам).', _kb(*rows))


@route('dsrt')
def r_design_restyle_go(q: Any, scope: str, token: str) -> None:
    cfg = load_config()
    keys = [k for k, v in _filter_lots(cfg, scope) if v.get('funpay_lot_id')]
    if not keys:
        _answer(q, 'Нет лотов с FunPay ID для переоформления.', True)
        return
    if not _account_ready():
        _answer(q, 'Аккаунт FunPay ещё не загружен.', True)
        return
    _answer(q)
    choice = _token_choice(token)
    def job(progress: Callable) -> str:
        done, errors = _restyle_lots(keys, choice, progress)
        return f'🎨 Переоформлено лотов: {done} из {len(keys)}' + (('\n\nОшибки:\n' + '\n'.join('• ' + escape(x) for x in errors[:15])) if errors else '')
    _progress_job(q.message.chat.id, 'Переоформляю лоты', job, _back_kb('ds', '◀️ Оформление'))


# ---------------------------------------------------------------------------
# Сообщения покупателю
# ---------------------------------------------------------------------------

@route('msg')
def r_messages(q: Any) -> None:
    cfg = load_config()
    parts = ['💬 <b>Сообщения покупателю</b>\n\nFunPay не поддерживает HTML — теги удаляются перед отправкой.\n']
    for key, (label, _) in MESSAGE_INFO.items():
        parts.append(f'<b>{escape(label)}:</b>\n<code>{escape(_short(cfg["messages"][key], 160))}</code>')
    rows = [[B('✏️ ' + MESSAGE_INFO[k][0], f'me:{k}'), B('↩️', f'mr:{k}')] for k in MESSAGE_INFO]
    rows.append([B('◀️ Назад', 'home')])
    _edit(q, '\n\n'.join(parts)[:4000], _kb(*rows))


@route('me')
def r_message_edit(q: Any, key: str) -> None:
    label, variables = MESSAGE_INFO[key]
    current = load_config()['messages'][key]
    _ask(q, 'message_edit', f'✏️ <b>{escape(label)}</b>\n\nТекущий текст:\n<code>{escape(current)}</code>\n\n'
                            f'Переменные: <code>{escape(variables)}</code>\n\nОтправьте новый текст:', {'key': key}, back='msg')


@inp('message_edit')
def i_message_edit(message: Any, text: str, data: Dict[str, Any]) -> None:
    if len(text) > MAX_MESSAGE_LEN:
        raise ValueError(f'Слишком длинный текст: FunPay не примет сообщение длиннее {MAX_MESSAGE_LEN} символов.')
    if not text.strip():
        raise ValueError('Текст не может быть пустым.')
    with config_tx() as tx:
        tx['messages'][data['key']] = text
    _clear(message)
    note = ''
    if data['key'] == 'after_confirmation' and 'чек' not in text.lower():
        note = '\n⚠️ В тексте нет подсказки «чек {panel_order_id}» — покупатели не узнают, как проверить статус.'
    _show(message.chat.id, '✅ Сообщение сохранено.' + escape(note), _back_kb('msg', '◀️ К сообщениям'))


@route('mr')
def r_message_reset(q: Any, key: str) -> None:
    with config_tx() as tx:
        tx['messages'][key] = DEFAULT_MESSAGES[key]
    _answer(q, '↩️ Восстановлен стандартный текст.')
    r_messages(q)


# ---------------------------------------------------------------------------
# Рефилл: общие и массовые настройки
# ---------------------------------------------------------------------------

REFILL_MOMENTS = ['after_start', 'after_complete', 'after_payment', 'never']
REFILL_MOMENT_LABELS = {'after_start': 'после запуска', 'after_complete': 'когда выполнен', 'after_payment': 'сразу после оплаты',
                        'never': 'не отправлять'}


@route('rf')
def r_refill(q: Any) -> None:
    cfg = load_config()
    lots = cfg['lot_mapping'].values()
    orders = _orders()
    active = sum(1 for x in orders if _refill_eligible(x))
    total = sum(_int((x.get('refill') or {}).get('used')) for x in orders)
    moment = cfg['refill']['send_moment']
    text = (f'♻️ <b>Рефилл</b>\n\nЛотов с рефиллом: {sum(1 for x in lots if x["refill"]["enabled"])} из {len(cfg["lot_mapping"])}\n'
            f'Заказов с активным рефиллом: {active} · запущено всего: {total}\n'
            f'Шаблон отправляется: {REFILL_MOMENT_LABELS[moment]}\n'
            f'Автовключение для услуг с гарантией: {"ВКЛ" if cfg["refill"].get("auto_enable") else "ВЫКЛ"}\n\n'
            f'<b>Шаблон:</b>\n<code>{escape(_short(cfg["messages"]["refill_info"], 300))}</code>\n\n'
            'Покупатель запускает рефилл командой !рефилл (или !refill, рефилл, /рефилл).')
    kb = _kb([B(f'💬 Момент: {REFILL_MOMENT_LABELS[moment]}', 'rfm')], [B('✏️ Изменить шаблон', 'me:refill_info')],
             [B(f'{"🔔" if cfg["refill"]["notify"] else "🔕"} Уведомлять меня', 'rfn'),
              B(f'🤖 Автовключение: {"ВКЛ" if cfg["refill"].get("auto_enable") else "ВЫКЛ"}', 'rfa')],
             [B('⚙️ Массовые настройки', 'rfb'), B('📋 Лоты с рефиллом', 'll:ref:0')], [B('◀️ Назад', 'home')])
    _edit(q, text, kb)


@route('rfm', 'rfn', 'rfa')
def r_refill_toggle(q: Any) -> None:
    with config_tx() as tx:
        r = tx['refill']
        if q.data == 'rfm':
            r['send_moment'] = REFILL_MOMENTS[(REFILL_MOMENTS.index(r['send_moment']) + 1) % len(REFILL_MOMENTS)] \
                if r['send_moment'] in REFILL_MOMENTS else 'after_start'
        elif q.data == 'rfn':
            r['notify'] = not r['notify']
        else:
            r['auto_enable'] = not r.get('auto_enable')
    if q.data == 'rfm' and load_config()['refill']['send_moment'] == 'after_payment':
        _answer(q, '⚠️ В момент оплаты ссылки и ID заказа ещё нет — {link} и {order_id} будут пустыми.', True)
    r_refill(q)


REFILL_BULK = {'on': ('✅ Рефилл ВСЕМ', 'enabled', True), 'off': ('❌ Убрать у ВСЕХ', 'enabled', False),
               'limon': ('🔁 Лимит: вкл всем', 'limit_enabled', True), 'limoff': ('🔁 Лимит: выкл всем', 'limit_enabled', False),
               'peron': ('⏳ Срок: вкл всем', 'period_enabled', True), 'peroff': ('⏳ Срок: выкл всем', 'period_enabled', False)}


@route('rfb')
def r_refill_bulk(q: Any) -> None:
    cfg = load_config()
    lots = list(cfg['lot_mapping'].values())
    text = (f'⚙️ <b>Массовые настройки рефилла</b>\n\nРефилл включён: {sum(1 for x in lots if x["refill"]["enabled"])}/{len(lots)}\n'
            f'Лимит включён: {sum(1 for x in lots if x["refill"]["limit_enabled"])}/{len(lots)}\n'
            f'Срок включён: {sum(1 for x in lots if x["refill"]["period_enabled"])}/{len(lots)}')
    kb = _kb([B(REFILL_BULK['on'][0], 'rfbx:on'), B(REFILL_BULK['off'][0], 'rfbx:off')],
             [B(REFILL_BULK['limon'][0], 'rfbx:limon'), B(REFILL_BULK['limoff'][0], 'rfbx:limoff')],
             [B(REFILL_BULK['peron'][0], 'rfbx:peron'), B(REFILL_BULK['peroff'][0], 'rfbx:peroff')],
             [B('✏️ Кол-во запусков всем', 'rfbv:limit'), B('✏️ Срок (часы) всем', 'rfbv:hours')],
             [B('🎯 Выбрать лоты вручную', 'rfs:0')], [B('◀️ Рефилл', 'rf')])
    _edit(q, text, kb)


@route('rfbx')
def r_refill_bulk_confirm(q: Any, action: str) -> None:
    count = len(load_config()['lot_mapping'])
    _edit(q, f'{REFILL_BULK[action][0]}\n\nПрименить к {count} лотам?', _kb([B('✅ Да, применить', f'rfbg:{action}'), B('❌ Отмена', 'rfb')]))


@route('rfbg')
def r_refill_bulk_go(q: Any, action: str) -> None:
    _, field, value = REFILL_BULK[action]
    with config_tx() as tx:
        for lot in tx['lot_mapping'].values():
            lot['refill'][field] = value
    _answer(q, '✅ Применено.')
    r_refill_bulk(q)


@route('rfbv')
def r_refill_bulk_value(q: Any, what: str) -> None:
    _ask(q, 'refill_bulk', '✏️ Введите количество запусков на 1 заказ (минимум 1):' if what == 'limit' else
         '✏️ Введите срок действия рефилла в часах (например 168 = 7 дней):', {'what': what}, back='rfb')


@inp('refill_bulk')
def i_refill_bulk(message: Any, text: str, data: Dict[str, Any]) -> None:
    value = _int(text)
    if value < 1:
        raise ValueError('Нужно целое число не меньше 1.')
    with config_tx() as tx:
        for lot in tx['lot_mapping'].values():
            lot['refill']['limit' if data['what'] == 'limit' else 'hours'] = value
    _clear(message)
    _show(message.chat.id, '✅ Применено ко всем лотам.', _back_kb('rfb', '◀️ К массовым настройкам'))


@route('rfs')
def r_refill_select(q: Any, page: str = '0') -> None:
    cfg = load_config()
    items, page_n, pages = _page(_filter_lots(cfg, 'all'), _int(page), 8)
    rows = [[B(('✅ ' if lot['refill']['enabled'] else '⬜ ') + _short(lot['name'], 44), f'rfst:{key}:{page_n}')] for key, lot in items]
    rows.append(_nav('rfs', page_n, pages))
    rows.append([B('✅ Вкл. всё на стр.', f'rfsp:{page_n}:1'), B('⬜ Выкл. всё на стр.', f'rfsp:{page_n}:0')])
    rows.append([B('🔙 К массовым настройкам', 'rfb')])
    _edit(q, '🎯 <b>Выбор лотов для рефилла</b>\n\nНажмите на лот, чтобы переключить.', _kb(*rows))


@route('rfst')
def r_refill_select_toggle(q: Any, key: str, page: str) -> None:
    with config_tx() as tx:
        if key in tx['lot_mapping']:
            tx['lot_mapping'][key]['refill']['enabled'] = not tx['lot_mapping'][key]['refill']['enabled']
    r_refill_select(q, page)


@route('rfsp')
def r_refill_select_page(q: Any, page: str, value: str) -> None:
    cfg = load_config()
    items, _, _ = _page(_filter_lots(cfg, 'all'), _int(page), 8)
    with config_tx() as tx:
        for key, _ in items:
            if key in tx['lot_mapping']:
                tx['lot_mapping'][key]['refill']['enabled'] = value == '1'
    r_refill_select(q, page)


# ---------------------------------------------------------------------------
# Настройки
# ---------------------------------------------------------------------------

SETTING_TOGGLES = [('auto_refunds', 'Автовозврат'), ('confirm_link', 'Подтверждение ссылки'), ('domain_check', 'Проверка доменов'),
                   ('anti_dumping', 'Антидемпинг'), ('auto_start', 'Автозапуск'), ('notify_new_order', 'Увед. о новом заказе'),
                   ('watcher_enabled', 'Вотчер услуг'), ('respect_blacklist', 'Чёрный список бота'),
                   ('watcher_autoprice', 'Цена за провайдером')]
SETTING_VALUES = {'min_margin': ('Мин. прибыль (₽)', 'float'), 'poll_interval': ('Проверка статусов (сек)', 'int'),
                  'early_complete_minutes': ("Защита от раннего 'выполнено' (мин)", 'int'), 'api_timeout': ('Таймаут API (сек)', 'int'),
                  'remind_hours': ('Напоминание о ссылке (ч, 0 — выкл)', 'int'), 'stale_hours': ('Сообщить мне о заказе без ссылки (ч)', 'int'),
                  'notification_chat_id': ('Чат уведомлений (ID, 0 — админы)', 'int'),
                  'watcher_threshold': ('Вотчер: важное изменение цены от (%)', 'float')}
SETTING_LIMITS = {'watcher_threshold': (0, 1000), 'poll_interval': (20, 3600), 'early_complete_minutes': (0, 1440), 'api_timeout': (5, 180), 'remind_hours': (0, 720),
                  'stale_hours': (0, 720)}


@route('st')
def r_settings(q: Any) -> None:
    cfg = load_config()
    lines = ['⚙️ <b>Настройки</b>', '']
    lines += [f'{_on(cfg[k])} {label}' for k, label in SETTING_TOGGLES]
    lines.append(f'👁 Уведомления вотчера: {WATCH_MODE_LABELS[cfg["watcher_notify"]]}')
    lines.append('<i>«Цена за провайдером» — при изменении цены услуги у провайдера цены лотов с ней пересчитываются сразу.</i>')
    lines.append('')
    lines += [f'• {label}: <code>{escape(str(cfg.get(k) if cfg.get(k) is not None else "—"))}</code>' for k, (label, _) in SETTING_VALUES.items()]
    rows = [[B(f'{_on(cfg[k])} {label}', f'stt:{k}') for k, label in SETTING_TOGGLES[i:i + 2]] for i in range(0, len(SETTING_TOGGLES), 2)]
    rows += [[B('✏️ ' + label, f'ste:{k}')] for k, (label, _) in SETTING_VALUES.items()]
    rows.append([B(f'👁 Уведомления вотчера: {WATCH_MODE_LABELS[cfg["watcher_notify"]]}', 'wnm')])
    rows.append([B(f'⏸ На паузе (/stop): {len(cfg["paused_buyers"])}', 'stp'), B('💾 Бэкап сейчас', 'stb')])
    rows.append([B('◀️ Назад', 'home')])
    _edit(q, '\n'.join(lines), _kb(*rows))


@route('stt')
def r_setting_toggle(q: Any, key: str) -> None:
    if key not in dict(SETTING_TOGGLES):
        return
    with config_tx() as tx:
        tx[key] = not tx[key]
    if key == 'domain_check' and load_config()['domain_check']:
        _answer(q, 'Проверка доменов включена: ссылки должны быть из доменов лота или общего списка «🌐 Домены».', True)
    r_settings(q)


@route('ste')
def r_setting_edit(q: Any, key: str) -> None:
    if key in SETTING_VALUES:
        _ask(q, 'setting_edit', f'✏️ {escape(SETTING_VALUES[key][0])}: введите значение', {'key': key}, back='st')


@inp('setting_edit')
def i_setting_edit(message: Any, text: str, data: Dict[str, Any]) -> None:
    key = data['key']
    _, kind = SETTING_VALUES[key]
    value: Any = _float(text, float('nan')) if kind == 'float' else _int(text, -10 ** 12)
    if (kind == 'float' and (not math.isfinite(value) or value < 0)) or (kind == 'int' and value == -10 ** 12):
        raise ValueError('Введите число.')
    if key in SETTING_LIMITS:
        low, high = SETTING_LIMITS[key]
        if not low <= value <= high:
            raise ValueError(f'Допустимо от {low} до {high}.')
    if key == 'notification_chat_id':
        value = value or None
    with config_tx() as tx:
        tx[key] = value
    _clear(message)
    _show(message.chat.id, '✅ Сохранено.', _back_kb('st', '◀️ К настройкам'))


@route('wnm')
def r_watch_mode(q: Any) -> None:
    with config_tx() as tx:
        tx['watcher_notify'] = WATCH_MODES[(WATCH_MODES.index(tx['watcher_notify']) + 1) % len(WATCH_MODES)]
    r_settings(q)


@route('wnot')
def r_watch_notify(q: Any, mode: str) -> None:
    if mode not in WATCH_MODES:
        return
    with config_tx() as tx:
        tx['watcher_notify'] = mode
    _answer(q, '🔇 Уведомления вотчера выключены. Цены лотов продолжат обновляться сами. Включить: ⚙️ Настройки.'
            if mode == 'off' else '🔕 Теперь только важные изменения (цена от порога, название, лимиты, пропажа услуги).', True)
    try:
        bot.edit_message_reply_markup(q.message.chat.id, q.message.message_id, reply_markup=None)
    except Exception:
        pass


@route('stp')
def r_paused(q: Any) -> None:
    cfg = load_config()
    rows = [[B(f'▶️ Возобновить {item.get("username") or ""} (ID {item.get("id") or "?"})', f'stpr:{index}')]
            for index, item in enumerate(cfg['paused_buyers'][:20])]
    rows.append([B('➕ Поставить на паузу', 'stpa'), B('🔙 Назад', 'st')])
    _edit(q, '⏸ <b>Покупатели на паузе</b>\n\n' + ('Нажмите, чтобы вернуть покупателя в автоматический режим.' if cfg['paused_buyers'] else
          'Список пуст. Чтобы поставить покупателя на паузу, напишите /stop в чате с ним на FunPay, вернуть — /start.'), _kb(*rows))


@route('stpa')
def r_paused_add(q: Any) -> None:
    _ask(q, 'pause_add', '⏸ Введите ник покупателя или его числовой ID. Бот перестанет обрабатывать его сообщения и новые заказы, '
                         'а заказы в работе переведёт в ручной режим.', back='stp')


@inp('pause_add')
def i_pause_add(message: Any, text: str, data: Dict[str, Any]) -> None:
    value = text.strip().lstrip('@')
    if not value:
        raise ValueError('Введите ник или ID.')
    buyer_id, username = (value, '') if value.isdigit() else ('', value)
    known = [x for x in list(waiting.values()) + _orders()
             if (buyer_id and str(x.get('buyer_id')) == buyer_id) or (username and str(x.get('buyer_username') or '').lower() == username.lower())]
    if known:
        buyer_id = buyer_id or str(known[-1].get('buyer_id') or '')
        username = username or str(known[-1].get('buyer_username') or '')
    _clear(message)
    _pause_buyer(cardinal_instance, buyer_id, username, None)
    _show(message.chat.id, f'⏸ Покупатель {escape(username or buyer_id)} на паузе.', _back_kb('stp', '◀️ К списку'))


@route('stpr')
def r_paused_resume(q: Any, index: str) -> None:
    with config_tx() as tx:
        if _int(index) < len(tx['paused_buyers']):
            tx['paused_buyers'].pop(_int(index))
    _answer(q, '▶️ Автообработка возобновлена.')
    r_paused(q)


@route('stb')
def r_backup(q: Any) -> None:
    for path in (CONFIG_PATH, ORDERS_PATH, STATE_PATH):
        _backup(path, force=True)
    _answer(q, f'💾 Резервные копии сохранены в {BACKUP_DIR}', True)


# ---------------------------------------------------------------------------
# Домены
# ---------------------------------------------------------------------------

@route('dm')
def r_domains(q: Any) -> None:
    cfg = load_config()
    domains = _global_domains()
    rows = [[B(d, 'noop'), B('❌', f'dmd:{i}')] for i, d in enumerate(domains[:40])]
    rows.append([B('➕ Добавить', 'dma'), B('↩️ Сбросить', 'dmr')])
    rows.append([B(f'{_on(cfg["domain_check"])} Проверка доменов', 'stt:domain_check'), B('◀️ Назад', 'home')])
    _edit(q, f'🌐 <b>Общий список доменов</b>\n\nПроверка доменов: {"ВКЛ" if cfg["domain_check"] else "ВЫКЛ"}.\n'
             'Используется для лотов без своих доменов. Домен подходит и для поддоменов (vk.com → m.vk.com).', _kb(*rows))


@route('dmd')
def r_domain_delete(q: Any, index: str) -> None:
    domains = _global_domains()
    if _int(index) >= len(domains):
        return
    _edit(q, f'❌ Удалить домен <b>{escape(domains[_int(index)])}</b>?', _kb([B('✅ Да, удалить', f'dmdd:{index}'), B('❌ Отмена', 'dm')]))


@route('dmdd')
def r_domain_delete_do(q: Any, index: str) -> None:
    domains = _global_domains()
    if _int(index) < len(domains):
        domains.pop(_int(index))
        _save_global_domains(domains)
    r_domains(q)


@route('dmr')
def r_domain_reset(q: Any) -> None:
    _save_global_domains(list(DEFAULT_DOMAINS))
    _answer(q, '↩️ Список сброшен к стандартному.')
    r_domains(q)


@route('dma')
def r_domain_add(q: Any) -> None:
    _ask(q, 'domain_add', '➕ Введите домен (например <code>x.com</code>) или ссылку — бот оставит только домен:', back='dm')


@inp('domain_add')
def i_domain_add(message: Any, text: str, data: Dict[str, Any]) -> None:
    new = _domains(text)
    if not new or new == ['*']:
        raise ValueError('Не удалось распознать домен.')
    domains = _global_domains()
    added = [d for d in new if d not in domains]
    if not added:
        raise ValueError('Такой домен уже есть.')
    _save_global_domains(domains + added)
    _clear(message)
    _show(message.chat.id, '✅ Добавлено: ' + escape(', '.join(added)), _back_kb('dm', '◀️ К доменам'))


# ---------------------------------------------------------------------------
# Статистика
# ---------------------------------------------------------------------------

@route('stats')
def r_stats(q: Any, period: str = 'today') -> None:
    days = {'today': 1, 'week': 7, 'month': 30, 'all': None}.get(period)
    title = {'today': 'Сегодня', 'week': '7 дней', 'month': '30 дней', 'all': 'Всё время'}.get(period, 'Всё время')
    s = _stats(days)
    text = (f'📊 <b>Статистика — {title}</b>\n\n🧾 Заказов: {s["orders"]} · ✅ выполнено: {s["done"]} · ⚙️ в работе: {s["active"]}\n'
            f'↩️ Возвратов: {s["refunded"]}\n\n💵 Выручка: <b>{s["revenue"]:.2f} ₽</b>\n💸 Себестоимость: {s["cost"]:.2f} ₽\n'
            f'✅ Чистая прибыль: <b>{s["profit"]:.2f} ₽</b>\n\nВозвраты исключены из выручки. Себестоимость — по данным панели '
            '(с пересчётом валюты), для частичных заказов — фактическое списание.')
    kb = _kb([B('Сегодня', 'stats:today'), B('7 дней', 'stats:week'), B('30 дней', 'stats:month'), B('Всё время', 'stats:all')],
             [B('◀️ Назад', 'home')])
    _edit(q, text, kb)
