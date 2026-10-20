

# ---------------------------------------------------------------------------
# Разбор услуги панели: соцсеть, тип, характеристики
# ---------------------------------------------------------------------------

_EMOJI_RE = re.compile('[\U0001F000-\U0001FAFF☀-➿⬀-⯿️‍←-⇿⌀-⏿]+')


def _service_text(service: Dict[str, Any]) -> Tuple[str, str]:
    return str(service.get('name') or '').lower(), str(service.get('category') or '').lower()


def _detect_platform(service: Dict[str, Any]) -> str:
    name, category = _service_text(service)
    for text in (category, name):
        for key in PLATFORM_ORDER:
            if key == 'website':
                continue
            if re.search(PLATFORMS[key]['pattern'], text):
                return key
    if re.search(r'traffic|трафик|website|визит|посещен', name + ' ' + category):
        return 'website'
    return 'other'


def _detect_kind(service: Dict[str, Any]) -> str:
    name, category = _service_text(service)
    service_type = str(service.get('type') or '').lower()
    if 'poll' in service_type:
        return 'votes'
    if 'comment' in service_type:
        return 'comments'
    for text in (name, category, name + ' ' + category):
        for kind, pattern in _COMPOUND_KINDS:
            if re.search(pattern, text):
                return kind
        best: Optional[Tuple[int, int, str]] = None
        for order, (kind, pattern) in enumerate(_SIMPLE_KINDS):
            match = re.search(pattern, text)
            if match and (best is None or (match.start(), order) < best[:2]):
                best = (match.start(), order, kind)
        if best:
            return best[2]
    return 'generic'


def _parse_service(service: Dict[str, Any]) -> Dict[str, Any]:
    """Извлекает из «TG Members | Max 50K | R30 | 1-2K/Day [ID 4512]» понятные поля."""
    raw = str(service.get('name') or '')
    text = raw
    info: Dict[str, Any] = {'speed': '', 'start': '', 'guarantee': '', 'guarantee_days': 0, 'quality': [], 'max_hint': ''}
    low = text.lower()

    match = re.search(r'(\d+(?:[.,]\d+)?\s*[kкmм]?\s*[-–]\s*)?\d+(?:[.,]\d+)?\s*[kкmм]?\s*(?:/|per|в|за)\s*(?:day|d|сут\w*|день|дн|h|hour|час)\b', low)
    if match:
        info['speed'] = match.group(0).replace(' ', '').upper().replace('/DAY', '/сутки').replace('/D', '/сутки') \
            .replace('/H', '/час').replace('/HOUR', '/час')
        text = text.replace(raw[match.start():match.end()], ' ')
    match = re.search(r'(?:start|старт)\s*:?\s*([0-9]+\s*[-–]\s*[0-9]+\s*(?:h|hr|hours?|ч|час\w*|min|мин\w*|m)|'
                      r'[0-9]+\s*(?:h|hr|hours?|ч|час\w*|min|мин\w*|m)|instant\w*|моментальн\w*|мгновенн\w*)', low)
    if match:
        value = match.group(1)
        value = re.sub(r'(?:hours?|hr|h)\b', 'ч', value)
        value = re.sub(r'(?:min|m)\b', 'мин', value)
        info['start'] = 'моментально' if re.match(r'instant|момент|мгновен', value) else value.replace(' ', '')
        text = re.sub(re.escape(raw[match.start():match.end()]), ' ', text, flags=re.I)
    elif re.search(r'\binstant\b|моментальн|мгновенн', low):
        info['start'] = 'моментально'
    match = re.search(r'(?:\br(\d{1,3})\b|refill\s*:?\s*(\d{1,3})\s*(?:d|days?|дн\w*)|(\d{1,3})\s*(?:d|days?|дн\w*)\s*refill|'
                      r'(?:гарант|рефил)\w*\s*:?\s*(\d{1,3})\s*(?:д|дн\w*)|(\d{1,3})\s*(?:д|дн\w*)\s*(?:гарант|рефил)\w*)', low)
    if match:
        days = _int(next(x for x in match.groups() if x))
        if 0 < days <= 3650:
            info['guarantee_days'] = days
            info['guarantee'] = f'{days} {_plural(days, ("день", "дня", "дней"))}'
        text = re.sub(re.escape(raw[match.start():match.end()]), ' ', text, flags=re.I)
    elif re.search(r'lifetime|навсегда|пожизнен', low):
        info['guarantee'] = 'пожизненная'
        info['guarantee_days'] = 3650
    elif service.get('refill') in (True, 1, '1', 'true', 'True'):
        info['guarantee'] = 'есть (рефилл)'
    info['no_refill'] = bool(re.search(r'no\s*refill|без\s*гарант|без\s*рефил|\bnr\b', low))
    if info['no_refill'] and not match:
        info['guarantee'], info['guarantee_days'] = '', 0
    match = re.search(r'max\s*:?\s*(\d+(?:[.,]\d+)?\s*[kкmм]?)', low)
    if match:
        info['max_hint'] = match.group(1).upper()
        text = re.sub(re.escape(raw[match.start():match.end()]), ' ', text, flags=re.I)
    for pattern, label in ((r'\breal\b|живы|реальн', 'ЖИВЫЕ'), (r'premium|премиум', 'PREMIUM'), (r'\bhq\b|high\s*quality|качествен', 'HQ'),
                           (r'\bru\b|рус|россия|рф\b', 'RU'), (r'снг|\bcis\b', 'СНГ'), (r'\bmix\b|микс', 'MIX'),
                           (r'\busa?\b|сша', 'USA'), (r'non[\s-]?drop|без\s+списан', 'БЕЗ СПИСАНИЙ'), (r'\bbots?\b|бот(ы|ов)\b', 'БОТЫ')):
        found = re.search(pattern, low)
        if found and label not in info['quality']:
            info['quality'].append(label)
            info.setdefault('quality_words', {})[label] = found.group(0)
    text = re.sub(r'\[[^\]]*\bid\b[^\]]*\]|\bid\s*[:#]?\s*\d+|^\s*\d+\s*[-–.|:]\s*', ' ', text, flags=re.I)
    text = _EMOJI_RE.sub(' ', text)
    info['clean_name'] = ' • '.join(_name_chunks(text)) or raw.strip() or 'SMM-услуга'
    return info


def _name_chunks(text: str) -> List[str]:
    """«Telegram Подписчики [Без гарантии] [Резервные]» → ['Telegram Подписчики', 'Без гарантии', 'Резервные']."""
    result: List[str] = []
    for chunk in re.split(r'[\[\]\(\)\|•{}]+|\s[-–—]\s', text):
        chunk = re.sub(r'\s+', ' ', chunk).strip(' .,;:-–—/\\*#')
        if len(chunk) >= 2 and not chunk.isdigit() and chunk.lower() not in (x.lower() for x in result):
            result.append(chunk)
    return result


_STRUCTURAL_RE = [
    r'(\d+(?:[.,]\d+)?\s*[kкmм]?\s*[-–]\s*)?\d+(?:[.,]\d+)?\s*[kкmм]?\s*(?:/|per|в|за)\s*(?:day|d|сут\w*|день|дн|h|hour|час)\b',
    r'(?:start|старт)\s*:?\s*(?:[0-9]+\s*[-–]\s*[0-9]+|[0-9]+)?\s*(?:h|hr|hours?|ч|час\w*|min|мин\w*|m)?\b',
    r'\b(?:max|min|макс|мин)\s*:?\s*\d+(?:[.,]\d+)?\s*[kкmм]?\b', r'\br\d{1,3}\b', r'\bid\s*[:#]?\s*\d+',
    r'\binstant\b|моментальн\w*|мгновенн\w*', r'\bnr\b',
]
_FILLER_RE = r'^(?:на|в|для|for|to|on|услуг\w*|services?|накрутк\w*|smm|new|новинка|best|топ|top)$'
_LONE_FILLER_RE = r'^(?:post|posts|пост|посты)$'


def _variant(service: Dict[str, Any], profile: Dict[str, Any]) -> str:
    """Чем услуга отличается от других того же типа: «Без гарантии • Резервные».
    Соцсеть, тип, скорость, старт и лимиты отбрасываются — они уже показаны отдельно."""
    text = _EMOJI_RE.sub(' ', str(service.get('name') or ''))
    for pattern in _STRUCTURAL_RE:
        text = re.sub(pattern, ' ', text, flags=re.I)
    words = [PLATFORMS.get(profile['platform'], {}).get('pattern', '')]
    words += [p for k, p in _COMPOUND_KINDS + _SIMPLE_KINDS if k == profile['kind']]
    words += [re.escape(x) for x in KINDS.get(profile['kind'], KINDS['generic'])['select'] if len(x) >= 4]
    # Слова из названия типа («бусты канала», «старты бота») не повторяем в варианте.
    kinfo = KINDS.get(profile['kind'], KINDS['generic'])
    title_stems = [w[:4] for w in re.split(r'[\s/-]+', (kinfo['ru'] + ' ' + kinfo['en']).lower()) if len(w) >= 4]
    parts = []
    for chunk in _name_chunks(text):
        tokens = []
        for token in chunk.split():
            low = token.lower()
            if any(w and re.search(w, low) for w in words) or re.fullmatch(_FILLER_RE, low) or \
                    any(low.startswith(stem) for stem in title_stems):
                continue
            tokens.append(token)
        if len(tokens) == 1 and re.fullmatch(_LONE_FILLER_RE, tokens[0].lower()):
            tokens = []
        cleaned = ' '.join(tokens).strip(' .,;:-–—/')
        if len(cleaned) >= 2 and not cleaned.isdigit() and cleaned.lower() not in (x.lower() for x in parts):
            parts.append(cleaned)
    if profile['kind'] == 'reactions':
        # Для реакций эмодзи — это и есть суть услуги (👍❤️🔥), их не выбрасываем.
        emojis = ''.join(dict.fromkeys(re.findall('[\U0001F300-\U0001FAFF\u2764\u2B50]', str(service.get('name') or ''))))
        if emojis:
            parts.insert(0, emojis[:12])
    return ' • '.join(parts[:4])


_EN_PHRASES = [
    (r'без\s+гарант\w*', 'no refill'), (r'без\s+рефил\w*', 'no refill'), (r'без\s+списан\w*', 'no drop'),
    (r'гарант\w*', 'refill'), (r'рефил\w*', 'refill'), (r'резервн\w*', 'reserve'), (r'живы\w*', 'real'),
    (r'реальн\w*', 'real'), (r'быстр\w*', 'fast'), (r'медленн\w*', 'slow'), (r'дешев\w*|дешёв\w*', 'cheap'),
    (r'премиум\w*', 'premium'), (r'русск\w*|россия\w*|\bрф\b', 'russian'), (r'бот(ы|ов)?\b', 'bots'),
    (r'смешанн\w*|микс\w*', 'mix'), (r'качествен\w*', 'HQ'), (r'стабильн\w*', 'stable'), (r'нов(ые|ый|ая)\b', 'new'),
    (r'активн\w*', 'active'), (r'целев\w*', 'targeted'), (r'мужск\w*', 'male'), (r'женск\w*', 'female'),
    (r'офферн\w*|оффер\w*', 'offer'), (r'медиа\w*', 'media'), (r'аудитори\w*', 'audience'), (r'снг', 'CIS'),
    (r'последн\w*', 'latest'), (r'будущ\w*', 'future'), (r'пост\b', 'post'), (r'пост(ов|а|ы)\b', 'posts'), (r'публикаци\w*', 'post'),
    (r'позитивн\w*', 'positive'), (r'негативн\w*', 'negative'), (r'аккаунт\w*', 'accounts'), (r'просмотр\w*', 'views'),
    (r'подписчик\w*', 'followers'), (r'реакци\w*', 'reactions'), (r'лайк\w*', 'likes'), (r'комментари\w*', 'comments'),
    (r'участник\w*', 'members'), (r'репост\w*', 'shares'), (r'канал\w*', 'channel'), (r'чат\w*', 'chat'),
    (r'групп\w*', 'group'), (r'видео', 'video'), (r'сторис', 'stories'), (r'эфир\w*', 'live'), (r'автопросмотр\w*', 'auto views'),
    (r'удержани\w*', 'retention'), (r'миров\w*', 'worldwide'), (r'онлайн\w*', 'online'), (r'час(ы|ов)\b', 'hours'),
    (r'бот(а|у)\b', 'bot'), (r'зарубежн\w*', 'foreign'), (r'иностранн\w*', 'foreign'), (r'мгновенн\w*', 'instant'),
    (r'моментальн\w*', 'instant'), (r'ручн\w*', 'manual'), (r'авто\w*', 'auto'), (r'дешёв\w*', 'cheap'),
    (r'\bдн(я|ей|и)?\b|\bдень\b', 'days'), (r'\bчас(а|ов)?\b|\bч\b', 'h'), (r'\bмин\w*', 'min'),
    (r'\bи\b', '&'), (r'\bс\b', 'with'), (r'\bот\b', 'from'), (r'\bдо\b', 'up to'),
]


def _en_phrase(text: str) -> str:
    result = str(text or '')
    for pattern, en in _EN_PHRASES:
        result = re.sub(pattern, en, result, flags=re.I)
    return _translit(result) if re.search('[а-яё]', result, re.I) else result


def _service_profile(service: Dict[str, Any]) -> Dict[str, Any]:
    platform = _detect_platform(service)
    kind = _detect_kind(service)
    if kind == 'traffic':
        platform = 'website'
    return {'platform': platform, 'kind': kind, **_parse_service(service)}


def _platform_info(platform: str) -> Dict[str, Any]:
    return PLATFORMS.get(platform, OTHER_PLATFORM)


def _infer_domains(service: Dict[str, Any]) -> List[str]:
    profile = _service_profile(service)
    return list(_platform_info(profile['platform']).get('domains') or [])


def _example_link(platform: str, kind: str) -> str:
    links = _platform_info(platform).get('links', {})
    target = KINDS.get(kind, KINDS['generic'])['target']
    return links.get(target) or links.get('post') or links.get('profile') or 'https://example.com/page'


def _example(domains: List[str], platform: str = '', kind: str = '') -> str:
    if platform:
        return _example_link(platform, kind or 'generic')
    joined = ' '.join(domains)
    for key in PLATFORM_ORDER:
        if any(domain in joined for domain in PLATFORMS[key]['domains'] if domain != '*'):
            return _example_link(key, kind or 'followers')
    return 'https://example.com/page'


# ---------------------------------------------------------------------------
# Сборка текстов лота
# ---------------------------------------------------------------------------

def _pieces_range(service: Dict[str, Any], quantity: int) -> Tuple[int, int]:
    quantity = max(1, quantity)
    smin, smax = max(1, _int(service.get('min'), 1)), _int(service.get('max'))
    min_pcs = max(1, math.ceil(smin / quantity))
    max_pcs = max(min_pcs, smax // quantity) if smax > 0 else 99999
    return min_pcs, min(99999, max_pcs)


def _design_context(service: Dict[str, Any], quantity: int, price: float = 0.0, cfg: Optional[Dict[str, Any]] = None,
                    profile: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    cfg = cfg or load_config()
    profile = profile or _service_profile(service)
    platform, kind = profile['platform'], profile['kind']
    pinfo, kinfo = _platform_info(platform), KINDS.get(kind, KINDS['generic'])
    min_pcs, max_pcs = _pieces_range(service, quantity)
    target = kinfo['target']
    notes_ru, notes_en = list(TARGET_NOTES.get(target, TARGET_NOTES['post'])[0]), list(TARGET_NOTES.get(target, TARGET_NOTES['post'])[1])
    extra = KIND_EXTRA_NOTES.get(kind)
    if extra:
        notes_ru += extra[0]
        notes_en += extra[1]
    notes_ru.append('Не запускайте эту же услугу параллельно у других продавцов')
    notes_en.append('Do not run the same service with other sellers at the same time')
    if kind == 'generic':
        title_ru = _short(profile['clean_name'].upper(), 60)
        title_en = _short(_translit(profile['clean_name']).upper(), 60)
    else:
        title_ru = f"{kinfo['ru']} {pinfo['short']}".strip()
        title_en = f"{pinfo['short'] if platform not in ('vk', 'ok', 'dzen', 'yamusic', 'website') else _translit(pinfo['short'])} {kinfo['en']}".strip()
    if platform == 'website':
        title_ru, title_en = kinfo['ru'] if kind == 'traffic' else title_ru, 'WEBSITE TRAFFIC' if kind == 'traffic' else title_en
    variant = _variant(service, profile)
    if cfg['design'].get('title_source') == 'panel':
        title_ru = _short(profile['clean_name'], 70)
        title_en = _short(_en_phrase(profile['clean_name']), 70)
        variant = ''
    unit = _plural(quantity, kinfo['unit'])
    unit_en = _plural_en(quantity, kinfo['unit_en'])
    return {
        'title': title_ru, 'title_en': title_en, 'platform': pinfo['name'], 'platform_en': _translit(pinfo['name']),
        'platform_key': platform, 'kind_key': kind, 'kind': kinfo['ru'].capitalize(), 'kind_en': kinfo['en'].capitalize(),
        'emoji': kinfo['emoji'], 'platform_emoji': pinfo['emoji'],
        'service_name': str(service.get('name') or ''), 'clean_name': profile['clean_name'],
        'per_piece': _human_number(quantity), 'per_piece_raw': quantity, 'unit': unit, 'unit_en': unit_en,
        'min_pcs': min_pcs, 'max_pcs': max_pcs, 'min': _human_number(service.get('min', '1')), 'max': _human_number(service.get('max', '—')),
        'speed': profile['speed'], 'start': profile['start'], 'guarantee': profile['guarantee'],
        'guarantee_days': profile['guarantee_days'], 'quality': ', '.join(profile['quality']),
        'quality_list': profile['quality'], 'quality_words': profile.get('quality_words', {}),
        'no_refill': profile.get('no_refill', False), 'variant': variant, 'variant_en': _en_phrase(variant),
        'example_link': _example_link(platform, kind), 'link_hint': TARGET_TEXT.get(target, TARGET_TEXT['post'])[0],
        'link_hint_en': TARGET_TEXT.get(target, TARGET_TEXT['post'])[1],
        'requirements': '\n'.join('• ' + x for x in notes_ru), 'requirements_en': '\n'.join('• ' + x for x in notes_en),
        'notes_ru': notes_ru, 'notes_en': notes_en,
        'price': _money(price) if price else '', 'service_id': str(service.get('service', '')),
        'category': str(service.get('category') or ''), 'confirm': bool(cfg.get('confirm_link', True)),
        'poll': kind == 'votes' and 'poll' in str(service.get('type', '')).lower(),
    }


def _badges(ctx: Dict[str, Any], english: bool = False) -> List[Tuple[str, int]]:
    """(текст, приоритет). Чем больше приоритет, тем дольше значок остаётся в названии."""
    result: List[Tuple[str, int]] = []
    variant = ctx.get('variant') or ''
    low = variant.lower()
    if variant:
        result.append(((ctx['variant_en'] if english else variant).upper(), 7))
    quality = [x for x in ctx['quality_list'] if x != 'БОТЫ' and x.lower() not in ctx['title'].lower() and
               not (low and str(ctx['quality_words'].get(x, '')).lower() in low)][:2]
    if quality:
        text = ' '.join(quality)
        if english:
            text = _en_text(text)
        result.append((text, 3))
    shown = bool(re.search(r'гарант|рефил|refill|guarantee|warranty', low))
    if ctx.get('no_refill') and not shown and not ctx['guarantee_days']:
        result.append(('⛔ NO REFILL' if english else '⛔ БЕЗ ГАРАНТИИ', 4))
    elif shown:
        pass
    elif ctx['guarantee_days']:
        days = ctx['guarantee_days']
        text = (f'♻️ {"LIFETIME" if days >= 3650 else str(days) + "D"} REFILL' if english else
                f'♻️ ГАРАНТИЯ {"НАВСЕГДА" if days >= 3650 else str(days) + " ДН"}')
        result.append((text, 4))
    elif ctx['guarantee']:
        result.append(('♻️ REFILL' if english else '♻️ ГАРАНТИЯ', 4))
    if ctx['speed']:
        result.append(((f'🚀 {_en_text(ctx["speed"]).upper()}' if english else f'🚀 {ctx["speed"].upper()}'), 1))
    elif ctx['start'] == 'моментально':
        result.append(('⚡ INSTANT' if english else '⚡ МОМЕНТАЛЬНО', 1))
    if ctx['per_piece_raw'] > 1:
        result.append(((f'{ctx["per_piece"]} PCS PACK' if english else f'ПАКЕТ {ctx["per_piece"]} ШТ'), 6))
    elif ctx['min_pcs'] > 1:
        result.append(((f'MIN {ctx["min_pcs"]} PCS' if english else f'ОТ {ctx["min_pcs"]} ШТ'), 6))
    result.append(('AUTO 24/7' if english else 'АВТОВЫДАЧА 24/7', 5))
    return result


def _compose_summary(theme: Dict[str, Any], title: str, badges: List[Tuple[str, int]], limit: int) -> str:
    items = list(badges)
    while True:
        badge_text = theme['joiner'].join(text for text, _ in items)
        text = _format(theme['summary'], {'title': title, 'badges': badge_text})
        text = re.sub(r'\s+', ' ', text).strip(' •|✦✔')
        if _utf16_len(text) <= limit or not items:
            break
        if len(items) == 1 and items[0][1] >= 7:
            # Остался только вариант услуги — укорачиваем его, но не убираем.
            base = _format(theme['summary'], {'title': title, 'badges': ''})
            room = limit - _utf16_len(re.sub(r'\s+', ' ', base).strip(' •|✦✔')) - 1
            if room >= 6:
                short = _summary_field(items[0][0], room)
                text = re.sub(r'\s+', ' ', _format(theme['summary'], {'title': title, 'badges': short})).strip(' •|✦✔')
                if _utf16_len(text) <= limit:
                    return text
        items.remove(min(items, key=lambda x: x[1]))
    if _utf16_len(text) > limit:
        text = _summary_field(_format(theme['summary'], {'title': title, 'badges': ''}).strip(' •|✦✔'), limit)
    return text


_QUALITY_EN = {'ЖИВЫЕ': 'REAL', 'БЕЗ СПИСАНИЙ': 'NO DROP', 'СНГ': 'CIS', 'БОТЫ': 'BOTS'}


def _en_text(value: str) -> str:
    value = str(value or '')
    for ru, en in _QUALITY_EN.items():
        value = value.replace(ru, en)
    value = (value.replace('моментально', 'instant').replace('сутки', 'day').replace('час', 'hour')
             .replace('мин', 'min').replace('ч', 'h'))
    return _translit(value) if re.search('[а-яё]', value, re.I) else value


def _theme_description(theme: Dict[str, Any], ctx: Dict[str, Any], english: bool) -> str:
    i = 1 if english else 0
    b = theme['bullet']
    lines = [theme['sep'], f"{ctx['emoji']} {ctx['title_en'] if english else ctx['title']}", theme['sep'], '', theme['intro'][i], '',
             theme['get'][i]]
    if english:
        lines.append(f"{b} 1 pc = {ctx['per_piece']} {ctx['unit_en']}")
        if ctx['clean_name']:
            lines.append(f"{b} 🧾 Service: {_en_phrase(ctx['clean_name'])}")

        if ctx['start']:
            lines.append(f"{b} 🚀 Start: {_en_text(ctx['start'])}")
        if ctx['speed']:
            lines.append(f"{b} 📈 Speed: {_en_text(ctx['speed'])}")
        if ctx['guarantee']:
            lines.append(f"{b} ♻️ Refill guarantee: " + (f"{ctx['guarantee_days']} days" if ctx['guarantee_days'] and ctx['guarantee_days'] < 3650
                                                         else 'lifetime' if ctx['guarantee_days'] else 'yes'))
        if ctx['quality']:
            lines.append(f"{b} 🌍 Audience: {_en_text(ctx['quality'])}")
        if theme.get('details'):
            lines.append(f"{b} 📦 Service limits: {ctx['min']} – {ctx['max']} {ctx['unit_en']}")
    else:
        lines.append(f"{b} 1 шт. = {ctx['per_piece']} {ctx['unit']}")
        if ctx['clean_name']:
            lines.append(f"{b} 🧾 Услуга: {ctx['clean_name']}")

        if ctx['start']:
            lines.append(f"{b} 🚀 Старт: {ctx['start']}")
        if ctx['speed']:
            lines.append(f"{b} 📈 Скорость: {ctx['speed']}")
        if ctx['guarantee']:
            lines.append(f"{b} ♻️ Гарантия от списаний: {ctx['guarantee']}")
        if ctx['quality']:
            lines.append(f"{b} 🌍 Аудитория: {ctx['quality']}")
        if theme.get('details'):
            lines.append(f"{b} 📦 Лимиты услуги: {ctx['min']} – {ctx['max']} {_plural(_int(ctx['max'].replace(' ', '')), KINDS[ctx['kind_key']]['unit'])}")
    lines += ['', theme['how'][i]]
    steps = theme['steps']
    if english:
        flow = [f"Pay for the quantity you need — from {ctx['min_pcs']} to {ctx['max_pcs']} pcs",
                f"Send {ctx['link_hint_en']} to the chat: {ctx['example_link']}"]
        if ctx['poll']:
            flow.append('Send the answer option number when the bot asks')
        if ctx['confirm']:
            flow.append('Confirm the start with "+"')
        flow.append('Check the status any time: check ORDER_NUMBER')
    else:
        flow = [f"Оплатите нужное количество — от {ctx['min_pcs']} до {ctx['max_pcs']} шт.",
                f"Отправьте в чат {ctx['link_hint']}: {ctx['example_link']}"]
        if ctx['poll']:
            flow.append('Укажите номер варианта ответа, когда бот спросит')
        if ctx['confirm']:
            flow.append('Подтвердите запуск знаком «+»')
        flow.append('Проверяйте статус в любой момент: чек НОМЕР_ЗАКАЗА')
    for index, step in enumerate(flow):
        lines.append(f"{steps[min(index, len(steps) - 1)]} {step}")
    lines += ['', theme['warn'][i]]
    for note in (ctx['notes_en'] if english else ctx['notes_ru']):
        lines.append(f'{b} {note}')
    lines += ['', theme['footer'][i]]
    return '\n'.join(lines)


def _template_choice(cfg: Dict[str, Any], profile: Dict[str, Any], override: str = '') -> str:
    """Порядок: шаблон лота/мастера → назначение соцсеть:тип → соцсеть:* → *:тип → тема по умолчанию."""
    if override and override != 'auto':
        return override
    assign = cfg['design'].get('assign', {})
    for key in (f"{profile['platform']}:{profile['kind']}", f"{profile['platform']}:*", f"*:{profile['kind']}"):
        if assign.get(key):
            return assign[key]
    return cfg['design'].get('theme') or 'premium'


def _template_name(cfg: Dict[str, Any], choice: str) -> str:
    if choice.startswith('custom:'):
        item = cfg['design']['custom'].get(choice[7:])
        return '🧩 ' + (item.get('name') if item else 'удалённый шаблон')
    if choice == 'auto':
        return '🎯 по настройкам оформления'
    return THEMES.get(choice, THEMES['premium'])['title']


def render_lot_texts(service: Dict[str, Any], quantity: int, price: float, choice: str, limits: Dict[str, int],
                     cfg: Optional[Dict[str, Any]] = None, tag: str = '') -> Dict[str, str]:
    """Готовит краткое и подробное описание RU/EN с учётом лимитов формы FunPay."""
    cfg = cfg or load_config()
    profile = _service_profile(service)
    ctx = _design_context(service, quantity, price, cfg, profile)
    choice = _template_choice(cfg, profile, choice)
    s_ru, s_en = limits.get('summary_ru', 80), limits.get('summary_en', 80)
    d_ru, d_en = limits.get('desc_ru', 5000), limits.get('desc_en', 5000)
    tag_line = f'\n\n#SMM:{tag}' if tag else ''
    custom = cfg['design']['custom'].get(choice[7:]) if choice.startswith('custom:') else None
    theme = THEMES.get(choice if not custom else 'premium', THEMES['premium'])
    if custom:
        values = {**ctx, 'badges': ' • '.join(text for text, _ in _badges(ctx))}
        summary_ru = _summary_field(_format(custom.get('summary_ru') or '{title}', values), s_ru)
        desc_ru = _format(custom.get('desc_ru') or '', values).strip() or _theme_description(theme, ctx, False)
        values_en = {**values, 'badges': ' • '.join(text for text, _ in _badges(ctx, True)), 'variant': ctx['variant_en'],
                     'requirements': ctx['requirements_en'], 'link_hint': ctx['link_hint_en']}
        summary_en = _summary_field(_format(custom.get('summary_en') or '', values_en), s_en) if custom.get('summary_en') else \
            _compose_summary(theme, ctx['title_en'], _badges(ctx, True), s_en)
        desc_en = _format(custom.get('desc_en') or '', values_en).strip() if custom.get('desc_en') else _theme_description(theme, ctx, True)
    else:
        summary_ru = _compose_summary(theme, ctx['title'], _badges(ctx), s_ru)
        summary_en = _compose_summary(theme, ctx['title_en'], _badges(ctx, True), s_en)
        desc_ru = _theme_description(theme, ctx, False)
        desc_en = _theme_description(theme, ctx, True)
    if not cfg['design'].get('english', True):
        summary_en = _summary_field(_translit(summary_ru), s_en)
        desc_en = _translit(desc_ru)
    summary_en = _summary_field(_latin_only(summary_en), s_en)
    desc_en = _latin_only(desc_en)
    desc_ru = _clip_units(desc_ru, max(1, d_ru - _utf16_len(tag_line))).rstrip() + tag_line
    desc_en = _clip_units(desc_en, max(1, d_en - _utf16_len(tag_line))).rstrip() + tag_line
    return {'summary_ru': summary_ru, 'summary_en': summary_en, 'desc_ru': desc_ru, 'desc_en': desc_en,
            'platform': profile['platform'], 'kind': profile['kind'], 'template': choice}


def _latin_only(text: str) -> str:
    """FunPay отклоняет английские поля с кириллицей («некорректное английское описание»)."""
    text = str(text or '')
    return _translit(text) if re.search('[а-яё]', text, re.I) else text


def _plain_english(text: str) -> str:
    """Запасной вариант EN-текста: только латиница, цифры и пунктуация, без эмодзи."""
    lines = []
    text = _latin_only(text)
    for ru, en in (('—', '-'), ('–', '-'), ('«', '"'), ('»', '"'), ('…', '...'), ('•', '-'), ('×', 'x')):
        text = text.replace(ru, en)
    for line in text.splitlines():
        line = re.sub(r'[^\x20-\x7E]', ' ', line)
        line = re.sub(r'\s+', ' ', line).strip(' -•|')
        if line or (lines and lines[-1]):
            lines.append(line)
    return '\n'.join(lines).strip()


def _with_tag(text: str, tag: str, limit: int = 5000) -> str:
    text = _clip_units(TAG_RE.sub('', str(text or '')).rstrip(), max(0, limit - 16)).rstrip()
    return (text + '\n\n' if text else '') + f'#SMM:{tag}'


def _preview_sample(platform: str, kind: str) -> Dict[str, Any]:
    pinfo, kinfo = _platform_info(platform), KINDS.get(kind, KINDS['generic'])
    return {'service': '1001', 'name': f"{pinfo['name']} {kinfo['en'].title()} | Real | R30 | 5K/Day | Start 0-1h",
            'category': f"{pinfo['name']} {kinfo['en'].title()}", 'rate': '45', 'min': '10', 'max': '50000',
            'type': 'Default', 'refill': True}
