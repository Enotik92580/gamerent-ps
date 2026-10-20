

# ---------------------------------------------------------------------------
# Новый лот: привязка существующего и ручное создание
# ---------------------------------------------------------------------------

def _wizard(user_id: int, kind: str, reset: bool = False) -> Dict[str, Any]:
    wizard = _wizards.get(user_id)
    if reset or not wizard or wizard.get('kind') != kind:
        wizard = {'kind': kind, 'created': time.time()}
        _wizards[user_id] = wizard
    return wizard


def _panel_rows(prefix: str, back: str) -> List[List[_TelegramButton]]:
    cfg = load_config()
    rows = [[B('🔌 ' + p['name'], f'{prefix}:{pid}')] for pid, p in cfg['services'].items() if p['enabled']]
    rows.append([B('◀️ Назад', back)])
    return rows


def _service_card(service: Dict[str, Any], panel_id: str, quantity: int = 0) -> str:
    profile = _service_profile(service)
    pinfo, kinfo = _platform_info(profile['platform']), KINDS[profile['kind']]
    lines = [f'🧾 <b>{escape(_short(str(service.get("name")), 120))}</b>',
             f'ID: <code>{escape(str(service.get("service")))}</code> · категория: {escape(_short(str(service.get("category") or "—"), 60))}',
             f'Цена: {escape(str(service.get("rate")))} за 1000 · мин {escape(str(service.get("min")))} · макс {escape(str(service.get("max")))}',
             f'Тип: {escape(str(service.get("type", "Default")))} · рефилл: {"да" if service.get("refill") in (True, 1, "1", "true") else "нет"}',
             f'Распознано: {pinfo["emoji"]} {escape(pinfo["name"])} · {kinfo["emoji"]} {escape(kinfo["ru"].lower())}']
    if quantity:
        try:
            cfg = load_config()
            cost, _, currency = _cost_rub(cfg, panel_id, service, quantity)
            lines.append(f'Себестоимость {quantity} ед.: <b>{_money(cost)} ₽</b>' + (f' (валюта панели {currency})' if currency != 'RUB' else ''))
        except PanelError as exc:
            lines.append(f'⚠️ {escape(str(exc))}')
    return '\n'.join(lines)


def _search_services(panel_id: str, text: str) -> List[Dict[str, Any]]:
    services = PanelAPI(load_config(), panel_id).services()
    text = text.strip().lower()
    if text.isdigit():
        exact = [x for x in services if str(x.get('service')) == text]
        if exact:
            return exact
    words = [w for w in re.split(r'\s+', text) if w]
    return [x for x in services if all(w in (str(x.get('name', '')) + ' ' + str(x.get('category', ''))).lower() for w in words)]


@route('new')
def r_new(q: Any) -> None:
    if CAN_CREATE_LOTS:
        _edit(q, '➕ <b>Новый лот</b>\n\n'
                 '🔗 <b>Привязать</b> — у вас уже есть лот на FunPay: бот свяжет его с услугой панели и запишет тег.\n'
                 '🛠 <b>Создать</b> — бот сам создаст красиво оформленный лот на FunPay из услуги панели.',
              _kb([B('🔗 Привязать лот FunPay по ID', 'nb')], [B('🛠 Создать лот из услуги панели', 'nc')],
                  [B('📋 Только текст для лота', 'ntx')], [B('◀️ Назад', 'home')]))
        return
    _edit(q, '➕ <b>Новый лот</b>\n\n'
             f'{MARKET} не даёт создавать лоты через API, поэтому так:\n'
             '1️⃣ <b>📋 Текст для лота</b> — бот подготовит красивое название и описание под услугу панели.\n'
             f'2️⃣ Создайте лот на {MARKET} вручную и вставьте этот текст.\n'
             '3️⃣ <b>🔗 Привязать</b> — введите ID лота, и бот начнёт выполнять по нему заказы, менять цену и следить за балансом.',
          _kb([B('📋 Текст для лота', 'ntx')], [B(f'🔗 Привязать лот {MARKET} по ID', 'nb')], [B('◀️ Назад', 'home')]))


@route('nb')
def r_new_bind(q: Any) -> None:
    if not load_config()['services']:
        _answer(q, 'Сначала добавьте SMM-панель в разделе «🔌 Панели».', True)
        return
    _wizard(q.from_user.id, 'bind', reset=True)
    _ask(q, 'bind_lot', f'🔗 Введите ID лота FunPay — {LOT_ID_HINT}:', back='new')


@inp('bind_lot')
def i_bind_lot(message: Any, text: str, data: Dict[str, Any]) -> None:
    lot_id = _int(text)
    if lot_id <= 0:
        raise ValueError('Введите числовой ID лота FunPay.')
    if not _account_ready():
        raise ValueError('Аккаунт FunPay ещё не загрузился после запуска бота. Подождите минуту и повторите.')
    existing = _lot_key_by_funpay_id(load_config(), lot_id)
    if existing:
        _clear(message)
        _show(message.chat.id, f'ℹ️ Этот лот уже привязан: <code>{existing}</code>.')
        _show(message.chat.id, *scr_lot(existing))
        return
    _clear(message)
    user_id, chat_id = message.from_user.id, message.chat.id
    _show(chat_id, '⏳ Ищу лот на FunPay…')

    def job() -> None:
        try:
            fields = _lot_fields(lot_id)
        except Exception as exc:
            _show(chat_id, f'❌ Лот {lot_id} не найден на вашем аккаунте ({escape(type(exc).__name__)}). Проверьте ID.',
                  _kb([B('🔁 Ввести другой ID', 'nb'), B('◀️ Назад', 'new')]))
            return
        wizard = _wizard(user_id, 'bind')
        wizard.update(lot_id=lot_id, name=fields.fields.get('fields[summary][ru]') or fields.fields.get('fields[name][ru]') or f'Лот #{lot_id}',
                      node=getattr(getattr(fields, 'subcategory', None), 'id', None) or _int(fields.fields.get('node_id')),
                      price=_float(getattr(fields, 'price', None) or fields.fields.get('price')))
        _show(chat_id, f'✅ Лот найден: <b>{escape(wizard["name"])}</b>\nЦена на FunPay: {_money(wizard["price"])} ₽\n\nВыберите SMM-панель:',
              _kb(*_panel_rows('nbp', 'new')))
    _submit(job)


@route('nbp')
def r_bind_panel(q: Any, panel_id: str) -> None:
    wizard = _wizard(q.from_user.id, 'bind')
    if not wizard.get('lot_id'):
        _answer(q, 'Мастер устарел — начните заново.', True)
        return
    wizard['panel'] = panel_id
    _ask(q, 'bind_service', '🆔 Введите ID услуги в панели или часть названия для поиска (например «telegram подписчики»):', back='new')


def _service_pick(message: Any, text: str, mode: str) -> None:
    """Общий шаг выбора услуги для привязки и создания."""
    wizard = _wizard(message.from_user.id, mode)
    if not wizard.get('panel'):
        raise ValueError('Мастер устарел — начните заново.')
    found = _search_services(wizard['panel'], text)
    if not found:
        raise ValueError('Услуга не найдена. Введите точный ID или другие слова для поиска.')
    _clear(message)
    if len(found) == 1:
        _service_chosen(message.chat.id, message.from_user.id, mode, str(found[0]['service']))
        return
    prefix = {'bind': 'nbs', 'create': 'ncs', 'text': 'nts'}[mode]
    again = {'bind': 'nbp', 'create': 'ncp', 'text': 'ntp'}[mode]
    rows = [[B(f'{x.get("service")} · {_short(str(x.get("name")), 40)} · {x.get("rate")}', f'{prefix}:{x.get("service")}')]
            for x in found[:12]]
    rows.append([B('🔍 Искать заново', f'{again}:{wizard["panel"]}')])
    _show(message.chat.id, f'🔍 Найдено услуг: {len(found)}' + (' (первые 12)' if len(found) > 12 else '') + '. Выберите:', _kb(*rows))


@inp('bind_service')
def i_bind_service(message: Any, text: str, data: Dict[str, Any]) -> None:
    _service_pick(message, text, 'bind')


@route('nbs', 'ncs', 'nts')
def r_service_chosen(q: Any, service_id: str) -> None:
    _answer(q)
    mode = {'nbs': 'bind', 'ncs': 'create', 'nts': 'text'}[q.data.split(':')[0]]
    _service_chosen(q.message.chat.id, q.from_user.id, mode, service_id)


def _qty_rows(prefix: str, service: Dict[str, Any]) -> List[List[_TelegramButton]]:
    smin, smax = max(1, _int(service.get('min'), 1)), _int(service.get('max'))
    values = sorted({1, smin, *(x for x in (100, 1000) if not smax or x <= smax)})[:4]
    return [[B(('1 шт. = 1 ед.' if v == 1 else f'Пакет {v}'), f'{prefix}:{v}') for v in values]]


def _service_chosen(chat_id: int, user_id: int, mode: str, service_id: str) -> None:
    wizard = _wizard(user_id, mode)
    try:
        service = PanelAPI(load_config(), wizard['panel']).service(service_id)
    except (PanelError, KeyError) as exc:
        _show(chat_id, f'❌ {escape(str(exc))}', _back_kb('new'))
        return
    if str(service.get('type', 'Default')).strip().lower() not in ('default', 'poll'):
        _show(chat_id, f'❌ Тип услуги «{escape(str(service.get("type")))}» не поддерживается. Нужны Default или Poll.', _back_kb('new'))
        return
    wizard['service'] = service
    wizard['service_id'] = str(service['service'])
    profile = _service_profile(service)
    wizard['profile'] = profile
    if mode == 'text':
        sent = _show(chat_id, _service_card(service, wizard['panel']) + '\n\n📦 Сколько единиц услуги в 1 шт. лота? Обычно 1.',
                     _kb(*_qty_rows('ntq', service), [B('❌ Отмена', 'x:new')]))
        cardinal_instance.telegram.set_state(chat_id, getattr(sent, 'message_id', 0), user_id, 'autosmm:text_qty', {})
        return
    if mode == 'create':
        cfg = load_config()
        node = cfg['design']['sections'].get(f'{profile["platform"]}:{profile["kind"]}') or cfg['design']['sections'].get(profile['platform'])
        rows = []
        if node:
            rows.append([B(f'🗺 Раздел из карты: {node}', f'ncn:{node}')])
        rows.append([B('❌ Отмена', 'x:new')])
        sent = _show(chat_id, _service_card(service, wizard['panel']) + '\n\n🗂 Введите ID раздела FunPay (число из ссылки '
                     '<code>funpay.com/lots/ЧИСЛО/</code>)' + (' или нажмите кнопку с разделом из карты.' if node else ':'), _kb(*rows))
        cardinal_instance.telegram.set_state(chat_id, getattr(sent, 'message_id', 0), user_id, 'autosmm:create_node', {})
        return
    sent = _show(chat_id, _service_card(service, wizard['panel']) + '\n\n📦 Сколько единиц услуги заказывать за 1 шт. лота? '
                 'Обычно 1 (1 шт. = 1 подписчик). Введите число или выберите:', _kb(*_qty_rows('nbq', service), [B('❌ Отмена', 'x:new')]))
    cardinal_instance.telegram.set_state(chat_id, getattr(sent, 'message_id', 0), user_id, 'autosmm:bind_qty', {})


@inp('bind_qty')
def i_bind_qty(message: Any, text: str, data: Dict[str, Any]) -> None:
    if _int(text) <= 0:
        raise ValueError('Введите число больше нуля.')
    _clear(message)
    _bind_finish(message.chat.id, message.from_user.id, _int(text))


@route('nbq')
def r_bind_qty(q: Any, value: str) -> None:
    _answer(q)
    try:
        cardinal_instance.telegram.clear_state(q.message.chat.id, q.from_user.id, False)
    except Exception:
        pass
    _bind_finish(q.message.chat.id, q.from_user.id, _int(value))


def _bind_finish(chat_id: int, user_id: int, quantity: int) -> None:
    wizard = _wizard(user_id, 'bind')
    if not wizard.get('service'):
        _show(chat_id, '⌛ Мастер устарел — начните заново.', _back_kb('new'))
        return
    service, profile = wizard['service'], wizard['profile']
    warn = ''
    if quantity < max(1, _int(service.get('min'), 1)):
        warn = f'\n⚠️ Количество меньше минимума услуги ({service.get("min")}): покупатель должен брать от {math.ceil(_int(service.get("min")) / quantity)} шт.'
    key = _register_lot(wizard['name'], wizard['lot_id'], wizard['panel'], wizard['service_id'], quantity,
                        list(_platform_info(profile['platform']).get('domains') or []), source='bind', subcategory=wizard.get('node'),
                        platform=profile['platform'], kind=profile['kind'], poll='poll' in str(service.get('type', '')).lower())
    _wizards.pop(user_id, None)
    _show(chat_id, f'✅ Лот привязан: <code>{key}</code>{escape(warn)}\n⏳ Записываю тег на FunPay…')

    def job() -> None:
        ok, message = _write_tag(key)
        cfg = load_config()
        lot = cfg['lot_mapping'].get(key)
        extra = ''
        try:
            info = _lot_price_info(cfg, lot)
            if wizard.get('price') and wizard['price'] < info['cost_rub']:
                extra = f'\n\n🛑 Цена лота {_money(wizard["price"])} ₽ НИЖЕ себестоимости {_money(info["cost_rub"])} ₽!'
        except PanelError:
            pass
        _show(chat_id, ('🏷 ' if ok else '⚠️ ') + escape(message) + extra)
        _show(chat_id, *scr_lot(key))
    _submit(job)


@route('nc')
def r_new_create(q: Any) -> None:
    if not CAN_CREATE_LOTS:
        _answer(q, f'{MARKET} не даёт создавать лоты через API. Используйте «📋 Текст для лота» и привязку.', True)
        return
    if not load_config()['services']:
        _answer(q, 'Сначала добавьте SMM-панель в разделе «🔌 Панели».', True)
        return
    _wizard(q.from_user.id, 'create', reset=True)
    _edit(q, '🛠 <b>Создание лота</b>\n\nВыберите SMM-панель:', _kb(*_panel_rows('ncp', 'new')))


@route('ncp')
def r_create_panel(q: Any, panel_id: str) -> None:
    wizard = _wizard(q.from_user.id, 'create')
    wizard['panel'] = panel_id
    _ask(q, 'create_service', '🆔 Введите ID услуги или часть названия для поиска (например «tiktok просмотры»):', back='new')


@inp('create_service')
def i_create_service(message: Any, text: str, data: Dict[str, Any]) -> None:
    _service_pick(message, text, 'create')


@inp('create_node')
def i_create_node(message: Any, text: str, data: Dict[str, Any]) -> None:
    if _int(text) <= 0:
        raise ValueError('ID раздела — число больше нуля.')
    _clear(message)
    _create_node_set(message.chat.id, message.from_user.id, _int(text))


@route('ncn')
def r_create_node(q: Any, node: str) -> None:
    _answer(q)
    try:
        cardinal_instance.telegram.clear_state(q.message.chat.id, q.from_user.id, False)
    except Exception:
        pass
    _create_node_set(q.message.chat.id, q.from_user.id, _int(node))


def _create_node_set(chat_id: int, user_id: int, node: int) -> None:
    wizard = _wizard(user_id, 'create')
    if not wizard.get('service'):
        _show(chat_id, '⌛ Мастер устарел — начните заново.', _back_kb('new'))
        return
    wizard['node'] = node
    service = wizard['service']
    sent = _show(chat_id, f'🗂 Раздел: {node}\n\n📦 Сколько единиц услуги в 1 шт. лота? Обычно 1 — тогда покупатель просто берёт нужное '
                 f'количество (минимум услуги: {service.get("min")}).',
                 _kb(*_qty_rows('ncq', service), [B('❌ Отмена', 'x:new')]))
    cardinal_instance.telegram.set_state(chat_id, getattr(sent, 'message_id', 0), user_id, 'autosmm:create_qty', {})


@inp('create_qty')
def i_create_qty(message: Any, text: str, data: Dict[str, Any]) -> None:
    if _int(text) <= 0:
        raise ValueError('Введите число больше нуля.')
    _clear(message)
    _create_qty_set(message.chat.id, message.from_user.id, _int(text))


@route('ncq')
def r_create_qty(q: Any, value: str) -> None:
    _answer(q)
    try:
        cardinal_instance.telegram.clear_state(q.message.chat.id, q.from_user.id, False)
    except Exception:
        pass
    _create_qty_set(q.message.chat.id, q.from_user.id, _int(value))


def _create_qty_set(chat_id: int, user_id: int, quantity: int) -> None:
    wizard = _wizard(user_id, 'create')
    service = wizard.get('service')
    if not service:
        _show(chat_id, '⌛ Мастер устарел — начните заново.', _back_kb('new'))
        return
    smin, smax = max(1, _int(service.get('min'), 1)), _int(service.get('max'))
    if smax and quantity > smax:
        _show(chat_id, f'❌ Количество больше максимума услуги ({smax}). Введите меньше.', _back_kb('new'))
        return
    wizard['qty'] = quantity
    cfg = load_config()
    mode, value = _effective_markup(cfg, None)
    note = '' if quantity >= smin else f'\nℹ️ Покупатель сможет заказать от {math.ceil(smin / quantity)} шт. — это будет указано в лоте.'
    sent = _show(chat_id, f'📦 За 1 шт.: {quantity}{escape(note)}\n\n📈 Наценка: выберите глобальную ({_markup_text(mode, value)}) '
                          'или введите свой процент:', _kb([B(f'🌍 Глобальная ({_markup_text(mode, value)})', 'ncm:g')], [B('❌ Отмена', 'x:new')]))
    cardinal_instance.telegram.set_state(chat_id, getattr(sent, 'message_id', 0), user_id, 'autosmm:create_markup', {})


@inp('create_markup')
def i_create_markup(message: Any, text: str, data: Dict[str, Any]) -> None:
    wizard = _wizard(message.from_user.id, 'create')
    wizard['markup'] = ('percent', _markup(text))
    _clear(message)
    _create_style(message.chat.id)


@route('ncm')
def r_create_markup(q: Any, value: str) -> None:
    wizard = _wizard(q.from_user.id, 'create')
    wizard['markup'] = None
    try:
        cardinal_instance.telegram.clear_state(q.message.chat.id, q.from_user.id, False)
    except Exception:
        pass
    _answer(q)
    _create_style(q.message.chat.id)


def _create_style(chat_id: int) -> None:
    cfg = load_config()
    rows = _template_buttons('nct', cfg)
    rows.append([B('❌ Отмена', 'new')])
    _show(chat_id, '🎨 Выберите оформление лота (можно потом поменять):', _kb(*rows))


@route('nct')
def r_create_style(q: Any, token: str) -> None:
    wizard = _wizard(q.from_user.id, 'create')
    if not wizard.get('qty'):
        _answer(q, 'Мастер устарел — начните заново.', True)
        return
    wizard['choice'] = _token_choice(token)
    _answer(q, '⏳ Готовлю предпросмотр…')
    _submit(_create_preview, q.message.chat.id, q.from_user.id)


def _create_preview(chat_id: int, user_id: int) -> None:
    wizard = _wizard(user_id, 'create')
    cfg = load_config()
    try:
        service = wizard['service']
        cost, _, _ = _cost_rub(cfg, wizard['panel'], service, wizard['qty'])
        mode, value = wizard.get('markup') or _effective_markup(cfg, None)
        price = wizard.get('price_override') or _price_from_cost(cost, mode, value)
        if 'form' not in wizard:
            wizard['form'] = _node_form(wizard['node'])
        limits = _form_limits(wizard['form'], cfg)
        texts = render_lot_texts(service, wizard['qty'], price, wizard['choice'], limits, cfg, 'xxxxxxxx')
        if wizard.get('name_override'):
            texts['summary_ru'] = _summary_field(wizard['name_override'], limits['summary_ru'])
    except Exception as exc:
        _show(chat_id, f'❌ {escape(str(exc) if isinstance(exc, (PanelError, RuntimeError)) else type(exc).__name__)}', _back_kb('new'))
        return
    wizard['price'] = price
    wizard['cost'] = cost
    desc = texts['desc_ru'].replace('#SMM:xxxxxxxx', '#SMM:…')
    text = (f'👁 <b>Предпросмотр лота</b>\n\n<b>Название:</b>\n{escape(texts["summary_ru"])}\n\n'
            f'<b>Описание:</b>\n<blockquote expandable>{escape(desc[:2500])}</blockquote>\n\n'
            f'💵 Цена: <b>{_money(price)} ₽</b> за 1 шт. (себестоимость {_money(cost)} ₽)\n'
            f'🗂 Раздел: {wizard["node"]} · 📦 За 1 шт.: {wizard["qty"]}\n'
            f'🎨 {escape(_template_name(cfg, wizard["choice"]))}')
    _show(chat_id, text, _kb([B('🚀 Опубликовать', 'ncgo')],
                             [B('✏️ Название', 'nce:name'), B('💵 Цена', 'nce:price'), B('🎨 Стиль', 'ncst')],
                             [B('🧩 Поля раздела', 'fl:create')] if wizard['form'].get('selects') else None,
                             [B('❌ Отмена', 'new')]))


@route('nce')
def r_create_edit(q: Any, field: str) -> None:
    prompt = '✏️ Введите своё название лота (краткое описание):' if field == 'name' else '💵 Введите цену за 1 шт. в рублях:'
    _ask(q, 'create_edit', prompt, {'field': field}, back='new')


@inp('create_edit')
def i_create_edit(message: Any, text: str, data: Dict[str, Any]) -> None:
    wizard = _wizard(message.from_user.id, 'create')
    if data['field'] == 'name':
        if not text.strip():
            raise ValueError('Название не может быть пустым.')
        wizard['name_override'] = text.strip()
    else:
        price = _float(text, -1)
        if price <= 0:
            raise ValueError('Цена должна быть больше нуля.')
        if wizard.get('cost') and price < wizard['cost']:
            _show(message.chat.id, f'⚠️ Цена ниже себестоимости ({_money(wizard["cost"])} ₽)!')
        wizard['price_override'] = price
    _clear(message)
    _submit(_create_preview, message.chat.id, message.from_user.id)


@route('ncst')
def r_create_restyle(q: Any) -> None:
    _answer(q)
    _create_style(q.message.chat.id)


@route('ncgo')
def r_create_go(q: Any) -> None:
    wizard = _wizard(q.from_user.id, 'create')
    if not wizard.get('price') or wizard.get('running'):
        _answer(q, 'Мастер устарел или уже публикуется.', True)
        return
    if not _account_ready():
        _answer(q, 'Аккаунт FunPay ещё не загружен.', True)
        return
    wizard['running'] = True
    _answer(q, '⏳ Публикую лот…')
    chat_id, user_id = q.message.chat.id, q.from_user.id

    def job() -> None:
        cfg = load_config()
        service, profile = wizard['service'], wizard['profile']
        tag = _new_tag(x.get('tag') for x in cfg['lot_mapping'].values())
        try:
            lot_id, name, warnings = _create_lot(wizard['node'], wizard['panel'], service, wizard['qty'], wizard['price'],
                                                 wizard['choice'], tag, form=wizard.get('form'),
                                                 select_overrides=wizard.get('selects'),
                                                 summary_override=wizard.get('name_override', ''))
        except Exception as exc:
            wizard['running'] = False
            _show(chat_id, '❌ ' + escape(str(exc) if isinstance(exc, (PanelError, LotPublishError, RuntimeError)) else type(exc).__name__),
                  _kb([B('🔁 Повторить', 'ncgo'), B('◀️ Назад', 'new')]))
            return
        refill = None
        if cfg['refill'].get('auto_enable') and (service.get('refill') in (True, 1, '1', 'true') or profile['guarantee_days']):
            refill = {'enabled': True, 'hours': (profile['guarantee_days'] or 30) * 24}
        key = _register_lot(name, lot_id, wizard['panel'], wizard['service_id'], wizard['qty'],
                            list(_platform_info(profile['platform']).get('domains') or []), source='manual', subcategory=wizard['node'],
                            tag=tag, platform=profile['platform'], kind=profile['kind'],
                            template='' if wizard['choice'] == 'auto' else wizard['choice'], price=wizard['price'],
                            markup=wizard.get('markup'), poll='poll' in str(service.get('type', '')).lower(),
                            tag_written=bool(lot_id), refill=refill)
        _wizards.pop(user_id, None)
        _show(chat_id, f'✅ Лот опубликован на FunPay и привязан: <code>{key}</code>' +
              ('\n\n⚠️ ' + '\n⚠️ '.join(escape(x) for x in warnings) if warnings else ''))
        _show(chat_id, *scr_lot(key))
    _submit(job)


# ---------------------------------------------------------------------------
# Автовыгрузка
# ---------------------------------------------------------------------------

@route('auto')
def r_auto(q: Any) -> None:
    if not CAN_CREATE_LOTS:
        _answer(q, f'{MARKET} не даёт создавать лоты через API — автовыгрузка недоступна.', True)
        return
    if not load_config()['services']:
        _answer(q, 'Сначала добавьте SMM-панель.', True)
        return
    _edit(q, '📦 <b>Автовыгрузка</b>\n\nБот создаст на FunPay оформленные лоты сразу для многих услуг панели.\n\nВыберите панель:',
          _kb(*_panel_rows('ap', 'home')))


@route('ap')
def r_auto_panel(q: Any, panel_id: str) -> None:
    _answer(q, '⏳ Загружаю услуги панели…')
    chat_id, user_id = q.message.chat.id, q.from_user.id

    def job() -> None:
        try:
            services = PanelAPI(load_config(), panel_id).services(refresh=True)
        except PanelError as exc:
            _show(chat_id, f'❌ {escape(str(exc))}', _back_kb('auto'))
            return
        services = [x for x in services if str(x.get('type', 'Default')).strip().lower() in ('default', 'poll')]
        if not services:
            _show(chat_id, '❌ В панели нет поддерживаемых услуг (Default / Poll).', _back_kb('auto'))
            return
        for item in services:
            item['_profile'] = _service_profile(item)
        wizard = _wizard(user_id, 'auto', reset=True)
        wizard.update(panel=panel_id, services=services, selected=set(), filter='')
        _show(chat_id, f'📦 Загружено услуг: {len(services)}.\n\nКак группировать?',
              _kb([B('📂 По категориям панели', 'am:cat'), B('🌐 По соцсетям', 'am:plat')], [B('◀️ Назад', 'auto')]))
    _submit(job)


def _auto_groups(wizard: Dict[str, Any]) -> List[Tuple[str, str]]:
    if wizard.get('group_mode') == 'plat':
        keys = sorted({x['_profile']['platform'] for x in wizard['services']},
                      key=lambda k: PLATFORM_ORDER.index(k) if k in PLATFORM_ORDER else 99)
        return [(k, f'{_platform_info(k)["emoji"]} {_platform_info(k)["name"]}') for k in keys]
    cats = sorted({str(x.get('category') or 'Без категории') for x in wizard['services']})
    return [(c, c) for c in cats]


def _group_services(wizard: Dict[str, Any], group: str) -> List[Dict[str, Any]]:
    if wizard.get('group_mode') == 'plat':
        return [x for x in wizard['services'] if x['_profile']['platform'] == group]
    return [x for x in wizard['services'] if str(x.get('category') or 'Без категории') == group]


@route('am')
def r_auto_mode(q: Any, mode: str) -> None:
    wizard = _wizards.get(q.from_user.id)
    if not wizard or wizard.get('kind') != 'auto':
        _answer(q, 'Мастер устарел — начните заново.', True)
        return
    wizard['group_mode'] = mode
    r_auto_groups(q, '0')


@route('ag')
def r_auto_groups(q: Any, page: str = '0') -> None:
    wizard = _wizards.get(q.from_user.id)
    if not wizard or wizard.get('kind') != 'auto':
        _answer(q, 'Мастер устарел — начните заново.', True)
        return
    groups = _auto_groups(wizard)
    wizard['groups'] = groups
    items, page_n, pages = _page(list(enumerate(groups)), _int(page), 8)
    rows = []
    for index, (key, label) in items:
        services = _group_services(wizard, key)
        chosen = sum(1 for x in services if str(x['service']) in wizard['selected'])
        rows.append([B(f'{"✅ " if chosen else ""}{_short(label, 40)} ({chosen}/{len(services)})', f'ags:{index}:0')])
    rows.append(_nav('ag', page_n, pages))
    rows.append([B(f'➡️ Далее (выбрано {len(wizard["selected"])})', 'anext')] if wizard['selected'] else None)
    rows.append([B('◀️ Назад', 'auto')])
    _edit(q, f'📦 <b>Автовыгрузка</b> — выберите группу услуг\n\nВыбрано услуг: <b>{len(wizard["selected"])}</b>. '
             'Можно отмечать услуги в разных группах.', _kb(*rows))


@route('ags')
def r_auto_group(q: Any, index: str, page: str = '0') -> None:
    wizard = _wizards.get(q.from_user.id)
    if not wizard or wizard.get('kind') != 'auto' or _int(index) >= len(wizard.get('groups', [])):
        _answer(q, 'Мастер устарел — начните заново.', True)
        return
    key, label = wizard['groups'][_int(index)]
    services = _group_services(wizard, key)
    if wizard.get('filter'):
        words = wizard['filter'].lower().split()
        services = [x for x in services if all(w in str(x.get('name', '')).lower() for w in words)]
    items, page_n, pages = _page(services, _int(page), 8)
    rows = []
    for item in items:
        mark = '✅' if str(item['service']) in wizard['selected'] else '⬜'
        rows.append([B(f'{mark} {item["service"]} · {_short(str(item.get("name")), 34)} · {item.get("rate")}',
                       f'at:{index}:{page_n}:{item["service"]}')])
    rows.append(_nav(f'ags:{index}', page_n, pages))
    rows.append([B('✅ Все в группе', f'aall:{index}:1'), B('⬜ Снять все', f'aall:{index}:0')])
    rows.append([B('🔍 Фильтр' + (f': {_short(wizard["filter"], 12)}' if wizard.get('filter') else ''), f'afilt:{index}'),
                 B('◀️ Группы', 'ag:0')])
    rows.append([B(f'➡️ Далее (выбрано {len(wizard["selected"])})', 'anext')] if wizard['selected'] else None)
    _edit(q, f'📦 <b>{escape(_short(label, 60))}</b>\nУслуг: {len(services)} · выбрано всего: {len(wizard["selected"])}\n\n'
             'Нажимайте на услуги, чтобы отметить. Формат: ID · название · цена за 1000.', _kb(*rows))


@route('at')
def r_auto_toggle(q: Any, index: str, page: str, service_id: str) -> None:
    wizard = _wizards.get(q.from_user.id)
    if not wizard or wizard.get('kind') != 'auto':
        _answer(q, 'Мастер устарел.', True)
        return
    wizard['selected'].symmetric_difference_update({service_id})
    r_auto_group(q, index, page)


@route('aall')
def r_auto_all(q: Any, index: str, value: str) -> None:
    wizard = _wizards.get(q.from_user.id)
    if not wizard or wizard.get('kind') != 'auto':
        return
    key, _ = wizard['groups'][_int(index)]
    ids = {str(x['service']) for x in _group_services(wizard, key)}
    if value == '1':
        wizard['selected'] |= ids
    else:
        wizard['selected'] -= ids
    r_auto_group(q, index, '0')


@route('afilt')
def r_auto_filter(q: Any, index: str) -> None:
    _ask(q, 'auto_filter', '🔍 Введите слова для фильтра по названию услуги (<code>-</code> — сбросить фильтр):',
         {'index': index}, back=f'ags:{index}:0')


@inp('auto_filter')
def i_auto_filter(message: Any, text: str, data: Dict[str, Any]) -> None:
    wizard = _wizards.get(message.from_user.id)
    if not wizard:
        raise ValueError('Мастер устарел.')
    wizard['filter'] = '' if text.strip() == '-' else text.strip()
    _clear(message)
    _show(message.chat.id, '✅ Фильтр применён.', _kb([B('📋 Открыть группу', f'ags:{data["index"]}:0')]))


@route('anext')
def r_auto_next(q: Any) -> None:
    wizard = _wizards.get(q.from_user.id)
    if not wizard or not wizard.get('selected'):
        _answer(q, 'Сначала отметьте услуги.', True)
        return
    _ask(q, 'auto_qty', f'📦 Выбрано услуг: {len(wizard["selected"])}.\n\nСколько единиц услуги выдавать за 1 шт. лота?\n'
                        '• <b>1</b> — обычная продажа: 1 шт. на FunPay = 1 подписчик/просмотр/лайк. Если у услуги есть минимум, '
                        'в лоте будет написано «от N шт.».\n• Больше 1 — лот продаётся пакетами.', back='ag:0')
    try:
        bot.edit_message_reply_markup(q.message.chat.id, q.message.message_id,
                                      reply_markup=_kb([B('1 шт. = 1 ед. (рекомендуется)', 'aq:1')],
                                                       [B('📏 Пакет = минимум услуги', 'aq:min')], [B('❌ Отмена', 'x:ag:0')]))
    except Exception:
        pass


@inp('auto_qty')
def i_auto_qty(message: Any, text: str, data: Dict[str, Any]) -> None:
    if _int(text) <= 0:
        raise ValueError('Введите число больше нуля.')
    wizard = _wizards.get(message.from_user.id)
    if not wizard:
        raise ValueError('Мастер устарел.')
    wizard['qty'] = _int(text)
    wizard.setdefault('adapt', False)
    _clear(message)
    _show(message.chat.id, *scr_auto_section(wizard))


@route('aq')
def r_auto_qty(q: Any, value: str) -> None:
    wizard = _wizards.get(q.from_user.id)
    if not wizard:
        return
    try:
        cardinal_instance.telegram.clear_state(q.message.chat.id, q.from_user.id, False)
    except Exception:
        pass
    wizard['qty'] = 0 if value == 'min' else max(1, _int(value, 1))
    wizard.setdefault('adapt', False)
    _edit(q, *scr_auto_section(wizard))


def _auto_selected(wizard: Dict[str, Any]) -> List[Dict[str, Any]]:
    return [x for x in wizard['services'] if str(x['service']) in wizard['selected']]


def scr_auto_section(wizard: Dict[str, Any]) -> Tuple[str, InlineKeyboardMarkup]:
    cfg = load_config()
    services = _auto_selected(wizard)
    sections = cfg['design']['sections']
    mapped = [x for x in services if sections.get(f'{x["_profile"]["platform"]}:{x["_profile"]["kind"]}') or sections.get(x['_profile']['platform'])]
    text = (f'🗂 <b>Раздел FunPay</b>\n\nУслуг: {len(services)}. По карте разделов найдено: {len(mapped)}.\n\n'
            '• <b>По карте разделов</b> — каждая услуга уйдёт в раздел своей соцсети (настраивается в «🎨 Оформление → 🗺 Карта разделов»).\n'
            '• <b>Один раздел</b> — все услуги в один раздел FunPay.')
    rows = []
    if mapped:
        rows.append([B(f'🗺 По карте разделов ({len(mapped)}/{len(services)})', 'asm')])
    rows.append([B('✏️ Один раздел для всех', 'asn')])
    rows.append([B('❌ Отмена', 'home')])
    return text, _kb(*rows)


@route('asm')
def r_auto_section_map(q: Any) -> None:
    wizard = _wizards.get(q.from_user.id)
    if not wizard:
        return
    wizard['node_mode'] = 'map'
    cfg = load_config()
    unmapped = [x for x in _auto_selected(wizard) if not (cfg['design']['sections'].get(f'{x["_profile"]["platform"]}:{x["_profile"]["kind"]}')
                                                         or cfg['design']['sections'].get(x['_profile']['platform']))]
    if unmapped:
        _ask(q, 'auto_node', f'⚠️ Для {len(unmapped)} услуг нет раздела в карте. Введите ID раздела FunPay для них '
                             'или <code>0</code>, чтобы пропустить их:', {'fallback': True}, back='home')
        return
    wizard['node'] = 0
    _edit(q, *scr_auto_markup(wizard))


@route('asn')
def r_auto_section_one(q: Any) -> None:
    wizard = _wizards.get(q.from_user.id)
    if not wizard:
        return
    wizard['node_mode'] = 'one'
    _ask(q, 'auto_node', '🗂 Введите ID раздела FunPay (число из ссылки <code>funpay.com/lots/ЧИСЛО/</code>):', back='home')


@inp('auto_node')
def i_auto_node(message: Any, text: str, data: Dict[str, Any]) -> None:
    wizard = _wizards.get(message.from_user.id)
    if not wizard:
        raise ValueError('Мастер устарел.')
    node = _int(text, -1)
    if node < 0 or (node == 0 and not data.get('fallback')):
        raise ValueError('Введите ID раздела — число больше нуля.')
    wizard['node'] = node
    _clear(message)
    _show(message.chat.id, *scr_auto_markup(wizard))


def scr_auto_markup(wizard: Dict[str, Any]) -> Tuple[str, InlineKeyboardMarkup]:
    mode, value = _effective_markup(load_config(), None)
    return (f'📈 <b>Наценка</b>\n\nГлобальная: {_markup_text(mode, value)} (лоты будут следовать общим настройкам автоцены).\n'
            'Или задайте свой процент для этой выгрузки.',
            _kb([B(f'🌍 Глобальная ({_markup_text(mode, value)})', 'amk:g'), B('✏️ Свой процент', 'amk:c')], [B('❌ Отмена', 'home')]))


@route('amk')
def r_auto_markup(q: Any, mode: str) -> None:
    wizard = _wizards.get(q.from_user.id)
    if not wizard:
        return
    if mode == 'c':
        _ask(q, 'auto_markup', '✏️ Введите наценку в процентах (например <code>50</code>):', back='home')
        return
    wizard['markup'] = None
    _edit(q, *scr_auto_style(wizard))


@inp('auto_markup')
def i_auto_markup(message: Any, text: str, data: Dict[str, Any]) -> None:
    wizard = _wizards.get(message.from_user.id)
    if not wizard:
        raise ValueError('Мастер устарел.')
    wizard['markup'] = ('percent', _markup(text))
    _clear(message)
    _show(message.chat.id, *scr_auto_style(wizard))


def scr_auto_style(wizard: Dict[str, Any]) -> Tuple[str, InlineKeyboardMarkup]:
    rows = _template_buttons('ath', load_config())
    rows.append([B('❌ Отмена', 'home')])
    return ('🎨 <b>Оформление</b>\n\n«🎯 По настройкам» — для каждой соцсети и типа услуги возьмётся назначенный шаблон или тема по умолчанию.',
            _kb(*rows))


@route('ath')
def r_auto_style(q: Any, token: str) -> None:
    wizard = _wizards.get(q.from_user.id)
    if not wizard:
        return
    wizard['choice'] = _token_choice(token)
    _edit(q, *scr_auto_review(wizard))


def _auto_plan(wizard: Dict[str, Any]) -> Dict[str, Any]:
    """Готовит план выгрузки: количество, раздел, пропуски и дубли."""
    cfg = load_config()
    existing = {(str(v['panel_id']), str(v['service_id']), v['quantity'], v.get('subcategory')) for v in cfg['lot_mapping'].values()}
    plan, skipped, duplicates = [], [], 0
    for service in _auto_selected(wizard):
        smin, smax = max(1, _int(service.get('min'), 1)), _int(service.get('max'))
        qty = wizard['qty'] or smin
        if qty < smin and wizard.get('adapt', False):
            qty = smin  # пакет: 1 шт. лота = минимум услуги
        if smax and qty > smax:
            skipped.append(f'{service["service"]}: максимум услуги {smax} < {qty}')
            continue
        profile = service['_profile']
        node = wizard['node']
        if wizard.get('node_mode') == 'map':
            node = _int(cfg['design']['sections'].get(f'{profile["platform"]}:{profile["kind"]}') or
                        cfg['design']['sections'].get(profile['platform'])) or wizard['node']
        if not node:
            skipped.append(f'{service["service"]}: нет раздела для {profile["platform"]}')
            continue
        if (str(wizard['panel']), str(service['service']), qty, node) in existing:
            duplicates += 1
            continue
        plan.append({'service': service, 'qty': qty, 'node': node})
    return {'plan': plan, 'skipped': skipped, 'duplicates': duplicates}


def scr_auto_review(wizard: Dict[str, Any]) -> Tuple[str, InlineKeyboardMarkup]:
    cfg = load_config()
    info = _auto_plan(wizard)
    wizard['plan'] = info['plan']
    kinds: Dict[str, int] = {}
    for item in info['plan']:
        profile = item['service']['_profile']
        label = f'{_platform_info(profile["platform"])["name"]} · {KINDS[profile["kind"]]["ru"].lower()}'
        kinds[label] = kinds.get(label, 0) + 1
    nodes = sorted({x['node'] for x in info['plan']})
    mode, value = wizard.get('markup') or _effective_markup(cfg, None)
    text = (f'🧾 <b>Проверьте выгрузку</b>\n\nБудет создано лотов: <b>{len(info["plan"])}</b>\n'
            f'Уже выставлены (пропуск дублей): {info["duplicates"]}\nПропущено: {len(info["skipped"])}\n'
            f'Разделы FunPay: {", ".join(map(str, nodes[:8])) or "—"}\n'
            f'За 1 шт.: {wizard["qty"] or "минимум каждой услуги"}'
            f' · {"пакет до минимума услуги" if wizard.get("adapt", False) else "без пакетов (покупатель берёт от минимума)"}\n'
            f'Наценка: {_markup_text(mode, value)} · Оформление: {escape(_template_name(cfg, wizard["choice"]))}\n\n'
            '<b>Типы услуг:</b>\n' + '\n'.join(f'• {escape(k)}: {v}' for k, v in sorted(kinds.items())[:20]))
    if info['skipped']:
        text += '\n\n<b>Пропуски:</b>\n' + '\n'.join('• ' + escape(x) for x in info['skipped'][:8])
    if wizard.get('selects'):
        text += f'\n\n🧩 Поля раздела заданы вручную: {len(wizard["selects"])}'
    rows = [[B(f'🚀 Создать {len(info["plan"])} лотов', 'ago')] if info['plan'] else None,
            [B('👁 Пример лота', 'asamp'), B(f'📏 Пакет по минимуму: {"ВКЛ" if wizard.get("adapt", False) else "ВЫКЛ"}', 'aadapt')],
            [B('🧩 Поля раздела', 'fl:auto')] if wizard.get('node_mode') == 'one' else None,
            [B('❌ Отмена', 'home')]]
    return text[:4000], _kb(*rows)


@route('aadapt')
def r_auto_adapt(q: Any) -> None:
    wizard = _wizards.get(q.from_user.id)
    if not wizard:
        return
    wizard['adapt'] = not wizard.get('adapt', False)
    _edit(q, *scr_auto_review(wizard))


@route('asamp')
def r_auto_sample(q: Any) -> None:
    wizard = _wizards.get(q.from_user.id)
    if not wizard or not wizard.get('plan'):
        _answer(q, 'Нет услуг для примера.', True)
        return
    _answer(q)
    cfg = load_config()
    item = wizard['plan'][_int(wizard.get('sample_index')) % len(wizard['plan'])]
    wizard['sample_index'] = _int(wizard.get('sample_index')) + 1
    try:
        cost, _, _ = _cost_rub(cfg, wizard['panel'], item['service'], item['qty'])
    except PanelError as exc:
        _show(q.message.chat.id, f'❌ {escape(str(exc))}')
        return
    mode, value = wizard.get('markup') or _effective_markup(cfg, None)
    price = _price_from_cost(cost, mode, value)
    texts = render_lot_texts(item['service'], item['qty'], price, wizard['choice'], {'summary_ru': 80, 'summary_en': 80,
                             'desc_ru': 5000, 'desc_en': 5000}, cfg, 'xxxxxxxx')
    _show(q.message.chat.id, f'👁 <b>Пример</b> (услуга {escape(str(item["service"]["service"]))})\n\n<b>{escape(texts["summary_ru"])}</b>\n\n'
                             f'<blockquote expandable>{escape(texts["desc_ru"][:2500])}</blockquote>\n\n💵 {_money(price)} ₽ · раздел {item["node"]}',
          _kb([B('👁 Следующий пример', 'asamp')]))


@route('ago')
def r_auto_go(q: Any) -> None:
    wizard = _wizards.get(q.from_user.id)
    if not wizard or not wizard.get('plan') or wizard.get('running'):
        _answer(q, 'Мастер устарел или выгрузка уже идёт.', True)
        return
    if not _account_ready():
        _answer(q, 'Аккаунт FunPay ещё не загружен.', True)
        return
    wizard['running'] = True
    _answer(q)
    batch_id = secrets.token_hex(3)
    job_state = {'stop': False}
    _jobs[batch_id] = job_state
    user_id = q.from_user.id

    def job(progress: Callable) -> str:
        cfg = load_config()
        plan = wizard['plan']
        created, failed, errors, warnings_total = 0, 0, [], 0
        forms: Dict[int, Dict[str, Any]] = {}
        stop_reason = ''
        processed = 0
        markup = wizard.get('markup')
        with config_tx() as tx:
            tx['batches'][batch_id] = {'created_at': time.time(), 'panel': wizard['panel'], 'count': 0}
        try:
            for index, item in enumerate(plan, 1):
                if job_state['stop']:
                    stop_reason = 'Остановлено вручную.'
                    break
                processed = index
                service, profile = item['service'], item['service']['_profile']
                try:
                    if item['node'] not in forms:
                        forms[item['node']] = _node_form(item['node'])
                    cost, _, _ = _cost_rub(cfg, wizard['panel'], service, item['qty'])
                    mode, value = markup or _effective_markup(cfg, None)
                    price = _price_from_cost(cost, mode, value)
                    tag = _new_tag(x.get('tag') for x in load_config()['lot_mapping'].values())
                    lot_id, name, warns = _create_lot(item['node'], wizard['panel'], service, item['qty'], price, wizard['choice'],
                                                      tag, form=forms[item['node']],
                                                      select_overrides=wizard.get('selects') if wizard.get('node_mode') == 'one' else None)
                    warnings_total += len(warns)
                    refill = None
                    if cfg['refill'].get('auto_enable') and (service.get('refill') in (True, 1, '1', 'true') or profile['guarantee_days']):
                        refill = {'enabled': True, 'hours': (profile['guarantee_days'] or 30) * 24}
                    _register_lot(name, lot_id, wizard['panel'], service['service'], item['qty'],
                                  list(_platform_info(profile['platform']).get('domains') or []), source='auto',
                                  subcategory=item['node'], batch_id=batch_id, tag=tag, platform=profile['platform'],
                                  kind=profile['kind'], template='' if wizard['choice'] == 'auto' else wizard['choice'],
                                  price=price, markup=markup, poll='poll' in str(service.get('type', '')).lower(),
                                  tag_written=bool(lot_id), refill=refill)
                    created += 1
                except Exception as exc:
                    failed += 1
                    reason = str(exc) if isinstance(exc, (PanelError, LotPublishError, RuntimeError)) else type(exc).__name__
                    errors.append(f'{service["service"]}: {_safe_detail(reason, cfg, 200)}')
                    if isinstance(exc, LotPublishError) and exc.stop_batch:
                        stop_reason = _safe_detail(reason, cfg, 300)
                        break
                progress(index, len(plan), f'✅ Создано: {created} · ❌ Ошибок: {failed}')
        finally:
            _jobs.pop(batch_id, None)
            if _wizards.get(user_id) is wizard:
                _wizards.pop(user_id, None)
            with config_tx() as tx:
                if created:
                    tx['batches'][batch_id]['count'] = created
                else:
                    tx['batches'].pop(batch_id, None)
        text = (f'{"⚠️ Выгрузка остановлена" if stop_reason else "✅ Выгрузка завершена"}\n\n'
                f'Создано: <b>{created}</b> · Ошибок: {failed} · Не обработано: {len(plan) - processed}'
                + (f'\nПолей выбрано по умолчанию: {warnings_total} (проверьте пару лотов)' if warnings_total else '')
                + (f'\nВыгрузка: <code>{batch_id}</code>' if created else ''))
        if stop_reason:
            text += '\n\n' + escape(stop_reason)
        if errors:
            text += '\n\n<b>Ошибки:</b>\n' + '\n'.join('• ' + escape(x) for x in errors[:15])
        return text

    _progress_job(q.message.chat.id, 'Создаю лоты на FunPay', job,
                  _kb([B('📦 Открыть выгрузку', f'bt:{batch_id}')], [B('◀️ Главное меню', 'home')]), stop_key=batch_id)


# ---------------------------------------------------------------------------
# Выгрузки (откат)
# ---------------------------------------------------------------------------

@route('bts')
def r_batches(q: Any) -> None:
    cfg = load_config()
    batches = sorted(cfg['batches'].items(), key=lambda x: -_float(x[1].get('created_at')))
    rows = []
    for batch_id, info in batches[:15]:
        count = sum(1 for x in cfg['lot_mapping'].values() if x.get('batch_id') == batch_id)
        when = datetime.fromtimestamp(_float(info.get('created_at'))).strftime('%d.%m %H:%M')
        rows.append([B(f'📦 {batch_id} · {when} · лотов {count}', f'bt:{batch_id}')])
    rows.append([B('◀️ Лоты', 'lots')])
    _edit(q, '📦 <b>Выгрузки</b>\n\nЗдесь можно выключить или удалить лоты одной выгрузки.' if batches else
          '📦 <b>Выгрузки</b>\n\nВыгрузок пока не было.', _kb(*rows))


@route('bt')
def r_batch(q: Any, batch_id: str) -> None:
    cfg = load_config()
    lots = [k for k, v in cfg['lot_mapping'].items() if v.get('batch_id') == batch_id]
    info = cfg['batches'].get(batch_id, {})
    when = datetime.fromtimestamp(_float(info.get('created_at'))).strftime('%d.%m.%Y %H:%M') if info else '—'
    _edit(q, f'📦 <b>Выгрузка {escape(batch_id)}</b>\nДата: {when}\nЛотов в плагине: {len(lots)}',
          _kb([B('📋 Лоты выгрузки', f'll:b_{batch_id}:0')],
              [B('🔴 Выключить все', f'bta:{batch_id}:0'), B('🟢 Включить все', f'bta:{batch_id}:1')],
              [B('🗑 Удалить выгрузку', f'btd:{batch_id}')], [B('◀️ Выгрузки', 'bts')]))


@route('bta')
def r_batch_active(q: Any, batch_id: str, value: str) -> None:
    lots = [v['funpay_lot_id'] for v in load_config()['lot_mapping'].values() if v.get('batch_id') == batch_id and v.get('funpay_lot_id')]
    _answer(q)
    def job(progress: Callable) -> str:
        changed, errors = _bulk_active(lots, value == '1', progress)
        return (f'{"🟢 Включено" if value == "1" else "🔴 Выключено"} лотов: {changed} из {len(lots)}' +
                (('\n\nОшибки:\n' + '\n'.join('• ' + escape(x) for x in errors[:10])) if errors else ''))
    _progress_job(q.message.chat.id, 'Меняю состояние лотов выгрузки', job, _back_kb(f'bt:{batch_id}', '◀️ К выгрузке'))


@route('btd')
def r_batch_delete(q: Any, batch_id: str) -> None:
    _edit(q, f'🗑 <b>Удалить выгрузку {escape(batch_id)}?</b>\n\nВыберите вариант:',
          _kb([B('🗑 Только из плагина', f'btdd:{batch_id}:0')], [B('🔥 Из плагина и с FunPay', f'btdd:{batch_id}:1')],
              [B('Отмена', f'bt:{batch_id}')]))


@route('btdd')
def r_batch_delete_do(q: Any, batch_id: str, remote: str) -> None:
    cfg = load_config()
    lots = [(k, v) for k, v in cfg['lot_mapping'].items() if v.get('batch_id') == batch_id]
    _answer(q)
    def job(progress: Callable) -> str:
        removed, errors = [], []
        for index, (key, lot) in enumerate(lots, 1):
            if remote == '1' and lot.get('funpay_lot_id'):
                try:
                    _delete_funpay_lot(lot['funpay_lot_id'])
                except Exception as exc:
                    errors.append(f'{lot["funpay_lot_id"]}: {_rejection_text(exc)}')
                    continue
            removed.append(key)
            if index % 5 == 0 or index == len(lots):
                progress(index, len(lots))
        with config_tx(allow_shrink=True) as tx:
            for key in removed:
                tx['lot_mapping'].pop(key, None)
            if len(removed) == len(lots):
                tx['batches'].pop(batch_id, None)
        return (f'🗑 Удалено лотов: {len(removed)} из {len(lots)}' + (' (и на FunPay)' if remote == '1' else '') +
                (('\n\nОшибки:\n' + '\n'.join('• ' + escape(x) for x in errors[:10])) if errors else ''))
    _progress_job(q.message.chat.id, 'Удаляю выгрузку', job, _back_kb('bts', '◀️ Выгрузки'))


# ---------------------------------------------------------------------------
# Поля раздела FunPay (выпадающие списки): ручной выбор вместо автоподбора
# ---------------------------------------------------------------------------

def _fields_wizard(user_id: int, mode: str) -> Optional[Dict[str, Any]]:
    wizard = _wizards.get(user_id)
    if not wizard or wizard.get('kind') != mode:
        return None
    return wizard


def _fields_form(wizard: Dict[str, Any]) -> Dict[str, Any]:
    if 'form' not in wizard:
        wizard['form'] = _node_form(wizard['node'])
    return wizard['form']


def scr_fields(wizard: Dict[str, Any], mode: str) -> Tuple[str, InlineKeyboardMarkup]:
    form = _fields_form(wizard)
    selects = [(name, options) for name, options in form['selects'].items() if name not in form['hidden']]
    wizard['_select_names'] = [name for name, _ in selects]
    chosen = wizard.setdefault('selects', {})
    rows = []
    for index, (name, options) in enumerate(selects):
        label = form['labels'].get(name, name)
        value = chosen.get(name)
        text = next((t for v, t in options if v == value), None) if value else None
        rows.append([B(f'{_short(label, 24)}: {_short(text, 20) if text else "🤖 авто"}', f'fs:{mode}:{index}:0')])
    rows.append([B('✅ Готово', 'ncpv' if mode == 'create' else 'arv')])
    return ('🧩 <b>Поля раздела FunPay</b>\n\n«🤖 авто» — бот подберёт значение по типу услуги (подписчики, просмотры, реакции…). '
            'Задайте значение вручную, если раздел требует другое.', _kb(*rows))


@route('fl')
def r_fields(q: Any, mode: str) -> None:
    wizard = _fields_wizard(q.from_user.id, mode)
    if not wizard or not wizard.get('node'):
        _answer(q, 'Мастер устарел — начните заново.', True)
        return
    try:
        _edit(q, *scr_fields(wizard, mode))
    except RuntimeError as exc:
        _answer(q, str(exc), True)


@route('fs')
def r_fields_select(q: Any, mode: str, index: str, page: str = '0') -> None:
    wizard = _fields_wizard(q.from_user.id, mode)
    if not wizard or _int(index) >= len(wizard.get('_select_names', [])):
        _answer(q, 'Мастер устарел — начните заново.', True)
        return
    name = wizard['_select_names'][_int(index)]
    options = [(v, t) for v, t in wizard['form']['selects'][name] if v]
    items, page_n, pages = _page(list(enumerate(options)), _int(page), 10)
    rows = [[B('🤖 Авто (по типу услуги)', f'fo:{mode}:{index}:-')]]
    rows += [[B(_short(text, 40), f'fo:{mode}:{index}:{i}')] for i, (_, text) in items]
    rows.append(_nav(f'fs:{mode}:{index}', page_n, pages))
    rows.append([B('◀️ Назад', f'fl:{mode}')])
    _edit(q, f'🧩 {escape(wizard["form"]["labels"].get(name, name))}: выберите значение', _kb(*rows))


@route('fo')
def r_fields_option(q: Any, mode: str, index: str, option: str) -> None:
    wizard = _fields_wizard(q.from_user.id, mode)
    if not wizard or _int(index) >= len(wizard.get('_select_names', [])):
        _answer(q, 'Мастер устарел — начните заново.', True)
        return
    name = wizard['_select_names'][_int(index)]
    options = [(v, t) for v, t in wizard['form']['selects'][name] if v]
    if option == '-':
        wizard.setdefault('selects', {}).pop(name, None)
    elif _int(option) < len(options):
        wizard.setdefault('selects', {})[name] = options[_int(option)][0]
    _edit(q, *scr_fields(wizard, mode))


@route('ncpv')
def r_create_preview_again(q: Any) -> None:
    _answer(q, '⏳ Готовлю предпросмотр…')
    _submit(_create_preview, q.message.chat.id, q.from_user.id)


@route('arv')
def r_auto_review_again(q: Any) -> None:
    wizard = _fields_wizard(q.from_user.id, 'auto')
    if not wizard:
        _answer(q, 'Мастер устарел — начните заново.', True)
        return
    _edit(q, *scr_auto_review(wizard))


# ---------------------------------------------------------------------------
# Текст для лота из услуги панели (без создания лота)
# ---------------------------------------------------------------------------

@route('ntx')
def r_text_start(q: Any) -> None:
    if not load_config()['services']:
        _answer(q, 'Сначала добавьте SMM-панель в разделе «🔌 Панели».', True)
        return
    _wizard(q.from_user.id, 'text', reset=True)
    _edit(q, '📋 <b>Текст для лота</b>\n\nБот подготовит название и описание под услугу панели. Выберите панель:',
          _kb(*_panel_rows('ntp', 'new')))


@route('ntp')
def r_text_panel(q: Any, panel_id: str) -> None:
    _wizard(q.from_user.id, 'text')['panel'] = panel_id
    _ask(q, 'text_service', '🆔 Введите ID услуги или часть названия для поиска:', back='new')


@inp('text_service')
def i_text_service(message: Any, text: str, data: Dict[str, Any]) -> None:
    _service_pick(message, text, 'text')


@inp('text_qty')
def i_text_qty(message: Any, text: str, data: Dict[str, Any]) -> None:
    if _int(text) <= 0:
        raise ValueError('Введите число больше нуля.')
    _clear(message)
    _text_show(message.chat.id, message.from_user.id, _int(text), 'auto')


@route('ntq')
def r_text_qty(q: Any, value: str) -> None:
    _answer(q)
    try:
        cardinal_instance.telegram.clear_state(q.message.chat.id, q.from_user.id, False)
    except Exception:
        pass
    _text_show(q.message.chat.id, q.from_user.id, _int(value, 1), 'auto')


@route('ntt')
def r_text_theme(q: Any, token: str) -> None:
    _answer(q)
    wizard = _wizard(q.from_user.id, 'text')
    _text_show(q.message.chat.id, q.from_user.id, _int(wizard.get('qty'), 1), _token_choice(token))


def _text_show(chat_id: int, user_id: int, quantity: int, choice: str) -> None:
    wizard = _wizard(user_id, 'text')
    service = wizard.get('service')
    if not service:
        _show(chat_id, '⌛ Мастер устарел — начните заново.', _back_kb('new'))
        return
    wizard['qty'] = max(1, quantity)
    cfg = load_config()
    try:
        cost, _, _ = _cost_rub(cfg, wizard['panel'], service, wizard['qty'])
        mode, value = _effective_markup(cfg, None)
        price = _price_from_cost(cost, mode, value)
        price_line = f'\n💵 Рекомендуемая цена: <b>{_money(price)} ₽</b> за 1 шт. (себестоимость {_money(cost)} ₽, наценка {_markup_text(mode, value)})'
    except PanelError as exc:
        price, price_line = 0.0, f'\n⚠️ Цену посчитать не удалось: {escape(str(exc))}'
    parts = _text_message(service, wizard['qty'], price, choice,
                          f'📋 <b>Текст для лота</b> — нажмите на блок, чтобы скопировать, и вставьте в редактор лота на {MARKET}.'
                          + price_line)
    _show(chat_id, parts[0])
    rows = _template_buttons('ntt', cfg)
    rows.append([B('🔗 Лот создан — привязать по ID', 'nb')])
    rows.append([B('◀️ Меню', 'home')])
    _show(chat_id, parts[1], _kb(*rows))
