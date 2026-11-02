"""dafeiyu_brain3d —— 把她脑子的 3D 视图做成 AstrBot 插件页面。

做三件事：
  1. 读 memory_brain 的 brain.db（含 -wal，否则新记忆看不到）
  2. 导出成 3D 要的 {nodes, edges, memories} JSON，写进自己的 pages/panel/data.json
  3. 定时刷新 + 提供 /脑3d 指令手动刷新

页面本身是纯静态（three.js + 真实皮层网格都在 pages/panel 下），
AstrBot 用带 token 的路径服务插件页面资源，所以 data.json 用相对路径就能取到。
"""

from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import time
from collections import defaultdict

from astrbot.api import logger
from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Context, Star, register

PLUGIN_NAME = "dafeiyu_brain3d"
BRAIN_DB = "/AstrBot/data/plugin_data/astrbot_plugin_memory_brain/brain.db"
PAGE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "pages", "panel")
DATA_FILE = os.path.join(PAGE_DIR, "data.json")

TYPE_MAP = {"person": "person", "people": "person", "place": "place", "location": "place",
            "topic": "topic", "skill": "skill", "memory": "memory"}



def _now() -> float:
    return time.time()


ANCHOR_PATHS = [
    "/AstrBot/data/plugins/dafeiyu_brain/persona.json",
    "/AstrBot/data/plugin_data/dafeiyu_brain/persona.json",
]


def load_anchors() -> list[str]:
    """读人格锚点（她每轮必须守住的东西）。"""
    for p in ANCHOR_PATHS:
        try:
            with open(p, encoding="utf-8") as f:
                data = json.load(f)
            anchors = data.get("core_anchor") or []
            if anchors:
                return [str(a) for a in anchors]
        except Exception:
            continue
    return []


MOOD_PATHS = [
    "/AstrBot/data/plugin_data/astrbot_plugin_persona_state/mood.json",
]


def load_mood() -> dict:
    """读 persona_state 的 PAD 情绪，换算成四条进度条。"""
    for p in MOOD_PATHS:
        try:
            with open(p, encoding="utf-8") as f:
                data = json.load(f)
        except Exception:
            continue
        pad = data.get("pad") if isinstance(data.get("pad"), dict) else data
        try:
            ple = float(pad.get("p", 0.0)); aro = float(pad.get("a", 0.35))
        except (TypeError, ValueError):
            ple, aro = 0.0, 0.35
        labels = pad.get("labels") if isinstance(pad.get("labels"), dict) else {}
        top = sorted(labels.items(), key=lambda kv: -float(kv[1] or 0))[:2] if labels else []
        return {
            "joy": max(0.0, min(1.0, (ple + 1) / 2)),
            "calm": max(0.0, min(1.0, 1 - aro)),
            "down": max(0.0, min(1.0, -ple)),
            "irritated": max(0.0, min(1.0, aro * (1.0 if ple < 0 else 0.4))),
            "labels": [{"name": k, "score": round(float(v or 0), 2)} for k, v in top],
            "trigger": str(pad.get("trigger") or "")[:24],
            "available": True,
        }
    return {"available": False}


# ------------------------------------------------------------------ 时间线

import re as _re

MEMDB = "/AstrBot/data/plugin_data/dafeiyu_brain/memory.db"
MAINDB_CANDIDATES = [
    "/AstrBot/data/main_db.json",
    "/AstrBot/data/databases/main_db.json",
    "/AstrBot/data/data_v4.db",
    "/AstrBot/data/databases/data_v4.db",
]


def _find_maindb() -> str:
    for c in MAINDB_CANDIDATES:
        if os.path.exists(c):
            return c
    return ""
IMG_DIR = os.path.join(PAGE_DIR, "images")
TIMELINE_FILE = os.path.join(PAGE_DIR, "timeline.json")
DATA_DIR = "/AstrBot/data/plugin_data/dafeiyu_brain3d"
EDITS_FILE = os.path.join(DATA_DIR, "edits.json")

_STOP = set("的了在是我你她他它们这那就也不都个有一和与或说会要可以吗呢吧啊哦嗯很")


def _bigrams(text: str) -> set[str]:
    t = _re.sub(r"[^一-龥A-Za-z0-9]", "", str(text or ""))
    return {t[i:i + 2] for i in range(len(t) - 1) if t[i] not in _STOP}


def _ts(v) -> float:
    """把各种时间表示统一成 epoch 秒。"""
    if isinstance(v, (int, float)):
        return float(v)
    s = str(v or "").strip()
    if not s:
        return 0.0
    for fmt in ("%Y-%m-%dT%H:%M:%S.%f", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            return time.mktime(time.strptime(s[:26].rstrip("Z"), fmt))
        except Exception:
            continue
    return 0.0


def load_timeline_entries() -> list[dict]:
    """时间线条目：旧库的 episodes（有真实日期）+ 新库新写的记忆。"""
    out: list[dict] = []
    if os.path.exists(MEMDB):
        try:
            db = sqlite3.connect(MEMDB, timeout=10)
            db.row_factory = sqlite3.Row
            for r in db.execute(
                    "SELECT id, occurred_at, gist, importance, people, kind FROM episodes"):
                out.append({
                    "id": f"ep{r['id']}", "src": "episodes",
                    "date": str(r["occurred_at"] or "")[:19],
                    "ts": _ts(r["occurred_at"]),
                    "text": r["gist"] or "",
                    "importance": float(r["importance"] or 0.7),
                    "kind": r["kind"] or "episode",
                    "people": r["people"] or "",
                    "images": [],
                })
            db.close()
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[brain3d] 读 memory.db 失败：{exc!r}")
    if os.path.exists(BRAIN_DB):
        try:
            db = sqlite3.connect(BRAIN_DB, timeout=10)
            db.row_factory = sqlite3.Row
            for r in db.execute(
                    "SELECT id, text, created_at, importance, kind, source FROM memories "
                    "WHERE status='active' AND source NOT LIKE 'migrate:%'"):
                ts = float(r["created_at"] or 0)
                out.append({
                    "id": f"bm{r['id']}", "src": "brain",
                    "date": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ts)) if ts else "",
                    "ts": ts, "text": r["text"] or "",
                    "importance": float(r["importance"] or 0.5),
                    "kind": r["kind"] or "episode",
                    "people": "", "images": [],
                })
            db.close()
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[brain3d] 读 brain.db 失败：{exc!r}")
    if os.path.exists(CORE_DB):
        try:
            db = sqlite3.connect(f"file:{CORE_DB}?mode=ro", uri=True, timeout=10)
            db.row_factory = sqlite3.Row
            for r in db.execute(
                    "SELECT id, text, t_anchor, created_at, importance, kind, pool, weight "
                    "FROM memories ORDER BY id DESC LIMIT 2000"):
                ts = float(r["t_anchor"] or r["created_at"] or 0)
                out.append({
                    "id": f"mc{r['id']}", "src": "mindcore",
                    "date": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ts)) if ts else "",
                    "ts": ts, "text": r["text"] or "",
                    "importance": float(r["weight"] or r["importance"] or 0.5),
                    "kind": r["kind"] or "episode",
                    "people": "", "images": [], "pool": r["pool"] or "",
                })
            db.close()
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[brain3d] 读心智库时间线失败：{exc!r}")
    out.sort(key=lambda x: x["ts"])
    return out





def _ts_utc(v) -> float:
    """主库里的时间是 UTC，转成本地 epoch 秒（否则和记忆库差 8 小时）。"""
    if isinstance(v, (int, float)):
        return float(v)
    s = str(v or "").strip().rstrip("Z")
    if not s:
        return 0.0
    # 去掉时区尾巴（+00:00 / +0800），再试各种格式（含空格+微秒）
    s = s.replace("+00:00", "").replace("+0000", "")
    if s.endswith("Z"):
        s = s[:-1]
    for fmt in ("%Y-%m-%dT%H:%M:%S.%f", "%Y-%m-%dT%H:%M:%S",
                "%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S"):
        try:
            import calendar as _cal
            return float(_cal.timegm(time.strptime(s[:26], fmt)))
        except Exception:
            continue
    return _ts(s)

def _as_obj(v):
    """content 在 SQLite 里是 JSON 字符串，在备份里是对象；统一成 dict。"""
    if isinstance(v, str):
        try:
            v = json.loads(v)
        except Exception:
            return {}
    return v if isinstance(v, dict) else {}

def _load_maindata():
    """AstrBot 主库：可能是 JSON（备份体）也可能是 SQLite（运行时），两种都读。"""
    path = _find_maindb()
    if not path:
        try:
            top = sorted(os.listdir("/AstrBot/data"))[:30]
        except Exception:
            top = ["?"]
        logger.warning(f"[brain3d] 找不到主数据库，/AstrBot/data 下：{top}")
        return None
    try:
        if path.endswith(".db"):
            db = sqlite3.connect(path, timeout=10)
            db.row_factory = sqlite3.Row
            atts = [dict(r) for r in db.execute(
                "SELECT attachment_id, path, type, mime_type, created_at FROM attachments")]
            hist = []
            for r in db.execute(
                    "SELECT created_at, sender_name, content FROM platform_message_history"):
                hist.append(dict(r))
            db.close()
            logger.info(f"[brain3d] 主库(SQLite) attachments={len(atts)} history={len(hist)}")
            return {"attachments": atts, "platform_message_history": hist}
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        logger.info(f"[brain3d] 主库(JSON) attachments={len(data.get('attachments') or [])}")
        return data
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"[brain3d] 读主库失败({path})：{exc!r}")
        return None

def load_chat_images() -> list[dict]:
    """从 AstrBot 的主库读图片附件，带上它前后的对话作为"话题上下文"。"""
    data = _load_maindata()
    if not data:
        return []

    atts = [a for a in (data.get("attachments") or []) if a.get("type") == "image"]
    hist = data.get("platform_message_history") or []

    # 消息按时间排序，方便取上下文
    rows = []
    for m in hist:
        rows.append((_ts_utc(m.get("created_at")), m))
    rows.sort(key=lambda x: x[0])

    # 附件 id → 出现在哪条消息
    where = {}
    for idx, (ts, m) in enumerate(rows):
        try:
            content = _as_obj(m.get("content"))
            for comp in (content.get("message") or []):
                aid = comp.get("attachment_id")
                if aid:
                    where[aid] = idx
        except Exception:
            continue

    os.makedirs(IMG_DIR, exist_ok=True)
    out = []
    for a in atts:
        aid = a.get("attachment_id") or ""
        src = a.get("path") or ""
        if not aid or not os.path.exists(src):
            continue
        idx = where.get(aid)
        ctx = ""
        if idx is not None:
            lo, hi = max(0, idx - 6), min(len(rows), idx + 7)
            parts = []
            for _, mm in rows[lo:hi]:
                c = _as_obj(mm.get("content"))
                for comp in (c.get("message") or []):
                    if comp.get("type") == "plain" and comp.get("text"):
                        parts.append(str(comp["text"]))
            ctx = " ".join(parts)[:600]
        ext = os.path.splitext(src)[1] or ".png"
        dst_name = f"{aid}{ext}"
        dst = os.path.join(IMG_DIR, dst_name)
        if not os.path.exists(dst):
            try:
                with open(src, "rb") as fi, open(dst, "wb") as fo:
                    fo.write(fi.read())
            except Exception:
                continue
        out.append({
            "file": dst_name, "ts": _ts_utc(a.get("created_at")),
            "date": str(a.get("created_at") or "")[:19],
            "context": ctx, "grams": list(_bigrams(ctx)),
        })
    # 补充：磁盘上有、但附件表里没有的图片（旧图/被清过库的），按文件时间纳入
    seen = {o["file"] for o in out}
    disk_dirs = ["/AstrBot/data/attachments", "/AstrBot/data/files/attachments"]
    disk_added = 0
    for d in disk_dirs:
        if not os.path.isdir(d):
            continue
        try:
            names = os.listdir(d)
        except Exception:
            continue
        for name in names:
            low = name.lower()
            if not low.endswith((".png", ".jpg", ".jpeg", ".gif", ".webp")):
                continue
            src = os.path.join(d, name)
            dst_name = name
            if dst_name in seen:
                continue
            try:
                mtime = os.path.getmtime(src)
            except Exception:
                continue
            dst = os.path.join(IMG_DIR, dst_name)
            if not os.path.exists(dst):
                try:
                    with open(src, "rb") as fi, open(dst, "wb") as fo:
                        fo.write(fi.read())
                except Exception:
                    continue
            out.append({
                "file": dst_name, "ts": mtime,
                "date": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(mtime)),
                "context": "", "grams": [],
            })
            seen.add(dst_name)
            disk_added += 1
    logger.info(f"[brain3d] 图片同步 {len(out)} 张（附件表 {len(out) - disk_added}，磁盘补 {disk_added}）")
    return out


def attach_images(entries: list[dict], images: list[dict]) -> dict:
    """把图片挂到最相关的条目：先看关键词，其次看是不是同一天。"""
    unmatched = []
    best_max = 0.0
    same_day_hits = kw_hits = far_skip = 0
    for img in images:
        grams = set(img.get("grams") or [])
        best, best_score = None, 0.0
        for e in entries:
            if not e.get("ts"):
                continue
            gap_h = abs(e["ts"] - img["ts"]) / 3600.0
            if gap_h > 36:                     # 超过一天半不考虑
                far_skip += 1
                continue
            hit = len(grams & _bigrams(e["text"]))      # 关键词命中
            same_day = (e.get("date") or "")[:10] == (img.get("date") or "")[:10]
            score = hit * 2.0
            if same_day:
                score += 3.0
            elif gap_h <= 6:
                score += 1.0
            if score > best_score:
                best, best_score = e, score
        best_max = max(best_max, best_score)
        if best is not None and best_score >= 1.0:
            best["images"].append({
                "file": img["file"],
                "context": img["context"][:220],
                "ts": img["ts"],
                "by": "关键词" if len(grams & _bigrams(best["text"])) else "同一天",
            })
        else:
            unmatched.append({"file": img["file"], "date": img["date"],
                              "context": img["context"][:220]})
    _tss = [e["ts"] for e in entries if e.get("ts")]
    _its = [i["ts"] for i in images if i.get("ts")]
    logger.info(
        f"[brain3d] 诊断数值：条目 ts {min(_tss) if _tss else 0:.0f}~{max(_tss) if _tss else 0:.0f}，"
        f"图片 ts {min(_its) if _its else 0:.0f}~{max(_its) if _its else 0:.0f}，"
        f"图例 {time.strftime('%Y-%m-%d %H:%M', time.localtime(max(_its))) if _its else '无'}"
    )
    logger.info(
        f"[brain3d] 匹配诊断：图片 {len(images)} 张，最高分 {round(best_max,2)}，"
        f"同天命中 {same_day_hits}，关键词命中 {kw_hits}，时间过远跳过 {far_skip}"
    )
    return {"unmatched": unmatched}




def load_edits() -> dict:
    try:
        with open(EDITS_FILE, encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {"override": {}, "pins": []}
    except Exception:
        return {"override": {}, "pins": []}


def save_edits(d: dict) -> None:
    os.makedirs(DATA_DIR, exist_ok=True)
    tmp = EDITS_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(d, f, ensure_ascii=False, indent=1)
    os.replace(tmp, EDITS_FILE)


def apply_overrides(tl: dict, edits: dict) -> dict:
    """把用户改过的文字/日期/重要性套上去；被删除的条目直接隐藏。"""
    ov = edits.get("override") or {}
    out = []
    for e in tl["entries"]:
        o = ov.get(e["id"])
        if o:
            if o.get("hidden"):
                continue
            if o.get("text"):
                e["text"] = o["text"]
            if o.get("date"):
                e["date"] = o["date"]
                ts = _ts(o["date"])
                if ts:
                    e["ts"] = ts
            if o.get("importance") is not None:
                e["importance"] = float(o["importance"])
            e["edited"] = True
        out.append(e)
    out.sort(key=lambda x: x.get("ts") or 0)
    tl["entries"] = out
    return tl

def apply_pins(tl: dict, pins: list[dict]) -> dict:
    """按配置里的 pins 新增时间线条目或强制挂图（不写数据库，属于覆盖层）。"""
    if not pins:
        return tl
    entries = tl["entries"]
    imgs = {i["file"]: i for i in tl.get("_all_images", [])}
    by_id = {e["id"]: e for e in entries}
    added = 0
    for k, pin in enumerate(pins):
        file = str(pin.get("file") or "")
        date = str(pin.get("date") or "")
        text = str(pin.get("text") or "")
        ctx = str(pin.get("context") or "")
        target = str(pin.get("entry") or "")
        image = None
        for fname, im in imgs.items():
            if file and (file in fname or fname == file):
                image = {"file": im["file"], "context": ctx or im.get("context", ""),
                         "ts": im.get("ts", 0), "by": "指定"}
                break
        if pin.get("create") and text:
            eid = f"pin{k}"
            ts = _ts(date) if date else time.time()
            e = {"id": eid, "src": "pin", "date": date or time.strftime("%Y-%m-%d %H:%M:%S"),
                 "ts": ts, "text": text, "importance": float(pin.get("importance") or 0.8),
                 "kind": "episode", "people": "", "images": ([image] if image else [])}
            entries.append(e); by_id[eid] = e; added += 1
        elif target and target in by_id and image:
            by_id[target]["images"].append(image)
    entries.sort(key=lambda x: x["ts"])
    tl["entries"] = entries
    tl["pins_added"] = added
    return tl


def _contain(a: set, b: set) -> float:
    """两个 bigram 集合的包含度（小的被大的覆盖多少）。"""
    if not a or not b:
        return 0.0
    small, big = (a, b) if len(a) <= len(b) else (b, a)
    return len(small & big) / len(small)


def dedupe_entries(entries: list[dict]) -> list[dict]:
    """新库里"复述旧事"的条目会带今天的日期，和旧库的历史条目重复。
    这里以旧库（有真实日期）为准，把重复的新库条目丢掉。"""
    eps = [e for e in entries if e.get("src") == "episodes"]
    brain = [e for e in entries if e.get("src") != "episodes"]
    ep_grams = [(e, _bigrams(e.get("text") or "")) for e in eps]
    kept, dropped = [], 0
    for e in brain:
        g = _bigrams(e.get("text") or "")
        if not g:
            kept.append(e); continue
        dup = False
        for ep, eg in ep_grams:
            if _contain(g, eg) >= 0.55:
                dup = True
                break
        # 明显是"元对话"的也丢掉（问她记不记得之类）
        t = str(e.get("text") or "")
        if any(k in t for k in ("astrbot问", "问我是否记得", "记忆 0 条", "重要性 ")):
            dup = True
        if dup:
            dropped += 1
        else:
            kept.append(e)
    logger.info(f"[brain3d] 去重：丢弃新库重复条目 {dropped} 条，保留 {len(kept)} 条")
    out = eps + kept
    out.sort(key=lambda x: x.get("ts") or 0)
    return out


def dedupe_all(entries: list[dict], days: float = 3.0, th: float = 0.6) -> list[dict]:
    """同一天内语义重复的条目合并成一条（保留文字更长的那个）。"""
    entries = sorted(entries, key=lambda x: x.get("ts") or 0)
    kept: list[dict] = []
    for e in entries:
        g = _bigrams(e.get("text") or "")
        if not g:
            kept.append(e); continue
        merged = False
        for k in kept:
            if abs((k.get("ts") or 0) - (e.get("ts") or 0)) > days * 86400:
                continue
            kg = _bigrams(k.get("text") or "")
            if _contain(g, kg) >= th:
                if len(e.get("text") or "") > len(k.get("text") or ""):
                    k["text"] = e["text"]
                if len(e.get("images") or []) > len(k.get("images") or []):
                    k["images"] = e["images"]
                if (e.get("importance") or 0) > (k.get("importance") or 0):
                    k["importance"] = e["importance"]
                merged = True
                break
        if not merged:
            kept.append(e)
    return kept

def build_timeline(pins_raw: str = "") -> dict:
    entries = dedupe_all(dedupe_entries(load_timeline_entries()))
    images = load_chat_images()
    stats = attach_images(entries, images)
    years: dict[str, int] = {}
    for e in entries:
        y = (e.get("date") or "")[:4] or "未知"
        years[y] = years.get(y, 0) + 1
    _pins = []
    try:
        txt = str(pins_raw or "").strip()
        _pins = json.loads(txt) if txt.startswith("[") else []
    except Exception:
        _pins = []
    _edits = load_edits()
    _pins = _pins + list(_edits.get("pins") or [])
    tl = {
        "entries": entries,
        "years": years,
        "images_total": len(images),
        "images_unmatched": len(stats["unmatched"]),
        "unmatched_images": stats["unmatched"],
        "_all_images": [{"file": i["file"], "ts": i["ts"], "context": i["context"]} for i in images],
    }
    tl = apply_pins(tl, _pins)
    tl = apply_overrides(tl, _edits)
    return tl


CORE_DB = "/AstrBot/data/plugin_data/dafeiyu_mindcore/memory_core.db"


def load_core_stats(limit: int = 8) -> dict:
    """读心智库：记忆池分布 + 权重最高的几条。只读。"""
    if not os.path.exists(CORE_DB):
        return {"available": False}
    out = {"available": True, "total": 0, "pools": {}, "top": [], "avg_weight": 0.0}
    try:
        con = sqlite3.connect(f"file:{CORE_DB}?mode=ro", uri=True, timeout=10)
        con.row_factory = sqlite3.Row
        try:
            out["total"] = int(con.execute("SELECT COUNT(*) FROM memories").fetchone()[0] or 0)
            for r in con.execute("SELECT pool, COUNT(*) n, AVG(weight) w FROM memories GROUP BY pool"):
                out["pools"][r["pool"] or "short"] = {"n": int(r["n"]), "w": round(float(r["w"] or 0), 3)}
            rows = con.execute(
                "SELECT text, pool, weight, access_count, t_anchor, created_at FROM memories "
                "ORDER BY weight DESC LIMIT ?", (limit,)).fetchall()
            now = time.time()
            for r in rows:
                ts = float(r["t_anchor"] or r["created_at"] or 0)
                out["top"].append({
                    "text": (r["text"] or "")[:80],
                    "pool": r["pool"] or "short",
                    "weight": round(float(r["weight"] or 0), 3),
                    "access": int(r["access_count"] or 0),
                    "age_days": int(max(0, (now - ts) / 86400)) if ts else None,
                })
            out["avg_weight"] = round(float(con.execute(
                "SELECT AVG(weight) FROM memories").fetchone()[0] or 0), 3)
        finally:
            con.close()
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"[brain3d] 读心智库失败：{exc!r}")
        out["available"] = False
    return out


def build_graph(db_path: str) -> dict:
    """把 brain.db 转成 3D 大脑的图数据。"""
    if not os.path.exists(db_path):
        raise FileNotFoundError(db_path)
    conn = sqlite3.connect(db_path, timeout=15)
    conn.row_factory = sqlite3.Row
    try:
        ents = {r["id"]: dict(r) for r in conn.execute(
            "SELECT id, name, type, mention_count FROM entities")}

        edges: list[dict] = []
        for r in conn.execute("SELECT src_id, dst_id, relation, weight FROM relations"):
            edges.append({"src": int(r["src_id"]), "dst": int(r["dst_id"]),
                          "kind": "relation", "label": r["relation"] or "",
                          "weight": float(r["weight"] or 1.0)})

        mem_ents: dict[int, list[int]] = defaultdict(list)
        for r in conn.execute("SELECT memory_id, entity_id FROM memory_entities"):
            mem_ents[r["memory_id"]].append(int(r["entity_id"]))

        pair: dict[tuple[int, int], int] = defaultdict(int)
        for ids in mem_ents.values():
            for i in range(len(ids)):
                for j in range(i + 1, len(ids)):
                    pair[tuple(sorted((ids[i], ids[j])))] += 1
        exists = {(e["src"], e["dst"]) for e in edges}
        for (a, b), n in sorted(pair.items(), key=lambda x: -x[1])[:1200]:
            if (a, b) in exists or (b, a) in exists:
                continue
            edges.append({"src": a, "dst": b, "kind": "cooccur", "label": "",
                          "weight": min(1.0, 0.3 + n * 0.1)})

        deg: dict[int, int] = defaultdict(int)
        for e in edges:
            deg[e["src"]] += 1
            deg[e["dst"]] += 1

        nodes = [{
            "id": int(eid), "name": e["name"],
            "type": TYPE_MAP.get(str(e["type"]).lower(), "thing"),
            "degree": max(int(e["mention_count"] or 1), deg.get(eid, 0)),
            "iso": deg.get(eid, 0) == 0,
        } for eid, e in ents.items()]

        memories = [{
            "id": int(r["id"]), "text": r["text"], "kind": r["kind"] or "episode",
            "importance": float(r["importance"] or 0.5),
            "created_at": float(r["created_at"] or 0),
            "entities": mem_ents.get(r["id"], []),
        } for r in conn.execute(
            "SELECT id, kind, text, importance, created_at FROM memories "
            "WHERE status='active' ORDER BY id DESC LIMIT 600")]

        _archived = int(conn.execute(
            "SELECT COUNT(*) FROM memories WHERE status!='active'").fetchone()[0] or 0)
        _relations = int(conn.execute("SELECT COUNT(*) FROM relations").fetchone()[0] or 0)

        _reflections = [{"text": r["text"], "created_at": float(r["created_at"] or 0)}
                        for r in conn.execute(
            "SELECT text, created_at FROM memories WHERE kind='reflection' AND status='active'"
            " ORDER BY id DESC LIMIT 8")]
        _lessons = [{"text": r["text"]} for r in conn.execute(
            "SELECT text FROM memories WHERE kind='lesson' AND status='active'"
            " ORDER BY id DESC LIMIT 6")]
        _kinds = {r[0]: r[1] for r in conn.execute(
            "SELECT kind, COUNT(*) FROM memories WHERE status='active' GROUP BY kind")}
        _recent = int(conn.execute(
            "SELECT COUNT(*) FROM memories WHERE status='active'"
            f" AND created_at > {_now() - 7*86400}").fetchone()[0] or 0)
    finally:
        conn.close()
    archived = locals().get("_archived", 0)
    _reflections = locals().get("_reflections", [])
    _lessons = locals().get("_lessons", [])
    _kinds = locals().get("_kinds", {})
    _recent = locals().get("_recent", 0)
    relations = locals().get("_relations", len(edges))
    orphan = sum(1 for n in nodes if n["iso"])
    return {
        "graph": {"nodes": nodes, "edges": edges, "memories": memories},
        "meta": {
            "active": len(memories),
            "archived": int(archived),
            "entities": len(nodes),
            "relations": int(relations),
            "orphan": orphan,
            "anchor": load_anchors(),
            "reflections": _reflections,
            "lessons": _lessons,
            "kinds": _kinds,
            "recent7": _recent,
            "mood": load_mood(),
            "core": load_core_stats(),
        },
    }


@register(PLUGIN_NAME, "Caoran1314",
          "把她脑子的 3D 视图做成插件页面（读 memory_brain 的 brain.db，定时刷新）",
          "0.1.0")
class Brain3D(Star):
    def __init__(self, context: Context, config: dict | None = None):
        super().__init__(context)
        self.cfg = config or {}
        self._task: asyncio.Task | None = None
        self._refresh_now()
        try:
            self._task = asyncio.create_task(self._loop())
        except RuntimeError:
            self._task = None
        # 页面桥的路由名在不同版本里前缀不一样，多注册几个别名，页面逐个试
        aliases = ["/save_edit", "/page/save_edit", "/dafeiyu_brain3d/save_edit",
                   "/api/save_edit", "/edit/save"]
        raliases = ["/reset_edit", "/page/reset_edit", "/dafeiyu_brain3d/reset_edit",
                    "/api/reset_edit", "/edit/reset"]
        ok = 0
        for route in aliases:
            try:
                self.context.register_web_api(route, self.api_save_edit, ["POST"], "保存时间线修改")
                ok += 1
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"[brain3d] 注册 {route} 失败：{exc!r}")
        for route in raliases:
            try:
                self.context.register_web_api(route, self.api_reset_edit, ["POST"], "撤销时间线修改")
                ok += 1
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"[brain3d] 注册 {route} 失败：{exc!r}")
        logger.info(f"[brain3d] 写接口别名注册 {ok} 个：{aliases + raliases}")
        logger.info(f"[brain3d] 已启动，页面目录={PAGE_DIR}")

    # ---------------------------------------------------------------- 导出

    def _refresh_now(self) -> dict:
        try:
            data = build_graph(BRAIN_DB)
            os.makedirs(PAGE_DIR, exist_ok=True)
            tmp = DATA_FILE + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, separators=(",", ":"))
            os.replace(tmp, DATA_FILE)
            try:
                tl = build_timeline(str(self.cfg.get("pins", "") or ""))
                tmp2 = TIMELINE_FILE + ".tmp"
                with open(tmp2, "w", encoding="utf-8") as f:
                    json.dump(tl, f, ensure_ascii=False, separators=(",", ":"))
                os.replace(tmp2, TIMELINE_FILE)
                logger.info(
                    f"[brain3d] 时间线 {len(tl['entries'])} 条，图片 {tl['images_total']} 张"
                    f"（已挂 {tl['images_total'] - tl['images_unmatched']}，未归档 {tl['images_unmatched']}）"
                )
            except Exception as exc:  # noqa: BLE001
                logger.exception("[brain3d] 时间线生成失败")
            g = data["graph"]
            logger.info(
                f"[brain3d] 已刷新：节点 {len(g['nodes'])}｜边 {len(g['edges'])}"
                f"｜记忆 {len(g['memories'])}"
            )
            return g
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[brain3d] 刷新失败：{exc!r}")
            return {"nodes": [], "edges": [], "memories": []}

    async def _loop(self) -> None:
        minutes = max(1, int(self.cfg.get("refresh_minutes", 5) or 5))
        while True:
            try:
                await asyncio.sleep(minutes * 60)
                await asyncio.to_thread(self._refresh_now)
            except asyncio.CancelledError:
                break
            except Exception:  # noqa: BLE001
                logger.exception("[brain3d] 定时刷新异常")


    def _api_ok(self, data=None):
        return {"status": "ok", "message": None, "data": data or {}}

    async def api_save_edit(self, **kwargs):
        """页面桥：保存对某条时间线的修改（覆盖层，不动数据库）。"""
        try:
            body = {}
            if "body" in kwargs:
                body = kwargs.get("body") or {}
            elif kwargs.get("request") is not None:
                body = await kwargs["request"].json()
            eid = str(body.get("id") or "").strip()
            if not eid:
                return {"status": "error", "message": "缺少 id", "data": {}}
            edits = load_edits()
            ov = edits.setdefault("override", {})
            cur = dict(ov.get(eid) or {})
            if body.get("delete"):
                cur["hidden"] = True
            else:
                if body.get("text") is not None:
                    cur["text"] = str(body["text"]).strip()
                if body.get("date"):
                    cur["date"] = str(body["date"]).strip()
                if body.get("importance") is not None:
                    try:
                        cur["importance"] = max(0.0, min(1.0, float(body["importance"])))
                    except (TypeError, ValueError):
                        pass
                cur.pop("hidden", None)
            ov[eid] = cur
            save_edits(edits)
            await asyncio.to_thread(self._refresh_now)
            logger.info(f"[brain3d] 已保存修改 {eid}: {cur}")
            return self._api_ok({"id": eid})
        except Exception as exc:  # noqa: BLE001
            logger.exception("[brain3d] 保存修改失败")
            return {"status": "error", "message": repr(exc), "data": {}}

    async def api_reset_edit(self, **kwargs):
        """撤销某条的时间线修改。"""
        try:
            body = kwargs.get("body") or {}
            if not body and kwargs.get("request") is not None:
                body = await kwargs["request"].json()
            eid = str(body.get("id") or "").strip()
            edits = load_edits()
            edits.get("override", {}).pop(eid, None)
            save_edits(edits)
            await asyncio.to_thread(self._refresh_now)
            logger.info(f"[brain3d] 已撤销修改 {eid}")
            return self._api_ok({"id": eid})
        except Exception as exc:  # noqa: BLE001
            logger.exception("[brain3d] 撤销失败")
            return {"status": "error", "message": repr(exc), "data": {}}


    @filter.command("人生保存", alias={"记忆保存"})
    async def cmd_save_edit(self, event: AstrMessageEvent, payload: str = ""):
        """命令行通道：/人生保存 {"id":"pin0","text":"...","importance":0.9}"""
        raw = (payload or "").strip()
        if not raw:
            yield event.plain_result(
                '用法：/人生保存 {"id":"条目id","text":"新文字","date":"2026-11-01 18:40:00","importance":0.9}'
                '；删除：/人生保存 {"id":"xxx","delete":true}'
                '；撤销：/人生撤销 {"id":"xxx"}'
            )
            return
        try:
            body = json.loads(raw)
        except Exception as exc:  # noqa: BLE001
            yield event.plain_result(f"JSON 解析失败：{exc!r}")
            return
        res = await self.api_save_edit(body=body)
        if res.get("status") == "ok":
            yield event.plain_result(f"已保存：{body.get('id')}")
        else:
            yield event.plain_result(f"保存失败：{res.get('message')}")

    @filter.command("人生撤销", alias={"记忆撤销"})
    async def cmd_reset_edit(self, event: AstrMessageEvent, payload: str = ""):
        """撤销某条的修改：/人生撤销 {"id":"pin0"}"""
        try:
            body = json.loads((payload or "").strip())
        except Exception as exc:  # noqa: BLE001
            yield event.plain_result(f"用法：/人生撤销 {{\"id\":\"xxx\"}}（{exc!r}）")
            return
        res = await self.api_reset_edit(body=body)
        if res.get("status") == "ok":
            yield event.plain_result(f"已撤销：{body.get('id')}")
        else:
            yield event.plain_result(f"撤销失败：{res.get('message')}")

    @filter.command("人生看")
    async def cmd_show_edit(self, event: AstrMessageEvent):
        """看当前有哪些覆盖层修改。"""
        edits = load_edits()
        ov = edits.get("override") or {}
        pins = edits.get("pins") or []
        if not ov and not pins:
            yield event.plain_result("目前没有任何手动修改。")
            return
        lines = [f"覆盖 {len(ov)} 条，补充 {len(pins)} 条："]
        for k, v in list(ov.items())[:15]:
            flag = "已删" if v.get("hidden") else "已改"
            lines.append(f"· {k} [{flag}] {(v.get('text') or '')[:28]}")
        yield event.plain_result("\n".join(lines))

    # ---------------------------------------------------------------- 指令

    @filter.command("脑3d")
    async def cmd_brain3d(self, event: AstrMessageEvent):
        """刷新 3D 记忆脑数据。用法：/脑3d"""
        g = await asyncio.to_thread(self._refresh_now)
        yield event.plain_result(
            f"3D 数据已刷新：节点 {len(g['nodes'])}｜边 {len(g['edges'])}"
            f"｜记忆 {len(g['memories'])}"
        )

    async def terminate(self):
        if self._task and not self._task.done():
            self._task.cancel()
        logger.info(f"[brain3d] 已卸载")
