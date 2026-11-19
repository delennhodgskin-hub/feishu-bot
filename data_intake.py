"""Screenshot intake rules. Zero is data; missing/conflicting evidence is never guessed."""
import json
import re
from datetime import datetime, timezone, timedelta
from decimal import Decimal, InvalidOperation

CST = timezone(timedelta(hours=8))
PERCENT = {'退款金额占比', '曝光-观看率', '商品曝光-点击率', '商品点击-成交转化率', '留存率'}
ANCHOR = {'直播时长', '直播时段', '直播间用户支付金额', '单小时用户支付金额',
          '最高在线人数', '平均在线人数', '曝光-观看率', '留存率', 'UV价值'}
FUNNEL = {'直播间曝光人数', '直播间观看人数', '曝光-观看率', '商品曝光-点击率', '商品点击-成交转化率'}
DASH = {'开播时间', '直播时长（min）', '直播间成交金额', '退款金额占比',
        '千次观看用户支付金额', '人均观看时长', '平均在线人数'}
ADS = {'综合营销ROI', '整体消耗'}
LIVE = FUNNEL | DASH | ADS
IDENTITY = {'数据日期', '账号', '所属账号', '主播姓名', '开播时间'}
BLANK = {'', '--', '—', '-', 'null', 'none', '未显示', '未识别', '待补数值', '[]'}

PROMPT_LIVE = '''逐字读取本张直播截图，只返回JSON。不要计算、补猜或借用示例数字。
先返回：页面类型（千川/罗盘大屏/成交漏斗/未知）、店铺名、数据日期（YYYY-MM-DD，未显示填null）。
千川页另返回统计口径（全天/单场/未知）；只有日期范围为同一天且没有场次筛选才是全天。
每个指标返回截图上的原始字符串，保留小数点、逗号、万、%和时长单位；未显示、模糊、--返回null。明确0、0.00、0%必须保留。
【千川】只读综合营销ROI、整体消耗两张同名卡片的主数值。不读其下方红绿环比涨跌；不能把整体成交金额、净成交金额、用户实际支付净成交金额当作直播间成交金额或整体消耗。ROI卡片为--时返回null，不能借旁边的百分比。
【罗盘大屏】读顶部开播时间（HH:MM，不读当前电脑时间）、直播时长（min）（原文如X小时Y分钟，不自己换算）、直播间成交金额、退款金额占比、千次观看用户支付金额、人均观看时长（原文）、平均在线人数。
大屏的曝光-观看率（次数）不得写入本项目的曝光-观看率（人数）。大屏不得返回成交漏斗的五个字段。
【成交漏斗】只从左侧成交转化漏斗读：直播间曝光人数、直播间观看人数、曝光-观看率（人数）、商品曝光-点击率（人数）、商品点击-成交转化率（人数）。JSON键仍用曝光-观看率、商品曝光-点击率、商品点击-成交转化率。
三个比例分别紧贴曝光→观看、商品曝光→商品点击、商品点击→成交三段。不能读观看→商品曝光率，不能读最左侧曝光→成交率，不能读右侧互动漏斗的观看→互动率。不要用四舍五入过的人数反推截图比例。
只返回当前页面允许的字段，所有值保留原文。开播时间、日期在图里没有时填null，不根据其他截图猜测。
返回一个JSON对象（不是数组或字符串），固定包含“页面特征原文”（图上能看到的标题文字）和“开播时间原文”（复制顶部开播信息的完整原文；没有则null）。
页面区分：巨量千川/千川乘方是千川；深色背景且顶部标题直播大屏的是罗盘大屏；有成交转化漏斗/互动转化漏斗的白色页面是成交漏斗。页面类型不能只因为有直播数据就都填罗盘大屏。
开播时间只能取“开播时间 ... 开播”附近的时刻；必须同步抄录这段开播时间原文。千川页面更新时间、日期选择器、评论时间、视频时长、数据过期提示、折线图横轴刻度都不是开播时间。漏斗图没有顶部开播信息时，开播时间及原文必须为null。
千川数据日期取右上日期选择器，不取“XX月XX日XX:XX更新”；原文日期范围可另填“统计日期原文”。不要把当天千川ROI和消耗当作另一个直播场次。'''

PROMPT_ANCHOR = '''逐字读取抖音罗盘主播对比表，只返回JSON，不计算或猜测。
页面类型固定为主播分析。数据日期、店铺名可为字符串；未显示填null。
按左至右主播姓名的列顺序返回等长数组：主播姓名、所属账号、直播时长、直播时段、直播间用户支付金额、单小时用户支付金额、最高在线人数、平均在线人数、曝光-观看率、留存率、UV价值。
直播时长在“概要数据”下面第一行，逐主播对齐，保留X小时Y分钟原文。时长不是直播时段；没有开始结束时刻就让直播时段为null。
所有数值逐字抄录，保留小数点、逗号、货币符号、万、百分号。不要把金额取整或丢小数点。
明确显示0、0.00、0%必须返回原始零值，不能当空值；未显示、模糊、--才返回null。所有缺项保留对应数组位置，不移动其他主播的数据。不要读颜色推断数值。
首选返回一个对象，其“主播姓名”等字段为数组，不是对象外包引号的JSON字符串。结构示意（null只是结构占位，实际值以图为准）：
{"页面类型":"主播分析","数据日期":null,"店铺名":null,"主播姓名":[],"所属账号":[],"直播时长":[],"直播时段":[],"直播间用户支付金额":[],"单小时用户支付金额":[],"最高在线人数":[],"平均在线人数":[],"曝光-观看率":[],"留存率":[],"UV价值":[]}'''

def text(v):
    if v is None:
        return ''
    if isinstance(v, list):
        return ''.join(text(x) for x in v)
    if isinstance(v, dict):
        return str(v.get('text') or v.get('name') or '')
    return str(v).strip()

def missing(v):
    return v is None or (isinstance(v, str) and v.strip().lower() in BLANK) or v == []

def number(v, percent=False):
    if missing(v):
        return None
    if isinstance(v, bool) or isinstance(v, (list, dict)):
        raise ValueError('数值类型异常')
    s = str(v).strip().replace('，', ',').replace('％', '%')
    if s.startswith('.'):
        s = '0' + s
    # Only one complete value: do not extract a convenient number from comparison text.
    m = re.fullmatch(r'[¥￥]?\s*([+-]?(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?)\s*(万|亿)?\s*(%|元|人|次|件|个)?', s)
    if not m:
        raise ValueError('数值含歧义或非单一指标')
    try:
        n = Decimal(m[1].replace(',', '')) * {'万': 10000, '亿': 100000000, None: 1}[m[2]]
    except InvalidOperation as e:
        raise ValueError('无效数值') from e
    if m[3] == '%' and not percent:
        raise ValueError('金额/人数/ROI不能使用百分比变化值')
    if percent:
        if m[2]:
            raise ValueError('百分比不能有万/亿单位')
        if isinstance(v, str) and m[3] != '%' and 0 < n < 1:
            raise ValueError('小于1的百分比字符串缺少百分号，无法确定比例单位')
        # Explicit % always divides, including 0.98%; numeric fractions remain fractions.
        if m[3] == '%' or n > 1:
            n /= 100
        if n < 0 or n > 1:
            raise ValueError('百分比超出0至100%')
    elif n < 0:
        raise ValueError('此业务指标不接受负值或同比变化值')
    return int(n) if n == n.to_integral_value() else float(n)

def duration(v, seconds=False):
    if missing(v):
        return None
    s = str(v).strip().removeprefix('共').replace(' ', '')
    if re.fullmatch(r'\d+(?:\.\d+)?', s):
        value = Decimal(s) * (1 if seconds else 60)
    else:
        m = re.fullmatch(r'(?:(\d+(?:\.\d+)?)小时)?(?:(\d+(?:\.\d+)?)(?:分钟|分))?(?:(\d+(?:\.\d+)?)秒)?', s)
        if not m or not any(x is not None for x in m.groups()):
            raise ValueError('时长格式无法确定')
        value = sum(Decimal(x or '0') * factor for x, factor in zip(m.groups(), (3600, 60, 1)))
    if value < 0 or value > 172800:
        raise ValueError('时长超过48小时，需核实单位')
    if seconds:
        return format(value.normalize(), 'f')
    minutes = value / 60
    # The existing native formulas accept Chinese hours/minutes or plain minutes.
    if minutes == minutes.to_integral_value():
        return f'{int(minutes)//60}小时{int(minutes)%60}分钟'
    return format(minutes.normalize(), 'f')

def start_time(v):
    if missing(v):
        return None
    s = text(v).replace('：', ':')
    m = re.fullmatch(r'(\d{1,2}):(\d{2})(?::\d{2})?', s)
    if not m or int(m[1]) > 23 or int(m[2]) > 59:
        raise ValueError('开播时间需HH:MM')
    return f'{int(m[1]):02d}:{m[2]}'

def message_time(message):
    times = re.findall(r'(?<!\d)([0-2]?\d[:：][0-5]\d)(?!\d)', message)
    values = {start_time(t) for t in times}
    if len(values) > 1:
        raise ValueError('一条消息包含多场开播时间，请分场发送')
    return next(iter(values), None)

def timestamp(v):
    if isinstance(v, (int, float)):
        return int(v)
    return int(datetime.strptime(str(v), '%Y-%m-%d').replace(tzinfo=CST).timestamp() * 1000)

def equivalent(a, b):
    if missing(a) or missing(b):
        return missing(a) and missing(b)
    try:
        return Decimal(str(a)) == Decimal(str(b))
    except InvalidOperation:
        return text(a) == text(b)

def normalize(key, value):
    if key in ('直播时长', '直播时长（min）'):
        return duration(value)
    if key == '人均观看时长':
        return duration(value, seconds=True)
    if key == '开播时间':
        return start_time(value)
    if key == '直播时段':
        return text(value) or None
    return number(value, key in PERCENT)

def parse_result(raw, kind=None):
    """Accept bounded common JSON envelopes without guessing missing anchor columns."""
    r = raw
    for _ in range(3):
        if not isinstance(r, str):
            break
        s = r.strip().lstrip('\ufeff')
        fence = re.fullmatch(r'```(?:json)?\s*([\s\S]*?)\s*```', s, re.IGNORECASE)
        if fence:
            s = fence[1]
        try:
            r = json.loads(s)
        except (ValueError, TypeError) as e:
            raise ValueError('识别返回的JSON格式不完整') from e
    metadata = {}
    wrapper_keys = ('data', 'result', 'results', 'records', 'anchors', '主播数据', '主播列表', '识别结果', '数据')
    meta_keys = {'页面类型', '页面特征原文', '数据日期', '店铺名', '所属账号', '账号', '类型',
                 '开播时间原文', '统计日期原文', '统计口径'}
    business = ANCHOR | LIVE | {'主播姓名', '直播时长(min)'}
    for _ in range(3):
        if isinstance(r, list) and len(r) == 1 and isinstance(r[0], dict):
            r = r[0]
            continue
        if not isinstance(r, dict) or any(k in business for k in r):
            break
        candidates = [k for k in wrapper_keys if isinstance(r.get(k), (dict, list))]
        if len(candidates) != 1:
            break
        for k in meta_keys:
            if k in r and not missing(r[k]):
                if k in metadata and not equivalent(metadata[k], r[k]):
                    raise ValueError(f'识别结果包装层的{k}不一致')
                metadata[k] = r[k]
        r = r[candidates[0]]
    if isinstance(r, list):
        if not r or not all(isinstance(row, dict) for row in r):
            raise ValueError('识别未返回有效主播记录，请补清晰原图')
        if kind == 'live' or not all(text(row.get('主播姓名')) for row in r):
            raise ValueError('单张图返回多组非主播数据，需重新识别')
        keys = set().union(*(row.keys() for row in r)) | set(metadata)
        columns = {k: [] for k in keys}
        for row in r:
            if page_kind(row) not in ('', '主播分析'):
                raise ValueError('主播列表混有其他页面，需重新识别')
            names = row['主播姓名'] if isinstance(row['主播姓名'], list) else [row['主播姓名']]
            if not names or any(not text(n) or isinstance(n, (dict, list)) for n in names):
                raise ValueError('主播姓名列格式不完整')
            for i in range(len(names)):
                for key in keys:
                    value = row.get(key, metadata.get(key))
                    if isinstance(value, list):
                        if key in ANCHOR and len(value) != len(names):
                            raise ValueError(f'{key}数组与主播人数不同，需重新识别并保留缺项位置')
                        value = value[i] if i < len(value) else None
                    elif len(names) > 1 and key in ANCHOR and not missing(value):
                        raise ValueError(f'{key}未按主播列返回，无法确定归属')
                    if key in metadata and not missing(value) and key in row and not equivalent(value, metadata[key]):
                        raise ValueError(f'识别结果内外层的{key}不一致')
                    columns[key].append(value)
        columns['页面类型'] = '主播分析'
        r = columns
    elif isinstance(r, dict):
        r = dict(r)
        for k, v in metadata.items():
            if k in r and not missing(r[k]) and not equivalent(r[k], v):
                raise ValueError(f'识别结果内外层的{k}不一致')
            r.setdefault(k, v)
    else:
        raise ValueError('识别未返回可登记的数据结构，请补清晰原图')
    if kind == 'anchor' and not text(r.get('页面类型')) and text(r.get('主播姓名')):
        r['页面类型'] = '主播分析'
    if kind == 'anchor' or page_kind(r) == '主播分析':
        names = r.get('主播姓名')
        names = names if isinstance(names, list) else [names]
        if not names or any(not text(n) or isinstance(n, (dict, list)) for n in names):
            raise ValueError('主播姓名未识别完整，请重新读取顶部主播列')
        for key in ANCHOR:
            value = r.get(key)
            if isinstance(value, list) and len(value) != len(names):
                raise ValueError(f'{key}数组与主播人数不同，需重新识别并保留缺项位置')
            if len(names) > 1 and not isinstance(value, list) and not missing(value):
                raise ValueError(f'{key}未按主播列返回，无法确定归属')
    return r


def header_identity(result):
    """Read only a labelled start header; bare clocks/graph ticks are not identity."""
    caption = text(result.get('开播时间原文'))
    if not caption:
        return None
    date = r'(20\d{2})[./年-](\d{1,2})[./月-](\d{1,2})日?'
    clock = r'([0-2]?\d[:：][0-5]\d)(?::\d{2})?'
    match = re.search(r'(?:开播时间|开播于)\s*[:：]?\s*(?:' + date + r'\s*)?' + clock, caption)
    if not match:
        match = re.search(date + r'\s*' + clock + r'\s*开播', caption)
    if not match:
        return None
    y, month, day, at = match.groups()
    when = start_time(at)
    shown_date = f'{y}-{int(month):02d}-{int(day):02d}' if y else None
    if shown_date:
        timestamp(shown_date)
    return {'time': when, 'date': shown_date, 'caption': caption}


def page_kind(result):
    features = text(result.get('页面特征原文'))
    # Visible source titles outrank a generic model-assigned page category.
    if re.search(r'巨量千川|千川乘方', features):
        return '千川'
    if '成交转化漏斗' in features or '互动转化漏斗' in features:
        return '成交漏斗'
    if '直播大屏' in features:
        return '罗盘大屏'
    if '主播分析' in features or '对比主播' in features:
        return '主播分析'
    page = text(result.get('页面类型'))
    return {'巨量千川': '千川', '千川乘方': '千川', '直播大屏': '罗盘大屏',
            '罗盘直播大屏': '罗盘大屏', '直播转化漏斗': '成交漏斗',
            '成交转化漏斗': '成交漏斗', '主播对比': '主播分析'}.get(page, page)


def recognition_retry_reason(result, kind):
    if kind == 'anchor':
        if page_kind(result) != '主播分析':
            return '未确认是主播对比表，请重读页面标题及顶部主播姓名列'
        return None
    page = page_kind(result)
    if page not in {'千川', '罗盘大屏', '成交漏斗'}:
        return '页面来源未识别，请逐字抄录页面标题并重新区分千川、大屏、成交漏斗'
    if page == '罗盘大屏' and not header_identity(result):
        return '缺少有效开播时间原文，请重看顶部开播信息；图表横轴和页面更新时间不能当开播时间'
    return None


def resolve_live_source(result, index):
    source, issues = dict(result), []
    page = page_kind(result)
    source['页面类型'] = page
    claimed = source.pop('开播时间', None)
    header = header_identity(source) if page == '罗盘大屏' else None
    if header:
        source['开播时间'] = header['time']
        if not missing(claimed):
            try:
                if start_time(claimed) != header['time']:
                    issues.append(f'第{index}张开播时间按顶部原文校正为{header["time"]}')
            except ValueError:
                issues.append(f'第{index}张开播时间按顶部原文提取')
        if header['date']:
            source['数据日期'] = header['date']
    elif not missing(claimed):
        issues.append(f'第{index}张时间缺少大屏顶部开播依据，未用于场次判断')
    return source, issues

def merge_results(results, kind, date, account, session=None):
    if not date or not account:
        raise ValueError('请在文字中注明数据日期和账号，不从上传日期猜业务日期')
    day = timestamp(date)
    records, issues, conflicts = {}, [], set()
    session_evidence = {}
    for index, result in enumerate(results, 1):
        if kind == 'live':
            result, source_issues = resolve_live_source(result, index)
            issues.extend(source_issues)
            if result.get('开播时间'):
                session_evidence.setdefault(result['开播时间'], []).append(index)
        # An explicit screenshot date contradicting the message must be resolved, not overwritten.
        shown_date = result.get('数据日期')
        shown_dates = shown_date if isinstance(shown_date, list) else [shown_date]
        for d in shown_dates:
            if not missing(d) and timestamp(d) != day:
                raise ValueError(f'第{index}张截图日期与文字日期不同，请核对后重发')
        page = page_kind(result)
        if kind == 'anchor':
            if page != '主播分析':
                issues.append(f'第{index}张未确认是主播分析表，未取值')
                continue
            names = result.get('主播姓名') or []
            names = names if isinstance(names, list) else [names]
        else:
            if page not in {'千川', '罗盘大屏', '成交漏斗'}:
                issues.append(f'第{index}张页面类型未确认，未取值')
                continue
            names = ['live']
        for i, name in enumerate(names):
            name = text(name)
            if not name:
                continue
            rec = records.setdefault(name, {'数据日期': day, '所属账号' if kind == 'anchor' else '账号': account})
            if kind == 'anchor':
                rec['主播姓名'] = name
            allowed = ANCHOR if kind == 'anchor' else {'千川': ADS, '罗盘大屏': DASH, '成交漏斗': FUNNEL}[page]
            source = dict(result)
            for alias in ('直播时长(min)', '直播时长（分钟）'):
                if alias in source and '直播时长（min）' not in source:
                    source['直播时长（min）'] = source[alias]
            for key in allowed:
                raw = source.get(key)
                if isinstance(raw, list):
                    raw = raw[i] if i < len(raw) else None
                elif kind == 'anchor' and len(names) > 1 and not missing(raw):
                    issues.append(f'{name} {key}未按主播列返回数组，未取值')
                    continue
                if missing(raw):
                    continue
                try:
                    value = normalize(key, raw)
                except ValueError as e:
                    issues.append(f'{name} {key}：{e}')
                    continue
                tag = (name, key)
                if tag in conflicts:
                    continue
                if key in rec and not equivalent(rec[key], value):
                    del rec[key]
                    conflicts.add(tag)
                    issues.append(f'{name} {key}：多图数值冲突，未写入；请单独补清晰截图')
                else:
                    rec[key] = value
            if page == '千川':
                scope = text(result.get('统计口径'))
                if scope != '全天':
                    for key in ADS:
                        rec.pop(key, None)
                        conflicts.add((name, key))
                    issues.append('千川统计口径不是已确认全天；投放指标未写入，请补同日全天截图')
                else:
                    rec['_ad_scope'] = '全天'
    if kind == 'live' and records:
        rec = records['live']
        if ('live', '开播时间') in conflicts:
            evidence = '；'.join('第' + '/'.join(map(str, indexes)) + '张顶部开播' + when
                                for when, indexes in session_evidence.items())
            raise ValueError(f'开播信息需核对：{evidence}。请按场分别发送，或重发顶部清晰原图')
        if session:
            if rec.get('开播时间') and rec['开播时间'] != session:
                raise ValueError('截图开播时间与文字不同，请核对场次')
            rec['开播时间'] = session
    return list(records.values()), issues

def select_target(fields, existing, kind, correction=False):
    account_key = '所属账号' if kind == 'anchor' else '账号'
    keys = ['数据日期', account_key] + (['主播姓名'] if kind == 'anchor' else [])
    rows = [r for r in existing if all(equivalent(r['fields'].get(k), fields.get(k)) for k in keys)
            and text(r['fields'].get('数据性质')) != '测试数据']
    if kind == 'live':
        session = fields.get('开播时间')
        if not session:
            raise ValueError('直播按场登记，请在文字注明开播时间HH:MM（补漏斗图也需要）')
        exact = [r for r in rows if text(r['fields'].get('开播时间')) == session]
        if not exact and any(missing(r['fields'].get('开播时间')) for r in rows):
            raise ValueError('已有记录缺开播时间，请先在表内补正确时间，避免重复登记')
        rows = exact
    if len(rows) > 1:
        raise ValueError('匹配到多条记录，请先核实重复项，未覆盖任意一条')
    if not rows and correction:
        raise ValueError('更正未找到原记录，请核对日期、账号、主播或开播时间')
    return rows[0] if rows else None

def route_daily_ads(fields, existing, target):
    """Keep one daily owner. A different session can never duplicate daily spend."""
    f = dict(fields)
    scope = f.pop('_ad_scope', None)
    same_day = [r for r in existing if equivalent(r['fields'].get('数据日期'), f['数据日期'])
                and text(r['fields'].get('账号')) == f['账号']
                and text(r['fields'].get('数据性质')) != '测试数据']
    owners = [r for r in same_day if text(r['fields'].get('投放统计口径')).startswith('全天主记录')]
    if len(owners) > 1:
        raise ValueError('同日存在多条全天投放主记录，请先核实，未新增重复投放')
    target_id = target.get('record_id') if target else None
    if owners and owners[0]['record_id'] != target_id:
        owner = owners[0]
        for key in ADS:
            if key in f and not equivalent(f[key], owner['fields'].get(key)):
                raise ValueError(f'全天{key}与已存主记录不同；请使用主记录开播时间发送更正')
            f.pop(key, None)
        f['投放统计口径'] = '同日共用：全天ROI及消耗见' + text(owner['fields'].get('开播时间')) + '场，不重复累计'
    elif scope == '全天':
        if any(r['record_id'] != target_id and any(not missing(r['fields'].get(k)) for k in ADS) for r in same_day):
            raise ValueError('同日其他记录已有未标口径的投放，请先核实归属')
        f['投放统计口径'] = '全天主记录：ROI及整体消耗为同账号同日全天，仅此行计入'
    elif not owners and not any(not missing(f.get(k)) for k in ADS):
        f['投放统计口径'] = '待补全天投放：本场可先登记；同日千川图补齐后自动共用'
    return f

def plan_patch(fields, target, correction=False):
    allowed = ANCHOR | LIVE | IDENTITY | {'投放统计口径'}
    clean = {k: v for k, v in fields.items() if k in allowed and not missing(v)}
    if not target:
        return clean, []
    old = target['fields']
    patch, conflicts = {}, []
    for key, value in clean.items():
        if equivalent(old.get(key), value):
            continue
        if key in IDENTITY:
            if missing(old.get(key)):
                patch[key] = value
            else:
                raise ValueError('不能通过识别修改记录身份字段')
        elif missing(old.get(key)) or correction or key == '投放统计口径':
            patch[key] = value
        else:
            conflicts.append(f'{key}已有{old.get(key)}，本次识别{value}；需发送“更正”才覆盖')
    return patch, conflicts

def append_attachments(old, tokens):
    seen, result = set(), []
    for item in (old or []) + [{'file_token': t} for t in tokens]:
        token = item.get('file_token')
        if token and token not in seen:
            seen.add(token)
            result.append({'file_token': token})
    return result
