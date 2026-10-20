def _funpay_lot_id(value: Any) -> int:
    if isinstance(value, bool) or value is None:
        return 0
    if isinstance(value, dict):
        for key in ('id', 'lot_id', 'offer_id', 'offerId'):
            if _int(value.get(key)):
                return _int(value.get(key))
        return 0
    if isinstance(value, (int, str)):
        return _int(value)
    for name in ('id', 'lot_id', 'offer_id', 'offerId'):
        if _int(getattr(value, name, 0)):
            return _int(getattr(value, name, 0))
    return 0


def _lot_text(value: Any) -> str:
    for name in ('description', 'summary', 'name', 'title'):
        text = getattr(value, name, None)
        if text:
            return re.sub(r'\s+', ' ', str(text)).strip().lower()
    return ''


def _register_lot(name: str, funpay_id: int, panel_id: str, service_id: Any, quantity: int, domains: List[str], *,
                  source: str, subcategory: Optional[int] = None, batch_id: str = '', tag: str = '', platform: str = '',
                  kind: str = '', template: str = '', price: float = 0.0, markup: Optional[Tuple[str, float]] = None,
                  poll: bool = False, stock: int = 0, tag_written: bool = False, refill: Optional[Dict[str, Any]] = None) -> str:
    with config_tx() as cfg:
        key = _lot_key_by_funpay_id(cfg, funpay_id) if funpay_id else None
        if not key:
            number = 1
            while f'lot_{number}' in cfg['lot_mapping']:
                number += 1
            key = f'lot_{number}'
        old = cfg['lot_mapping'].get(key, {})
        item = {**old, 'name': name, 'funpay_lot_id': funpay_id or None, 'panel_id': str(panel_id), 'service_id': str(service_id),
                'quantity': quantity, 'domains': domains, 'source': old.get('source') or source,
                'subcategory': subcategory or old.get('subcategory'), 'batch_id': batch_id or old.get('batch_id', ''),
                'tag': tag or old.get('tag') or _new_tag(x.get('tag') for x in cfg['lot_mapping'].values()),
                'tag_written': tag_written or old.get('tag_written', False), 'platform': platform, 'kind': kind,
                'template': template or old.get('template', ''), 'poll': poll or old.get('poll', False),
                'created_at': old.get('created_at') or time.time(), 'stock_target': stock or old.get('stock_target', 0)}
        if price:
            item['last_price'] = price
        if markup:
            item['markup_mode'], item['markup_value'] = markup
        if refill:
            item['refill'] = {**_refill_settings(old.get('refill')), **refill}
        cfg['lot_mapping'][key] = item
    return key
