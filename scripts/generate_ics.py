#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Notion → ICS 生成器（零第三方依赖，Python 3.8+ 标准库即可运行）


两种运行模式：
  
    云端模式   GitHub Actions / Cron 定时跑，生成后自动提交推送

配置优先级：命令行参数 > 环境变量 > .env 文件 > 内置默认值


"""

import argparse
import io
import json
import os
import re
import sys
import urllib.request
import urllib.error
from datetime import datetime, timedelta, timezone

NOTION_API = "https://api.notion.com/v1"
NOTION_VERSION = "2022-06-28"

DEFAULT_DB = ""   # 📅 每周训练表

# 中国全境无夏令时，固定 +08:00，避免依赖 tzdata（Windows Python 常缺 tzdata）
CST = timezone(timedelta(hours=8))
UTC = timezone.utc

# ---------- 动作名映射：次数基准里的简称 → 完整动作名 ----------
FULL_NAME = {
    "划船": "俯身哑铃划船", "单臂划船": "单臂哑铃划船", "反向飞鸟": "哑铃反向飞鸟",
    "弯举": "哑铃二头弯举", "卧推": "地板哑铃卧推", "俯卧撑": "俯卧撑（跪姿或标准）",
    "肩推": "哑铃站姿肩推", "侧平举": "哑铃侧平举", "臂屈伸": "哑铃过顶臂屈伸",
    "健腹轮靠墙": "健腹轮·靠墙", "健腹轮上斜": "健腹轮·上斜",
    "健腹轮跪姿半程": "健腹轮·跪姿半程", "健腹轮跪姿全程": "健腹轮·跪姿全程",
    "健腹轮": "健腹轮", "平板": "平板支撑", "转体": "俄罗斯转体", "侧平板": "侧平板支撑",
}
A_UPPER = ["地板哑铃卧推", "俯卧撑（跪姿或标准）", "哑铃站姿肩推", "哑铃侧平举", "哑铃过顶臂屈伸"]
B_UPPER = ["俯身哑铃划船", "单臂哑铃划船", "哑铃反向飞鸟", "哑铃二头弯举"]

WHEEL_TIP = {
    "靠墙": "靠墙推出：面向墙站立，双手握轮推向墙面，身体前倾后收回。腰一旦塌就立刻停止该组。",
    "上斜": "上斜推出：跪姿，把健腹轮推向抬高的平面（沙发/床沿/台阶），幅度比靠墙大。",
    "跪姿半程": "跪姿半程：只推到身体与地面约 45° 就收回。别贪远，全程腰不能塌。",
    "跪姿全程": "跪姿全程：躯干推到接近与地面平行，再靠腹部把自己拉回来。4 周内最高难度。",
}


# ---------------- 配置 ----------------
REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def load_dotenv():
    """本地模式：从仓库根目录 / 当前目录读取 .env（已存在的环境变量优先）。"""
    bases = [REPO_ROOT, os.getcwd()]
    for base in bases:
        p = os.path.join(base, ".env")
        if os.path.isfile(p):
            with io.open(p, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line or line.startswith("#") or "=" not in line:
                        continue
                    k, v = line.split("=", 1)
                    k = k.strip()
                    v = v.strip().strip('"').strip("'")
                    if k and k not in os.environ:
                        os.environ[k] = v
            return p
    return None


def env(k, default=""):
    return (os.environ.get(k) or "").strip() or default


def parse_hhmm(s, default=(20, 0)):
    try:
        parts = (s or "").split(":")
        return (int(parts[0]), int(parts[1]) if len(parts) > 1 else 0)
    except (ValueError, IndexError):
        return default


# ---------------- Notion 请求 ----------------
def notion_query(db_id, token):
    """拉取数据库全部页面（自动处理分页）。"""
    url = "{}/databases/{}/query".format(NOTION_API, db_id)
    headers = {
        "Authorization": "Bearer {}".format(token),
        "Notion-Version": NOTION_VERSION,
        "Content-Type": "application/json",
    }
    pages, cursor, guard = [], None, 0
    while True:
        body = {"page_size": 100}
        if cursor:
            body["start_cursor"] = cursor
        req = urllib.request.Request(
            url, data=json.dumps(body).encode("utf-8"), headers=headers, method="POST")
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        pages.extend(data.get("results", []))
        if not data.get("has_more") or guard > 50:
            break
        cursor = data.get("next_cursor")
        guard += 1
    return pages


def txt(prop):
    """把 Notion 的 title / rich_text / select / number 统一取成字符串。"""
    if not prop:
        return ""
    if "title" in prop:
        return "".join(r.get("plain_text", "") for r in prop["title"])
    if "rich_text" in prop:
        return "".join(r.get("plain_text", "") for r in prop["rich_text"])
    if "select" in prop and prop["select"]:
        return prop["select"].get("name", "")
    if "number" in prop and prop["number"] is not None:
        return str(prop["number"])
    return ""


# ---------------- 解析辅助 ----------------
def parse_duration(s):
    """'36-40（C课 25）' -> 40；'30' -> 30"""
    if not s:
        return 30
    m = re.search(r"(\d+)\s*[-–~]\s*(\d+)", s)
    if m:
        return int(m.group(2))
    m = re.search(r"(\d+)", s)
    return int(m.group(1)) if m else 30


def parse_sets(s):
    """'上肢 3 组 / 核心 3 组' -> 3"""
    if not s:
        return 2
    m = re.search(r"(\d+)\s*组", s)
    return int(m.group(1)) if m else 2


def build_exercise_list(kind, sets, reps_text):
    """
    生成当日动作清单。
    次数基准是详细版（含 · 分隔的动作与次数）就解析它，否则回落到课程模板。
    """
    if "·" in reps_text or "•" in reps_text:
        items = [x.strip() for x in re.split(r"\s*[·•|]\s*|\s*/\s*", reps_text) if x.strip()]
        out = []
        for it in items:
            m = re.match(r"^(.*?)\s*(\d+)\s*(每侧|秒|次)?", it)
            if not m:
                continue
            name = FULL_NAME.get(m.group(1).strip(), m.group(1).strip())
            out.append("{0} {1}×{2}{3}".format(name, sets, m.group(2), m.group(3) or ""))
        if len(out) >= 4:            # 解析成功才用它，否则走模板
            return out

    upper = A_UPPER if "A课" in kind else B_UPPER
    out = ["{0} {1}×12".format(a, sets) for a in upper]
    if "A课" in kind:
        out.append("平板支撑 {0}×30 秒".format(sets))
    else:
        out.append("俄罗斯转体 {0}×16".format(sets))
        out.append("侧平板支撑 {0}×20 秒".format(sets))
    return out


# ---------------- ICS 生成 ----------------
def fold(line):
    """RFC 5545 行折叠：单行 ≤75 字节，按字符边界切（不切断 UTF-8 汉字）。"""
    if len(line.encode("utf-8")) <= 73:
        return line
    out, cur, size, first = [], "", 0, True
    for ch in line:
        cb = len(ch.encode("utf-8"))
        limit = 73 if first else 72
        if size + cb > limit:
            out.append(("" if first else " ") + cur)
            cur, size, first = ch, cb, False
        else:
            cur += ch
            size += cb
    if cur:
        out.append(("" if first else " ") + cur)
    return "\r\n".join(out)


def esc(t):
    return (t.replace("\\", "\\\\").replace("\n", "\\n")
             .replace(";", "\\;").replace(",", "\\,"))


def stamp(dt):
    return dt.astimezone(UTC).strftime("%Y%m%dT%H%M%SZ")


def build_ics(sessions, train_time, protein_delay):
    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//WorkBuddy//Notion Fitness Plan//ZH",
        "CALSCALE:GREGORIAN",
        "METHOD:PUBLISH",
        fold("X-WR-CALNAME:🏋️ 新手增肌 · 腹部+上肢 4 周"),
        "X-WR-TIMEZONE:Asia/Shanghai",
        fold("X-WR-CALDESC:Notion 健身计划训练课，双重提醒：训练开始 + 练后补蛋白质"),
        # 提示客户端刷新间隔（iOS 不完全遵守，但写了无害）
        "X-PUBLISHED-TTL:PT12H",
        "REFRESH-INTERVAL;VALUE=DURATION:PT12H",
        # 以下多行块不能交给 fold()，否则内含的换行会被折断
        "BEGIN:VTIMEZONE", "TZID:Asia/Shanghai", "BEGIN:STANDARD",
        "DTSTART:19910915T000000", "TZOFFSETFROM:+0900", "TZOFFSETTO:+0800",
        "TZNAME:CST", "END:STANDARD", "END:VTIMEZONE",
    ]

    for s in sessions:
        y, m, d = map(int, s["date"].split("-"))
        start = datetime(y, m, d, train_time[0], train_time[1], tzinfo=CST)
        end = start + timedelta(minutes=s["dur"])

        body = [f"{s['week']} · {s['kind']}", "", "【今日动作清单】"]
        body.extend("· " + e for e in s["ex"])
        body += ["",
                 "组数基准：" + s["sets"],
                 "离心节奏：" + s["ecc"],
                 "目标强度：RIR " + s["rir"] + "（每组做完后还剩几次力竭）",
                 "健腹轮档位：" + s["wheel"],
                 "",
                 "【健腹轮要点】" + WHEEL_TIP.get(s["wheel"], "全程腰不能塌，塌了就退回上一档。"),
                 "",
                 "【练后】1 小时内补充蛋白质 30-40g，并在 Notion 打卡日志里记录实际组数与 RIR。",
                 "Notion 原页面：" + s["url"]]

        summary = "🏋️ " + s["title"]
        lines += [
            "BEGIN:VEVENT",
            "UID:{0}-fitness-workout".format(s["uid"]),
            "DTSTAMP:" + stamp(datetime.now(CST)),
            "DTSTART:" + stamp(start),
            "DTEND:" + stamp(end),
            fold("SUMMARY:" + esc(summary)),
            fold("DESCRIPTION:" + esc("\n".join(body))),
            "URL:" + s["url"],
            "CATEGORIES:健身,训练",
            "STATUS:CONFIRMED",
            "TRANSP:OPAQUE",
            "BEGIN:VALARM", "ACTION:DISPLAY", "TRIGGER:PT0M",
            fold("DESCRIPTION:🏋️ 训练时间到：" + summary),
            "END:VALARM",
            "BEGIN:VALARM", "ACTION:DISPLAY",
            "TRIGGER;RELATED=START:PT{0}M".format(s["dur"] + protein_delay),
            fold("DESCRIPTION:🥛 练完该补蛋白质了，1 小时内 30-40g（鸡蛋/牛奶/蛋白粉/鸡胸）"),
            "END:VALARM",
            "END:VEVENT",
        ]

    lines.append("END:VCALENDAR")
    return "\r\n".join(lines) + "\r\n"


# ---------------- 校验 ----------------
def verify(content):
    """生成后自检：事件数、提醒数、行长度、UID 唯一、时间区正确。"""
    problems = []
    raw = content.split("\r\n")
    n_ev = sum(1 for l in raw if l == "BEGIN:VEVENT")
    n_al = sum(1 for l in raw if l == "BEGIN:VALARM")
    if n_ev == 0:
        problems.append("没有生成任何 VEVENT")
    if n_al != n_ev * 2:
        problems.append("提醒数 {0} ≠ 事件数 {1} × 2".format(n_al, n_ev))
    long_lines = [i + 1 for i, l in enumerate(raw) if len(l.encode("utf-8")) > 75]
    if long_lines:
        problems.append("有 {0} 行超过 75 字节：行号 {1}".format(
            len(long_lines), long_lines[:5]))
    uids = [l[4:] for l in raw if l.startswith("UID:")]
    if len(uids) != len(set(uids)):
        problems.append("UID 有重复")
    if "BEGIN:VCALENDAR" not in raw or raw[-1] != "":
        problems.append("VCALENDAR 结构不完整")
    if "END:VCALENDAR" not in content:
        problems.append("缺少 END:VCALENDAR")
    return n_ev, n_al, problems




# ---------------- 主流程 ----------------
def main():
    ap = argparse.ArgumentParser(description="从 Notion 训练表生成可订阅的 .ics 日历")
    ap.add_argument("--token", help="Notion 集成密钥（也可设 NOTION_TOKEN）")
    ap.add_argument("--db", help="Notion 数据库 ID（也可设 NOTION_DATABASE_ID）")
    ap.add_argument("--out", help="输出文件路径（也可设 OUTFILE，默认 fitness.ics）")
    ap.add_argument("--time", help="训练开始时间 HH:MM（默认 20:00）")
    ap.add_argument("--delay", type=int, help="练后多久提醒补蛋白质（分钟，默认 60）")
    ap.add_argument("--no-verify", action="store_true", help="跳过生成后的格式自检")
    ap.add_argument("--mock", action="store_true",
                    help="不联网，用内置样例数据演练整条链路（验证脚本与托管是否正常）")
    args = ap.parse_args()

    env_file = load_dotenv()

    token = (args.token or env("NOTION_TOKEN")).strip()
    db_id = (args.db or env("NOTION_DATABASE_ID")).strip()

    # 输出路径：相对路径一律以仓库根目录为基准，
    # 这样无论从哪个目录调用（Actions / 双击 / 终端），都稳定写到仓库里
    outfile = (args.out or env("OUTFILE", "fitness.ics")).strip()
    if not os.path.isabs(outfile):
        outfile = os.path.join(REPO_ROOT, outfile)
    train_time = parse_hhmm(args.time or env("TRAIN_TIME", "20:00"))
    try:
        protein_delay = args.delay if args.delay is not None else \
            int(env("PROTEIN_DELAY_MIN", "60"))
    except ValueError:
        protein_delay = 60

    if args.mock:
        print("🔬 离线演练模式（--mock）：不联网，用内置样例数据跑通整条链路。")
        sessions = sorted(MOCK, key=lambda x: x["date"])
    else:
        if not token:
            print("⚠️  未配置 NOTION_TOKEN，跳过生成，保留现有 {}。".format(
                os.path.basename(outfile)))
            print("   本地模式：复制 .env.example 为 .env，填入密钥后重跑")
            print("   云端模式：在仓库 Settings → Secrets 里添加 NOTION_TOKEN")
            print("   只想先验证脚本？加 --mock 参数离线演练")
            return 0

        try:
            pages = notion_query(db_id, token)
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", "ignore")[:300]
            print("❌ Notion API 返回 {}：{}".format(e.code, detail))
            if e.code in (401, 403):
                print("   密钥无效，或该数据库没有授权给集成：")
                print("   Notion 打开数据库 → 右上角 ··· → 连接 → 搜索并选中你的集成")
            elif e.code == 404:
                print("   数据库 ID 不对，或集成没被授权（未授权时 Notion 也返回 404）")
            return 1
        except Exception as e:
            print("❌ 请求 Notion 失败：{}".format(e))
            return 1

        sessions = []
        for p in pages:
            pr = p.get("properties", {})
            date = (pr.get("日期", {}).get("date") or {}).get("start")
            if not date:
                continue
            kind = txt(pr.get("课程")) or "训练"
            sets_txt = txt(pr.get("组数基准"))
            sessions.append(dict(
                date=date.split("T")[0],
                title=txt(pr.get("训练课")) or "训练",
                kind=kind,
                week=txt(pr.get("第几周")) or "",
                sets=sets_txt,
                ecc=txt(pr.get("离心秒数")) or "2-3 秒",
                rir=txt(pr.get("目标RIR")) or "2-3",
                wheel=txt(pr.get("健腹轮阶段")) or "靠墙",
                dur=parse_duration(txt(pr.get("预计时长(分钟)"))),
                ex=build_exercise_list(kind, parse_sets(sets_txt), txt(pr.get("次数基准"))),
                url=p.get("url", ""),
                uid=(p.get("id", "") or "").replace("-", ""),
            ))

        sessions.sort(key=lambda x: x["date"])

    if not sessions:
        print("⚠️  Notion 里没有带日期的训练课，保留现有 {} 不覆盖。".format(
            os.path.basename(outfile)))
        return 0

    # 演练模式别把真实日历覆盖掉
    if args.mock and os.path.basename(outfile) == "fitness.ics" and os.path.isfile(outfile):
        outfile = os.path.join(os.path.dirname(outfile), "fitness.mock.ics")
        print("   检测到已有 fitness.ics，演练输出改写到 "
              + os.path.basename(outfile) + "，不覆盖真实日历")

    content = build_ics(sessions, train_time, protein_delay)

    # 兜底：自检发现严重问题时，宁可不覆盖，也别把日历写坏
    n_ev, n_al, problems = verify(content)
    if problems:
        print("❌ 生成的 ics 未通过自检，已放弃写入（保留旧文件）：")
        for p in problems:
            print("   · " + p)
        return 1

    with io.open(outfile, "w", encoding="utf-8", newline="") as f:
        f.write(content)

    print("✅ 已写入 {}".format(os.path.basename(outfile)))
    print("   训练日程 {} 个 · 提醒 {} 个 · 训练时间 {:02d}:{:02d}".format(
        n_ev, n_al, train_time[0], train_time[1]))
    print("   日期范围：{} → {}".format(sessions[0]["date"], sessions[-1]["date"]))
    if env_file:
        print("   配置来源：{}".format(env_file))
    if not args.no_verify:
        print("   格式自检：通过（RFC 5545 行折叠 / UID 唯一 / 提醒配对）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
