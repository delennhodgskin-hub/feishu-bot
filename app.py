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
PROMPT_ANCHOR = "识别这张抖音电商罗盘主播分析数据截图。返回JSON，字段：类型(填anchor)、数据日期(YYYY-MM-DD)、主播姓名、所属账号(佳能Canon数码旗舰店/Canon佳能摄影摄像旗舰店/汉印拍立得旗舰店)、引流-曝光进入率(小数)、留存率(小数)、互动率(小数)、新增粉丝(整数)、观看-关注率(小数)、新增团人数(整数)、UV价值、单小时支付金额、最高在线(整数)、平均在线(整数)、平均客单价、成交金额。数值只返数字。只返JSON不要其他文字。"


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
    token = get_tenant_token()
    requests.post(
        "https://open.feishu.cn/open-apis/im/v1/messages",
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        params={"receive_id_type": "chat_id"},
        json={"receive_id": GROUP_CHAT, "msg_type": "text",
              "content": json.dumps({"text": text})}, timeout=15)


ACCOUNTS = ["佳能Canon数码旗舰店", "Canon佳能摄影摄像旗舰店", "汉印拍立得旗舰店"]


def parse_text_meta(text):
    """从消息文字提取日期和账号（问题4：格式'时间+账号+直播数据'）。
    支持 '2026.11.1主播数据'、'2026-11-01 佳能Canon数码旗舰店 直播数据'、'11.1' 等。
    返回 (date_str 'YYYY-MM-DD' or None, account or None)。"""
    import re
    account = None
    for a in ACCOUNTS:
        if a in text:
            account = a
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
    """拉取表格已有记录（用于去重判断）"""
    try:
        token = get_tenant_token()
        r = requests.get(
            f"https://open.feishu.cn/open-apis/bitable/v1/apps/{base}/tables/{table}/records",
            headers={"Authorization": f"Bearer {token}"},
            params={"page_size": page_size}, timeout=15)
        data = r.json()
        if data.get("code") != 0:
            return []
        return data.get("data", {}).get("items", [])
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
    raw = request.get_data(as_text=True)
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
        return jsonify({"challenge": data["challenge"]})
    header = data.get("header", {})
    if header.get("event_type") != "im.message.receive_v1":
        return jsonify({"ok": True})
    event = data.get("event", {})
    msg = event.get("message", {})
    if msg.get("message_type") != "post":
        return jsonify({"ok": True})
    content = json.loads(msg.get("content", "{}"))
    image_keys, text_parts = [], []
    for row in content.get("content", []):
        for node in row:
            if node.get("tag") == "img":
                image_keys.append(node.get("image_key"))
            elif node.get("tag") == "text":
                text_parts.append(node.get("text", ""))
    full_text = "".join(text_parts)
    if not image_keys:
        return jsonify({"ok": True, "msg": "no image"})

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

    total_ok, all_labels, skipped_dups, errors = 0, [], 0, []
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
            if is_anchor:
                result["所属账号"] = msg_account
            else:
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
        parts.append("识别完成但无有效数据写入（可能已重复登记或截图无有效数据）。")
    send_group_msg("\n".join(parts))
    return jsonify({"ok": True})


@app.route("/", methods=["GET"])
def index():
    return "feishu-bot running"


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 3000)))
