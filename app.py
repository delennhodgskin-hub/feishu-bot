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

PROMPT_LIVE = """识别这张抖音电商直播数据截图（可能是巨量千川直播页、抖音电商罗盘直播大屏、或直播转化漏斗页）。
首先看截图右上角的店铺名称，在JSON里填"店铺名"字段。
返回JSON，字段：
- 类型：固定填"live"
- 店铺名：截图右上角显示的店铺名称（如"佳能Canon数码旗舰店"，如果看不清或没有就填空字符串""）
- 数据日期：YYYY-MM-DD格式，如果图里没有就填空字符串""
- 账号：佳能Canon数码旗舰店/Canon佳能摄影摄像旗舰店/汉印拍立得旗舰店
- 综合营销ROI：数字（如48.34）
- 整体消耗：数字（去¥和逗号）
- 直播时长（min）：分钟数
- 直播间成交金额：数字（去¥和逗号）
- 退款金额占比：小数（如0.6505表示65.05%）
- 千次观看用户支付金额：数字（去¥和逗号）
- 人均观看时长：秒整数
- 平均在线人数：整数
- 直播间曝光人数：整数（如"37.64万"转成376400）
- 曝光-观看率：小数（如0.1567表示15.67%）
- 直播间观看人数：整数（如"5.9万"转成59000）
- 商品曝光-点击率：小数，指商品曝光后用户点击的比例（转化漏斗"商品曝光→商品点击"那层的转化率，约18%，注意不要和"观看→商品曝光"的转化率约76%混淆）
- 商品点击-成交转化率：小数，指商品点击后成交的比例（转化漏斗"商品点击→成交"那层的转化率，约0.92%）

重要规则：
1. 只返这张图里显示的指标，没有显示的指标填空字符串""
2. 数值不要带¥、逗号、万、%等符号，"万"要换算成纯数字（如5.9万=59000）
3. 只返JSON，不要任何其他文字、不要markdown代码块"""
PROMPT_ANCHOR = """识别这张抖音电商罗盘主播分析数据截图。这是主播对比表，可能显示基础数据或能力分析数据。
返回严格JSON格式（不要markdown代码块），字段：
- 类型：固定填"anchor"
- 数据日期：YYYY-MM-DD格式，如果图里没有就填空字符串""
- 主播姓名：数组，每个主播一个元素（如["张三","李四"]）
- 所属账号：数组，每个主播对应的账号
- 直播间用户支付金额：数组，数字（去逗号）
- 单小时用户支付金额：数组，数字（去逗号）
- 最高在线人数：数组，整数
- 平均在线人数：数组，整数
- 曝光-观看率：数组，小数（如0.1567表示15.67%）
- 留存率：数组，小数
- UV价值：数组，数字

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


def upload_attachment(image_bytes, filename, base_token):
    """上传图片到飞书表格附件字段，返回 file_token。
    用 drive/v1/medias/upload_all 接口，parent_type=bitable_file。"""
    token = get_tenant_token()
    fmt = detect_image_format(image_bytes)
    if not filename.endswith(f".{fmt}"):
        filename = filename.rsplit(".", 1)[0] + f".{fmt}"
    size = len(image_bytes)
    r = requests.post(
        "https://open.feishu.cn/open-apis/drive/v1/medias/upload_all",
        headers={"Authorization": f"Bearer {token}"},
        data={"file_name": filename, "parent_type": "bitable_file",
              "parent_node": base_token, "size": str(size)},
        files={"file": (filename, image_bytes)},
        timeout=30)
    data = r.json()
    if data.get("code") != 0:
        raise Exception(f"上传附件失败 code={data.get('code')}: {data.get('msg','')}")
    return data.get("data", {}).get("file_token", "")


def download_media(message_id, file_key):
    """下载群消息里的视频/文件资源。
    用消息资源接口 type=file（download_image 用 type=image 只能下图片）。"""
    token = get_tenant_token()
    r = requests.get(
        f"https://open.feishu.cn/open-apis/im/v1/messages/{message_id}/resources/{file_key}",
        headers={"Authorization": f"Bearer {token}"},
        params={"type": "file"}, timeout=120)
    if r.status_code != 200:
        raise Exception(f"下载视频HTTP {r.status_code}: {r.text[:200]}")
    return r.content


def upload_video_to_drive(video_bytes, file_name):
    """上传视频到飞书云空间根目录，返回 file_token 和可访问链接。"""
    token = get_tenant_token()
    size = len(video_bytes)
    r = requests.post(
        "https://open.feishu.cn/open-apis/drive/v1/files/upload_all",
        headers={"Authorization": f"Bearer {token}"},
        data={"file_name": file_name, "parent_type": "explorer",
              "parent_node": "", "size": str(size)},
        files={"file": (file_name, video_bytes)},
        timeout=180)
    data = r.json()
    if data.get("code") != 0:
        raise Exception(f"视频上传云空间失败 code={data.get('code')}: {data.get('msg','')}")
    file_token = data.get("data", {}).get("file_token", "")
    # 飞书云空间文件链接格式
    share_url = f"https://fcndv9la878k.feishu.cn/file/{file_token}"
    return file_token, share_url


def update_record(base, table, record_id, fields):
    """更新飞书表格已有记录的部分字段（用于补充录屏链接）。"""
    token = get_tenant_token()
    r = requests.put(
        f"https://open.feishu.cn/open-apis/bitable/v1/apps/{base}/tables/{table}/records/{record_id}",
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        json={"fields": fields}, timeout=15)
    data = r.json()
    if data.get("code") != 0:
        raise Exception(f"飞书更新失败 code={data.get('code')}: {data.get('msg','')}")
    return data


def _handle_recording(message_id, full_text, media_files):
    """处理单独发送的录屏视频消息：下载视频→上传云空间→查找主播记录→更新录屏链接。
    触发条件：消息无图片、含"录屏"关键词、有视频文件（media节点）。
    文字格式如"@看数据 小琦 录屏"，从中提取主播名"小琦"。"""
    import re
    from datetime import datetime, timezone, timedelta
    CST = timezone(timedelta(hours=8))

    # 从文字提取主播名（去掉@用户名、去掉"录屏"关键词后的剩余文字）
    anchor_name = full_text.replace("录屏", "").strip()
    anchor_name = re.sub(r"@\S+", "", anchor_name).strip()
    if not anchor_name:
        send_group_msg("录屏处理：未识别到主播姓名，请在消息里写明主播名+录屏")
        return

    base = BASE_ANCHOR
    table = TABLE_ANCHOR

    for mf in media_files:
        file_key = mf.get("file_key", "")
        if not file_key:
            continue
        try:
            # 1. 下载视频（消息资源接口 type=file）
            video = download_media(message_id, file_key)
            # 2. 上传到飞书云空间，获取 file_token 和分享链接
            file_name = mf.get("file_name", "录屏.mp4")
            if "." not in file_name:
                file_name = file_name + ".mp4"
            file_token, share_url = upload_video_to_drive(video, file_name)

            # 3. 查找该主播今天的记录
            existing = list_records(base, table)
            today = datetime.now(CST).strftime("%Y-%m-%d")
            updated = False
            for rec in existing:
                rec_fields = rec.get("fields", {})
                rec_anchor = str(rec_fields.get("主播姓名", ""))
                rec_date = rec_fields.get("数据日期", "")
                # 日期是毫秒时间戳，转成 YYYY-MM-DD
                if isinstance(rec_date, (int, float)) and rec_date > 1000000000:
                    rec_date_str = datetime.fromtimestamp(rec_date / 1000, tz=CST).strftime("%Y-%m-%d")
                else:
                    rec_date_str = str(rec_date)[:10]

                # 模糊匹配主播名 + 今天日期
                if anchor_name in rec_anchor and today == rec_date_str:
                    # 找到第一个空的录屏链接字段填入
                    link_fields = ["录屏链接1", "录屏链接2", "录屏链接3"]
                    update_data = {}
                    for lf in link_fields:
                        if not rec_fields.get(lf):
                            update_data[lf] = share_url
                            break
                    if update_data:
                        update_record(base, table, rec.get("record_id"), update_data)
                        updated = True
                        break

            if updated:
                send_group_msg(f"录屏已收集：主播 {anchor_name} 的录屏已登记到今日记录")
            else:
                send_group_msg(f"录屏已上传：{share_url}（未找到 {anchor_name} 今天的主播记录，请手动登记链接）")
        except Exception as e:
            send_group_msg(f"录屏处理失败({anchor_name}): {e}")


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
    image_keys, text_parts, link_urls, media_files = [], [], [], []
    for row in content_rows:
        if not isinstance(row, list):
            continue
        for node in row:
            if not isinstance(node, dict):
                continue
            tag = node.get("tag")
            if tag == "img":
                image_keys.append(node.get("image_key"))
            elif tag == "text":
                text_parts.append(node.get("text", ""))
            elif tag == "a":
                href = node.get("href", "")
                if href:
                    link_urls.append(href)
            elif tag in ("media", "video", "file"):
                media_files.append({"file_key": node.get("file_key", ""), "file_name": node.get("file_name", "")})
    full_text = "".join(text_parts)
    # 从文字里提取URL（录屏链接可能以纯文字形式发送）
    import re as _re
    for u in _re.findall(r'https?://[^\s，,\）\)]+', full_text):
        if u not in link_urls:
            link_urls.append(u)
    # 视频文件名也作为录屏链接的候选（占位，后续用户可替换成分享链接）
    for mf in media_files:
        fn = mf.get("file_name", "")
        if fn and fn not in link_urls:
            link_urls.append(fn)
    if not image_keys:
        # 无图片：可能是单独发送的录屏视频消息
        # 如果消息含"录屏"关键词且有视频文件 → 走录屏收集流程
        if media_files and "录屏" in full_text:
            message_id = msg.get("message_id", "")
            _handle_recording(message_id, full_text, media_files)
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

    # 表格字段白名单：只处理这些字段名，智谱返回的其他字段名一律忽略（避免 FieldNameNotFound）
    if rtype_hint == "anchor":
        allowed = {"主播姓名", "所属账号", "数据日期", "直播间用户支付金额", "单小时用户支付金额",
                   "最高在线人数", "平均在线人数", "曝光-观看率", "留存率", "UV价值"}
    else:
        allowed = {"数据日期", "账号", "综合营销ROI", "整体消耗", "直播时长（min）",
                   "直播间成交金额", "退款金额占比", "千次观看用户支付金额", "人均观看时长",
                   "平均在线人数", "直播间曝光人数", "曝光-观看率", "直播间观看人数",
                   "商品曝光-点击率", "商品点击-成交转化率"}

    # 百分比类型字段：飞书百分比字段存储小数(如0.1325表示13.25%)
    # 智谱可能返回百分比数值(如13.25)，需除以100转成小数，否则飞书会显示1325%
    PERCENT_FIELDS = {"退款金额占比", "曝光-观看率", "商品曝光-点击率", "商品点击-成交转化率", "留存率"}

    def build_fields(rec, ai_raw):
        """构造飞书 fields：数据日期转毫秒时间戳、文本转str、数字清洗。
        空值或清洗不出数字的字段跳过（留空待人工复查补），避免 NumberFieldConvFail。
        只处理白名单内字段名，避免智谱返回额外字段名导致 FieldNameNotFound。"""
        f = {}
        for k, v in rec.items():
            if k not in allowed:
                continue  # 非白名单字段一律跳过
            if k == "数据日期":
                try:
                    f[k] = int(datetime.strptime(str(v)[:10], "%Y-%m-%d").replace(tzinfo=CST).timestamp() * 1000)
                except Exception:
                    pass  # 空或格式不对则跳过（datetime 字段留空）
            elif k in ("账号", "主播姓名", "所属账号", "人均观看时长", "直播时长（min）"):
                if str(v).strip():
                    f[k] = str(v)
            else:
                s = str(v).strip().replace(",", "").replace("，", "")
                s = re.sub(r"[¥元%人件秒个次]", "", s)
                m = re.search(r"-?\d+\.?\d*", s)
                if m:
                    num = m.group()
                    val = float(num) if "." in num else int(num)
                    # 百分比字段：智谱可能返回百分比数值(如13.25)而非小数(0.1325)
                    # 飞书百分比字段存储小数，值>1说明是百分比数值，需除以100
                    if k in PERCENT_FIELDS and val > 1:
                        val = val / 100
                    f[k] = val
        f["数据状态"] = "草稿"
        f["AI识别结果"] = ai_raw
        return f

    # 预拉取表格已有记录用于去重（问题2、3：每天每人/每账号只一组，重复发消息不重复登记）
    existing = list_records(base, table)

    # 统计变量初始化（必须在所有使用前定义，避免 UnboundLocalError/NameError）
    total_ok = 0
    skipped_dups = 0
    errors = []
    all_labels = []

    # 主播消息特殊处理：多张图可能是同一批主播的不同维度数据（基础数据+能力分析）
    # 需要按主播姓名合并到同一条记录，而非分开识别
    if is_anchor:
        # 收集所有图片的识别结果
        all_anchor_results = []
        anchor_attachment_tokens = []  # 主播截图附件file_token
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
            # 上传主播截图到飞书表格附件
            try:
                ft = upload_attachment(img, f"主播截图{idx+1}", base)
                if ft:
                    anchor_attachment_tokens.append(ft)
            except Exception as e:
                errors.append(f"第{idx+1}张图上传附件失败: {e}")
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
            # 主播截图附件（所有主播记录都放同一组截图）
            if anchor_attachment_tokens:
                fields["截图附件"] = [{"file_token": t} for t in anchor_attachment_tokens]
            # 录屏链接（从消息里提取的URL/文件名，分别放入录屏链接1/2/3）
            for i, url in enumerate(link_urls[:3]):
                fields[f"录屏链接{i+1}"] = url
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
        # 直播消息：多张图是同一场直播的不同页面数据（千川页/直播大屏/转化漏斗），
        # 合并成一条记录，而非每张图独立一条
        merged = {}  # 合并后的记录
        ai_raws = []  # 所有图的AI原始返回
        shop_names = []  # 各张图识别到店铺名
        attachment_tokens = []  # 各张图上传附件后的file_token
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
            # 收集店铺名用于核对
            sn = str(result.get("店铺名", "")).strip()
            if sn:
                shop_names.append(sn)
            result.pop("店铺名", None)
            ai_raws.append(ai_result)
            # 上传图片到飞书表格附件（收集每天3张直播截图）
            try:
                ft = upload_attachment(img, f"直播截图{idx+1}", base)
                if ft:
                    attachment_tokens.append(ft)
            except Exception as e:
                errors.append(f"第{idx+1}张图上传附件失败: {e}")
            # 合并字段（后识别的补充先识别的空值）
            for k, v in result.items():
                existing_val = merged.get(k)
                if existing_val is None or str(existing_val).strip() == "":
                    merged[k] = v

        # 核对店铺名：如果AI识别的店铺名和消息文字的账号不一致，提醒人工核实
        for sn in shop_names:
            if msg_account and sn and msg_account not in sn and sn not in msg_account:
                errors.append(f"店铺名核对警告：截图右上角'{sn}'与消息账号'{msg_account}'不一致，请人工核实")
                break

        # 用消息文字的日期/账号覆盖AI识别结果
        if msg_date:
            merged["数据日期"] = msg_date
        if msg_account:
            merged["账号"] = msg_account
        merged.pop("类型", None)

        # 写入合并后的直播记录
        ai_raw_combined = "\n---\n".join(ai_raws) if ai_raws else ""
        fields = build_fields(merged, ai_raw_combined)
        # 把每天3张直播截图作为附件写入"截图附件"字段
        if attachment_tokens:
            fields["截图附件"] = [{"file_token": t} for t in attachment_tokens]
        if len(fields) <= 2:
            if not errors:
                errors.append("识别完成但无有效业务数据（截图可能无对应指标）")
        else:
            if is_duplicate(fields, existing, "live"):
                skipped_dups += 1
            else:
                try:
                    write_record(base, table, fields)
                    total_ok += 1
                    all_labels.append(merged.get("账号") or "未命名")
                except Exception as e:
                    errors.append(f"写入失败: {e}")

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
        "version": "7.13",
        "encrypt_key_set": bool(ENCRYPT_KEY),
        "app_id_set": bool(APP_ID),
        "app_secret_set": bool(APP_SECRET),
        "zhipu_key_set": bool(ZHIPU_KEY),
        "group_chat_set": bool(GROUP_CHAT),
    })


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 3000)))
