"""B站 接口层：搜索 / 字幕 / 评论 / 发评论 / 私信 / 扫码登录。

这些调用方式是从插件内置的 ``vendor/bilibili-mcp/mcp_server.py``（MIT，
上游 AGI_Ananas/bilibili-mcp）里提炼的 —— 那几个坑（cid 回退、``//`` 开头的字幕地址、
评论抓取失败要能分辨"真没评论"和"被风控"、回复回执里的 rpid）都在实测里踩过，
这里保持同样的写法，只是不再经过 MCP。
"""

from __future__ import annotations

import asyncio
import json
import os
import re
from pathlib import Path
from typing import Any

import httpx
from bilibili_api import Credential, comment, search, session, video
from bilibili_api.login_v2 import QrCodeLogin, QrCodeLoginEvents

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
)

#: 只有一行署名、没有实际语音内容的字幕（纯音乐 / 无声录屏常见）
_ATTRIBUTION_RE = re.compile(r"^[\s【\[（(]*(字幕|字幕by|字幕组|字幕提供|AI字幕)[^\n]{0,24}[\s】\]）)]*$", re.IGNORECASE)


class BiliError(RuntimeError):
    """接口层错误（未登录、风控、网络）。"""


class NotLoggedIn(BiliError):
    pass


# ══════════════════════════════════════════════════════════════════════════
# 凭证
# ══════════════════════════════════════════════════════════════════════════

def credential_file(data_dir: Path) -> Path:
    return data_dir / "bili_credential.json"


def load_credential(data_dir: Path) -> Credential | None:
    """优先环境变量，其次 ``data/bili_credential.json``（扫码登录会写这里）。"""
    env = {
        "sessdata": (os.environ.get("BILI_SESSDATA") or "").strip(),
        "bili_jct": (os.environ.get("BILI_JCT") or "").strip(),
        "buvid3": (os.environ.get("BILI_BUVID3") or "").strip(),
        "dedeuserid": (os.environ.get("BILI_DEDEUSERID") or "").strip(),
    }
    if env["sessdata"]:
        return Credential(
            sessdata=env["sessdata"], bili_jct=env["bili_jct"],
            buvid3=env["buvid3"], dedeuserid=env["dedeuserid"],
        )
    path = credential_file(data_dir)
    if not path.is_file():
        return None
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(raw, dict) or not raw.get("sessdata"):
        return None
    return Credential(
        sessdata=str(raw.get("sessdata") or ""),
        bili_jct=str(raw.get("bili_jct") or ""),
        buvid3=str(raw.get("buvid3") or ""),
        dedeuserid=str(raw.get("dedeuserid") or ""),
    )


def save_credential(data_dir: Path, cred: Credential) -> Path:
    path = credential_file(data_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "sessdata": cred.sessdata,
        "bili_jct": cred.bili_jct,
        "buvid3": cred.buvid3,
        "dedeuserid": cred.dedeuserid,
    }
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)
    return path


# ══════════════════════════════════════════════════════════════════════════
# 接口
# ══════════════════════════════════════════════════════════════════════════

_ORDER = {
    "totalrank": search.OrderVideo.TOTALRANK,
    "click": search.OrderVideo.CLICK,
    "pubdate": search.OrderVideo.PUBDATE,
    "dm": search.OrderVideo.DM,
}


class Bili:
    """一个薄封装。只暴露流水线要用的五件事。"""

    def __init__(self, cred: Credential | None, delays: dict[str, float] | None = None):
        self.cred = cred
        self.delays = {"search": 0.5, "subtitle": 0.8, "comments": 0.5}
        self.delays.update(delays or {})

    # ── 内部 ────────────────────────────────────────────────────────────
    def _need_cred(self) -> Credential:
        if not self.cred:
            raise NotLoggedIn("没有 B站 凭证：先跑 `biliduty login` 扫码，或设好 BILI_SESSDATA 等环境变量")
        return self.cred

    async def _sleep(self, kind: str) -> None:
        delay = float(self.delays.get(kind, 0) or 0)
        if delay > 0:
            await asyncio.sleep(delay)

    @staticmethod
    def _unwrap(result: Any) -> dict:
        """bilibili_api 有时返回 {"code":0,"data":{...}} 的信封，这里拆平。"""
        if isinstance(result, dict) and "data" in result and isinstance(result["data"], dict) and "code" in result:
            return result["data"]
        return result if isinstance(result, dict) else {}

    # ── 搜索 ────────────────────────────────────────────────────────────
    async def search(self, keyword: str, num: int = 12, order: str = "pubdate") -> list[dict]:
        order_enum = _ORDER.get(str(order), search.OrderVideo.PUBDATE)
        last_error: Exception | None = None
        for attempt in range(3):
            try:
                result = self._unwrap(await search.search_by_type(
                    keyword=keyword,
                    search_type=search.SearchObjectType.VIDEO,
                    page=1,
                    order_type=order_enum,
                ))
                break
            except Exception as exc:  # 风控 / 网络抖动：重试两次，别把整轮拖垮
                last_error = exc
                await asyncio.sleep(1.5 * (attempt + 1))
        else:
            raise BiliError(f"搜索「{keyword}」失败：{last_error}")

        videos: list[dict] = []
        for item in (result.get("result") or [])[: max(1, int(num))]:
            title = re.sub(r"<[^>]+>", "", str(item.get("title") or "")).strip()
            videos.append({
                "bvid": item.get("bvid") or "",
                "title": title,
                "author": item.get("author") or "",
                "play": int(item.get("play") or 0),
                "review": int(item.get("review") or 0),
                "duration": str(item.get("duration") or ""),
                "pubdate": int(item.get("pubdate") or item.get("senddate") or 0),
                "description": str(item.get("description") or "")[:200],
            })
        await self._sleep("search")
        return [v for v in videos if v["bvid"]]

    # ── 视频信息 ────────────────────────────────────────────────────────
    async def video_info(self, bvid: str) -> dict:
        cred = self._need_cred()
        info = self._unwrap(await video.Video(bvid=bvid, credential=cred).get_info())
        stat = info.get("stat") or {}
        return {
            "bvid": info.get("bvid") or bvid,
            "aid": info.get("aid"),
            "title": info.get("title") or "",
            "description": str(info.get("desc") or "")[:500],
            "author": (info.get("owner") or {}).get("name") or "",
            "duration": int(info.get("duration") or 0),
            "pubdate": int(info.get("pubdate") or info.get("ctime") or 0),
            "view": int(stat.get("view") or 0),
            "reply": int(stat.get("reply") or 0),
            "like": int(stat.get("like") or 0),
            "tags": [t.get("tag_name") for t in (info.get("tag") or []) if t.get("tag_name")],
        }

    # ── 字幕 ────────────────────────────────────────────────────────────
    async def subtitle(self, bvid: str) -> dict:
        """返回 ``{"text": ...}``，或 ``{"empty": "no_subtitle"|"no_voice"|"error:..."}``。"""
        cred = self._need_cred()
        try:
            v = video.Video(bvid=bvid, credential=cred)
            info = self._unwrap(await v.get_info())
            cid = info.get("cid") or 0
            if not cid and info.get("pages"):
                cid = (info["pages"][0] or {}).get("cid") or 0
            if not cid:
                return {"empty": "no_cid"}
            listing = await v.get_subtitle(cid=int(cid))
        except Exception as exc:
            return {"empty": f"error:{type(exc).__name__}: {exc}"}

        subtitles = (listing or {}).get("subtitles") or []
        if not subtitles:
            return {"empty": "no_subtitle"}

        target = next(
            (s for s in subtitles if s.get("lan") in ("ai-zh", "zh-CN", "zh", "zh-Hans")),
            subtitles[0],
        )
        url = str(target.get("subtitle_url") or "")
        if url.startswith("//"):
            url = "https:" + url
        if not url:
            return {"empty": "no_subtitle"}
        try:
            async with httpx.AsyncClient(timeout=30, follow_redirects=True) as client:
                response = await client.get(url, headers={"User-Agent": UA, "Referer": "https://www.bilibili.com/"})
                response.raise_for_status()
                payload = response.json()
        except Exception as exc:
            return {"empty": f"error:字幕下载失败:{type(exc).__name__}"}

        lines = [str(item.get("content") or "").strip() for item in (payload.get("body") or [])]
        text = "\n".join(line for line in lines if line)

        if not text.strip():
            return {"empty": "no_voice"}
        if len(text.strip()) < 12 or _ATTRIBUTION_RE.fullmatch(text.strip()):
            return {"empty": "no_voice"}
        await self._sleep("subtitle")
        return {
            "text": text,
            "language": str(target.get("lan_doc") or ""),
            "segments": len(lines),
            "title": str(info.get("title") or ""),
        }

    # ── 评论 ────────────────────────────────────────────────────────────
    async def comments(self, bvid: str, num: int = 20) -> dict:
        cred = self._need_cred()
        v = video.Video(bvid=bvid, credential=cred)
        info = self._unwrap(await v.get_info())
        aid = info.get("aid")
        if not aid:
            return {"bvid": bvid, "count": 0, "comments": [], "error": "拿不到 aid"}

        collected: list[dict] = []
        error = ""
        page = 1
        while len(collected) < num and page <= 3:
            try:
                resp = await comment.get_comments(
                    oid=int(aid),
                    type_=comment.CommentResourceType.VIDEO,
                    page_index=page,
                    order=comment.OrderType.LIKE,
                    credential=cred,
                )
            except Exception as exc:  # 风控 -412 不能伪装成"这条视频 0 条评论"
                error = f"{type(exc).__name__}: {exc}"
                break
            replies = (resp or {}).get("replies") or []
            if not replies:
                break
            for item in replies:
                member = item.get("member") or {}
                content = item.get("content") or {}
                collected.append({
                    "rpid": item.get("rpid", 0),
                    "user": member.get("uname", ""),
                    "content": content.get("message", ""),
                    "like": item.get("like", 0),
                    "rcount": item.get("rcount", 0),
                    "ctime": item.get("ctime", 0),
                })
            page += 1
            await asyncio.sleep(0.5)
        await self._sleep("comments")
        out = {"bvid": bvid, "count": len(collected[:num]), "comments": collected[:num]}
        if error:
            out["error"] = error
        return out

    # ── 发评论 ──────────────────────────────────────────────────────────
    async def reply(self, bvid: str, text: str, rpid: int = 0, root: int = 0) -> dict:
        cred = self._need_cred()
        info = self._unwrap(await video.Video(bvid=bvid, credential=cred).get_info())
        aid = info.get("aid")
        if not aid:
            return {"success": False, "error": "拿不到 aid"}
        try:
            if rpid:
                result = await comment.send_comment(
                    text=text, oid=int(aid), type_=comment.CommentResourceType.VIDEO,
                    root=int(root or rpid), parent=int(rpid), credential=cred,
                )
            else:
                result = await comment.send_comment(
                    text=text, oid=int(aid), type_=comment.CommentResourceType.VIDEO,
                    credential=cred,
                )
        except Exception as exc:
            return {"success": False, "error": f"{type(exc).__name__}: {exc}"}

        out: dict[str, Any] = {"success": True}
        if isinstance(result, dict):
            for key in ("rpid", "rpid_str", "root", "parent"):
                value = result.get(key)
                if value not in (None, "", 0, "0"):
                    out[key] = value
        return out

    # ── 私信 ────────────────────────────────────────────────────────────
    async def send_dm(self, uid: int, text: str) -> dict:
        cred = self._need_cred()
        if not uid:
            return {"success": False, "error": "没配 dmUid"}
        try:
            result = await session.send_msg(
                credential=cred,
                receiver_id=int(uid),
                msg_type=session.EventType.TEXT,
                content=str(text).strip(),
            )
            return {"success": True, "data": result if isinstance(result, dict) else str(result)}
        except Exception as exc:
            return {"success": False, "error": f"{type(exc).__name__}: {exc}"}

    # ── 我是谁（校验凭证是否过期）────────────────────────────────────────
    async def identity(self) -> dict:
        cred = self._need_cred()
        from bilibili_api import user

        info = await user.User(uid=int(cred.dedeuserid), credential=cred).get_user_info()
        return {"uid": cred.dedeuserid, "name": info.get("name", "")}


# ══════════════════════════════════════════════════════════════════════════
# 扫码登录（服务器上没有浏览器，所以打一份 PNG 出来 + 终端文本码兜底）
# ══════════════════════════════════════════════════════════════════════════

async def qr_login(data_dir: Path, timeout_seconds: int = 180) -> Credential:
    login = QrCodeLogin()
    await login.generate_qrcode()

    png = credential_file(data_dir).with_name("qrcode_login.png")
    try:
        png.parent.mkdir(parents=True, exist_ok=True)
        picture = login.get_qrcode_picture()
        png.write_bytes(picture.content)
        print(f"[二维码] 已写到 {png}（用手机 B站 扫这张图；180 秒内有效）")
    except Exception as exc:
        print(f"[二维码] 写 PNG 失败：{exc}")

    try:
        print(login.get_qrcode_terminal())
    except Exception:
        pass

    waited = 0
    while waited < timeout_seconds:
        state = await login.check_state()
        if state == QrCodeLoginEvents.DONE:
            cred = login.get_credential()
            path = save_credential(data_dir, cred)
            print(f"[登录] 成功，凭证已写到 {path}")
            return cred
        if state == QrCodeLoginEvents.TIMEOUT:
            raise BiliError("二维码过期了，重新跑一次 `biliduty login`")
        await asyncio.sleep(3)
        waited += 3
    raise BiliError("等扫码超时了（180 秒）")
