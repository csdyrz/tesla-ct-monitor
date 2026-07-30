#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
特斯拉官方库存监控 —— Cybertruck 上新微信提醒

功能:
  抓取 tesla.com 官方库存(全新车 / Cybertruck / 邮编 92614 / 全部可交付范围),
  排除 Cyberbeast(野兽版)与 Long Range 后驱(丐版),只保留 AWD 全轮驱动等其余版本;
  用脱敏 VIN 去重,发现新上架车辆时通过 Server酱 / PushPlus / 企业微信机器人 推送到微信。

用法:
  python tesla_ct_monitor.py                # 单次检查(GitHub Actions 用这个)
  python tesla_ct_monitor.py --loop 60      # 本地常驻:每约 60 秒检查一次
  python tesla_ct_monitor.py --test-notify  # 发一条测试消息验证微信通道
  python tesla_ct_monitor.py --dry-run      # 只打印不推送、不写状态(调试用)
  python tesla_ct_monitor.py --headed       # 显示浏览器窗口(排障用)

配置(环境变量,或与脚本同目录的 config.json,环境变量优先):
  SERVERCHAN_SENDKEY   Server酱 SendKey(sct.ftqq.com,扫码即得,推荐)
  PUSHPLUS_TOKEN       PushPlus token(pushplus.plus,免费额度更大)
  WECOM_WEBHOOK        企业微信群机器人 webhook 完整地址
  ZIP_CODE             搜索邮编,默认 92614
  EXCLUDE_DEMO         设为 1 时排除展车(默认 0:展车也提醒,消息里会标注)
  RESURFACE_DAYS       同一辆车从库存消失超过 N 天后重新出现时再次提醒,默认 3
  FAIL_ALERT_THRESHOLD 连续失败 N 次后发一条告警(24 小时内不重复),默认 10
  FETCH_MODES          抓取模式尝试顺序,默认 "headless"(云端建议 "headless,virtual")
  STATE_FILE           去重状态文件路径,默认 state/seen_vins.json
"""

from __future__ import annotations

import argparse
import json
import os
import random
import re
import sys
import time
import urllib.parse
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

BASE_DIR = Path(__file__).resolve().parent
INVENTORY_PAGE = "https://www.tesla.com/inventory/new/ct?arrangeby=plh&zip={zip}&range=0"
API_TEMPLATE = "https://www.tesla.com/inventory/api/v4/inventory-results?query={query}"
PAGE_SIZE = 50
MAX_OFFSET = 300          # 翻页保险上限(Cybertruck 全国库存远小于此)
MESSAGE_CAR_CAP = 10      # 单条推送最多列出的车辆数
STATE_PRUNE_DAYS = 90     # 状态文件里超过 90 天没再见到的车辆记录被清理
PAGE_REFRESH_SECONDS = 25 * 60  # 常驻模式下浏览器页面定期刷新(保持 Akamai cookie 新鲜)

WHEEL_NAMES = {"CORE_WHEELS": '20" Core 轮毂', "CYBER_WHEELS": '20" Cyber 轮毂'}
INTERIOR_NAMES = {"WHITE": "白色内饰", "GREY": "灰色内饰", "GRAY": "灰色内饰", "BLACK": "黑色内饰"}


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def la_time_str(dt: datetime | None = None) -> str:
    try:
        from zoneinfo import ZoneInfo

        local = (dt or now_utc()).astimezone(ZoneInfo("America/Los_Angeles"))
        return local.strftime("%Y-%m-%d %H:%M") + "(尔湾当地时间)"
    except Exception:
        return (dt or now_utc()).strftime("%Y-%m-%d %H:%M UTC")


def log(msg: str) -> None:
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)


# ---------------------------------------------------------------------------
# 配置
# ---------------------------------------------------------------------------

def load_config() -> dict:
    cfg: dict = {}
    cfg_file = BASE_DIR / "config.json"
    if cfg_file.exists():
        try:
            raw = json.loads(cfg_file.read_text(encoding="utf-8"))
            cfg = {str(k).upper(): v for k, v in raw.items() if v not in (None, "")}
        except Exception as e:
            log(f"⚠️ config.json 解析失败,忽略: {e}")
    for key in (
        "SERVERCHAN_SENDKEY",
        "PUSHPLUS_TOKEN",
        "WECOM_WEBHOOK",
        "ZIP_CODE",
        "EXCLUDE_DEMO",
        "RESURFACE_DAYS",
        "FAIL_ALERT_THRESHOLD",
        "FETCH_MODES",
        "STATE_FILE",
    ):
        val = os.environ.get(key)
        if val not in (None, ""):
            cfg[key] = val
    cfg.setdefault("ZIP_CODE", "92614")
    cfg.setdefault("EXCLUDE_DEMO", "0")
    cfg.setdefault("RESURFACE_DAYS", "3")
    cfg.setdefault("FAIL_ALERT_THRESHOLD", "10")
    cfg.setdefault("FETCH_MODES", "headless")
    cfg.setdefault("STATE_FILE", str(BASE_DIR / "state" / "seen_vins.json"))
    # 密钥经由 Secrets/管道注入时可能混入尾随换行或空格,一律清理
    return {k: (v.strip() if isinstance(v, str) else v) for k, v in cfg.items()}


# ---------------------------------------------------------------------------
# 抓取(camoufox 反检测浏览器,页面上下文内调官方库存 API)
# ---------------------------------------------------------------------------

class FetchError(Exception):
    pass


def build_query(zip_code: str, offset: int, count: int = PAGE_SIZE) -> str:
    payload = {
        "query": {
            "model": "ct",
            "condition": "new",
            "options": {},
            "arrangeby": "Price",
            "order": "asc",
            "market": "US",
            "language": "en",
            "super_region": "north america",
            "zip": zip_code,
            "range": 0,  # range=0 即官网“All deliverable(全部可交付)”搜索范围
        },
        "offset": offset,
        "count": count,
        "outsideOffset": 0,
        "outsideSearch": False,
    }
    return API_TEMPLATE.format(query=urllib.parse.quote(json.dumps(payload)))


class InventoryFetcher:
    """维护一个 camoufox 浏览器实例;页面加载时由浏览器自动通过 Akamai 挑战,
    之后在页面上下文里 fetch 官方库存 API 拿 JSON。"""

    def __init__(self, zip_code: str, modes: list[str], headed: bool = False):
        self.zip_code = zip_code
        self.modes = modes or ["headless"]
        self.headed = headed
        self._cm = None
        self._browser = None
        self._page = None
        self._page_loaded_at = 0.0

    # -- 浏览器生命周期 --------------------------------------------------
    def _launch(self, mode: str):
        from camoufox.sync_api import Camoufox

        headless: object
        if self.headed:
            headless = False
        elif mode == "virtual":
            headless = "virtual"
        else:
            headless = True
        log(f"🚀 启动 camoufox(mode={mode}, headless={headless})...")
        self._cm = Camoufox(headless=headless, geoip=True, humanize=True)
        self._browser = self._cm.__enter__()
        self._page = self._browser.new_page()

    def close(self) -> None:
        if self._cm is not None:
            try:
                self._cm.__exit__(None, None, None)
            except Exception:
                pass
        self._cm = self._browser = self._page = None
        self._page_loaded_at = 0.0

    def _load_inventory_page(self) -> None:
        url = INVENTORY_PAGE.format(zip=self.zip_code)
        self._page.goto(url, wait_until="domcontentloaded", timeout=90_000)
        self._page.wait_for_timeout(8_000)  # 留时间让 Akamai 挑战脚本自动完成
        title = self._page.title() or ""
        if "denied" in title.lower():
            raise FetchError(f"库存页被拦截(标题: {title!r})")
        self._page_loaded_at = time.monotonic()

    def _ensure_ready(self, mode: str) -> None:
        if self._page is None:
            self._launch(mode)
            self._load_inventory_page()
        elif time.monotonic() - self._page_loaded_at > PAGE_REFRESH_SECONDS:
            log("🔄 定期刷新库存页(保持会话新鲜)...")
            self._load_inventory_page()

    # -- 数据抓取 --------------------------------------------------------
    def _fetch_json(self, url: str) -> dict:
        result = self._page.evaluate(
            """async (url) => {
                try {
                    const r = await fetch(url, {headers: {accept: 'application/json'}});
                    return {status: r.status, text: await r.text()};
                } catch (e) {
                    return {status: -1, text: String(e)};
                }
            }""",
            url,
        )
        status, text = result.get("status"), result.get("text") or ""
        if status != 200:
            raise FetchError(f"API HTTP {status}: {text[:120]!r}")
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            raise FetchError(f"API 返回非 JSON: {text[:120]!r}")
        if "results" not in data:
            raise FetchError(f"API 返回异常结构(疑似风控挑战): {text[:120]!r}")
        return data

    @staticmethod
    def _extract_vehicles(data: dict) -> list[dict]:
        results = data.get("results")
        if isinstance(results, list):
            return [v for v in results if isinstance(v, dict)]
        if isinstance(results, dict):  # 某些地区返回 {exact: [], approximate: [], ...}
            merged: list[dict] = []
            for val in results.values():
                if isinstance(val, list):
                    merged.extend(v for v in val if isinstance(v, dict))
            return merged
        return []

    def _fetch_all_once(self) -> list[dict]:
        vehicles: dict[str, dict] = {}
        offset, total = 0, None
        while True:
            data = self._fetch_json(build_query(self.zip_code, offset))
            try:
                total = int(data.get("total_matches_found") or 0)
            except (TypeError, ValueError):
                total = 0
            batch = self._extract_vehicles(data)
            before = len(vehicles)
            for v in batch:
                vehicles[vehicle_key(v)] = v
            if (
                not batch
                or len(vehicles) >= total
                or len(vehicles) == before
                or offset + PAGE_SIZE >= MAX_OFFSET
            ):
                break
            offset += PAGE_SIZE
        log(f"📦 官方库存返回 {len(vehicles)} 辆(total_matches_found={total})")
        return list(vehicles.values())

    def fetch_all(self) -> list[dict]:
        """带重试与模式降级的完整抓取。任一模式成功即返回。"""
        last_err: Exception | None = None
        for mode in self.modes:
            for attempt in (1, 2):
                try:
                    self._ensure_ready(mode)
                    return self._fetch_all_once()
                except Exception as e:
                    last_err = e
                    log(f"⚠️ 抓取失败(mode={mode} 第{attempt}次): {e}")
                    try:
                        if self._page is not None and attempt == 1:
                            self._load_inventory_page()  # 先试重载页面
                            continue
                    except Exception as e2:
                        last_err = e2
                    self.close()  # 重载也不行 → 换新浏览器/下一模式
            self.close()
        raise FetchError(f"所有抓取方式均失败: {last_err}")


def vehicle_key(v: dict) -> str:
    key = v.get("VIN") or v.get("Hash")
    if key:
        return str(key)
    basis = json.dumps(
        [v.get("TrimCode"), v.get("OptionCodeList"), v.get("Price"), v.get("Odometer")],
        sort_keys=True,
    )
    import hashlib

    return "nokey_" + hashlib.sha256(basis.encode()).hexdigest()[:20]


# ---------------------------------------------------------------------------
# 版本过滤:只要 Cybertruck,排除 Cyberbeast(野兽版)与 Long Range 后驱(丐版)
# ---------------------------------------------------------------------------

# 注意:"CYBERTRUCK" 本身含有 "CYB"/"CYBER" 字样,野兽版必须用精确 token 匹配,
# 不能用裸 "CYB" 子串(否则 "Cybertruck All-Wheel Drive" 会被误杀)。
_BEAST_PAT = re.compile(r"CT_CYB|CYBERBEAST|\bBEAST\b|\$MTC04")
_AWD_PAT = re.compile(r"CT_AWD|_AWD|\bAWD\b|ALL[- ]WHEEL")
_BASE_PAT = re.compile(r"\bRWD\b|REAR[- ]WHEEL|CT_LR|LONG[ _]RANGE|STANDARD[ _]RANGE")


def classify_vehicle(v: dict) -> tuple[str, str]:
    """返回 (决策, 说明)。决策: include / include_unknown / exclude_beast / exclude_base"""
    trim_codes = " ".join(v.get("TRIM") or []) + " " + str(v.get("TrimCode") or "")
    trim_name = str(v.get("TrimName") or "")
    text = f"{trim_codes} {trim_name}".upper()

    if _BEAST_PAT.search(text):
        return "exclude_beast", trim_name or trim_codes
    if _AWD_PAT.search(text):  # AWD 判定先于丐版:未来若出现 "Long Range AWD" 应算 AWD
        return "include", trim_name or trim_codes
    if _BASE_PAT.search(text):
        return "exclude_base", trim_name or trim_codes
    # 未知新版本:宁可多报也不漏报,消息里会带【未知版本】标签
    return "include_unknown", trim_name or trim_codes or "未知"


# ---------------------------------------------------------------------------
# 车辆信息整理与消息排版
# ---------------------------------------------------------------------------

def summarize_vehicle(v: dict, zip_code: str, unknown: bool = False) -> dict:
    price = v.get("InventoryPrice") or v.get("PurchasePrice") or v.get("Price")
    total = v.get("TotalPrice")
    discount = v.get("Discount") or 0
    vin = str(v.get("VIN") or v.get("Hash") or "")
    order_url = (
        f"https://www.tesla.com/ct/order/{urllib.parse.quote(vin)}?postal={zip_code}&range=0"
        if vin
        else INVENTORY_PAGE.format(zip=zip_code)
    )
    wheels = ", ".join(WHEEL_NAMES.get(w, w) for w in (v.get("WHEELS") or []))
    interior = ", ".join(INTERIOR_NAMES.get(i, i) for i in (v.get("INTERIOR") or []))
    delivery = v.get("DeliveryDateDisplay")
    if not isinstance(delivery, str) or delivery.strip().lower() in ("", "true", "false"):
        delivery = ""  # 该字段有时是布尔开关而非日期文本,布尔值没有展示意义
    return {
        "key": vehicle_key(v),
        "trim": str(v.get("TrimName") or "Cybertruck"),
        "unknown": unknown,
        "year": v.get("Year"),
        "price": price,
        "total": total,
        "discount": discount,
        "demo": bool(v.get("IsDemo")),
        "odometer": v.get("Odometer"),
        "odo_unit": str(v.get("OdometerType") or "Miles"),
        "wheels": wheels,
        "interior": interior,
        "in_transit": bool(v.get("InTransit") or v.get("IsInTransit")),
        "delivery": delivery,
        "url": order_url,
    }


def _fmt_money(x) -> str:
    try:
        return f"${int(x):,}"
    except (TypeError, ValueError):
        return "价格未知"


def format_message(cars: list[dict], zip_code: str, resurfaced_keys: set[str]) -> tuple[str, str]:
    n_new = sum(1 for c in cars if c["key"] not in resurfaced_keys)
    n_back = len(cars) - n_new
    title_parts = []
    if n_new:
        title_parts.append(f"上新{n_new}辆")
    if n_back:
        title_parts.append(f"回补{n_back}辆")
    title = f"🛻 Cybertruck {'、'.join(title_parts)}"

    lines = [
        f"# 🛻 Cybertruck 库存提醒(邮编 {zip_code},全部可交付范围)",
        "",
        f"共 **{len(cars)}** 辆符合条件(已排除野兽版 Cyberbeast 和后驱丐版):",
        "",
    ]
    for i, c in enumerate(cars[:MESSAGE_CAR_CAP], 1):
        year = f"{c['year']} 款 " if c.get("year") else ""
        tags = []
        if c["key"] in resurfaced_keys:
            tags.append("🔁 重新上架")
        if c["unknown"]:
            tags.append("❓ 未知版本(请自行确认)")
        if c["demo"]:
            odo = c.get("odometer")
            odo_txt = f",已行驶 {int(odo):,} {c['odo_unit']}" if odo else ""
            tags.append(f"🚗 展车{odo_txt}")
        if c["in_transit"]:
            tags.append("🚛 在途")
        lines.append(f"### {i}. {year}{c['trim']} — {_fmt_money(c['price'])}")
        if c["discount"]:
            lines.append(f"- 💰 官方直降 {_fmt_money(c['discount'])}(原价 {_fmt_money(c['total'])})")
        spec = " · ".join(x for x in (c["interior"], c["wheels"]) if x)
        if spec:
            lines.append(f"- 🎨 {spec}")
        if c["delivery"]:
            lines.append(f"- 📅 交付信息: {c['delivery']}")
        for t in tags:
            lines.append(f"- {t}")
        lines.append(f"- 👉 [点此进入官网下单页]({c['url']})")
        lines.append("")
    if len(cars) > MESSAGE_CAR_CAP:
        lines.append(f"……另有 {len(cars) - MESSAGE_CAR_CAP} 辆未逐一列出。")
    lines.append(
        f"[查看全部库存]({INVENTORY_PAGE.format(zip=zip_code)}) · 检查时间 {la_time_str()}"
    )
    return title, "\n".join(lines)


# ---------------------------------------------------------------------------
# 微信推送(Server酱 / PushPlus / 企业微信机器人,配了哪个走哪个)
# ---------------------------------------------------------------------------

def _post_with_retry(fn, channel: str) -> tuple[str, bool, str]:
    last = ""
    for attempt in (1, 2):
        try:
            ok, detail = fn()
            if ok:
                return channel, True, detail
            last = detail
        except Exception as e:
            last = f"{type(e).__name__}: {e}"
        if attempt == 1:
            time.sleep(2)
    return channel, False, last


def send_serverchan(key: str, title: str, md: str) -> tuple[bool, str]:
    r = requests.post(
        f"https://sctapi.ftqq.com/{key}.send",
        data={"title": title[:32], "desp": md},
        timeout=20,
    )
    j = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
    ok = r.status_code == 200 and j.get("code") == 0
    return ok, f"HTTP {r.status_code} {str(j)[:120]}"


def send_pushplus(token: str, title: str, md: str) -> tuple[bool, str]:
    r = requests.post(
        "https://www.pushplus.plus/send",
        json={"token": token, "title": title, "content": md, "template": "markdown"},
        timeout=20,
    )
    j = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
    ok = r.status_code == 200 and j.get("code") == 200
    return ok, f"HTTP {r.status_code} {str(j)[:120]}"


def send_wecom(webhook: str, title: str, md: str) -> tuple[bool, str]:
    content = f"**{title}**\n\n{md}"
    if len(content.encode("utf-8")) > 3800:  # 企业微信 markdown 上限 4096 字节
        content = content.encode("utf-8")[:3700].decode("utf-8", errors="ignore") + "\n\n(内容过长已截断)"
    r = requests.post(webhook, json={"msgtype": "markdown", "markdown": {"content": content}}, timeout=20)
    j = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
    ok = r.status_code == 200 and j.get("errcode") == 0
    return ok, f"HTTP {r.status_code} {str(j)[:120]}"


def notify_all(cfg: dict, title: str, md: str) -> tuple[bool, list[tuple[str, bool, str]]]:
    jobs = []
    if cfg.get("SERVERCHAN_SENDKEY"):
        jobs.append(("Server酱", lambda: send_serverchan(cfg["SERVERCHAN_SENDKEY"], title, md)))
    if cfg.get("PUSHPLUS_TOKEN"):
        jobs.append(("PushPlus", lambda: send_pushplus(cfg["PUSHPLUS_TOKEN"], title, md)))
    if cfg.get("WECOM_WEBHOOK"):
        jobs.append(("企业微信", lambda: send_wecom(cfg["WECOM_WEBHOOK"], title, md)))
    if not jobs:
        log("⚠️ 未配置任何微信推送通道(SERVERCHAN_SENDKEY / PUSHPLUS_TOKEN / WECOM_WEBHOOK)")
        return False, []
    results = [_post_with_retry(fn, name) for name, fn in jobs]
    for name, ok, detail in results:
        log(f"{'✅' if ok else '❌'} {name} 推送{'成功' if ok else '失败'}: {detail}")
    return any(ok for _, ok, _ in results), results


# ---------------------------------------------------------------------------
# 去重状态
# ---------------------------------------------------------------------------

def load_state(path: Path) -> dict:
    if path.exists():
        try:
            state = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(state, dict) and isinstance(state.get("vins"), dict):
                return state
        except Exception as e:
            log(f"⚠️ 状态文件损坏,重建: {e}")
            try:
                path.rename(path.with_suffix(".corrupt.json"))
            except Exception:
                pass
    return {
        "version": 1,
        "vins": {},
        "consecutive_failures": 0,
        "consecutive_notify_failures": 0,
        "last_failure_alert": None,
    }


def save_state(path: Path, state: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(path)


def _parse_ts(s) -> datetime | None:
    try:
        return datetime.fromisoformat(str(s))
    except (TypeError, ValueError):
        return None


def diff_against_state(
    summaries: list[dict], state: dict, resurface_days: float
) -> tuple[list[dict], set[str]]:
    """返回 (需要提醒的车辆列表, 其中属于“重新上架”的 key 集合),并更新 last_seen。"""
    now = now_utc()
    vins: dict = state["vins"]
    to_notify: list[dict] = []
    resurfaced: set[str] = set()
    for c in summaries:
        rec = vins.get(c["key"])
        if rec is None:
            to_notify.append(c)
        else:
            last_seen = _parse_ts(rec.get("last_seen"))
            if last_seen and (now - last_seen) > timedelta(days=resurface_days):
                # 回补车这里不动 last_seen:若本轮推送失败,下一轮仍能按“回补”重试;
                # 推送成功后由 commit_notified 统一刷新。
                to_notify.append(c)
                resurfaced.add(c["key"])
            else:
                rec["last_seen"] = now.isoformat()
                rec["price"] = c["price"]
    # 清理超过 STATE_PRUNE_DAYS 没见到的旧记录
    cutoff = now - timedelta(days=STATE_PRUNE_DAYS)
    for key in [k for k, r in vins.items() if (_parse_ts(r.get("last_seen")) or now) < cutoff]:
        del vins[key]
    return to_notify, resurfaced


def commit_notified(state: dict, cars: list[dict]) -> None:
    now = now_utc().isoformat()
    for c in cars:
        rec = state["vins"].setdefault(c["key"], {"first_seen": now})
        rec.update({"last_seen": now, "last_notified": now, "trim": c["trim"], "price": c["price"]})


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------

def run_once(fetcher: InventoryFetcher, cfg: dict, dry_run: bool = False) -> bool:
    """执行一轮检查。返回 True 表示本轮抓取成功(无论有没有新车)。"""
    state_path = Path(cfg["STATE_FILE"])
    state = load_state(state_path)
    resurface_days = float(cfg["RESURFACE_DAYS"])
    fail_threshold = int(float(cfg["FAIL_ALERT_THRESHOLD"]))

    try:
        vehicles = fetcher.fetch_all()
    except Exception as e:
        state["consecutive_failures"] = int(state.get("consecutive_failures") or 0) + 1
        n = state["consecutive_failures"]
        log(f"❌ 本轮抓取失败(连续第 {n} 次): {e}")
        last_alert = _parse_ts(state.get("last_failure_alert"))
        if n >= fail_threshold and (
            last_alert is None or now_utc() - last_alert > timedelta(hours=24)
        ):
            title = "⚠️ 特斯拉库存监控连续失败"
            md = (
                f"监控已连续失败 **{n}** 次,最近错误:\n\n```\n{str(e)[:400]}\n```\n\n"
                "可能原因:Akamai 风控升级、运行环境 IP 被拉黑、特斯拉接口变更。\n\n"
                f"排查时间 {la_time_str()}"
            )
            ok, _ = notify_all(cfg, title, md)
            if ok:
                state["last_failure_alert"] = now_utc().isoformat()
        if not dry_run:
            save_state(state_path, state)
        return False

    state["consecutive_failures"] = 0

    # 过滤版本
    kept: list[dict] = []
    counts = {"exclude_beast": 0, "exclude_base": 0, "demo_skipped": 0}
    exclude_demo = str(cfg["EXCLUDE_DEMO"]) == "1"
    for v in vehicles:
        decision, label = classify_vehicle(v)
        if decision in ("exclude_beast", "exclude_base"):
            counts[decision] += 1
            continue
        if exclude_demo and v.get("IsDemo"):
            counts["demo_skipped"] += 1
            continue
        kept.append(summarize_vehicle(v, cfg["ZIP_CODE"], unknown=(decision == "include_unknown")))
    log(
        f"🔎 符合条件 {len(kept)} 辆(排除野兽版 {counts['exclude_beast']}、"
        f"丐版 {counts['exclude_base']}、跳过展车 {counts['demo_skipped']})"
    )

    to_notify, resurfaced = diff_against_state(kept, state, resurface_days)

    if not to_notify:
        log("😴 没有新上架车辆")
        if not dry_run:
            save_state(state_path, state)
        return True

    title, md = format_message(to_notify, cfg["ZIP_CODE"], resurfaced)
    for c in to_notify:
        tag = "🔁重新上架" if c["key"] in resurfaced else "🆕新上架"
        log(f"{tag}: {c['trim']} {_fmt_money(c['price'])} ({c['key'][:24]}...)")
    if dry_run:
        log("🧪 dry-run:只打印,不推送、不写状态。消息内容如下:\n" + md)
        return True

    sent, results = notify_all(cfg, title, md)
    if results:
        # 推送通道持续全败时累计计数,让 Actions 亮红灯(GitHub 会邮件提醒),
        # 否则通道坏掉用户会毫无感知地漏车
        state["consecutive_notify_failures"] = (
            0 if sent else int(state.get("consecutive_notify_failures") or 0) + 1
        )
    if sent or not results:
        # 推送成功(或压根没配通道)才把车辆记为已提醒;
        # 配了通道但全部失败 → 不落状态,下一轮重试,避免静默漏报。
        commit_notified(state, to_notify)
    else:
        log("⚠️ 所有推送通道都失败,本轮不落盘,下一轮将重试提醒")
    save_state(state_path, state)
    return True


def main() -> int:
    parser = argparse.ArgumentParser(description="特斯拉 Cybertruck 官方库存监控(微信提醒)")
    parser.add_argument("--loop", nargs="?", const=60, type=int, metavar="秒",
                        help="常驻模式:每 N 秒检查一次(默认 60)")
    parser.add_argument("--max-runs", type=int, default=0, help="常驻模式最多跑几轮(0=不限,测试用)")
    parser.add_argument("--dry-run", action="store_true", help="只打印不推送、不写状态")
    parser.add_argument("--test-notify", action="store_true", help="发送一条测试消息后退出")
    parser.add_argument("--headed", action="store_true", help="显示浏览器窗口(排障)")
    args = parser.parse_args()

    cfg = load_config()

    if args.test_notify:
        title = "🛻 特斯拉监控测试消息"
        md = (
            f"如果你在微信收到这条消息,说明通知链路配置成功 ✅\n\n"
            f"- 监控目标:Cybertruck(排除野兽版/丐版)\n"
            f"- 邮编:{cfg['ZIP_CODE']}(全部可交付范围)\n\n发送时间 {la_time_str()}"
        )
        ok, results = notify_all(cfg, title, md)
        if not results:
            log("❌ 没有配置任何通道,请先设置 SERVERCHAN_SENDKEY(或 PUSHPLUS_TOKEN / WECOM_WEBHOOK)")
        return 0 if ok else 1

    modes = [m.strip() for m in str(cfg["FETCH_MODES"]).split(",") if m.strip()]
    fetcher = InventoryFetcher(cfg["ZIP_CODE"], modes, headed=args.headed)

    def exit_code() -> int:
        # 偶发失败返回 0(下轮自愈);抓取或推送持续失败返回 1 让 Actions 亮红灯
        state = load_state(Path(cfg["STATE_FILE"]))
        fails = max(
            int(state.get("consecutive_failures") or 0),
            int(state.get("consecutive_notify_failures") or 0),
        )
        return 1 if fails >= int(float(cfg["FAIL_ALERT_THRESHOLD"])) else 0

    try:
        if args.loop is None:
            run_once(fetcher, cfg, dry_run=args.dry_run)
            return exit_code()

        interval = max(20, args.loop)
        log(f"🕐 常驻模式启动:约每 {interval} 秒检查一次(Ctrl+C 退出)")
        runs = 0
        while True:
            run_once(fetcher, cfg, dry_run=args.dry_run)
            runs += 1
            if args.max_runs and runs >= args.max_runs:
                log(f"到达 --max-runs={args.max_runs},退出")
                return exit_code()
            sleep_s = interval * random.uniform(0.85, 1.2)
            time.sleep(sleep_s)
    except KeyboardInterrupt:
        log("👋 收到退出信号")
        return 0
    finally:
        fetcher.close()


if __name__ == "__main__":
    sys.exit(main())
