# -*- coding: utf-8 -*-
"""国内 GitHub 镜像支持。

这里只做三件事：
  1. 维护一份"当前可用"的镜像站列表（下面的 MIRRORS）
  2. 启动时让用户挑一个，或者自动测速挑最快的
  3. 把 GitHub 下载地址改写成镜像地址

重要：改写只发生在"发请求"的那一刻，写入 history.json / 缓存文件里的
URL 仍然是 GitHub 原始地址。否则换个镜像就等于整盘重下。

想增删镜像：直接改 MIRRORS，重新发个版本即可。
"""

import json
import os
import socket
import ssl
import sys
import threading
import time
import unicodedata
from concurrent.futures import ThreadPoolExecutor
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen

# ---------------------------------------------------------------------------
# 镜像站列表：顺序 = 自动选择失败时的兜底顺序（第一个失败就试第二个……）
# 这些节点都是 2026-11 实测能用的。失效了也不用慌，程序会自动跳过。
# ---------------------------------------------------------------------------
MIRRORS = [
    "https://gh-proxy.com",
    "https://ghproxy.net",
    "https://gh.07150721.xyz",
    "https://github.tbap.top",
    "https://eogh.719831.xyz",
    "https://gh.dpik.top",
    "https://git.yylx.win",
    "https://ghm.078465.xyz",
    "https://tvv.tw",
    "https://gh.acmsz.top",
]

# 测速用的样本：Lilu 的 release 包（约 760 KB），拿前 256 KB 计时
PROBE_ASSET = ("https://github.com/acidanthera/Lilu/releases/download/"
               "1.7.2/Lilu-1.7.2-RELEASE.zip")
# 备用样本：一个长期存在的 raw 文件，万一上面的资产被删了还能测
PROBE_RAW = "https://raw.githubusercontent.com/torvalds/linux/master/MAINTAINERS"
PROBE_BYTES = 256 * 1024

# 一个地址最多试几个镜像（含首选）。全是超时的话，逐个试 10 个太浪费时间，
# 所以失败时只往后再试 3 个，然后直接回落到 GitHub 原始地址。
FALLBACK_DEPTH = 3

USER_AGENT = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")

ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONFIG_PATH = os.path.join(ROOT_DIR, "mirror_config.json")

# 运行时状态：order 是"优先使用顺序"，空列表表示直连
_state = {"order": [], "label": "直连 GitHub"}


# ---------------------------------------------------------------------------
# URL 判定与改写
# ---------------------------------------------------------------------------
def _is_download_url(url):
    """只改写"确定是文件下载"的地址。

    GitHub 的网页类地址（/releases 列表页、/commits、仓库首页）绝对不要
    走镜像：镜像站对这类地址基本都返回它自己的首页，拿到的 HTML 解析不出
    东西，反而从"能慢速访问"变成"直接解析失败"。
    """
    try:
        parsed = urlparse(url)
    except Exception:
        return False

    host = (parsed.netloc or "").lower()
    path = parsed.path or ""

    if host == "raw.githubusercontent.com":
        return True
    if host in ("github.com", "www.github.com"):
        return "/releases/download/" in path or "/archive/" in path
    return False


def candidates(url):
    """返回按优先级排好的候选地址；最后一项永远是 GitHub 原始地址。"""
    if not _is_download_url(url):
        return [url]

    bases = _state.get("order") or []
    result = []
    for base in bases:
        if len(result) >= FALLBACK_DEPTH:
            break
        if url.startswith(base):
            continue
        result.append("{}/{}".format(base.rstrip("/"), url))

    result.append(url)
    return result


def describe():
    """当前生效的镜像，给日志用。"""
    return _state.get("label") or "直连 GitHub"


def demote():
    """某个镜像这次给出的文件校验不过，把它挪到队尾，
    别在同一个坑上连撞三次。"""
    order = _state.get("order") or []
    if len(order) > 1:
        _state["order"] = order[1:] + order[:1]


# ---------------------------------------------------------------------------
# 配置读写
# ---------------------------------------------------------------------------
def load_config():
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as handle:
            data = json.load(handle)
        if isinstance(data, dict):
            return data
    except Exception:
        pass
    return {}


def save_config(data):
    try:
        with open(CONFIG_PATH, "w", encoding="utf-8") as handle:
            json.dump(data, handle, indent=2, ensure_ascii=False)
    except Exception as error:
        print("  保存镜像设置失败：{}".format(error))


def _all_mirrors(config):
    """内置镜像 + 用户自己加的，去重后保持顺序。"""
    result = []
    for base in list(MIRRORS) + list(config.get("custom_mirrors") or []):
        if not isinstance(base, str):
            continue
        base = base.strip().rstrip("/")
        if base and base not in result and base.startswith("http"):
            result.append(base)
    return result


def apply_choice(choice, config=None):
    """choice: 镜像地址字符串，或 "direct"。"""
    config = config if config is not None else load_config()
    mirrors = _all_mirrors(config)

    if choice == "direct":
        _state["order"] = []
        _state["label"] = "直连 GitHub"
        return

    choice = str(choice).rstrip("/")
    if choice not in mirrors:
        mirrors.insert(0, choice)
    _state["order"] = [choice] + [m for m in mirrors if m != choice]
    _state["label"] = _host(choice)


def _host(base):
    try:
        return urlparse(base).netloc or base
    except Exception:
        return base


# ---------------------------------------------------------------------------
# 测速
# ---------------------------------------------------------------------------
def _ssl_context():
    context = ssl.create_default_context()
    try:
        import certifi
    except Exception:
        return context
    try:
        context.load_verify_locations(cafile=certifi.where())
    except Exception:
        pass
    return context


def _probe_once(target, timeout, sample):
    request = Request(target, headers={
        "User-Agent": USER_AGENT,
        "Range": "bytes=0-{}".format(sample - 1),
        "Accept-Encoding": "identity",
    })
    started = time.monotonic()
    response = urlopen(request, timeout=timeout, context=_ssl_context())
    try:
        if response.getcode() not in (200, 206):
            raise RuntimeError("HTTP {}".format(response.getcode()))
        chunk = response.read(sample)
    finally:
        response.close()
    elapsed = max(time.monotonic() - started, 0.001)
    if not chunk:
        raise RuntimeError("没有收到数据")
    return len(chunk) / 1024.0 / elapsed


def _probe(base, timeout=8, sample=PROBE_BYTES):
    """返回 (KB/s, 备注)；失败时 KB/s 为 0。base 为空串表示直连。"""
    last_error = ""
    for asset in (PROBE_ASSET, PROBE_RAW):
        target = "{}/{}".format(base.rstrip("/"), asset) if base else asset
        try:
            return _probe_once(target, timeout, sample), ""
        except HTTPError as error:
            last_error = "HTTP {}".format(error.code)
        except (socket.timeout, TimeoutError):
            last_error = "连接超时"
        except ssl.SSLError:
            last_error = "TLS 握手失败"
        except URLError as error:
            if isinstance(error.reason, (socket.timeout, TimeoutError)):
                last_error = "连接超时"
            else:
                last_error = "无法连接"
        except Exception as error:
            last_error = str(error)[:40] or error.__class__.__name__
    return 0, last_error


def speed_test(timeout=8, log=print, mirrors=None):
    """并行测速，每测完一个就打印一行（这就是用户要看的"测速过程"）。

    返回 [(镜像地址或 "", KB/s)]，按速度从快到慢。直连也参与排名，
    地址为空串的那一项就是 GitHub 直连。
    """
    pool_targets = list(mirrors or _state.get("candidates") or MIRRORS)
    targets = pool_targets + [""]
    total = len(targets)
    results = {}
    lock = threading.Lock()
    finished = [0]

    def worker(base):
        speed, note = _probe(base, timeout=timeout)
        with lock:
            finished[0] += 1
            name = _host(base) if base else "GitHub 直连"
            if speed > 0:
                log("  [{:>2}/{}]  {} {:>10}".format(
                    finished[0], total, _ljust(name, 24), _fmt_speed(speed)))
            else:
                log("  [{:>2}/{}]  {} 失败（{}）".format(
                    finished[0], total, _ljust(name, 24), note or "无响应"))
            results[base] = speed

    log("")
    with ThreadPoolExecutor(max_workers=min(8, total)) as pool:
        list(pool.map(worker, targets))
    log("")

    ranked = sorted(results.items(), key=lambda item: item[1], reverse=True)
    return ranked


# ---------------------------------------------------------------------------
# 交互菜单
# ---------------------------------------------------------------------------
def _display_width(text):
    width = 0
    for char in text:
        if unicodedata.combining(char):
            continue
        width += 2 if unicodedata.east_asian_width(char) in ("W", "F") else 1
    return width


def _pad_middle(text, width):
    space = max(width - _display_width(text), 0)
    left = space // 2
    return " " * left + text + " " * (space - left)


def _ljust(text, width):
    return text + " " * max(width - _display_width(text), 0)


def _head(text):
    os.system("cls" if os.name == "nt" else "clear")
    print("\033[8;30;100t", end="")
    width = 68
    separator = "═" * (width - 2)
    print("╔{}╗".format(separator))
    print("║{}║".format(_pad_middle(" {} ".format(text), width - 2)))
    print("╚{}╝".format(separator))


def _ask(prompt):
    try:
        return input(prompt).strip()
    except (EOFError, KeyboardInterrupt):
        return ""


def _fmt_speed(kbps):
    if kbps >= 1024:
        return "{:.1f} MB/s".format(kbps / 1024.0)
    return "{:.0f} KB/s".format(kbps)


def select(force=False):
    """启动时的镜像选择页。返回最终生效的镜像地址（"direct" 表示直连）。"""
    config = load_config()
    mirrors = _all_mirrors(config)
    last = config.get("last_choice")

    # 命令行参数 / 环境变量可以直接跳过菜单（方便脚本调用和复现问题）
    preset = _preset_choice()
    if preset:
        apply_choice(preset, config)
        print("")
        print("  GitHub 下载：{}".format(
            "直连" if preset == "direct" else _host(preset)))
        print("")
        return preset

    if not force and last and not sys.stdin.isatty():
        apply_choice(last, config)
        return last

    if not sys.stdin.isatty():
        apply_choice(last or "direct", config)
        return last or "direct"

    _menu(mirrors, last, config)
    return config.get("last_choice") or "direct"


def _preset_choice():
    for arg in sys.argv[1:]:
        if arg.startswith("--mirror="):
            value = arg.split("=", 1)[1].strip()
            if value.lower() in ("auto", "x"):
                return None
            if value.lower() in ("dir", "direct", "none", "off"):
                return "direct"
            return value
        if arg in ("--no-mirror", "--direct"):
            return "direct"
    return os.environ.get("OPCORE_MIRROR", "").strip() or None


def _label(choice):
    return "直连 GitHub" if choice == "direct" else _host(choice)


def _menu(mirrors, last, config):
    while True:
        _head("GitHub 下载加速")
        print("")
        print("  本程序需要从 GitHub 下载大量文件，大陆网络直连经常超时")
        print("  选定一个国内镜像站可以快很多")
        print("")
        print("  \033[1m选择下载线路：\033[0m")
        print("")
        for index, base in enumerate(mirrors, 1):
            mark = "\033[92m>\033[0m" if base == last else " "
            print("  {} [{:>2}] {}".format(mark, index, _host(base)))
        print("")
        print("    [ x] 自动测速，选最快的（推荐）")
        print("    [ u] 手动填写镜像地址")
        print("    [ d] 不使用镜像，直连 GitHub")
        print("")
        if last:
            print("  上次使用：{}（直接回车沿用）".format(_label(last)))
        else:
            print("  首次运行，建议直接选 x 自动测速")
        print("")

        answer = _ask("  请输入序号或字母：").lower()

        if answer == "" and last:
            _commit(last, config, mirrors)
            print("")
            print("  沿用上次：{}".format(_label(last)))
            print("")
            _ask("  按回车开始……")
            return

        if answer in ("x", "auto", ""):
            print("")
            print("  正在测速，请稍候……")
            ranked = speed_test(mirrors=mirrors)
            _show_ranking(ranked)
            usable = [item for item in ranked if item[1] > 0]
            if not usable:
                print("  \033[1;93m测速全部失败，先按直连运行\033[0m")
                print("  可以稍后重跑本程序再试，或在菜单里手动选一个")
                print("")
                _ask("  按回车继续……")
                continue
            best, speed = usable[0]
            if best == "":
                print("  \033[92m直连最快（{}），本次不走镜像\033[0m".format(_fmt_speed(speed)))
                _commit("direct", config, mirrors)
                print("")
                _ask("  按回车开始……")
                return
            _commit(best, config, mirrors)
            print("")
            print("  \033[92m已选择：{} （{}）\033[0m".format(_host(best), _fmt_speed(speed)))
            print("  下载失败时会自动换用其他镜像，最后才回落到直连")
            print("")
            _ask("  按回车开始……")
            return

        if answer in ("d", "direct"):
            _commit("direct", config, mirrors)
            print("")
            print("  已选择：直连 GitHub（不使用镜像）")
            print("")
            _ask("  按回车开始……")
            return

        if answer in ("u", "custom"):
            print("")
            print("  格式：https://你的镜像站   （不要带结尾斜杠）")
            custom = _ask("  地址：")
            if not custom.startswith("http"):
                print("  \033[91m地址无效，请输入以 http 开头的完整地址\033[0m")
                _ask("  按回车返回……")
                continue
            custom = custom.strip().rstrip("/")
            customs = config.get("custom_mirrors") or []
            if custom not in customs:
                customs.append(custom)
                config["custom_mirrors"] = customs
            mirrors = _all_mirrors(config)
            _commit(custom, config, mirrors)
            print("")
            print("  已选择：{}".format(custom))
            print("")
            _ask("  按回车开始……")
            return

        if answer.isdigit():
            index = int(answer)
            if 1 <= index <= len(mirrors):
                base = mirrors[index - 1]
                _commit(base, config, mirrors)
                print("")
                print("  已选择：{}".format(_host(base)))
                print("")
                _ask("  按回车开始……")
                return

        print("  \033[91m输入无效，请重新选择\033[0m")
        time.sleep(1)


def _show_ranking(ranked):
    print("  \033[1m测速结果（越快越好）：\033[0m")
    print("")
    for position, (base, speed) in enumerate(ranked, 1):
        name = _ljust(_host(base) if base else "GitHub 直连", 24)
        if speed <= 0:
            print("   {:>2}  {} 不可用".format(position, name))
        elif position == 1:
            print("   {:>2}  {} {:>10}  ★".format(position, name, _fmt_speed(speed)))
        else:
            print("   {:>2}  {} {:>10}".format(position, name, _fmt_speed(speed)))
    print("")


def _commit(choice, config, mirrors):
    config["last_choice"] = choice
    save_config(config)
    apply_choice(choice, config)
    _state["candidates"] = list(mirrors)


def reset():
    """删掉记忆的选择（下次启动重新问你）。"""
    config = load_config()
    config.pop("last_choice", None)
    save_config(config)
