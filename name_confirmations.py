"""Pending anchor-name questions, scoped to one Feishu chat and original post."""
from contextlib import contextmanager
import json
import os
from pathlib import Path
import re
import sqlite3
import tempfile
import threading
import time
import unicodedata


def parse_reply(text, *, allow_bare=False):
    """Parse a name assertion, not a question, a registration or general chat."""
    value = unicodedata.normalize('NFKC', str(text or '')).strip()
    value = re.sub(r'^(?:(?:@_user_\d+|@看数据|<at\b[^>]*>[^<]*</at>)\s*)+', '', value).strip()
    value = value.rstrip('。.!！').strip()
    value = re.sub(r'[,，]\s*(?:谢谢|辛苦了|没错)$', '', value).strip()
    value = re.sub(r'^是的[,，]\s*(?:是\s*)?', '是', value)
    value = re.sub(r'([“「"\'])([^“”「」"\']+)[”」"\']$', r'\2', value)
    name_pattern = r'([\u3400-\u9fffA-Za-z0-9][\u3400-\u9fffA-Za-z0-9·]{0,19})'
    found = re.fullmatch(
        r'(?:第\s*(\d{1,2})\s*列\s*[,，:]?\s*)?'
        r'(?:(?:确认(?:主播)?姓名|正确(?:的)?(?:主播)?姓名|(?:主播)?姓名|名字|主播名字|主播|确认)'
        r'\s*(?:就是|应该是|应为|为|是|[:：])|正确的是|就是|应该是|应为|是)\s*[:：]?\s*'
        + name_pattern, value)
    if found:
        number, name = found.groups()
    elif allow_bare and re.fullmatch(name_pattern, value):
        number, name = None, value
    else:
        return None
    if (not re.search(r'[\u3400-\u9fffA-Za-z]', name)
            or name in ('是', '的', '她', '他', '这个', '那个', '好', '好的', '知道了', '收到',
                        '对', '对的', '是的', '谢谢', '确认', '不是')
            or name.startswith(('否', '不是')) or any(x in name for x in ('还是', '或者', '不确定'))
            or name.endswith(('吗', '么', '对不对', '对吧')) or (number and int(number) == 0)):
        return None
    return (int(number) if number else None, name)


def _default_path():
    configured = os.getenv('ANCHOR_CONFIRM_DB')
    if configured:
        return configured
    directory = Path(tempfile.gettempdir()) / 'feishu-anchor-intake'
    directory.mkdir(mode=0o700, exist_ok=True)
    return str(directory / 'pending.sqlite3')


class PendingStore:
    """SQLite claims work across threads/processes sharing the same filesystem.

    A replacement Render instance can lose its temporary filesystem. Quoted
    messages with unavailable tickets never fall back to another open question.
    """
    def __init__(self, path=None, *, ttl=86400, limit=256, clock=time.time, lease=600):
        self.path = path
        self.ttl = ttl
        self.limit = limit
        self.lease = lease
        self.clock = clock
        self._lock = threading.RLock()
        self._db = None

    def _connection(self):
        if self._db is None:
            db = sqlite3.connect(str(self.path or _default_path()), timeout=10,
                                 check_same_thread=False, isolation_level=None)
            db.row_factory = sqlite3.Row
            db.execute('PRAGMA foreign_keys = ON')
            db.executescript("""
                CREATE TABLE IF NOT EXISTS questions (
                    chat TEXT NOT NULL, source TEXT NOT NULL, fingerprint TEXT,
                    payload TEXT NOT NULL, expires REAL NOT NULL,
                    status TEXT NOT NULL, holder TEXT, leased REAL,
                    PRIMARY KEY(chat,source)
                );
                CREATE INDEX IF NOT EXISTS question_fingerprint ON questions(chat,fingerprint);
                CREATE TABLE IF NOT EXISTS prompts (
                    chat TEXT NOT NULL, prompt TEXT NOT NULL, source TEXT NOT NULL,
                    PRIMARY KEY(chat,prompt),
                    FOREIGN KEY(chat,source) REFERENCES questions(chat,source) ON DELETE CASCADE
                );
                CREATE TABLE IF NOT EXISTS answered (
                    chat TEXT NOT NULL, reply TEXT NOT NULL, expires REAL NOT NULL,
                    PRIMARY KEY(chat,reply)
                );
            """)
            self._db = db
        return self._db

    @contextmanager
    def _transaction(self):
        with self._lock:
            db = self._connection()
            db.execute('BEGIN IMMEDIATE')
            try:
                now = self.clock()
                db.execute('DELETE FROM questions WHERE expires<=?', (now,))
                db.execute('DELETE FROM answered WHERE expires<=?', (now,))
                db.execute("UPDATE questions SET status='pending', holder=NULL, leased=NULL "
                           "WHERE status='busy' AND leased<=?", (now,))
                yield db, now
                db.commit()
            except BaseException:
                db.rollback()
                raise

    @staticmethod
    def _payload(value):
        if not isinstance(value, dict):
            raise ValueError('待确认上下文必须是对象')
        data = json.dumps(value, ensure_ascii=False, separators=(',', ':'), allow_nan=False)
        if len(data.encode('utf-8')) > 512 * 1024:
            raise ValueError('此批待确认内容过大，请将截图分批发送')
        return data

    def put(self, chat_id, source_id, payload, *, fingerprint=None):
        """Return False for duplicate/reprocessing; supersede exact-image pending reposts."""
        if not chat_id or not source_id:
            raise ValueError('待确认消息缺少群或原消息标识')
        data = self._payload(payload)
        with self._transaction() as (db, now):
            if db.execute('SELECT 1 FROM questions WHERE chat=? AND source=?',
                          (chat_id, source_id)).fetchone():
                return False
            if fingerprint:
                # Completed original images remain valid for ordinary re-registration.
                # Only a currently executing identical continuation must be left alone.
                if db.execute("SELECT 1 FROM questions WHERE chat=? AND fingerprint=? AND status='busy'",
                              (chat_id, fingerprint)).fetchone():
                    return False
                db.execute("UPDATE questions SET status='replaced', payload='{}' "
                           "WHERE chat=? AND fingerprint=? AND status='pending'", (chat_id, fingerprint))
            count = db.execute('SELECT COUNT(*) FROM questions').fetchone()[0]
            if count >= self.limit:
                # Only discard terminal receipts. Never evict an unresolved question
                # and then route its unquoted reply into a different screenshot.
                evicted = db.execute("DELETE FROM questions WHERE rowid=(SELECT rowid FROM questions "
                                     "WHERE status IN ('done','replaced') ORDER BY expires LIMIT 1)")
                if not evicted.rowcount:
                    raise ValueError('当前待确认的截图较多，请先处理已有姓名确认，再发送此图')
            db.execute("INSERT INTO questions(chat,source,fingerprint,payload,expires,status) "
                       "VALUES(?,?,?,?,?,'pending')", (chat_id, source_id, fingerprint, data, now + self.ttl))
            return True

    def bind_prompt(self, chat_id, source_id, prompt_id):
        if not prompt_id or not source_id or not chat_id:
            return False
        with self._transaction() as (db, _):
            if not db.execute("SELECT 1 FROM questions WHERE chat=? AND source=? AND status='pending'",
                              (chat_id, source_id)).fetchone():
                return False
            db.execute('INSERT OR IGNORE INTO prompts(chat,prompt,source) VALUES(?,?,?)',
                       (chat_id, prompt_id, source_id))
            return True

    def claim(self, chat_id, reply_id, *, parent_id=None, root_id=None):
        """Reserve one ticket atomically. Quotes must refer to a known prompt or source."""
        if not chat_id or not reply_id:
            return 'none', None, None
        with self._transaction() as (db, now):
            if db.execute('SELECT 1 FROM answered WHERE chat=? AND reply=?', (chat_id, reply_id)).fetchone():
                return 'done', None, None
            quote = parent_id or root_id
            if quote:
                # A parent identifies exactly which message the user replied to.
                # Do not use a broader thread root to rescue an unrelated parent.
                row = db.execute('SELECT * FROM questions WHERE chat=? AND (source=? OR source='
                                 '(SELECT source FROM prompts WHERE chat=? AND prompt=?))',
                                 (chat_id, quote, chat_id, quote)).fetchone()
            else:
                matches = db.execute("SELECT * FROM questions WHERE chat=? AND status IN ('pending','busy') "
                                     "LIMIT 2", (chat_id,)).fetchall()
                if len(matches) > 1:
                    return 'ambiguous', None, None
                row = matches[0] if matches else None
            if not row:
                return 'none', None, None
            if row['status'] != 'pending':
                status = 'busy' if row['status'] == 'busy' else 'done'
                return status, row['source'], None
            db.execute("UPDATE questions SET status='busy', holder=?, leased=? WHERE chat=? AND source=?",
                       (reply_id, now + self.lease, chat_id, row['source']))
            return 'ready', row['source'], json.loads(row['payload'])

    def _settle(self, chat_id, source_id, reply_id, status, data, remember):
        with self._transaction() as (db, now):
            changed = db.execute('UPDATE questions SET status=?,payload=COALESCE(?,payload),'
                                 "holder=NULL,leased=NULL WHERE chat=? AND source=? AND status='busy' AND holder=?",
                                 (status, data, chat_id, source_id, reply_id))
            if not changed.rowcount:
                return False
            if remember:
                db.execute('INSERT OR REPLACE INTO answered(chat,reply,expires) VALUES(?,?,?)',
                           (chat_id, reply_id, now + self.ttl))
                db.execute('DELETE FROM answered WHERE rowid IN (SELECT rowid FROM answered '
                           'ORDER BY expires DESC, rowid DESC LIMIT -1 OFFSET ?)', (self.limit * 16,))
            return True

    def finish(self, chat_id, source_id, reply_id):
        return self._settle(chat_id, source_id, reply_id, 'done', '{}', True)

    def release(self, chat_id, source_id, reply_id):
        """Make a failed continuation retryable without consuming its evidence."""
        return self._settle(chat_id, source_id, reply_id, 'pending', None, False)

    def replace(self, chat_id, source_id, reply_id, payload):
        """Advance to another column. Its prompt must be bound again by the caller."""
        data = self._payload(payload)
        with self._transaction() as (db, now):
            changed = db.execute("UPDATE questions SET status='pending',payload=?,holder=NULL,leased=NULL "
                                 "WHERE chat=? AND source=? AND status='busy' AND holder=?",
                                 (data, chat_id, source_id, reply_id))
            if not changed.rowcount:
                return False
            db.execute('DELETE FROM prompts WHERE chat=? AND source=?', (chat_id, source_id))
            db.execute('INSERT OR REPLACE INTO answered(chat,reply,expires) VALUES(?,?,?)',
                       (chat_id, reply_id, now + self.ttl))
            db.execute('DELETE FROM answered WHERE rowid IN (SELECT rowid FROM answered '
                       'ORDER BY expires DESC, rowid DESC LIMIT -1 OFFSET ?)', (self.limit * 16,))
            return True

    def close(self):
        with self._lock:
            if self._db is not None:
                self._db.close()
                self._db = None
