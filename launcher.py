# -*- coding: utf-8 -*-
"""
Ueban Launcher — лаунчер Minecraft Java Edition (офлайн-режим).

  * список версий Mojang, загрузка клиента / библиотек / ассетов / нативок;
  * автоматическая загрузка нужной Java (официальный рантайм Mojang);
  * профили JVM-флагов для оптимизации, настройки памяти, окна, Java;
  * анимированный градиентный интерфейс в пиксельном стиле.

Только стандартная библиотека Python. Сборка в exe: build_standalone.bat
"""
import hashlib
import json
import math
import os
import platform
import queue
import re
import shutil
import subprocess
import sys
import threading
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request
import uuid
import webbrowser
import zipfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

try:
    import tkinter as tk
    from tkinter import ttk, messagebox, filedialog
    import tkinter.font as tkfont
except ImportError:  # для тестов без GUI
    tk = None

APP_NAME = "Ueban Launcher"
APP_VERSION = "3.1"
UA = f"UebanLauncher/{APP_VERSION}"
# Microsoft Client ID приложения лаунчера. Если вписать его сюда (один раз, разработчиком),
# пользователям не придётся ничего вводить: вход через Microsoft заработает сразу.
# Как получить: portal.azure.com → App registrations → New registration (Personal accounts,
# Allow public client flows = Yes), затем одобрить ID у Mojang: https://aka.ms/mce-reviewappid
DEFAULT_MS_CLIENT_ID = ""

MANIFEST_URL = "https://piston-meta.mojang.com/mc/game/version_manifest_v2.json"
JAVA_ALL_URL = ("https://launchermeta.mojang.com/v1/products/java-runtime/"
                "2ec0cc96c44e5a76b9c8b7c39df7210883d12871/all.json")
RESOURCES_URL = "https://resources.download.minecraft.net/"

IS_WIN = sys.platform == "win32"
OS_NAME = {"win32": "windows", "darwin": "osx"}.get(sys.platform, "linux")
IS64 = sys.maxsize > 2 ** 32
_m = platform.machine().lower()
ARCH = "arm64" if ("arm" in _m or "aarch64" in _m) else ("x86_64" if IS64 else "x86")

if IS_WIN:
    BASE = Path(os.environ.get("APPDATA", str(Path.home()))) / ".uebanlauncher"
else:
    BASE = Path.home() / ".uebanlauncher"

VERSIONS = BASE / "versions"
LIBS = BASE / "libraries"
ASSETS = BASE / "assets"
RUNTIME = BASE / "runtime"
GAME_DIR = BASE / "game"
CONFIG = BASE / "config.json"
INSTANCES = BASE / "instances"
GAME_BY_VERSION = BASE / "game_by_version"
BACKUPS = BASE / "backups"
AUTHLIB = BASE / "authlib-injector.jar"
LAST_LOG = BASE / "last_game.log"
VERIFY = False  # проверять sha1 уже скачанных файлов (настройка)

CREATE_NO_WINDOW = 0x08000000
HIGH_PRIORITY_CLASS = 0x00000080



def resource_path(name):
    """Путь к файлу рядом со скриптом или внутри собранного exe (PyInstaller)."""
    base = getattr(sys, "_MEIPASS", None) or os.path.dirname(os.path.abspath(__file__))
    return os.path.join(base, name)


ICON_FILE = BASE / "ueban.ico"


def ensure_icon_file():
    """Кладём ueban.ico в папку лаунчера: на него ссылаются ярлыки, значок везде один и тот же."""
    try:
        src_ico = resource_path("ueban.ico")
        if os.path.exists(src_ico):
            BASE.mkdir(parents=True, exist_ok=True)
            if (not ICON_FILE.exists()) or ICON_FILE.stat().st_size != os.path.getsize(src_ico):
                shutil.copyfile(src_ico, ICON_FILE)
            return str(ICON_FILE)
    except Exception:
        pass
    return None


def set_app_id():
    """Без этого Windows группирует окно под значком python и не показывает наш значок на панели задач."""
    if IS_WIN:
        try:
            import ctypes
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("Ueban.Launcher.Minecraft")
        except Exception:
            pass


def create_desktop_shortcut():
    """Ярлык на рабочем столе с тем же значком ueban.ico. → путь к ярлыку."""
    if not IS_WIN:
        raise RuntimeError("Ярлык на рабочем столе поддерживается только в Windows")
    ico = ensure_icon_file()
    if getattr(sys, "frozen", False):
        target, args, workdir = sys.executable, "", str(Path(sys.executable).parent)
    else:
        pyw = Path(sys.executable).with_name("pythonw.exe")
        target = str(pyw if pyw.exists() else sys.executable)
        args = '"%s"' % os.path.abspath(__file__)
        workdir = os.path.dirname(os.path.abspath(__file__))
    icon = (ico or target) + ",0"

    def q(s):
        return "'" + str(s).replace("'", "''") + "'"
    ps = ("$d=[Environment]::GetFolderPath('Desktop');"
          "$l=Join-Path $d 'Ueban Launcher.lnk';"
          "$s=(New-Object -ComObject WScript.Shell).CreateShortcut($l);"
          f"$s.TargetPath={q(target)};$s.Arguments={q(args)};$s.WorkingDirectory={q(workdir)};"
          f"$s.IconLocation={q(icon)};$s.Description='Ueban Launcher';$s.Save();Write-Output $l")
    r = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", ps],
                       capture_output=True, text=True, timeout=30, creationflags=CREATE_NO_WINDOW)
    if r.returncode != 0:
        raise RuntimeError((r.stderr or r.stdout or "PowerShell error").strip())
    return r.stdout.strip()


# ───────────────────────── сеть / файлы ─────────────────────────

def http_json(url, timeout=30):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def http_request(url, data=None, headers=None, method=None, timeout=30, form=False):
    """→ (код ответа, json). Ошибки HTTP не бросают исключение: тело ответа часто нужно (Ely.by, Microsoft)."""
    hdrs = {"User-Agent": UA, "Accept": "application/json"}
    hdrs.update(headers or {})
    body = None
    if data is not None:
        if form:
            body = urllib.parse.urlencode(data).encode("utf-8")
            hdrs["Content-Type"] = "application/x-www-form-urlencoded"
        else:
            body = json.dumps(data).encode("utf-8")
            hdrs["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=body, headers=hdrs,
                                 method=method or ("POST" if body is not None else "GET"))
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw, status = r.read(), r.status
    except urllib.error.HTTPError as e:
        raw, status = e.read(), e.code
    try:
        js = json.loads(raw.decode("utf-8")) if raw else {}
    except Exception:
        js = {"raw": raw.decode("utf-8", "replace")}
    return status, js


def sha1_file(path):
    h = hashlib.sha1()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def fetch(url, dest, sha1=None, size=None, retries=3):
    dest = Path(dest)
    if dest.exists() and (size is None or dest.stat().st_size == size):
        if not (VERIFY and sha1) or sha1_file(dest) == sha1:
            return
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_name(dest.name + ".part")
    last = None
    for _ in range(retries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=30) as r, open(tmp, "wb") as f:
                shutil.copyfileobj(r, f)
            if sha1 and sha1_file(tmp) != sha1:
                raise IOError(f"sha1 mismatch: {url}")
            os.replace(tmp, dest)
            return
        except Exception as e:  # noqa
            last = e
            time.sleep(1)
    raise last


def download_many(tasks, progress=None, workers=16):
    """tasks: список (url, dest, sha1, size)."""
    total = len(tasks)
    if not total:
        return
    done = 0
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futs = [ex.submit(fetch, *t) for t in tasks]
        for f in as_completed(futs):
            f.result()  # пробросит исключение
            done += 1
            if progress:
                progress(done, total)


# ───────────────────────── правила Mojang ─────────────────────────

def os_match(o):
    if "name" in o and o["name"] != OS_NAME:
        return False
    if "arch" in o:
        a = o["arch"]
        if a == "x86" and ARCH != "x86":
            return False
        if a in ("arm64", "aarch64") and ARCH != "arm64":
            return False
    if "version" in o:
        try:
            if not re.search(o["version"], platform.version()):
                return False
        except re.error:
            pass
    return True


def rules_ok(rules, features=None):
    if not rules:
        return True
    features = features or {}
    allowed = False
    for r in rules:
        ok = True
        if "os" in r:
            ok = os_match(r["os"])
        if ok and "features" in r:
            ok = all(features.get(k, False) == v for k, v in r["features"].items())
        if ok:
            allowed = (r.get("action") == "allow")
    return allowed


# ───────────────────────── версии ─────────────────────────

def local_versions():
    out = []
    if VERSIONS.exists():
        for d in VERSIONS.iterdir():
            if (d / f"{d.name}.json").exists():
                out.append(d.name)
    return sorted(out, reverse=True)


def load_version(vid, manifest=None, log=print):
    """Возвращает json версии (скачивает при необходимости). Поддерживает inheritsFrom."""
    jpath = VERSIONS / vid / f"{vid}.json"
    if not jpath.exists():
        if not manifest:
            manifest = http_json(MANIFEST_URL)
        entry = next((v for v in manifest["versions"] if v["id"] == vid), None)
        if not entry:
            raise RuntimeError(f"Версия {vid} не найдена")
        log(f"Скачиваю описание версии {vid}...")
        fetch(entry["url"], jpath, entry.get("sha1"))
    with open(jpath, "r", encoding="utf-8") as f:
        data = json.load(f)

    if "inheritsFrom" in data:
        parent = load_version(data["inheritsFrom"], manifest, log)
        merged = dict(parent)
        for k, v in data.items():
            if k not in ("libraries", "arguments"):
                merged[k] = v
        merged["libraries"] = data.get("libraries", []) + parent.get("libraries", [])
        if "arguments" in data or "arguments" in parent:
            args = {}
            for k in ("game", "jvm"):
                args[k] = (parent.get("arguments", {}).get(k, [])
                           + data.get("arguments", {}).get(k, []))
            merged["arguments"] = args
        merged["_jar_id"] = parent.get("_jar_id", data["inheritsFrom"])
        merged["id"] = vid
        return merged
    data["_jar_id"] = vid
    return data


def lib_path_from_name(name):
    parts = name.split("@")[0].split(":")
    g, a, v = parts[0], parts[1], parts[2]
    cl = parts[3] if len(parts) > 3 else None
    fn = f"{a}-{v}" + (f"-{cl}" if cl else "") + ".jar"
    return f"{g.replace('.', '/')}/{a}/{v}/{fn}"


def collect_libraries(vj):
    """→ (tasks, classpath[list[Path]], natives[list[(Path, exclude)]])"""
    tasks, cp, natives = [], [], []
    for lib in vj.get("libraries", []):
        if not rules_ok(lib.get("rules")):
            continue
        dl = lib.get("downloads")
        if dl is None:
            rel = lib_path_from_name(lib["name"])
            base = lib.get("url", "https://libraries.minecraft.net/")
            if not base.endswith("/"):
                base += "/"
            tasks.append((base + rel, LIBS / rel, None, None))
            cp.append(LIBS / rel)
            continue
        art = dl.get("artifact")
        if art:
            p = LIBS / art["path"]
            if art.get("url"):
                tasks.append((art["url"], p, art.get("sha1"), art.get("size")))
            cp.append(p)
        nat = lib.get("natives")
        if nat and OS_NAME in nat:
            cls = nat[OS_NAME].replace("${arch}", "64" if IS64 else "32")
            a2 = dl.get("classifiers", {}).get(cls)
            if a2:
                p = LIBS / a2["path"]
                tasks.append((a2["url"], p, a2.get("sha1"), a2.get("size")))
                natives.append((p, lib.get("extract", {}).get("exclude", [])))
    seen, uniq = set(), []
    for p in cp:
        if p not in seen:
            seen.add(p)
            uniq.append(p)
    return tasks, uniq, natives


def extract_natives(natives, ndir):
    ndir.mkdir(parents=True, exist_ok=True)
    if any(ndir.iterdir()):
        return
    for p, excl in natives:
        with zipfile.ZipFile(p) as z:
            for n in z.namelist():
                if n.endswith("/") or any(n.startswith(e) for e in excl):
                    continue
                z.extract(n, ndir)


def prepare_assets(vj):
    ai = vj["assetIndex"]
    idx_path = ASSETS / "indexes" / f"{ai['id']}.json"
    fetch(ai["url"], idx_path, ai.get("sha1"), ai.get("size"))
    with open(idx_path, "r", encoding="utf-8") as f:
        idx = json.load(f)
    tasks = []
    for name, o in idx["objects"].items():
        h = o["hash"]
        tasks.append((f"{RESOURCES_URL}{h[:2]}/{h}", ASSETS / "objects" / h[:2] / h, h, o["size"]))
    return ai["id"], idx, tasks


def materialize_legacy_assets(aid, idx, game_dir):
    """Для очень старых версий: virtual / map_to_resources."""
    target = None
    if idx.get("map_to_resources"):
        target = Path(game_dir) / "resources"
    elif idx.get("virtual"):
        target = ASSETS / "virtual" / aid
    if not target:
        return
    for name, o in idx["objects"].items():
        h = o["hash"]
        src = ASSETS / "objects" / h[:2] / h
        dst = target / name
        if not dst.exists() and src.exists():
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(src, dst)


# ───────────────────────── Java ─────────────────────────

def java_exe_in(root):
    exe = root / "bin" / ("java.exe" if IS_WIN else "java")
    return exe if exe.exists() else None


def runtime_key():
    if IS_WIN:
        return "windows-arm64" if ARCH == "arm64" else ("windows-x64" if IS64 else "windows-x86")
    if OS_NAME == "osx":
        return "mac-os-arm64" if ARCH == "arm64" else "mac-os"
    return "linux" if IS64 else "linux-i386"


def download_runtime(component, log, progress):
    allj = http_json(JAVA_ALL_URL)
    entries = allj.get(runtime_key(), {}).get(component) or []
    if not entries:
        raise RuntimeError(f"Mojang не предоставляет рантайм {component} для {runtime_key()}")
    mf = http_json(entries[0]["manifest"]["url"])
    root = RUNTIME / component
    tasks, execs = [], []
    for rel, info in mf["files"].items():
        if info["type"] == "directory":
            (root / rel).mkdir(parents=True, exist_ok=True)
        elif info["type"] == "file":
            raw = info["downloads"]["raw"]
            tasks.append((raw["url"], root / rel, raw["sha1"], raw["size"]))
            if info.get("executable"):
                execs.append(root / rel)
    log(f"Скачиваю Java ({component}), файлов: {len(tasks)}...")
    download_many(tasks, progress)
    if not IS_WIN:
        for p in execs:
            os.chmod(p, 0o755)


def system_java_candidates():
    c = []
    if os.environ.get("JAVA_HOME"):
        c.append(Path(os.environ["JAVA_HOME"]) / "bin" / ("java.exe" if IS_WIN else "java"))
    w = shutil.which("java")
    if w:
        c.append(Path(w))
    if IS_WIN:
        for pf in (os.environ.get("ProgramFiles"), os.environ.get("ProgramFiles(x86)")):
            if not pf:
                continue
            for vendor in ("Java", "Eclipse Adoptium", "Microsoft", "Zulu", "BellSoft"):
                d = Path(pf) / vendor
                if d.exists():
                    c += sorted(d.glob("*/bin/java.exe"), reverse=True)
    return [p for p in c if p.exists()]


def java_major(exe):
    try:
        out = subprocess.run([str(exe), "-version"], capture_output=True, text=True,
                             timeout=15,
                             creationflags=CREATE_NO_WINDOW if IS_WIN else 0).stderr
        m = re.search(r'version "(\d+)(?:\.(\d+))?', out)
        if not m:
            return None
        major = int(m.group(1))
        return int(m.group(2)) if major == 1 and m.group(2) else major
    except Exception:
        return None


def ensure_java(vj, cfg, log, progress):
    custom = cfg.get("java_path")
    if custom and Path(custom).exists():
        return str(custom)
    jv = vj.get("javaVersion") or {"component": "jre-legacy", "majorVersion": 8}
    comp, need = jv["component"], jv["majorVersion"]
    root = RUNTIME / comp
    exe = java_exe_in(root)
    if exe:
        return str(exe)
    try:
        download_runtime(comp, log, progress)
        exe = java_exe_in(root)
        if exe:
            return str(exe)
    except Exception as e:
        log(f"Не удалось скачать Java от Mojang: {e}")
    for cand in system_java_candidates():
        if (java_major(cand) or 0) >= need:
            log(f"Использую системную Java: {cand}")
            return str(cand)
    raise RuntimeError(f"Не найдена Java {need}+. Установите её (например, Temurin) "
                       f"или проверьте интернет.")


# ───────────────────────── JVM-флаги и система ─────────────────────────

_G1_AIKAR = [
    "-XX:+UseG1GC", "-XX:+ParallelRefProcEnabled", "-XX:MaxGCPauseMillis=200",
    "-XX:+UnlockExperimentalVMOptions", "-XX:+DisableExplicitGC",
    "-XX:G1NewSizePercent=30", "-XX:G1MaxNewSizePercent=40", "-XX:G1HeapRegionSize=8M",
    "-XX:G1ReservePercent=20", "-XX:G1HeapWastePercent=5", "-XX:G1MixedGCCountTarget=4",
    "-XX:InitiatingHeapOccupancyPercent=15", "-XX:G1MixedGCLiveThresholdPercent=90",
    "-XX:G1RSetUpdatingPauseTimePercent=5", "-XX:SurvivorRatio=32",
    "-XX:+PerfDisableSharedMem", "-XX:MaxTenuringThreshold=1",
]

_G1_BIG = [
    "-XX:+UseG1GC", "-XX:+ParallelRefProcEnabled", "-XX:MaxGCPauseMillis=200",
    "-XX:+UnlockExperimentalVMOptions", "-XX:+DisableExplicitGC",
    "-XX:G1NewSizePercent=40", "-XX:G1MaxNewSizePercent=50", "-XX:G1HeapRegionSize=16M",
    "-XX:G1ReservePercent=15", "-XX:G1HeapWastePercent=5", "-XX:G1MixedGCCountTarget=4",
    "-XX:InitiatingHeapOccupancyPercent=20", "-XX:G1MixedGCLiveThresholdPercent=90",
    "-XX:G1RSetUpdatingPauseTimePercent=5", "-XX:SurvivorRatio=32",
    "-XX:+PerfDisableSharedMem", "-XX:MaxTenuringThreshold=1",
    "-XX:ReservedCodeCacheSize=512m",
]

# ключ: (название, описание, флаги)
FLAG_PRESETS = {
    "none": ("Без флагов (как в обычном лаунчере)",
             "Только объём памяти. Так игру запускает стандартный лаунчер.",
             []),
    "aikar": ("Оптимальный (G1GC, рекомендуется)",
              "G1GC с тонкой настройкой (флаги Aikar). Обычно даёт меньше фризов от сборщика "
              "мусора; лучше всего работает при 3 ГБ памяти и больше.",
              _G1_AIKAR),
    "aikar_big": ("Большая сборка (G1GC, 8 ГБ и больше)",
                  "Вариант флагов Aikar для больших объёмов памяти: крупнее регионы G1 и молодое "
                  "поколение, плюс увеличенный кэш JIT. Для тяжёлых сборок с модами и 8+ ГБ.",
                  _G1_BIG),
    "zgc": ("Минимум пауз (ZGC, нужна Java 21+)",
            "Сборщик ZGC почти без пауз. Нужна Java 21 и новее: на более старой Java "
            "автоматически включится профиль «Оптимальный». Может использовать больше памяти.",
            ["-XX:+UseZGC", "-XX:+DisableExplicitGC"]),
    "lowend": ("Слабый ПК (SerialGC)",
               "Самый лёгкий сборщик мусора. Подходит для слабых процессоров и небольшого "
               "объёма ОЗУ (примерно до 3 ГБ для игры).",
               ["-XX:+UseSerialGC", "-XX:+DisableExplicitGC", "-XX:-UsePerfData"]),
}


# дополнительные флаги: независимые галочки поверх выбранного профиля
EXTRA_FLAGS = {
    "pretouch": ("Предзагрузка памяти (меньше лагов в игре, дольше старт)", ["-XX:+AlwaysPreTouch"]),
    "codecache": ("Увеличенный кэш JIT (для сборок с модами)", ["-XX:ReservedCodeCacheSize=512m"]),
    "ipv4": ("Предпочитать IPv4 (если медленно заходит на серверы)", ["-Djava.net.preferIPv4Stack=true"]),
}


def jvm_flags(cfg, java_ver, log=print):
    key = cfg.get("preset", "aikar")
    if key not in FLAG_PRESETS:
        key = "aikar"
    if key == "zgc" and java_ver < 21:
        log(f"ZGC требует Java 21+, у вас {java_ver}: включаю профиль «Оптимальный».")
        key = "aikar"
    flags = list(FLAG_PRESETS[key][2])
    for k in cfg.get("extra_flags") or []:
        for f in EXTRA_FLAGS.get(k, ("", []))[1]:
            if f not in flags:
                flags.append(f)
    custom = (cfg.get("custom_jvm") or "").strip()
    if custom:
        flags += custom.split()
    return flags


def total_ram_mb():
    try:
        if IS_WIN:
            import ctypes

            class MEMSTAT(ctypes.Structure):
                _fields_ = [("dwLength", ctypes.c_ulong), ("dwMemoryLoad", ctypes.c_ulong),
                            ("ullTotalPhys", ctypes.c_ulonglong), ("ullAvailPhys", ctypes.c_ulonglong),
                            ("ullTotalPageFile", ctypes.c_ulonglong), ("ullAvailPageFile", ctypes.c_ulonglong),
                            ("ullTotalVirtual", ctypes.c_ulonglong), ("ullAvailVirtual", ctypes.c_ulonglong),
                            ("sullAvailExtendedVirtual", ctypes.c_ulonglong)]
            s = MEMSTAT()
            s.dwLength = ctypes.sizeof(MEMSTAT)
            ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(s))
            return int(s.ullTotalPhys // (1024 * 1024))
        return int(os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES") // (1024 * 1024))
    except Exception:
        return 8192


# ───────────────────────── команда запуска ─────────────────────────

def offline_uuid(name):
    d = hashlib.md5(("OfflinePlayer:" + name).encode("utf-8")).digest()
    return str(uuid.UUID(bytes=d, version=3))


def subst(s, d):
    return re.sub(r"\$\{([^}]+)\}", lambda m: str(d.get(m.group(1), m.group(0))), s)


def flatten_args(args, feats, d):
    out = []
    for a in args:
        if isinstance(a, str):
            out.append(subst(a, d))
        else:
            if not rules_ok(a.get("rules"), feats):
                continue
            v = a["value"]
            for x in ([v] if isinstance(v, str) else v):
                out.append(subst(x, d))
    return out


def build_command(vj, java, classpath, natives_dir, ram_mb, assets_id, game_dir, auth, log_cfg=None,
                  xms_mb=256, extra_jvm=(), extra_game=()):
    jar_id = vj.get("_jar_id", vj["id"])
    cp = list(classpath) + [VERSIONS / jar_id / f"{jar_id}.jar"]
    sep = os.pathsep
    d = {
        "auth_player_name": auth["name"],
        "version_name": vj["id"],
        "game_directory": str(game_dir),
        "assets_root": str(ASSETS),
        "assets_index_name": assets_id,
        "auth_uuid": auth["uuid"],
        "auth_access_token": auth["token"],
        "auth_session": auth["token"],
        "user_type": auth["user_type"],
        "user_properties": "{}",
        "version_type": vj.get("type", "release"),
        "natives_directory": str(natives_dir),
        "launcher_name": "UebanLauncher",
        "launcher_version": APP_VERSION,
        "classpath": sep.join(str(p) for p in cp),
        "classpath_separator": sep,
        "library_directory": str(LIBS),
        "clientid": "0",
        "auth_xuid": "0",
        "game_assets": str(ASSETS / "virtual" / assets_id),
    }
    feats = {}

    if "arguments" in vj:
        jvm = flatten_args(vj["arguments"].get("jvm", []), feats, d)
        game = flatten_args(vj["arguments"].get("game", []), feats, d)
    else:
        jvm = ["-Djava.library.path=" + d["natives_directory"],
               "-Dminecraft.launcher.brand=UebanLauncher",
               "-Dminecraft.launcher.version=" + APP_VERSION,
               "-cp", d["classpath"]]
        game = [subst(x, d) for x in vj.get("minecraftArguments", "").split()]

    xms_mb = max(128, min(int(xms_mb), int(ram_mb)))
    cmd = [java, f"-Xmx{ram_mb}M", f"-Xms{xms_mb}M"]
    cmd += list(extra_jvm)
    if log_cfg:
        cmd.append(log_cfg[0].replace("${path}", str(log_cfg[1])))
    cmd += jvm + [vj["mainClass"]] + game + list(extra_game)
    return cmd


# ───────────────────────── общие утилиты ─────────────────────────

def safe_name(s):
    return re.sub(r'[\\/:*?"<>|★]+', "_", str(s)).strip() or "default"


def open_path(path):
    path = Path(path)
    path.mkdir(parents=True, exist_ok=True)
    if IS_WIN:
        os.startfile(str(path))  # noqa
    elif OS_NAME == "osx":
        subprocess.Popen(["open", str(path)])
    else:
        subprocess.Popen(["xdg-open", str(path)])


def fmt_num(n):
    try:
        n = int(n)
    except Exception:
        return "0"
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 1000:
        return f"{n / 1000:.1f}K"
    return str(n)


def http_text(url, timeout=30):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8", "replace")


def version_info(vid):
    """→ (версия Minecraft, загрузчик или None) по id установленной/обычной версии."""
    mc, loader = vid, None
    p = VERSIONS / vid / f"{vid}.json"
    try:
        if p.exists():
            with open(p, "r", encoding="utf-8") as f:
                mc = json.load(f).get("inheritsFrom", vid)
    except Exception:
        pass
    low = vid.lower()
    for k in ("neoforge", "fabric", "quilt", "forge"):
        if k in low:
            loader = k
            break
    return mc, loader


def resolve_game_dir(cfg, version_id, inst=None):
    if inst:
        return instance_game_dir(inst)
    if cfg.get("per_version_dirs"):
        return GAME_BY_VERSION / safe_name(version_id)
    return GAME_DIR


def official_minecraft_dir():
    if IS_WIN:
        return Path(os.environ.get("APPDATA", str(Path.home()))) / ".minecraft"
    if OS_NAME == "osx":
        return Path.home() / "Library" / "Application Support" / "minecraft"
    return Path.home() / ".minecraft"


def backup_saves(game_dir, label):
    saves = Path(game_dir) / "saves"
    if not saves.exists() or not any(saves.iterdir()):
        raise RuntimeError("В этой папке игры нет миров")
    BACKUPS.mkdir(parents=True, exist_ok=True)
    name = f"{safe_name(label)}-{time.strftime('%Y%m%d-%H%M%S')}"
    return Path(shutil.make_archive(str(BACKUPS / name), "zip", str(game_dir), "saves"))


def import_from_official(game_dir):
    src = official_minecraft_dir()
    if not src.exists():
        raise RuntimeError(f"Папка {src} не найдена")
    game_dir = Path(game_dir)
    game_dir.mkdir(parents=True, exist_ok=True)
    copied = []
    for name in ("saves", "resourcepacks", "shaderpacks", "screenshots", "options.txt", "servers.dat"):
        s = src / name
        if not s.exists():
            continue
        if s.is_dir():
            shutil.copytree(s, game_dir / name, dirs_exist_ok=True)
        else:
            shutil.copy2(s, game_dir / name)
        copied.append(name)
    return copied


# ───────────────────────── сборки (instances) ─────────────────────────

LOADER_NAMES = {"vanilla": "Vanilla", "fabric": "Fabric", "quilt": "Quilt",
                "forge": "Forge", "neoforge": "NeoForge"}
LOADER_KEY = {v: k for k, v in LOADER_NAMES.items()}


def slugify(name):
    s = re.sub(r"[^\w\-. ]+", "", str(name), flags=re.UNICODE).strip().replace(" ", "_")
    return s[:40] or "build"


def instance_game_dir(inst):
    return INSTANCES / inst["id"] / "minecraft"


def instance_label(inst):
    return f"★ {inst['name']}  ({inst['mc']} {LOADER_NAMES.get(inst['loader'], inst['loader'])})"


def list_instances():
    out = []
    if INSTANCES.exists():
        for d in sorted(INSTANCES.iterdir(), key=lambda p: p.name.lower()):
            f = d / "instance.json"
            if f.exists():
                try:
                    with open(f, "r", encoding="utf-8") as fh:
                        inst = json.load(fh)
                    inst["id"] = d.name
                    out.append(inst)
                except Exception:
                    pass
    return out


def save_instance(inst):
    d = INSTANCES / inst["id"]
    (d / "minecraft").mkdir(parents=True, exist_ok=True)
    with open(d / "instance.json", "w", encoding="utf-8") as f:
        json.dump({k: v for k, v in inst.items() if k != "id"}, f, ensure_ascii=False, indent=2)


def create_instance(name, mc, loader):
    base = slugify(name)
    iid, n = base, 2
    while (INSTANCES / iid).exists():
        iid = f"{base}_{n}"
        n += 1
    inst = {"id": iid, "name": name, "mc": mc, "loader": loader, "loader_version": "",
            "version_id": mc if loader == "vanilla" else "", "created": int(time.time())}
    save_instance(inst)
    return inst


def delete_instance(iid):
    shutil.rmtree(INSTANCES / iid, ignore_errors=True)


def duplicate_instance(iid, new_name):
    with open(INSTANCES / iid / "instance.json", "r", encoding="utf-8") as f:
        old = json.load(f)
    new = create_instance(new_name, old["mc"], old["loader"])
    new["loader_version"] = old.get("loader_version", "")
    new["version_id"] = old.get("version_id", "")
    src = INSTANCES / iid / "minecraft"
    if src.exists():
        shutil.copytree(src, INSTANCES / new["id"] / "minecraft", dirs_exist_ok=True)
    save_instance(new)
    return new


# ───────────────────────── загрузчики модов ─────────────────────────

FABRIC_META = {"fabric": "https://meta.fabricmc.net/v2", "quilt": "https://meta.quiltmc.org/v3"}


def install_fabric_like(kind, mc, loader_version, log):
    meta = FABRIC_META[kind]
    mcq = urllib.parse.quote(mc)
    if not loader_version:
        lst = http_json(f"{meta}/versions/loader/{mcq}")
        if not lst:
            raise RuntimeError(f"{LOADER_NAMES[kind]}: нет загрузчика для Minecraft {mc}")
        good = [x for x in lst if x["loader"].get("stable", True) and "-" not in x["loader"]["version"]]
        loader_version = (good or lst)[0]["loader"]["version"]
    prof = http_json(f"{meta}/versions/loader/{mcq}/{urllib.parse.quote(loader_version)}/profile/json")
    vid = prof["id"]
    path = VERSIONS / vid / f"{vid}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(prof, f)
    log(f"Установлен {LOADER_NAMES[kind]} {loader_version} для Minecraft {mc}")
    return vid


def _version_dirs():
    if not VERSIONS.exists():
        return set()
    return {d.name for d in VERSIONS.iterdir() if d.is_dir() and (d / f"{d.name}.json").exists()}


def install_forge_like(kind, mc, loader_version, manifest, cfg, log, progress):
    """Forge / NeoForge через официальный установщик (экспериментально)."""
    if kind == "forge":
        promos = http_json("https://files.minecraftforge.net/net/minecraftforge/forge/"
                           "promotions_slim.json").get("promos", {})
        fv = loader_version or promos.get(f"{mc}-recommended") or promos.get(f"{mc}-latest")
        if not fv:
            raise RuntimeError(f"Для Minecraft {mc} нет Forge")
        full = f"{mc}-{fv}"
        url = (f"https://maven.minecraftforge.net/net/minecraftforge/forge/{full}/"
               f"forge-{full}-installer.jar")
    else:
        m = re.match(r"^1\.(\d+)(?:\.(\d+))?", mc)
        if not m:
            raise RuntimeError(f"NeoForge: версия {mc} не поддерживается")
        prefix = f"{m.group(1)}.{m.group(2) or '0'}."
        xml = http_text("https://maven.neoforged.net/releases/net/neoforged/neoforge/maven-metadata.xml")
        cand = [v for v in re.findall(r"<version>([^<]+)</version>", xml)
                if v.startswith(prefix) and not re.search(r"alpha|beta|snapshot", v, re.I)]
        if not cand:
            raise RuntimeError(f"Для Minecraft {mc} нет NeoForge")
        fv = loader_version or max(cand, key=lambda v: [int(x) if x.isdigit() else 0
                                                        for x in re.split(r"[.\-]", v)])
        url = (f"https://maven.neoforged.net/releases/net/neoforged/neoforge/{fv}/"
               f"neoforge-{fv}-installer.jar")

    vj = load_version(mc, manifest, log)
    cl = (vj.get("downloads") or {}).get("client")
    if cl:
        fetch(cl["url"], VERSIONS / mc / f"{mc}.jar", cl.get("sha1"), cl.get("size"))
    java = ensure_java(vj, cfg, log, lambda c, t: progress(c, t, "Java"))
    jar = BASE / "installers" / f"{kind}-{fv}-installer.jar"
    log(f"Скачиваю установщик {LOADER_NAMES[kind]} {fv}...")
    fetch(url, jar, None, None)
    lp = BASE / "launcher_profiles.json"
    if not lp.exists():
        with open(lp, "w", encoding="utf-8") as f:
            json.dump({"profiles": {}, "version": 3}, f)
    before = _version_dirs()
    log("Запускаю установщик (может занять несколько минут)...")
    proc = subprocess.Popen([java, "-jar", str(jar), "--installClient", str(BASE)], cwd=str(BASE),
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                            errors="replace", creationflags=CREATE_NO_WINDOW if IS_WIN else 0)
    for line in proc.stdout:
        log(line.rstrip())
    if proc.wait() != 0:
        raise RuntimeError("Установщик завершился с ошибкой — подробности выше в логе")
    now = _version_dirs()
    new = sorted(now - before)
    pref = [n for n in new if kind in n.lower()] or new
    if not pref:
        pref = [n for n in now if kind in n.lower() and str(fv) in n]
    if not pref:
        raise RuntimeError("Установщик не создал версию")
    return pref[0]


def ensure_loader(inst, manifest, cfg, log, progress):
    """Возвращает id версии для запуска; при необходимости ставит загрузчик."""
    loader, mc = inst["loader"], inst["mc"]
    if loader == "vanilla":
        return mc
    vid = inst.get("version_id")
    if vid and (VERSIONS / vid / f"{vid}.json").exists():
        return vid
    if loader in FABRIC_META:
        vid = install_fabric_like(loader, mc, inst.get("loader_version"), log)
    else:
        vid = install_forge_like(loader, mc, inst.get("loader_version"), manifest, cfg, log, progress)
    inst["version_id"] = vid
    save_instance(inst)
    return vid


# ───────────────────────── аккаунты ─────────────────────────

ACC_TYPES = {"offline": "офлайн", "ely": "Ely.by", "microsoft": "Microsoft"}
ELY_AUTH = "https://authserver.ely.by"
MS_OAUTH = "https://login.microsoftonline.com/consumers/oauth2/v2.0"
MS_SCOPE = "XboxLive.signin offline_access"
XSTS_ERRORS = {
    2148916233: "У этой учётной записи Microsoft нет профиля Xbox. Войдите на minecraft.net или xbox.com и создайте его.",
    2148916235: "Xbox Live недоступен в вашей стране.",
    2148916238: "Детская учётная запись: её нужно добавить в семейную группу Microsoft.",
}


class AuthError(Exception):
    def __init__(self, msg, two_factor=False):
        super().__init__(msg)
        self.two_factor = two_factor


def account_label(a):
    return f"{a['name']}  [{ACC_TYPES.get(a['type'], a['type'])}]"


def new_offline(name):
    return {"id": uuid.uuid4().hex, "type": "offline", "name": name, "uuid": offline_uuid(name)}


def ely_login(login, password, totp=""):
    token = uuid.uuid4().hex
    pw = f"{password}:{totp.strip()}" if totp and totp.strip() else password
    st, js = http_request(ELY_AUTH + "/auth/authenticate",
                          {"username": login, "password": pw, "clientToken": token, "requestUser": True})
    if st != 200 or "accessToken" not in js:
        msg = js.get("errorMessage") or js.get("error") or f"HTTP {st}"
        raise AuthError(str(msg), two_factor="two factor" in str(msg).lower())
    prof = js.get("selectedProfile") or next(iter(js.get("availableProfiles") or []), None)
    if not prof:
        raise AuthError("На аккаунте Ely.by нет игрового профиля")
    return {"id": uuid.uuid4().hex, "type": "ely", "name": prof["name"], "uuid": prof["id"],
            "access_token": js["accessToken"], "client_token": js.get("clientToken", token)}


def ely_refresh(acct):
    st, _ = http_request(ELY_AUTH + "/auth/validate", {"accessToken": acct["access_token"]})
    if st in (200, 204):
        return
    st, js = http_request(ELY_AUTH + "/auth/refresh",
                          {"accessToken": acct["access_token"], "clientToken": acct.get("client_token", "")})
    if st != 200 or "accessToken" not in js:
        raise AuthError("Сессия Ely.by истекла — войдите заново на вкладке «Аккаунты»")
    acct["access_token"] = js["accessToken"]
    prof = js.get("selectedProfile")
    if prof:
        acct["name"], acct["uuid"] = prof["name"], prof["id"]


def ensure_authlib(log):
    """authlib-injector нужен, чтобы игра принимала сессии Ely.by и показывала скины."""
    if AUTHLIB.exists() and AUTHLIB.stat().st_size > 20000:
        return AUTHLIB
    url = None
    try:
        rel = http_json("https://api.github.com/repos/yushijinhun/authlib-injector/releases/latest")
        url = next((a["browser_download_url"] for a in rel.get("assets", [])
                    if a.get("name", "").endswith(".jar")), None)
    except Exception as e:
        log(f"GitHub недоступен ({e}), пробую зеркало...")
    if not url:
        try:
            url = http_json("https://authlib-injector.yushi.moe/artifact/latest.json")["download_url"]
        except Exception as e:
            raise RuntimeError(f"Не удалось получить authlib-injector: {e}")
    log("Скачиваю authlib-injector (скины Ely.by)...")
    if AUTHLIB.exists():
        AUTHLIB.unlink()
    fetch(url, AUTHLIB, None, None)
    return AUTHLIB


def ms_device_start(client_id):
    st, js = http_request(f"{MS_OAUTH}/devicecode", {"client_id": client_id, "scope": MS_SCOPE}, form=True)
    if st != 200 or "device_code" not in js:
        raise AuthError(js.get("error_description") or js.get("error") or f"HTTP {st}")
    return js


def ms_device_poll(client_id, dev, cancelled=lambda: False):
    interval = int(dev.get("interval", 5))
    deadline = time.time() + int(dev.get("expires_in", 900))
    while time.time() < deadline:
        for _ in range(interval):
            if cancelled():
                raise AuthError("Вход отменён")
            time.sleep(1)
        st, js = http_request(f"{MS_OAUTH}/token", {
            "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
            "client_id": client_id, "device_code": dev["device_code"]}, form=True)
        if st == 200 and "access_token" in js:
            return js
        err = js.get("error")
        if err == "authorization_pending":
            continue
        if err == "slow_down":
            interval += 5
            continue
        raise AuthError(js.get("error_description") or err or f"HTTP {st}")
    raise AuthError("Время ожидания входа истекло")


def ms_refresh(client_id, refresh_token):
    st, js = http_request(f"{MS_OAUTH}/token", {"grant_type": "refresh_token", "client_id": client_id,
                                                "refresh_token": refresh_token, "scope": MS_SCOPE}, form=True)
    if st != 200 or "access_token" not in js:
        raise AuthError("Не удалось обновить вход Microsoft — войдите заново")
    return js


def ms_minecraft_login(ms_access_token):
    st, xbl = http_request("https://user.auth.xboxlive.com/user/authenticate", {
        "Properties": {"AuthMethod": "RPS", "SiteName": "user.auth.xboxlive.com",
                       "RpsTicket": "d=" + ms_access_token},
        "RelyingParty": "http://auth.xboxlive.com", "TokenType": "JWT"})
    if st != 200 or "Token" not in xbl:
        raise AuthError(f"Xbox Live: HTTP {st}")
    st, xsts = http_request("https://xsts.auth.xboxlive.com/xsts/authorize", {
        "Properties": {"SandboxId": "RETAIL", "UserTokens": [xbl["Token"]]},
        "RelyingParty": "rp://api.minecraftservices.com/", "TokenType": "JWT"})
    if st != 200 or "Token" not in xsts:
        code = xsts.get("XErr")
        raise AuthError(XSTS_ERRORS.get(code, f"XSTS: HTTP {st} {code or ''}"))
    uhs = xsts["DisplayClaims"]["xui"][0]["uhs"]
    st, mc = http_request("https://api.minecraftservices.com/authentication/login_with_xbox",
                          {"identityToken": f"XBL3.0 x={uhs};{xsts['Token']}"})
    if st != 200 or "access_token" not in mc:
        raise AuthError(f"Minecraft Services: HTTP {st}. Если «Invalid app registration» — ваш Client ID "
                        f"ещё не одобрен Mojang (форма aka.ms/mce-reviewappid).")
    st, prof = http_request("https://api.minecraftservices.com/minecraft/profile",
                            headers={"Authorization": "Bearer " + mc["access_token"]})
    if st == 404:
        raise AuthError("На этом аккаунте нет Minecraft Java Edition")
    if st != 200 or "id" not in prof:
        raise AuthError(f"Профиль Minecraft: HTTP {st}")
    return {"name": prof["name"], "uuid": prof["id"], "access_token": mc["access_token"],
            "expires": int(time.time()) + int(mc.get("expires_in", 86400)) - 120}


def ms_account_from_tokens(client_id, tok):
    mc = ms_minecraft_login(tok["access_token"])
    return {"id": uuid.uuid4().hex, "type": "microsoft", "name": mc["name"], "uuid": mc["uuid"],
            "access_token": mc["access_token"], "expires": mc["expires"],
            "refresh_token": tok.get("refresh_token", ""), "client_id": client_id}


def ms_refresh_if_needed(acct, cfg, log):
    if acct.get("access_token") and time.time() < acct.get("expires", 0):
        return
    cid = acct.get("client_id") or cfg.get("ms_client_id") or DEFAULT_MS_CLIENT_ID
    if not cid or not acct.get("refresh_token"):
        raise AuthError("Войдите в Microsoft-аккаунт заново (вкладка «Аккаунты»)")
    log("Обновляю вход Microsoft...")
    tok = ms_refresh(cid, acct["refresh_token"])
    mc = ms_minecraft_login(tok["access_token"])
    acct.update(name=mc["name"], uuid=mc["uuid"], access_token=mc["access_token"], expires=mc["expires"],
                refresh_token=tok.get("refresh_token", acct["refresh_token"]))


def resolve_auth(account, cfg, log):
    t = account["type"]
    if t == "offline":
        return {"name": account["name"], "uuid": account.get("uuid") or offline_uuid(account["name"]),
                "token": "0", "user_type": "legacy", "agent": None}
    if t == "ely":
        ely_refresh(account)
        jar = ensure_authlib(log)
        return {"name": account["name"], "uuid": account["uuid"], "token": account["access_token"],
                "user_type": "mojang", "agent": f"-javaagent:{jar}=ely.by"}
    if t == "microsoft":
        ms_refresh_if_needed(account, cfg, log)
        return {"name": account["name"], "uuid": account["uuid"], "token": account["access_token"],
                "user_type": "msa", "agent": None}
    raise RuntimeError("Неизвестный тип аккаунта")


# ───────────────────────── библиотека модов ─────────────────────────

MR_API = "https://api.modrinth.com/v2"
# ключ: (название, тип проекта Modrinth, зарезервировано, папка в игре)
CONTENT_TYPES = {
    "mods": ("Моды", "mod", 6, "mods"),
    "resourcepacks": ("Ресурспаки", "resourcepack", 12, "resourcepacks"),
    "shaders": ("Шейдеры", "shader", 6552, "shaderpacks"),
}


def modrinth_search(query, ctype, mc, loader, sort, offset=0, limit=20):
    facets = [[f"project_type:{CONTENT_TYPES[ctype][1]}"]]
    if mc:
        facets.append([f"versions:{mc}"])
    if loader and ctype == "mods":
        facets.append([f"categories:{loader}"])
    qs = urllib.parse.urlencode({"query": query, "facets": json.dumps(facets), "limit": limit,
                                 "offset": offset, "index": sort})
    st, js = http_request(f"{MR_API}/search?{qs}")
    if st != 200:
        raise RuntimeError(f"Modrinth: HTTP {st}")
    items = [dict(source="modrinth", id=h["project_id"], title=h.get("title", ""),
                  author=h.get("author", ""), desc=h.get("description", ""),
                  downloads=h.get("downloads", 0),
                  url=f"https://modrinth.com/{h.get('project_type', 'mod')}/{h.get('slug', '')}")
             for h in js.get("hits", [])]
    return items, int(js.get("total_hits", len(items)))


def modrinth_install(pid, ctype, mc, loader, dest, log, _seen=None, _depth=0):
    seen = _seen if _seen is not None else set()
    if pid in seen:
        return []
    seen.add(pid)
    params = {}
    if mc:
        params["game_versions"] = json.dumps([mc])
    if loader and ctype == "mods":
        params["loaders"] = json.dumps([loader])
    st, vers = http_request(f"{MR_API}/project/{pid}/version?" + urllib.parse.urlencode(params))
    if st != 200 or not isinstance(vers, list) or not vers:
        raise RuntimeError("Нет подходящей версии для вашей версии Minecraft / загрузчика")
    v = next((x for x in vers if x.get("version_type") == "release"), vers[0])
    files = v.get("files") or []
    f = next((x for x in files if x.get("primary")), files[0] if files else None)
    if not f:
        raise RuntimeError("В этой версии нет файлов")
    Path(dest).mkdir(parents=True, exist_ok=True)
    fetch(f["url"], Path(dest) / f["filename"], (f.get("hashes") or {}).get("sha1"), f.get("size"))
    done = [f["filename"]]
    log(f"Скачано: {f['filename']}")
    if _depth < 3 and ctype == "mods":
        for dep in v.get("dependencies") or []:
            if dep.get("dependency_type") != "required":
                continue
            dpid = dep.get("project_id")
            if not dpid and dep.get("version_id"):
                st2, dv = http_request(f"{MR_API}/version/{dep['version_id']}")
                dpid = dv.get("project_id") if st2 == 200 else None
            if dpid:
                try:
                    done += modrinth_install(dpid, ctype, mc, loader, dest, log, seen, _depth + 1)
                except Exception as e:
                    log(f"Зависимость {dpid}: {e}")
    return done


# ───────────────────────── подготовка и запуск ─────────────────────────

def prepare_and_launch(cfg, target, account, log, progress, manifest=None, on_start=None, on_account=None):
    global VERIFY
    VERIFY = bool(cfg.get("verify_files"))
    inst = target.get("instance")
    vid = target["version"]
    if inst:
        vid = ensure_loader(inst, manifest, cfg, log, progress)
    game_dir = resolve_game_dir(cfg, vid, inst)
    game_dir.mkdir(parents=True, exist_ok=True)

    auth = resolve_auth(account, cfg, log)
    if on_account:
        on_account()

    ram = int(cfg.get("ram", 2048))
    xms = int(cfg.get("xms", 512))
    vj = load_version(vid, manifest, log)

    jar_id = vj.get("_jar_id", vid)
    jar_path = VERSIONS / jar_id / f"{jar_id}.jar"
    cl = (vj.get("downloads") or {}).get("client")
    tasks = []
    if cl:
        tasks.append((cl["url"], jar_path, cl.get("sha1"), cl.get("size")))
    elif not jar_path.exists():
        raise RuntimeError("Нет клиентского jar для этой версии")

    lib_tasks, classpath, natives = collect_libraries(vj)
    tasks += lib_tasks

    aid, idx, asset_tasks = prepare_assets(vj)
    tasks += asset_tasks

    log_cfg = None
    lg = (vj.get("logging") or {}).get("client")
    if lg and lg.get("file"):
        f = lg["file"]
        lp = ASSETS / "log_configs" / f["id"]
        tasks.append((f["url"], lp, f.get("sha1"), f.get("size")))
        log_cfg = (lg["argument"], lp)

    log(f"Проверяю/скачиваю файлы: {len(tasks)}...")
    download_many(tasks, lambda c, t: progress(c, t, "Файлы игры"))
    materialize_legacy_assets(aid, idx, game_dir)

    progress(0, 1, "Java")
    java = ensure_java(vj, cfg, log, lambda c, t: progress(c, t, "Java"))
    jver = java_major(java) or (vj.get("javaVersion") or {}).get("majorVersion", 8)

    ndir = VERSIONS / vid / "natives"
    extract_natives(natives, ndir)

    flags = jvm_flags(cfg, jver, log)
    if auth.get("agent"):
        flags = [auth["agent"]] + flags
    extra_game = []
    if cfg.get("custom_res"):
        extra_game += ["--width", str(int(cfg.get("width", 1280))),
                       "--height", str(int(cfg.get("height", 720)))]
    if cfg.get("fullscreen"):
        extra_game.append("--fullscreen")
    if (cfg.get("game_args") or "").strip():
        extra_game += cfg["game_args"].split()

    cmd = build_command(vj, java, classpath, ndir, ram, aid, game_dir, auth, log_cfg,
                        xms_mb=xms, extra_jvm=flags, extra_game=extra_game)
    label = FLAG_PRESETS.get(cfg.get("preset", "aikar"), FLAG_PRESETS["aikar"])[0]
    log(f"Версия {vid}, Java {jver}, память {xms}–{ram} МБ, профиль: {label}")
    log(f"Папка игры: {game_dir}")
    if cfg.get("show_cmd"):
        tok = auth["token"]
        shown = [a.replace(tok, "***") if tok and tok != "0" else a for a in cmd]
        log("Команда запуска: " + " ".join(shown))

    flags_win = 0
    if IS_WIN:
        flags_win = CREATE_NO_WINDOW | (HIGH_PRIORITY_CLASS if cfg.get("high_priority") else 0)

    BASE.mkdir(parents=True, exist_ok=True)
    if cfg.get("close_on_start"):
        out = open(LAST_LOG, "w", encoding="utf-8", errors="replace")
        subprocess.Popen(cmd, cwd=str(game_dir), stdout=out, stderr=subprocess.STDOUT,
                         creationflags=flags_win)
        if on_start:
            on_start()
        return 0

    proc = subprocess.Popen(
        cmd, cwd=str(game_dir), stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, errors="replace", creationflags=flags_win)
    if on_start:
        on_start()
    with open(LAST_LOG, "w", encoding="utf-8", errors="replace") as lf:
        for line in proc.stdout:
            log(line.rstrip())
            lf.write(line)
    code = proc.wait()
    log(f"Игра завершилась с кодом {code}")
    return code



# ═════════════════════════════ ИНТЕРФЕЙС ═════════════════════════════

C = {
    "bg0": "#0a0914",     # самый тёмный (боковая панель, нижняя панель)
    "bg1": "#110f1f",     # основной фон страниц
    "bg2": "#201d38",     # выделение
    "hover": "#171428",
    "card": "#171529",    # карточки
    "input": "#0c0b17",
    "line": "#2c2848",
    "text": "#f4f2fc",
    "muted": "#9d98b8",
    "yellow": "#ffb020",      # акцентный цвет
    "yellow_hi": "#ffd36b",
    "yellow_lo": "#e8890c",
    "orange": "#ff8a1f",
    "brown": "#1a1205",       # текст на акцентной кнопке
    "red": "#ff7b7b",
    "green": "#6ee7a0",
    "violet": "#9d7bff",
    "violet_lo": "#3a2f78",
    "pink": "#ff6fae",
    "blue": "#55c6ff",
}
FONT = "Segoe UI" if IS_WIN else "Helvetica"


def _load_pixel_font():
    """Подключает Monocraft.ttf (лежит рядом с лаунчером) для кнопок. Windows: без установки в систему."""
    path = resource_path("Monocraft.ttf")
    if not os.path.exists(path):
        return FONT
    try:
        if IS_WIN:
            import ctypes
            # FR_PRIVATE = 0x10: шрифт виден только этому процессу
            if ctypes.windll.gdi32.AddFontResourceExW(path, 0x10, 0):
                return "Monocraft"
        elif OS_NAME == "linux":
            d = Path.home() / ".local" / "share" / "fonts"
            d.mkdir(parents=True, exist_ok=True)
            dst = d / "Monocraft.ttf"
            if not dst.exists():
                shutil.copyfile(path, dst)
                subprocess.run(["fc-cache", "-f", str(d)], timeout=20)
            return "Monocraft"
    except Exception:
        pass
    return FONT


BTN_FONT = _load_pixel_font()


def font(size=10, bold=False):
    """Размер задан в «точках», но переводится в пиксели — интерфейс не плывёт при разном DPI."""
    return (FONT, -int(round(size * 1.35)), "bold" if bold else "normal")


STOPS = [(255, 226, 0), (255, 178, 0), (255, 124, 0), (255, 76, 0),
         (255, 124, 0), (255, 178, 0), (255, 226, 0)]


def grad_color(t):
    t %= 1.0
    x = t * (len(STOPS) - 1)
    i = int(x)
    f = x - i
    a = STOPS[i]
    b = STOPS[min(i + 1, len(STOPS) - 1)]
    return "#%02x%02x%02x" % tuple(int(a[k] + (b[k] - a[k]) * f) for k in range(3))


if tk is not None:

    def _mix(c1, c2, t):
        """Смешать два цвета #rrggbb (t от 0 до 1)."""
        a = [int(c1[i:i + 2], 16) for i in (1, 3, 5)]
        b = [int(c2[i:i + 2], 16) for i in (1, 3, 5)]
        return "#%02x%02x%02x" % tuple(int(a[k] + (b[k] - a[k]) * t) for k in range(3))

    def _ease(t):
        return 1 - (1 - t) ** 3

    def _rrect(cv, x0, y0, x1, y1, r, **kw):
        r = max(0, min(r, (x1 - x0) / 2, (y1 - y0) / 2))
        pts = [x0 + r, y0, x1 - r, y0, x1, y0, x1, y0 + r, x1, y1 - r, x1, y1, x1 - r, y1,
               x0 + r, y1, x0, y1, x0, y1 - r, x0, y0 + r, x0, y0]
        return cv.create_polygon(pts, smooth=True, splinesteps=12, **kw)

    def _grad_rrect(cv, x0, y0, x1, y1, r, c_top, c_bot):
        """Скруглённый прямоугольник с вертикальным градиентом (построчно)."""
        h = max(1, int(y1 - y0))
        r = max(0, min(r, h / 2, (x1 - x0) / 2))
        for i in range(h):
            t = i / max(1, h - 1)
            d = r - i if i < r else (i - (h - 1 - r) if i > h - 1 - r else 0)
            inset = (r - math.sqrt(max(0.0, r * r - d * d))) if d > 0 else 0
            cv.create_line(x0 + inset, y0 + i, x1 - inset, y0 + i, fill=_mix(c_top, c_bot, t))

    def _nice(text):
        """ВЕРХНИЙ РЕГИСТР → обычный (Первая буква заглавная, латинские слова с заглавной)."""
        if not text or not text.isupper():
            return text
        out, first = [], True
        for w in text.lower().split(" "):
            if w and all(ord(ch) < 128 for ch in w) and any(ch.isalpha() for ch in w):
                w = w[:1].upper() + w[1:] if not w.startswith("+") else w
                if w.startswith("."):
                    w = w
            out.append(w)
        s = " ".join(out)
        for i, ch in enumerate(s):
            if ch.isalpha():
                s = s[:i] + ch.upper() + s[i + 1:]
                break
        return s

    class GradHub:
        """Один общий анимированный градиент (жёлтый ↔ оранжевый) для области страниц.
        Контейнеры GradFrame рисуют каждый свой кусок этого градиента, поэтому карточки и кнопки
        «лежат» на нём, а промежутки между ними прозрачны."""
        C1, C2, BANDS = "#ffd23f", "#ff7a14", 64

        def __init__(self):
            self.origin = None      # хост страниц: начало координат градиента
            self.src = None         # объект с .enabled и .step (настройки анимации)
            self.root = None
            self.t = 0.0
            self.colors = []
            self.frames = []
            self.followers = []
            self.active = set()     # страницы, которые сейчас видны
            self._ra = None
            self._n = 0
            self._recolor()

        def start(self, root, src):
            self.root, self.src = root, src
            root.after(60, self._tick)

        def mid(self):
            return _mix(self.C1, self.C2, 0.5)

        def _recolor(self):
            n = self.BANDS
            self.colors = [_mix(self.C1, self.C2, 0.5 + 0.5 * math.sin(self.t + i * 2 * math.pi / n))
                           for i in range(n)]

        def geo(self):
            o = self.origin
            W = max(1, o.winfo_width()) if o else 1
            H = max(1, o.winfo_height()) if o else 1
            slant = H * 0.45
            return W, H, slant, (W + slant) / self.BANDS

        def color_at(self, x, y):
            W, H, slant, sw = self.geo()
            i = int((x + slant * y / H) / sw)
            return self.colors[min(self.BANDS - 1, max(0, i))]

        def page_of(self, w):
            o = self.origin
            while w is not None and w.master is not o:
                w = w.master
            return w

        def follow(self, w, fn):
            rec = (w, fn, self.page_of(w))
            self.followers.append(rec)

            def drop(e, r=rec):
                if e.widget is w and r in self.followers:
                    self.followers.remove(r)
            w.bind("<Destroy>", drop, add="+")

        def _follow_tick(self):
            o = self.origin
            if o is None:
                return
            orx, ory = o.winfo_rootx(), o.winfo_rooty()
            for w, fn, pg in list(self.followers):
                if pg not in self.active:
                    continue
                try:
                    ox, oy = w.winfo_rootx() - orx, w.winfo_rooty() - ory
                    fn(lambda x, y: self.color_at(ox + x, oy + y))
                except tk.TclError:
                    pass

        def realign_soon(self):
            if self._ra is None and self.root is not None:
                self._ra = self.root.after_idle(self._realign)

        def _realign(self, pages=None):
            self._ra = None
            try:
                pg = pages or self.active
                for f in list(self.frames):
                    if f._page in pg:
                        f.layout()
                self._follow_tick()
            except tk.TclError:
                pass

        def _tick(self):
            try:
                if self.active and getattr(self.src, "enabled", True):
                    self.t += getattr(self.src, "step", 0.0035) * 9 * 2
                    self._recolor()
                    for f in self.frames:
                        if f._page in self.active:
                            f.paint()
                    self._follow_tick()
            except tk.TclError:
                pass
            finally:
                try:
                    self.root.after(90, self._tick)
                except tk.TclError:
                    pass

    HUB = GradHub()

    class GradFrame(tk.Canvas):
        """«Прозрачный» контейнер: рисует свой кусок общего градиента. Дочерние виджеты кладём как в Frame."""

        def __init__(self, master, **kw):
            for k in ("bg", "padx", "pady"):
                kw.pop(k, None)
            kw.setdefault("highlightthickness", 0)
            kw.setdefault("bd", 0)
            super().__init__(master, bg=HUB.mid(), **kw)
            self._items = []
            self._page = HUB.page_of(self)
            HUB.frames.append(self)
            self.bind("<Configure>", lambda e: HUB.realign_soon(), add="+")
            self.bind("<Destroy>", self._gone, add="+")

        def _gone(self, e):
            if e.widget is self and self in HUB.frames:
                HUB.frames.remove(self)

        def paint(self):
            c = HUB.colors
            for rec in self._items:           # перекрашиваем только те полосы, цвет которых изменился
                col = c[rec[1]]
                if col != rec[2]:
                    rec[2] = col
                    self.itemconfigure(rec[0], fill=col)

        def layout(self):
            o = HUB.origin
            w, h = self.winfo_width(), self.winfo_height()
            if o is None or w < 4 or h < 4 or o.winfo_width() < 20:
                return
            W, H, slant, sw = HUB.geo()
            dx, dy = self.winfo_rootx() - o.winfo_rootx(), self.winfo_rooty() - o.winfo_rooty()
            key = (dx, dy, w, h, int(self.canvasx(0)), int(self.canvasy(0)), W, H)
            if key == getattr(self, "_key", None) and self._items:
                return                                   # положение не менялось — не пересобираем полосы
            self._key = key
            self.delete("band")
            self._items = []
            ax, ay = self.canvasx(0) - dx, self.canvasy(0) - dy      # origin → координаты холста
            lo = max(0, int((dx + slant * dy / H) / sw) - 1)
            hi = min(HUB.BANDS - 1, int((dx + w + slant * (dy + h) / H) / sw) + 1)
            for i in range(lo, hi + 1):
                x = i * sw
                it = self.create_polygon(x + ax, ay, x + sw + 1 + ax, ay, x - slant + sw + 1 + ax, H + ay,
                                         x - slant + ax, H + ay, fill=HUB.colors[i], outline="", tags="band")
                self._items.append([it, i, HUB.colors[i]])
            self.tag_lower("band")

    class GradLabel(tk.Label):
        """Подпись прямо на градиенте: фон подстраивается под градиент, текст тёмный."""

        def __init__(self, master, **kw):
            kw["bg"] = HUB.mid()
            kw["fg"] = C["brown"]
            super().__init__(master, **kw)
            HUB.follow(self, lambda at: self.configure(bg=at(self.winfo_width() // 2, self.winfo_height() // 2)))

    class AnimatedBG(tk.Canvas):
        """Окно за панелью: тёмный фон. Хранит настройки анимации (enabled / step) — их читает GradHub."""

        def __init__(self, master):
            super().__init__(master, highlightthickness=0, bd=0, bg=C["bg0"])
            self.enabled = True
            self.step = 0.0035

    # ───────── пиксель-арт иконки 12×12 ─────────
    # X — основной цвет (края получают объём автоматически: светлая кромка сверху, тёмная снизу/справа),
    # h — светлый пиксель, o — тёмный пиксель, f — огонь (оранжевый), «.» — пусто.
    PX_ICONS = {
        "play": ["............", "..X.........", "..XXX.......", "..XXXXX.....", "..XXXXXXX...", "..XXXXXXXXX.",
                 "..XXXXXXXXX.", "..XXXXXXX...", "..XXXXX.....", "..XXX.......", "..X.........", "............"],
        "cube": ["............", "XXXXXXXXXXXX", "XhXhXXhXXhXX", "XXXXXXXXXXXX", "XoXXXXoXXXoX", "XXXXoXXXXXXX",
                 "XXXXXXXXoXXX", "XoXXXXXXXXXo", "XXXXXoXXXXXX", "XXoXXXXXoXXX", "XXXXXXXXXXXX", "............"],
        "plus": ["............", "....XXXX....", "....XXXX....", "....XXXX....", ".XXXXXXXXXX.", ".XXXXXXXXXX.",
                 ".XXXXXXXXXX.", ".XXXXXXXXXX.", "....XXXX....", "....XXXX....", "....XXXX....", "............"],
        "head": ["............", ".oooooooooo.", ".oooooooooo.", ".oXXXXXXXXo.", ".XXXXXXXXXX.", ".XhoXXXXohX.",
                 ".XXXXXXXXXX.", ".XXXXooXXXX.", ".XXXooooXXX.", ".XXXXXXXXXX.", "..XXXXXXXX..", "............"],
        "gear": [".....XX.....", "..XX.XX.XX..", "..XXXXXXXX..", "...XXXXXX...", ".XXXX..XXXX.", "XXXXX..XXXXX",
                 "XXXXX..XXXXX", ".XXXX..XXXX.", "...XXXXXX...", "..XXXXXXXX..", "..XX.XX.XX..", ".....XX....."],
        "list": ["............", "............", "XX..XXXXXXXX", "XX..XXXXXXXX", "............", "XX..XXXXXXXX",
                 "XX..XXXXXXXX", "............", "XX..XXXXXXXX", "XX..XXXXXXXX", "............", "............"],
        "chip": ["............", "..X.X.X.X.X.", "..X.X.X.X.X.", ".XXXXXXXXXX.", ".XXXXXXXXXX.", ".XXooooooXX.",
                 ".XXooooooXX.", ".XXXXXXXXXX.", ".XXXXXXXXXX.", "..X.X.X.X.X.", "..X.X.X.X.X.", "............"],
        "cup": ["............", "...X..X.....", "....X..X....", "............", ".XXXXXXXX...", ".XXXXXXXX.XX",
                ".XXXXXXXX..X", ".XXXXXXXX..X", "..XXXXXX.XX.", "..XXXXXX....", ".XXXXXXXX...", "............"],
        "flag": ["............", ".XXXXXX.....", ".XXXXXXXX...", ".XXXXXXXXXX.", ".XXXXXXXX...", ".XXXXXX.....",
                 ".X..........", ".X..........", ".X..........", ".X..........", "XXX.........", "............"],
        "folder": ["............", "............", ".XXXX.......", "XXXXXXXXXXX.", "XXXXXXXXXXXX", "XXXXXXXXXXXX",
                   "XXXXXXXXXXXX", "XXXXXXXXXXXX", "XXXXXXXXXXXX", "XXXXXXXXXXXX", "XXXXXXXXXXXX", "............"],
        "gamepad": ["............", "............", "............", ".XXXXXXXXXX.", "XXXXXXXXXXXX", "XXoXXXXXXoXX",
                    "XoooXXXXXXoX", "XXoXXXXXXXXX", "XXXXXXXXXXXX", ".XXX....XXX.", "..XX....XX..", "............"],
        "rocket": [".....XX.....", "....XXXX....", "...XXXXXX...", "...XXooXX...", "...XXooXX...", "...XXXXXX...",
                   "..XXXXXXXX..", ".XXXXXXXXXX.", ".XX.XXXX.XX.", ".X...ff...X.", ".....ff.....", "............"],
        "plug": ["............", "............", "...X..X.....", "...X..X.....", "..XXXXXX....", "..XXXXXX....",
                 "..XXXXXX....", "...XXXX.....", "....XX......", "....XX......", "....XX......", "............"],
        "download": ["............", ".....XX.....", ".....XX.....", ".....XX.....", "..XXXXXXXX..", "...XXXXXX...",
                     "....XXXX....", ".....XX.....", "............", "XX........XX", "XXXXXXXXXXXX", "............"],
        "search": ["............", "...XXXXX....", "..XX...XX...", ".XX.....XX..", ".X.......X..", ".XX.....XX..",
                   "..XX...XXX..", "...XXXXX.XX.", ".........XXX", "..........XX", "............", "............"],
        "bolt": ["............", ".......XXX..", "......XXX...", ".....XXX....", "....XXX.....", "...XXXXXXX..",
                 ".....XXX....", "....XXX.....", "...XXX......", "..XXX.......", ".XX.........", "............"],
        "bulb": ["............", "....XXXX....", "...XXXXXX...", "..XXXXXXXX..", "..XXXXXXXX..", "..XXXXXXXX..",
                 "...XXXXXX...", "....XXXX....", "....oooo....", "....XXXX....", ".....XX.....", "............"],
        "trash": ["............", "....XXXX....", ".XXXXXXXXXX.", "............", "..XXXXXXXX..", "..XXoXXoXX..",
                  "..XXoXXoXX..", "..XXoXXoXX..", "..XXoXXoXX..", "..XXXXXXXX..", "............", "............"],
        "copy": ["............", ".XXXXXXX....", ".X.....X....", ".X..XXXXXXX.", ".X..X.....X.", ".X..X.....X.",
                 ".X..X.....X.", ".XXXX.....X.", "....X.....X.", "....XXXXXXX.", "............", "............"],
    }
    PX_FIXED = {"f": "#ff9a2e"}
    _PX_CACHE = {}

    def _px_grid(name):
        """Сетка 12×12 с автоматической «объёмной» подсветкой краёв."""
        rows = [r.ljust(12, ".")[:12] for r in PX_ICONS[name]]
        rows += ["." * 12] * (12 - len(rows))
        out = []
        for y in range(12):
            line = []
            for x in range(12):
                ch = rows[y][x]
                if ch == "X":
                    up = rows[y - 1][x] if y > 0 else "."
                    dn = rows[y + 1][x] if y < 11 else "."
                    rt = rows[y][x + 1] if x < 11 else "."
                    if up == ".":
                        ch = "h"
                    elif dn == "." or rt == ".":
                        ch = "o"
                line.append(ch)
            out.append(line)
        return out

    def px_icon(name, color, scale=2):
        """PhotoImage с пиксель-артом. Кэшируется по (имя, цвет, масштаб)."""
        key = (name, color, scale)
        img = _PX_CACHE.get(key)
        if img is None:
            hi, lo = _mix(color, "#ffffff", 0.42), _mix(color, "#000000", 0.38)
            pal = {"X": color, "h": hi, "o": lo, **PX_FIXED}
            img = tk.PhotoImage(width=12 * scale, height=12 * scale)
            for y, line in enumerate(_px_grid(name)):
                x = 0
                while x < 12:
                    ch = line[x]
                    if ch == ".":
                        x += 1
                        continue
                    x2 = x
                    while x2 + 1 < 12 and line[x2 + 1] == ch:
                        x2 += 1
                    img.put(pal[ch], to=(x * scale, y * scale, (x2 + 1) * scale, (y + 1) * scale))
                    x = x2 + 1
            _PX_CACHE[key] = img
        return img

    # ───────── выпадающий список в стиле лаунчера ─────────
    def _px_shape(x0, y0, x1, y1, n):
        return [x0, y0 + n, x0 + n, y0 + n, x0 + n, y0, x1 - n, y0, x1 - n, y0 + n, x1, y0 + n,
                x1, y1 - n, x1 - n, y1 - n, x1 - n, y1, x0 + n, y1, x0 + n, y1 - n, x0, y1 - n]

    def _select_font(fs):
        return (BTN_FONT, -max(11, round(fs * 1.3)), "normal")

    class SelectPopup(tk.Toplevel):
        """Меню выбора: пиксельная рамка, строки Monocraft, подсветка жёлтым, прокрутка колесом."""
        ROW, MAXROWS, KEY = 30, 9, "#010203"

        def __init__(self, owner):
            super().__init__(owner)
            self.owner = owner
            self.vals = list(owner._values)
            self.fnt = owner._fnt
            self.rows = max(1, min(len(self.vals), self.MAXROWS))
            cur = owner.get()
            self.sel = self.vals.index(cur) if cur in self.vals else -1
            self.hov = self.sel
            self.top = max(0, min(max(0, self.sel - self.rows // 2), max(0, len(self.vals) - self.rows)))
            self._prev_grab = None
            try:
                self._prev_grab = owner.grab_current()
            except Exception:
                pass
            self.overrideredirect(True)
            try:
                self.attributes("-topmost", True)
                if IS_WIN:
                    self.attributes("-transparentcolor", self.KEY)   # срезанные углы реально прозрачные
            except tk.TclError:
                pass
            self.configure(bg=self.KEY)
            self.w = max(owner.winfo_width(), 170)
            self.h = self.rows * self.ROW + 14
            x, y = owner.winfo_rootx(), owner.winfo_rooty() + owner.winfo_height() + 2
            if y + self.h > self.winfo_screenheight() - 48:
                y = owner.winfo_rooty() - self.h - 2
            self.geometry(f"{self.w}x{self.h}+{x}+{y}")
            self.cv = tk.Canvas(self, width=self.w, height=self.h, bg=self.KEY, highlightthickness=0, bd=0)
            self.cv.pack(fill="both", expand=True)
            self.cv.bind("<Motion>", self._motion)
            self.cv.bind("<Button-1>", self._click)
            self.cv.bind("<MouseWheel>", self._wheel)
            self.cv.bind("<Button-4>", lambda e: self._scroll(-1))
            self.cv.bind("<Button-5>", lambda e: self._scroll(1))
            for k in ("<Up>", "<Down>", "<Return>", "<Escape>"):
                self.bind(k, self._key)
            self.bind("<FocusOut>", lambda e: self.after(60, self._focus_check))
            self.draw()
            self.after(10, self._grab)

        def _grab(self):
            try:
                self.grab_set()
                self.focus_force()
            except tk.TclError:
                pass

        def _focus_check(self):
            try:
                if self.focus_displayof() is None:
                    self.close()
            except Exception:
                pass

        def _index_at(self, y):
            i = (y - 7) // self.ROW + self.top
            return i if 0 <= i < len(self.vals) and self.top <= i < self.top + self.rows else -1

        def _motion(self, e):
            i = self._index_at(e.y)
            if i != self.hov:
                self.hov = i
                self.draw()

        def _click(self, e):
            inside = self.winfo_rootx() <= e.x_root < self.winfo_rootx() + self.w and \
                self.winfo_rooty() <= e.y_root < self.winfo_rooty() + self.h
            if not inside:
                self.close()
                return
            i = self._index_at(e.y)
            if i >= 0:
                self.pick(i)

        def _scroll(self, d):
            top = max(0, min(self.top + d, max(0, len(self.vals) - self.rows)))
            if top != self.top:
                self.top = top
                self.draw()

        def _wheel(self, e):
            self._scroll(-1 if e.delta > 0 else 1)
            return "break"

        def _key(self, e):
            if e.keysym == "Escape":
                self.close()
            elif e.keysym in ("Up", "Down") and self.vals:
                i = (self.hov if self.hov >= 0 else self.sel) + (-1 if e.keysym == "Up" else 1)
                self.hov = max(0, min(len(self.vals) - 1, i))
                if self.hov < self.top:
                    self.top = self.hov
                elif self.hov >= self.top + self.rows:
                    self.top = self.hov - self.rows + 1
                self.draw()
            elif e.keysym == "Return" and self.hov >= 0:
                self.pick(self.hov)

        def pick(self, i):
            self.owner._choose(self.vals[i])
            self.close()

        def close(self):
            o = self.owner
            try:
                self.grab_release()
            except tk.TclError:
                pass
            try:
                self.destroy()
            except tk.TclError:
                pass
            o._popup = None
            try:
                o._draw()
                if self._prev_grab is not None:
                    self._prev_grab.grab_set()
            except Exception:
                pass

        def draw(self):
            cv, w, h, n = self.cv, self.w, self.h, 4
            cv.delete("all")
            edge = C["yellow"]
            cv.create_polygon(_px_shape(0, 0, w, h, n), fill=edge, outline="")
            cv.create_polygon(_px_shape(2, 2, w - 2, h - 2, n - 1), fill=C["input"], outline="")
            cv.create_rectangle(n, h - 5, w - n, h - 2, fill=_mix(C["input"], "#000000", 0.5), outline="")
            for r in range(self.rows):
                i = self.top + r
                if i >= len(self.vals):
                    break
                y0 = 7 + r * self.ROW
                on = i == self.hov
                if on:
                    cv.create_polygon(_px_shape(7, y0, w - 7 - (6 if len(self.vals) > self.rows else 0), y0 + self.ROW - 2, 3),
                                      fill=C["yellow"], outline="")
                    cv.create_rectangle(10, y0, w - 10 - (6 if len(self.vals) > self.rows else 0), y0 + 2,
                                        fill=C["yellow_hi"], outline="")
                fg = C["brown"] if on else C["text"]
                cv.create_text(16, y0 + self.ROW // 2 - 1, text=self.vals[i], anchor="w", fill=fg, font=self.fnt)
                if i == self.sel:                       # пиксельная «галочка» у выбранного
                    cx, cy = w - 24 - (6 if len(self.vals) > self.rows else 0), y0 + self.ROW // 2
                    col = C["brown"] if on else C["yellow"]
                    for dx, dy in ((0, 0), (2, 2), (4, 4), (6, 2), (8, 0), (10, -2)):
                        cv.create_rectangle(cx + dx, cy + dy - 2, cx + dx + 3, cy + dy + 1, fill=col, outline="")
            if len(self.vals) > self.rows:               # пиксельный скроллбар
                th = h - 14
                bh = max(16, int(th * self.rows / len(self.vals)))
                by = 7 + int((th - bh) * self.top / max(1, len(self.vals) - self.rows))
                cv.create_rectangle(w - 11, 7, w - 7, 7 + th, fill=_mix(C["input"], "#000000", 0.45), outline="")
                cv.create_rectangle(w - 11, by, w - 7, by + bh, fill=C["yellow"], outline="")

    class PixelSelect(tk.Canvas):
        """Замена ttk.Combobox: тот же интерфейс (get/set/['values']/<<ComboboxSelected>>), но в пиксельном стиле."""

        def __init__(self, master, textvariable=None, values=(), width=14, fs=10, bg=None, **ignored):
            self._fnt = _select_font(ignored.get("fs_font", fs))
            cw = tkfont.Font(font=self._fnt).measure("0")
            w = max(96, cw * int(width) + 60)
            h = 36 if fs >= 11 else 34
            super().__init__(master, width=w, height=h, bg=bg or master.cget("bg"),
                             highlightthickness=0, bd=0, cursor="hand2")
            self._var = textvariable if textvariable is not None else tk.StringVar(master=self)
            self._values = list(values)
            self._hover = False
            self._popup = None
            self._var.trace_add("write", lambda *a: self._draw())
            self.bind("<Configure>", lambda e: self._draw())
            self.bind("<Enter>", lambda e: self._set_hover(True))
            self.bind("<Leave>", lambda e: self._set_hover(False))
            self.bind("<Button-1>", lambda e: self.toggle())
            if bg is None and isinstance(master, GradFrame):
                HUB.follow(self, lambda at: self.configure(bg=at(self.winfo_width() // 2, self.winfo_height() // 2)))

        # ── интерфейс как у Combobox ──
        def get(self):
            return self._var.get()

        def set(self, v):
            self._var.set(v)

        def __setitem__(self, key, val):
            if key == "values":
                self._values = list(val)
                self._draw()
            else:
                super().__setitem__(key, val)

        def __getitem__(self, key):
            return tuple(self._values) if key == "values" else super().__getitem__(key)

        def configure(self, cnf=None, **kw):
            cnf = dict(cnf or {}, **kw)
            redraw = False
            if "values" in cnf:
                self._values = list(cnf.pop("values"))
                redraw = True
            cnf.pop("state", None)
            if cnf:
                super().configure(**cnf)
            if redraw:
                self._draw()

        config = configure

        # ── поведение ──
        def _set_hover(self, v):
            self._hover = v
            self._draw()

        def toggle(self):
            if self._popup is not None:
                self._popup.close()
                return
            if not self._values:
                return
            self._popup = SelectPopup(self)
            self._draw()

        def _choose(self, val):
            self._var.set(val)
            self.event_generate("<<ComboboxSelected>>")

        def _draw(self):
            try:
                if not self.winfo_exists():
                    return
                w, h = self.winfo_width(), self.winfo_height()
                if w < 30:
                    w, h = int(self.cget("width")), int(self.cget("height"))
            except tk.TclError:
                return
            self.delete("all")
            n = 3
            opened = self._popup is not None
            edge = C["yellow"] if (opened or self._hover) else C["line"]
            fill = _mix(C["input"], "#ffffff", 0.05 if self._hover else 0.0)
            self.create_polygon(_px_shape(0, 0, w, h, n), fill=fill, outline="")
            self.create_rectangle(n, 0, w - n, 2, fill=_mix(edge, "#ffffff", 0.12), outline="")
            self.create_rectangle(n, h - 3, w - n, h, fill=_mix(fill, "#000000", 0.5), outline="")
            self.create_rectangle(0, n, 2, h - n, fill=edge, outline="")
            self.create_rectangle(w - 2, n, w, h - n, fill=edge, outline="")
            # текст (обрезаем «…», если не помещается)
            txt = str(self._var.get())
            fo = tkfont.Font(font=self._fnt)
            room = w - 14 - 38
            if fo.measure(txt) > room:
                while txt and fo.measure(txt + "…") > room:
                    txt = txt[:-1]
                txt += "…"
            self.create_text(12, h // 2 - 1, text=txt, anchor="w", fill=C["text"], font=self._fnt)
            # пиксельная стрелка
            ax, ay, cell = w - 26, h // 2, 2
            rows = (7, 5, 3, 1)
            if opened:
                rows = rows[::-1]
            col = C["yellow"]
            for r, cnt in enumerate(rows):
                x0 = ax + (7 - cnt) * cell // 2
                self.create_rectangle(x0, ay - 4 + r * cell, x0 + cnt * cell, ay - 4 + (r + 1) * cell, fill=col, outline="")
            self.create_rectangle(w - 34, 8, w - 32, h - 8, fill=_mix(edge, "#000000", 0.3), outline="")  # разделитель

    _BTN_ICONS = (("играть", "play"), ("выбрать для", "play"), ("скача", "download"), ("устан", "download"),
                  ("бэкап", "download"), ("импорт", "download"), ("иска", "search"), ("поиск", "search"),
                  ("созда", "plus"), ("добав", "plus"), ("ещё", "plus"), ("моды", "plus"), ("удал", "trash"),
                  ("очист", "trash"), ("копи", "copy"), ("папка", "folder"), ("открыть папку", "folder"),
                  ("обзор", "folder"), ("настройки", "gear"), ("сборки", "cube"), ("авто", "bolt"))

    def _btn_icon_name(label):
        t = str(label).casefold().strip()
        for pre, ic in _BTN_ICONS:
            if t.startswith(pre):
                return ic
        return None

    def _on_page(w):
        """True, если виджет не на странице (боковая панель, нижняя панель) или его страница сейчас показана.
        Нужен, чтобы скрытые страницы не тратили время на анимацию."""
        pg = getattr(w, "_pgc", None)
        if pg is None:
            pg = w._pgc = HUB.page_of(w) or False
        return pg is False or pg in HUB.active

    _FONT_CACHE = {}

    def _fit_font(label, size, maxw):
        """Шрифт кнопки подбирается один раз для (текст, размер, ширина) — раньше создавался при каждой перерисовке."""
        key = (label, size, maxw)
        f = _FONT_CACHE.get(key)
        if f is None:
            while True:
                f = (BTN_FONT, -size, "normal")
                obj = _FONT_CACHE.get(("obj", size))
                if obj is None:
                    obj = _FONT_CACHE[("obj", size)] = tkfont.Font(font=f)
                if size <= 8 or obj.measure(label) <= maxw:
                    break
                size -= 1
            _FONT_CACHE[key] = f
        return f

    class AnimButton(tk.Canvas):
        """Пиксельная кнопка (форма как у «Скачать» на сайте), цвета лаунчера: жёлтая главная и тёмная обычная."""
        PRIMARY = ("ИГРАТЬ", "ВОЙТИ", "СОЗДАТЬ", "ИСКАТЬ", "СКАЧАТЬ", "ДОБАВИТЬ", "ИСПОЛЬЗОВАТЬ",
                   "СОХРАНИТЬ И ВОЙТИ", "ВЫБРАТЬ ДЛЯ ИГРЫ", "ВКЛЮЧИТЬ")

        def __init__(self, master, text, command, width=170, height=40, fs=11, bg=None, kind=None, pulse=False):
            super().__init__(master, width=width, height=height, bg=bg or master.cget("bg"),
                             highlightthickness=0, bd=0, cursor="hand2")
            self._label = text
            self._cmd = command
            self._bw, self._bh, self._fs = width, height, fs
            self._primary = (kind == "primary") if kind else (text.upper() in self.PRIMARY)
            self._pulse = pulse
            self._hover = self._down = False
            self._enabled = True
            if bg is None and isinstance(master, GradFrame):
                HUB.follow(self, lambda at: self.configure(bg=at(self._bw // 2, self._bh // 2)))
            self._h = 0.0          # 0..1 — степень «наведения»
            self._tgt = 0.0
            self._ph = 0.0
            self._job = None
            self.bind("<Enter>", lambda e: self._go(1.0, hover=True))
            self.bind("<Leave>", lambda e: self._go(0.0, hover=False, down=False))
            self.bind("<ButtonPress-1>", self._press)
            self.bind("<ButtonRelease-1>", self._release)
            self._draw()
            if pulse:
                self.after(90, self._pulse_tick)

        def _go(self, tgt, **kw):
            self._hover = kw.get("hover", self._hover)
            self._down = kw.get("down", self._down)
            self._tgt = tgt
            if self._job is None:
                self._anim()

        def _anim(self):
            d = self._tgt - self._h
            if abs(d) < 0.02:
                self._h = self._tgt
                self._job = None
            else:
                self._h += d * 0.3
                self._job = self.after(16, self._anim)
            self._draw()

        def _press(self, e):
            if self._enabled:
                self._down = True
                self._draw()

        def _release(self, e):
            was = self._down
            self._down = False
            self._draw()
            if was and self._enabled and self._hover and self._cmd:
                self._cmd()

        def set_text(self, text):
            self._label = text
            self._draw()

        def set_enabled(self, flag):
            self._enabled = bool(flag)
            self.configure(cursor="hand2" if flag else "arrow")
            self._draw()

        def _pulse_tick(self):
            try:
                if self._enabled and _on_page(self) and self.winfo_viewable():
                    self._ph += 0.22
                    self._draw()
            finally:
                self.after(100, self._pulse_tick)

        def _draw(self):
            """Форма как у кнопки «Скачать» на сайте: срезанные ступенчатые углы, светлая кромка сверху,
            тёмная плашка снизу. Цвета — родные цвета лаунчера. Размеры пропорциональны высоте кнопки."""
            w, h = self._bw, self._bh
            bgc = self.cget("bg")
            self.delete("all")
            hov = self._h if self._enabled else 0.0
            if self._primary:
                fill, hi, lo, fg = C["yellow"], C["yellow_hi"], C["yellow_lo"], C["brown"]
                side = lo
                if not self._enabled:
                    fill = _mix(C["bg2"], C["muted"], 0.18)
                    hi, lo, fg = _mix(fill, "#ffffff", 0.10), _mix(fill, "#000000", 0.30), C["muted"]
                    side = lo
            else:
                fill = _mix(C["bg2"], "#ffffff", 0.05)
                edge = _mix(C["line"], C["violet"], hov)
                hi, side, lo, fg = _mix(edge, "#ffffff", 0.12), edge, _mix(C["bg2"], "#000000", 0.45), C["text"]
                if not self._enabled:
                    fill = C["bg2"]
                    hi = side = C["line"]
                    fg = C["muted"]
            if self._enabled:   # плавное осветление при наведении
                fill, hi, lo = (_mix(c, "#ffffff", 0.14 * hov) for c in (fill, hi, lo))
                if self._primary:
                    side = lo
            inset = 2 if (self._down and self._enabled) else 0
            m = 3                                           # поле под свечение главной кнопки
            if self._primary and self._enabled and self._pulse and not self._hover:
                g = (math.sin(self._ph) + 1) / 2
                glow = _mix(bgc, C["yellow"], 0.08 + 0.20 * g)
            elif self._primary and self._enabled and self._h > 0:
                glow = _mix(bgc, C["yellow"], 0.26 * self._h)
            else:
                glow = None

            def shape(x0, y0, x1, y1, n):
                return [x0, y0 + n, x0 + n, y0 + n, x0 + n, y0, x1 - n, y0, x1 - n, y0 + n, x1, y0 + n,
                        x1, y1 - n, x1 - n, y1 - n, x1 - n, y1, x0 + n, y1, x0 + n, y1 - n, x0, y1 - n]

            if glow:
                self.create_polygon(shape(0, 0, w, h, 4), fill=glow, outline="")
            x0, y0, x1, y1 = m, m + inset, w - m, h - m
            bh = y1 - y0
            k = bh / 56.0
            n = max(3, round(6 * k))
            top_h, bot_h = max(1, round(2 * k)), max(2, round(4 * k))
            sd, px = max(1, round(2 * k)), max(2, round(3 * k))
            self.create_polygon(shape(x0, y0, x1, y1, n), fill=fill, outline="")
            cr = self.create_rectangle
            cr(x0 + n, y0, x1 - n, y0 + top_h, fill=hi, outline="")              # верхняя кромка
            cr(x0 + n, y1 - bot_h, x1 - n, y1, fill=lo, outline="")              # нижняя плашка
            cr(x0, y0 + n, x0 + sd, y1 - n, fill=side, outline="")               # левый край
            cr(x1 - sd, y0 + n, x1, y1 - n, fill=side, outline="")               # правый край
            for x in (x0 + n, x1 - n - px):                                      # угловые «пиксели»
                cr(x, y0 + n, x + px, y0 + n + px, fill=hi, outline="")
                cr(x, y1 - n - px, x + px, y1 - n, fill=lo, outline="")
            label = _nice(self._label)
            avail = (x1 - x0) - 2 * n - 10
            size0 = max(10, round(h * 0.30))
            ic = _btn_icon_name(self._label)
            cy = (y0 + y1) / 2 - 1
            if ic:
                sc = 2 if h >= 40 else 1
                iw, gap = 12 * sc, 8
                f = _fit_font(label, size0, avail - iw - gap)
                if -f[1] < size0:            # с иконкой текст пришлось бы уменьшать — рисуем без иконки
                    ic = None
            if ic:
                tw = _FONT_CACHE[("obj", -f[1])].measure(label)
                x = (w - (iw + gap + tw)) / 2
                self.create_image(x, cy + 1, image=px_icon(ic, fg, sc), anchor="w")
                self.create_text(x + iw + gap, cy, text=label, fill=fg, font=f, anchor="w")
            else:
                f = _fit_font(label, size0, avail)
                self.create_text(w / 2, cy, text=label, fill=fg, font=f)

    class PixelFrame(tk.Frame):
        """Карточка в пиксельном стиле кнопок: срезанные ступенчатые углы, светлая кромка сверху,
        тёмная плашка снизу. Ведёт себя как обычный Frame: дочерние виджеты кладём прямо в неё."""

        def __init__(self, master, fill=None, edge=None, accent=None, step=6, **kw):
            self._fill = fill or C["card"]
            self._edge = edge or C["line"]
            self._accent = accent
            self._n = step
            self._pbg = master.cget("bg")
            kw.setdefault("padx", 16)
            kw.setdefault("pady", 14)
            cur = kw.get("cursor", "")
            super().__init__(master, bg=self._fill, **kw)
            self.cv = tk.Canvas(self, bg=self._pbg, highlightthickness=0, bd=0, cursor=cur)
            self.cv.place(x=0, y=0, relwidth=1, relheight=1)
            self.cv.bind("<Configure>", lambda e: self._redraw())
            self._cids, self._cpos = [], []
            if isinstance(master, GradFrame):
                HUB.follow(self, self._corners)

        def _corners(self, at):
            for cid, (x, y) in zip(self._cids, self._cpos):
                self.cv.itemconfigure(cid, fill=at(x + self._n // 2, y + self._n // 2))

        def set_edge(self, color):
            self._edge = color
            self._redraw()

        def _redraw(self):
            cv = self.cv
            w, h = cv.winfo_width(), cv.winfo_height()
            if w < 24 or h < 24:
                return
            rk = (w, h, self._edge, self._fill, self._accent)
            if rk == getattr(self, "_rk", None):
                return
            self._rk = rk
            cv.delete("all")
            n = self._n
            self._cpos = [(0, 0), (w - n, 0), (0, h - n), (w - n, h - n)]
            self._cids = [cv.create_rectangle(x, y, x + n, y + n, fill=self._pbg, outline="")
                          for x, y in self._cpos]
            fill, edge = self._fill, self._edge
            top = self._accent or _mix(edge, "#ffffff", 0.14)
            lo = _mix(fill, "#000000", 0.45)
            side = max(1, round(n / 3))
            px = max(2, round(n / 2))
            cv.create_polygon(0, n, n, n, n, 0, w - n, 0, w - n, n, w, n,
                              w, h - n, w - n, h - n, w - n, h, n, h, n, h - n, 0, h - n,
                              fill=fill, outline="")
            cr = cv.create_rectangle
            cr(n, 0, w - n, 3 if self._accent else 2, fill=top, outline="")        # верхняя кромка
            cr(n, h - 3, w - n, h, fill=lo, outline="")                            # нижняя плашка
            cr(0, n, side, h - n, fill=edge, outline="")                           # левый край
            cr(w - side, n, w, h - n, fill=edge, outline="")                       # правый край
            for x in (n, w - n - px):                                              # угловые «пиксели»
                cr(x, n, x + px, n + px, fill=top, outline="")
                cr(x, h - n - px, x + px, h - n, fill=lo, outline="")

    class ToggleSwitch(tk.Frame):
        """Переключатель: бегунок плавно скользит, цвет перетекает."""

        def __init__(self, master, text, variable, command=None, bg=None):
            bg = bg or master.cget("bg")
            super().__init__(master, bg=bg, cursor="hand2")
            self._var, self._cmd, self._bgc = variable, command, bg
            self._x = 1.0 if variable.get() else 0.0
            self._job = None
            self._box = tk.Canvas(self, width=44, height=24, bg=bg, highlightthickness=0, bd=0, cursor="hand2")
            self._box.pack(side="left")
            self._lbl = tk.Label(self, text=text, bg=bg, fg=C["text"], font=font(10), cursor="hand2",
                                 anchor="w", justify="left", wraplength=330)
            self._lbl.pack(side="left", padx=(10, 0))
            for w in (self, self._box, self._lbl):
                w.bind("<Button-1>", self._toggle)
            variable.trace_add("write", lambda *a: self._start())
            self._draw()

        def _toggle(self, e=None):
            self._var.set(not self._var.get())
            if self._cmd:
                self._cmd()

        def _start(self):
            if self._job is None:
                self._step()

        def _step(self):
            tgt = 1.0 if self._var.get() else 0.0
            d = tgt - self._x
            if abs(d) < 0.03:
                self._x, self._job = tgt, None
            else:
                self._x += d * 0.35
                self._job = self.after(16, self._step)
            self._draw()

        def _draw(self):
            b = self._box
            b.delete("all")
            _rrect(b, 1, 2, 43, 22, 10, fill=_mix(C["bg2"], C["yellow"], _ease(self._x)), outline="")
            kx = 12 + 20 * self._x
            b.create_oval(kx - 8, 4, kx + 8, 20, fill=_mix(C["muted"], "#ffffff", self._x), outline="")

    class SmoothSlider(tk.Canvas):
        """Ползунок: градиентная дорожка, круглый бегунок, который «вырастает» при наведении."""

        def __init__(self, master, variable, lo, hi, step=256, width=380, bg=None):
            super().__init__(master, width=width, height=28, bg=bg or master.cget("bg"),
                             highlightthickness=0, bd=0, cursor="hand2")
            self._var, self._lo, self._hi, self._step = variable, lo, hi, step
            self._sw = width
            self._g = 0.0
            self._gt = 0.0
            self._job = None
            self.bind("<Button-1>", self._click)
            self.bind("<B1-Motion>", self._click)
            self.bind("<Enter>", lambda e: self._glow(1.0))
            self.bind("<Leave>", lambda e: self._glow(0.0))
            self.bind("<Configure>", self._resized)
            variable.trace_add("write", lambda *a: self._draw())
            self._draw()

        def _glow(self, t):
            self._gt = t
            if self._job is None:
                self._anim()

        def _anim(self):
            d = self._gt - self._g
            if abs(d) < 0.04:
                self._g, self._job = self._gt, None
            else:
                self._g += d * 0.3
                self._job = self.after(16, self._anim)
            self._draw()

        def _resized(self, e):
            if e.width > 20 and e.width != self._sw:
                self._sw = e.width
                self._draw()

        def _click(self, e):
            tw = 20
            frac = min(1.0, max(0.0, (e.x - tw / 2) / max(1, self._sw - tw)))
            v = int(round((self._lo + frac * (self._hi - self._lo)) / self._step) * self._step)
            self._var.set(max(self._lo, min(self._hi, v)))

        def _draw(self):
            w, tw = self._sw, 20
            try:
                v = int(self._var.get())
            except Exception:
                v = self._lo
            v = max(self._lo, min(self._hi, v))
            frac = (v - self._lo) / (self._hi - self._lo) if self._hi > self._lo else 0
            x = frac * (w - tw) + tw / 2
            self.delete("all")
            _rrect(self, 2, 11, w - 2, 17, 3, fill=C["bg2"], outline="")
            if x > 6:
                _rrect(self, 2, 11, x, 17, 3, fill=_mix(C["yellow_lo"], C["yellow_hi"], frac), outline="")
            r = 8 + 2 * self._g
            if self._g > 0.05:
                self.create_oval(x - r - 4, 14 - r - 4, x + r + 4, 14 + r + 4,
                                 fill=_mix(C["bg1"], C["yellow"], 0.25 * self._g), outline="")
            self.create_oval(x - r, 14 - r, x + r, 14 + r, fill=C["yellow"], outline="")

    class SmoothProgress(tk.Canvas):
        """Полоса прогресса: плавно догоняет значение, по ней бежит блик."""

        def __init__(self, master, bg=None):
            super().__init__(master, height=14, bg=bg or C["bg0"], highlightthickness=0, bd=0)
            self._val, self._max = 0, 1
            self._shown = 0.0
            self._sh = 0.0
            self.bind("<Configure>", lambda e: self._draw())
            self.after(40, self._tick)

        def set(self, val, mx):
            self._val, self._max = val, max(mx, 1)

        def _tick(self):
            try:
                tgt = min(1.0, self._val / self._max)
                if tgt < self._shown - 0.3:
                    self._shown = tgt
                else:
                    self._shown += (tgt - self._shown) * 0.2
                if 0.0 < tgt < 1.0 or abs(tgt - self._shown) > 0.002:
                    self._sh = (self._sh + 0.03) % 1.4
                state = (round(self._shown, 3), round(self._sh, 2))
                if state != getattr(self, "_st", None) and self.winfo_viewable():
                    self._st = state
                    self._draw()
            finally:
                self.after(40, self._tick)

        def _draw(self):
            w, h = self.winfo_width(), 14
            if w < 12:
                return
            self.delete("all")
            _rrect(self, 0, 3, w, h - 3, 4, fill=C["bg2"], outline="")
            fw = int(w * self._shown)
            if fw > 8:
                _rrect(self, 0, 3, fw, h - 3, 4, fill=C["yellow"], outline="")
                _rrect(self, 0, 3, fw, 6, 2, fill=C["yellow_hi"], outline="")
                bx = (self._sh - 0.2) * fw
                x0, x1 = max(2, bx), min(fw - 2, bx + 70)
                if x1 > x0 and self._shown < 0.995:
                    _rrect(self, x0, 4, x1, h - 4, 3, fill=C["yellow_hi"], outline="")

    class Banner(tk.Canvas):
        """Градиентная шапка страницы: закат от индиго к оранжевому, внутри плывут пиксели."""
        ANIM = True
        STOPS = ("#1c1342", "#3a1a64", "#8c2c4c", "#ff8a1f")

        def __init__(self, master, title, sub="", height=76, big=False, bg=None):
            self._pbg = bg or (HUB.mid() if isinstance(master, GradFrame) else C["bg1"])
            super().__init__(master, height=height, bg=self._pbg, highlightthickness=0, bd=0)
            self._title, self._sub, self._big = title, sub, big
            self._px = []
            for k in range(16):
                self._px.append([(k * 0.137 + 0.05) % 1.0, (k * 0.311) % 1.0, 3 + (k % 3) * 2,
                                 0.0016 + 0.0011 * (k % 4), k % 5])
            self._items = []
            self._cw = 0
            self._cids, self._cpos = [], []
            if isinstance(master, GradFrame):
                HUB.follow(self, self._corners)
            self.bind("<Configure>", lambda e: self._redraw())
            self.after(80, self._tick)

        def _corners(self, at):
            for cid, (x, y) in zip(self._cids, self._cpos):
                self.itemconfigure(cid, fill=at(x + 3, y + 3))

        def set_text(self, title=None, sub=None):
            if title is not None:
                self._title = title
            if sub is not None:
                self._sub = sub
            self._redraw()

        def _grad(self, t):
            s = self.STOPS
            t = min(1.0, max(0.0, t)) ** 1.7
            x = t * (len(s) - 1)
            i = min(int(x), len(s) - 2)
            return _mix(s[i], s[i + 1], x - i)

        def _redraw(self):
            w, h = self.winfo_width(), int(self.cget("height"))
            if w < 40:
                return
            rkey = (w, h, self._title, self._sub, self._big)
            if rkey == getattr(self, "_rkey", None):
                return
            self._rkey = rkey
            self._cw = w
            self.delete("all")
            step = 12
            n = w // step + 1
            for i in range(n):
                self.create_rectangle(i * step, 0, i * step + step + 1, h, fill=self._grad(i / max(1, n - 1)),
                                      outline="")
            gx, gy = w - h * 0.55, h * 0.5
            for k in range(9):          # мягкое свечение справа (без stipple — плавные кольца)
                rr = h * (1.25 - k * 0.115)
                edge = min(1.0, max(0.0, (gx - rr) / max(1, w)))
                self.create_oval(gx - rr, gy - rr, gx + rr, gy + rr, outline="",
                                 fill=_mix(self._grad(edge), "#ffc15a", 0.03 + 0.5 * (k / 8) ** 2))
            self._items = []
            for px, py, sz, sp, ph in self._px:
                col = _mix(self._grad(px), "#ffffff", 0.35)
                self._items.append(self.create_rectangle(0, 0, 1, 1, fill=col, outline="", tags="px"))
            self._move()
            for i in range(n):                                      # кромки как у кнопок
                x = i * step
                col = self._grad(i / max(1, n - 1))
                self.create_rectangle(x, 0, x + step + 1, 3, fill=_mix(col, "#ffffff", 0.28), outline="")
                self.create_rectangle(x, h - 4, x + step + 1, h, fill=_mix(col, "#000000", 0.40), outline="")
            c = 6
            self._cids, self._cpos = [], []
            for sx, sy in ((0, 0), (1, 0), (0, 1), (1, 1)):     # срезанные ступенчатые углы
                x0 = w - c if sx else 0
                y0 = h - c if sy else 0
                self._cpos.append((x0, y0))
                self._cids.append(self.create_rectangle(x0, y0, x0 + c, y0 + c, fill=self._pbg, outline=""))
            ty = h / 2 - (10 if self._sub else 0)
            if self._big:
                ty = h / 2 - 18
            f1 = font(26 if self._big else 19, True)
            self.create_text(26, ty + 2, text=self._title, anchor="w", fill="#0a0612", font=f1)
            self.create_text(24, ty, text=self._title, anchor="w", fill="#ffffff", font=f1)
            if self._sub:
                self.create_text(25, ty + (36 if self._big else 28), text=self._sub, anchor="w",
                                 fill="#d8cffa", font=font(12 if self._big else 10))

        def _move(self):
            w, h = self._cw, int(self.cget("height"))
            for it, (px, py, sz, sp, ph) in zip(self._items, self._px):
                x, y = px * w, py * h
                self.coords(it, x, y, x + sz, y + sz)

        def _tick(self):
            try:
                if self.ANIM and self._items and _on_page(self) and self.winfo_viewable():
                    for p in self._px:
                        p[1] -= p[3]
                        if p[1] < -0.1:
                            p[1] = 1.05
                    self._move()
            finally:
                self.after(90, self._tick)

    # старые имена оставлены для совместимости
    PixelButton, PixelCheck, PixelSlider, PixelProgress = AnimButton, ToggleSwitch, SmoothSlider, SmoothProgress


    class ScrollFrame(tk.Frame):
        """Прокручиваемая область: виджеты кладём в .body (прозрачная, градиент стоит на месте при прокрутке)"""

        def __init__(self, master, bg):
            super().__init__(master, bg=bg)
            self.cv = GradFrame(self)
            self.sb = ttk.Scrollbar(self, style="U.Vertical.TScrollbar", command=self.cv.yview)
            self.cv.configure(yscrollcommand=self._yscroll)
            self.sb.pack(side="right", fill="y")
            self.cv.pack(side="left", fill="both", expand=True)
            self.body = GradFrame(self.cv)
            self._win = self.cv.create_window(0, 0, window=self.body, anchor="nw")
            self.body.bind("<Configure>", lambda e: self.cv.configure(scrollregion=self.cv.bbox(self._win)), add="+")
            self.cv.bind("<Configure>", lambda e: self.cv.itemconfigure(self._win, width=e.width), add="+")

        def _yscroll(self, lo, hi):
            self.sb.set(lo, hi)
            HUB.realign_soon()

        def wheel(self, e):
            lo, hi = self.cv.yview()
            if lo <= 0.0 and hi >= 1.0:
                return
            self.cv.yview_scroll(-1 if e.delta > 0 else 1, "units")

    class Dialog(tk.Toplevel):
        """Диалог с плавным появлением. Содержимое кладём в .body"""

        def __init__(self, master, title, width=460):
            super().__init__(master, bg=C["line"])
            self.title(title)
            self.transient(master)
            self.resizable(False, False)
            self.minsize(width, 120)
            inner = tk.Frame(self, bg=C["bg1"])
            inner.pack(fill="both", expand=True, padx=1, pady=1)
            self.body = tk.Frame(inner, bg=C["bg1"])
            self.body.pack(fill="both", expand=True, padx=24, pady=20)
            try:
                self.geometry(f"+{master.winfo_rootx() + 90}+{master.winfo_rooty() + 90}")
                self.focus_force()
                self.attributes("-alpha", 0.0)
                self._fade(0.0)
            except Exception:
                pass

        def _fade(self, a):
            try:
                a = min(1.0, a + 0.12)
                self.attributes("-alpha", a)
                if a < 1.0:
                    self.after(16, lambda: self._fade(a))
            except Exception:
                pass


# ─────────────── картинки (вшиты в файл, отдельные файлы не нужны) ───────────────
LOGO_B64 = "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"
ICON_B64 = "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"


def load_cfg():
    try:
        with open(CONFIG, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def save_cfg(cfg):
    BASE.mkdir(parents=True, exist_ok=True)
    with open(CONFIG, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)


NAME_RE = re.compile(r"[A-Za-z0-9_]{3,16}")
LOADER_CHOICES = ["Fabric", "Quilt", "Forge", "NeoForge", "Любой"]
SORTS = {"По скачиваниям": "downloads", "По релевантности": "relevance",
         "По обновлению": "updated", "Новые": "newest"}
TYPE_BY_LABEL = {v[0]: k for k, v in CONTENT_TYPES.items()}

TIPS = [
    "Для чистой версии хватает 3–4 ГБ памяти, для сборок с модами и текстурами — 6 ГБ и больше.",
    "Не отдавайте игре больше ~70% ОЗУ: системе и браузеру тоже нужна память.",
    "Профиль «Оптимальный» (G1GC) обычно уменьшает фризы из-за сборщика мусора.",
    "Сборки (вкладка «Сборки») хранят моды, миры и настройки отдельно друг от друга.",
    "Если игра не запускается, загляните в «Лог игры»: причина обычно видна в последних строках.",
    "Клик по карточке профиля слева внизу открывает вкладку «Аккаунты».",
    "В «Настройках» есть кнопка «Ярлык на рабочий стол» — значок у ярлыка тот же, что у лаунчера.",
]


class App:
    HEADER = 16
    MARGIN = 16
    NAV = (("home", "Играть"), ("builds", "Сборки"), ("mods", "Моды"),
           ("accounts", "Аккаунты"), ("settings", "Настройки"), ("log", "Лог игры"))
    SPEEDS = {"Медленно": 0.002, "Нормально": 0.0035, "Быстро": 0.007}

    def __init__(self, root):
        self.root = root
        self.cfg = load_cfg()
        self.q = queue.Queue()
        self.manifest = None
        self.running = False
        self.page = "home"
        self.launch_map = {}
        self.acc_map = {}
        self.scrolls = {}
        self.instances = list_instances()
        self.total_ram = total_ram_mb()
        self._skip_save = False
        self._sq = None
        self._busy_search = False
        self._shown = 0
        self._ensure_accounts()

        root.title(APP_NAME)
        root.geometry("1200x800")
        root.minsize(1020, 720)
        root.configure(bg=C["bg0"])
        self._load_images()
        self._init_style()
        self._make_vars()

        self.bg = AnimatedBG(root)
        HUB.start(root, self.bg)
        self.bg.pack(fill="both", expand=True)
        self._build_header()
        self._build_panel()
        self.bg.bind("<Configure>", self._layout, add="+")

        self.bg.enabled = self.v_anim.get()
        self.bg.step = self.SPEEDS.get(self.v_speed.get(), 0.0035)
        Banner.ANIM = self.v_anim.get()
        self.v_anim.trace_add("write", lambda *a: self._anim_changed())
        self.v_speed.trace_add("write", lambda *a: self._anim_changed())
        self.root.after(12000, self._rotate_tip)
        self.refresh_targets()
        self.refresh_accounts()
        self.refresh_builds_list()
        self.update_summary()
        self.sync_mods_target()
        self.show_page("home")
        threading.Thread(target=self.load_manifest, daemon=True).start()
        root.after(100, self.poll)
        root.protocol("WM_DELETE_WINDOW", self.on_close)
        root.bind_all("<MouseWheel>", self._wheel)

    # ───── подготовка ─────
    def _ensure_accounts(self):
        accs = self.cfg.setdefault("accounts", [])
        if not accs:
            nm = self.cfg.get("name") or "Player"
            a = new_offline(nm if NAME_RE.fullmatch(nm) else "Player")
            accs.append(a)
            self.cfg["active_account"] = a["id"]
        if not any(a["id"] == self.cfg.get("active_account") for a in accs):
            self.cfg["active_account"] = accs[0]["id"]

    def _load_images(self):
        """Значок окна/панели задач берём из ueban.ico — тот же файл, что и у exe и ярлыка."""
        self.logo_img = self.icon_img = None
        try:
            self.logo_img = tk.PhotoImage(data=LOGO_B64)
        except Exception:
            pass
        ico = ensure_icon_file() or resource_path("ueban.ico")
        done = False
        if IS_WIN and ico and os.path.exists(ico):
            try:
                self.root.iconbitmap(default=ico)
                self.root.iconbitmap(ico)
                done = True
            except Exception:
                pass
        if not done:
            try:
                self.icon_img = tk.PhotoImage(data=ICON_B64)
                self.root.iconphoto(True, self.icon_img)
            except Exception:
                pass

    def _anim_changed(self):
        on = bool(self.v_anim.get())
        self.bg.enabled = on
        Banner.ANIM = on
        self.bg.step = self.SPEEDS.get(self.v_speed.get(), 0.0035)

    def _rotate_tip(self):
        try:
            if self.page == "home":
                self._next_tip()
        finally:
            self.root.after(12000, self._rotate_tip)

    def _next_tip(self):
        self._tip_i = (getattr(self, "_tip_i", 0) + 1) % len(TIPS)
        if hasattr(self, "tip_lbl"):
            self.tip_lbl.configure(text=TIPS[self._tip_i])
            self.tip_no.configure(text=f"{self._tip_i + 1} / {len(TIPS)}")
    def _init_style(self):
        st = ttk.Style()
        try:
            st.theme_use("clam")
        except Exception:
            pass
        st.configure("U.TCombobox", fieldbackground=C["input"], background=C["bg2"],
                     foreground=C["text"], arrowcolor=C["yellow"], bordercolor=C["line"],
                     lightcolor=C["line"], darkcolor=C["line"], padding=7)
        st.map("U.TCombobox",
               fieldbackground=[("readonly", C["input"])],
               foreground=[("readonly", C["text"])],
               selectbackground=[("readonly", C["input"])],
               selectforeground=[("readonly", C["text"])],
               background=[("active", C["bg2"])])
        st.map("U.Vertical.TScrollbar", background=[("active", C["line"]), ("!active", C["bg2"])])
        st.configure("U.Vertical.TScrollbar", background=C["bg2"], troughcolor=C["bg0"], borderwidth=0,
                     relief="flat", arrowsize=14, gripcount=0,
                     bordercolor=C["line"], arrowcolor=C["yellow"], lightcolor=C["line"],
                     darkcolor=C["line"])
        o = self.root.option_add
        o("*TCombobox*Listbox.background", C["input"])
        o("*TCombobox*Listbox.foreground", C["text"])
        o("*TCombobox*Listbox.selectBackground", C["yellow"])
        o("*TCombobox*Listbox.selectForeground", C["brown"])
        o("*TCombobox*Listbox.font", font(10))

    def _make_vars(self):
        c = self.cfg
        self.max_ram = max(2048, (int(self.total_ram * 0.75) // 256) * 256)
        self.default_ram = min(4096, max(2048, (self.total_ram // 2 // 256) * 256), self.max_ram)

        def gi(key, d):
            try:
                return int(c.get(key, d))
            except Exception:
                return d

        self.v_launch = tk.StringVar(value="")
        self.v_account = tk.StringVar(value="")
        self.v_ram = tk.IntVar(value=min(max(gi("ram", self.default_ram), 1024), self.max_ram))
        self.v_xms = tk.IntVar(value=gi("xms", 512))
        key = c.get("preset", "aikar")
        self.v_preset = tk.StringVar(value=key if key in FLAG_PRESETS else "aikar")
        self.v_custom_jvm = tk.StringVar(value=c.get("custom_jvm", ""))
        saved_extra = c.get("extra_flags") or []
        self.v_extra = {k: tk.BooleanVar(value=k in saved_extra) for k in EXTRA_FLAGS}
        self.v_custom_res = tk.BooleanVar(value=bool(c.get("custom_res", False)))
        self.v_w = tk.StringVar(value=str(gi("width", 1280)))
        self.v_h = tk.StringVar(value=str(gi("height", 720)))
        self.v_full = tk.BooleanVar(value=bool(c.get("fullscreen", False)))
        self.v_game_args = tk.StringVar(value=c.get("game_args", ""))
        self.v_java = tk.StringVar(value=c.get("java_path", ""))
        self.v_hide = tk.BooleanVar(value=bool(c.get("hide_on_start", False)))
        self.v_close = tk.BooleanVar(value=bool(c.get("close_on_start", False)))
        self.v_prio = tk.BooleanVar(value=bool(c.get("high_priority", False)))
        self.v_anim = tk.BooleanVar(value=bool(c.get("animate", True)))
        self.v_speed = tk.StringVar(value=c.get("speed", "Нормально"))
        self.v_snap = tk.BooleanVar(value=bool(c.get("snapshots", False)))
        self.v_old = tk.BooleanVar(value=bool(c.get("old_versions", False)))
        self.v_perver = tk.BooleanVar(value=bool(c.get("per_version_dirs", False)))
        self.v_verify = tk.BooleanVar(value=bool(c.get("verify_files", False)))
        self.v_showcmd = tk.BooleanVar(value=bool(c.get("show_cmd", False)))
        self.v_ms_cid = tk.StringVar(value=c.get("ms_client_id", ""))
        for v in (self.v_ram, self.v_xms, self.v_preset, self.v_java):
            v.trace_add("write", lambda *a: self.update_summary())
        self.v_launch.trace_add("write", lambda *a: self._on_launch_change())
        self.v_anim.trace_add("write", lambda *a: setattr(self.bg, "enabled", self.v_anim.get()))
        self.v_speed.trace_add("write", lambda *a: setattr(
            self.bg, "step", self.SPEEDS.get(self.v_speed.get(), 0.0035)))

    def gather(self):
        def gi(v, d):
            try:
                return int(str(v.get()).strip())
            except Exception:
                return d
        kind, ident = self.launch_map.get(self.v_launch.get(), (None, None))
        return dict(
            launch_kind=kind or self.cfg.get("launch_kind"), launch_id=ident or self.cfg.get("launch_id"),
            ram=gi(self.v_ram, 2048), xms=gi(self.v_xms, 512),
            preset=self.v_preset.get(), custom_jvm=self.v_custom_jvm.get().strip(),
            extra_flags=[k for k, v in self.v_extra.items() if v.get()],
            custom_res=self.v_custom_res.get(), width=gi(self.v_w, 1280), height=gi(self.v_h, 720),
            fullscreen=self.v_full.get(), game_args=self.v_game_args.get().strip(),
            java_path=self.v_java.get().strip(), hide_on_start=self.v_hide.get(),
            close_on_start=self.v_close.get(), high_priority=self.v_prio.get(),
            animate=self.v_anim.get(), speed=self.v_speed.get(),
            snapshots=self.v_snap.get(), old_versions=self.v_old.get(),
            per_version_dirs=self.v_perver.get(), verify_files=self.v_verify.get(),
            show_cmd=self.v_showcmd.get(), ms_client_id=self.v_ms_cid.get().strip())

    def save_all(self):
        if self._skip_save:
            return
        self.cfg.update(self.gather())
        save_cfg(self.cfg)

    def ui(self, fn):
        """Выполнить fn в главном потоке (безопасно вызывать из фоновых потоков)."""
        self.q.put(("call", fn))

    # ───── вёрстка ─────
    def _build_header(self):
        pass

    def _layout(self, e):
        W, H = e.width, e.height
        m, top = self.MARGIN, self.HEADER
        self.bg.coords(self._panel_id, m, top)
        self.bg.itemconfigure(self._panel_id, width=max(300, W - 2 * m), height=max(300, H - top - m))

    def _build_panel(self):
        self.panel = tk.Frame(self.bg, bg=C["line"])
        self._panel_id = self.bg.create_window(self.MARGIN, self.HEADER, window=self.panel,
                                               anchor="nw", width=900, height=500)
        inner = tk.Frame(self.panel, bg=C["bg0"])
        inner.pack(fill="both", expand=True, padx=1, pady=1)
        inner.columnconfigure(1, weight=1)
        inner.rowconfigure(0, weight=1)
        self._build_sidebar(inner)
        self._build_pages(inner)
        self._build_bottom(inner)

    # ───── сайдбар ─────
    def _build_sidebar(self, parent):
        sb = tk.Frame(parent, bg=C["bg0"], width=250)
        sb.grid(row=0, column=0, sticky="ns")
        sb.pack_propagate(False)
        top = tk.Frame(sb, bg=C["bg0"])
        top.pack(fill="x", pady=(24, 0))
        if self.logo_img:
            tk.Label(top, image=self.logo_img, bg=C["bg0"]).pack()
        else:
            tk.Label(top, text="UEBAN", bg=C["bg0"], fg=C["yellow"], font=font(26, True)).pack()
        tk.Label(top, text="Minecraft Java Edition  ·  v" + APP_VERSION, bg=C["bg0"], fg=C["muted"],
                 font=font(9)).pack(pady=(8, 0))
        tk.Frame(sb, bg=C["line"], height=1).pack(fill="x", padx=20, pady=(18, 12))
        sym = "Segoe UI Symbol" if IS_WIN else "DejaVu Sans"
        icons = {"home": "play", "builds": "cube", "mods": "plus", "accounts": "head",
                 "settings": "gear", "log": "list"}
        self.nav = {}
        for key, text in self.NAV:
            row = tk.Frame(sb, bg=C["bg0"], cursor="hand2")
            row.pack(fill="x", padx=10, pady=1)
            bar = tk.Frame(row, bg=C["bg0"], width=4)
            bar.pack(side="left", fill="y")
            ico = tk.Label(row, image=px_icon(icons.get(key, "plus"), C["muted"], 2), bg=C["bg0"],
                           width=28, height=24, cursor="hand2")
            ico._name, ico._c = icons.get(key, "plus"), C["muted"]
            ico.pack(side="left", padx=(10, 0))
            lab = tk.Label(row, text=text, bg=C["bg0"], fg=C["muted"], font=font(12, True),
                           anchor="w", padx=6, pady=10, cursor="hand2")
            lab.pack(side="left", fill="x", expand=True)
            for w in (row, bar, ico, lab):
                w.bind("<Button-1>", lambda e, k=key: self.show_page(k))
                w.bind("<Enter>", lambda e, k=key: self._nav_hover(k, True))
                w.bind("<Leave>", lambda e, k=key: self._nav_hover(k, False))
            self.nav[key] = (row, bar, lab, ico)
        # профиль внизу: клик ведёт в «Аккаунты»
        card = PixelFrame(sb, step=4, padx=0, pady=0, cursor="hand2")
        card.pack(side="bottom", fill="x", padx=14, pady=(0, 16))
        self.av = tk.Canvas(card, width=44, height=44, bg=C["card"], highlightthickness=0, cursor="hand2")
        self.av.pack(side="left", padx=(10, 8), pady=10)
        col = tk.Frame(card, bg=C["card"], cursor="hand2")
        col.pack(side="left", fill="x", expand=True)
        self.prof_name = tk.Label(col, text="", bg=C["card"], fg=C["text"], font=font(11, True), anchor="w",
                                  cursor="hand2")
        self.prof_name.pack(fill="x")
        self.prof_type = tk.Label(col, text="", bg=C["card"], fg=C["muted"], font=font(9), anchor="w",
                                  cursor="hand2")
        self.prof_type.pack(fill="x")
        for w in (card, card.cv, self.av, col, self.prof_name, self.prof_type):
            w.bind("<Button-1>", lambda e: self.show_page("accounts"))
        PixelButton(sb, "Папка Minecraft", self.open_minecraft_folder, width=222, height=34, fs=10,
                    bg=C["bg0"]).pack(side="bottom", pady=(0, 8))
        PixelButton(sb, "Папка версии", self.open_version_folder, width=222, height=34, fs=10,
                    bg=C["bg0"]).pack(side="bottom", pady=(0, 8))

    def _update_profile(self):
        if not hasattr(self, "prof_name"):
            return
        a = self.active_account()
        name = a["name"] if a else "—"
        self.prof_name.configure(text=name)
        self.prof_type.configure(text=("Аккаунт: " + ACC_TYPES.get(a["type"], a["type"])) if a else "")
        cols = [C["orange"], C["violet"], C["pink"], C["blue"], C["green"]]
        colr = cols[sum(map(ord, name)) % len(cols)]
        cv = self.av
        cv.delete("all")
        cv.create_oval(2, 2, 42, 42, fill=colr, outline="")
        cv.create_text(22, 22, text=(name[:1] or "?").upper(), fill="#15101f", font=font(15, True))

    def _nav_hover(self, key, on):
        self._nav_to(key, 1.0 if (on or key == self.page) else 0.0)

    def _style_nav(self):
        for key in self.nav:
            self._nav_to(key, 1.0 if key == self.page else 0.0)

    def _nav_to(self, key, tgt):
        if not hasattr(self, "_nav_lv"):
            self._nav_lv, self._nav_t, self._nav_job = {}, {}, {}
        self._nav_t[key] = tgt
        if self._nav_job.get(key) is None:
            self._nav_step(key)

    def _nav_step(self, key):
        row, bar, lab, ico = self.nav[key]
        lv, tgt = self._nav_lv.get(key, 0.0), self._nav_t.get(key, 0.0)
        d = tgt - lv
        if abs(d) < 0.03:
            lv, self._nav_job[key] = tgt, None
        else:
            lv += d * 0.3
            self._nav_job[key] = self.root.after(16, lambda: self._nav_step(key))
        self._nav_lv[key] = lv
        sel = key == self.page
        k = min(1.0, lv)
        bgc = _mix(C["bg0"], C["bg2"] if sel else C["hover"], k)
        for w in (row, lab, ico):
            w.configure(bg=bgc)
        lab.configure(fg=_mix(C["muted"], C["text"], k))
        ic = _mix(C["muted"], C["yellow"] if sel else C["text"], round(k * 8) / 8)
        if ic != ico._c:
            ico._c = ic
            ico.configure(image=px_icon(ico._name, ic, 2))
        bar.configure(bg=_mix(bgc, C["yellow"], 1.0 if sel else 0.0), width=4)

    def show_page(self, key):
        old = getattr(self, "_shown", None)
        self.page = key
        self._style_nav()
        if key == "mods":
            self.sync_mods_target()
        if old is None or old == key or not self.v_anim.get():
            self._stop_slide()
            HUB.active = {self.pages[key]}
            self.pages[key].place_configure(relx=0.0)
            self.pages[key].tkraise()
            self._shown = key
            HUB.realign_soon()
            return
        self._slide_in(old, key)

    def _stop_slide(self):
        job = getattr(self, "_slide_job", None)
        if job:
            try:
                self.root.after_cancel(job)
            except Exception:
                pass
        self._slide_job = None
        try:
            self._cover.place_forget()
        except Exception:
            pass

    def _slide_in(self, old_key, key, dur=0.24):
        """Страница не «едет» (перекладывать сотни виджетов на каждом кадре тяжело), а открывается шторкой слева направо:
        страница уже стоит на месте, а тёмная шторка, которая её закрывала, быстро уезжает вправо."""
        self._stop_slide()
        new = self.pages[key]
        self._shown = key
        HUB.active = {new}
        new.tkraise()
        try:
            HUB._realign({new})
        except Exception:
            pass
        cover = self._cover
        cover.place(relx=0.0, rely=0.0, relwidth=1.0, relheight=1.0)
        cover.tkraise()
        t0 = time.perf_counter()

        def step():
            t = min(1.0, (time.perf_counter() - t0) / dur)
            e = 1 - (1 - t) ** 3
            if t < 1.0:
                cover.place_configure(relx=e, relwidth=1.0 - e)
                self._slide_job = self.root.after(10, step)
            else:
                cover.place_forget()
                self._slide_job = None
                HUB.realign_soon()
        step()

    def _wheel(self, e):
        sf = self.scrolls.get(self.page)
        if sf:
            sf.wheel(e)

    # ───── помощники ─────
    def _section(self, parent, title):
        f = PixelFrame(parent, padx=18, pady=16)
        ic = {"Память": "chip", "Флаги оптимизации (JVM)": "flag", "Папки и сервис": "folder", "Игра": "gamepad",
              "Java": "cup", "Интеграции": "plug", "Лаунчер": "rocket"}.get(title)
        if ic:
            tk.Label(f, text="  " + title, image=px_icon(ic, C["yellow"], 2), compound="left", bg=C["card"],
                     fg=C["yellow"], font=font(12, True)).pack(anchor="w")
        else:
            tk.Label(f, text=title, bg=C["card"], fg=C["yellow"], font=font(12, True)).pack(anchor="w")
        tk.Frame(f, bg=C["line"], height=1).pack(fill="x", pady=(6, 10))
        return f

    def _label(self, parent, text, muted=False, wrap=0, bg=None):
        if isinstance(parent, GradFrame):
            return GradLabel(parent, text=text, font=font(10), anchor="w", justify="left", wraplength=wrap)
        return tk.Label(parent, text=text, bg=bg or parent.cget("bg"), fg=C["muted"] if muted else C["text"],
                        font=font(10), anchor="w", justify="left", wraplength=wrap)

    def _entry(self, parent, var, width=22, show=None):
        e = tk.Entry(parent, textvariable=var, width=width, bg=C["input"], fg=C["text"],
                     insertbackground=C["yellow"], relief="flat", highlightthickness=1,
                     highlightbackground=C["line"], highlightcolor=C["yellow"], font=font(11),
                     disabledbackground=C["input"])
        if show:
            e.configure(show=show)
        return e

    def _combo(self, parent, var, values, width=14):
        return PixelSelect(parent, textvariable=var, values=list(values), width=width, fs=10)

    def _listbox(self, parent, width=28, height=12):
        return tk.Listbox(parent, width=width, height=height, bg=C["card"], fg=C["text"],
                          selectbackground=C["violet_lo"], selectforeground="#ffffff",
                          highlightthickness=0, relief="flat", bd=0, font=font(11),
                          activestyle="none", exportselection=False)

    def _title(self, parent, text, sub=""):
        return Banner(parent, _nice(text) if text.isupper() else text, sub, height=70)

    # ───── страницы ─────
    def _build_pages(self, parent):
        content = tk.Frame(parent, bg=C["bg1"])
        HUB.origin = content
        self._cover = tk.Canvas(content, bg=C["bg1"], highlightthickness=0, bd=0)
        content.bind("<Configure>", lambda e: HUB.realign_soon(), add="+")
        content.grid(row=0, column=1, sticky="nsew")
        content.rowconfigure(0, weight=1)
        content.columnconfigure(0, weight=1)
        self.pages = {}
        for key, _t in self.NAV:
            p = GradFrame(content)
            p.place(relx=0.0, rely=0.0, relwidth=1.0, relheight=1.0)
            self.pages[key] = p
        self._build_home(self.pages["home"])
        self._build_builds(self.pages["builds"])
        self._build_mods(self.pages["mods"])
        self._build_accounts(self.pages["accounts"])
        self._build_settings(self.pages["settings"])
        self._build_log(self.pages["log"])

    # --- главная
    def _card(self, parent, **kw):
        return PixelFrame(parent, padx=18, pady=16, **kw)

    def _tile(self, parent, key, title, accent, on_click):
        card = PixelFrame(parent, accent=accent, padx=0, pady=0, cursor="hand2")
        inner = tk.Frame(card, bg=C["card"], cursor="hand2")
        inner.pack(fill="both", expand=True, padx=18, pady=(14, 14))
        tic = {"launch": "play", "acc": "head", "folder": "folder", "mem": "chip", "flags": "flag",
               "java": "cup"}.get(key, "plus")
        cap = tk.Label(inner, text="  " + title.upper(), image=px_icon(tic, C["yellow"], 1), compound="left",
                       bg=C["card"], fg=C["muted"], font=font(9, True), anchor="w", cursor="hand2")
        cap.pack(anchor="w")
        val = tk.Label(inner, text="", bg=C["card"], fg=C["text"], font=font(12, True), anchor="w",
                       justify="left", wraplength=220, cursor="hand2")
        val.pack(anchor="w", fill="x", pady=(5, 0))
        card.bind("<Configure>", lambda e, v=val: v.configure(wraplength=max(120, e.width - 44)))
        for w in (card, card.cv, inner, cap, val):
            w.bind("<Button-1>", lambda e: on_click())
            w.bind("<Enter>", lambda e: card.set_edge(accent))
            w.bind("<Leave>", lambda e: card.set_edge(C["line"]))
        self.sum[key] = val
        return card

    def _build_home(self, page):
        pad = GradFrame(page)
        pad.pack(fill="both", expand=True, padx=24, pady=20)
        self.hero = Banner(pad, "Привет!", "Выберите версию внизу и нажмите «Играть»", height=148, big=True)
        self.hero.pack(fill="x")
        grid = GradFrame(pad)
        grid.pack(fill="x", pady=(16, 0))
        for c in range(3):
            grid.columnconfigure(c, weight=1, uniform="t")
        self.sum = {}
        tiles = (("launch", "Запуск", C["orange"], lambda: self.show_page("builds")),
                 ("acc", "Аккаунт", C["violet"], lambda: self.show_page("accounts")),
                 ("folder", "Папка игры", C["blue"], self.open_version_folder),
                 ("mem", "Память", C["green"], lambda: self.show_page("settings")),
                 ("flags", "Профиль флагов", C["pink"], lambda: self.show_page("settings")),
                 ("java", "Java", C["yellow"], lambda: self.show_page("settings")))
        for i, (key, title, accent, cmd) in enumerate(tiles):
            self._tile(grid, key, title, accent, cmd).grid(row=i // 3, column=i % 3, sticky="nsew",
                                                           padx=(0 if i % 3 == 0 else 6, 0 if i % 3 == 2 else 6),
                                                           pady=(0 if i < 3 else 12, 0))
        row2 = GradFrame(pad)
        row2.pack(fill="both", expand=True, pady=(16, 0))
        row2.columnconfigure(0, weight=1, uniform="r")
        row2.columnconfigure(1, weight=1, uniform="r")
        row2.rowconfigure(0, weight=1)
        qa = self._card(row2)
        qa.grid(row=0, column=0, sticky="nsew", padx=(0, 6))
        tk.Label(qa, text="  Быстрые действия", image=px_icon("bolt", C["yellow"], 2), compound="left",
                 bg=C["card"], fg=C["yellow"], font=font(12, True)).pack(anchor="w")
        bt = tk.Frame(qa, bg=C["card"])
        bt.pack(anchor="w", pady=(10, 0))
        acts = (("Сборки", lambda: self.show_page("builds")), ("Моды", lambda: self.show_page("mods")),
                ("Папка игры", self.open_version_folder), ("Настройки", lambda: self.show_page("settings")))
        for i, (t, cmd) in enumerate(acts):
            PixelButton(bt, t, cmd, width=168, height=40, fs=10).grid(row=i // 2, column=i % 2,
                                                                      padx=(0, 8), pady=(0, 6))
        tp = self._card(row2)
        tp.grid(row=0, column=1, sticky="nsew", padx=(6, 0))
        head = tk.Frame(tp, bg=C["card"])
        head.pack(fill="x")
        tk.Label(head, text="  Совет", image=px_icon("bulb", C["yellow"], 2), compound="left",
                 bg=C["card"], fg=C["yellow"], font=font(12, True)).pack(side="left")
        self.tip_no = tk.Label(head, text=f"1 / {len(TIPS)}", bg=C["card"], fg=C["muted"], font=font(9))
        self.tip_no.pack(side="right")
        self._tip_i = 0
        self.tip_lbl = tk.Label(tp, text=TIPS[0], bg=C["card"], fg=C["text"], font=font(11), anchor="nw",
                                justify="left", wraplength=360)
        self.tip_lbl.pack(anchor="w", fill="x", pady=(10, 0))
        tp.bind("<Configure>", lambda e: self.tip_lbl.configure(wraplength=max(160, e.width - 40)))
        nxt = tk.Label(tp, text="Следующий совет  \u2192", bg=C["card"], fg=C["violet"], font=font(10, True),
                       cursor="hand2")
        nxt.pack(anchor="w", side="bottom")
        nxt.bind("<Button-1>", lambda e: self._next_tip())
        nxt.bind("<Enter>", lambda e: nxt.configure(fg=C["text"]))
        nxt.bind("<Leave>", lambda e: nxt.configure(fg=C["violet"]))

    @staticmethod
    def _short_path(p, keep=3):
        parts = Path(p).parts
        return str(p) if len(parts) <= keep + 1 else "\u2026" + os.sep + os.sep.join(parts[-keep:])

    def update_summary(self):
        if not hasattr(self, "sum"):
            return
        g = self.gather()
        tgt = self.current_target()
        acct = self.active_account()
        self.sum["launch"].configure(text=self.v_launch.get() or "—")
        self.sum["acc"].configure(text=account_label(acct) if acct else "—")
        self.sum["folder"].configure(text=self._short_path(tgt["game_dir"]) if tgt else "—")
        self.sum["mem"].configure(text=f"{g['ram']} МБ  (старт {min(g['xms'], g['ram'])} МБ)")
        extra = f"  + {len(g['extra_flags'])} доп." if g["extra_flags"] else ""
        self.sum["flags"].configure(text=FLAG_PRESETS.get(g["preset"], FLAG_PRESETS["aikar"])[0] + extra)
        self.sum["java"].configure(text="своя" if g["java_path"] else "авто (официальная от Mojang)")
        self._update_profile()
        if hasattr(self, "hero"):
            nm = acct["name"] if acct else "игрок"
            sub = (f"Готов запустить {self.v_launch.get()}" if self.v_launch.get()
                   else "Выберите версию внизу и нажмите «Играть»")
            if getattr(self, "_hero_key", None) != (nm, sub):
                self._hero_key = (nm, sub)
                self.hero.set_text(f"Привет, {nm}!", sub)

    # --- сборки
    def _build_builds(self, page):
        pad = GradFrame(page)
        pad.pack(fill="both", expand=True, padx=26, pady=20)
        pad.columnconfigure(1, weight=1)
        pad.rowconfigure(1, weight=1)
        self._title(pad, "Сборки", "Отдельные папки с модами, мирами и настройками").grid(
            row=0, column=0, columnspan=2, sticky="ew")
        left = GradFrame(pad)
        left.grid(row=1, column=0, sticky="ns", padx=(0, 20), pady=(12, 0))
        box = tk.Frame(left, bg=C["line"])
        box.pack(fill="both", expand=True)
        self.b_list = self._listbox(box, 30, 12)
        self.b_list.pack(fill="both", expand=True, padx=2, pady=2)
        self.b_list.bind("<<ListboxSelect>>", lambda e: self.show_build_info())
        row = GradFrame(left)
        row.pack(fill="x", pady=(10, 0))
        PixelButton(row, "СОЗДАТЬ", self.create_build_dialog, width=104, height=34, fs=9).pack(side="left")
        PixelButton(row, "КОПИЯ", self.build_copy, width=84, height=34, fs=9).pack(side="left", padx=6)
        PixelButton(row, "УДАЛИТЬ", self.build_delete, width=94, height=34, fs=9).pack(side="left")
        right = GradFrame(pad)
        right.grid(row=1, column=1, sticky="nsew", pady=(12, 0))
        self.b_info = GradLabel(right, text="", bg=C["bg1"], fg=C["text"], font=font(11), anchor="nw",
                               justify="left", wraplength=480)
        self.b_info.pack(anchor="w", fill="x")
        btns = GradFrame(right)
        btns.pack(anchor="w", pady=(18, 0))
        PixelButton(btns, "ВЫБРАТЬ ДЛЯ ИГРЫ", self.build_select, width=200, height=40, fs=10).pack(anchor="w")
        PixelButton(btns, "ОТКРЫТЬ ПАПКУ", self.build_open, width=200, height=40, fs=10).pack(
            anchor="w", pady=(8, 0))
        PixelButton(btns, "ИСКАТЬ МОДЫ", self.build_mods, width=200, height=40, fs=10).pack(
            anchor="w", pady=(8, 0))
        self._label(right, "Сборка — это отдельная папка игры со своими модами, мирами и настройками. "
                    "Загрузчик (Fabric/Quilt/Forge/NeoForge) устанавливается при первом запуске.",
                    muted=True, wrap=480).pack(anchor="w", pady=(18, 0))

    def selected_build(self):
        sel = self.b_list.curselection()
        if sel and sel[0] < len(self.instances):
            return self.instances[sel[0]]
        return None

    def refresh_builds_list(self, select=None):
        prev = self.selected_build()
        want = select or (prev["id"] if prev else None)
        self.b_list.delete(0, "end")
        for inst in self.instances:
            self.b_list.insert("end", f"{inst['name']}  ({inst['mc']} {LOADER_NAMES.get(inst['loader'])})")
        if self.instances:
            idx = next((i for i, x in enumerate(self.instances) if x["id"] == want), 0)
            self.b_list.selection_set(idx)
        self.show_build_info()

    def show_build_info(self):
        inst = self.selected_build()
        if not inst:
            self.b_info.configure(text="Сборок пока нет.\nНажмите «Создать», чтобы сделать первую.")
            return
        gd = instance_game_dir(inst)
        mods = len(list((gd / "mods").glob("*.jar"))) if (gd / "mods").exists() else 0
        self.b_info.configure(text=(
            f"Название:  {inst['name']}\nMinecraft:  {inst['mc']}\n"
            f"Загрузчик:  {LOADER_NAMES.get(inst['loader'], inst['loader'])}"
            + (f" ({inst['version_id']})" if inst.get("version_id") and inst["loader"] != "vanilla"
               else (" — установится при первом запуске" if inst["loader"] != "vanilla" else ""))
            + f"\nМодов:  {mods}\nПапка:  {gd}"))

    def create_build_dialog(self):
        d = Dialog(self.root, "Новая сборка")
        b = d.body
        v_name = tk.StringVar(value="Моя сборка")
        v_mc = tk.StringVar()
        v_loader = tk.StringVar(value="Fabric")
        rel = [v["id"] for v in (self.manifest or {}).get("versions", []) if v["type"] == "release"]
        if not rel:
            rel = [v for v in local_versions() if re.fullmatch(r"\d+\.\d+(\.\d+)?", v)]
        if rel:
            v_mc.set(rel[0])
        self._label(b, "Название").pack(anchor="w")
        self._entry(b, v_name, 34).pack(fill="x", pady=(2, 10))
        self._label(b, "Версия Minecraft").pack(anchor="w")
        self._combo(b, v_mc, rel, 20).pack(anchor="w", pady=(2, 10))
        self._label(b, "Загрузчик модов").pack(anchor="w")
        self._combo(b, v_loader, list(LOADER_NAMES.values()), 20).pack(anchor="w", pady=(2, 8))
        err = tk.Label(b, text="", bg=C["bg1"], fg=C["red"], font=font(9), anchor="w")
        err.pack(anchor="w")

        def ok():
            name, mc = v_name.get().strip(), v_mc.get().strip()
            if not name or not mc:
                err.configure(text="Укажите название и версию")
                return
            inst = create_instance(name, mc, LOADER_KEY.get(v_loader.get(), "vanilla"))
            d.destroy()
            self.reload_instances(select=inst["id"])
        PixelButton(b, "СОЗДАТЬ", ok, width=150, height=40, fs=11).pack(anchor="e", pady=(10, 0))

    def build_select(self):
        inst = self.selected_build()
        if inst:
            self.v_launch.set(instance_label(inst))
            self.show_page("home")

    def build_open(self):
        inst = self.selected_build()
        if inst:
            open_path(instance_game_dir(inst))

    def build_mods(self):
        inst = self.selected_build()
        if inst:
            self.v_launch.set(instance_label(inst))
            self.show_page("mods")

    def build_delete(self):
        inst = self.selected_build()
        if inst and messagebox.askyesno(APP_NAME, f"Удалить сборку «{inst['name']}» вместе с её модами и мирами?"):
            delete_instance(inst["id"])
            self.reload_instances()

    def build_copy(self):
        inst = self.selected_build()
        if not inst:
            return
        self.status.configure(text="Копирую сборку...")

        def work():
            try:
                new = duplicate_instance(inst["id"], inst["name"] + " (копия)")
                self.ui(lambda i=new["id"]: self.reload_instances(select=i))
            except Exception as e:
                self.ui(lambda m=str(e): messagebox.showerror(APP_NAME, m))
        threading.Thread(target=work, daemon=True).start()

    def reload_instances(self, select=None):
        self.instances = list_instances()
        self.refresh_targets()
        self.refresh_builds_list(select)
        self.update_summary()
        self.sync_mods_target()

    # --- библиотека модов
    def _build_mods(self, page):
        pad = GradFrame(page)
        pad.pack(fill="both", expand=True, padx=22, pady=14)
        self._title(pad, "Моды и ресурсы", "Поиск и установка с Modrinth").pack(
            fill="x", pady=(0, 10))
        self.mods_target = GradLabel(pad, text="", bg=C["bg1"], fg=C["yellow"], font=font(10, True),
                                    anchor="w", justify="left", wraplength=760)
        self.mods_target.pack(fill="x")
        self.v_ctype = tk.StringVar(value="Моды")
        self.v_mloader = tk.StringVar(value="Fabric")
        self.v_sort = tk.StringVar(value="По скачиваниям")
        self.v_mc_only = tk.BooleanVar(value=True)
        self.v_query = tk.StringVar()
        f1 = GradFrame(pad)
        f1.pack(fill="x", pady=(8, 0))
        for text, var, vals, w in (("Тип", self.v_ctype, [v[0] for v in CONTENT_TYPES.values()], 12),
                                   ("Загрузчик", self.v_mloader, LOADER_CHOICES, 10),
                                   ("Сортировка", self.v_sort, list(SORTS), 18)):
            col = GradFrame(f1)
            col.pack(side="left", padx=(0, 10))
            self._label(col, text, muted=True).pack(anchor="w")
            self._combo(col, var, vals, w).pack()
        f2 = GradFrame(pad)
        f2.pack(fill="x", pady=(10, 0))
        ent = self._entry(f2, self.v_query, 30)
        ent.pack(side="left", ipady=6, fill="x", expand=True)
        ent.bind("<Return>", lambda e: self.mods_search())
        PixelButton(f2, "ИСКАТЬ", self.mods_search, width=110, height=34, fs=10).pack(side="left", padx=8)
        PixelCheck(f2, "Только для моей версии", self.v_mc_only).pack(side="left", padx=8)
        f3 = GradFrame(pad)
        f3.pack(fill="x", pady=(10, 6))
        self.tab_btn = {}
        self.tab_btn["search"] = PixelButton(f3, "ПОИСК", lambda: self.set_mods_tab("search"), width=110,
                                             height=32, fs=10)
        self.tab_btn["search"].pack(side="left")
        self.tab_btn["installed"] = PixelButton(f3, "УСТАНОВЛЕННЫЕ", lambda: self.set_mods_tab("installed"),
                                                width=170, height=32, fs=10)
        self.tab_btn["installed"].pack(side="left", padx=6)
        PixelButton(f3, "ПАПКА", self.open_content_folder, width=90, height=32, fs=10).pack(side="left")
        stack = GradFrame(pad)
        stack.pack(fill="both", expand=True)
        stack.rowconfigure(0, weight=1)
        stack.columnconfigure(0, weight=1)
        self.tab_search = GradFrame(stack)
        self.tab_installed = GradFrame(stack)
        for t in (self.tab_search, self.tab_installed):
            t.grid(row=0, column=0, sticky="nsew")
        self.res_scroll = ScrollFrame(self.tab_search, C["bg1"])
        bot = GradFrame(self.tab_search)
        bot.pack(side="bottom", fill="x", pady=(6, 0))
        self.more_btn = PixelButton(bot, "ЕЩЁ", lambda: self.mods_search(more=True), width=100, height=32, fs=10)
        self.more_btn.pack(side="left")
        self.more_btn.set_enabled(False)
        self.mods_status_lbl = GradLabel(bot, text="Введите запрос и нажмите «Искать».", bg=C["bg1"],
                                        fg=C["muted"], font=font(9), anchor="w")
        self.mods_status_lbl.pack(side="left", padx=10)
        self.res_scroll.pack(fill="both", expand=True)
        self.inst_scroll = ScrollFrame(self.tab_installed, C["bg1"])
        self.inst_scroll.pack(fill="both", expand=True)
        self.set_mods_tab("search")

    def set_mods_tab(self, tab):
        for k, b in getattr(self, "tab_btn", {}).items():
            b._primary = (k == tab)
            b._draw()
        if tab == "installed":
            self.tab_installed.tkraise()
            self.scrolls["mods"] = self.inst_scroll
            self.refresh_installed()
        else:
            self.tab_search.tkraise()
            self.scrolls["mods"] = self.res_scroll

    def mods_status(self, text):
        self.mods_status_lbl.configure(text=text)

    def _ctype(self):
        return TYPE_BY_LABEL.get(self.v_ctype.get(), "mods")

    def sync_mods_target(self):
        if not hasattr(self, "mods_target") or not hasattr(self, "inst_scroll"):
            return
        tgt = self.current_target()
        if not tgt:
            self.mods_target.configure(text="Выберите версию или сборку внизу окна.")
            return
        self.mods_target.configure(text=f"Куда:  {self.v_launch.get()}   →   {tgt['game_dir']}")
        if tgt["loader"] and tgt["loader"] != "vanilla":
            self.v_mloader.set(LOADER_NAMES.get(tgt["loader"], "Fabric"))
        self.refresh_installed()

    def open_content_folder(self):
        tgt = self.current_target()
        if tgt:
            open_path(tgt["game_dir"] / CONTENT_TYPES[self._ctype()][3])

    def mods_search(self, more=False):
        if self._busy_search:
            return
        tgt = self.current_target()
        if not tgt:
            self.mods_status("Сначала выберите версию или сборку")
            return
        if not more:
            ctype = self._ctype()
            loader = LOADER_KEY.get(self.v_mloader.get()) if ctype == "mods" else None
            self._sq = dict(ctype=ctype,
                            mc=tgt["mc"] if self.v_mc_only.get() else None, loader=loader,
                            sort=SORTS.get(self.v_sort.get(), "downloads"),
                            query=self.v_query.get().strip(), offset=0)
        q = dict(self._sq)
        self._busy_search = True
        self.mods_status("Ищу...")

        def work():
            try:
                items, total = modrinth_search(q["query"], q["ctype"], q["mc"], q["loader"], q["sort"], q["offset"])
                self.ui(lambda: self.render_results(items, total, more))
            except Exception as e:
                self.ui(lambda m=str(e): self.mods_status("Ошибка поиска: " + m))
            finally:
                self.ui(lambda: setattr(self, "_busy_search", False))
        threading.Thread(target=work, daemon=True).start()

    def render_results(self, items, total, append):
        body = self.res_scroll.body
        if not append:
            for w in body.winfo_children():
                w.destroy()
            self._shown = 0
        for it in items:
            self._result_row(body, it)
        self._shown += len(items)
        self._sq["offset"] = self._shown
        self.more_btn.set_enabled(self._shown < total and bool(items))
        self.mods_status(f"Найдено: {total}. Показано: {self._shown}" if (items or append)
                         else "Ничего не найдено — попробуйте другой запрос или снимите фильтр версии")

    def _result_row(self, body, it):
        row = PixelFrame(body, step=4, padx=0, pady=0)
        row.pack(fill="x", pady=4, padx=(0, 6))
        row.columnconfigure(0, weight=1)
        info = tk.Frame(row, bg=C["card"])
        info.grid(row=0, column=0, sticky="ew", padx=12, pady=8)
        tk.Label(info, text=it["title"], bg=C["card"], fg=C["text"], font=font(11, True),
                 anchor="w").pack(anchor="w")
        tk.Label(info, text=f"{it['author']}   ·   скачиваний: {fmt_num(it['downloads'])}", bg=C["card"],
                 fg=C["yellow"], font=font(9), anchor="w").pack(anchor="w")
        tk.Label(info, text=it["desc"], bg=C["card"], fg=C["muted"], font=font(9), anchor="w",
                 justify="left", wraplength=480).pack(anchor="w")
        PixelButton(row, "СКАЧАТЬ", lambda i=it: self.mods_install(i), width=120, height=36, fs=10,
                    bg=C["card"]).grid(row=0, column=1, padx=12)

    def mods_install(self, it):
        tgt = self.current_target()
        if not tgt:
            self.mods_status("Сначала выберите версию или сборку")
            return
        ctype = self._ctype()
        folder = CONTENT_TYPES[ctype][3]
        loader = tgt["loader"] if tgt["loader"] and tgt["loader"] != "vanilla" else LOADER_KEY.get(self.v_mloader.get())
        if ctype != "mods":
            loader = None
        mc = tgt["mc"]
        dest = tgt["game_dir"] / folder
        self.mods_status(f"Скачиваю: {it['title']}...")

        def work():
            try:
                done = modrinth_install(it["id"], ctype, mc, loader, dest, self.log)
                self.ui(lambda n=len(done): (self.mods_status(f"Готово: {it['title']} (файлов: {n})"),
                                             self.refresh_installed()))
            except Exception as e:
                self.ui(lambda m=str(e): self.mods_status("Ошибка: " + m))
        threading.Thread(target=work, daemon=True).start()

    def refresh_installed(self):
        if not hasattr(self, "inst_scroll"):
            return
        body = self.inst_scroll.body
        for w in body.winfo_children():
            w.destroy()
        tgt = self.current_target()
        if not tgt:
            return
        d = tgt["game_dir"] / CONTENT_TYPES[self._ctype()][3]
        files = sorted([f for f in d.iterdir() if f.is_file()], key=lambda p: p.name.lower()) if d.exists() else []
        if not files:
            GradLabel(body, text="Здесь пока ничего нет.", bg=C["bg1"], fg=C["muted"], font=font(10)).pack(
                anchor="w", pady=10)
            return
        for f in files:
            off = f.name.endswith(".disabled")
            row = PixelFrame(body, step=4, padx=0, pady=0)
            row.pack(fill="x", pady=3, padx=(0, 6))
            tk.Label(row, text=("[выкл]  " if off else "") + f.name.replace(".disabled", ""), bg=C["card"],
                     fg=C["muted"] if off else C["text"], font=font(10), anchor="w").pack(
                side="left", padx=12, pady=8)
            PixelButton(row, "УДАЛИТЬ", lambda p=f: self.mod_delete(p), width=96, height=30, fs=9,
                        bg=C["card"]).pack(side="right", padx=8, pady=4)
            PixelButton(row, "ВКЛЮЧИТЬ" if off else "ВЫКЛЮЧИТЬ", lambda p=f: self.mod_toggle(p),
                        width=116, height=30, fs=9, bg=C["card"]).pack(side="right", pady=4)

    def mod_toggle(self, path):
        try:
            if path.name.endswith(".disabled"):
                path.rename(path.with_name(path.name[:-len(".disabled")]))
            else:
                path.rename(path.with_name(path.name + ".disabled"))
        except Exception as e:
            messagebox.showerror(APP_NAME, str(e))
        self.refresh_installed()

    def mod_delete(self, path):
        if messagebox.askyesno(APP_NAME, f"Удалить файл {path.name}?"):
            try:
                path.unlink()
            except Exception as e:
                messagebox.showerror(APP_NAME, str(e))
            self.refresh_installed()

    # --- аккаунты
    def _build_accounts(self, page):
        pad = GradFrame(page)
        pad.pack(fill="both", expand=True, padx=26, pady=20)
        pad.columnconfigure(1, weight=1)
        pad.rowconfigure(1, weight=1)
        self._title(pad, "Аккаунты", "Офлайн, Ely.by и Microsoft — у каждого свои скины").grid(
            row=0, column=0, columnspan=2, sticky="ew")
        left = GradFrame(pad)
        left.grid(row=1, column=0, sticky="ns", padx=(0, 20), pady=(12, 0))
        box = tk.Frame(left, bg=C["line"])
        box.pack(fill="both", expand=True)
        self.acc_list = self._listbox(box, 32, 10)
        self.acc_list.pack(fill="both", expand=True, padx=2, pady=2)
        self.acc_list.bind("<<ListboxSelect>>", lambda e: self.show_account_info())
        self.acc_list.bind("<Double-Button-1>", lambda e: self.use_selected_account())
        right = GradFrame(pad)
        right.grid(row=1, column=1, sticky="nsew", pady=(12, 0))
        self.acc_info = GradLabel(right, text="", bg=C["bg1"], fg=C["text"], font=font(10), anchor="nw",
                                 justify="left", wraplength=440)
        self.acc_info.pack(anchor="w", fill="x")
        btns = GradFrame(right)
        btns.pack(anchor="w", pady=(14, 0))
        for text, cmd in (("ИСПОЛЬЗОВАТЬ", self.use_selected_account), ("+ ОФЛАЙН", self.add_offline_dialog),
                          ("+ ELY.BY", self.add_ely_dialog), ("+ MICROSOFT", self.add_ms_dialog),
                          ("УДАЛИТЬ", self.delete_selected_account)):
            PixelButton(btns, text, cmd, width=210, height=38, fs=10).pack(anchor="w", pady=3)
        self._label(right, "Ely.by даёт скины: нужен authlib-injector, лаунчер скачает его сам. "
                    "Токены входа хранятся в config.json на этом компьютере; пароли не сохраняются.",
                    muted=True, wrap=440).pack(anchor="w", pady=(10, 0))

    def active_account(self):
        aid = self.cfg.get("active_account")
        return next((a for a in self.cfg.get("accounts", []) if a["id"] == aid), None)

    def selected_account(self):
        sel = self.acc_list.curselection()
        accs = self.cfg["accounts"]
        if sel and sel[0] < len(accs):
            return accs[sel[0]]
        return None

    def refresh_accounts(self):
        accs = self.cfg["accounts"]
        active = self.cfg.get("active_account")
        self.acc_list.delete(0, "end")
        self.acc_map = {}
        labels = []
        for a in accs:
            self.acc_list.insert("end", ("● " if a["id"] == active else "   ") + account_label(a))
            lab = base = account_label(a)
            n = 2
            while lab in self.acc_map:
                lab = f"{base} #{n}"
                n += 1
            self.acc_map[lab] = a["id"]
            labels.append(lab)
        self.acc_combo["values"] = labels
        self.v_account.set(next((l for l, i in self.acc_map.items() if i == active), ""))
        for i, a in enumerate(accs):
            if a["id"] == active:
                self.acc_list.selection_set(i)
        self.show_account_info()
        self.update_summary()

    def show_account_info(self):
        a = self.selected_account() or self.active_account()
        if not a:
            self.acc_info.configure(text="")
            return
        skin = {"ely": "скины Ely.by", "microsoft": "официальные скины", "offline": "без скинов"}[a["type"]]
        self.acc_info.configure(text=(f"Ник:  {a['name']}\nТип:  {ACC_TYPES.get(a['type'])}  ({skin})\n"
                                      f"UUID:  {a.get('uuid', '—')}\n"
                                      + ("Сейчас используется для запуска" if a["id"] == self.cfg.get("active_account") else "")))

    def set_active_account(self, aid):
        self.cfg["active_account"] = aid
        self.refresh_accounts()
        self.save_all()

    def use_selected_account(self):
        a = self.selected_account()
        if a:
            self.set_active_account(a["id"])

    def on_account_combo(self, e=None):
        aid = self.acc_map.get(self.v_account.get())
        if aid:
            self.set_active_account(aid)

    def delete_selected_account(self):
        a = self.selected_account()
        if not a or not messagebox.askyesno(APP_NAME, f"Удалить аккаунт {account_label(a)}?"):
            return
        self.cfg["accounts"] = [x for x in self.cfg["accounts"] if x["id"] != a["id"]]
        self._ensure_accounts()
        self.refresh_accounts()
        self.save_all()

    def _account_added(self, acct, dialog=None):
        self.cfg["accounts"].append(acct)
        self.set_active_account(acct["id"])
        if dialog is not None:
            try:
                dialog.destroy()
            except Exception:
                pass

    def add_offline_dialog(self):
        d = Dialog(self.root, "Офлайн-аккаунт", 380)
        b = d.body
        v = tk.StringVar()
        self._label(b, "Ник (3–16 символов: латиница, цифры, _)").pack(anchor="w")
        self._entry(b, v, 28).pack(fill="x", pady=(2, 8))
        err = tk.Label(b, text="", bg=C["bg1"], fg=C["red"], font=font(9), anchor="w")
        err.pack(anchor="w")

        def ok():
            name = v.get().strip()
            if not NAME_RE.fullmatch(name):
                err.configure(text="Недопустимый ник")
                return
            self._account_added(new_offline(name), d)
        PixelButton(b, "ДОБАВИТЬ", ok, width=140, height=38, fs=11).pack(anchor="e", pady=(8, 0))

    def add_ely_dialog(self):
        d = Dialog(self.root, "Вход через Ely.by", 440)
        b = d.body
        v_login, v_pw, v_2fa = tk.StringVar(), tk.StringVar(), tk.StringVar()
        self._label(b, "Ник или e-mail").pack(anchor="w")
        self._entry(b, v_login, 36).pack(fill="x", pady=(2, 8))
        self._label(b, "Пароль").pack(anchor="w")
        self._entry(b, v_pw, 36, show="•").pack(fill="x", pady=(2, 8))
        self._label(b, "Код двухфакторной защиты (только если включена)").pack(anchor="w")
        self._entry(b, v_2fa, 12).pack(anchor="w", pady=(2, 8))
        err = tk.Label(b, text="", bg=C["bg1"], fg=C["red"], font=font(9), anchor="w", justify="left",
                       wraplength=380)
        err.pack(anchor="w")

        def go():
            login, pw = v_login.get().strip(), v_pw.get()
            if not login or not pw:
                err.configure(text="Введите логин и пароль", fg=C["red"])
                return
            btn.set_enabled(False)
            err.configure(text="Входим...", fg=C["muted"])

            def fail(m):
                err.configure(text=m, fg=C["red"])
                btn.set_enabled(True)

            def work():
                try:
                    acct = ely_login(login, pw, v_2fa.get())
                    self.ui(lambda: self._account_added(acct, d))
                except AuthError as e:
                    m = str(e) + (" — введите код 2FA и повторите." if e.two_factor else "")
                    self.ui(lambda mm=m: fail(mm))
                except Exception as e:
                    self.ui(lambda mm=f"Ошибка сети: {e}": fail(mm))
            threading.Thread(target=work, daemon=True).start()
        btn = PixelButton(b, "ВОЙТИ", go, width=140, height=38, fs=11)
        btn.pack(anchor="e", pady=(8, 0))

    def add_ms_dialog(self):
        cid = self.v_ms_cid.get().strip() or DEFAULT_MS_CLIENT_ID
        if not cid:
            self._ms_setup_dialog()
            return
        d = Dialog(self.root, "Вход через Microsoft", 480)
        b = d.body
        status = tk.Label(b, text="Запрашиваю код входа...", bg=C["bg1"], fg=C["muted"], font=font(10),
                          anchor="w", justify="left", wraplength=420)
        status.pack(anchor="w")
        code_lbl = tk.Label(b, text="", bg=C["bg1"], fg=C["yellow"], font=font(26, True))
        code_lbl.pack(pady=10)
        cancel = {"flag": False}
        info = {"url": "https://www.microsoft.com/link", "code": ""}

        def on_close():
            cancel["flag"] = True
            d.destroy()
        d.protocol("WM_DELETE_WINDOW", on_close)
        row = tk.Frame(b, bg=C["bg1"])
        row.pack(anchor="w")
        PixelButton(row, "ОТКРЫТЬ СТРАНИЦУ", lambda: webbrowser.open(info["url"]), width=190, height=36,
                    fs=10).pack(side="left")
        PixelButton(row, "КОПИРОВАТЬ КОД", lambda: (self.root.clipboard_clear(),
                                                    self.root.clipboard_append(info["code"])),
                    width=190, height=36, fs=10).pack(side="left", padx=8)

        def show_code(dev):
            info["url"] = dev.get("verification_uri", info["url"])
            info["code"] = dev["user_code"]
            code_lbl.configure(text=dev["user_code"])
            status.configure(text=f"Откройте {info['url']} и введите этот код. Окно ждёт входа...",
                             fg=C["text"])
            try:
                webbrowser.open(info["url"])
            except Exception:
                pass

        def fail(m):
            status.configure(text="Ошибка: " + m, fg=C["red"])

        def work():
            try:
                dev = ms_device_start(cid)
                self.ui(lambda: show_code(dev))
                tok = ms_device_poll(cid, dev, lambda: cancel["flag"])
                self.ui(lambda: status.configure(text="Входим в Minecraft...", fg=C["muted"]))
                acct = ms_account_from_tokens(cid, tok)
                self.ui(lambda: self._account_added(acct, d))
            except AuthError as e:
                self.ui(lambda m=str(e): fail(m))
            except Exception as e:
                self.ui(lambda m=f"Ошибка сети: {e}": fail(m))
        threading.Thread(target=work, daemon=True).start()

    def _ms_setup_dialog(self):
        d = Dialog(self.root, "Microsoft: нужен Client ID", 520)
        b = d.body
        txt = ("Microsoft требует собственное приложение для входа:\n"
               "1. portal.azure.com → App registrations → New registration.\n"
               "2. Тип аккаунтов: «Personal Microsoft accounts only».\n"
               "3. Authentication → Allow public client flows = Yes.\n"
               "4. Скопируйте Application (client) ID и вставьте ниже.\n"
               "5. Mojang должен одобрить ваш ID для Minecraft API (форма aka.ms/mce-reviewappid), "
               "иначе вход закончится ошибкой «Invalid app registration».")
        tk.Label(b, text=txt, bg=C["bg1"], fg=C["text"], font=font(10), anchor="w", justify="left",
                 wraplength=470).pack(anchor="w")
        self._label(b, "Application (client) ID").pack(anchor="w", pady=(10, 0))
        self._entry(b, self.v_ms_cid, 40).pack(fill="x", pady=(2, 10))
        row = tk.Frame(b, bg=C["bg1"])
        row.pack(anchor="e")
        PixelButton(row, "ОТКРЫТЬ AZURE", lambda: webbrowser.open("https://portal.azure.com"), width=170,
                    height=38, fs=10).pack(side="left")

        def save():
            if self.v_ms_cid.get().strip():
                self.save_all()
                d.destroy()
                self.add_ms_dialog()
        PixelButton(row, "СОХРАНИТЬ И ВОЙТИ", save, width=210, height=38, fs=10).pack(side="left", padx=8)

    # --- настройки
    def _build_settings(self, page):
        sf = ScrollFrame(page, C["bg1"])
        sf.pack(fill="both", expand=True)
        self.scrolls["settings"] = sf
        pad = GradFrame(sf.body)
        pad.pack(fill="both", expand=True, padx=26, pady=20)
        pad.columnconfigure(0, weight=1, uniform="col")
        pad.columnconfigure(1, weight=1, uniform="col")
        self._title(pad, "Настройки", "Память, оптимизация Java, окно игры и сам лаунчер").grid(
            row=0, column=0, columnspan=2, sticky="ew", pady=(0, 16))
        left = GradFrame(pad)
        left.grid(row=1, column=0, sticky="new", padx=(0, 14))
        right = GradFrame(pad)
        right.grid(row=1, column=1, sticky="new", padx=(14, 0))

        # --- память
        s = self._section(left, "Память")
        s.pack(fill="x", pady=(0, 14))
        row = tk.Frame(s, bg=C["card"])
        row.pack(fill="x")
        self._label(row, "Максимум (Xmx)").pack(side="left")
        self.ram_label = tk.Label(row, text="", bg=C["card"], fg=C["yellow"], font=font(11, True))
        self.ram_label.pack(side="right")
        PixelSlider(s, self.v_ram, 1024, self.max_ram, 256).pack(fill="x", pady=(4, 6))
        self.v_ram.trace_add("write", lambda *a: self._ram_text())
        self._ram_text()
        row = tk.Frame(s, bg=C["card"])
        row.pack(fill="x", pady=(2, 4))
        self._label(row, "Старт (Xms, МБ)").pack(side="left")
        tk.Spinbox(row, from_=256, to=self.max_ram, increment=256, textvariable=self.v_xms, width=7,
                   bg=C["input"], fg=C["text"], insertbackground=C["yellow"],
                   buttonbackground=C["bg2"], relief="flat", highlightthickness=2,
                   highlightbackground=C["line"], highlightcolor=C["yellow"],
                   font=font(11)).pack(side="right")
        row = tk.Frame(s, bg=C["card"])
        row.pack(fill="x", pady=(6, 0))
        PixelButton(row, "АВТО", self.auto_ram, width=90, height=30, fs=9).pack(side="left")
        self._label(row, f"  всего ОЗУ: {self.total_ram} МБ", muted=True).pack(side="left")

        # --- флаги
        s = self._section(left, "Флаги оптимизации (JVM)")
        s.pack(fill="x", pady=(0, 14))
        self._label_to_key = {v[0]: k for k, v in FLAG_PRESETS.items()}
        self.preset_combo = PixelSelect(s, values=[v[0] for v in FLAG_PRESETS.values()], width=34, fs=10)
        self.preset_combo.set(FLAG_PRESETS[self.v_preset.get()][0])
        self.preset_combo.pack(fill="x")
        self.preset_combo.bind("<<ComboboxSelected>>", self._preset_changed)
        self.preset_desc = self._label(s, FLAG_PRESETS[self.v_preset.get()][1], muted=True, wrap=380)
        self.preset_desc.pack(anchor="w", pady=(6, 8))
        for k, (text, _fl) in EXTRA_FLAGS.items():
            PixelCheck(s, text, self.v_extra[k], self.update_summary).pack(anchor="w", pady=2)
        self._label(s, "Свои JVM-аргументы (необязательно)").pack(anchor="w", pady=(8, 0))
        self._entry(s, self.v_custom_jvm, 30).pack(fill="x", pady=(4, 0))

        # --- папки и сервис
        s = self._section(left, "Папки и сервис")
        s.pack(fill="x", pady=(0, 14))
        PixelCheck(s, "Отдельная папка для каждой версии", self.v_perver, self._folders_changed).pack(
            anchor="w", pady=2)
        PixelCheck(s, "Проверять целостность файлов при запуске", self.v_verify).pack(anchor="w", pady=2)
        PixelCheck(s, "Показывать команду запуска в логе", self.v_showcmd).pack(anchor="w", pady=2)
        r1 = tk.Frame(s, bg=C["card"])
        r1.pack(anchor="w", pady=(8, 0))
        PixelButton(r1, "ПАПКА MINECRAFT", self.open_minecraft_folder, width=178, height=34, fs=9).pack(side="left")
        PixelButton(r1, "ПАПКА ВЕРСИИ", self.open_version_folder, width=160, height=34, fs=9).pack(
            side="left", padx=6)
        r2 = tk.Frame(s, bg=C["card"])
        r2.pack(anchor="w", pady=(6, 0))
        PixelButton(r2, "БЭКАП МИРОВ", self.do_backup, width=140, height=34, fs=9).pack(side="left")
        PixelButton(r2, "ИМПОРТ ИЗ .MINECRAFT", self.do_import, width=210, height=34, fs=9).pack(
            side="left", padx=6)
        r3 = tk.Frame(s, bg=C["card"])
        r3.pack(anchor="w", pady=(6, 0))
        PixelButton(r3, "ЯРЛЫК НА РАБОЧИЙ СТОЛ", self.do_shortcut, width=230, height=34, fs=9).pack(side="left")
        PixelButton(r3, "СБРОС НАСТРОЕК", self.reset_settings, width=170, height=34, fs=9).pack(
            side="left", padx=6)

        # --- игра
        s = self._section(right, "Игра")
        s.pack(fill="x", pady=(0, 14))
        PixelCheck(s, "Свой размер окна", self.v_custom_res).pack(anchor="w")
        row = tk.Frame(s, bg=C["card"])
        row.pack(anchor="w", pady=6)
        self._entry(row, self.v_w, 6).pack(side="left")
        self._label(row, "  ×  ").pack(side="left")
        self._entry(row, self.v_h, 6).pack(side="left")
        PixelCheck(s, "Полный экран", self.v_full).pack(anchor="w", pady=(0, 8))
        self._label(s, "Доп. аргументы игры").pack(anchor="w")
        self._entry(s, self.v_game_args, 30).pack(fill="x", pady=(4, 0))

        # --- java
        s = self._section(right, "Java")
        s.pack(fill="x", pady=(0, 14))
        row = tk.Frame(s, bg=C["card"])
        row.pack(fill="x")
        self._entry(row, self.v_java, 24).pack(side="left", fill="x", expand=True, padx=(0, 8))
        PixelButton(row, "ОБЗОР", self.browse_java, width=90, height=34, fs=10).pack(side="right")
        self._label(s, "Пусто = автоматически скачать нужную версию Java.", muted=True,
                    wrap=380).pack(anchor="w", pady=(6, 0))

        # --- интеграции
        s = self._section(right, "Интеграции")
        s.pack(fill="x", pady=(0, 14))
        self._label(s, "Microsoft Client ID (необязательно)").pack(anchor="w")
        self._entry(s, self.v_ms_cid, 30).pack(fill="x", pady=(4, 8))
        self._label(s, "Нужен только для входа через Microsoft. Моды ищутся на Modrinth, ключи не нужны.",
                    muted=True, wrap=380).pack(anchor="w", pady=(2, 0))

        # --- лаунчер
        s = self._section(right, "Лаунчер")
        s.pack(fill="x")
        for text, var, cmd in (
                ("Анимированный фон", self.v_anim, None),
                ("Скрывать лаунчер, пока идёт игра", self.v_hide, None),
                ("Закрывать лаунчер после запуска игры", self.v_close, None),
                ("Высокий приоритет процесса игры", self.v_prio, None),
                ("Показывать снапшоты", self.v_snap, self.refresh_targets),
                ("Старые версии (beta/alpha, эксперимент.)", self.v_old, self.refresh_targets)):
            PixelCheck(s, text, var, cmd).pack(anchor="w", pady=3)
        row = tk.Frame(s, bg=C["card"])
        row.pack(anchor="w", pady=(6, 0))
        self._label(row, "Скорость анимации  ").pack(side="left")
        self._combo(row, self.v_speed, list(self.SPEEDS), 12).pack(side="left")

    def _ram_text(self):
        try:
            self.ram_label.configure(text=f"{int(self.v_ram.get())} МБ")
        except Exception:
            pass

    def auto_ram(self):
        self.v_ram.set(self.default_ram)
        self.v_xms.set(min(1024, self.default_ram))

    def _preset_changed(self, e=None):
        key = self._label_to_key.get(self.preset_combo.get(), "aikar")
        self.v_preset.set(key)
        self.preset_desc.configure(text=FLAG_PRESETS[key][1])

    def _folders_changed(self):
        self.update_summary()
        self.sync_mods_target()

    def browse_java(self):
        p = filedialog.askopenfilename(title="Выберите java.exe" if IS_WIN else "Выберите java",
                                       filetypes=[("Java", "java.exe" if IS_WIN else "java"),
                                                  ("Все файлы", "*.*")])
        if p:
            self.v_java.set(p)

    def open_minecraft_folder(self):
        try:
            open_path(BASE)
        except Exception as e:
            messagebox.showerror(APP_NAME, str(e))

    def open_version_folder(self):
        tgt = self.current_target()
        if not tgt:
            messagebox.showerror(APP_NAME, "Сначала выберите версию или сборку внизу окна")
            return
        try:
            open_path(tgt["game_dir"])
        except Exception as e:
            messagebox.showerror(APP_NAME, str(e))

    def do_backup(self):
        tgt = self.current_target()
        if not tgt:
            return

        def work():
            try:
                p = backup_saves(tgt["game_dir"], self.v_launch.get())
                self.ui(lambda m=f"Резервная копия миров создана:\n{p}": messagebox.showinfo(APP_NAME, m))
            except Exception as e:
                self.ui(lambda m=str(e): messagebox.showerror(APP_NAME, m))
        threading.Thread(target=work, daemon=True).start()

    def do_import(self):
        tgt = self.current_target()
        if not tgt:
            return
        src = official_minecraft_dir()
        if not messagebox.askyesno(APP_NAME, f"Скопировать миры, ресурспаки, шейдеры, настройки и список серверов "
                                             f"из {src}\nв {tgt['game_dir']}?\nСуществующие файлы будут перезаписаны."):
            return

        def work():
            try:
                done = import_from_official(tgt["game_dir"])
                self.ui(lambda m="Скопировано: " + (", ".join(done) or "ничего не найдено"):
                        messagebox.showinfo(APP_NAME, m))
            except Exception as e:
                self.ui(lambda m=str(e): messagebox.showerror(APP_NAME, m))
        threading.Thread(target=work, daemon=True).start()

    def do_shortcut(self):
        self.status.configure(text="Создаю ярлык...")

        def work():
            try:
                p = create_desktop_shortcut()
                self.ui(lambda: (self.status.configure(text="Ярлык создан"),
                                 messagebox.showinfo(APP_NAME, f"Ярлык создан на рабочем столе:\n{p}\n\n"
                                                     "Значок у него тот же (ueban.ico), что и у лаунчера.")))
            except Exception as e:
                self.ui(lambda m=str(e): (self.status.configure(text="Не удалось создать ярлык"),
                                          messagebox.showerror(APP_NAME, m)))
        threading.Thread(target=work, daemon=True).start()

    def reset_settings(self):
        if not messagebox.askyesno(APP_NAME, "Сбросить все настройки? Аккаунты и сборки останутся."):
            return
        keep = {k: self.cfg[k] for k in ("accounts", "active_account") if k in self.cfg}
        save_cfg(keep)
        self._skip_save = True
        messagebox.showinfo(APP_NAME, "Настройки сброшены. Перезапустите лаунчер.")

    # --- лог
    def _build_log(self, page):
        pad = GradFrame(page)
        pad.pack(fill="both", expand=True, padx=22, pady=18)
        self._title(pad, "Лог игры", "Здесь видно, что делает лаунчер и почему игра могла не запуститься").pack(
            fill="x", pady=(0, 10))
        box = tk.Frame(pad, bg=C["line"])
        box.pack(fill="both", expand=True)
        self.text = tk.Text(box, wrap="word", state="disabled", bg=C["input"], fg=C["text"],
                            insertbackground=C["yellow"], relief="flat", bd=0, font=("Consolas", -14),
                            padx=10, pady=8, highlightthickness=0)
        sbar = ttk.Scrollbar(box, style="U.Vertical.TScrollbar", command=self.text.yview)
        self.text.configure(yscrollcommand=sbar.set)
        sbar.pack(side="right", fill="y", padx=(0, 2), pady=2)
        self.text.pack(side="left", fill="both", expand=True, padx=2, pady=2)
        self.text.tag_configure("err", foreground=C["red"])
        row = GradFrame(pad)
        row.pack(fill="x", pady=(10, 0))
        PixelButton(row, "ОЧИСТИТЬ", self.clear_log, width=130, height=36, fs=10).pack(side="left")
        PixelButton(row, "КОПИРОВАТЬ", self.copy_log, width=140, height=36, fs=10).pack(side="left", padx=10)

    def _build_bottom(self, parent):
        bottom = tk.Frame(parent, bg=C["bg0"])
        bottom.grid(row=1, column=0, columnspan=2, sticky="ew")
        tk.Frame(bottom, bg=C["line"], height=1).pack(fill="x")
        strip = tk.Frame(bottom, bg=C["bg0"])
        strip.pack(fill="x", padx=24, pady=(10, 0))
        self.status = tk.Label(strip, text="Загрузка списка версий...", bg=C["bg0"], fg=C["muted"],
                               font=font(9), anchor="w")
        self.status.pack(fill="x")
        self.pbar = SmoothProgress(strip, bg=C["bg0"])
        self.pbar.pack(fill="x", pady=(3, 0))

        ctrl = tk.Frame(bottom, bg=C["bg0"])
        ctrl.pack(fill="x", padx=24, pady=(10, 16))
        ctrl.columnconfigure(0, weight=1, uniform="c")
        ctrl.columnconfigure(1, weight=0)
        ctrl.columnconfigure(2, weight=1, uniform="c")

        left = tk.Frame(ctrl, bg=C["bg0"])
        left.grid(row=0, column=0, sticky="w")
        tk.Label(left, text="ВЕРСИЯ / СБОРКА", bg=C["bg0"], fg=C["muted"], font=font(9, True)).pack(anchor="w")
        self.combo = PixelSelect(left, textvariable=self.v_launch, width=28, fs=11)
        self.combo.pack(pady=(4, 0))

        self.play = PixelButton(ctrl, "ИГРАТЬ", self.on_play, width=320, height=64, fs=18, bg=C["bg0"],
                                kind="primary", pulse=True)
        self.play.grid(row=0, column=1)

        right = tk.Frame(ctrl, bg=C["bg0"])
        right.grid(row=0, column=2, sticky="e")
        tk.Label(right, text="АККАУНТ", bg=C["bg0"], fg=C["muted"], font=font(9, True)).pack(anchor="w")
        self.acc_combo = PixelSelect(right, textvariable=self.v_account, width=26, fs=11)
        self.acc_combo.pack(pady=(4, 0))
        self.acc_combo.bind("<<ComboboxSelected>>", self.on_account_combo)

    # ───── версии и цели запуска ─────
    def load_manifest(self):
        try:
            self.manifest = http_json(MANIFEST_URL)
        except Exception as e:
            self.q.put(("log", f"Нет связи с Mojang ({e}). Доступны только установленные версии."))
        self.q.put(("versions", None))

    def refresh_targets(self):
        types = {"release"}
        if self.v_snap.get():
            types.add("snapshot")
        if self.v_old.get():
            types |= {"old_beta", "old_alpha"}
        vs = []
        if self.manifest:
            vs = [v["id"] for v in self.manifest["versions"] if v["type"] in types]
        for lv in local_versions():
            if lv not in vs:
                vs.append(lv)
        self.launch_map = {}
        labels = []
        for inst in self.instances:
            lab = instance_label(inst)
            self.launch_map[lab] = ("instance", inst["id"])
            labels.append(lab)
        for v in vs:
            self.launch_map[v] = ("version", v)
            labels.append(v)
        self.combo["values"] = labels
        if self.v_launch.get() not in self.launch_map:
            want = None
            k, i = self.cfg.get("launch_kind"), self.cfg.get("launch_id")
            for lab, (kk, ii) in self.launch_map.items():
                if (kk, ii) == (k, i):
                    want = lab
                    break
            if not want and labels:
                latest = self.manifest["latest"]["release"] if self.manifest else None
                want = latest if latest in self.launch_map else labels[0]
            if want:
                self.v_launch.set(want)

    def current_target(self):
        kind, ident = self.launch_map.get(self.v_launch.get(), (None, None))
        cfg = self.gather()
        if kind == "instance":
            inst = next((i for i in self.instances if i["id"] == ident), None)
            if inst:
                return dict(instance=inst, version=inst.get("version_id") or inst["mc"], mc=inst["mc"],
                            loader=inst["loader"], game_dir=instance_game_dir(inst))
        elif kind == "version":
            mc, loader = version_info(ident)
            return dict(instance=None, version=ident, mc=mc, loader=loader,
                        game_dir=resolve_game_dir(cfg, ident, None))
        return None

    def _on_launch_change(self):
        kind, ident = self.launch_map.get(self.v_launch.get(), (None, None))
        if kind:
            self.cfg["launch_kind"], self.cfg["launch_id"] = kind, ident
        self.update_summary()
        self.sync_mods_target()

    # ───── действия ─────
    def log(self, text):
        self.q.put(("log", text))

    def _append_log(self, data):
        err = "ОШИБКА" in data or "Exception" in data or "Traceback" in data
        self.text.configure(state="normal")
        if err:
            self.text.insert("end", data + "\n", "err")
        else:
            self.text.insert("end", data + "\n")
        self.text.see("end")
        self.text.configure(state="disabled")

    def clear_log(self):
        self.text.configure(state="normal")
        self.text.delete("1.0", "end")
        self.text.configure(state="disabled")

    def copy_log(self):
        self.root.clipboard_clear()
        self.root.clipboard_append(self.text.get("1.0", "end"))
        self.status.configure(text="Лог скопирован в буфер обмена")

    def on_play(self):
        if self.running:
            return
        tgt = self.current_target()
        if not tgt:
            messagebox.showerror(APP_NAME, "Выберите версию или сборку")
            return
        acct = self.active_account()
        if not acct:
            messagebox.showerror(APP_NAME, "Добавьте аккаунт на вкладке «Аккаунты»")
            return
        if acct["type"] == "offline" and not NAME_RE.fullmatch(acct["name"]):
            messagebox.showerror(APP_NAME, "Ник: 3–16 символов, латиница, цифры и _")
            return
        s = self.gather()
        self.save_all()
        self.running = True
        self.play.set_enabled(False)
        self.play.set_text("ЗАПУСК...")
        self.show_page("log")
        threading.Thread(target=self.worker, args=(s, tgt, acct), daemon=True).start()

    def worker(self, s, tgt, acct):
        try:
            prepare_and_launch(
                s, tgt, acct,
                log=self.log,
                progress=lambda c, t, lbl="": self.q.put(("progress", (c, t, lbl))),
                manifest=self.manifest,
                on_start=lambda: self.q.put(("started", None)),
                on_account=lambda: self.ui(self.save_all))
        except Exception as e:
            self.q.put(("log", "ОШИБКА: " + str(e)))
            self.q.put(("log", traceback.format_exc()))
        finally:
            self.q.put(("done", None))

    def poll(self):
        try:
            while True:
                kind, data = self.q.get_nowait()
                if kind == "log":
                    self._append_log(data)
                elif kind == "call":
                    try:
                        data()
                    except Exception:
                        self._append_log("ОШИБКА интерфейса: " + traceback.format_exc())
                elif kind == "progress":
                    c, t, lbl = data
                    self.pbar.set(c, t)
                    self.status.configure(text=f"{lbl}: {c} / {t}")
                elif kind == "versions":
                    self.refresh_targets()
                    self.status.configure(text="Готово")
                elif kind == "started":
                    self.status.configure(text="Игра запущена")
                    self.pbar.set(1, 1)
                    self.play.set_text("ИГРА ЗАПУЩЕНА")
                    if self.v_close.get():
                        self.on_close()
                        return
                    if self.v_hide.get():
                        self.root.withdraw()
                elif kind == "done":
                    self.running = False
                    self.play.set_enabled(True)
                    self.play.set_text("ИГРАТЬ")
                    self.pbar.set(0, 1)
                    self.status.configure(text="Готово")
                    if self.root.state() == "withdrawn":
                        self.root.deiconify()
                    self.reload_instances()
        except queue.Empty:
            pass
        self.root.after(100, self.poll)

    def on_close(self):
        try:
            self.save_all()
        except Exception:
            pass
        self.root.destroy()


def main():
    BASE.mkdir(parents=True, exist_ok=True)
    set_app_id()
    ensure_icon_file()
    root = tk.Tk()
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()
