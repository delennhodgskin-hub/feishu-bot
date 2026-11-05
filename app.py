import os, json, base64, requests
from flask import Flask, request, jsonify

app = Flask(__name__)

try:
    CONFIG = json.load(open(os.path.join(os.path.dirname(__file__), "config.json"), encoding="utf-8"))
except (FileNotFoundError, json.JSONDecodeError):
    CONFIG = {}
APP_ID = os.environ.get("FEISHU_APP_ID") or CONFIG["feishu_app_id"]
APP_SECRET = os.environ.get("FEISHU_APP_SECRET") or CONFIG["feishu_app_secret"]
ZHIPU_KEY = os.environ.get("ZHIPU_KEY") or CONFIG["zhipu_key"]
VERIFY_TOKEN = os.environ.get("FEISHU_VERIFY_TOKEN") or CONFIG.get("feishu_verify_token", "")
ENCRYPT_KEY = os.environ.get("FEISHU_ENCRYPT_KEY") or CONFIG.get("feishu_encrypt_key", "")


def decrypt_payload(encrypt_str):
    import hashlib, base64
    from Crypto.Cipher import AES
    key_hash = hashlib.sha256(ENCRYPT_KEY.encode("utf-8")).digest()
    raw = base64.b64decode(encrypt_str)
    iv, encrypted = raw[:16], raw[16:]
    cipher = AES.new(key_hash, AES.MODE_CBC, iv)
    plain = cipher.decrypt(encrypted)
    pad = plain[-1]
    return json.loads(plain[:-pad].decode("utf-8"))

BASE_LIVE = "QGxybhAfFanBrusW5q9cOd2XnUc"
BASE_ANCHOR = "MGY2bjjQpa579LsWnvYcOfPZnqe"
TABLE_LIVE = "tbl4Vmkv2B5zd2Cv"
TABLE_ANCHOR = "tbljoPMRJvyzKupS"
GROUP_CHAT = "oc_ecc73eac1ebd2fe694ef5b3d7687587f"

PROMPT_LIVE = "识别这张抖音电商罗盘直播数据截图。返回JSON，字段：类型(填live)、数据日期(YYYY-MM-DD)、账号(佳能Canon数码旗舰店/Canon佳能摄影摄像旗舰店/汉印拍立得旗舰店)、直播间成交金额、用户支付金额、千次观看成交金额、成交人数、成交件数、转化率(小数)、商品点击-成交率(小数)、平均在线人数、曝光-进入率(小数)、人均观看时长(秒整数)、退款金额、退款金额占比(小数)、新增粉丝数。数值只返数字。只返JSON不要其他文字。"
PROMPT_ANCHOR = """识别这张抖音电商罗盘主播分析数据截图。这是主播对比表，可能显示基础数据或能力分析数据。
返回严格JSON格式（不要markdown代码块），字段：
- 类型：固定填"anchor"
- 数据日期：YYYY-MM-DD格式，如果图里没有就填空字符串""
- 主播姓名：数组，每个主播一个元素（如["张三","李四"]）
- 所属账号：数组，每个主播对应的账号
- 引流-曝光观看率：数组，小数（如0.109表示10.9%）
- 留存率：数组，小数
- 观看互动率：数组，小数
- 新增粉丝：数组，整数
- 观看-关注率：数组，小数
- 新增团人数：数组，整数
- UV价值：数组，数字
- 单小时支付金额：数组，数字（去逗号）
- 最高在线：数组，整数
- 平均在线：数组，整数
- 平均客单价：数组，数字（去逗号）
- 成交金额：数组，数字（去逗号）

重要规则：
1. 所有字段都返回数组（即使只有1个主播）
2. 如果图里某个指标没有显示，对应位置填空字符串""
3. 数值不要带¥、逗号、%等符号，只返纯数字
4. 主播姓名要准确识别，不要编造
5. 只返JSON，不要任何其他文字、不要markdown代码块"""


def get_tenant_token():
    r = requests.post(
        "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal",
        json={"app_id": APP_ID, "app_secret": APP_SECRET}, timeout=15)
    return r.json()["tenant_access_token"]


def download_image(message_id, file_key):
    """下载群消息里的图片资源。
    用消息资源接口 GET /im/v1/messages/{message_id}/resources/{file_key}?type=image，
    而非 GET /im/v1/images/{image_key}——后者要求应用是图片上传者(飞书报 234008
    'The app is not the resource sender')，群里 user 发的截图 bot 无权用该接口下载。"""
    token = get_tenant_token()
    r = requests.get(
        f"https://open.feishu.cn/open-apis/im/v1/messages/{message_id}/resources/{file_key}",
        headers={"Authorization": f"Bearer {token}"},
        params={"type": "image"}, timeout=30)
    if r.status_code != 200:
        raise Exception(f"下载图片HTTP {r.status_code}: {r.text[:200]}")
    return r.content


def detect_image_format(image_bytes):
    """通过文件头魔数检测图片格式，零依赖。
    替代已废弃的 imghdr：imghdr 在 Python 3.11 弃用、3.13 彻底移除，
    render.com 跑 Python 3.13，import imghdr 会直接 ModuleNotFoundError。"""
    if image_bytes.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if image_bytes.startswith(b"\xff\xd8"):
        return "jpeg"
    if image_bytes[:6] in (b"GIF87a", b"GIF89a"):
        return "gif"
    if image_bytes[:4] == b"RIFF" and image_bytes[8:12] == b"WEBP":
        return "webp"
    return "png"  # 飞书截图默认 PNG


def ai_recognize(image_bytes, prompt):
    b64 = base64.b64encode(image_bytes).decode()
    fmt = detect_image_format(image_bytes)
    r = requests.post(
        "https://open.bigmodel.cn/api/paas/v4/chat/completions",
        headers={"Authorization": f"Bearer {ZHIPU_KEY}"},
        json={"model": "glm-4v-flash", "messages": [{"role": "user", "content": [
            {"type": "text", "text": prompt},
            {"type": "image_url", "image_url": {"url": f"data:image/{fmt};base64,{b64}"}}]}]},
        timeout=60)
    data = r.json()
    if "choices" not in data:
        raise Exception(f"智谱返回: {json.dumps(data, ensure_ascii=False)[:300]}")
    return data["choices"][0]["message"]["content"]


def write_record(base, table, fields):
    token = get_tenant_token()
    r = requests.post(
        f"https://open.feishu.cn/open-apis/bitable/v1/apps/{base}/tables/{table}/records",
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        json={"fields": fields}, timeout=15)
    data = r.json()
    if data.get("code") != 0:
        raise Exception(f"飞书写入失败 code={data.get('code')}: {data.get('msg','')}")
    return data


def send_group_msg(text):
    try:
        token = get_tenant_token()
        requests.post(
            "https://open.feishu.cn/open-apis/im/v1/messages",
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
            params={"receive_id_type": "chat_id"},
            json={"receive_id": GROUP_CHAT, "msg_type": "text",
                  "content": json.dumps({"text": text})}, timeout=15)
    except Exception:
        pass  # 发群消息失败不阻断主流程，避免 500


ACCOUNTS = ["佳能Canon数码旗舰店", "Canon佳能摄影摄像旗舰店", "汉印拍立得旗舰店"]
# 账号简称关键词 → 完整账号名（蔡广文可能写"佳能数码旗舰店"而非完整名）
ACCOUNT_KEYWORDS = [
    ("佳能Canon数码", "佳能Canon数码旗舰店"),
    ("佳能数码", "佳能Canon数码旗舰店"),       # 简称
    ("Canon佳能摄影", "Canon佳能摄影摄像旗舰店"),
    ("佳能摄影", "Canon佳能摄影摄像旗舰店"),    # 简称
    ("摄影摄像", "Canon佳能摄影摄像旗舰店"),    # 简称
    ("汉印拍立得", "汉印拍立得旗舰店"),
    ("汉印", "汉印拍立得旗舰店"),              # 简称
    ("拍立得", "汉印拍立得旗舰店"),            # 简称
]


def parse_text_meta(text):
    """从消息文字提取日期和账号（问题4：格式'时间+账号+直播数据'）。
    支持 '2026.11.1主播数据'、'2026-11-01 佳能Canon数码旗舰店 直播数据'、'11.1' 等。
    账号支持简称（如'佳能数码'→佳能Canon数码旗舰店）。
    返回 (date_str 'YYYY-MM-DD' or None, account or None)。"""
    import re
    account = None
    for keyword, full_name in ACCOUNT_KEYWORDS:
        if keyword in text:
            account = full_name
            break
    # 完整日期 2026.11.1 / 2026-11-01
    m = re.search(r"(20\d{2})[.\-/](\d{1,2})[.\-/](\d{1,2})", text)
    if m:
        y, mo, d = m.groups()
        return f"{y}-{int(mo):02d}-{int(d):02d}", account
    # 只有月日 11.1 / 11-01（默认当前年份）
    m = re.search(r"(?<!\d)(\d{1,2})[.\-/](\d{1,2})(?!\d)", text)
    if m:
        mo, d = m.groups()
        from datetime import datetime
        y = datetime.now().year
        return f"{y}-{int(mo):02d}-{int(d):02d}", account
    return None, account


def list_records(base, table, page_size=100):
    """拉取表格已有记录（用于去重判断）。
    注意：飞书表格为空时 data.items 值是 null（key 存在但值为 null），
    此时 .get("items", []) 返回的是 None 而非 []（默认值只在 key 不存在时生效），
    必须显式处理 None，否则 is_duplicate 里 for rec in existing 会报 NoneType。"""
    try:
        token = get_tenant_token()
        r = requests.get(
            f"https://open.feishu.cn/open-apis/bitable/v1/apps/{base}/tables/{table}/records",
            headers={"Authorization": f"Bearer {token}"},
            params={"page_size": page_size}, timeout=15)
        data = r.json()
        if data.get("code") != 0:
            return []
        items = data.get("data", {}).get("items")
        return items if isinstance(items, list) else []
    except Exception:
        return []


def field_text(v):
    """提取飞书字段值的纯文本，用于去重比较（select 返回 {text:...}，text 返回 str）"""
    if v is None:
        return ""
    if isinstance(v, dict):
        return v.get("text", "") or v.get("name", "")
    if isinstance(v, list) and v:
        return field_text(v[0])
    return str(v)


def is_duplicate(fields, existing, rtype):
    """去重判断。
    主播：数据日期+主播姓名+所属账号 相同视为重复（每天每人只一条）
    直播：数据日期+账号+直播间成交金额 相同视为重复（同天同账号不同成交金额=不同场，允许）"""
    if not existing:  # 防御：existing 为 None/[] 时直接返回不重复
        return False
    my_date = fields.get("数据日期")
    if rtype == "anchor":
        my_anchor = fields.get("主播姓名")
        my_account = fields.get("所属账号", "")
        if not my_anchor:
            return False
        for rec in existing:
            rf = rec.get("fields", {})
            if rf.get("数据日期") == my_date and \
               field_text(rf.get("主播姓名")) == str(my_anchor) and \
               field_text(rf.get("所属账号")) == str(my_account):
                return True
    else:
        my_account = fields.get("账号")
        my_amount = fields.get("直播间成交金额")
        if not my_account:
            return False
        for rec in existing:
            rf = rec.get("fields", {})
            if rf.get("数据日期") == my_date and \
               field_text(rf.get("账号")) == str(my_account) and \
               rf.get("直播间成交金额") == my_amount:
                return True
    return False


@app.route("/webhook", methods=["POST"])
def webhook():
    """收到飞书回调后立即返回200（防止飞书因超时重试），
    实际处理逻辑在后台线程执行。用 event_id 去重防止重复处理。"""
    import threading
    raw = request.get_data(as_text=True)
    # challenge 需同步返回（飞书验证URL时发challenge，不触发消息事件）
    try:
        data_check = json.loads(raw) if raw else {}
    except Exception:
        data_check = {}
    if "encrypt" in data_check and ENCRYPT_KEY:
        try:
            data_check = decrypt_payload(data_check["encrypt"])
        except Exception:
            pass
    if "challenge" in data_check:
        return jsonify({"challenge": data_check["challenge"]})
    # event_id 去重：飞书重试推送时 event_id 相同，跳过避免重复处理
    event_id = data_check.get("header", {}).get("event_id", "")
    if event_id and event_id in _PROCESSED_EVENTS:
        return jsonify({"ok": True, "msg": "duplicate event"})
    if event_id:
        _PROCESSED_EVENTS.add(event_id)
        # 只保留最近100个，避免内存无限增长
        if len(_PROCESSED_EVENTS) > 100:
            _PROCESSED_EVENTS.clear()
            _PROCESSED_EVENTS.add(event_id)
    # 后台线程处理消息，立即返回200
    threading.Thread(target=_process_in_background, args=(raw,), daemon=True).start()
    return jsonify({"ok": True})


_PROCESSED_EVENTS = set()  # 已处理的 event_id，防止飞书重试导致重复
_PROCESSED_MSGS = set()    # 已处理的 message_id，双重去重保险


def _process_in_background(raw):
    """后台线程处理飞书消息：解析→下载图片→AI识别→写入表格→群回复。
    所有异常发群里（不返回给飞书，因为已经返回200了）。"""
    try:
        _handle_webhook(raw)
    except Exception as e:
        import traceback
        tb = traceback.format_exc()
        try:
            send_group_msg(f"处理异常: {str(e)[:200]}\n\n--- traceback ---\n{tb[-800:]}")
        except Exception:
            pass


def _handle_webhook(raw):
    try:
        data = json.loads(raw) if raw else {}
    except Exception:
        data = {}
    if "encrypt" in data and ENCRYPT_KEY:
        try:
            data = decrypt_payload(data["encrypt"])
        except Exception:
            pass
    if "challenge" in data:
        return  # challenge 已在 webhook 同步处理
    header = data.get("header", {})
    if header.get("event_type") != "im.message.receive_v1":
        return
    event = data.get("event", {})
    msg = event.get("message", {})
    if msg.get("message_type") != "post":
        return
    # message_id 去重（双重保险）：同一条消息飞书重试时 message_id 相同，跳过
    message_id_dedup = msg.get("message_id", "")
    if message_id_dedup and message_id_dedup in _PROCESSED_MSGS:
        return  # 已处理过这条消息，跳过
    if message_id_dedup:
        _PROCESSED_MSGS.add(message_id_dedup)
        if len(_PROCESSED_MSGS) > 50:
            _PROCESSED_MSGS.clear()
            _PROCESSED_MSGS.add(message_id_dedup)
    content_raw = msg.get("content", "{}")
    try:
        content = json.loads(content_raw) if content_raw else {}
    except Exception:
        content = {}
    if not isinstance(content, dict):
        content = {}
    # 兼容飞书 post 消息多种 content 结构：
    #   {"zh_cn":{"content":[...]}}  （飞书事件推送的标准格式，有 locale 嵌套）
    #   {"title":"","content":[...]} （飞书消息 API 返回的扁平格式，无 locale）
    #   {"content":[...]}            （部分客户端/伪造测试的扁平格式）
    post_obj = content.get("zh_cn") or content.get("en_us") or content
    if not isinstance(post_obj, dict):
        post_obj = {}
    content_rows = post_obj.get("content") or []
    if not isinstance(content_rows, list):
        content_rows = []
    image_keys, text_parts = [], []
    for row in content_rows:
        if not isinstance(row, list):
            continue
        for node in row:
            if not isinstance(node, dict):
                continue
            if node.get("tag") == "img":
                image_keys.append(node.get("image_key"))
            elif node.get("tag") == "text":
                text_parts.append(node.get("text", ""))
    full_text = "".join(text_parts)
    if not image_keys:
        return  # 无图，后台线程直接结束

    # 从消息文字提取日期和账号（问题4：格式"时间+账号+直播数据"，文字覆盖图片识别）
    msg_date, msg_account = parse_text_meta(full_text)
    is_anchor = "主播" in full_text
    prompt = PROMPT_ANCHOR if is_anchor else PROMPT_LIVE
    message_id = msg.get("message_id", "")
    rtype_hint = "anchor" if is_anchor else "live"
    base = BASE_ANCHOR if rtype_hint == "anchor" else BASE_LIVE
    table = TABLE_ANCHOR if rtype_hint == "anchor" else TABLE_LIVE

    from datetime import datetime, timezone, timedelta
    import re
    CST = timezone(timedelta(hours=8))  # 表格时区 Asia/Shanghai

    def build_fields(rec, ai_raw):
        """构造飞书 fields：数据日期转毫秒时间戳、文本转str、数字清洗。
        空值或清洗不出数字的字段跳过（留空待人工复查补），避免 NumberFieldConvFail。"""
        f = {}
        for k, v in rec.items():
            if k == "数据日期":
                try:
                    f[k] = int(datetime.strptime(str(v)[:10], "%Y-%m-%d").replace(tzinfo=CST).timestamp() * 1000)
                except Exception:
                    pass  # 空或格式不对则跳过（datetime 字段留空）
            elif k in ("账号", "主播姓名", "所属账号", "人均观看时长"):
                if str(v).strip():
                    f[k] = str(v)
            else:
                s = str(v).strip().replace(",", "").replace("，", "")
                s = re.sub(r"[¥元%人件秒个次]", "", s)
                m = re.search(r"-?\d+\.?\d*", s)
                if m:
                    num = m.group()
                    f[k] = float(num) if "." in num else int(num)
        f["数据状态"] = "草稿"
        f["AI识别结果"] = ai_raw
        return f

    # 预拉取表格已有记录用于去重（问题2、3：每天每人/每账号只一组，重复发消息不重复登记）
    existing = list_records(base, table)

    # 主播消息特殊处理：多张图可能是同一批主播的不同维度数据（基础数据+能力分析）
    # 需要按主播姓名合并到同一条记录，而非分开识别
    if is_anchor:
        # 收集所有图片的识别结果
        all_anchor_results = []
        for idx, img_key in enumerate(image_keys):
            try:
                img = download_image(message_id, img_key)
                ai_result = ai_recognize(img, prompt)
            except Exception as e:
                errors.append(f"第{idx+1}张图识别失败: {e}")
                continue
            try:
                clean = ai_result.strip()
                if clean.startswith("```"):
                    clean = clean.split("\n", 1)[1].rsplit("```", 1)[0]
                result = json.loads(clean)
            except Exception:
                errors.append(f"第{idx+1}张图JSON解析失败: {ai_result[:120]}")
                continue
            # 用消息文字的日期/账号覆盖
            if msg_date:
                result["数据日期"] = msg_date
            if msg_account:
                result["所属账号"] = msg_account
            result.pop("类型", None)
            all_anchor_results.append((idx, ai_result, result))

        # 按主播姓名合并多张图的数据
        merged_anchors = {}  # 主播姓名 -> 合并后的记录
        for idx, ai_raw, result in all_anchor_results:
            # 拆分多主播
            list_keys = [k for k, v in result.items() if isinstance(v, list)]
            if list_keys:
                n = max(len(result[k]) for k in list_keys)
                for i in range(n):
                    rec = {}
                    for k, v in result.items():
                        rec[k] = v[i] if isinstance(v, list) and i < len(v) else v
                    anchor_name = str(rec.get("主播姓名", "")).strip()
                    if not anchor_name:
                        continue
                    if anchor_name not in merged_anchors:
                        merged_anchors[anchor_name] = {"_ai_raw": ai_raw}
                    # 合并字段（后识别的补充先识别的空值）
                    for k, v in rec.items():
                        if k == "主播姓名":
                            continue
                        existing_val = merged_anchors[anchor_name].get(k)
                        # 只在原值为空或不存在时填充
                        if existing_val is None or str(existing_val).strip() == "":
                            merged_anchors[anchor_name][k] = v
            else:
                anchor_name = str(result.get("主播姓名", "")).strip()
                if not anchor_name:
                    continue
                if anchor_name not in merged_anchors:
                    merged_anchors[anchor_name] = {"_ai_raw": ai_raw}
                for k, v in result.items():
                    if k == "主播姓名":
                        continue
                    existing_val = merged_anchors[anchor_name].get(k)
                    if existing_val is None or str(existing_val).strip() == "":
                        merged_anchors[anchor_name][k] = v

        # 写入合并后的主播记录
        for anchor_name, rec in merged_anchors.items():
            ai_raw = rec.pop("_ai_raw", "")
            rec["主播姓名"] = anchor_name
            fields = build_fields(rec, ai_raw)
            if len(fields) <= 2:
                continue
            if is_duplicate(fields, existing, "anchor"):
                skipped_dups += 1
                continue
            try:
                write_record(base, table, fields)
                total_ok += 1
                all_labels.append(anchor_name)
            except Exception as e:
                errors.append(f"写入失败({anchor_name}): {e}")
    else:
        # 直播消息：每张图是一条独立记录（两张图=两场直播）
        for idx, img_key in enumerate(image_keys):
            try:
                img = download_image(message_id, img_key)
                ai_result = ai_recognize(img, prompt)
            except Exception as e:
                errors.append(f"第{idx+1}张图识别失败: {e}")
                continue
            try:
                clean = ai_result.strip()
                if clean.startswith("```"):
                    clean = clean.split("\n", 1)[1].rsplit("```", 1)[0]
                result = json.loads(clean)
            except Exception:
                errors.append(f"第{idx+1}张图JSON解析失败: {ai_result[:120]}")
                continue

            # 用消息文字的日期/账号覆盖AI识别结果（问题4）
            if msg_date:
                result["数据日期"] = msg_date
            if msg_account:
                result["账号"] = msg_account

            rtype = result.pop("类型", rtype_hint)

            # 多主播拆分：主播对比表一屏多主播，智谱返回数组，拆成多条
            list_keys = [k for k, v in result.items() if isinstance(v, list)]
            if list_keys:
                n = max(len(result[k]) for k in list_keys)
                records = [{} for _ in range(n)]
                for k, v in result.items():
                    for i in range(n):
                        records[i][k] = v[i] if isinstance(v, list) and i < len(v) else v
            else:
                records = [result]

            for rec in records:
                fields = build_fields(rec, ai_result)
                if len(fields) <= 2:  # 只有数据状态+AI识别结果，无有效业务数据，跳过
                    continue
                if is_duplicate(fields, existing, rtype):
                    skipped_dups += 1
                    continue
                try:
                    write_record(base, table, fields)
                    total_ok += 1
                    all_labels.append(rec.get("账号") or rec.get("主播姓名") or "未命名")
                except Exception as e:
                    errors.append(f"写入失败({rec.get('主播姓名') or rec.get('账号') or '?'}): {e}")

    # 汇总群回复
    parts = []
    if total_ok:
        parts.append(f"已识别并登记 {total_ok} 条草稿：{', '.join(all_labels)}")
    if skipped_dups:
        parts.append(f"跳过 {skipped_dups} 条重复记录")
    if errors:
        parts.append("问题：\n" + "\n".join(errors))
    if total_ok:
        parts.append("请复查后改数据状态为已复查。")
    if not parts:
        parts.append("识别完成但无有效数据写入（可能已重复登件或截图无有效数据）。")
    send_group_msg("\n".join(parts))
    return  # 后台线程结束（已返回200给飞书）


@app.route("/", methods=["GET"])
def index():
    return "feishu-bot running"


@app.route("/version", methods=["GET"])
def version():
    """版本和环境变量状态确认（排查部署问题用）"""
    return jsonify({
        "version": "7.6",
        "encrypt_key_set": bool(ENCRYPT_KEY),
        "app_id_set": bool(APP_ID),
        "app_secret_set": bool(APP_SECRET),
        "zhipu_key_set": bool(ZHIPU_KEY),
        "group_chat_set": bool(GROUP_CHAT),
    })


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 3000)))
