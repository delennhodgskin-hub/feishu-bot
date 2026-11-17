import os, json, base64, requests
import tempfile
import threading
import time
import re
import zlib
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

from data_intake import PROMPT_LIVE, PROMPT_ANCHOR
import data_intake as intake
_SCREENSHOT_WRITE_LOCK = threading.Lock()


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


RECORDING_CHUNK_BYTES = 4 * 1024 * 1024
RECORDING_MAX_BYTES = 2 * 1024 * 1024 * 1024
_RECORDING_LOCK = threading.Lock()


def recording_api_result(response, action):
    """仅检查录屏接口；HTTP或业务错误均不能当作成功。"""
    try:
        body = response.json()
    except (ValueError, TypeError):
        raise RuntimeError(f"{action}失败：HTTP {response.status_code}，接口未返回JSON。") from None
    if not isinstance(body, dict):
        raise RuntimeError(f"{action}失败：接口返回格式异常。")
    if response.status_code != 200 or body.get("code") != 0:
        raise RuntimeError(f"{action}失败：HTTP {response.status_code}，"
                           f"code={body.get('code')}，{str(body.get('msg', ''))[:160]}")
    return body.get("data") or {}


def recording_request(method, url, **kwargs):
    """录屏分片可重试网络错误/限流；权限错误立即返回上层。"""
    for attempt in range(3):
        try:
            response = getattr(requests, method)(url, **kwargs)
        except (requests.Timeout, requests.ConnectionError):
            if attempt == 2:
                raise RuntimeError("录屏传输网络超时，请稍后用一条新消息重发。") from None
        else:
            if response.status_code not in (429, 500, 502, 503, 504) or attempt == 2:
                return response
            response.close()
        time.sleep(2 ** attempt)


def download_recording(message_id, file_key, destination, token):
    """按Range下载到临时文件，验证长度，避免长录屏全部占用内存。"""
    from urllib.parse import quote
    url = ("https://open.feishu.cn/open-apis/im/v1/messages/"
           f"{quote(message_id, safe='')}/resources/{quote(file_key, safe='')}")
    offset, total = 0, None
    while total is None or offset < total:
        end = offset + RECORDING_CHUNK_BYTES - 1
        if total is not None:
            end = min(end, total - 1)
        # 流读取中断时重下当前分片，先截断该分片的残留，禁止拼接损坏文件。
        for attempt in range(3):
            destination.seek(offset)
            destination.truncate()
            response = recording_request("get", url,
                headers={"Authorization": f"Bearer {token}", "Range": f"bytes={offset}-{end}"},
                params={"type": "file"}, stream=True, timeout=(15, 120))
            try:
                if response.status_code not in (200, 206):
                    raise RuntimeError(f"下载录屏失败：HTTP {response.status_code}。"
                                       "请核对机器人读取消息资源权限；过大视频可分段发送。")
                mime = response.headers.get("Content-Type", "").lower()
                if "json" in mime or "text/html" in mime:
                    raise RuntimeError("下载录屏失败：接口返回错误信息而非文件。")
                if response.status_code == 206:
                    match = re.fullmatch(r"bytes (\d+)-(\d+)/(\d+)", response.headers.get("Content-Range", ""))
                    if not match:
                        raise RuntimeError("录屏分片缺少有效Content-Range，未保存不完整文件。")
                    start, last, received_total = map(int, match.groups())
                    if (start != offset or last != min(end, received_total - 1)
                            or received_total <= last or (total is not None and total != received_total)):
                        raise RuntimeError("录屏分片范围不一致，未保存不完整文件。")
                    expected = last - start + 1
                    total = received_total
                else:
                    # 小文件接口可能忽略Range并返回整个文件，仅首段允许。
                    if offset:
                        raise RuntimeError("录屏接口未继续返回分片，未保存不完整文件。")
                    length = response.headers.get("Content-Length")
                    expected = int(length) if length is not None else None
                    total = expected
                if total is not None and (total <= 0 or total > RECORDING_MAX_BYTES):
                    raise RuntimeError("录屏为空或超过2GB，请拆分后发送。")
                count = 0
                for chunk in response.iter_content(chunk_size=256 * 1024):
                    if not chunk:
                        continue
                    count += len(chunk)
                    if offset + count > RECORDING_MAX_BYTES or (expected is not None and count > expected):
                        raise RuntimeError("录屏长度超过预期，未保存异常文件。")
                    destination.write(chunk)
                if not count or (expected is not None and count != expected):
                    raise RuntimeError("录屏下载不完整，未写入附件，请用一条新消息重发。")
                if total is None:
                    total = count
                offset += count
                break
            except (requests.Timeout, requests.ConnectionError, requests.exceptions.ChunkedEncodingError):
                if attempt == 2:
                    raise RuntimeError("录屏下载中断，未保存不完整文件，请用一条新消息重发。") from None
                time.sleep(2 ** attempt)
            finally:
                response.close()
    destination.seek(0)
    return total


def upload_recording(file_obj, file_name, size, token):
    """上传到当前主播多维表格的素材空间；不上传到云盘根目录。"""
    root = "https://open.feishu.cn/open-apis/drive/v1/medias"
    headers = {"Authorization": f"Bearer {token}"}
    response = recording_request("post", root + "/upload_prepare", headers=headers,
        json={"file_name": file_name, "parent_type": "bitable_file", "parent_node": BASE_ANCHOR,
              "size": size}, timeout=(15, 60))
    prepared = recording_api_result(response, "准备录屏附件上传")
    upload_id = prepared.get("upload_id")
    block_size, block_num = prepared.get("block_size"), prepared.get("block_num")
    if (not upload_id or not isinstance(block_size, int) or not 0 < block_size <= 20 * 1024 * 1024
            or not isinstance(block_num, int) or block_num != (size + block_size - 1) // block_size):
        raise RuntimeError("录屏上传分片策略异常，未写入附件。")
    file_obj.seek(0)
    for seq in range(block_num):
        part = file_obj.read(min(block_size, size - seq * block_size))
        if len(part) != min(block_size, size - seq * block_size):
            raise RuntimeError("本地录屏分片不完整，未写入附件。")
        response = recording_request("post", root + "/upload_part", headers=headers,
            data={"upload_id": upload_id, "seq": str(seq), "size": str(len(part)),
                  "checksum": str(zlib.adler32(part) & 0xffffffff)},
            files={"file": (file_name, part, "application/octet-stream")}, timeout=(15, 120))
        recording_api_result(response, f"上传录屏分片{seq + 1}/{block_num}")
        # 官方素材上传接口限频5QPS，录屏线程串行并留出间隔。
        time.sleep(0.25)
    response = recording_request("post", root + "/upload_finish", headers=headers,
        json={"upload_id": upload_id, "block_num": block_num}, timeout=(15, 120))
    file_token = recording_api_result(response, "完成录屏附件上传").get("file_token")
    if not isinstance(file_token, str) or not file_token:
        raise RuntimeError("上传完成未返回附件token，未写入表格。")
    return file_token


def store_recording_attachment(message_id, media, filename):
    file_key = str(media.get("file_key") or "").strip()
    if not file_key:
        raise ValueError("录屏消息缺少文件标识，请将文字和视频放在同一条消息中重新发送。")
    token = get_tenant_token()
    with tempfile.TemporaryFile(mode="w+b") as video:
        size = download_recording(message_id, file_key, video, token)
        return {"file_token": upload_recording(video, filename, size, token), "size": size}


def get_recording_record(record_id):
    token = get_tenant_token()
    response = recording_request("get",
        f"https://open.feishu.cn/open-apis/bitable/v1/apps/{BASE_ANCHOR}/tables/{TABLE_ANCHOR}/records/{record_id}",
        headers={"Authorization": f"Bearer {token}"}, timeout=(15, 30))
    record = recording_api_result(response, "核对录屏附件").get("record")
    if not isinstance(record, dict) or record.get("record_id") != record_id:
        raise RuntimeError("录屏记录读回异常，尚未确认附件保存成功。")
    return record


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


def parse_recording_command(full_text, current_year=None):
    """解析日期、账号、主播姓名和录屏序号；不把命令文字混入姓名。"""
    import re
    import unicodedata
    from datetime import datetime, timezone, timedelta

    text = unicodedata.normalize("NFKC", full_text).strip()
    text = re.sub(r"@\S+", "", text).strip()
    target_date = None
    date_pattern = r"(?<!\d)(?:(20\d{2})[.\-/年])?(\d{1,2})[.\-/月](\d{1,2})(?:日)?(?!\d)"
    dates = list(re.finditer(date_pattern, text))
    if len(dates) > 1:
        raise ValueError("请只写一个录屏日期，例如 2026-11-11。")
    if dates:
        match = dates[0]
        year, month, day = match.groups()
        year = int(year or current_year or datetime.now(timezone(timedelta(hours=8))).year)
        try:
            target_date = datetime(year, int(month), int(day)).strftime("%Y-%m-%d")
        except ValueError:
            raise ValueError("录屏日期无效，请使用真实日期，例如 2026-11-11。") from None
        text = text[:match.start()] + " " + text[match.end():]

    account = None
    account_names = sorted(
        set(ACCOUNTS + [keyword for keyword, _ in ACCOUNT_KEYWORDS]),
        key=len, reverse=True)
    for name in account_names:
        if name in text:
            _, found = parse_text_meta(name)
            if account and account != found:
                raise ValueError("一条录屏消息只能指定一个账号。")
            account = found
            text = text.replace(name, " ")

    slots = re.findall(r"录屏\s*(?:第\s*)?(\d+|[一二三四五六七八九十]+)\s*(?:段|号|个)?", text)
    if len(slots) > 1:
        raise ValueError("一条消息请只指定一个录屏序号。")
    slot = None
    if slots:
        slot = {"一": 1, "二": 2, "三": 3}.get(slots[0])
        if slot is None and slots[0].isdigit():
            slot = int(slots[0])
        if slot not in (1, 2, 3):
            raise ValueError("录屏序号只能是 1、2 或 3。")
    text = re.sub(r"的\s*(?=录屏)", "", text)
    text = re.sub(r"录屏\s*(?:第\s*)?(?:\d+|[一二三四五六七八九十]+)\s*(?:段|号|个)?", " ", text)
    text = text.replace("录屏", " ")
    text = re.sub(r"^\s*(?:(?:请|帮我|记录|登记|收集|上传|保存|查看|查询|回看)\s*)+", "", text)
    text = re.sub(r"(?:主播姓名|主播|账号)\s*[:：]?", " ", text)
    anchor_name = re.sub(r"\s+", "", text).strip(",:：，。;；")
    if not anchor_name or not re.fullmatch(r"[\u4e00-\u9fffA-Za-z0-9·._-]+", anchor_name):
        raise ValueError("未识别到主播姓名，请写“记录11.11刘雅菊录屏1”并附上视频。")
    return {"anchor_name": anchor_name, "date": target_date, "account": account, "slot": slot}


def recording_date(value):
    """统一表格日期；按北京时间解释毫秒时间戳。"""
    from datetime import datetime, timezone, timedelta
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(value / 1000, tz=timezone(timedelta(hours=8))).strftime("%Y-%m-%d")
    text = field_text(value).strip()
    if text.isdigit() and len(text) >= 12:
        return recording_date(int(text))
    return text[:10]


def recording_links(fields):
    return [(slot, field_text(fields.get(f"录屏链接{slot}")).strip()) for slot in (1, 2, 3)]


def recording_attachments(fields, slot):
    value = fields.get(f"录屏附件{slot}")
    if not value:
        return []
    if not isinstance(value, list) or any(not isinstance(item, dict) or not item.get("file_token") for item in value):
        raise ValueError(f"录屏附件{slot}字段不是有效的附件列表，请先核对字段类型。")
    return value


def recording_record_url(record_id):
    return (f"https://fcndv9la878k.feishu.cn/base/{BASE_ANCHOR}"
            f"?table={TABLE_ANCHOR}&view=vewxtRhz5q&record={record_id}")


def select_recording_record(existing, command, for_view=False):
    """姓名精确匹配；显式日期不回退；多账号或多条同日记录不猜测。"""
    candidates = []
    for record in existing:
        fields = record.get("fields", {})
        if field_text(fields.get("主播姓名")).strip() != command["anchor_name"]:
            continue
        if command["account"] and field_text(fields.get("所属账号")).strip() != command["account"]:
            continue
        if command["date"] and recording_date(fields.get("数据日期")) != command["date"]:
            continue
        candidates.append(record)
    if not candidates:
        when = command["date"] or "已有日期"
        raise ValueError(f"未找到 {command['anchor_name']}（{when}）的主播记录，请先登记该日期的主播数据。")
    accounts = {field_text(r.get("fields", {}).get("所属账号")).strip() for r in candidates}
    if not command["account"] and len(accounts) > 1:
        raise ValueError("该主播有多个账号的记录，请在命令中补充账号名称。")
    if not command["date"]:
        if for_view:
            with_video = [r for r in candidates if any(
                (value.startswith("om_") or recording_attachments(r.get("fields", {}), slot))
                and (command["slot"] is None or slot == command["slot"])
                for slot, value in recording_links(r.get("fields", {})))]
            if with_video:
                candidates = with_video
        newest = max(recording_date(r.get("fields", {}).get("数据日期")) for r in candidates)
        candidates = [r for r in candidates if recording_date(r.get("fields", {}).get("数据日期")) == newest]
    if len(candidates) != 1:
        raise ValueError("同一主播、账号和日期有多条记录，请先核对表格，录屏未写入。")
    record = candidates[0]
    if not record.get("record_id"):
        raise ValueError("主播记录缺少记录 ID，录屏未写入。")
    return record


def _handle_recording(message_id, full_text, media_files):
    """串行保存录屏文件；只更新指定附件及其原消息标识，其他业务字段不变。"""
    saved_label = None
    try:
        command = parse_recording_command(full_text)
        if not message_id or not message_id.startswith("om_") or not media_files:
            raise ValueError("录屏消息缺少视频或消息 ID，请将文字和视频放在同一条消息中发送。")
        if len(media_files) != 1:
            raise ValueError("一条录屏登记消息请只附一段视频，分别使用录屏1、2、3登记。")
        if not media_files[0].get("file_key"):
            raise ValueError("录屏消息缺少文件标识，请将文字和视频放在同一条消息中重发。")
        with _RECORDING_LOCK:
            existing = list_records(BASE_ANCHOR, TABLE_ANCHOR, strict=True)
            record = select_recording_record(existing, command)
            fields = record.get("fields", {})
            links = recording_links(fields)
            existing_slot = next((slot for slot, value in links if value == message_id), None)
            slot = existing_slot or command["slot"]
            if slot is None:
                slot = next((index for index, value in links
                             if not value and not recording_attachments(fields, index)), None)
            if slot is None:
                raise ValueError("该记录的 3 个录屏位置已满，未覆盖原录屏，请先在表格中核对。")
            current_value = dict(links)[slot]
            attachments = recording_attachments(fields, slot)
            if (current_value and current_value != message_id) or (attachments and existing_slot != slot):
                raise ValueError(f"录屏{slot}已存在，未覆盖。请使用空闲序号或先在表格中核对。")
            if not attachments:
                ext = os.path.splitext(str(media_files[0].get("file_name") or ""))[1].lower()
                if ext not in (".mp4", ".mov", ".m4v", ".webm", ".mkv", ".avi"):
                    ext = ".mp4"
                filename = re.sub(r'[\\/:*?"<>|\x00-\x1f]', "_",
                    f"{recording_date(fields.get('数据日期'))}_{command['anchor_name']}_录屏{slot}{ext}")
                uploaded = store_recording_attachment(message_id, media_files[0], filename)
                token = uploaded.get("file_token")
                if not token:
                    raise RuntimeError("未取得视频附件token，未写入表格。")
                # 长视频传输期间用户可能编辑记录；写入前重新核对归属和槽位。
                latest = get_recording_record(record["record_id"])
                latest_fields = latest.get("fields", {})
                for name in ("主播姓名", "所属账号", "数据日期"):
                    normalize = recording_date if name == "数据日期" else lambda value: field_text(value).strip()
                    if normalize(latest_fields.get(name)) != normalize(fields.get(name)):
                        raise RuntimeError("上传期间主播记录的姓名、账号或日期发生变化，未写入附件。")
                if (dict(recording_links(latest_fields))[slot] != current_value
                        or recording_attachments(latest_fields, slot) != attachments):
                    raise RuntimeError(f"上传期间录屏{slot}已被修改，未覆盖，请核对表格。")
                patch_fields = {f"录屏附件{slot}": [{"file_token": token}], f"录屏链接{slot}": message_id}
                update_record(BASE_ANCHOR, TABLE_ANCHOR, record["record_id"], patch_fields)
                # 只有文件token与消息标识读回一致才发送成功提示。
                checked = get_recording_record(record["record_id"]).get("fields", {})
                actual = recording_attachments(checked, slot)
                if ([item["file_token"] for item in actual] != [token]
                        or dict(recording_links(checked))[slot] != message_id):
                    raise RuntimeError("已提交附件写入，但读回未匹配，尚未确认保存成功；请核对表格。")
            saved_label = f"{command['anchor_name']}（{recording_date(fields.get('数据日期'))}）录屏{slot}"
        reply_message(message_id, f"✅ 已保存视频附件：{saved_label}\n"
                      f"在每日主播明细的“录屏附件{slot}”中打开：\n"
                      f"{recording_record_url(record['record_id'])}", strict=True)
    except Exception as exc:
        if saved_label:
            send_group_msg(f"已保存附件 {saved_label}，但发送确认回复失败：{exc}")
        else:
            send_group_msg(f"录屏附件保存未完成：{exc}")


def _handle_view_recording(message_id, full_text):
    """按同一套命令规则查询；指定录屏序号时只返回该段，未指定时返回所有已存段。"""
    try:
        command = parse_recording_command(full_text)
        existing = list_records(BASE_ANCHOR, TABLE_ANCHOR, strict=True)
        record = select_recording_record(existing, command, for_view=True)
        fields = record.get("fields", {})
        videos = [(slot, value) for slot, value in recording_links(fields)
                  if value.startswith("om_") and (command["slot"] is None or slot == command["slot"])]
        label = f"{command['anchor_name']}（{recording_date(fields.get('数据日期'))}）"
        attachment_slots = [slot for slot in (1, 2, 3)
                            if (command["slot"] is None or slot == command["slot"])
                            and recording_attachments(fields, slot)]
        if attachment_slots:
            reply_message(message_id, f"📹 {label} 已保存的视频附件："
                          + "、".join(f"录屏{x}" for x in attachment_slots)
                          + f"\n{recording_record_url(record['record_id'])}", strict=True)
            videos = [(slot, value) for slot, value in videos if slot not in attachment_slots]
            if not videos:
                return
        if not videos:
            requested = f"录屏{command['slot']}" if command["slot"] else "录屏"
            raise ValueError(f"{label}没有登记可引用的{requested}，请先收集视频。")
        for slot, video_message_id in videos:
            reply_message(video_message_id, f"📹 录屏回看：{label} 录屏{slot}\n原视频消息见上方引用。", strict=True)
        reply_message(message_id, f"已发送 {label} 的 {len(videos)} 段录屏消息引用。", strict=True)
    except Exception as exc:
        reply_message(message_id, f"查看录屏未完成：{exc}")


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


def send_card_msg(title, content, button_text, button_url, template="green"):
    """发送带按钮的卡片消息到群（用于录屏收集成功后发"查看录屏"按钮）。"""
    try:
        token = get_tenant_token()
        card = {
            "config": {"wide_screen_mode": True},
            "header": {"title": {"tag": "plain_text", "content": title}, "template": template},
            "elements": [
                {"tag": "div", "text": {"tag": "lark_md", "content": content}},
                {"tag": "action", "actions": [
                    {"tag": "button", "text": {"tag": "plain_text", "content": button_text},
                     "type": "primary", "url": button_url}
                ]}
            ]
        }
        requests.post(
            "https://open.feishu.cn/open-apis/im/v1/messages",
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
            params={"receive_id_type": "chat_id"},
            json={"receive_id": GROUP_CHAT, "msg_type": "interactive",
                  "content": json.dumps(card)}, timeout=15)
    except Exception:
        pass


def reply_message(message_id, text, strict=False):
    """回复原消息；录屏流程校验 API 结果，避免接口拒绝时仍宣称成功。"""
    try:
        token = get_tenant_token()
        response = requests.post(
            f"https://open.feishu.cn/open-apis/im/v1/messages/{message_id}/reply",
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
            json={"msg_type": "text", "content": json.dumps({"text": text})},
            timeout=15)
        response.raise_for_status()
        result = response.json()
        if result.get("code") != 0:
            raise RuntimeError(f"引用回复失败 code={result.get('code')}: {result.get('msg', '')}")
        return result
    except Exception:
        if strict:
            raise
        return None


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


def list_records(base, table, page_size=100, strict=False):
    """读取所有页；录屏查询遇到权限或网络错误时不伪装成“没有记录”。"""
    try:
        token = get_tenant_token()
        records, seen_tokens = [], set()
        page_token = None
        while True:
            params = {"page_size": page_size}
            if page_token:
                params["page_token"] = page_token
            response = requests.get(
                f"https://open.feishu.cn/open-apis/bitable/v1/apps/{base}/tables/{table}/records",
                headers={"Authorization": f"Bearer {token}"},
                params=params, timeout=15)
            response.raise_for_status()
            result = response.json()
            if result.get("code") != 0:
                raise RuntimeError(f"读取主播记录失败 code={result.get('code')}: {result.get('msg', '')}")
            data = result.get("data") or {}
            items = data.get("items")
            if items is not None and not isinstance(items, list):
                raise RuntimeError("表格返回的记录格式不正确。")
            records.extend(items or [])
            if not data.get("has_more"):
                return records
            page_token = data.get("page_token")
            if not page_token or page_token in seen_tokens:
                raise RuntimeError("表格分页信息异常，未取得完整记录。")
            seen_tokens.add(page_token)
    except Exception:
        if strict:
            raise
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



def ensure_summary_entries(base, kind, fields):
    """Create only the current business month/profile; never invent HR metadata."""
    from datetime import datetime
    date = datetime.fromtimestamp(fields["数据日期"] / 1000, intake.CST)
    month = date.strftime("%Y-%m")
    if kind == "anchor":
        profile_table, monthly_table = "tblHvBriYitkY3n7", "tblJqwosky9lCcwe"
        identity = {"主播姓名": fields["主播姓名"], "所属账号": fields["所属账号"]}
        profiles = list_records(base, profile_table, strict=True)
        if not any(all(intake.equivalent(r["fields"].get(k), v) for k, v in identity.items())
                   and intake.text(r["fields"].get("数据类型")) != "测试数据" for r in profiles):
            write_record(base, profile_table, {**identity, "数据类型": "正式数据"})
    else:
        monthly_table = "tbllka7HqEQ3DGBZ"
        identity = {"账号": fields["账号"]}
    monthly = list_records(base, monthly_table, strict=True)
    key = {**identity, "月份": month}
    if not any(all(intake.equivalent(r["fields"].get(k), v) for k, v in key.items())
               and intake.text(r["fields"].get("数据类型")) != "测试数据" for r in monthly):
        write_record(base, monthly_table, {**key, "数据类型": "正式数据",
                                         "月初日期": intake.timestamp(month + "-01")})


def sync_shared_ad_scope(base, table, existing, owner):
    fields = owner["fields"]
    if not intake.text(fields.get("投放统计口径")).startswith("全天主记录"):
        return
    scope = "同日共用：全天ROI及消耗见" + intake.text(fields.get("开播时间")) + "场，不重复累计"
    for other in existing:
        f = other["fields"]
        if (other["record_id"] != owner["record_id"]
                and intake.equivalent(f.get("数据日期"), fields["数据日期"])
                and intake.text(f.get("账号")) == fields["账号"]
                and intake.text(f.get("数据性质")) != "测试数据"
                and all(intake.missing(f.get(k)) for k in intake.ADS)
                and intake.text(f.get("投放统计口径")) != scope):
            update_record(base, table, other["record_id"], {"投放统计口径": scope})
            f["投放统计口径"] = scope


def _handle_screenshots(message_id, full_text, image_keys):
    """7.21: one session per message; partial fills or explicit corrections in-place."""
    date, account = parse_text_meta(full_text)
    kind = "anchor" if "主播" in full_text else "live"
    correction = "更正" in full_text or "纠错" in full_text
    if not date or not account:
        send_group_msg("请注明数据日期和账号，例如：2026-11-16 佳能数码 07:10 直播数据；主播用2026-11-15 佳能摄影 主播数据。")
        return
    intake.timestamp(date)  # Validate date before downloading/uploading.
    session = intake.message_time(full_text) if kind == "live" else None
    base, table = (BASE_ANCHOR, TABLE_ANCHOR) if kind == "anchor" else (BASE_LIVE, TABLE_LIVE)
    prompt = PROMPT_ANCHOR if kind == "anchor" else PROMPT_LIVE
    results, raw_results, attachments, issues = [], [], [], []
    for i, image_key in enumerate(image_keys, 1):
        try:
            img = download_image(message_id, image_key)
            raw = ai_recognize(img, prompt)
            result = intake.parse_result(raw)
            # Recognizable account contradictions block the batch. Truncated shop names
            # remain a review warning, not evidence for overwriting a different account.
            for key in ("店铺名", "账号", "所属账号"):
                values = result.get(key)
                values = values if isinstance(values, list) else [values]
                for value in values:
                    if intake.missing(value):
                        continue
                    _, recognized = parse_text_meta(intake.text(value))
                    if recognized and recognized != account:
                        raise ValueError("截图账号与文字账号不一致，整条消息未登记")
            results.append(result)
            raw_results.append({"image_index": i, "result": result})
            try:
                token = upload_attachment(img, f"截图{i}", base)
                if token:
                    attachments.append(token)
            except Exception as e:
                issues.append(f"第{i}张原图保存失败：{e}")
        except ValueError as e:
            send_group_msg(f"未登记：{e}。请核对截图后重新发送。")
            return
        except Exception as e:
            issues.append(f"第{i}张识别失败：{e}")
    candidates, merge_issues = intake.merge_results(results, kind, date, account, session)
    issues.extend(merge_issues)
    named = [r for r in candidates if kind == "anchor" and r["主播姓名"] in full_text]
    if named:
        candidates = named
    created = updated = unchanged = 0
    # Serialize read/plan/write to prevent concurrent screenshot events creating duplicates.
    with _SCREENSHOT_WRITE_LOCK:
        existing = list_records(base, table, strict=True)
        plans = []
        for fields in candidates:
            try:
                metrics = intake.ANCHOR if kind == "anchor" else intake.LIVE - {"开播时间"}
                if not any(k in fields for k in metrics):
                    continue
                target = intake.select_target(fields, existing, kind, correction)
                if kind == "live":
                    fields = intake.route_daily_ads(fields, existing, target)
                patch, conflict = intake.plan_patch(fields, target, correction)
                issues.extend(conflict)
                if not patch:
                    unchanged += 1
                    if target:
                        try:
                            ensure_summary_entries(base, kind, target["fields"])
                            if kind == "live":
                                sync_shared_ad_scope(base, table, existing, target)
                        except Exception as e:
                            issues.append(f"原明细未变，档案/月度同步失败：{e}")
                    continue
                old = target["fields"] if target else {}
                patch["数据状态"] = "草稿"
                if attachments:
                    patch["截图附件"] = intake.append_attachments(old.get("截图附件"), attachments)
                audit = {"version": "7.21", "message_id": message_id,
                         "mode": "更正" if correction else "补充", "issues": issues,
                         "changes": {k: {"before": old.get(k), "after": v}
                                     for k, v in patch.items() if k not in {"截图附件", "数据状态"}},
                         "sources": raw_results}
                # Preserve the immediate previous provenance and bounded earlier history.
                prior = intake.text(old.get("AI识别结果"))
                patch["AI识别结果"] = json.dumps(audit, ensure_ascii=False) + ("\n---此前记录---\n" + prior[-20000:] if prior else "")
                plans.append((target, patch))
            except ValueError as e:
                issues.append(str(e))
        for target, patch in plans:
            try:
                saved = target
                if target:
                    update_record(base, table, target["record_id"], patch)
                    target["fields"].update(patch)
                    updated += 1
                else:
                    result = write_record(base, table, patch)
                    new_record = (result.get("data") or {}).get("record")
                    if new_record:
                        existing.append(new_record)
                        saved = new_record
                    created += 1
            except Exception as e:
                issues.append(f"保存失败：{e}")
                continue
            try:
                if saved:
                    ensure_summary_entries(base, kind, saved["fields"])
                    if kind == "live":
                        sync_shared_ad_scope(base, table, existing, saved)
            except Exception as e:
                issues.append(f"明细已保存，但档案/月度或投放标记未同步：{e}；请稍后重发补图重试")
    parts = [f"新增{created}条，更新原记录{updated}条，无数值变化{unchanged}条。"]
    if created or updated:
        parts.append("新增或更正的记录已设为草稿，请对照截图复查，再改为已复查；月汇总按已复查数据自动重算。")
    if issues:
        parts.append("需核对：\n" + "\n".join(dict.fromkeys(issues)))
    if not candidates:
        parts.append("没有取得可登记指标，请补清晰截图。")
    send_group_msg("\n".join(parts))


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
    if msg.get("message_type") not in ("post", "text"):
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
    if msg.get("message_type") == "text":
        # 普通文本的“查看录屏”也需要处理，不能在 post 类型检查时丢弃。
        content = {"content": [[{"tag": "text", "text": content.get("text", "")}]]}
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

    # 优先检查"查看录屏"命令（纯文字消息，优先级最高）
    # 支持多种格式："查看录屏 小琦"、"查看小琦的录屏"、"查看小琦 11.5的录屏"
    if "查看" in full_text and "录屏" in full_text:
        message_id = msg.get("message_id", "")
        _handle_view_recording(message_id, full_text)
        return  # 查看录屏命令处理完直接结束

    if media_files and "录屏" in full_text:
        _handle_recording(msg.get("message_id", ""), full_text, media_files)
        return
    if not image_keys:
        return  # 无图，后台线程直接结束

    _handle_screenshots(msg.get("message_id", ""), full_text, image_keys)



@app.route("/", methods=["GET"])
def index():
    return "feishu-bot running"


@app.route("/version", methods=["GET"])
def version():
    """版本和环境变量状态确认（排查部署问题用）"""
    return jsonify({
        "version": "7.21",
        "encrypt_key_set": bool(ENCRYPT_KEY),
        "app_id_set": bool(APP_ID),
        "app_secret_set": bool(APP_SECRET),
        "zhipu_key_set": bool(ZHIPU_KEY),
        "group_chat_set": bool(GROUP_CHAT),
    })


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 3000)))
