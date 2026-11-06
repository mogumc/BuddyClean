#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""wbctx — WorkBuddy 会话上下文清理工具

WorkBuddy 把每次会话的完整上下文写在 <home>/projects/<项目>/<会话id>.jsonl，
客户端里删掉会话只会给它打一个 deleted_at 标记，磁盘上的文件一律留着。

wbctx 干三件事：

  1. 用客户端自己的 sessions 表判断「哪些会话用户已经删了、哪些还在用」，
     正在用的永远不动。
  2. 清理前先把每个会话里的 token 用量行单独抽出来留下，所以清理完
     BuddySwitch 的统计一分不少（input/output/cache/reasoning 全在）。
  3. 给出「折叠 / 瘦身 / 彻底删除」三档，先列清单等确认，默认只折叠不删。

用法：
    python wbctx.py report                 # 只读报告现状，一个字节都不动
    python wbctx.py plan                   # 预演清理：范围 + 能省多少，也不动
    python wbctx.py clean                  # 折叠已删会话（交互确认后才动手）
    python wbctx.py clean --action slim    # 仅瘦身，保留对话可读
    python wbctx.py clean --action purge   # 彻底删除（先进回收站）
    python wbctx.py verify                 # 对账：token 统计有没有断
    python wbctx.py trash --list           # 看回收站占了多少
    python wbctx.py trash --empty          # 清空回收站

三条命令的边界很清楚：report 只看，plan 只算，clean 才动手。
"""

import argparse
import json
import os
import shutil
import sqlite3
import sys
import tempfile
import time
import unicodedata
from collections import Counter

HOME = os.path.expanduser("~")
# 回收站/台账/基线放哪，运行期再定：默认跟着实际存在的那个数据根走
# （只装了国际版时不能硬塞到 ~/.workbuddy 里），也可用环境变量固定。
STATE_DIR = os.environ.get("WBCTX_STATE") or os.path.join(HOME, ".workbuddy", ".wbctx")
TRASH_DIR = os.path.join(STATE_DIR, "trash")
# 一次运行里的所有文件放同一个时间戳目录，回收站里好找。
RUN_STAMP = time.strftime("%Y%m%d-%H%M%S")
LEDGER = os.path.join(STATE_DIR, "ledger.jsonl")
BASELINE = os.path.join(STATE_DIR, "baseline.json")

# 与 BuddySwitch 的 plugin/builtin/workbuddy.json 保持一致。
# 顺序即优先级：第一个能取到值的就算数。
TOKEN_FIELDS = {
    "input": [
        "providerData.usage.inputTokens",
        "providerData.rawUsage.prompt_tokens",
    ],
    "output": [
        "providerData.usage.outputTokens",
        "providerData.rawUsage.completion_tokens",
    ],
    "cache_read": [
        "providerData.usage.inputTokensDetails[].cached_tokens",
        "providerData.rawUsage.prompt_tokens_details.cached_tokens",
    ],
    "cache_write": [
        "providerData.rawUsage.cache_creation_input_tokens",
        "providerData.rawUsage.prompt_cache_write_tokens",
    ],
    "reasoning": [
        "providerData.usage.outputTokensDetails[].reasoning_tokens",
        "providerData.rawUsage.completion_tokens_details.reasoning_tokens",
    ],
}
TOKEN_KEYS = ("input", "output", "cache_read", "cache_write", "reasoning")

# 折叠后一条用量记录只留这些字段。BuddySwitch 要的是
# timestamp / cwd / sessionId / providerData.usage / providerData.model，
# 其余（arguments、reasoning、rawContent、整段正文）全是体积。
KEEP_TOP = ("id", "parentId", "timestamp", "type", "role", "sessionId",
            "cwd", "name", "callId", "status")
KEEP_PROVIDER = ("messageId", "model", "requestModelId", "requestModelName",
                 "agent", "traceId", "usage", "rawUsage", "stepSeq")

TRUNC_MARK = "…[wbctx 已截断]"
MAX_LINE_PARSE = 64 << 20


# ---------------------------------------------------------------------------
# 基础工具
# ---------------------------------------------------------------------------

def human(n):
    n = float(n)
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return ("%.0f %s" % (n, unit)) if unit == "B" else ("%.1f %s" % (n, unit))
        n /= 1024


def disp_width(text):
    return sum(2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1 for ch in text)


def pad(text, width, right=False):
    gap = width - disp_width(text)
    if gap <= 0:
        return text
    return (" " * gap + text) if right else (text + " " * gap)


def clip(text, width):
    if disp_width(text) <= width:
        return text
    out, used = "", 0
    for ch in text:
        w = 2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1
        if used + w > width - 1:
            break
        out += ch
        used += w
    return out + "…"


def parse_age(text):
    """7d / 12h / 30m / 90s -> 秒；不带单位的数字按秒算。"""
    text = str(text).strip().lower()
    units = {"s": 1, "m": 60, "h": 3600, "d": 86400}
    if text and text[-1] in units:
        return float(text[:-1]) * units[text[-1]]
    return float(text)


def parse_size(text):
    text = str(text).strip().lower()
    units = {"k": 1 << 10, "m": 1 << 20, "g": 1 << 30}
    if text and text[-1] in units:
        return float(text[:-1]) * units[text[-1]]
    return float(text)


def collect(node, path):
    """按 'a.b[].c' 取所有值，返回列表（与 BuddySwitch 的 PathList 语义一致）。"""
    parts = path.split(".")
    out = []

    def rec(cur, i):
        if i == len(parts):
            out.append(cur)
            return
        seg = parts[i]
        is_list = seg.endswith("[]")
        if is_list:
            seg = seg[:-2]
        if not isinstance(cur, dict) or seg not in cur:
            return
        val = cur[seg]
        if is_list:
            if isinstance(val, list):
                for item in val:
                    rec(item, i + 1)
            return
        rec(val, i + 1)

    rec(node, 0)
    return out


def first_number(node, paths):
    for path in paths:
        for val in collect(node, path):
            if isinstance(val, bool):
                continue
            if isinstance(val, (int, float)):
                return int(val)
    return 0


def usage_of(record):
    """返回这条记录的 token 计数；没有 providerData.usage 就返回 None。"""
    pd = record.get("providerData")
    if not isinstance(pd, dict) or not isinstance(pd.get("usage"), dict):
        return None
    raw = {k: first_number(record, v) for k, v in TOKEN_FIELDS.items()}
    # BuddySwitch 的 input_includes_cache=true：缓存命中已单列，从输入里扣掉。
    fresh = raw["input"] - raw["cache_read"] - raw["cache_write"]
    raw["input_fresh"] = fresh if fresh > 0 else 0
    return raw


def add_tokens(acc, tok):
    for k in TOKEN_KEYS:
        acc[k] += tok.get(k, 0)
    acc["input_fresh"] += tok.get("input_fresh", 0)


def zero_tokens():
    d = {k: 0 for k in TOKEN_KEYS}
    d["input_fresh"] = 0
    return d


# ---------------------------------------------------------------------------
# Profile：一个 WorkBuddy 数据根目录（~/.workbuddy 或 ~/.workbuddy-ai）
# ---------------------------------------------------------------------------

class Profile:
    def __init__(self, home):
        self.home = os.path.abspath(home)
        self.name = os.path.basename(self.home.rstrip("\\/"))
        self.projects = os.path.join(self.home, "projects")
        self.db = os.path.join(self.home, "workbuddy.db")
        self.sessions_json = os.path.join(self.home, "app", "sessions.json")


def discover_profiles(roots=None):
    if roots:
        return [Profile(r) for r in roots]
    found = []
    for entry in sorted(os.listdir(HOME)):
        if not entry.startswith(".workbuddy"):
            continue
        home = os.path.join(HOME, entry)
        if os.path.isdir(os.path.join(home, "projects")):
            found.append(Profile(home))
    return found


_STATE_ANCHORED = bool(os.environ.get("WBCTX_STATE"))


def anchor_state(profiles):
    """把回收站/台账/基线落到实际存在的那个数据根里。

    只装了国际版（~/.workbuddy-ai）的机器上，不能硬往 ~/.workbuddy 里塞，
    否则凭空造出一个国内版目录，用户去原目录也找不到回收站。
    """
    global STATE_DIR, TRASH_DIR, LEDGER, BASELINE, _STATE_ANCHORED
    if _STATE_ANCHORED or not profiles:
        return
    homes = [p.home for p in profiles]
    chosen = None
    for home in homes:
        # 之前已经用过哪个，就继续在哪个里找回收站
        if os.path.isdir(os.path.join(home, ".wbctx")):
            chosen = home
            break
    if chosen is None:
        preferred = os.path.join(HOME, ".workbuddy")
        chosen = preferred if preferred in homes else homes[0]
    STATE_DIR = os.path.join(chosen, ".wbctx")
    TRASH_DIR = os.path.join(STATE_DIR, "trash")
    LEDGER = os.path.join(STATE_DIR, "ledger.jsonl")
    BASELINE = os.path.join(STATE_DIR, "baseline.json")
    _STATE_ANCHORED = True


def resolve(args):
    """发现数据根，并把状态目录锚定好。所有命令都走这一条。"""
    profiles = discover_profiles(getattr(args, "root", None))
    anchor_state(profiles)
    return profiles


def no_profile_hint():
    print("没找到任何 WorkBuddy 数据目录。查过这几处：")
    for name in (".workbuddy", ".workbuddy-ai"):
        path = os.path.join(HOME, name)
        if not os.path.isdir(path):
            state = "不存在"
        elif not os.path.isdir(os.path.join(path, "projects")):
            state = "存在但没有 projects 目录"
        else:
            state = "存在"
        print("  %-58s %s" % (path, state))
    print("只装国际版或只装国内版都正常；目录不在这两处时用 --root 指定。")


def load_session_rows(profile):
    """读客户端的 sessions 表。只读：先把 db + wal 拷到临时目录再查。"""
    if not os.path.exists(profile.db):
        print("  ! %s 下没有 workbuddy.db，判断不了哪些会话已删，"
              "全部按「未知」处理、一个都不会清理。" % profile.home, file=sys.stderr)
        return {}
    tmp = tempfile.mkdtemp(prefix="wbctx-db-")
    try:
        for ext in ("", "-wal", "-shm"):
            src = profile.db + ext
            if os.path.exists(src):
                shutil.copy2(src, os.path.join(tmp, "wb.db" + ext))
        con = sqlite3.connect(os.path.join(tmp, "wb.db"))
        con.row_factory = sqlite3.Row
        rows = {}
        for r in con.execute(
            "select id, title, custom_title, deleted_at, last_activity_at, "
            "updated_at, status, transport, cwd from sessions"
        ):
            rows[r["id"]] = dict(r)
        con.close()
        return rows
    except sqlite3.Error as exc:
        print("  ! 读 %s 失败（%s），该目录下所有会话按「未知」处理，不会被清理。"
              % (profile.db, exc), file=sys.stderr)
        return {}
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def load_recent_opens(profile):
    """app/sessions.json 里的会话是最近打开过的，作为额外保护信号。"""
    try:
        with open(profile.sessions_json, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return {}
    out = {}
    for item in data.get("sessions", []) or []:
        cid = item.get("conversationId")
        if cid:
            out[cid] = item
    return out


# ---------------------------------------------------------------------------
# 会话分析
# ---------------------------------------------------------------------------

# 上次跑到一半被打断留下的临时文件，扫描时顺手记一笔，报告里提醒一句。
STALE_TMPS = []


class Session:
    __slots__ = ("profile", "project", "sid", "path", "dir", "size", "mtime",
                 "lines", "usage_lines", "types", "tokens", "first", "last",
                 "models", "title", "state", "row", "fold_size", "fold_lines",
                 "reason", "extras")

    def __init__(self, profile, project, sid, path):
        self.profile = profile
        self.project = project
        self.sid = sid
        self.path = path
        self.dir = os.path.join(os.path.dirname(path), sid)
        st = os.stat(path)
        self.size = st.st_size
        self.mtime = st.st_mtime
        # 同一个会话还会带几个小尾巴：<sid>.file-rollback.ndjson 之类，
        # 跟着会话一起生一起死，清理时要一并算进去。
        self.extras = []
        pdir = os.path.dirname(path)
        for name in os.listdir(pdir):
            full = os.path.join(pdir, name)
            if name.startswith(sid + ".") and not name.endswith(".jsonl") \
                    and not name.endswith(".wbctx-tmp") and os.path.isfile(full):
                self.extras.append(full)
        if os.path.exists(path + ".wbctx-tmp"):
            STALE_TMPS.append(path + ".wbctx-tmp")
        self.lines = 0
        self.usage_lines = 0
        self.types = Counter()
        self.tokens = zero_tokens()
        self.first = 0
        self.last = 0
        self.models = set()
        self.title = ""
        self.state = "orphan"
        self.row = None
        self.fold_size = 0
        self.fold_lines = 0
        self.reason = ""

    @property
    def key(self):
        return "%s/%s/%s" % (self.profile.name, self.project, self.sid)

    @property
    def sidecar_size(self):
        total = 0
        if os.path.isdir(self.dir):
            for dp, _dn, fn in os.walk(self.dir):
                for f in fn:
                    try:
                        total += os.path.getsize(os.path.join(dp, f))
                    except OSError:
                        pass
        return total

    @property
    def total_size(self):
        return self.size + self.sidecar_size + self.extra_size

    @property
    def extra_size(self):
        total = 0
        for p in self.extras:
            try:
                total += os.path.getsize(p)
            except OSError:
                pass
        return total

    @property
    def activity(self):
        if self.row and self.row.get("last_activity_at"):
            return int(self.row["last_activity_at"]) / 1000.0
        return self.last / 1000.0 if self.last else self.mtime

    def when(self):
        ts = self.activity
        return time.strftime("%Y-%m-%d %H:%M", time.localtime(ts)) if ts else "-"


def analyze(session):
    """流式扫一遍文件：统计类型分布、token 用量、时间范围，并预演折叠后体积。"""
    try:
        fh = open(session.path, "rb")
    except OSError as exc:
        session.reason = "打不开：%s" % exc
        return session

    fold_bytes = 0
    fold_lines = 0
    with fh:
        for raw in fh:
            session.lines += 1
            stripped = raw.strip()
            if not stripped:
                continue
            try:
                text = stripped.decode("utf-8", "replace")
            except Exception:
                continue

            is_meta = '"session-meta"' in text
            is_title = '"aiTitle"' in text
            if '"providerData"' not in text and not is_meta and not is_title:
                # 绝大多数行是正文/工具结果，连解析都省掉。
                continue
            if len(stripped) > MAX_LINE_PARSE:
                continue
            try:
                rec = json.loads(text)
            except ValueError:
                continue

            rtype = rec.get("type") or "?"
            session.types[rtype] += 1

            if is_meta or is_title:
                fold_bytes += len(stripped) + 1
                fold_lines += 1
                if is_title and not session.title:
                    session.title = str(rec.get("aiTitle") or "")
                continue

            tok = usage_of(rec)
            if tok is None:
                continue

            session.usage_lines += 1
            add_tokens(session.tokens, tok)
            fold_bytes += len(json.dumps(slim_record(rec), ensure_ascii=False).encode("utf-8")) + 1
            fold_lines += 1

            ts = rec.get("timestamp")
            if isinstance(ts, (int, float)):
                ts = int(ts)
                if not session.first or ts < session.first:
                    session.first = ts
                if ts > session.last:
                    session.last = ts
            for path in ("providerData.requestModelName", "providerData.model"):
                vals = collect(rec, path)
                if vals and isinstance(vals[0], str):
                    session.models.add(vals[0])
                    break

    session.fold_size = fold_bytes
    session.fold_lines = fold_lines
    if not session.title:
        session.title = session.sid
    return session


def slim_record(rec):
    """把一条用量记录压到只够统计用。"""
    out = {}
    for k in KEEP_TOP:
        if k in rec and rec[k] is not None:
            out[k] = rec[k]
    pd = rec.get("providerData") or {}
    kept_pd = {}
    for k in KEEP_PROVIDER:
        if k in pd and pd[k] is not None:
            kept_pd[k] = pd[k]
    if kept_pd:
        out["providerData"] = kept_pd
    return out


def scan_profiles(profiles, min_activity=0.0):
    STALE_TMPS.clear()
    sessions = []
    for profile in profiles:
        rows = load_session_rows(profile)
        recent = load_recent_opens(profile)
        root = profile.projects
        if not os.path.isdir(root):
            continue
        for project in sorted(os.listdir(root)):
            pdir = os.path.join(root, project)
            if not os.path.isdir(pdir):
                continue
            for fn in sorted(os.listdir(pdir)):
                if not fn.endswith(".jsonl"):
                    continue
                path = os.path.join(pdir, fn)
                if not os.path.isfile(path):
                    continue
                sess = Session(profile, project, fn[:-6], path)
                sess.row = rows.get(sess.sid)
                analyze(sess)
                sess.state, sess.reason = classify(sess, rows, recent)
                sessions.append(sess)
    return sessions


def classify(sess, rows, recent):
    """返回 (状态, 说明)。状态只有 alive / deleted / orphan 三种。

    判断依据是客户端自己的 sessions.deleted_at：
      NULL / 0 / -1  = 用户没删，界面里还挂着 → 绝不动
      正数时间戳      = 用户在界面里删掉了，只是文件没回收 → 可清理
    数据库里查不到记录时，退回 app/sessions.json 的「最近打开」名单，
    再查不到就算未知，默认不动。
    """
    row = sess.row
    if row is None:
        if sess.sid in recent:
            return "alive", "数据库无记录，但最近打开过"
        return "orphan", "客户端数据库里没有这条记录"
    deleted_at = row.get("deleted_at")
    if deleted_at is None or int(deleted_at) <= 0:
        # 云端会话用 -1 表示「占位但没删」，一并按在用处理。
        return "alive", "客户端里仍在"
    ts = int(deleted_at)
    return "deleted", "用户已删除（%s）" % time.strftime("%Y-%m-%d %H:%M", time.localtime(ts / 1000.0))


# ---------------------------------------------------------------------------
# 报告
# ---------------------------------------------------------------------------

STATE_MARK = {"alive": "在用", "deleted": "已删", "orphan": "未知"}
STATE_ORDER = {"alive": 0, "deleted": 1, "orphan": 2}


def print_report(sessions, show_all=True, with_plan=False):
    """现状清单。with_plan 时额外带上「清理后会有多大」的推演列。"""
    if not sessions:
        print("没有找到任何会话文件。")
        return

    groups = {}
    for s in sessions:
        groups.setdefault((s.profile.name, s.project), []).append(s)

    header = (pad("  ", 6) + pad("会话", 10) + pad("标题", 30) +
              pad("大小", 10, True))
    if with_plan:
        header += pad("折叠后", 10, True)
    header += (pad("输入", 12, True) + pad("输出", 11, True) +
               "  " + pad("最后活动", 16))
    print(header)
    print("-" * disp_width(header))

    for (pname, project), items in sorted(groups.items()):
        items.sort(key=lambda s: (STATE_ORDER[s.state], -s.total_size))
        total = sum(s.total_size for s in items)
        print()
        print(highlight("%s / %s" % (pname, project)) +
              "   %d 个会话  %s" % (len(items), human(total)))
        for s in items:
            if not show_all and s.state == "alive":
                continue
            line = (pad("  " + STATE_MARK[s.state], 6) +
                    pad(s.sid[:8], 10) +
                    pad(clip(s.title, 30), 30) +
                    pad(human(s.total_size), 10, True))
            if with_plan:
                line += pad("-" if s.state == "alive" else human(s.fold_size), 10, True)
            line += (pad("{:,}".format(s.tokens["input_fresh"]), 12, True) +
                     pad("{:,}".format(s.tokens["output"]), 11, True) +
                     "  " + pad(s.when(), 16))
            print(line)

    print()
    print_summary(sessions, with_plan=with_plan)
    print()
    if STALE_TMPS:
        tmp_size = 0
        for p in STALE_TMPS:
            try:
                tmp_size += os.path.getsize(p)
            except OSError:
                pass
        print("提示：有 %d 个上次中断留下的 .wbctx-tmp 临时文件（%s），"
              "跑 clean 时会自动清掉。" % (len(STALE_TMPS), human(tmp_size)))
    print("（只读扫描，没有改动任何文件。想看清理能省多少：wbctx.py plan）")


def print_summary(sessions, with_plan=False):
    by_state = {}
    for s in sessions:
        by_state.setdefault(s.state, []).append(s)

    total = sum(s.total_size for s in sessions)
    print(highlight("合计 %d 个会话，占用 %s" % (len(sessions), human(total))))
    for state in ("alive", "deleted", "orphan"):
        items = by_state.get(state, [])
        if not items:
            continue
        size = sum(s.total_size for s in items)
        tok = zero_tokens()
        for s in items:
            add_tokens(tok, s.tokens)
        line = "  %s %2d 个  %10s" % (STATE_MARK[state], len(items), human(size))
        if with_plan and state != "alive":
            folded = sum(s.fold_size for s in items)
            line += "  折叠后 %8s（省 %s）" % (human(folded), human(max(size - folded, 0)))
        line += "  token 输入 %s / 输出 %s" % (
            "{:,}".format(tok["input_fresh"]), "{:,}".format(tok["output"]))
        print(line)

    if by_state.get("orphan"):
        print()
        print("  注意：有 %d 个会话在客户端数据库里找不到记录，默认不动，"
              "需要 --include-unknown 才会处理。" % len(by_state["orphan"]))


# ---------------------------------------------------------------------------
# 动作
# ---------------------------------------------------------------------------

def fold_file(session, backup=True):
    """只保留用量记录 + 标题 + 元信息，其余行丢掉。"""
    body = []
    kept = 0
    with open(session.path, "rb") as fh:
        for raw in fh:
            stripped = raw.strip()
            if not stripped:
                continue
            try:
                text = stripped.decode("utf-8", "replace")
            except Exception:
                continue
            is_meta = '"session-meta"' in text
            is_title = '"aiTitle"' in text
            if is_meta or is_title:
                body.append(stripped)
                kept += 1
                continue
            if '"providerData"' not in text:
                continue
            if len(stripped) > MAX_LINE_PARSE:
                continue
            try:
                rec = json.loads(text)
            except ValueError:
                continue
            if usage_of(rec) is None:
                continue
            body.append(json.dumps(slim_record(rec), ensure_ascii=False).encode("utf-8"))
            kept += 1

    if backup:
        stash(session)

    swap_in(session.path, body)

    if os.path.isdir(session.dir):
        shutil.rmtree(session.dir, ignore_errors=True)
    drop_extras(session)
    return kept, len(body)


def slim_file(session, keep_chars, drop_sidecar=False, backup=True):
    """保留全部记录，只把大字段截断。用在还想继续读/接着聊的会话上。"""
    body = []
    for rec in iter_records(session.path):
        touch_record(rec, keep_chars)
        body.append(json.dumps(rec, ensure_ascii=False).encode("utf-8"))

    if backup:
        stash(session)

    swap_in(session.path, body)

    if drop_sidecar and os.path.isdir(session.dir):
        shutil.rmtree(session.dir, ignore_errors=True)
    return len(body), len(body)


def iter_records(path):
    with open(path, "rb") as fh:
        for raw in fh:
            stripped = raw.strip()
            if not stripped:
                continue
            if len(stripped) > MAX_LINE_PARSE:
                continue
            try:
                yield json.loads(stripped.decode("utf-8", "replace"))
            except ValueError:
                continue


def trunc(value, limit):
    if isinstance(value, str) and len(value) > limit:
        return value[:limit] + TRUNC_MARK
    return value


def touch_record(rec, limit):
    """就地把一条记录里的大文本砍短。"""
    long_limit = limit * 4
    for key in ("content", "rawContent"):
        items = rec.get(key)
        if isinstance(items, list):
            for item in items:
                if isinstance(item, dict) and "text" in item:
                    item["text"] = trunc(item["text"], long_limit)
    pd = rec.get("providerData")
    if isinstance(pd, dict):
        pd.pop("reasoning", None)
        if "argumentsDisplayText" in pd:
            pd["argumentsDisplayText"] = trunc(pd["argumentsDisplayText"], limit)
    if "arguments" in rec:
        rec["arguments"] = trunc(rec["arguments"], limit)
    out = rec.get("output")
    if isinstance(out, dict):
        if "text" in out:
            out["text"] = trunc(out["text"], long_limit)
        if isinstance(out.get("content"), list):
            for item in out["content"]:
                if isinstance(item, dict) and "text" in item:
                    item["text"] = trunc(item["text"], long_limit)
    snap = rec.get("snapshot")
    if isinstance(snap, dict) and isinstance(snap.get("trackedFileBackups"), dict):
        snap["trackedFileBackups"] = {}
    return rec


def swap_in(path, lines):
    """把新内容原子换进去；中途失败不留 .wbctx-tmp 垃圾。"""
    tmp = path + ".wbctx-tmp"
    try:
        with open(tmp, "wb") as fh:
            for line in lines:
                fh.write(line)
                fh.write(b"\n")
        os.replace(tmp, path)
    except OSError:
        if os.path.exists(tmp):
            try:
                os.remove(tmp)
            except OSError:
                pass
        raise


def drop_extras(session):
    for p in session.extras:
        try:
            os.remove(p)
        except OSError:
            pass


def stash(session):
    """把原文件挪进回收站（保留目录结构，方便手动还原）。"""
    dest_dir = os.path.join(TRASH_DIR, RUN_STAMP,
                            session.profile.name, session.project)
    os.makedirs(dest_dir, exist_ok=True)
    dest = os.path.join(dest_dir, session.sid + ".jsonl")
    idx = 0
    while os.path.exists(dest):
        idx += 1
        dest = os.path.join(dest_dir, "%s.%d.jsonl" % (session.sid, idx))
    shutil.copy2(session.path, dest)
    return dest


def purge_session(session, hard=False):
    """彻底删掉会话文件与侧车目录。默认先移进回收站。"""
    if hard:
        try:
            os.remove(session.path)
        except OSError as exc:
            return False, "删不掉：%s" % exc
        shutil.rmtree(session.dir, ignore_errors=True)
        drop_extras(session)
    else:
        dest_dir = os.path.join(TRASH_DIR, RUN_STAMP,
                                session.profile.name, session.project)
        os.makedirs(dest_dir, exist_ok=True)
        try:
            shutil.move(session.path, os.path.join(dest_dir, session.sid + ".jsonl"))
            if os.path.isdir(session.dir):
                shutil.move(session.dir, os.path.join(dest_dir, session.sid))
            for p in session.extras:
                shutil.move(p, os.path.join(dest_dir, os.path.basename(p)))
        except OSError as exc:
            return False, "移不动（可能被占用）：%s" % exc
    return True, ""


def ledger_append(entry):
    os.makedirs(STATE_DIR, exist_ok=True)
    with open(LEDGER, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(entry, ensure_ascii=False) + "\n")


def token_map(sessions):
    return {s.key: dict(s.tokens, size=s.size, sid=s.sid, title=s.title)
            for s in sessions}


def save_baseline(payload):
    os.makedirs(STATE_DIR, exist_ok=True)
    with open(BASELINE, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False, indent=1)


# ---------------------------------------------------------------------------
# 选择与确认
# ---------------------------------------------------------------------------

def slim_size(session, keep_chars, drop_sidecar):
    """真算一遍瘦身后的体积：逐行套用同样的截断规则再量长度。"""
    total = 0
    for rec in iter_records(session.path):
        touch_record(rec, keep_chars)
        total += len(json.dumps(rec, ensure_ascii=False).encode("utf-8")) + 1
    if not drop_sidecar:
        total += session.sidecar_size
    return total


def select_sessions(sessions, args):
    """按参数筛出这次要动的会话。"""
    now = time.time()
    picked = []
    skipped_recent = []
    for s in sessions:
        if s.state == "alive" and not args.include_alive:
            continue
        if s.state == "orphan" and not args.include_unknown:
            continue
        if args.min_age and now - s.activity < args.min_age:
            continue
        if args.min_size and s.total_size < args.min_size:
            continue
        if args.projects and s.project not in args.projects:
            continue
        # 文件刚被写过，说明可能正开着这个会话 —— 无条件让开。
        if now - s.mtime < args.lock_grace:
            skipped_recent.append(s)
            continue
        picked.append(s)

    if args.select:
        wanted = set()
        for token in args.select.replace(" ", "").split(","):
            if not token:
                continue
            wanted.add(token.lower())
        filtered = []
        for s in picked:
            if s.sid.lower() in wanted or s.sid[:8].lower() in wanted:
                filtered.append(s)
            elif s.project.lower() in wanted:
                filtered.append(s)
        picked = filtered

    if args.exclude:
        drop = set(t.lower() for t in args.exclude.replace(" ", "").split(",") if t)
        picked = [s for s in picked
                  if s.sid.lower() not in drop and s.sid[:8].lower() not in drop]
    return picked, skipped_recent


def confirm(prompt, assume_yes):
    if assume_yes:
        return True
    try:
        answer = input(prompt)
    except (EOFError, KeyboardInterrupt):
        print()
        return False
    return answer.strip().lower() in ("y", "yes", "是")


# ---------------------------------------------------------------------------
# 命令
# ---------------------------------------------------------------------------

def cmd_report(args):
    profiles = resolve(args)
    if not profiles:
        no_profile_hint()
        return 1
    sessions = scan_profiles(profiles)
    if args.json:
        payload = []
        for s in sessions:
            payload.append({
                "profile": s.profile.name, "project": s.project, "session": s.sid,
                "state": s.state, "title": s.title, "size": s.size,
                "sidecar": s.sidecar_size, "folded": s.fold_size,
                "usage_lines": s.usage_lines, "lines": s.lines,
                "types": dict(s.types), "tokens": s.tokens,
                "activity": int(s.activity), "reason": s.reason,
            })
        print(json.dumps(payload, ensure_ascii=False, indent=1))
        return 0
    print_report(sessions, show_all=not args.only_deleted, with_plan=args.plan)
    return 0


def cmd_clean(args):
    profiles = resolve(args)
    if not profiles:
        no_profile_hint()
        return 1

    if not args.action:
        args.action = "fold"
    action = args.action

    print("扫描 %s ..." % ", ".join(os.path.basename(p.home) for p in profiles))
    sessions = scan_profiles(profiles)
    picked, skipped_recent = select_sessions(sessions, args)

    if skipped_recent:
        print("跳过 %d 个刚被写入的会话（可能正开着）："
              % len(skipped_recent) +
              "、".join(s.sid[:8] for s in skipped_recent[:5]) +
              ("…" if len(skipped_recent) > 5 else ""))

    if not picked:
        print("没有符合条件、可以处理的会话。")
        if any(s.state == "alive" for s in sessions):
            print("（在用会话默认跳过；确实要动它，加 --include-alive）")
        return 0

    # 瘦身的体积要真算，光按比例估会差一半。
    if action == "slim":
        for s in picked:
            s.fold_size = slim_size(s, args.keep_chars, args.drop_sidecar)

    before = sum(s.total_size for s in picked)
    after = 0
    for s in picked:
        if action == "purge":
            after += 0
        elif action == "slim":
            after += s.fold_size
        else:
            after += s.fold_size

    print()
    print(highlight("将要执行：%s" % ACTION_LABEL[action]))
    print()
    for s in sorted(picked, key=lambda x: -x.total_size):
        mark = STATE_MARK[s.state]
        if action == "purge":
            tail = "删除 %s" % human(s.total_size)
        else:
            tail = "%s → %s" % (human(s.total_size), human(s.fold_size))
        print("  [%s] %-8s %s" % (mark, s.sid[:8], clip(s.title, 34)))
        print("        %s%s · %s · token 输入 %s / 输出 %s" % (
            tail, "/侧车 %s" % human(s.sidecar_size) if s.sidecar_size else "",
            pad(s.project, 20),
            "{:,}".format(s.tokens["input_fresh"]),
            "{:,}".format(s.tokens["output"])))

    print()
    print("%d 个会话，%s → %s（释放 %s）"
          % (len(picked), human(before), human(after), human(max(before - after, 0))))
    if action != "purge":
        kept_tok = sum(s.tokens["input_fresh"] for s in picked)
        print("token 记录保留 %s 输入（不动用量行，统计不断档）"
              % "{:,}".format(kept_tok))
    else:
        print("注意：彻底删除会让这些会话从 BuddySwitch 的统计里消失，")
        print("      明细会写进 %s 留底。" % LEDGER)

    if args.dry_run:
        print()
        print("（预演，没有改动任何文件。要真执行：把 plan 换成 clean）")
        return 0

    print()
    if not confirm("确认执行？[y/N] ", args.yes):
        print("已取消，没有改动任何文件。")
        return 0

    ok, failed, skipped = 0, [], []
    stale = 0
    for s in picked:
        # 上一次跑到一半被打断留下的临时文件，先清掉
        tmp = s.path + ".wbctx-tmp"
        if os.path.exists(tmp):
            try:
                os.remove(tmp)
                stale += 1
            except OSError:
                pass
    if stale:
        print("  清掉 %d 个上次残留的临时文件" % stale)

    for s in picked:
        try:
            if action == "purge":
                done, err = purge_session(s, hard=args.hard)
                if not done:
                    skipped.append((s, err))
                    continue
                ledger_append({
                    "ts": int(time.time()), "action": "purge",
                    "profile": s.profile.name, "project": s.project,
                    "session": s.sid, "title": s.title,
                    "size": s.total_size, "tokens": s.tokens,
                    "activity": int(s.activity),
                })
            elif action == "slim":
                slim_file(s, args.keep_chars, args.drop_sidecar, not args.no_backup)
            else:
                fold_file(s, backup=not args.no_backup)
            ok += 1
            print("  ok  %s  %s" % (s.sid[:8], clip(s.title, 30)))
        except PermissionError as exc:
            why = "写不进去（文件被占用或没有权限，WorkBuddy 可能正开着）：%s" % exc.filename
            failed.append((s, why))
            print("  !!  %s  %s" % (s.sid[:8], why))
        except OSError as exc:
            failed.append((s, str(exc)))
            print("  !!  %s  %s" % (s.sid[:8], exc))
        except Exception as exc:  # noqa: BLE001 单条失败不影响整体
            failed.append((s, repr(exc)))
            print("  !!  %s  %r" % (s.sid[:8], exc))

    print()
    print(highlight("完成：成功 %d，失败 %d，跳过 %d" % (ok, len(failed), len(skipped))))
    for s, err in failed + skipped:
        print("  %s：%s" % (s.sid[:8], err))

    # 清理空目录：只收拾这次真正动过、而且已经空了的那几个，
    # 原本就空的项目目录一律留着（客户端可能拿它记工作区）。
    if action in ("purge", "fold"):
        drop_empty_projects({os.path.dirname(s.path) for s in picked})

    # 对账：重新扫一遍，确认 token 没丢；再把当前状态存成新基线
    expected = token_map(picked)
    print()
    print("对账中 ...")
    after_sessions = scan_profiles(profiles)
    if action in ("fold", "slim"):
        compare_tokens(expected, after_sessions, verbose=True)
    save_baseline({
        "when": time.strftime("%Y-%m-%d %H:%M:%S"),
        "action": action,
        "sessions": token_map(after_sessions),
    })
    if not args.no_backup:
        print()
        print("原文件已备份在 %s，确认没问题后 wbctx.py trash --empty 回收。" % TRASH_DIR)
    return 0 if not failed else 2


def drop_empty_projects(dirs):
    for pdir in sorted(dirs):
        if os.path.isdir(pdir) and not os.listdir(pdir):
            try:
                os.rmdir(pdir)
                print("  移除已空的会话目录 %s" % os.path.basename(pdir))
            except OSError:
                pass


def compare_tokens(expected, sessions, verbose=False):
    """逐会话比对 token 合计，返回不一致的条数。"""
    now = {s.key: s for s in sessions}
    bad = []
    for key, exp in expected.items():
        sess = now.get(key)
        if sess is None:
            bad.append((key, "会话文件不见了"))
            continue
        for field in ("input", "output", "cache_read", "cache_write", "reasoning"):
            if sess.tokens[field] != exp.get(field, 0):
                bad.append((key, "%s 从 %s 变成 %s" % (
                    field, "{:,}".format(exp.get(field, 0)),
                    "{:,}".format(sess.tokens[field]))))
                break
    if verbose:
        if not bad:
            print(highlight("  对账通过：%d 个会话的 token 记录逐项一致。" % len(expected)))
        else:
            print("  !! 有 %d 个会话的 token 对不上：" % len(bad))
            for key, why in bad[:20]:
                print("     %s  %s" % (key, why))
            print("     原文件在 %s，可以直接拷回去。" % TRASH_DIR)
    return len(bad)


def cmd_verify(args):
    profiles = resolve(args)  # 先锚定，才知道基线文件在哪
    if not os.path.exists(BASELINE):
        print("还没有基线（%s）。跑一次 clean 之后会自动记一份。" % BASELINE)
        return 1
    with open(BASELINE, "r", encoding="utf-8") as fh:
        base = json.load(fh)
    expected = base.get("sessions", {})
    sessions = scan_profiles(profiles)

    print("基线时间：%s（执行的动作：%s）" % (base.get("when", "?"), base.get("action", "?")))
    print("基线会话：%d 个\n" % len(expected))
    bad = compare_tokens(expected, sessions)
    if bad == 0:
        print(highlight("全部一致，可以放心清空回收站。"))
        return 0
    print()
    print("有 %d 个会话与基线不符。" % bad)
    return 2


def cmd_trash(args):
    resolve(args)  # 先把状态目录锚到正确的数据根上
    if not os.path.isdir(TRASH_DIR):
        print("回收站是空的（%s）。" % TRASH_DIR)
        return 0
    total, count = 0, 0
    for dp, _dn, fn in os.walk(TRASH_DIR):
        for f in fn:
            try:
                total += os.path.getsize(os.path.join(dp, f))
                count += 1
            except OSError:
                pass
    if args.empty:
        if not confirm("清空回收站（%s，%d 个文件）？此操作不可恢复 [y/N] " % (human(total), count),
                       args.yes):
            print("已取消。")
            return 0
        shutil.rmtree(TRASH_DIR, ignore_errors=True)
        print("已清空，释放 %s。" % human(total))
        return 0
    print("回收站：%s / %d 个文件" % (human(total), count))
    for entry in sorted(os.listdir(TRASH_DIR)):
        path = os.path.join(TRASH_DIR, entry)
        size = 0
        for dp, _dn, fn in os.walk(path):
            for f in fn:
                try:
                    size += os.path.getsize(os.path.join(dp, f))
                except OSError:
                    pass
        print("  %s  %s" % (entry, human(size)))
    print("\n要还原：把对应目录里的文件拷回 <home>/projects/<项目>/ 即可。")
    return 0


ACTION_LABEL = {
    "fold": "折叠已删会话（保留 token 用量行，丢掉正文/工具结果/思考过程）",
    "slim": "瘦身（保留对话结构，只截断超长字段）",
    "purge": "彻底删除（移入回收站，token 历史只剩台账）",
}


def add_filter_args(p):
    """clean / plan 共用的筛选参数。"""
    p.add_argument("--action", choices=["fold", "slim", "purge"], help="fold=折叠 slim=瘦身 purge=删除")
    p.add_argument("--select", help="只处理这些会话（会话 id 前缀或项目名，逗号分隔）")
    p.add_argument("--exclude", help="排除这些会话")
    p.add_argument("--projects", action="append", help="只处理这些项目目录")
    p.add_argument("--min-age", type=parse_age, default=0, help="只处理闲置超过该时长的会话，如 7d")
    p.add_argument("--min-size", type=parse_size, default=0, help="只处理大于该体积的会话，如 5MB")
    p.add_argument("--include-alive", action="store_true", help="也处理客户端里还在用的会话（危险）")
    p.add_argument("--include-unknown", action="store_true", help="也处理数据库里查不到的会话")
    p.add_argument("--keep-chars", type=int, default=600, help="slim 时单个文本字段保留的字符数")
    p.add_argument("--lock-grace", type=parse_age, default=180,
                   help="最近多少秒内被写过的会话一律跳过，默认 180")
    p.add_argument("--drop-sidecar", action="store_true", help="slim 时连同 tool-results 侧车一起删")
    p.add_argument("--no-backup", action="store_true", help="不留回收站备份（省一次写盘）")
    p.add_argument("--hard", action="store_true", help="purge 时直接删，不进回收站")
    return p


def build_parser():
    p = argparse.ArgumentParser(
        prog="wbctx", description="WorkBuddy 会话上下文清理工具",
        formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--root", action="append", metavar="DIR",
                   help="指定数据根目录（默认自动发现 ~/.workbuddy 和 ~/.workbuddy-ai）")
    sub = p.add_subparsers(dest="cmd")

    r = sub.add_parser("report", help="只读扫描，报告现状（不改任何文件）")
    r.add_argument("--json", action="store_true", help="输出 JSON")
    r.add_argument("--only-deleted", action="store_true", help="只显示可以清理的会话")
    r.add_argument("--plan", action="store_true", help="额外带上清理后的体积推演")
    r.set_defaults(func=cmd_report)

    pl = sub.add_parser("plan", help="预演清理：列出范围和省多少，不动文件")
    add_filter_args(pl)
    pl.set_defaults(func=cmd_clean, dry_run=True, yes=False)

    c = sub.add_parser("clean", help="执行清理（默认折叠已删会话）")
    add_filter_args(c)
    c.add_argument("--dry-run", action="store_true", help="只出清单，不动文件")
    c.add_argument("-y", "--yes", action="store_true", help="跳过确认")
    c.set_defaults(func=cmd_clean)

    v = sub.add_parser("verify", help="和基线对账，确认 token 统计没断")
    v.set_defaults(func=cmd_verify)

    t = sub.add_parser("trash", help="查看/清空回收站")
    t.add_argument("--list", action="store_true", help="列出回收站内容（不带参数时的默认行为）")
    t.add_argument("--empty", action="store_true", help="清空")
    t.add_argument("-y", "--yes", action="store_true", help="跳过确认")
    t.set_defaults(func=cmd_trash)
    return p


def highlight(text):
    if not sys.stdout.isatty():
        return text
    return "\033[1m" + text + "\033[0m"


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "cmd", None):
        parser.print_help()
        return 0
    if not hasattr(args, "projects"):
        args.projects = None
    if not hasattr(args, "exclude"):
        args.exclude = None
    if not hasattr(args, "select"):
        args.select = None
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
