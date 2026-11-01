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


def download_image(image_key):
    token = get_tenant_token()
    r = requests.get(
        f"https://open.feishu.cn/open-apis/im/v1/images/{image_key}",
        headers={"Authorization": f"Bearer {token}"}, timeout=30)
    return r.content


def ai_recognize(image_bytes, prompt):
    b64 = base64.b64encode(image_bytes).decode()
    r = requests.post(
        "https://open.bigmodel.cn/api/paas/v4/chat/completions",
        headers={"Authorization": f"Bearer {ZHIPU_KEY}"},
        json={"model": "glm-4v-flash", "messages": [{"role": "user", "content": [
            {"type": "text", "text": prompt},
            {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64}"}}]}]},
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
    return r.json()


def send_group_msg(text):
    token = get_tenant_token()
    requests.post(
        "https://open.feishu.cn/open-apis/im/v1/messages",
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        params={"receive_id_type": "chat_id"},
        json={"receive_id": GROUP_CHAT, "msg_type": "text",
              "content": json.dumps({"text": text})}, timeout=15)


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
    is_anchor = "主播" in full_text
    prompt = PROMPT_ANCHOR if is_anchor else PROMPT_LIVE
    try:
        img = download_image(image_keys[0])
        ai_result = ai_recognize(img, prompt)
    except Exception as e:
        send_group_msg(f"AI识别失败: {e}")
        return jsonify({"ok": False, "error": str(e)})
    try:
        clean = ai_result.strip()
        if clean.startswith("```"):
            clean = clean.split("\n", 1)[1].rsplit("```", 1)[0]
        result = json.loads(clean)
    except Exception:
        send_group_msg(f"AI结果解析失败，原始返回：{ai_result[:300]}")
        return jsonify({"ok": False, "error": "parse"})
    rtype = result.pop("类型", "anchor" if is_anchor else "live")
    base = BASE_ANCHOR if rtype == "anchor" else BASE_LIVE
    table = TABLE_ANCHOR if rtype == "anchor" else TABLE_LIVE
    fields = {}
    for k, v in result.items():
        if k in ("数据日期", "账号", "主播姓名", "所属账号"):
            fields[k] = v
        else:
            try:
                fields[k] = float(v) if "." in str(v) else int(v)
            except (ValueError, TypeError):
                fields[k] = v
    fields["数据状态"] = "草稿"
    fields["AI识别结果"] = ai_result
    try:
        write_record(base, table, fields)
        label = result.get("账号") or result.get("主播姓名") or ""
        date = result.get("数据日期", "")
        send_group_msg(f"已识别并登记草稿：{label} {date}，请复查后改数据状态为已复查。")
    except Exception as e:
        send_group_msg(f"写入表格失败: {e}")
        return jsonify({"ok": False, "error": str(e)})
    return jsonify({"ok": True})


@app.route("/", methods=["GET"])
def index():
    return "feishu-bot running"


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 3000)))
