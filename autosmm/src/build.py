"""Собирает AutoSMM для FunPay Cardinal и Starvell Cardinal из общих частей."""
import os, sys

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.dirname(HERE)
COMMON_TAIL = ['p02_utils_store.py', 'p03_panel.py', 'p04_design.py']
FUNPAY = ['p01a_head_funpay.py', 'p01b_catalog.py', *COMMON_TAIL, 'p05a_funpay.py', 'p05c_common.py', 'p06_orders.py',
          'p07_background.py', 'p08_ui_core.py', 'p09_ui_create.py', 'p10_ui_settings.py', 'p11_init.py']
STARVELL = ['s01a_head_starvell.py', 'p01b_catalog.py', *COMMON_TAIL, 's05a_starvell.py', 'p05c_common.py', 'p06_orders.py',
            'p07_background.py', 'p08_ui_core.py', 'p09_ui_create.py', 'p10_ui_settings.py', 's11_init_starvell.py']
UI = {'p06_orders.py', 'p07_background.py', 'p08_ui_core.py', 'p09_ui_create.py', 'p10_ui_settings.py'}


def read(name, starvell):
    text = open(os.path.join(HERE, name), encoding='utf-8').read()
    if starvell and name in UI:
        # Тексты для пользователя: «FunPay» → «Starvell». Идентификаторы (funpay_lot_id) — в нижнем регистре и не меняются.
        text = text.replace('FunPay', 'Starvell')
    return text


for target, parts, starvell in (('AutoSMM_LuckyFunpay.py', FUNPAY, False), ('AutoSMM_Starvell.py', STARVELL, True)):
    if 'funpay' in sys.argv[1:] and starvell or 'starvell' in sys.argv[1:] and not starvell:
        continue
    try:
        body = ''.join(read(p, starvell) for p in parts)
    except FileNotFoundError as exc:
        print('skip', target, exc)
        continue
    open(os.path.join(OUT, target), 'w', encoding='utf-8').write(body)
    print(target, body.count('\n'), 'lines')
