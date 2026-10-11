#!/usr/bin/env -S uv run --quiet --script
# /// script
# requires-python = ">=3.10"
# dependencies = ["requests", "img2pdf", "pillow", "keyring", "numpy"]
# ///
# SPDX-License-Identifier: MIT
# Copyright (c) 2026 8eoyw
# Portions ported from PeiPei233/zju-learning-assistant, Copyright (c) 2023 PeiPei233 (MIT).
"""学在浙大 / 智云课堂 命令行工具。

API 逻辑移植自 PeiPei233/zju-learning-assistant (ZLA) 的 src-tauri/src/zju_assist.rs，
改成可写进脚本、可定时运行、可被 AI agent 直接调用的单文件 CLI。

  zju.py login                         # 首次：存学号，密码进 macOS Keychain
  zju.py courses [--all]               # 学在浙大课程列表
  zju.py sync [课程...] [--dry-run]     # 增量同步课程附件（含定时发布的活动）
  zju.py todo                          # 待办
  zju.py notify [课程...] [--json]      # 测验／作业新增、开放、时间变更和临近截止提醒
  zju.py activities [课程...] [--type forum homework ...]  # 所有活动（含测验）
  zju.py show <活动id> [--read]         # 活动详情；作业显示自己的提交状态；--read 打印附件文字
  zju.py forum list|read|post|reply ... # 讨论区
  zju.py upload 文件...                 # 上传，打印 upload id
  zju.py submit <作业id> --file ... [--body ...] [--draft] [-y]  # 交作业
  zju.py classroom search 关键词        # 智云课堂找课（id 与学在浙大不同）
  zju.py classroom subs <cid>          # 列出每堂课
  zju.py classroom day [日期] [--days N]   # 课表的课＋追踪中的旁听课
  zju.py classroom add|rm <cid>...      # 追踪课表外的课（听课申请已获批准的），--days 也会抓
  zju.py classroom tracked             # 列出追踪中的课
  zju.py rollcall [--in-class] [--json]  # 签到提醒用：只列出进行中的签到，不代签
  zju.py ppt --course <cid> | --days N [--dedup]  # 智云 PPT 截图合并 PDF
  zju.py transcript --course <cid> | --days N [--format txt|srt|md]
"""
from __future__ import annotations

import argparse
import datetime as dt
import html
import json
import os
import random
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import unquote, urljoin, urlparse

import ssl

import requests
from requests.adapters import HTTPAdapter

KEYCHAIN_SERVICE = "zju-learning"
STATE_DIR = Path(os.environ.get("ZJU_STATE_DIR") or (Path.home() / ".config" / "zju-learning"))
CONFIG_FILE = STATE_DIR / "config.json"
COOKIE_FILE = STATE_DIR / "cookies.json"
SCHEDULE_FILE = STATE_DIR / "schedule.json"  # rollcall --in-class 的当日课表缓存
# 这两台只支持 1024-bit DHE / 静态 RSA，OpenSSL 3 默认拒绝；只对它们降级
LEGACY_TLS_HOSTS = ("courses.zju.edu.cn", "identity.zju.edu.cn")
CST = dt.timezone(dt.timedelta(hours=8))  # 学校 API 没带时区时视为北京时间
LMS = "https://courses.zju.edu.cn"
DEFAULT_OUT = Path.home() / "ZJU-Courses"  # 可用 config.json 的 "out" 或环境变量 ZJU_OUT 覆盖
UA = "Mozilla/5.0 (X11; Linux x86_64; rv:88.0) Gecko/20100101 Firefox/88.0"
MEDIA_EXT = {".mp4", ".mov", ".avi", ".mkv", ".flv", ".m4v", ".wmv", ".webm", ".mp3", ".m4a", ".wav"}
TIMEOUT = (6, 60)  # connect, read — 定时任务别卡在单个 hop 上

COURSE_FIELDS = (
    "id,name,course_code,department(id,name),start_date,end_date,is_started,is_closed,"
    "academic_year_id,semester_id,credit,display_name,instructors(id,name)"
)


class ZjuError(RuntimeError):
    pass


def log(*a):
    print(*a, file=sys.stderr, flush=True)


WIN_RESERVED = {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))}


def safe_name(s: str) -> str:
    s = re.sub(r'[/\\:*?"<>|\x00-\x1f]', "_", str(s)).strip(" .")
    s = s[:150].rstrip(" .") or "_"
    if s.split(".")[0].upper() in WIN_RESERVED:  # Windows 保留设备名
        s = "_" + s
    return s


# ---------------- config / credentials ----------------

def state_dir() -> Path:
    STATE_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(STATE_DIR, 0o700)
    return STATE_DIR


def load_config() -> dict:
    if not CONFIG_FILE.exists():
        return {}
    try:
        return json.loads(CONFIG_FILE.read_text())
    except ValueError as e:
        raise ZjuError(f"{CONFIG_FILE} 格式错误：{e}")


def save_config(cfg: dict):
    state_dir()
    CONFIG_FILE.write_text(json.dumps(cfg, ensure_ascii=False, indent=2))


def keychain_get(user: str) -> str | None:
    if sys.platform != "darwin":
        try:
            import keyring
            return keyring.get_password(KEYCHAIN_SERVICE, user)
        except Exception:
            return None
    r = subprocess.run(
        ["security", "find-generic-password", "-s", KEYCHAIN_SERVICE, "-a", user, "-w"],
        capture_output=True, text=True,
    )
    return r.stdout.rstrip("\n") if r.returncode == 0 else None


def keychain_set_interactive(user: str):
    if sys.platform != "darwin":  # Windows 凭据管理器 / Linux Secret Service
        import getpass
        import keyring
        try:
            keyring.set_password(KEYCHAIN_SERVICE, user, getpass.getpass("密码："))
        except Exception as e:
            raise ZjuError(f"系统凭证库不可用（{e}）；改用环境变量 ZJU_USER / ZJU_PASS")
        return
    # macOS 走系统 security CLI（定时任务读取不会弹授权窗口）；-w 放最后 = security 自己在 tty 上问密码，密码不进 argv / shell history
    r = subprocess.run(
        ["security", "add-generic-password", "-U", "-s", KEYCHAIN_SERVICE, "-a", user, "-w"]
    )
    if r.returncode != 0:
        raise ZjuError("写入 Keychain 失败")


def get_credentials() -> tuple[str, str]:
    user = os.environ.get("ZJU_USER") or load_config().get("username")
    if not user:
        raise ZjuError("尚未设置账号，先运行：zju.py login")
    pwd = os.environ.get("ZJU_PASS") or keychain_get(user)
    if not pwd:
        raise ZjuError("Keychain 找不到密码，先运行：zju.py login")
    return user, pwd


# ---------------- client ----------------

class LegacyTLS(HTTPAdapter):
    """只挂在 LEGACY_TLS_HOSTS：它们只给 1024-bit DHE，OpenSSL 3 报 DH_KEY_TOO_SMALL。"""

    def init_poolmanager(self, *a, **kw):
        ctx = ssl.create_default_context()
        ctx.set_ciphers("DEFAULT:@SECLEVEL=1")
        kw["ssl_context"] = ctx
        return super().init_poolmanager(*a, **kw)

    def proxy_manager_for(self, *a, **kw):
        ctx = ssl.create_default_context()
        ctx.set_ciphers("DEFAULT:@SECLEVEL=1")
        kw["ssl_context"] = ctx
        return super().proxy_manager_for(*a, **kw)


class NoCookieHTTP(HTTPAdapter):
    """明文 http:// 一律不带 cookie / Authorization。
    .zju.edu.cn 的 SSO cookie（iPlanetDirectoryPro 等）没设 Secure，浏览器和 requests
    都会照样发给 http 网址 —— 智云 PPT 图片就是 http，等于把登录凭证明文发出去。"""

    def send(self, request, **kw):
        request.headers.pop("Cookie", None)
        request.headers.pop("Authorization", None)
        return super().send(request, **kw)


class DownloadError(ZjuError):
    def __init__(self, msg: str, codes: list[int]):
        super().__init__(msg)
        self.codes = codes


class TooBig(ZjuError):
    pass


def secure_url(u: str) -> str:
    """学校主机的 http 网址升级成 https（实测 video.cmc 等都支持）。"""
    p = urlparse(u)
    if p.scheme == "http" and (p.hostname or "").endswith(".zju.edu.cn"):
        return "https" + u[4:]
    return u


def refer_params(activity: dict | None) -> dict | None:
    """拼 /uploads/{id}/blob 的 reference 参数，与官方网页前端下载按钮发送的相同：
    classroom→classroom_activity、exam 不带、其余→learning_activity；
    服务器只认 snake_case 参数名。"""
    if not activity or not activity.get("id"):
        return None
    t = activity.get("type")
    if t == "exam":
        return None
    return {"refer_id": activity["id"],
            "refer_type": "classroom_activity" if t == "classroom" else "learning_activity"}


class Zju:
    def __init__(self):
        self.jar = requests.cookies.RequestsCookieJar()  # 各线程 session 共用（CookieJar 自带锁）
        self._tl = threading.local()
        self.logged_in = False
        self._load_cookies()

    def _load_cookies(self):
        (STATE_DIR / "cookies.pkl").unlink(missing_ok=True)  # 旧版 pickle 缓存：不再读取
        if not COOKIE_FILE.exists():
            return
        try:
            for d in json.loads(COOKIE_FILE.read_text()):
                self.jar.set_cookie(requests.cookies.create_cookie(**d))
        except (ValueError, TypeError, KeyError):
            COOKIE_FILE.unlink(missing_ok=True)  # 坏了就重新登录

    @property
    def s(self) -> requests.Session:
        """每个线程一个 session：连接池不互抢，trust_env 切换也不会互相干扰。"""
        if not hasattr(self._tl, "s"):
            s = requests.Session()
            s.mount("https://", HTTPAdapter(pool_connections=8, pool_maxsize=8))
            for h in LEGACY_TLS_HOSTS:
                s.mount(f"https://{h}", LegacyTLS(pool_connections=8, pool_maxsize=8))
            s.mount("http://", NoCookieHTTP())
            s.headers["User-Agent"] = UA
            s.cookies = self.jar
            self._tl.s = s
        return self._tl.s

    def req(self, method: str, url: str, retry: bool | None = None, **kw) -> requests.Response:
        """默认直连，连不上才退回环境 proxy。
        重试只给幂等请求（GET 或明确 retry=True）；非幂等只在“确定没发出”（连接超时／proxy 错误）时重试，
        免得登录 POST 被重发、触发 CAS 验证码。TLS 错误不重试：要和断线区分开。"""
        kw.setdefault("timeout", TIMEOUT)
        idempotent = method in ("GET", "HEAD") if retry is None else retry
        last = None
        for attempt in range(4):
            self.s.trust_env = attempt % 2 == 1
            try:
                r = self.s.request(method, url, **kw)
            except requests.exceptions.SSLError as e:
                raise ZjuError(f"TLS 验证失败（网络可能被拦截，或学校证书有问题）：{url}\n{e}")
            except (requests.exceptions.ConnectTimeout, requests.exceptions.ProxyError) as e:
                last = e
            except (requests.ConnectionError, requests.Timeout) as e:
                if not idempotent:
                    raise ZjuError(f"连接中断（请求可能已发出，不自动重发）：{url}\n{e}")
                last = e
            else:
                if r.status_code in (429, 503) and attempt < 3:  # 被限流：按 Retry-After 退避
                    wait = r.headers.get("Retry-After", "")
                    r.close()
                    time.sleep(min(int(wait), 60) if wait.isdigit() else 2 * 2 ** attempt)
                    continue
                return r
            time.sleep(0.3 * 2 ** attempt)
        raise ZjuError(f"连接失败：{url}\n{last}")

    def get(self, url, **kw):
        return self.req("GET", url, **kw)

    def post(self, url, **kw):
        return self.req("POST", url, **kw)

    def save_cookies(self):
        """JSON 不是 pickle：缓存文件被别人改了也只是读到坏 cookie，不会运行代码。"""
        state_dir()
        data = [{"name": c.name, "value": c.value, "domain": c.domain, "path": c.path,
                 "secure": c.secure, "expires": c.expires, "rest": c._rest} for c in self.jar]
        tmp = COOKIE_FILE.with_suffix(".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            json.dump(data, f)
        os.replace(tmp, COOKIE_FILE)
        os.chmod(COOKIE_FILE, 0o600)

    # ---- auth ----

    def login(self, user: str, pwd: str):
        self.s.cookies.clear()
        text = self.get("https://zjuam.zju.edu.cn/cas/login").text
        m = re.search(r'name="execution" value="(.*?)"', text)
        if not m:
            raise ZjuError("CAS 页面找不到 execution 字段（登录页改版？）")
        key = self.get("https://zjuam.zju.edu.cn/cas/v2/getPubKey").json()
        n, e = int(key["modulus"], 16), int(key["exponent"], 16)
        enc = format(pow(int.from_bytes(pwd.encode(), "big"), e, n), "x")
        if len(enc) % 2:
            enc = "0" + enc
        r = self.post("https://zjuam.zju.edu.cn/cas/login", data={
            "username": user, "password": enc, "execution": m.group(1),
            "_eventId": "submit", "authcode": "",
        })
        if "统一身份认证平台" in r.text:
            raise ZjuError("登录失败：学号或密码错误（或需要验证码，先在浏览器登录一次）")
        # 让各子系统吃到 SSO
        self.get("https://courses.zju.edu.cn/user/courses")
        try:
            self.get("https://tgmedia.cmc.zju.edu.cn/index.php?r=auth/login&auType=cmc&tenant_code=112"
                     "&forward=https%3A%2F%2Fclassroom.zju.edu.cn%2F")
        except ZjuError as e:
            log(f"警告：智云 SSO 连不上（只影响 classroom/ppt/transcript）：{str(e).splitlines()[0]}")
        self.logged_in = True
        self.save_cookies()

    def courses_alive(self) -> bool:
        try:
            r = self.get("https://courses.zju.edu.cn/api/todos?no-intercept=true", allow_redirects=False)
            return r.status_code == 200 and "todo_list" in r.json()
        except (ValueError, ZjuError):
            return False

    def ensure(self, need_classroom: bool = False):
        if self.logged_in:
            return
        if self.courses_alive() and (not need_classroom or self._token(silent=True)):
            self.logged_in = True
            return
        self.login(*get_credentials())

    def _token(self, silent=False) -> str | None:
        for c in self.s.cookies:
            if (c.domain or "").lstrip(".") not in ("classroom.zju.edu.cn", "zju.edu.cn"):
                continue
            m = re.search(r'\{i:\d+;s:\d+:"_token";i:\d+;s:\d+:"(.+?)";\}', unquote(c.value or ""))
            if m:
                return m.group(1)
        if silent:
            return None
        raise ZjuError("智云课堂 token 解析失败：classroom cookie 格式可能改了，检查 _token 正则")

    def bearer(self) -> dict:
        return {"Authorization": f"Bearer {self._token()}"}

    def json(self, r: requests.Response, what: str):
        try:
            return r.json()
        except ValueError:
            raise ZjuError(f"{what}：响应不是 JSON（HTTP {r.status_code}），session 可能失效，重新运行即可")

    # ---- 学在浙大 ----

    def courses(self) -> list[dict]:
        self.ensure()
        out, page = [], 1
        while True:
            j = self.json(self.post("https://courses.zju.edu.cn/api/my-courses", retry=True, json={
                "fields": COURSE_FIELDS, "page": page, "page_size": 100,
                "conditions": {"status": ["ongoing", "notStarted", "closed"], "keyword": "",
                               "classify_type": "recently_started", "display_studio_list": False},
                "showScorePassedStatus": False,
            }), "my-courses")
            out += j.get("courses", [])
            if page >= j.get("pages", 1):
                return out
            page += 1

    def semesters(self) -> dict[int, str]:
        self.ensure()
        j = self.json(self.get("https://courses.zju.edu.cn/api/my-semesters?"), "semesters")
        return {x["id"]: x.get("name") or x.get("real_name") or str(x["id"]) for x in j.get("semesters", [])}

    def uploads(self, course_id: int) -> list[tuple[dict, dict]]:
        """返回 (活动, upload) — 含普通活动与作业附件。"""
        res = []
        j = self.json(self.get(f"https://courses.zju.edu.cn/api/courses/{course_id}/activities"), "activities")
        for a in j.get("activities", []):
            for u in a.get("uploads") or []:
                res.append((a, u))
        page = 1
        while True:
            j = self.json(self.get(
                f"https://courses.zju.edu.cn/api/courses/{course_id}/homework-activities",
                params={"conditions": '{"itemsSortBy":{"predicate":"module","reverse":false}}',
                        "page": page, "page_size": 20, "reloadPage": "false"}), "homework")
            for h in j.get("homework_activities", []):
                for u in h.get("uploads") or []:
                    res.append((h, u))
            if page >= (j.get("pages") or 1):
                return res
            page += 1

    def upload_response(self, uid: int, rid: int, activity: dict | None = None) -> tuple[requests.Response, str]:
        """附件下载来源，按优先级依次尝试，用第一个能拿到文件的，返回 (response, 来源)：
        1. reference blob — 常规下载
        2. upload blob — 原始文件
        3. upload blob + reference 参数 — 参数与官方网页前端相同，部分活动的附件由此提供
        4. 预览器转换的 PDF — document/{rid}/url?preview=true 返回 {url}
        全部来源都不可用时抛出 DownloadError（codes 为各来源的 HTTP 码）。"""
        base = "https://courses.zju.edu.cn/api/uploads"
        sources = [
            (f"{base}/reference/{rid}/blob", None, "下载"),
            (f"{base}/{uid}/blob", None, "原文件"),
        ]
        refer = refer_params(activity)
        if refer:
            sources.append((f"{base}/{uid}/blob", refer, "未开放原文件"))
        codes = []
        for url, params, src in sources:
            r = self.get(url, params=params, stream=True)
            if r.ok:
                return r, src
            codes.append(r.status_code)
            r.close()
        r = self.get(f"{base}/reference/document/{rid}/url", params={"preview": "true"})
        codes.append(r.status_code)
        if r.ok:
            try:
                url = r.json().get("url")
            except ValueError:
                url = None
            if url:
                r = self.get(secure_url(urljoin(base, url)), stream=True)
                if r.ok:
                    return r, "预览PDF"
                codes.append(r.status_code)
                r.close()
        raise DownloadError(f"下载失败 HTTP {'/'.join(map(str, codes))}", codes)

    def todos(self) -> list[dict]:
        self.ensure()
        return self.json(self.get("https://courses.zju.edu.cn/api/todos?no-intercept=true"), "todos").get("todo_list", [])

    def rollcalls(self) -> list[dict]:
        """进行中的签到（字段同 XMU-Rollcall-Bot）。只读列表：不碰 answer / student_rollcalls。"""
        self.ensure()
        return self.json(self.get(f"{LMS}/api/radar/rollcalls", params={"api_version": "1.1.0"}),
                         "rollcalls").get("rollcalls", [])

    # ---- 活动 / 讨论 / 作业提交（端点取自官方前端 JS）----

    def user_id(self) -> int:
        if not hasattr(self, "_uid"):
            self.ensure()
            m = re.search(r'ng-init="userId=(\d+);', self.get(f"{LMS}/user/index").text)
            if not m:
                raise ZjuError("无法获取自己的用户 ID（/user/index 改版？）")
            self._uid = int(m.group(1))
        return self._uid

    def activities(self, course_id: int) -> list[dict]:
        """课程所有活动（课件、视频、作业、讨论、网页、链接…）加上测验。"""
        self.ensure()
        acts = self.json(self.get(f"{LMS}/api/courses/{course_id}/activities"), "activities").get("activities", [])
        r = self.get(f"{LMS}/api/courses/{course_id}/exams")
        if r.ok:
            acts += [dict(e, type="exam") for e in self.json(r, "exams").get("exams", [])]
        return acts

    def assessments(self, course_id: int, types: list[str]) -> list[dict]:
        """提醒用的完整作业／测验列表；失败不能当成空列表。"""
        self.ensure()
        out = {}
        for kind in dict.fromkeys(types):
            route, key = ("homework-activities", "homework_activities") if kind == "homework" else ("exams", "exams")
            page = 1
            while True:
                params = {"page": page, "page_size": 100}
                if kind == "homework":
                    params.update(conditions='{"itemsSortBy":{"predicate":"module","reverse":false}}',
                                  reloadPage="false")
                r = self.get(f"{LMS}/api/courses/{course_id}/{route}", params=params)
                if not r.ok:
                    raise ZjuError(f"课程 {course_id} {route} 查询失败：HTTP {r.status_code}")
                data = self.json(r, route)
                if not isinstance(data, dict) or not isinstance(data.get(key), list):
                    raise ZjuError(f"课程 {course_id} {route} 响应缺少 {key} 列表")
                for row in data[key]:
                    if not isinstance(row, dict) or row.get("id") is None:
                        raise ZjuError(f"课程 {course_id} {route} 返回无效活动")
                    out[(kind, str(row["id"]))] = dict(row, type=kind)
                pages = data.get("pages") or 1  # exams 当前不分页；空作业列表的 pages 为 0
                if not isinstance(pages, int) or pages < 1:
                    raise ZjuError(f"课程 {course_id} {route} 返回无效分页信息")
                if page >= pages:
                    break
                page += 1
        return list(out.values())

    def activity(self, aid: int) -> dict:
        self.ensure()
        r = self.get(f"{LMS}/api/activities/{aid}")
        if r.status_code == 404:
            raise ZjuError(f"找不到活动 {aid}（测验请用课程的 activities 看）")
        return self.json(r, "activity")

    def forum_category(self, aid: int) -> int:
        """讨论活动 id → 讨论区分类 id（发帖、列帖都用分类 id）。"""
        cid = self.activity(aid)["course_id"]
        j = self.json(self.get(f"{LMS}/api/courses/{cid}/topic-categories"), "topic-categories")
        for cat in j.get("topic_categories", []):
            if cat.get("activity_id") == aid:
                return cat["id"]
        raise ZjuError(f"活动 {aid} 不是讨论（或没有讨论区分类）")

    def topics(self, category_id: int) -> list[dict]:
        out, page = [], 1
        while True:
            j = self.json(self.get(f"{LMS}/api/forum/categories/{category_id}",
                                   params={"page": page, "page_size": 50}), "forum")["result"]
            out += j.get("topics", [])
            if page >= (j.get("pages") or 1):
                return out
            page += 1

    def topic(self, tid: int) -> dict:
        self.ensure()
        return self.json(self.get(f"{LMS}/api/topics/{tid}"), "topic")

    def upload_file(self, path: Path) -> dict:
        """两段式：先登记取得 upload_url，再按 storage_type 上传文件（学校目前是本地存储 multipart PUT）。"""
        self.ensure()
        pre = self.json(self.post(f"{LMS}/api/uploads", json={
            "name": path.name, "size": path.stat().st_size, "parent_type": None, "parent_id": 0,
            "is_scorm": False, "is_wmpkg": False, "source": "", "is_marked_attachment": False,
            "embed_material_type": "",
        }), "uploads")
        if "upload_url" not in pre:
            raise ZjuError(f"上传登记失败：{pre}")
        if pre.get("storage_type") in ("S3", "QINIU"):
            raise ZjuError(f"存储后端 {pre['storage_type']} 尚未支持（学校改了上传方式）")
        with path.open("rb") as f:
            r = self.req("PUT", pre["upload_url"], files={"file": (path.name, f)}, retry=False,
                         timeout=(6, 600))
        if not r.ok:
            raise ZjuError(f"上传 {path.name} 失败 HTTP {r.status_code}：{r.text[:200]}")
        return pre

    def create_topic(self, category_id: int, title: str, content: str, uploads: list[int]) -> dict:
        r = self.post(f"{LMS}/api/topics", json={"title": title, "content": content,
                                                  "category_id": category_id, "uploads": uploads})
        if not r.ok:
            raise ZjuError(f"发帖失败 HTTP {r.status_code}：{r.text[:200]}")
        return self.json(r, "topic")

    def reply_topic(self, tid: int, content: str, uploads: list[int]) -> dict:
        self.ensure()
        r = self.post(f"{LMS}/api/topics/{tid}/replies", json={"content": content, "uploads": uploads})
        if not r.ok:
            raise ZjuError(f"回帖失败 HTTP {r.status_code}：{r.text[:200]}")
        return self.json(r, "reply")

    def my_submission(self, aid: int) -> dict:
        return self.json(self.get(f"{LMS}/api/course/activities/{aid}/students/{self.user_id()}/submission"),
                         "submission")

    def submit(self, aid: int, comment: str, uploads: list[int], draft: bool, mode: str,
               draft_id: int | None) -> dict:
        """与网页“提交”相同的 payload；已有草稿时用 PUT 覆盖草稿。"""
        body = {"comment": comment, "uploads": uploads, "slides": [], "is_draft": draft, "mode": mode,
                "other_resources": [], "uploads_in_rich_text": []}
        method = "POST"
        if draft_id:
            method, body["submission_id"] = "PUT", draft_id
        r = self.req(method, f"{LMS}/api/course/activities/{aid}/submissions", json=body)
        if not r.ok:
            raise ZjuError(f"提交失败 HTTP {r.status_code}：{r.text[:300]}")
        return self.json(r, "submission")

    # ---- 智云课堂 ----

    def infosimple(self) -> dict:
        self.ensure(need_classroom=True)
        return self.json(self.get("https://classroom.zju.edu.cn/userapi/v1/infosimple",
                                  headers=self.bearer()), "infosimple")["params"]

    def classroom_search(self, title: str, teacher: str = "") -> list[dict]:
        info = self.infosimple()
        out, page = [], 1
        while True:
            j = self.json(self.get("https://classroom.zju.edu.cn/pptnote/v1/searchlist", headers=self.bearer(), params={
                "tenant_id": 112, "user_id": info["id"], "user_name": info["account"], "page": page,
                "per_page": 16, "title": title, "realname": teacher, "trans": "", "tenant_code": 112,
                "randomKey": random.random()}), "searchlist")
            if j.get("code") != 0:
                raise ZjuError(j.get("msg", "searchlist 失败"))
            lst = j["total"]["list"]
            out += lst
            if not lst or len(out) >= int(j["total"]["total"]):
                return out
            page += 1

    def course_subs(self, course_id: int) -> list[dict]:
        info = self.infosimple()
        j = self.json(self.get("https://yjapi.cmc.zju.edu.cn/courseapi/v3/multi-search/get-course-detail",
                               headers=self.bearer(),
                               params={"course_id": course_id, "student": info["account"]}), "course-detail")
        data = j["data"]
        subs = []
        for year in (data.get("sub_list") or {}).values():
            for month in year.values():
                for week in month.values():
                    for s in week:
                        begin = int(s.get("class_begin") or 0)
                        subs.append({"course_id": course_id, "course_name": data["title"],
                                     "sub_id": int(s["id"]), "sub_name": s["sub_title"],
                                     "lecturer": s.get("lecturer_name", ""),
                                     "day": dt.date.fromtimestamp(begin) if begin else None})
        subs.sort(key=lambda s: s["sub_name"])
        return subs

    def day_subs(self, day: dt.date) -> list[dict]:
        self.ensure(need_classroom=True)
        j = self.json(self.get("https://classroom.zju.edu.cn/courseapi/v2/course-live/get-my-course-day",
                               headers=self.bearer(), params={"day": day.isoformat()}), "course-day")
        subs = []
        lst = j.get("list")
        for d in (lst.values() if isinstance(lst, dict) else lst or []):
            for c in d.get("course", []):
                subs.append({"course_id": int(c["id"]), "course_name": c["title"], "sub_id": int(c["sub_id"]),
                             "sub_name": c["sub_title"], "lecturer": c.get("realname", ""),
                             "begin": int(c.get("course_begin") or 0), "over": int(c.get("course_over") or 0)})
        return subs

    def ppt_urls(self, course_id: int, sub_id: int) -> list[str]:
        """智云 PPT 截图。API 不守 per_page：常常一页就返回全部、下一页再重复一次
        （ZLA 假设每页 ≤100 会在 >100 张时卡死重试）→ 按序去重，凑满 total 或遇到没新东西就停。"""
        self.ensure(need_classroom=True)
        urls: list[str] = []
        seen: set[str] = set()
        page = 1
        while True:
            j = self.json(self.get("https://classroom.zju.edu.cn/pptnote/v1/schedule/search-ppt", params={
                "course_id": course_id, "sub_id": sub_id, "page": page, "per_page": 100},
                headers=self.bearer()), "search-ppt")
            total = int(j.get("total") or 0)
            added = 0
            for p in j.get("list") or []:
                u = json.loads(p["content"]).get("pptimgurl")
                if u and u not in seen:
                    seen.add(u)
                    urls.append(u)
                    added += 1
            if len(urls) >= total or added == 0 or page >= 50:
                if len(urls) < total:
                    log(f"[注意] PPT 只拿到 {len(urls)}/{total} 张 course={course_id} sub={sub_id}")
                return urls
            page += 1

    def subtitle(self, sub_id: int) -> list[dict]:
        self.ensure(need_classroom=True)
        j = self.json(self.get("https://yjapi.cmc.zju.edu.cn/courseapi/v3/web-socket/search-trans-result",
                               params={"sub_id": sub_id, "format": "json"}), "trans-result")
        if j.get("code") == 10002:  # 未查询到语音数据：当天课程通常还没转完
            return []
        if j.get("code") != 0:
            raise ZjuError(f"取转录失败 code={j.get('code')} {j.get('msg', '')}")
        lst = j.get("list") or []
        return lst[0].get("all_content", []) if lst else []


# ---------------- helpers ----------------

class Manifest:
    """out/.zju_manifest.json：记录 upload id → 本地路径，换了版本（新 id）就重新下载。"""

    def __init__(self, root: Path):
        self.path = root / ".zju_manifest.json"
        self.data = json.loads(self.path.read_text()) if self.path.exists() else {}

    def get(self, key):
        return self.data.get(key)

    def put(self, key, val):
        self.data[key] = val
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.data, ensure_ascii=False, indent=1))
        os.replace(tmp, self.path)


def stream_to(r: requests.Response, dest: Path, limit: int | None = None) -> Path:
    """先写临时文件再 rename；校验长度、拒收空文件和错误页，免得坏文件被记进 manifest 后永远不再重新下载。"""
    expected = r.headers.get("Content-Length")
    expected = int(expected) if expected and expected.isdigit() and not r.headers.get("Content-Encoding") else None
    if limit and expected and expected > limit:
        r.close()
        raise TooBig(f"{expected / 2**20:.0f}MB")
    if "text/html" in r.headers.get("Content-Type", "") and dest.suffix.lower() not in (".html", ".htm"):
        r.close()
        raise ZjuError("服务器返回 HTML（错误页或登录页），不保存")
    dest.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=dest.parent, prefix=".part-")
    try:
        written = 0
        with os.fdopen(fd, "wb") as f:
            for chunk in r.iter_content(1 << 20):
                f.write(chunk)
                written += len(chunk)
                if limit and written > limit:
                    raise TooBig(f">{limit / 2**20:.0f}MB")
        if written == 0:
            raise ZjuError("服务器返回空文件")
        if expected is not None and written != expected:
            raise ZjuError(f"下载不完整：{written}/{expected} bytes")
        with open(tmp, "rb") as f:
            head = f.read(5)
        # preview 版常是 PDF，但文件名还是 .pptx/.docx — 补扩展名免得打不开
        if head == b"%PDF-" and dest.suffix.lower() != ".pdf":
            dest = dest.with_name(dest.name + ".pdf")
        os.replace(tmp, dest)
        return dest
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
    finally:
        r.close()


def current_year(courses: list[dict]) -> list[dict]:
    """is_closed 学校常不关，靠 academic_year_id 取最新学年。"""
    latest = max((c.get("academic_year_id") or 0 for c in courses), default=0)
    return [c for c in courses if (c.get("academic_year_id") or 0) == latest and not c.get("is_closed")]


def today_cst() -> dt.date:
    """课表以北京时间分日；主机在别的时区时 dt.date.today() 会差一天。"""
    return dt.datetime.now(CST).date()


def class_span(s: dict) -> str:
    if not s.get("begin"):
        return "-"
    f = lambda t: dt.datetime.fromtimestamp(t, CST).strftime("%H:%M")
    return f"{f(s['begin'])}-{f(s['over'])}"


def in_class(subs: list[dict], now: float, margin: int = 10) -> list[dict]:
    """now（Unix 秒）落在 [上课 - margin 分, 下课 + margin 分] 的课堂。"""
    m = margin * 60
    return [s for s in subs if s.get("begin") and s["begin"] - m <= now <= s["over"] + m]


def rollcall_kind(r: dict) -> str:
    return "雷达" if r.get("is_radar") else "数字" if r.get("is_number") else "点名"


def local_time(iso: str | None, fmt: str = "%m-%d %H:%M") -> str:
    if not iso:
        return "时间未定"
    try:
        d = dt.datetime.fromisoformat(iso.replace("Z", "+00:00"))
    except ValueError:
        return iso
    if d.tzinfo is None:
        d = d.replace(tzinfo=CST)
    return d.astimezone().strftime(fmt)


def match_courses(courses: list[dict], keys: list[str], include_all: bool) -> list[dict]:
    if not keys:
        return courses if include_all else current_year(courses)
    out = []
    for c in courses:
        for k in keys:
            if str(c["id"]) == k or k.lower() in c["name"].lower():
                out.append(c)
                break
    return out


ACT_TYPES = {
    "material": "课件", "online_video": "视频", "homework": "作业", "forum": "讨论", "exam": "测验",
    "page": "网页", "web_link": "链接", "questionnaire": "问卷", "classroom": "课堂互动",
    "lesson": "直播", "vocabulary": "单词", "survey": "调查", "chatroom": "聊天室",
}


def html_to_text(s: str | None) -> str:
    def link(m):  # 链接文字 ≠ 网址时两个都留；网址前后补空格，免得粘在中文上
        href, text = html.unescape(m.group(1)), re.sub(r"<[^>]+>", "", m.group(2)).strip()
        return f" {href} " if not text or html.unescape(text) == href else f"{text} ({href})"
    s = re.sub(r"(?is)<a\b[^>]*?href=\"([^\"]+)\"[^>]*>(.*?)</a>", link, s or "")
    s = re.sub(r"(?i)<img\b[^>]*?src=\"([^\"]+)\"[^>]*>", lambda m: f"[图 {urljoin(LMS, html.unescape(m.group(1)))}]", s)
    s = re.sub(r"(?i)<li\b[^>]*>", "- ", s)
    s = re.sub(r"(?i)</t[dh]>", "\t", s)
    s = re.sub(r"(?i)<br\s*/?>|</p>|</div>|</li>|</tr>|</h\d>", "\n", s)
    s = html.unescape(re.sub(r"<[^>]+>", "", s))
    s = re.sub(r"[ \t]+\n", "\n", re.sub(r"(?m)^- +", "- ", s))
    return re.sub(r"\n{3,}", "\n\n", s).strip()


def doc_to_text(data: bytes) -> str | None:
    """附件字节 → 纯文本；按文件头判断格式（预览来源可能把 docx 换成 PDF）。不支持就返回 None。"""
    if data[:4] == b"PK\x03\x04":
        import zipfile, io
        z = zipfile.ZipFile(io.BytesIO(data))
        names = z.namelist()
        if "word/document.xml" in names:
            parts = ["word/document.xml"]
            para, tag = r"</w:p>", r"<(?:w|m):t[^>]*>([^<]*)</(?:w|m):t>"  # m:t = 公式里的字
        else:
            parts = sorted((n for n in names if re.fullmatch(r"ppt/slides/slide\d+\.xml", n)),
                           key=lambda n: int(re.search(r"\d+", n.rsplit("/", 1)[1]).group()))
            para, tag = r"</a:p>", r"<a:t>([^<]*)</a:t>"
        if not parts:
            return None
        out = []
        for i, n in enumerate(parts):
            xml = z.read(n).decode("utf-8", "replace")
            lines = ["".join(re.findall(tag, p_)) + " [图]" * len(re.findall(r"<w:drawing>|<w:pict>", p_))
                     for p_ in re.split(para, xml)]  # 公式常是贴图，标出来免得以为漏字
            txt = "\n".join(l for l in lines if l.strip())
            out.append(f"--- 第 {i + 1} 页 ---\n{txt}" if len(parts) > 1 else txt)
        return html.unescape("\n".join(out)).strip()
    tool = None
    if data[:5] == b"%PDF-" and shutil.which("pdftotext"):
        tool = ["pdftotext", "-layout", "{f}", "-"]
    elif data[:4] == b"\xd0\xcf\x11\xe0" and shutil.which("textutil"):  # 旧版 .doc，macOS 内置
        tool = ["textutil", "-convert", "txt", "-stdout", "{f}"]
    elif data[:5] == b"%PDF-" or data[:4] == b"\xd0\xcf\x11\xe0":
        return None
    else:
        try:
            return data.decode("utf-8").strip()
        except UnicodeDecodeError:
            return None
    with tempfile.NamedTemporaryFile(suffix=".bin") as f:
        f.write(data)
        f.flush()
        r = subprocess.run([f.name if x == "{f}" else x for x in tool], capture_output=True)
    return r.stdout.decode("utf-8", "replace").strip() if r.returncode == 0 else None


def text_to_html(s: str) -> str:
    """纯文本 → 段落 HTML（空行分段、单换行 <br>），网页编辑器存的也是这种格式。"""
    paras = [p for p in re.split(r"\n\s*\n", s.strip()) if p.strip()]
    return "".join("<p>" + html.escape(p.strip()).replace("\n", "<br>") + "</p>" for p in paras)


def act_status(a: dict) -> str:
    if a.get("is_closed"):
        return "已关闭"
    if a.get("is_started") is False:
        return "未开始"
    end = a.get("end_time")
    if end and dt.datetime.fromisoformat(end.replace("Z", "+00:00")) < dt.datetime.now(dt.timezone.utc):
        return "已截止"
    return "进行中"


NOTIFY_REASONS = {"new": "新发现", "opened": "已开放", "schedule_changed": "时间变更", "due": "即将截止"}
ASSESSMENT_STATUSES = {"unpublished", "closed", "submitted", "upcoming", "open"}


def assessment_time(value: str | None) -> dt.datetime | None:
    if not value:
        return None
    try:
        parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (ValueError, AttributeError) as e:
        raise ZjuError(f"无法解析活动时间：{value!r}") from e
    return (parsed if parsed.tzinfo else parsed.replace(tzinfo=CST)).astimezone(dt.timezone.utc)


def assessment_snapshot(row: dict, now: dt.datetime) -> dict:
    start = assessment_time(row.get("start_time"))
    end = assessment_time(row.get("end_time") or row.get("deadline"))
    if row.get("published") is False or row.get("is_published") is False:
        status = "unpublished"
    elif row.get("is_closed") or (end and end <= now):
        status = "closed"
    elif row.get("submitted") is True or row.get("is_submitted") is True:
        status = "submitted"
    elif row.get("is_started") is False or (start and start > now):
        status = "upcoming"
    else:
        status = "open"
    return {"status": status, "start_time": start.isoformat() if start else None,
            "end_time": end.isoformat() if end else None}


def assessment_notifications(course: dict, rows: list[dict], seen: dict, now: dt.datetime,
                             due_within: int) -> list[dict]:
    """按课程、类型和 ID 去重；seen 是本轮内存副本，全部查询成功后才保存。"""
    events = []
    for row in rows:
        key = f"{course['id']}:{row['type']}:{row['id']}"
        previous = seen.get(key)
        current = assessment_snapshot(row, now)
        current["due_end"] = previous.get("due_end") if previous else None
        reasons = []
        if current["status"] in ("upcoming", "open"):
            if previous is None or previous["status"] == "unpublished":
                reasons.append("new")
            else:
                if current["status"] == "open" and previous["status"] != "open":
                    reasons.append("opened")
                if any(current[k] != previous[k] for k in ("start_time", "end_time")):
                    reasons.append("schedule_changed")
            end = assessment_time(current["end_time"])
            if (current["status"] == "open" and due_within > 0 and end
                    and dt.timedelta(0) < end - now <= dt.timedelta(minutes=due_within)
                    and current["due_end"] != current["end_time"]):
                reasons.append("due")
                current["due_end"] = current["end_time"]
        seen[key] = current
        if reasons:
            events.append({"id": row["id"], "type": row["type"], "title": row.get("title", ""),
                           "course_id": course["id"], "course_name": course["name"], "reasons": reasons,
                           **{k: current[k] for k in ("status", "start_time", "end_time")}})
    return events


@contextmanager
def notification_state(user_id: int):
    """按用户隔离，进程锁防止定时任务重叠；输出成功后原子保存。"""
    path = state_dir() / f"notifications-{user_id}.json"
    fd = os.open(path.with_suffix(".lock"), os.O_RDWR | os.O_CREAT, 0o600)
    with os.fdopen(fd, "r+b") as lock:
        try:
            if sys.platform == "win32":
                import msvcrt
                if not os.fstat(lock.fileno()).st_size:
                    lock.write(b"\0")
                    lock.flush()
                lock.seek(0)
                msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as e:
            raise ZjuError("另一个 notify 正在运行，请等它结束后重试") from e
        try:
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except FileNotFoundError:
                data = {"version": 1, "items": {}}
            except ValueError as e:
                raise ZjuError(f"通知记录 {path} 格式错误，请备份后移走再运行") from e
            if (not isinstance(data, dict) or data.get("version") != 1
                    or not isinstance(data.get("items"), dict)
                    or any(not isinstance(v, dict) or not isinstance(v.get("status"), str)
                           or v["status"] not in ASSESSMENT_STATUSES
                           or any(k not in v or (v[k] is not None and not isinstance(v[k], str))
                                  for k in ("start_time", "end_time", "due_end"))
                           for v in data["items"].values())):
                raise ZjuError(f"通知记录 {path} 格式错误，请备份后移走再运行")
            yield data["items"]
            fd, tmp = tempfile.mkstemp(prefix=".notifications-", dir=path.parent)
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    json.dump(data, f, ensure_ascii=False, indent=1)
                    f.flush()
                    os.fsync(f.fileno())
                os.replace(tmp, path)
            finally:
                Path(tmp).unlink(missing_ok=True)
        finally:
            if sys.platform == "win32":
                lock.seek(0)
                msvcrt.locking(lock.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(lock, fcntl.LOCK_UN)


def read_body(a) -> str:
    """--body 文字或 --body-file 文件（- = stdin）。"""
    if a.body_file:
        return sys.stdin.read() if a.body_file == "-" else Path(a.body_file).read_text(encoding="utf-8")
    return a.body or ""


def upload_all(z: "Zju", files: list[str] | None) -> list[int]:
    ids = []
    for f in files or []:
        p = Path(f).expanduser()
        if not p.is_file():
            raise ZjuError(f"找不到文件：{p}")
        u = z.upload_file(p)
        log(f"[上传] {p.name} → upload {u['id']}")
        ids.append(u["id"])
    return ids


def fmt_ts(sec: float, srt=True) -> str:
    sec = float(sec)
    h, rem = divmod(int(sec), 3600)
    m, s = divmod(rem, 60)
    ms = int((sec - int(sec)) * 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}" if srt else f"{h:02d}:{m:02d}:{s:02d}"


def render_transcript(items: list[dict], fmt: str, title: str) -> str:
    lines = []
    if fmt == "srt":
        for i, c in enumerate(items, 1):
            lines += [str(i), f"{fmt_ts(c.get('BeginSec', 0))} --> {fmt_ts(c.get('EndSec', 0))}", c.get("Text", ""), ""]
    elif fmt == "md":
        lines.append(f"# {title}\n")
        for c in items:
            lines.append(f"**[{fmt_ts(c.get('BeginSec', 0), False)}]** {c.get('Text', '')}  ")
    else:
        for c in items:
            lines.append(f"[{fmt_ts(c.get('BeginSec', 0), False)}] {c.get('Text', '')}")
    return "\n".join(lines) + "\n"


def images_to_pdf(paths: list[Path], pdf: Path):
    import img2pdf
    from PIL import Image

    tmp = pdf.with_name(".part-" + pdf.name)

    def write(srcs):
        with open(tmp, "wb") as f:  # 直接流式写入文件，不在内存里拼整个 PDF
            img2pdf.convert([str(x) for x in srcs], outputstream=f)

    try:
        try:
            write(paths)  # 智云截图几乎都是 JPEG：直接嵌入，不重新编码
        except Exception:
            fixed = []  # 有 alpha / 特殊格式的才转 JPEG
            for p in paths:
                with Image.open(p) as im:
                    if im.format == "JPEG" and im.mode in ("RGB", "L", "CMYK"):
                        fixed.append(p)
                        continue
                    q = p.with_suffix(".conv.jpg")
                    im.convert("RGB").save(q, quality=92)
                    fixed.append(q)
            write(fixed)
        os.replace(tmp, pdf)
    finally:
        tmp.unlink(missing_ok=True)


def dedup_slides(paths: list[Path], max_lost_cells: int = 2) -> list[Path]:
    """智云截图去重。智云是对投影画面定时截图，同一页会因动画逐步出现、老师边讲边写、
    翻回前面而被截很多次。规则只有一条：一页的笔画若全都还在后面那页（或之前留下的某页）里，
    它就是多余的——所以连续的一串只留最后、最完整的一张，批注不会丢。

    “笔画”= 跟 15×15 邻域中位数差很多的像素，大片纯色（白底、黑底、视频画面）不算；
    八成以上的页都有的（底图纹理、黑边、页脚）也不算。“全都还在”= 消失的笔画没有聚成块：
    有 4 个以上笔画像素消失的 8×8 格不超过 max_lost_cells 个（JPEG 噪点零星，真的少了东西会成块；
    再高就抓不到视频里又细又淡的线）。
    在三堂课（白底英文、底图＋手写、黑底教学视频夹着资源管理器画面）逐页核对过，没有误删。
    """
    import numpy as np
    from PIL import Image, ImageFilter

    T, W, H = 40, 512, 288  # 灰度阈值；分辨率再低，细的手写笔迹就糊掉看不见了
    if len(paths) < 2:
        return list(paths)

    def prep(p):
        with Image.open(p) as im:
            g = im.convert("L").resize((W, H), Image.BOX)
        f = np.asarray(g, dtype=np.int16)
        return f, strokes(f)

    def strokes(f):
        bg = Image.fromarray(f.astype(np.uint8)).filter(ImageFilter.MedianFilter(15))
        return np.abs(f - np.asarray(bg, dtype=np.int16)) > T

    frames, raw = zip(*map(prep, paths))
    frames = np.stack(frames)
    med = np.median(frames, axis=0).astype(np.int16)
    # 版面 = 八成以上的页在那里都一样的笔画；只看中位数的话，一张讲了半堂课的幻灯片会被当成版面
    layout = strokes(med) & ((np.abs(frames - med) <= T).sum(axis=0) >= 0.8 * len(frames))
    masks = [m & ~(layout & (np.abs(f - med) <= T)) for f, m in zip(frames, raw)]

    def lost(i, js):  # i 的笔画在 js 各页消失成块的格数
        gone = masks[i] & (np.abs(frames[i] - frames[js]) > T)
        return (gone.reshape(len(js), H // 8, 8, W // 8, 8).sum(axis=(2, 4)) >= 4).sum(axis=(1, 2))

    keep: list[int] = []
    for j in range(len(frames)):
        if frames[j].std() < 3:  # 全黑 / 全白过场
            continue
        if keep and lost(keep[-1], [j])[0] <= max_lost_cells:
            keep[-1] = j  # 前一张是这张的子集（动画没跑完、还没写完）→ 换成较完整的这张
            continue
        # 翻回讲过的页、擦掉批注的干净版；近乎空白的页什么都“包含得住”，不拿来比
        if keep and masks[j].sum() >= 400 and (lost(j, keep) <= max_lost_cells).any():
            continue
        keep.append(j)
    return [paths[k] for k in keep] or list(paths)


def tracked_courses() -> dict[str, str]:
    """config.json 的 tracked_courses：{智云 course_id: 显示名}。
    听课申请已获批准的课不在 get-my-course-day（只返回课表），智云也没有“我的申请”列表 API，只能自己记。"""
    return load_config().get("tracked_courses") or {}


def range_subs(z: Zju, end: dt.date, days: int) -> list[tuple[dt.date, dict]]:
    """end 往前 days 天：课表的课＋追踪课程当天的堂次，按 sub_id 去重。"""
    out, seen = [], set()
    for i in range(days):
        d = end - dt.timedelta(days=i)
        for s in z.day_subs(d):
            if s["sub_id"] not in seen:
                seen.add(s["sub_id"])
                out.append((d, s))
    start = end - dt.timedelta(days=days - 1)
    for cid, name in tracked_courses().items():
        try:
            subs = z.course_subs(int(cid))
        except Exception as e:  # 一门坏掉不拖垮课表的课
            log(f"[失败] 追踪课程 {cid} {name}: {e}")
            continue
        for s in subs:
            if s["day"] and start <= s["day"] <= end and s["sub_id"] not in seen:
                seen.add(s["sub_id"])
                # 旁听课常和自己那班同名同节次（别的老师开的同一门），不改名会写进同一个文件夹而被当成已存在跳过
                out.append((s["day"], {**s, "course_name": name}))
    out.sort(key=lambda x: x[0], reverse=True)
    return out


def resolve_subs(z: Zju, a) -> list[dict]:
    if a.course:
        subs = z.course_subs(a.course)
        name = tracked_courses().get(str(a.course))
        if name:
            subs = [{**s, "course_name": name} for s in subs]
        if a.sub:
            subs = [s for s in subs if s["sub_id"] in a.sub]
        return subs
    return [s for _, s in range_subs(z, today_cst(), a.days or 1)]


# ---------------- commands ----------------

def cmd_login(a):
    cfg = load_config()
    user = a.username or input(f"学号 [{cfg.get('username', '')}]: ").strip() or cfg.get("username")
    if not user:
        raise ZjuError("没有学号")
    if not keychain_get(user) or a.reset:
        print("输入统一身份认证密码（存进系统凭证库）：")
        keychain_set_interactive(user)
    cfg["username"] = user
    save_config(cfg)
    z = Zju()
    z.login(*get_credentials())
    ok = z._token(silent=True) is not None
    print(f"登录成功：{user}；智云课堂 token {'OK' if ok else '缺（classroom 命令可能失败）'}")


def cmd_courses(a):
    z = Zju()
    cs = z.courses()
    if a.json:
        print(json.dumps(cs, ensure_ascii=False, indent=1))
        return
    sem = z.semesters()
    for c in (cs if a.all else current_year(cs)):
        teachers = ",".join(i["name"] for i in c.get("instructors") or [])
        print(f"{c['id']}\t{sem.get(c.get('semester_id'), '-')}\t{c['name']}\t{teachers}")


def cmd_sync(a):
    z = Zju()
    courses = match_courses(z.courses(), a.course, a.all)
    if not courses:
        raise ZjuError("没有符合的课程（用 courses --all 看 id）")
    root = Path(a.out).expanduser()
    root.mkdir(parents=True, exist_ok=True)
    man = Manifest(root)
    new = skipped = failed = media = total = 0
    big: list[str] = []
    jobs: list[tuple] = []
    pending: list[str] = []
    dup = {n for n in (c["name"] for c in courses) if [x["name"] for x in courses].count(n) > 1}
    for c in courses:
        # 同名课程（不同班）分开放，免得同名文件互相覆盖
        cdir = root / safe_name(f"{c['name']} ({c['id']})" if c["name"] in dup else c["name"])
        items = z.uploads(c["id"])
        log(f"== {c['name']}（{len(items)} 个文件）")
        # 同名不同文件：全部加 id，命名不依赖 API 返回的顺序（顺序变了也不会重新下载、不会互换）
        uids_by_name: dict[str, set] = {}
        for _, u in items:
            uids_by_name.setdefault(safe_name(u.get("name") or str(u["id"])), set()).add(u["id"])
        seen_keys: set[str] = set()
        for a_, u in items:
            act = a_.get("title", "")
            uid, rid = u["id"], u.get("reference_id") or u["id"]
            key = f"{c['id']}:{uid}"
            if key in seen_keys:  # 同一个文件同时挂在活动和作业下
                continue
            seen_keys.add(key)
            name = safe_name(u.get("name") or str(uid))
            if len(uids_by_name[name]) > 1:
                stem, dot, ext = name.rpartition(".")
                name = f"{stem} ({uid}).{ext}" if dot else f"{name} ({uid})"
            rec = man.get(key)
            if rec and (root / rec["path"]).exists():
                skipped += 1
                continue
            size = u.get("size") or 0
            if not a.videos and Path(name).suffix.lower() in MEDIA_EXT:
                media += 1
                continue
            if a.max_size and size > a.max_size * 2**20:
                big.append(f"{c['name']}/{name}  {size / 2**20:.0f}MB")
                continue
            if a.dry_run:
                print(f"[会下载] {c['name']}/{name}  {size / 2**20:.1f}MB  ({act})")
                new += 1
                total += size
                continue
            jobs.append((key, uid, rid, cdir / name, a_))

    limit = a.max_size * 2**20 if a.max_size else None

    def fetch(job):
        key, uid, rid, dest, a_ = job
        r, src = z.upload_response(uid, rid, a_)
        return stream_to(r, dest, limit), src  # API 没给 size 的文件，靠 Content-Length 把关

    # 多文件并行：单条连接常被服务器限速，并行吃满带宽；manifest 只在主线程写
    with ThreadPoolExecutor(max_workers=max(1, a.jobs)) as pool:
        futs = {pool.submit(fetch, j): j for j in jobs}
        for f in as_completed(futs):
            key, uid, rid, dest0, a_ = futs[f]
            act = a_.get("title", "")
            try:
                dest, src = f.result()
                man.put(key, {"path": str(dest.relative_to(root)), "rid": rid, "source": src,
                              "activity": act, "at": dt.datetime.now().isoformat(timespec="seconds")})
                new += 1
                print(f"[{src}] {dest.relative_to(root)}", flush=True)
            except TooBig as e:
                big.append(f"{dest0.parent.name}/{dest0.name}  {e}")
            except Exception as e:
                if a_.get("is_started") is False and 403 in getattr(e, "codes", ()):
                    # 定时发布还没开放、所有来源都返回 403：开放后下次 sync 自动下载
                    pending.append(f"{dest0.parent.name}/{dest0.name}（{local_time(a_.get('start_time'))} 开放）")
                    continue
                failed += 1
                log(f"[失败] {dest0.name}: {e}")
    for p_ in pending:
        log(f"[未开放] {p_}")
    for b in big:
        log(f"[太大跳过] {b}")
    if big:
        log(f"  → {len(big)} 个文件超过 {a.max_size}MB，要下载就指定课程加 --max-size 0")
    extra = f"、未开放 {len(pending)}" if pending else ""
    extra += f"、音视频跳过 {media}（加 --videos 才下载）" if media else ""
    size_s = f"（约 {total / 2**20:.0f}MB）" if a.dry_run else ""
    log(f"{'预览' if a.dry_run else '完成'}：{'待下载' if a.dry_run else '新增'} {new}{size_s}、已有 {skipped}、失败 {failed}{extra} → {root}")
    if failed:
        sys.exit(2)


def cmd_todo(a):
    z = Zju()
    ts = z.todos()
    if a.json:
        print(json.dumps(ts, ensure_ascii=False, indent=1))
        return
    for t in sorted(ts, key=lambda t: t.get("end_time") or ""):
        end = local_time(t.get("end_time"), "%Y-%m-%d %H:%M")  # API 给 UTC
        print(f"{end}\t{t.get('course_name', '')}\t{t.get('title', '')}\t{t.get('type', '')}")


def cmd_notify(a):
    if a.due_within < 0:
        raise ZjuError("--due-within 不能为负数（0 = 不提醒临近截止）")
    z = Zju()
    now_subs = None
    if a.in_class:
        now_subs = in_class(today_subs(z), time.time(), a.margin)
        if not now_subs:
            log("现在不是上课时段，跳过")
            if a.json:
                print("[]")
            return
        log("上课中：" + "、".join(f"{s['course_name']} {class_span(s)}" for s in now_subs))
    courses = match_courses(z.courses(), a.course, a.all)
    if now_subs:  # 课上小测只会发在正在上的课；课表名和课程名对不上时退回全查
        names = {s["course_name"].strip() for s in now_subs}
        courses = [c for c in courses if c["name"].strip() in names] or courses
    if not courses:
        raise ZjuError("没有符合的课程（用 courses --all 看 id）")
    try:
        with notification_state(z.user_id()) as seen:
            now = dt.datetime.now(dt.timezone.utc)
            events = []
            for course in courses:
                rows = z.assessments(course["id"], a.type)
                events.extend(assessment_notifications(course, rows, seen, now, a.due_within))
            events.sort(key=lambda e: (e["end_time"] or "9999", str(e["course_id"]), e["type"], str(e["id"])))
            if a.init:
                events = []
                log("通知记录已初始化，当前项目不输出提醒；后续新增、开放或时间变更仍会提醒")
            if a.json:
                print(json.dumps(events, ensure_ascii=False, indent=1))
            else:
                for e in events:
                    reasons = "、".join(NOTIFY_REASONS[r] for r in e["reasons"])
                    start = local_time(e["start_time"], "%Y-%m-%d %H:%M")
                    end = local_time(e["end_time"], "%Y-%m-%d %H:%M")
                    print(f"[{reasons}]\t{e['course_name']}\t{ACT_TYPES[e['type']]}\t{e['title']}"
                          f"\t开放 {start}\t截止 {end}\t课程 {e['course_id']} / 活动 {e['id']}")
            sys.stdout.flush()  # 管道输出失败时不把提醒记成已发送
    except OSError as e:
        raise ZjuError(f"通知输出或记录读写失败：{e}") from e


def cmd_activities(a):
    z = Zju()
    courses = match_courses(z.courses(), a.course, a.all)
    if not courses:
        raise ZjuError("没有符合的课程（用 courses --all 看 id）")
    rows = []
    for c in courses:
        for x in z.activities(c["id"]):
            if a.type and x.get("type") not in a.type:
                continue
            rows.append((c, x))
    if a.json:
        print(json.dumps([dict(x, course_name=c["name"]) for c, x in rows], ensure_ascii=False, indent=1))
        return
    for c, x in rows:
        t = x.get("type", "")
        end = local_time(x.get("end_time"), "%Y-%m-%d %H:%M") if x.get("end_time") else "-"
        print(f"{x['id']}\t{c['name']}\t{ACT_TYPES.get(t, t)}\t{act_status(x)}\t{end}\t{x.get('title', '')}")


def cmd_show(a):
    z = Zju()
    x = z.activity(a.activity)
    t = x.get("type", "")
    d = x.get("data") or {}
    print(f"[{ACT_TYPES.get(t, t)}] {x.get('title')}  (id {x['id']}, 课程 {x.get('course_id')})")
    print(f"状态：{act_status(x)}　开始 {local_time(x.get('start_time'), '%Y-%m-%d %H:%M')}"
          f"　截止 {local_time(x.get('end_time'), '%Y-%m-%d %H:%M') if x.get('end_time') else '无'}")
    if x.get("completion_criterion"):
        print(f"完成条件：{x['completion_criterion']}")
    desc = html_to_text(d.get("description") or x.get("description"))
    if desc:
        print(f"\n{desc}\n")
    for u in x.get("uploads") or []:
        print(f"附件：{u.get('name')}  (upload {u.get('id')})")
        if a.read:
            try:
                r, _ = z.upload_response(u["id"], u.get("reference_id") or u["id"], x)
                txt = doc_to_text(r.content)
            except ZjuError as e:
                txt = f"（{e}）"
            print(f"\n{txt or '（这种格式读不出文字，用 sync 下载后自己打开）'}\n")
    if x.get("uploads") and not a.read and not desc:
        print("（说明在附件里：加 --read 直接打印附件内容）")
    if t == "web_link" and d.get("link"):
        print(f"链接：{d['link']}")
    if t == "homework":
        s = z.my_submission(x["id"])
        if s.get("created_at"):
            kind = "草稿" if s.get("is_draft") else "已提交"
            print(f"我的提交：{kind} {local_time(s.get('created_at'), '%Y-%m-%d %H:%M')}"
                  f"　分数 {s.get('score') if s.get('score') is not None else '未评'}")
            for u in s.get("uploads") or []:
                print(f"  - {u.get('name')}")
            if s.get("comment"):
                print("  " + html_to_text(s["comment"]).replace("\n", "\n  "))
        else:
            print("我的提交：尚未提交")
    elif t == "forum":
        ts = z.topics(z.forum_category(x["id"]))
        print(f"讨论帖 {len(ts)} 条（forum list {x['id']} 看全部）")


def cmd_forum(a):
    z = Zju()
    if a.action == "list":
        uid = z.user_id() if a.mine else None
        for t in z.topics(z.forum_category(a.id)):
            by = t.get("created_by") or {}
            if uid and by.get("id") != uid:
                continue
            print(f"{t['id']}\t{local_time(t.get('created_at'))}\t{by.get('name', '')}\t"
                  f"回复 {t.get('reply_count', 0)}\t{t.get('title', '')}")
            if a.full:
                print("  " + html_to_text(t.get("content")).replace("\n", "\n  "))
    elif a.action == "read":
        t = z.topic(a.id)
        by = t.get("created_by") or {}
        print(f"# {t.get('title')}\n{by.get('name', '')}  {local_time(t.get('created_at'), '%Y-%m-%d %H:%M')}\n")
        print(html_to_text(t.get("content")))
        for u in t.get("uploads") or []:
            print(f"附件：{u.get('name')}")

        def show(rs, depth):
            for r in rs or []:
                rb = r.get("created_by") or {}
                pad = "  " * depth
                print(f"\n{pad}↳ {rb.get('name', '')}  {local_time(r.get('created_at'))}")
                print(pad + html_to_text(r.get("content")).replace("\n", "\n" + pad))
                show(r.get("replies"), depth + 1)
        show(t.get("replies"), 1)
    else:
        body = read_body(a)
        if not body.strip():
            raise ZjuError("内容是空的：用 --body 或 --body-file")
        content = body if a.html else text_to_html(body)
        if a.action == "post":
            if not a.title:
                raise ZjuError("发帖要 --title")
            cat = z.forum_category(a.id)
            ids = upload_all(z, a.attach)
            t = z.create_topic(cat, a.title, content, ids)
            print(f"[已发帖] topic {t['id']}：{t.get('title')}")
        else:
            ids = upload_all(z, a.attach)
            r = z.reply_topic(a.id, content, ids)
            print(f"[已回帖] reply {r.get('id')} → topic {a.id}")


def cmd_upload(a):
    z = Zju()
    for f in a.files:
        p = Path(f).expanduser()
        if not p.is_file():
            raise ZjuError(f"找不到文件：{p}")
        u = z.upload_file(p)
        print(f"{u['id']}\t{p.name}")


def cmd_submit(a):
    z = Zju()
    x = z.activity(a.activity)
    if x.get("type") != "homework":
        raise ZjuError(f"活动 {a.activity} 是 {x.get('type')}，不是作业")
    status = act_status(x)
    if status != "进行中" and not x.get("is_resubmit_open"):
        raise ZjuError(f"作业“{x.get('title')}”{status}，网页上也交不了")
    comment = read_body(a)
    if not comment.strip() and not a.file and not a.upload_id:
        raise ZjuError("没有东西可交：给 --file、--upload-id 或 --body")
    prev = z.my_submission(x["id"])
    draft_id = prev.get("id") if prev.get("is_draft") else None
    kind = "存草稿" if a.draft else "正式提交"
    print(f"{kind}“{x.get('title')}”（截止 {local_time(x.get('end_time'), '%Y-%m-%d %H:%M')}）")
    for f in a.file or []:
        print(f"  文件：{f}")
    if comment.strip():
        print(f"  文字：{comment.strip()[:80]}{'…' if len(comment.strip()) > 80 else ''}")
    if prev.get("created_at") and not prev.get("is_draft"):
        print("  注意：已经交过一次，这次会新增一份提交")
    if not a.yes:
        if not sys.stdin.isatty():
            raise ZjuError("非交互环境要加 --yes 才会真的提交")
        try:
            ok = input("确定提交？[y/N] ").strip().lower() == "y"
        except EOFError:  # Windows 的 NUL 也算 tty，读不到就当取消
            ok = False
        if not ok:
            log("已取消（非交互环境加 --yes）")
            return
    ids = upload_all(z, a.file) + (a.upload_id or [])
    content = comment if a.html else text_to_html(comment) if comment.strip() else ""
    s = z.submit(x["id"], content, ids, a.draft, (x.get("data") or {}).get("mode") or "normal", draft_id)
    print(f"[{kind}] submission {s.get('id', '')} ✓")


def cmd_classroom(a):
    z = Zju()
    if a.action == "search":
        for c in z.classroom_search(a.arg or "", a.teacher or ""):
            print(f"{c.get('course_id')}\t{c.get('title')}\t{c.get('realname')}\t{c.get('term_name', '')}")
    elif a.action == "subs":
        if not a.arg:
            raise ZjuError("用法：classroom subs <course_id>")
        for s in z.course_subs(int(a.arg)):
            print(f"{s['sub_id']}\t{s['sub_name']}\t{s['lecturer']}")
    elif a.action == "day":
        start = dt.date.fromisoformat(a.arg) if a.arg else today_cst()
        for d, s in range_subs(z, start, a.days or 1):
            print(f"{d}\t{s['course_id']}\t{s['sub_id']}\t{s['course_name']}\t{s['sub_name']}\t{s['lecturer']}"
                  f"\t{class_span(s)}")
    elif a.action in ("add", "rm"):
        ids = [a.arg, *a.more] if a.arg else []
        if not ids or not all(v.isdigit() for v in ids):
            raise ZjuError(f"用法：classroom {a.action} <course_id>...（course_id 用 classroom search 查）")
        cfg = load_config()
        tracked = cfg.setdefault("tracked_courses", {})
        for cid in ids:
            if a.action == "rm":
                name = tracked.pop(cid, None)
                print(f"[移除] {cid} {name}" if name else f"[未追踪] {cid}")
                continue
            subs = z.course_subs(int(cid))  # 顺便验证 id 存在
            name = f"{subs[0]['course_name']} {subs[0]['lecturer']}" if subs else cid
            tracked[cid] = name
            print(f"[追踪] {cid} {name}（{len(subs)} 堂）")
        save_config(cfg)
    elif a.action == "tracked":
        for cid, name in tracked_courses().items():
            print(f"{cid}\t{name}")


def today_subs(z: Zju) -> list[dict]:
    """当日课表一天只抓一次（定时任务每几分钟运行一次，别每次都打智云）。"""
    today = today_cst().isoformat()
    try:
        cache = json.loads(SCHEDULE_FILE.read_text())
        if cache.get("day") == today:
            return cache["subs"]
    except (OSError, ValueError, KeyError):
        pass
    subs = z.day_subs(today_cst())
    state_dir()
    SCHEDULE_FILE.write_text(json.dumps({"day": today, "subs": subs}, ensure_ascii=False))
    return subs


def cmd_rollcall(a):
    z = Zju()
    if a.in_class:
        now_subs = in_class(today_subs(z), time.time(), a.margin)
        if not now_subs:
            log("现在不是上课时段，跳过")
            if a.json:
                print("[]")
            return
        log("上课中：" + "、".join(f"{s['course_name']} {class_span(s)}" for s in now_subs))
    rs = z.rollcalls()
    if a.json:
        print(json.dumps(rs, ensure_ascii=False, indent=1))
        return
    if not rs:
        log("没有进行中的签到")
    for r in rs:
        signed = "已签" if r.get("status") == "on_call_fine" else "未签"
        print(f"{r.get('rollcall_id')}\t{r.get('course_title', '')}\t{rollcall_kind(r)}\t{signed}"
              f"\t{r.get('created_by_name', '')}")


def cmd_ppt(a):
    z = Zju()
    root = Path(a.out).expanduser()
    subs = resolve_subs(z, a)
    if not subs:
        log("没有课堂")
        return
    failed = 0
    for s in subs:
        try:
            ppt_one(z, a, root, s)
        except Exception as e:  # 一堂坏掉不拖垮其他堂
            failed += 1
            log(f"[失败] {s['course_name']} {s['sub_name']}: {e}")
    if failed:
        sys.exit(2)


def ppt_one(z: Zju, a, root: Path, s: dict):
    cdir = root / safe_name(s["course_name"]) / "智云PPT"
    pdf = cdir / f"{safe_name(s['sub_name'])}.pdf"
    if pdf.exists() and not a.force:
        log(f"[跳过] {pdf.relative_to(root)}")
        return
    urls = z.ppt_urls(s["course_id"], s["sub_id"])
    if not urls:
        log(f"[无PPT] {s['course_name']} {s['sub_name']}")
        return
    tmpdir = Path(tempfile.mkdtemp(prefix="zju-ppt-"))
    try:
        def grab(iu):
            i, u = iu
            p = tmpdir / f"{i:04d}{Path(urlparse(u).path).suffix[:5] or '.jpg'}"
            for attempt in range(5):
                r = z.get(secure_url(u))
                if r.ok and r.content:
                    p.write_bytes(r.content)
                    return p
                time.sleep(0.2 * 2 ** attempt)
            raise ZjuError(f"PPT 图下载失败：{u}")

        with ThreadPoolExecutor(max_workers=8) as pool:
            paths = list(pool.map(grab, enumerate(urls)))  # map 保序 = 页序
        pages = dedup_slides(paths) if a.dedup else paths
        cdir.mkdir(parents=True, exist_ok=True)
        images_to_pdf(pages, pdf)
        if a.keep_images:  # 留全部原图，去重只影响 PDF
            shutil.copytree(tmpdir, cdir / safe_name(s["sub_name"]), dirs_exist_ok=True)
        note = f"，去重前 {len(paths)}" if len(pages) != len(paths) else ""
        print(f"[PDF] {pdf.relative_to(root)}（{len(pages)} 页{note}）")
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def cmd_transcript(a):
    z = Zju()
    root = Path(a.out).expanduser()
    failed = 0
    for s in resolve_subs(z, a):
        out = root / safe_name(s["course_name"]) / "转录" / f"{safe_name(s['sub_name'])}.{a.format}"
        if out.exists() and not a.force:
            log(f"[跳过] {out.relative_to(root)}")
            continue
        try:
            items = z.subtitle(s["sub_id"])
        except Exception as e:
            failed += 1
            log(f"[失败] {s['course_name']} {s['sub_name']}: {e}")
            continue
        if not items:
            log(f"[无转录] {s['course_name']} {s['sub_name']}")
            continue
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(render_transcript(items, a.format, f"{s['course_name']} {s['sub_name']}"))
        print(f"[转录] {out.relative_to(root)}（{len(items)} 段）")
    if failed:
        sys.exit(2)


def main():
    p = argparse.ArgumentParser(prog="zju.py", description="学在浙大 / 智云课堂 CLI")
    try:
        default_out = os.environ.get("ZJU_OUT") or load_config().get("out") or str(DEFAULT_OUT)
    except ZjuError as e:
        log(f"错误：{e}")
        sys.exit(1)
    p.add_argument("--out", default=default_out, help=f"输出根目录（目前 {default_out}；config.json 的 out 或 ZJU_OUT 可改）")
    sp = p.add_subparsers(dest="cmd", required=True)

    x = sp.add_parser("login", help="设置学号并把密码存进 Keychain")
    x.add_argument("username", nargs="?")
    x.add_argument("--reset", action="store_true", help="重设 Keychain 密码")
    x.set_defaults(fn=cmd_login)

    x = sp.add_parser("courses", help="列出学在浙大课程")
    x.add_argument("--all", action="store_true", help="含往年课程（默认只列最新学年）")
    x.add_argument("--json", action="store_true")
    x.set_defaults(fn=cmd_courses)

    x = sp.add_parser("sync", help="增量同步课件")
    x.add_argument("course", nargs="*", help="课程 id 或名称片段；省略 = 最新学年所有课程")
    x.add_argument("--all", action="store_true", help="没指定课程时下载全部学年")
    x.add_argument("--dry-run", action="store_true")
    x.add_argument("--videos", action="store_true", help="连音视频文件也下载（默认跳过）")
    x.add_argument("-j", "--jobs", type=int, default=4, help="并行下载数（默认 4）")
    x.add_argument("--max-size", type=int, default=200, metavar="MB", help="单个文件上限，超过只列出（默认 200，0 = 不限）")
    x.set_defaults(fn=cmd_sync)

    x = sp.add_parser("todo", help="待办事项")
    x.add_argument("--json", action="store_true")
    x.set_defaults(fn=cmd_todo)

    x = sp.add_parser("notify", help="测验／作业提醒：新增、开放、时间变更、临近截止（本地去重）")
    x.add_argument("course", nargs="*", help="课程 id 或名称片段；省略 = 最新学年所有课程")
    x.add_argument("--all", action="store_true", help="没指定课程时含往年课程")
    x.add_argument("--type", nargs="+", choices=["exam", "homework"], default=["exam", "homework"],
                   help="监测类型（默认两者都查，包括以作业形式发布的小测）")
    x.add_argument("--due-within", type=int, default=30, metavar="MINUTES",
                   help="截止前多少分钟提醒一次（默认 30，0 = 关闭）")
    x.add_argument("--init", action="store_true", help="仅建立当前范围的记录，不输出现有项目的提醒")
    x.add_argument("--in-class", action="store_true",
                   help="只在今天课表的上课时段查，且只查正在上的课（给定时任务用）")
    x.add_argument("--margin", type=int, default=10, metavar="MIN", help="上课前后各放宽几分钟（默认 10）")
    x.add_argument("--json", action="store_true", help="输出事件数组；没有新提醒时为 []")
    x.set_defaults(fn=cmd_notify)

    x = sp.add_parser("activities", help="列出课程活动（课件／视频／作业／讨论／测验…）")
    x.add_argument("course", nargs="*", help="课程 id 或名称片段；省略 = 最新学年所有课程")
    x.add_argument("--all", action="store_true", help="没指定课程时含往年课程")
    x.add_argument("--type", nargs="*", metavar="T", help=f"只列这些类型：{', '.join(ACT_TYPES)}")
    x.add_argument("--json", action="store_true")
    x.set_defaults(fn=cmd_activities)

    x = sp.add_parser("show", help="单个活动详情（说明、附件、作业提交状态、讨论帖数）")
    x.add_argument("activity", type=int)
    x.add_argument("--read", action="store_true", help="下载附件并打印文字（docx/pptx/pdf/doc/txt）")
    x.set_defaults(fn=cmd_show)

    def body_args(x):
        x.add_argument("--body", help="内容（纯文本，空行分段）")
        x.add_argument("--body-file", help="从文件读内容；- = stdin")
        x.add_argument("--html", action="store_true", help="内容已经是 HTML，不转换")
        x.add_argument("--attach", nargs="*", metavar="FILE", help="附件")

    x = sp.add_parser("forum", help="讨论区：list / read / post / reply")
    fp = x.add_subparsers(dest="action", required=True)
    y = fp.add_parser("list", help="列出讨论帖（给讨论活动 id）")
    y.add_argument("id", type=int, help="讨论活动 id（activities --type forum 查）")
    y.add_argument("--mine", action="store_true", help="只看自己发的")
    y.add_argument("--full", action="store_true", help="连正文一起打印")
    y = fp.add_parser("read", help="读一个帖子及其回复")
    y.add_argument("id", type=int, help="topic id")
    y = fp.add_parser("post", help="发新帖")
    y.add_argument("id", type=int, help="讨论活动 id")
    y.add_argument("--title", required=True)
    body_args(y)
    y = fp.add_parser("reply", help="回帖")
    y.add_argument("id", type=int, help="topic id")
    body_args(y)
    x.set_defaults(fn=cmd_forum)

    x = sp.add_parser("upload", help="上传文件到学在浙大，打印 upload id")
    x.add_argument("files", nargs="+")
    x.set_defaults(fn=cmd_upload)

    x = sp.add_parser("submit", help="交作业（附件＋文字），默认提交前确认")
    x.add_argument("activity", type=int, help="作业活动 id（activities --type homework 查）")
    x.add_argument("--file", nargs="*", metavar="FILE", help="要交的文件")
    x.add_argument("--upload-id", type=int, nargs="*", help="已用 upload 命令传好的文件 id")
    x.add_argument("--body", help="作业文字内容")
    x.add_argument("--body-file", help="从文件读作业文字；- = stdin")
    x.add_argument("--html", action="store_true", help="文字已经是 HTML")
    x.add_argument("--draft", action="store_true", help="只存草稿不正式提交")
    x.add_argument("-y", "--yes", action="store_true", help="不确认直接提交")
    x.set_defaults(fn=cmd_submit)

    x = sp.add_parser("classroom", help="智云课堂：search / subs / day / add / rm / tracked")
    x.add_argument("action", choices=["search", "subs", "day", "add", "rm", "tracked"])
    x.add_argument("arg", nargs="?")
    x.add_argument("more", nargs="*", help=argparse.SUPPRESS)
    x.add_argument("--teacher")
    x.add_argument("--days", type=int)
    x.set_defaults(fn=cmd_classroom)

    x = sp.add_parser("rollcall", help="进行中的签到（只查看、不代签）")
    x.add_argument("--in-class", action="store_true", help="只在今天课表的上课时段才查（给定时任务用）")
    x.add_argument("--margin", type=int, default=10, metavar="MIN", help="上课前后各放宽几分钟（默认 10）")
    x.add_argument("--json", action="store_true")
    x.set_defaults(fn=cmd_rollcall)

    for name, fn in (("ppt", cmd_ppt), ("transcript", cmd_transcript)):
        x = sp.add_parser(name, help="智云 PPT → PDF" if name == "ppt" else "智云课堂语音转录")
        x.add_argument("--course", type=int, help="智云课堂 course_id（classroom search 查）")
        x.add_argument("--sub", type=int, nargs="*", help="只处理这些 sub_id")
        x.add_argument("--days", type=int, help="不给 --course 时：最近 N 天的课（默认 1 = 今天；含 classroom add 追踪的课）")
        x.add_argument("--force", action="store_true", help="已存在也重新下载")
        if name == "ppt":
            x.add_argument("--keep-images", action="store_true")
            x.add_argument("--dedup", action="store_true",
                           help="删掉重复截图（动画逐步出现、边讲边写、翻回前页），每页只留最完整的一张")
        else:
            x.add_argument("--format", choices=["txt", "srt", "md"], default="txt")
        x.set_defaults(fn=fn)

    a = p.parse_args()
    try:
        a.fn(a)
    except ZjuError as e:
        log(f"错误：{e}")
        sys.exit(1)
    except KeyboardInterrupt:
        sys.exit(130)


if __name__ == "__main__":
    main()
