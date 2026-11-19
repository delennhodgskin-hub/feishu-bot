"""Check the identities and retention values that drive anchor/month registration."""
import json
import re

import data_intake as intake


VERIFY_PROMPT = """独立核对当前主播对比截图的两行文字。仅阅读原图，按从左到右的列顺序，逐字抄录顶部的每位主播姓名和“留人能力 / 留存率”一行。
放大辨认姓名的每一个汉字和留存率小数点后的每一位。返回一个JSON对象，两个键分别为“主播姓名”和“留存率”，两个数组长度相同，每列在两个数组中位置相同。
留存率保留截图里的百分号，明确的零照常抄录。缺失或看不清的单元格请用null保留原位置。"""


def _names(result):
    raw = result.get('主播姓名')
    values = raw if isinstance(raw, list) else [raw]
    if not values or any(not isinstance(n, str) or intake.missing(n) for n in values):
        raise ValueError('主播核对未能读清姓名，请补清晰的主播对比图')
    names = [n.strip() for n in values]
    if len(set(names)) != len(names):
        raise ValueError('截图中多列识别为同一姓名，请补清晰的主播对比图')
    return names


def _rates(result, count):
    raw = result.get('留存率')
    values = raw if isinstance(raw, list) else ([None] * count if raw is None else [raw])
    if len(values) != count:
        raise ValueError('留存率与主播列数不一致，请补清晰的主播对比图')
    return values


def verify_anchor_columns(result, image_bytes, recognize):
    """An independent read of the original image; no first-pass guesses in the prompt."""
    names = _names(result)
    try:
        raw = recognize(image_bytes, VERIFY_PROMPT)
        check = intake.parse_result(raw, kind='anchor')
    except Exception as error:
        raise ValueError('主播姓名与留存率的二次核对未完成，请重发清晰原图') from error
    if intake.page_kind(check) != '主播分析':
        raise ValueError('二次核对未识别到主播对比图，请补清晰原图')
    checked_names = _names(check)
    if len(names) != len(checked_names):
        raise ValueError('两次读取的主播人数不一致，请核对姓名列后重发')
    if '留存率' not in check:
        raise ValueError('二次核对缺少留存率行，请补清晰的主播对比图')
    primary_rates = _rates(result, len(names))
    checked_rates = _rates(check, len(names))
    issues = []
    for index, (name, checked, rate, second_rate) in enumerate(
            zip(names, checked_names, primary_rates, checked_rates), 1):
        if name != checked:
            issues.append(f'第{index}列姓名两次读为“{name}”和“{checked}”')
        try:
            same_rate = intake.equivalent(intake.number(rate, percent=True),
                                          intake.number(second_rate, percent=True))
        except ValueError:
            same_rate = False
        if not same_rate:
            issues.append(f'第{index}列留存率两次读为“{rate if rate is not None else "空"}”和“{second_rate if second_rate is not None else "空"}”')
    if issues:
        raise ValueError('；'.join(issues) + '。请截取含完整姓名和留存率的清晰对比图，重新发送')
    audit = {'核对项目': '主播姓名与留存率', '主播姓名': checked_names, '留存率': checked_rates}
    return result, audit


def _manually_verified_name(fields, name):
    """Only accept a human audit entry about this stored name, outside OCR sources."""
    raw = intake.text(fields.get('AI识别结果'))
    if not raw:
        return False
    for part in raw.split('\n---此前记录---\n'):
        try:
            audit = json.loads(part)
        except ValueError:
            continue  # Older stored history can be truncated.
        checks = audit.get('人工原图核对') if isinstance(audit, dict) else None
        if not isinstance(checks, list):
            continue
        for check in checks:
            verified = check.get('字段') if isinstance(check, dict) else None
            entry = verified.get('主播姓名') if isinstance(verified, dict) else None
            if isinstance(entry, dict) and entry.get('核对后') == name:
                return True
    return False


def check_roster(candidates, existing, account, message):
    """Detect a one-character mismatch with a reviewed or manually verified name."""
    known = set()
    for row in existing:
        f = row['fields']
        if intake.text(f.get('所属账号')) != account or intake.text(f.get('数据性质')) == '测试数据':
            continue
        name = intake.text(f.get('主播姓名'))
        if name and (intake.text(f.get('数据状态')) == '已复查' or _manually_verified_name(f, name)):
            known.add(name)
    for row in candidates:
        name = row.get('主播姓名', '')
        if len(name) < 3 or name in known:
            continue
        similar = sorted(n for n in known if len(n) == len(name) and n[0] == name[0]
                         and sum(a != b for a, b in zip(name, n)) == 1)
        if not similar:
            continue
        explicit = re.search(r'确认新主播\s*[:：]\s*' + re.escape(name) + r'(?=$|[\s，。；、,;])', message)
        if not explicit:
            raise ValueError(f'图中姓名“{name}”和本账号已核对姓名“{"、".join(similar)}”相近。'
                             f'请核对原图；若确为另一位主播，在重发文字中注明“确认新主播：{name}”')
