# -*- coding: utf-8 -*-
"""本地保活：别让 GitHub 把定时任务悄悄关掉。

GitHub 的规矩：仓库连续 **60 天**没有任何提交，就把仓库里所有定时任务（schedule）
静默禁用——不报错、不弹窗，只是从此不再触发。仓库里已经有防线（每轮心跳提交、
有提交就自动叫醒），但那些防线都在"仓库内部"，万一整个被关掉，就得从外面拉一把。
这个脚本就是那只手：在你自己的电脑上跑，用你自己的 GitHub 账号。

它做三件事（都是幂等的，随便重复跑）：

  1. 看 daily.yml 的 state。是 disabled_inactivity 就用 API 重新启用。
  2. 看最后一次提交距今多少天。超过 --max-age（默认 25 天）就用 Contents API
     补一个提交——不动你本地那份 git 仓库，也不用管本地有没有未提交的改动。
  3. 顺手汇报最近几次运行结果，便于发现"任务在跑、但一直失败"这种情况。

token 从 Git Credential Manager 里取（就是你之前授权 GitHub 时存的那份），
只在本进程内存里用，不落盘、不打印。

用法（在哪个目录跑都行）：
    python keepalive_local.py                 # 检查 + 该修就修
    python keepalive_local.py --dry-run       # 只看，不动
    python keepalive_local.py --max-age 40    # 改"多久算太久"的天数
    python keepalive_local.py --repo a/b      # 换个仓库
"""

import argparse
import base64
import datetime
import json
import os
import subprocess
import sys
import urllib.error
import urllib.request

REPO = os.getenv("GK_REPO", "Minalinsk/gk-daily-push")
API = "https://api.github.com"
WATCH_WORKFLOW = "daily.yml"
KEEPALIVE_PATH = "state/keepalive.txt"

# 这台机器上的 PortableGit（PATH 里不一定有 git）
GIT_CANDIDATES = [
    r"C:\Users\SanSan\.workbuddy\binaries\PortableGit\versions\1.2.0\cmd\git.exe",
    "git",
]


def log(msg=""):
    print(msg, flush=True)


def find_git():
    for cand in GIT_CANDIDATES:
        if cand == "git" or os.path.exists(cand):
            return cand
    return "git"


def get_token():
    """从 GCM 取 github.com 的 token（不打印、不落盘）。"""
    git = find_git()
    proc = subprocess.run(
        [git, "credential", "fill"],
        input="protocol=https\nhost=github.com\n\n",
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        raise RuntimeError("git credential fill 失败：%s" % (proc.stderr or "").strip())
    for line in proc.stdout.splitlines():
        if line.startswith("password="):
            token = line.split("=", 1)[1].strip()
            if token:
                return token
    raise RuntimeError(
        "凭据里没有 token。先手动推一次仓库（会弹 GitHub 授权），再来跑这个脚本。"
    )


def api(token, method, path, payload=None):
    url = path if path.startswith("http") else API + path
    body = json.dumps(payload).encode("utf-8") if payload is not None else None
    req = urllib.request.Request(url, data=body, method=method)
    req.add_header("Authorization", "Bearer " + token)
    req.add_header("Accept", "application/vnd.github+json")
    req.add_header("User-Agent", "gk-daily-push-keepalive")
    if body is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            raw = resp.read().decode("utf-8")
            return resp.status, (json.loads(raw) if raw else None)
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", "replace")
        try:
            return exc.code, json.loads(raw)
        except Exception:
            return exc.code, {"message": raw[:300]}


# ---------------------------- 三件检查 ----------------------------

def check_workflow(token, dry_run, problems):
    """① 定时任务是不是被关了。"""
    status, data = api(token, "GET", "/repos/%s/actions/workflows" % REPO)
    if status != 200:
        problems.append("读不到 workflow 列表（HTTP %s）" % status)
        return
    for wf in (data or {}).get("workflows", []):
        if wf.get("path", "").split("/")[-1] != WATCH_WORKFLOW:
            continue
        state = wf.get("state", "?")
        log("① 定时任务 %s 状态：%s" % (WATCH_WORKFLOW, state))
        if state == "active":
            return
        log("   ⚠️ 被禁用了（%s），正在重新启用…" % state)
        if dry_run:
            log("   （dry-run，跳过）")
            problems.append("定时任务处于 %s 状态（未修复）" % state)
            return
        code, resp = api(token, "PUT",
                         "/repos/%s/actions/workflows/%s/enable" % (REPO, WATCH_WORKFLOW))
        if code in (200, 204):
            log("   ✅ 已重新启用")
            problems.append("定时任务曾被禁用（%s），已重新启用" % state)
        else:
            log("   ❌ 启用失败：HTTP %s %s" % (code, (resp or {}).get("message", "")))
            problems.append("重新启用定时任务失败（HTTP %s）" % code)
        return
    problems.append("仓库里找不到 %s" % WATCH_WORKFLOW)


def check_last_commit(token, dry_run, max_age, problems):
    """② 上一次提交距今多少天，太久了就补一个。"""
    status, data = api(token, "GET", "/repos/%s/commits?per_page=1&sha=main" % REPO)
    if status != 200:
        problems.append("读不到提交记录（HTTP %s）" % status)
        return
    try:
        iso = data[0]["commit"]["committer"]["date"]
        when = datetime.datetime.strptime(iso, "%Y-%m-%dT%H:%M:%SZ").replace(
            tzinfo=datetime.timezone.utc)
    except Exception:
        problems.append("提交记录解析失败")
        return
    now = datetime.datetime.now(datetime.timezone.utc)
    days = (now - when).days
    log("② 最后一次提交：%s UTC（%d 天前，门槛 %d 天）" % (iso, days, max_age))
    if days < max_age:
        log("   离 60 天还早，不用动")
        return

    log("   ⚠️ 太久了，补一个提交把禁用计时器清零…")
    if dry_run:
        log("   （dry-run，跳过）")
        problems.append("距上次提交 %d 天（未提交）" % days)
        return

    # 文件存在就更新（要带 sha），不存在就新建 —— 走 Contents API，不碰本地仓库
    code, cur = api(token, "GET", "/repos/%s/contents/%s?ref=main" % (REPO, KEEPALIVE_PATH))
    payload = {
        "message": "chore: 本地保活提交（防 60 天无活动自动禁用）[skip ci]",
        "branch": "main",
        "content": base64.b64encode(
            ("本地保活 %s\n（由 keepalive_local.py 提交，"
             "目的只是让仓库保持活跃）\n" % now.strftime("%Y-%m-%d %H:%M:%S UTC")
             ).encode("utf-8")
        ).decode("ascii"),
    }
    if code == 200 and cur and cur.get("sha"):
        payload["sha"] = cur["sha"]
    code, resp = api(token, "PUT", "/repos/%s/contents/%s" % (REPO, KEEPALIVE_PATH), payload)
    if code in (200, 201):
        log("   ✅ 已提交（%s）" % (resp or {}).get("commit", {}).get("sha", "")[:8])
        problems.append("距上次提交 %d 天，已补一个保活提交" % days)
    else:
        log("   ❌ 提交失败：HTTP %s %s" % (code, (resp or {}).get("message", "")))
        problems.append("补提交失败（HTTP %s）" % code)


def check_recent_runs(token, problems):
    """③ 最近几次运行怎么样（顺带发现"任务在跑但一直失败"）。"""
    status, data = api(token, "GET", "/repos/%s/actions/runs?per_page=6" % REPO)
    if status != 200:
        log("③ 读不到运行记录（HTTP %s）" % status)
        return
    runs = (data or {}).get("workflow_runs", [])
    if not runs:
        log("③ 还没有运行记录")
        return
    log("③ 最近 %d 次运行：" % len(runs))
    fails = 0
    for r in runs:
        concl = r.get("conclusion") or r.get("status")
        if concl == "failure":
            fails += 1
        log("   %s  %-9s  %s" % (r.get("created_at", "")[:16].replace("T", " "),
                                 concl, r.get("head_commit", {}).get("message", "").split("\n")[0][:42]))
    if fails >= 3:
        problems.append("最近 %d 次运行里有 %d 次失败" % (len(runs), fails))


def main():
    global REPO

    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

    ap = argparse.ArgumentParser(description="GK 推送仓库本地保活检查")
    ap.add_argument("--repo", default=REPO, help="owner/repo，默认 %s" % REPO)
    ap.add_argument("--max-age", type=int, default=25,
                    help="距上次提交超过这么多天就补一个提交，默认 25（GitHub 的门槛是 60）")
    ap.add_argument("--dry-run", action="store_true", help="只看，不动")
    args = ap.parse_args()
    REPO = args.repo

    log("=" * 46)
    log("GK 推送仓库保活检查 · %s" % REPO)
    log("=" * 46)

    token = get_token()
    problems = []
    try:
        check_workflow(token, args.dry_run, problems)
        check_last_commit(token, args.dry_run, args.max_age, problems)
        check_recent_runs(token, problems)
    except Exception as exc:
        log("检查出错：%s" % exc)
        return 2

    log("")
    if problems:
        log("需要留意：")
        for p in problems:
            log("  · %s" % p)
    else:
        log("一切正常：定时任务在启用状态，提交也新鲜。")
    return 1 if any("失败" in p or "错误" in p for p in problems) else 0


if __name__ == "__main__":
    sys.exit(main())
