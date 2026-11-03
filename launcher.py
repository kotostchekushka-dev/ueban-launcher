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
except ImportError:  # для тестов без GUI
    tk = None

APP_NAME = "Ueban Launcher"
APP_VERSION = "3.0"
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
CF_API = "https://api.curseforge.com/v1"
# ключ: (название, тип проекта Modrinth, classId CurseForge, папка в игре)
CONTENT_TYPES = {
    "mods": ("Моды", "mod", 6, "mods"),
    "resourcepacks": ("Ресурспаки", "resourcepack", 12, "resourcepacks"),
    "shaders": ("Шейдеры", "shader", 6552, "shaderpacks"),
}
CF_LOADER = {"forge": 1, "fabric": 4, "quilt": 5, "neoforge": 6}
CF_SORT = {"relevance": 1, "downloads": 6, "updated": 3, "newest": 11}


class ManualDownload(Exception):
    def __init__(self, url, filename=""):
        super().__init__(url)
        self.url, self.filename = url, filename


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


def cf_headers(key):
    return {"x-api-key": key}


def cf_search(key, query, ctype, mc, loader, sort, offset=0, limit=20):
    p = {"gameId": 432, "classId": CONTENT_TYPES[ctype][2], "searchFilter": query,
         "sortField": CF_SORT.get(sort, 2), "sortOrder": "desc", "pageSize": limit, "index": offset}
    if mc:
        p["gameVersion"] = mc
    if loader and ctype == "mods" and loader in CF_LOADER:
        p["modLoaderType"] = CF_LOADER[loader]
    st, js = http_request(f"{CF_API}/mods/search?" + urllib.parse.urlencode(p), headers=cf_headers(key))
    if st in (401, 403):
        raise RuntimeError("CurseForge не принял API-ключ (проверьте его в настройках)")
    if st != 200:
        raise RuntimeError(f"CurseForge: HTTP {st}")
    items = [dict(source="curseforge", id=m["id"], title=m.get("name", ""),
                  author=((m.get("authors") or [{}])[0]).get("name", ""), desc=m.get("summary", ""),
                  downloads=m.get("downloadCount", 0), url=(m.get("links") or {}).get("websiteUrl", ""))
             for m in js.get("data", [])]
    total = int((js.get("pagination") or {}).get("totalCount", len(items)))
    return items, total


def cf_install(key, mod_id, ctype, mc, loader, dest, log, site_url="", _seen=None, _depth=0):
    seen = _seen if _seen is not None else set()
    if mod_id in seen:
        return []
    seen.add(mod_id)
    p = {"pageSize": 50}
    if mc:
        p["gameVersion"] = mc
    if loader and ctype == "mods" and loader in CF_LOADER:
        p["modLoaderType"] = CF_LOADER[loader]
    st, js = http_request(f"{CF_API}/mods/{mod_id}/files?" + urllib.parse.urlencode(p),
                          headers=cf_headers(key))
    if st in (401, 403):
        raise ManualDownload(site_url)
    if st != 200:
        raise RuntimeError(f"CurseForge: HTTP {st}")
    files = js.get("data", [])
    if not files:
        raise RuntimeError("Нет подходящего файла для вашей версии Minecraft / загрузчика")
    files.sort(key=lambda x: (x.get("releaseType", 1) == 1, x.get("fileDate", "")), reverse=True)
    f = files[0]
    url = f.get("downloadUrl")
    if not url:
        st, js2 = http_request(f"{CF_API}/mods/{mod_id}/files/{f['id']}/download-url",
                               headers=cf_headers(key))
        url = js2.get("data") if st == 200 else None
    if not url:
        raise ManualDownload(site_url, f.get("fileName", ""))
    sha1 = next((h["value"] for h in f.get("hashes", []) if h.get("algo") == 1), None)
    Path(dest).mkdir(parents=True, exist_ok=True)
    fetch(url, Path(dest) / f["fileName"], sha1, f.get("fileLength"))
    done = [f["fileName"]]
    log(f"Скачано: {f['fileName']}")
    if _depth < 3 and ctype == "mods":
        for dep in f.get("dependencies", []):
            if dep.get("relationType") == 3:
                try:
                    done += cf_install(key, dep["modId"], ctype, mc, loader, dest, log, "", seen, _depth + 1)
                except ManualDownload:
                    log(f"Зависимость {dep['modId']} нельзя скачать автоматически — скачайте вручную с CurseForge")
                except Exception as e:
                    log(f"Зависимость {dep['modId']}: {e}")
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
    "bg0": "#0a0c13",     # самый тёмный (панели, фон)
    "bg1": "#10131c",     # основной фон страниц
    "bg2": "#1a1f2e",     # выделение
    "hover": "#151927",
    "input": "#0b0d14",
    "line": "#272d42",
    "text": "#eef0f6",
    "muted": "#8b93a9",
    "yellow": "#ffb020",      # акцентный цвет
    "yellow_hi": "#ffd36b",
    "yellow_lo": "#e8890c",
    "orange": "#ff8a1f",
    "brown": "#1a1205",       # текст на акцентной кнопке
    "red": "#ff7b7b",
    "green": "#6ee7a0",
}
FONT = "Segoe UI" if IS_WIN else "Helvetica"


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

    class AnimatedBG(tk.Canvas):
        """Фон: тёмный градиент и мягкие светящиеся шары, которые медленно плывут."""
        ORBS = [("#ff8a1f", 0.30, 0.00, 0.9, 0.7, 0.8), ("#ffb020", 0.22, 1.9, 0.7, 1.1, 0.6),
                ("#7a5cff", 0.26, 3.7, 1.1, 0.8, 0.5)]

        def __init__(self, master):
            super().__init__(master, highlightthickness=0, bd=0, bg=C["bg0"])
            self.enabled = True
            self.step = 0.0035
            self._t = 0.0
            self._rings = []
            self._dims = (0, 0)
            self.bind("<Configure>", self._on_resize)
            self.after(50, self._tick)

        def _on_resize(self, e):
            if (e.width, e.height) == self._dims or e.width < 10 or e.height < 10:
                return
            self._dims = (e.width, e.height)
            self.delete("orb")
            self._rings = []
            base = C["bg0"]
            for col, size, *_ in self.ORBS:
                ids = []
                for k in range(9):
                    t = k / 8
                    ids.append(self.create_oval(0, 0, 1, 1, outline="", tags="orb",
                                                fill=_mix(base, col, 0.06 + 0.30 * t * t)))
                self._rings.append(ids)
            self.tag_lower("orb")
            self._paint()

        def _paint(self):
            w, h = self._dims
            for (col, size, ph, fx, fy, _a), ids in zip(self.ORBS, self._rings):
                cx = w * (0.5 + 0.42 * math.sin(self._t * fx + ph))
                cy = h * (0.45 + 0.38 * math.cos(self._t * fy + ph * 1.3))
                R = max(w, h) * size
                n = len(ids)
                for k, item in enumerate(ids):
                    r = R * (1 - k / n)
                    self.coords(item, cx - r, cy - r, cx + r, cy + r)

        def _tick(self):
            try:
                if self.enabled and self._rings and self.winfo_viewable():
                    self._t += self.step * 9
                    self._paint()
            finally:
                self.after(40, self._tick)

    class AnimButton(tk.Canvas):
        """Скруглённая кнопка: плавная смена цвета при наведении, «нажатие» и мягкая пульсация."""
        PRIMARY = ("ИГРАТЬ", "ВОЙТИ", "СОЗДАТЬ", "ИСКАТЬ", "СКАЧАТЬ", "ДОБАВИТЬ", "ИСПОЛЬЗОВАТЬ",
                   "СОХРАНИТЬ И ВОЙТИ", "ВЫБРАТЬ ДЛЯ ИГРЫ", "ВКЛЮЧИТЬ")

        def __init__(self, master, text, command, width=170, height=40, fs=11, bg=None, kind=None, pulse=False):
            super().__init__(master, width=width, height=height, bg=bg or C["bg1"],
                             highlightthickness=0, bd=0, cursor="hand2")
            self._label = text
            self._cmd = command
            self._bw, self._bh, self._fs = width, height, fs
            self._primary = (kind == "primary") if kind else (text.upper() in self.PRIMARY)
            self._pulse = pulse
            self._hover = self._down = False
            self._enabled = True
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
                self.after(60, self._pulse_tick)

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
                if self._enabled and self.winfo_viewable():
                    self._ph += 0.08
                    self._draw()
            finally:
                self.after(50, self._pulse_tick)

        def _draw(self):
            w, h = self._bw, self._bh
            bgc = self.cget("bg")
            self.delete("all")
            if self._primary:
                base, top, fg = C["yellow"], C["yellow_hi"], C["brown"]
                if not self._enabled:
                    base, fg = _mix(C["bg2"], C["muted"], 0.25), C["muted"]
            else:
                base, top, fg = C["bg2"], C["line"], C["text"]
                if not self._enabled:
                    fg = C["muted"]
            col = _mix(base, top, 0.55 * self._h) if self._enabled else base
            inset = 1.5 if (self._down and self._enabled) else 0
            if self._primary and self._enabled and self._pulse and not self._hover:
                g = (math.sin(self._ph) + 1) / 2
                _rrect(self, 0, 0, w, h, 14, fill=_mix(bgc, C["yellow"], 0.10 + 0.22 * g), outline="")
            elif self._primary and self._enabled and self._h > 0:
                _rrect(self, 0, 0, w, h, 14, fill=_mix(bgc, C["yellow"], 0.28 * self._h), outline="")
            _rrect(self, 3 + inset, 3 + inset, w - 3 - inset, h - 3 - inset, 11, fill=col, outline="")
            if not self._primary:
                _rrect(self, 3 + inset, 3 + inset, w - 3 - inset, h - 3 - inset, 11, fill="",
                       outline=_mix(C["line"], C["yellow"], self._h) if self._enabled else C["line"])
            f = font(self._fs, True)
            self.create_text(w / 2, h / 2 + inset - 1, text=_nice(self._label), fill=fg, font=f)

    class ToggleSwitch(tk.Frame):
        """Переключатель: бегунок плавно скользит, цвет перетекает."""

        def __init__(self, master, text, variable, command=None, bg=None):
            bg = bg or C["bg1"]
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
            super().__init__(master, width=width, height=28, bg=bg or C["bg1"],
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
                self._sh = (self._sh + 0.025) % 1.4
                if self.winfo_viewable():
                    self._draw()
            finally:
                self.after(33, self._tick)

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

    # старые имена оставлены для совместимости
    PixelButton, PixelCheck, PixelSlider, PixelProgress = AnimButton, ToggleSwitch, SmoothSlider, SmoothProgress


    class ScrollFrame(tk.Frame):
        """Прокручиваемая область: виджеты кладём в .body"""

        def __init__(self, master, bg):
            super().__init__(master, bg=bg)
            self.cv = tk.Canvas(self, bg=bg, highlightthickness=0, bd=0)
            self.sb = ttk.Scrollbar(self, style="U.Vertical.TScrollbar", command=self.cv.yview)
            self.cv.configure(yscrollcommand=self.sb.set)
            self.sb.pack(side="right", fill="y")
            self.cv.pack(side="left", fill="both", expand=True)
            self.body = tk.Frame(self.cv, bg=bg)
            self._win = self.cv.create_window(0, 0, window=self.body, anchor="nw")
            self.body.bind("<Configure>", lambda e: self.cv.configure(scrollregion=self.cv.bbox("all")))
            self.cv.bind("<Configure>", lambda e: self.cv.itemconfigure(self._win, width=e.width))

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
LOGO_B64 = "iVBORw0KGgoAAAANSUhEUgAAARAAAABUCAYAAABKtGjbAAAt2UlEQVR42u2da5BlWVXnf2ufc+/NR3X2G7rpgm5ABuUpiiIKyMAgoygizhhE6ITMOKESRsgYgTFjIMaMOjHjB8PnyBgzRiA4oaPSIPgERHkIyMOxgW6oovpd1V2vrMyszLyvc85e8+Hsk7nvued5782sLKwTcfPevI+zH2ut//6vtdfeW9i/pOC1/1z2uZTco+jSBq+Lrqq61ZVdd++ml3DtanrpjDIvk6lcQVlow8+koQ7pgmxDGrz2/9eS8qqeta5uUmOgUgIWUvCo6khtUPEmxisNOmwehWijpNcAZTZD0IaGIS2M47BAo81gJHP0VxPbkIZ9VlWmFrzWkvcL6xVWsA3/f1MCGlLy/SYdVVTBJgAiMwCJtlQEafn/tatc8atGtqr+lwMYPBYBhIvQn7Z9pDXg0dQjKLLB7GG9z633Xhlz0gxAigSXFWxyAGLcIyj4TBoYcRHa2RrElRoQq0PeIkHYCqHMM/LJIRmo/7/kBCxXEDjKRrh8v2sNA8nLm5qBQxfQbs31Y9H/1NRfGuhRXV9l/WRrysnbJy0H8rz9WSBxz36f2gIbEt+FKTNKk3uIA44AiFxh+c9NCRJWAUjH/S4paXB2z1HOYJooV5HwBeg58LS530cV9ThKI7y4+neAMRBfYXakNeCRPYzre/H6Ph9HCFx7xgU6IAcYF9EGjCrTn67re+u9l+lwlNOrOqAt6q+sn/Dulb8CV96oQciBEpBSD6yyduH63xY8plwc3+iNV2DgfRZ4z+Ju/gzgLvdeE3TzK5q4h7rGnwG2gOWCzjJOIAHwLOCWGVynfN1GwEngoisz67AIuB24KQeioQec0jAW1ISRzavsZ4DHgKe7fpklWCwLMrQmFBlgE7jPyaCXG92y/ho6GfwzYLUlRW/TNm35efb/GDgFnPX0B6APXAc82T3nWbtvQ3UAchE44XSyW2IXA+B64OuAYw3cmTJQz+69AXwB2HVlRjmbLQSRItcg3+DAGVH2/1uAH3ZCrnJV6gI06gz3fuA3gQ96SuWDx5OAnwVeASwtyPhOAb8GfNi17QbgR10Z1xfEf0wOYDlggGhyXQQedgq0cpXESMbAJ4FfBB4skPcQeDHwn9yAYY5oOx4C/gfw556xvQ74Vw7QlymfZGhyDYBPAP8VeMRjO9ngPgS+CfgZ4HkL6qfIyeZtwL3OLmJvwC8CESu5kTUf48hG366r9E8BbwUQEQ0CMxd1TBKLqmaK9ePAR5wx+O7N251SYYxRY2a3WVUlSWxW5xHwE8AXgV8FvgUgCAJkcbCgB6G9rt/2ajlvvxzGZa1irc0q+SUH2DseMEeO1f6uA3RdsCwWIs3EJqgizqB+2g18bwb+vZMFNbLQBjqa3eCzwI/lfjMGngq8A7jZ6azO00852ZwEvh+44MklzjGRzIPQsCJ4lbGPjvv8NuAHM+RR1SCOk0UYTeIA6o2uwzIfeRd4kQMPC4i1VqxdiCrErpx/6ejmt7h6SJIkbURxJad6M5BdZL8cRqzEOtb0MjeCr7n3LgHf7cAjBsIkObLhqNgNrq8GzgP/LmubtdbkZDFLrCbrpxc63fyYx4y3XD/d7PWTLEg2iXMdXwW8yxvMszb7MRoL+9O4ZeCRuS8KPAG4RUREVc1znvNs/vUPvIbRYBOZRNzaxogIcZzwjne8l7NnL5j0lvpkV8bAgVbs/EkVMahaefnLX8YrXvEiBoNNjDGtuiYIAzY2LvM7v/PH9PvDwLXpmY5ualotMW984w9y1103MxoO8u268tZnld5Sj7vv/gD33HNCgiCQJEl4+ctfxj9/xYsYDbaObJ0/f88J3n33B8Tpjzrq/VnHBI8BTwSek8lieXmZH/vRH2bt+pA4HiNXmIqICHGU8L/+9x+xvr4ZuGbcAbwGyHQ4eP3rX8vzn/81jIY7rWShqvS6XR548DS/+7vv9d2el7t4164z2jXgeNZPxgT8yI/8EHfccQPj8aBVP2Wy+dzn7uV97/uwL5s7nQ12POCwRWGJkOIpIeOBR8ZAummFDUmS8IIXPJufe9urGfT/giAIW3QUGCNEkfLhDy9x9iwigqgSeL586KhTLy1TNEngNa/5dt7ylheyu/NROp1uY7KjCt2u4aGHAn7/9wP6fUREUNXnAs91ryUIurz5J7+P53/94wz6DxEEnYPyQmZ0XZTllWUeuH+Je+6BIBCSBF7zXS/lLT/9Qgb9jxME3SNY5yX+7x+s8e67QQSTeq28HvhWByJZUH7ZKXGwtnYDb33r67nl1s8xHGxgTHDF2qWa9vVgkHD3e0LW11M7VeVZwNf6A+cbf/i7+J7XLjPof76VLKxVlpa7fPxju7zznYKqGlfG9wHfAXwZeKd77rl+Igg6/Ic3fy/PevajDAePYkxznc1k86533sD73geuPH+GL/QIhB903SMdoQceftAwKACQCZQYjfqMRx9nff3drToqBRCIY4O1m/nI8pK7UejoVMf3igaDDaLor7l06X2EYa8lgAgbGwmqowlPKwiMZsIIw5DL2w+xffmDbG2dOJLGeKMNGY7OZmqXhv8HG4zHH2Z9/f0EQe+I1RluiA27u7sEgWCMOOujk1j7ZMcyERGMMU6JlTAM2Nj8EiLvZTi8iDHhFQYQGA4FkR2CwGAMiIixVk06aKd16/fPMxx8ifX1T7aShbWwuirs7saEocFazQw6TBJ7vXPn73IBZs9ehY3N+7m89Zdsbz/YEkAy2fTz0Ycs7plnIBm5mMhEpQA88iCSzRGLT+m6vRFra5cJgqWWAKLEcUAQJH7FjcdAsqhz1/9tEECnM2Bt7TJhuNwSQODYMZ0KynkBK5JkF2OUY8f6qG4fQWNUVo8FdDoR+X7pdgdOFstHDkCOrQlBOCJJlCTRojgOqjoRf9raGtLrKWtru3S7260M46AApNMVdnYiksRSFp7pdGFpeZe1tW2CIGoJILC0rERRUhafeCLwL5jIVRK6Xct11+0ichljeq0A5Nh1wtLyOP9RBiChN5hneScTM0t1MZCMgZg8GxBRYJul5UsEJpye3S7JjVQFI0qcBBixeQaynGMgXf8OIhbYZGl5gzDcSW8mufIKlg5ZhU4HekvG3cMJu9PhDW/4Xm6+OSCOhgThEsePDxFzmpWVS4gJGrWpMtE4X6eG/VS0TCuJ03+CwDoZZB8nwBZLSxsEwc7+j6UirC01dW9SNylpk/eZdf7Kc58b8qY3vYAwvIswDHjkkfO8+90fzfxubrjhBt7whu+m1xsSRWNuuukWrr/+Ip3O44jZRLJBt6aPKvs8rycNP0t1NgWRN73pds6ceR6dTg9VeM97Ps7p0xdwbjBGYmAjtYugk/6YermrBRMod97Z4Sff/Bxs8nTCMGR9fZs//MO/kdEoyuIsTyeX7BgEY8Sss7R0aZ+pSUE5OfkmzhnpdIL8p8aLgWSzMH46g827MFIRAwm911MTKBopGkSt0FwNaJygGhS5MJb9LNHO9PyOK1PH7eLLBogDJ9BU2EtLq/z3//Y6nnTHh11w22KjPyIenAaxaGLblbHoOF/unpq4qJCaCQNSFyTXuKJfqup3EHX3FCtO4BueZ/mt38oSNRP+4TPCe98bYN2UxfHjx3n7218H/IHT13Mko08QjzYQLHqFE4StM4K3vjXLOUxN5OSJkNOnU1ZtrSeLSFE7blVGnMBTnhTza78a7XkOjz0Kf/7nAaNRZNzdX8Bk7kw6QmqExhY1zeWv2RBtySONyTGQsABE9gAkn7mWB5DsuUsB+Yz7oEZAtFgRc4ieoXmcSH70Ey8GkkXMunnwgbRMQqlnIO49BYII4kFWZPZXuLx5iiesvZdB/xLG4Hzb8vrny1CvXofhDnQ6go1yA3Ami13Q0Gt02SqRgs/aTgOLCJIf6Sr6K1KLtZ/H2s+zsgq7m2BMMFHu9qUvEujd2MQiAiZI1aoRg3B21NbLSXM2tDE7Ge6cBT2LKoQh2DicFIbLkpiQRUNdyuzD2hNYe4KlJdi6kPZTEKTxIWvp2ZywbAKMhbgPRprbYpJArwvJaLpbnO2Z3IzsBHj4MZAyF8bkIrIyUZkxRH2wprmy7gGIdQ0vZyA6xXo0Bf+o7+aPGgpdAemkAJJXMLEhOlrCjgREsKKNhZ4pUSc8HAAxCkYVMzWUeLIIy0GirE0CdHvtSMh4rCSJA9vGboKkg6UBjUJEksnxw4bY8RI2GSA4WWSDRANZdDsQBs0xxFoYDXSyDY3cG0kxOzGozU+NAsOcLFoAiEsoILGpIcTDkJ3tiKqUGE3A9iHaTQfApnJPEtAOJONCAAlzz4ULWv0YSJErk3djJr2JsWMgQTFjoqgNCmLSyjOdcNPzAjY2DyCqqXBSBlJf1t7grGC60wAiDr3jgRL3Ne18tLzuvnuvqbI++ig88lhIEJiJgaxtCKQBSSCxsHadcO6c6zwtVtqqJclTWU0C4wg+95WAOA4QM+kaTdVPILHC19wZcNONI+I4mQpMa6ksUpofAcl42sxtDPTTQGt6T23kfeGCnCdOCefXQ4JAptowUTdnPDeuGZ7xNEuSjPeBiCb9p6kuhopamfhcLVinoxrSSJeK+kkVxjF0sbz6lXdwuX+csBMyHsd86lP/yHA42sfdBKJBKv+ghS0mCcQh2FElgOQZyAReFGWi5td/+DeZFLgDEBsUx9+KKq/qBGjJo7c4ptMtAxAUdFQMIFXxPlUwEcTDaXdD4xRY4r5D74aDhbXQXYGPfMzwm++E1ZX2bsAsswFhqJx6wE66HZoqgd8vVVtl7a3TVuiGsLEu/MIvw+bllFFVuWRGYDBM+MW3vZpXvuhetjbvTw02B7xlr62FWFMjy8f3NIJkkAaLRcp1KR+/tRY6y3D3+4S7/0o5tiqVsjBG2N0d87Jvexa/9LPH2dn+K0SK446lsU/HjzWZZiDJ0GPJNYNClZ4NBnDTSsI7fuV6gqWvIQw7bG3FfPOr7uOR06MJBpIM9wfzsnvmZWJtCiAVDKRuL6CpWRgpARJ/Nep+FNcBSIs8sgkXJgcgxov6+lNHExLIOioXXq0tM3HKOQWCiQOQwSSANKG/MUBiCEMlDBQrhwEgTI/4mvqx8aA41F11vziAZGgIDK4d1QAiAmFo6Sx9PRpdIO7fP2Uotf3m6ptXbBunst1zjZreUyFGEJVGsjBGCAIl6D4BMc8m6f9VyrxayoKQQhcmGTpdCxcj86h/H2rvIwxgaycNomeZ2MaYNObidLjIGyiNqVkn/3Fh7Dvw7NIUsI8pBtKEiUyO7DZFP5V2HaJmGrndlZ/1MVNCy8o0M5RZtI2Qeu1oEYBTmz3SwM4M8buZJmXK6jizLCQzAqnd6We/HgbUoFbbl2n3H4X1ce2gbTti91utb4Pfj1kb2gpvr++0OCbRtl/qBZ9ag9qQ3d14j2GNhrskiaKJQNKuGXt9bQsxwOQApJSBFDHdMhA5yCuffxJzdJdzX7uuiebwLs+dCwLLNzz3Ji5cvg1jAsLOMqsrIdbGi5yKFy+MULktQUj1/opNtytcNICUxl2uXUdMq69dh9Pb6RQuKz3Lu37lFnprL0bMEiBEg48xuHyhlQveUMCV8Q+fgUiJG1P2/0EOa8HVBh4i4h5N6a8eSQVt0ob0e7P78/7j4NpQ3Y7971yFICiQDE7S75+cDPSK7+8sDEC0wvaFklkYKN+yr9g/azP7oKW/abYloL85YpsybUXftr2f9xtrIYpioqjZLIwAQdhZvGLN0y8W4tgSRfFeHKHKSKPIgWCLMkUgNG5ZgYHQaHUbpL1OJUm6jqROFtlq8DiOZtPhOp3SGXVqHg44C26U17HO7vcmkcKKD+tFOCbdvSNcWOWlsvIuYYoh7bY+zpYijQpKyzZrG7Z0610axkpgeOJtt7KysoparaxCug9KxNbmhcWyEOvaNmwpCzeTIGPl5huP0Vu9gTCQWgAZjqHX7WEHSaMyRWA0hp2BQQlY7sHmpYDJfZNd1GvonlsCiFo4ttThtttu5NjqErZCFmIM1/WH3HjjE9Cxa8MspwuFBTo8qyyuxJXl549LQagsLkrehakCuWpj3p2xo6ZRXxoByNCV2WlZVod029t8J2VLG3bbAYhBGA2Vlz4z4MUv/GY6x54xOXTns5hV6XRDHnlsg5/7lT9mt98nCILFAEm2PXVLWQgQC6xh+fkfeQqdm16KmF45BXHJXaoBq50T7J59mKDG+KzCyhJ8+gvCb7zH0O0GCDAYF2hs5GQUtTNogzDuK9//4hVe/53fTrj0pOmpnKmscqUTCoMzn0L6M/Z56A1kfir7cA67OGwAoWBgne6xUmnM18SRE/isAJJUujHFvxm7MmcBkIJUdhL3/m7bqEs6dbsifczlD8H239b6/90Qli8EacaUyOICAT6AzOAdBcCN4Zfh7AONAxxJHKHJuJ4/K0gC413D+Q2l1021Ni6izvFsAJLJ4pjZxFz6E5CgEXyqTYji4Wz0v4qBDOawC9qflzkXgMg0EWzjEIVzKe14TgCxle6TFDZ4OBuAaOh+W8RAMoG3DdsKJAqxjkph3AcQQog3jVsksUDVUA/MO7PpkSVGNW4XsKThQt4EJBI6Hd1bN7SfB+QNdJGTxXi27klQYh22YmAiMy5GznTK1uiothOjyDQR3tv8Vg8AQIoZyMEDiABdC8fidna3t7J+2mOQJmWGSVpm2DLRqAusFkyVh5rez8TpYrXZo1lSW4eehVWb5ho3SXgqukcRaTFAN4HrEgjk4NpQqGM1DbAKqwEsJW7VuZ/AlfttV6EXT6ayzxZVbBtAmQGz1WU92skSA1V6iaR2Ie0EnE3Vjq0hS40VlK5JCt2wea/EwmoCvTnuOReAYGHNQjDDthkJEGiLmEtGEFyZYYsyrYKxcMxOr23oaAqEoU1T7OejAdUKJwKrVukEQhimCtYWQIpS2cWxubUEAnNwbZhpkNMUnJdtvUL1nCzUMudxDgc/Ta4KYqd1OEAwVlNZtNjG1SqYDpw8L/znvzSEYcg4Tjj+hJv4+R84zsr259E4XugxF4mFICmVjRwogOwdCzVif0OSOp8+v7JHW8rblSmjAupYh86Jo8Z5B9O690fsbyJQds+yVWJN/G1VSNJFZGcet2wOlMC0TJ8nXa+zs1vwo4j9wz+paUebfmsqy7LPrKcrTWJio4YuTFX5815N+svVd8qtsE4W45yOS41M3HeHA8PJ80onsAxHEfGxW0lu+w7Y/DKMYm//hAb1lprys1mYaHbQnS+Imp1gamc531jqZmGKUXCcCUfaWV5YGCxK6z6S9LO5jtfQWposFuxQeeS0cnk0f5KA+mARSft+qWpDrTil3S0j9he7lHVX7Ml35my1NsECmR2BsnPZbElscORk0WZwVJBYWO6kixrFQq8bphmn2T2NLA41s22Sk7LaHAaAjJjxOGppOu882axI9kfaNsJJ3G811/k+AwlmHJbF1BpU5mZIEhAG0V5Gp2p7zu223s/1i88GZxGHvwCzgd0lsfe7esMgFjQIUdNx+4cm04qTeGAuM+qUNEyazvYBmNVfynSqCEAmdLShLuk+e7G6/0hdJQNRB4ndgnjbNkhUUgfrACmeHQLmBBDxGMgsAKJQvqWiKQSQ2AmHluhugbEUuzbZ6B00Pbd532f90P3C791nWOkapnOXJgVnDFweCf2x5gOi83m26smiRb+ow77NAfzCJ4TLcUBoQCuUXgRGsfITr3oO33j9WezWxcozWwxArLzwpoB3vfFlcNtzCLsdvnjqUX7ov/wBExtAW08WLQzEKpgQfu8L8KGHA1a6gq3c00QYjGO+/mm38+aXHEPPn2yf1p4xkEIXxpdFQ2auHvigWIUwNJw7d5af+vW/ILgYk8SG1eVl3vaau7ipfxIdj1rUW4rLDLIyDxtAJmIg0nYM3e/saX2rTp3PaC4tyyyKgWR1GDlQCttTznOXAz7zWMJ1PSqVNpNhFGv2vUyz+qQHKDehjtmO2NnpZAUxkJb9YiAaGe45Z9kYJbWZqEZgd5ywcfw7QT6Knr0InboyhetNzHN3PgsP3wcdIT6n06NzFgMZSVpQ03iT67VHNuHTj1mu7wlJVRuMYWcn5tjTnwp3PR8ePTkbC0lkX+g6pwuzN5jtN0pEGI1G/OOpx5DdmDiKWLvlVsZPey3c+9swHKaRWn/nd21vhgUM5BDyQPDYQFsGorI/AZ8PYk8/iv3LtgykDGmt53cn0tD/31faDsJqF1Y69QAiAqNpRR0Bj+UARAtCXuIBSDwBID4DaRsDkTRHY6UjjGy6XqUSwdx0b9g7BoNOY0NRBd3dwNpLmA6MLnamNdVtDsy45Yo95yp0UVY7qSyqASTdC7TX6YIs7wNW2zEwlHIXZjyDjoaZjoqnM8JKt4OMhRhY6YXQOYYdGxcTYfZFgerZcTWAlP0v8wHIvH63LXwny0GedpInXBjaA0gRA0k8nzWktVuk8aTPWut2a6UobQGASAHR1fLAXctRSFKjtdbzu2tdBkndnLE2LjNN2ko3CTVJGgua2lUq8dhg25QUC5qkB0DUymIvvqDp9mizxFyyGIhWMPNZ4nTjadCxqkimY2LoBh2MDdxeogHEETMtmFeazZAdqAszc+R/ato3AG4nPZ09m1y6qTBYmPnIrQGkhIGM3Ge2vQszT+e7q+PaLJSv8fTdOiU9jHr+2NAegMwwaqljCrOMtElJmYm734wMpJJ2VLJomS0JyMo0Utk5GIh19SkMVyjGCKN+n1/+o49w7OGIeNgjCEP+7QuezO3jx9HRqB0bKXdhDiuIWjKqN6n5NAB0gJeWfF2m6GFbfy/zL7VACTJDsCxM4C2iWseAb5w7iLoHrMwGIG1/o7LPQJmh36KyAWmGWZiJ5fMt0wkyMDMz9HlQwKJVPDebxQDrnskI0XjMBz59L2ztYJOIcKnD697wPdz+lfegmw8jYYv8+XIX5pAAxDkcrfYn9fTPAIERjEjl1H1gzN66BZL9PSdbB4ySKvrbfMYt8+lxZ/gEIu5An/oYiDFpe4I5FtJZ1elVvLZ9v2RrL7DpzIQJ0ue6GIixexXZk0fbfsMWH/lB4u7ZcqnC3ixO2wHYMtOeqGVJkIrOJgv/iI5KHRJWl7vIwGBjCHsGs3YrSBcSTW2xTZnFe6IeUhA1m4rUGbYUVmV7nJBYJan5beJ2hxlEdj8632JbK92rq5Y3ou1WWa7Jo8RyeRhjo/pQkJAmEw4iFn+ptu4X/6c745jtkaZhoKodzYF+BFG2EXPbMrVKFvufSZsAwsRXZQ5FbjkQqpbXZRZZaDPbtdaLiaQnXIHaNGdkBlnMsyp8/h0LWmJHNsgbjfmZl9zJ6TteRGflWOW+GCLCKE54+bOeBGfuw8gMwcKy7yuTK9u0eTtIlG++ZYm3PefldJ94l1tUXq3AicL5HUtitbWqW1XCpR7/5/1/zafvPYkx6QlmNSHW2usYCW/+tmcxfNo3YcJupRsgKJEavnZN4OR6e1l4dZzKRFdvleFMOjiDIVjdz8hsW6apA/PZdNR4IZlmTEzK5++auH5zXHMyEHWKoC49sikjEATLG55wGW59EMJefatF4NQ/YM/cjwTG7VmnjVjmfjBLS0A/S+xSL81Ta0QmWFWeuTTimSunQQfN+21tZt8FVjrc29vm0xScebF31oY2BkGrsGzge6+7BHKywcEi7mjBz3wUe+7B1rLQvbMQguJwzV66ext/0lnazGn8Wu87FBGFUmNtL4t9ImTYHYG4Yz2NwvJyEzucocy9Npg8fTt4F0ZVsdamJ8wlKXg0D2QrihCvr6Pn1xtX1wRgAkGlmXJl5MK6KGxSMLdnrbpHak3SCr4hGY2xX7mv3b4PMzLGBOh0YbRecCP1sltb3t8CybnH4bHHG//GGJCWssimTrM+L3MldIYzWmYhH/vHVLbfEUSrGK1tLwt1jCZJ4L4zCZGbUbpuLeF5T212g5nK1LlW1ejMANIJQ8xSl15AuqmGtelh2S2mkYKMq5WdvVgezGh2/70/qXBWg9xtRVhd6mI6QjcgXZsdWSzNZ1bSTZJNs8SzOZdii0LYFbdVRFLhjrVXiVJZlNW9jSw03WZg79zWQFkJyhRac0P8wcVAlHx5Ld0NW9ox7WWxN4E/mcfSsHtni4GVx0Dq8n5nZyBZWQ+dPsOffUYZne+QmIAnrnZ4yc3AYHsG+ljErecLyxiBS2P4yEaAlQ4dA+fHwsg7STiJxrz/01/kjocs460uJgh5yW09btFtbBy3U0WtIIDiDfWzDJPagKrPEUQtlEXRcnNKPquThYFH+/D3WyHGhHQMnBqk20oVuzEOLVvSf0H24ge1M0nirYzXvRu0JPCKlq0xYUZZaC4yJEzM8BUm/c4auJ0xZjYngKSl3XfiK/zCxTPoZaE/HPGt3/hMXvLcZ8An3u2WEJaMXnVHrS/gs2yR2NlBwC+dEGIsgYFxovS9zTij0ZD/+f6P0Nu5jI0s0jX89itfy60Pfwh9/AwSlljKottUNpBJSeBOC3Qu4WDO8puzbappvvOXtgw//yVlOUxnC2LsdOA8y9Vpuit7bm+ZKIZBpHSoS2VX+hGMY/ZzUkzLvihL+VOvHdKCfWb3yx1PGUcx/cEYidNd3IxRukW/jSlPCCvTM3/6OxfOPvAYSBiGrHZDbJAGfHprN2FveCrscBgbQtX69FZAxsJKCIkqgRFCzctRWO52WA7TM0VMJ0Bueyb2gU9hL5Pug3iEzoCyCraT7smc/8D2FbsN0juCdQ7AjCXd2tAtlyzaDsWOFbuTbrrUJqEycatxH76o3HfW0jHVABIYQxTD8YsJdkjab2YGXDXTRquxa0NLWWT9ZPuTRK/f7/MPJ09jJI0bHbsu5nnX5X4bKXY3LbPNPqxW0+1597aRnUFv5guiquJij4gYwiRMAaTtwUAHwU4Elr11KqLFHoTN+ZzLEmL6QncbaGuMC4hzVArLLbjq5JS2G3QwUUhvm+Js20NifuU+DCwN3TqVPU9p0lcSI6zQhR0wg3ZMKusXM04ZT2zxVzwXhZLcBwajHcyOV17T9rpM1CAXiuqFHczIVMuirP8EVgr25k6s3Yt4xUmWJ7P/+1XT3dfZFoNe6NrQGzaLdywUQCYCZIGwsbnJR758GraWGqxrP9grWyj5qDWg42ZGLaBq+fsTD3LhbEy03cEMwoM5g3HGKwGWA+HMKOXeWTefeuQMn+zuMNgOCYadI1VnC3QF7onBFJ4fkNZ1d3eXv773QTobHezIIqb5OhILLHn9ItPRp8JrfWODv/vSGaKtXusVrQqEgeFSFE3EBu859Qg3nu63lkXWTycTgzCoGLjtHoCI2xTpk19+gNPnlOhyBxM219mEdAr/C7tCbm3BIWWiuhG80wl48OFH+I8PX4BLCUfl4GWRhE5XETH127WKQJLw9j/7IHrhnFvQZTlKlwJdUU4MrKPu6fMHP/UZ7iNmvCWIOVp19uMOnU7BAO8Ec/HCBd72Jx/Enhu3nhVRoCPKKdcv3vi15UVUsgjS9app4sOJBx7kpzfXsZd0JlmEYnnQ+WKW9MS73//QR/jT7XXiviAt9UeARJJ8LDdm/2QjojhhHMXuQ8GOx/z6n34AvbDu1mXZlv0G63Gy14ZDc2GKDLAXGI6UAz5DbbphyD4/5Mi1pVPA7kNj6CIIbfJYDr/2WiEXEaEXGuwM0wLZVhpmelA/SepUZ2fI9UgXLnYhnd3YL5OZyswPlZ3Q0BUIZpDFFA9Irx339q2ARuOx3PvAacTtIyGh8nVPNqwaWrcja0OnudnogQGIT+Ou5kuPcCMqs/GPJOS11x9dYL8wveRND7rMWc77KbHQjKwJcNEBiIBqf+TtS2GVxDvnfIYzyefSG9OijK8CeLh2/RO6/Mlt/6GLL+ZgPXFgnf1tL/eOABPmPcuoVaO0qQujh9Uz165r1wFfMfv7hpVt6HA1ACHAQ86duR24Tg/v6G5tykC0hPJdA5Jr19XKQGL2U9Oy56ug2pp/I3Ieylngc8C5ozLIhzWudQM389p17TqS5N/Pz8z2KwuvQj32mVN2DFRyVEQT1kBgfrPf0i/KVayke6efX7PpA5fF3rIgPZSgewYg/m72VxuyZvUW77U9pFrUhjPCCpbh7zQ5tfWIiBAaSU9ocAhyFDMQArdoSije+tGIEIqk+ykf0TZ8NVziZKHinpM0h+hgDredcmECN2qHRx47prvCX1mTzfQmVxA8Gq3G9WdefACx2TTn5e0d7o2G6DjBWmXNJNxx7OjwQyHdsf/+bSVxqz4Tt4v/nmSSmFNnzhJGQ9QmmDDmzjU9aktgrvrLANsxnB4qIqksxjZbAnFgTCSLHUTeQBjnDONIks6ifd6vEICUTYNrPgZCDjBs7n8BNkmnk1YAOxyNgrOj0d4IEg8tx48dLcOzCo8PNJfCtN8BqioXt3Y8umI5ru1OjLh2NXBbBIYWzg8PNZSWAYgfA+kc9f6y0ywkO/sgO7VxfAiuWFH6RtERWnsMpAhxsk7PpsEuA38G/LijhHv5+KreXsdHTCChTLIOzYVsJOfuZBmTekSNUWf47CjUuejYI11QG3K/8/2i7LCLzIWZWqs6b5lTbdDZ9SfbYlKn4zgjz1anAGTWRK2K39gCMlEosrAAbXzg8KnfEvB3wDLwSuAWTVOD0xMZXEzEHhFSaDy+qtMBqU339g3qZYeniyWEUEzaDjl6xhgWbBomIoTI3omhR7XOOe3LduEIMsMzIjMlRpX0S3Zenw8gE95pGsczM+usQXO7Siqqlo5IGndrecRE4F7k+mngACQpA5DAGMIZywyLN6Er8kIKk0rDAubhP/xMvogUMP4W+ALwZODfqOpNgO7s7spXNEm56hGJgYw13QvEG5XOAPd5AgiBrwWeksZEEnno3Hl6oyEaJ0fOGrPVYNtuK/ZMaOcvXWJkSFexih7JOvenV2ifAp6Y6c9gMJBTZ88jg1kOaUmFuzWxRf0egETeYBildp7efzAY8JWLOrvOKmwlk/vSnj5/gY0kwo7by0JIq+J1Vcb8Bw788EAx/Za1PHrhAr3hCE3a6Wwmm91p2SSe51EGHhMuDCUMxH/4IDImTa3dID1+UkejkTw6Gh1pVxy45Orcc+9vk64zuBOwqMq5jc2rJ77gzpHY2tlh6yoLiwAXSE/luwnQKIrk9KXNBXULOMPbzJW7AwyBVUCHi9PZPSKyvnVZ1hc7Dp5n/6TdDEAu7Xk8qnJuY2tRcskeZz0CYQsYifouDCVujA8ekfcYeb7kJ4CnZy6AHE1t3Ru4gfvZT2/O2v2ga8MNB9QGPaAbSlks5ygjh8cEz7nXxxelP7rvvQL8vQOMLvsnv/SB/we8iv0DEhfVprwcdAH3PO+YWpwz4BPANwC3LUr2uq9PnyVdxRw4W7clLERxX8qiu8ZTyuz9zEf1/8++Fzol6AN3eKP6UbzOO9frPPsnkGYR+r5T6OvcY9G7isoBPRI3wgpXQX6DR40fdsY9dvXfBq53+rMIe74M/A3wSS/W4i+ke9TF8W53er0IWQyA0+5+nQXd8zHgPY6pWS/2YZ3OPuzY2xr7Zw/Mc41I0+Tf4eQSee5S5JXvEwsVr8Gh1wEdh9w991jOPVa81wZ4kkPDjnefsAScFjjYTAWTi3y22Alh6OpVtgd8CNzs2iQeWJKrf1k7CjZVnOk7+c+14HuZoWw4WaweDXJR+tqn3+tMnuuWkAbor6d4i40iil3kZmfTneddGYbpBEjj3f8pDkQ6uYEyaKm3mUux49qx6mTSZXoL47IJC78t1oHqQ+458OIRePXKtoe53Q18QclgXxSi8OOamVdxwbHxLGjbd6/7znaG7v3IB2UfQALP8PMgsuQeywXPyx5o9Lzfhl5jghrjmxc8inJYfOEYJg+LyGfgm9z7ebZlWoLHQbs56vXrAg4oXChwaAmwq6dnfixEPAMq698y8PANwWcZvv+en0k27E8eZANnNzeA+sbYRG+z7/o6R0nfFIFGnGuL//uCycQJwMt0IbO5sAAIyYUmYo9djDxwiD3QGLiHDx6jXF/HfiJZZmhZISYXPA0KEDn73ZIDjyhX2So0n9XwyhRVS8CjKDmuiAWYgvqaEvbRBASbHpc1D8hc6aVIbQGk6HvCdEynLG2kKsjvx+viAmP2AcQfKP17CtPL/01Jfcr6QWq+k9fNJBes9A+GyL9f1o6wwO7KAMRnIPnZliKWkeTAeOI5LEBHySFVng4VVSiLK3S9BoUeggcFo/0sil/lulQxkPwpHkXKWwQcZQxEFmy4B7Yg5Aq5LXVgktejKiApG8ETzxiS3HNM8RqufFwvzBmh73oHnuxZQGysKEUiz6aSnHHHFC9klVxbOrn2BDn9JVdu0cTIKMc0xgVAM9WnVdO4fsOkhFJm34tcI8a5xpSN4osAkDIXRmvYh1YEOcuAY1YAkQMy2KMGIFC9bkKpPueOChBpEgOxJcpeNnKXgUjZhMGi0gqLbKwolhMzPYWa1ys/PDDOuV1VAGJzTG3sgcjYe/YBJikAkb08EM0FEyXnj1ZRyazB3QIfLKxwAeY1rqKRzda4MdrAhSkDEVOg3HIAIHE1gkrVvqO2hJk0mbGiYtAqYyIx00swtODeeUMLKZ9xzAdfF8VAbAUQlo74ubYEOSApA8Aiu/XDE+PcrMs4Bx5xiT1NrYXxhZ40QFEfMfOuS5EhcgAA4tfZVjCRpgxESsCjTLnlnzB4NIl/lLkwVLCQMgDJz7QlBQZYxD6oYSFFhlcm93n7yDZkIrYkdpcf8IICICmyOS1wY/JBaB84xhVxkL2HH43WHAuxFR1RhGJ5KpgXgqmgpwcVSNUWAEJD96VNG76aXZi2gdQmbagDZ60xPlvjshYNDvkRXA4APGjApGwFCJYxKcm1wRRMdkiB7Ra5TRHT07txzoXJM6KJTFR/JiZpCB6JxzpMzm0JKgzwIFwYrQGSKgpdxUKazMDIHEYvDX8jR5x9lLkxjXa2qmAhlNzLlgCJVozcZfGuqsB53YDRVl51Mbsq9lHEpKSiDUV1tlQvV8k/qhiRlm1pKAUgUob6cY3/JQcMIEWBurLU2yYAIhUKtMjYh7QwzKttNqbuPJaqaU9pAMpN8n+0pKwq4zto9kGFXtoFtaNId+sC0fmZoCQX+8iDCJQspssLMqF+r5CgBskpiH/MY4BNFNZWgIdWKGwRiNAy9iE0ywto2+ajONV7UJtxS81IXhZP0DzFrjC8KsZZ5m7LAmTRhjEXtUMqGFVV9mxZtrYtiSPl3anC+vgxkDLjykfSM3CwTK+bEZonXi0SQOrcmSYBvDq2MWvwVA7IUK8WAJml/lLj3mnNaF4341ZmfLPGuhYBIItoR53N1YFI3WPKzqSBIVHS0U1yJaoo/6KyMrVGOE0UuQ4oDiJ9/avl0hlBhQZuzDwxsPx3ffZQBRYHPcumLcCw6HtSwajqAtHawg0sc6Mm6icNqGNRBQ3NVxXWKYXMoaxNQKROgaUhkCwaBA+LERwEK9IZv6MLrlfTdPm6QO1BrnNq64I3HfjqlgBIg37TmpiM1tVNGvqfVQhnaL6eQQ7BaKo6v0nEvG4EusY8ZgeXgwAQaM40mwDJYcpZF9wOWgJI/rVtAMzaRGB1kXFp2QC5AoqrDUbCJgxsnvr/U8gDOcg6S0tm08SdkppYi1zBPpp30KsiBdoASKpYfOF3/j95BCXPPVAeFQAAAABJRU5ErkJggg=="
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
]


class App:
    HEADER = 104
    MARGIN = 18
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
        root.geometry("1120x760")
        root.minsize(980, 690)
        root.configure(bg=C["bg0"])
        self._load_images()
        self._init_style()
        self._make_vars()

        self.bg = AnimatedBG(root)
        self.bg.pack(fill="both", expand=True)
        self._build_header()
        self._build_panel()
        self.bg.bind("<Configure>", self._layout, add="+")

        self.bg.enabled = self.v_anim.get()
        self.bg.step = self.SPEEDS.get(self.v_speed.get(), 0.0035)
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
        self.logo_img = None
        try:
            self.logo_img = tk.PhotoImage(data=LOGO_B64)
            self.icon_img = tk.PhotoImage(data=ICON_B64)
            self.root.iconphoto(True, self.icon_img)
        except Exception:
            pass

    def _init_style(self):
        st = ttk.Style()
        try:
            st.theme_use("clam")
        except Exception:
            pass
        st.configure("U.TCombobox", fieldbackground=C["input"], background=C["bg2"],
                     foreground=C["text"], arrowcolor=C["yellow"], bordercolor=C["line"],
                     lightcolor=C["line"], darkcolor=C["line"], padding=4)
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
        self.v_cf_key = tk.StringVar(value=c.get("cf_api_key", ""))
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
            show_cmd=self.v_showcmd.get(), ms_client_id=self.v_ms_cid.get().strip(),
            cf_api_key=self.v_cf_key.get().strip())

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
        if self.logo_img:
            self.bg.create_image(28, 12, image=self.logo_img, anchor="nw", tags="logo")
        else:
            self.bg.create_text(28, 40, text="UEBAN", anchor="w", fill=C["text"],
                                font=font(30, True), tags="logo")
        sub = "Minecraft Java Edition · v" + APP_VERSION
        self.bg.create_text(0, 0, text="", anchor="ne", fill=C["bg0"], font=font(11, True), tags="sub_sh")
        self.bg.create_text(0, 0, text=sub, anchor="ne", fill=C["muted"], font=font(11), tags="sub")

    def _layout(self, e):
        W, H = e.width, e.height
        m, top = self.MARGIN, self.HEADER
        self.bg.coords(self._panel_id, m, top)
        self.bg.itemconfigure(self._panel_id, width=max(300, W - 2 * m), height=max(300, H - top - m))
        self.bg.coords("sub", W - m, 34)
        self.bg.coords("sub_sh", W - m + 2, 36)

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
        sb = tk.Frame(parent, bg=C["bg0"], width=210)
        sb.grid(row=0, column=0, sticky="ns")
        sb.pack_propagate(False)
        tk.Label(sb, text="МЕНЮ", bg=C["bg0"], fg=C["muted"], font=font(9, True)).pack(
            anchor="w", padx=18, pady=(12, 6))
        self.nav = {}
        for key, text in self.NAV:
            row = tk.Frame(sb, bg=C["bg0"], cursor="hand2")
            row.pack(fill="x")
            bar = tk.Frame(row, bg=C["bg0"], width=5)
            bar.pack(side="left", fill="y")
            lab = tk.Label(row, text=text, bg=C["bg0"], fg=C["muted"], font=font(12, True),
                           anchor="w", padx=14, pady=9, cursor="hand2")
            lab.pack(side="left", fill="x", expand=True)
            for w in (row, bar, lab):
                w.bind("<Button-1>", lambda e, k=key: self.show_page(k))
                w.bind("<Enter>", lambda e, k=key: self._nav_hover(k, True))
                w.bind("<Leave>", lambda e, k=key: self._nav_hover(k, False))
            self.nav[key] = (row, bar, lab)
        PixelButton(sb, "ПАПКА ВЕРСИИ", self.open_version_folder, width=176, height=36, fs=10,
                    bg=C["bg0"]).pack(side="bottom", pady=(0, 12))
        PixelButton(sb, "ПАПКА MINECRAFT", self.open_minecraft_folder, width=176, height=36, fs=10,
                    bg=C["bg0"]).pack(side="bottom", pady=(0, 8))

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
        row, bar, lab = self.nav[key]
        lv, tgt = self._nav_lv.get(key, 0.0), self._nav_t.get(key, 0.0)
        d = tgt - lv
        if abs(d) < 0.03:
            lv, self._nav_job[key] = tgt, None
        else:
            lv += d * 0.3
            self._nav_job[key] = self.root.after(16, lambda: self._nav_step(key))
        self._nav_lv[key] = lv
        sel = key == self.page
        bgc = _mix(C["bg0"], C["bg2"] if sel else C["hover"], min(1.0, lv))
        row.configure(bg=bgc)
        lab.configure(bg=bgc, fg=_mix(C["muted"], C["text"], min(1.0, lv)))
        bar.configure(bg=_mix(bgc, C["yellow"], 1.0 if sel else 0.0), width=max(1, int(4 * lv)) if sel else 4)

    def show_page(self, key):
        self.page = key
        self.pages[key].tkraise()
        self._style_nav()
        if key == "mods":
            self.sync_mods_target()

    def _wheel(self, e):
        sf = self.scrolls.get(self.page)
        if sf:
            sf.wheel(e)

    # ───── помощники ─────
    def _section(self, parent, title):
        f = tk.Frame(parent, bg=C["bg1"])
        tk.Label(f, text=title, bg=C["bg1"], fg=C["yellow"], font=font(12, True)).pack(anchor="w")
        tk.Frame(f, bg=C["line"], height=1).pack(fill="x", pady=(4, 10))
        return f

    def _label(self, parent, text, muted=False, wrap=0, bg=None):
        return tk.Label(parent, text=text, bg=bg or C["bg1"], fg=C["muted"] if muted else C["text"],
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
        return ttk.Combobox(parent, textvariable=var, values=list(values), state="readonly",
                            width=width, style="U.TCombobox", font=font(10))

    def _listbox(self, parent, width=28, height=12):
        return tk.Listbox(parent, width=width, height=height, bg=C["input"], fg=C["text"],
                          selectbackground=C["yellow"], selectforeground=C["brown"],
                          highlightthickness=0, relief="flat", bd=0, font=font(11),
                          activestyle="none", exportselection=False)

    def _title(self, parent, text):
        return tk.Label(parent, text=text, bg=C["bg1"], fg=C["yellow"], font=font(20, True))

    # ───── страницы ─────
    def _build_pages(self, parent):
        content = tk.Frame(parent, bg=C["bg1"])
        content.grid(row=0, column=1, sticky="nsew")
        content.rowconfigure(0, weight=1)
        content.columnconfigure(0, weight=1)
        self.pages = {}
        for key, _t in self.NAV:
            p = tk.Frame(content, bg=C["bg1"])
            p.grid(row=0, column=0, sticky="nsew")
            self.pages[key] = p
        self._build_home(self.pages["home"])
        self._build_builds(self.pages["builds"])
        self._build_mods(self.pages["mods"])
        self._build_accounts(self.pages["accounts"])
        self._build_settings(self.pages["settings"])
        self._build_log(self.pages["log"])

    # --- главная
    def _build_home(self, page):
        pad = tk.Frame(page, bg=C["bg1"])
        pad.pack(fill="both", expand=True, padx=30, pady=22)
        self._title(pad, "ГОТОВ К ИГРЕ").pack(anchor="w")
        self._label(pad, "Выберите версию или сборку и аккаунт внизу, затем нажмите «Играть».",
                    muted=True).pack(anchor="w", pady=(2, 16))
        card = tk.Frame(pad, bg=C["bg0"], highlightbackground=C["line"], highlightthickness=1)
        card.pack(fill="x")
        grid = tk.Frame(card, bg=C["bg0"])
        grid.pack(fill="x", padx=18, pady=12)
        grid.columnconfigure(1, weight=1)
        self.sum = {}
        for r, (key, title) in enumerate((("launch", "Запуск"), ("acc", "Аккаунт"), ("folder", "Папка игры"),
                                          ("mem", "Память"), ("flags", "Профиль флагов"), ("java", "Java"))):
            tk.Label(grid, text=title.upper(), bg=C["bg0"], fg=C["muted"], font=font(10, True),
                     anchor="w").grid(row=r, column=0, sticky="w", pady=2)
            v = tk.Label(grid, text="", bg=C["bg0"], fg=C["text"], font=font(10, True), anchor="w",
                         justify="left", wraplength=560)
            v.grid(row=r, column=1, sticky="w", padx=(24, 0), pady=2)
            self.sum[key] = v
        sec = self._section(pad, "Советы")
        sec.pack(fill="x", pady=(20, 0))
        for t in TIPS:
            tk.Label(sec, text="•  " + t, bg=C["bg1"], fg=C["text"], font=font(10), anchor="w",
                     justify="left", wraplength=640).pack(anchor="w", pady=2)

    def update_summary(self):
        if not hasattr(self, "sum"):
            return
        g = self.gather()
        tgt = self.current_target()
        acct = self.active_account()
        self.sum["launch"].configure(text=self.v_launch.get() or "—")
        self.sum["acc"].configure(text=account_label(acct) if acct else "—")
        self.sum["folder"].configure(text=str(tgt["game_dir"]) if tgt else "—")
        self.sum["mem"].configure(text=f"{g['ram']} МБ  (старт {min(g['xms'], g['ram'])} МБ)")
        extra = f"  + {len(g['extra_flags'])} доп." if g["extra_flags"] else ""
        self.sum["flags"].configure(text=FLAG_PRESETS.get(g["preset"], FLAG_PRESETS["aikar"])[0] + extra)
        self.sum["java"].configure(text="своя" if g["java_path"] else "авто (официальная от Mojang)")

    # --- сборки
    def _build_builds(self, page):
        pad = tk.Frame(page, bg=C["bg1"])
        pad.pack(fill="both", expand=True, padx=26, pady=20)
        pad.columnconfigure(1, weight=1)
        pad.rowconfigure(1, weight=1)
        self._title(pad, "СБОРКИ").grid(row=0, column=0, columnspan=2, sticky="w")
        left = tk.Frame(pad, bg=C["bg1"])
        left.grid(row=1, column=0, sticky="ns", padx=(0, 20), pady=(12, 0))
        box = tk.Frame(left, bg=C["line"])
        box.pack(fill="both", expand=True)
        self.b_list = self._listbox(box, 30, 12)
        self.b_list.pack(fill="both", expand=True, padx=2, pady=2)
        self.b_list.bind("<<ListboxSelect>>", lambda e: self.show_build_info())
        row = tk.Frame(left, bg=C["bg1"])
        row.pack(fill="x", pady=(10, 0))
        PixelButton(row, "СОЗДАТЬ", self.create_build_dialog, width=104, height=34, fs=9).pack(side="left")
        PixelButton(row, "КОПИЯ", self.build_copy, width=84, height=34, fs=9).pack(side="left", padx=6)
        PixelButton(row, "УДАЛИТЬ", self.build_delete, width=94, height=34, fs=9).pack(side="left")
        right = tk.Frame(pad, bg=C["bg1"])
        right.grid(row=1, column=1, sticky="nsew", pady=(12, 0))
        self.b_info = tk.Label(right, text="", bg=C["bg1"], fg=C["text"], font=font(11), anchor="nw",
                               justify="left", wraplength=480)
        self.b_info.pack(anchor="w", fill="x")
        btns = tk.Frame(right, bg=C["bg1"])
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
        pad = tk.Frame(page, bg=C["bg1"])
        pad.pack(fill="both", expand=True, padx=22, pady=14)
        self.mods_target = tk.Label(pad, text="", bg=C["bg1"], fg=C["yellow"], font=font(10, True),
                                    anchor="w", justify="left", wraplength=760)
        self.mods_target.pack(fill="x")
        self.v_src = tk.StringVar(value=self.cfg.get("mods_source", "Modrinth"))
        self.v_ctype = tk.StringVar(value="Моды")
        self.v_mloader = tk.StringVar(value="Fabric")
        self.v_sort = tk.StringVar(value="По скачиваниям")
        self.v_mc_only = tk.BooleanVar(value=True)
        self.v_query = tk.StringVar()
        f1 = tk.Frame(pad, bg=C["bg1"])
        f1.pack(fill="x", pady=(8, 0))
        for text, var, vals, w in (("Источник", self.v_src, ["Modrinth", "CurseForge"], 12),
                                   ("Тип", self.v_ctype, [v[0] for v in CONTENT_TYPES.values()], 12),
                                   ("Загрузчик", self.v_mloader, LOADER_CHOICES, 10),
                                   ("Сортировка", self.v_sort, list(SORTS), 18)):
            col = tk.Frame(f1, bg=C["bg1"])
            col.pack(side="left", padx=(0, 10))
            self._label(col, text, muted=True).pack(anchor="w")
            self._combo(col, var, vals, w).pack()
        f2 = tk.Frame(pad, bg=C["bg1"])
        f2.pack(fill="x", pady=(10, 0))
        ent = self._entry(f2, self.v_query, 30)
        ent.pack(side="left", ipady=4)
        ent.bind("<Return>", lambda e: self.mods_search())
        PixelButton(f2, "ИСКАТЬ", self.mods_search, width=110, height=34, fs=10).pack(side="left", padx=8)
        PixelCheck(f2, "Только для моей версии", self.v_mc_only).pack(side="left", padx=8)
        f3 = tk.Frame(pad, bg=C["bg1"])
        f3.pack(fill="x", pady=(10, 6))
        PixelButton(f3, "ПОИСК", lambda: self.set_mods_tab("search"), width=110, height=32, fs=10).pack(side="left")
        PixelButton(f3, "УСТАНОВЛЕННЫЕ", lambda: self.set_mods_tab("installed"), width=170, height=32,
                    fs=10).pack(side="left", padx=6)
        PixelButton(f3, "ПАПКА", self.open_content_folder, width=90, height=32, fs=10).pack(side="left")
        stack = tk.Frame(pad, bg=C["bg1"])
        stack.pack(fill="both", expand=True)
        stack.rowconfigure(0, weight=1)
        stack.columnconfigure(0, weight=1)
        self.tab_search = tk.Frame(stack, bg=C["bg1"])
        self.tab_installed = tk.Frame(stack, bg=C["bg1"])
        for t in (self.tab_search, self.tab_installed):
            t.grid(row=0, column=0, sticky="nsew")
        self.res_scroll = ScrollFrame(self.tab_search, C["bg1"])
        bot = tk.Frame(self.tab_search, bg=C["bg1"])
        bot.pack(side="bottom", fill="x", pady=(6, 0))
        self.more_btn = PixelButton(bot, "ЕЩЁ", lambda: self.mods_search(more=True), width=100, height=32, fs=10)
        self.more_btn.pack(side="left")
        self.more_btn.set_enabled(False)
        self.mods_status_lbl = tk.Label(bot, text="Введите запрос и нажмите «Искать».", bg=C["bg1"],
                                        fg=C["muted"], font=font(9), anchor="w")
        self.mods_status_lbl.pack(side="left", padx=10)
        self.res_scroll.pack(fill="both", expand=True)
        self.inst_scroll = ScrollFrame(self.tab_installed, C["bg1"])
        self.inst_scroll.pack(fill="both", expand=True)
        self.set_mods_tab("search")

    def set_mods_tab(self, tab):
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
            self._sq = dict(src=self.v_src.get().lower(), ctype=ctype,
                            mc=tgt["mc"] if self.v_mc_only.get() else None, loader=loader,
                            sort=SORTS.get(self.v_sort.get(), "downloads"),
                            query=self.v_query.get().strip(), offset=0)
        q = dict(self._sq)
        key = self.v_cf_key.get().strip()
        if q["src"] == "curseforge" and not key:
            # ключ CurseForge необязателен: без него просто ищем на Modrinth
            q["src"] = "modrinth"
            self._sq["src"] = "modrinth"
            self.v_src.set("Modrinth")
        self._busy_search = True
        self.mods_status("Ищу...")

        def work():
            try:
                if q["src"] == "modrinth":
                    items, total = modrinth_search(q["query"], q["ctype"], q["mc"], q["loader"], q["sort"], q["offset"])
                else:
                    items, total = cf_search(key, q["query"], q["ctype"], q["mc"], q["loader"], q["sort"], q["offset"])
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
        row = tk.Frame(body, bg=C["bg0"], highlightbackground=C["line"], highlightthickness=1)
        row.pack(fill="x", pady=4, padx=(0, 6))
        row.columnconfigure(0, weight=1)
        info = tk.Frame(row, bg=C["bg0"])
        info.grid(row=0, column=0, sticky="ew", padx=12, pady=8)
        tk.Label(info, text=it["title"], bg=C["bg0"], fg=C["text"], font=font(11, True),
                 anchor="w").pack(anchor="w")
        tk.Label(info, text=f"{it['author']}   ·   скачиваний: {fmt_num(it['downloads'])}", bg=C["bg0"],
                 fg=C["yellow"], font=font(9), anchor="w").pack(anchor="w")
        tk.Label(info, text=it["desc"], bg=C["bg0"], fg=C["muted"], font=font(9), anchor="w",
                 justify="left", wraplength=480).pack(anchor="w")
        PixelButton(row, "СКАЧАТЬ", lambda i=it: self.mods_install(i), width=120, height=36, fs=10,
                    bg=C["bg0"]).grid(row=0, column=1, padx=12)

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
        key = self.v_cf_key.get().strip()
        self.mods_status(f"Скачиваю: {it['title']}...")

        def work():
            try:
                if it["source"] == "modrinth":
                    done = modrinth_install(it["id"], ctype, mc, loader, dest, self.log)
                else:
                    done = cf_install(key, it["id"], ctype, mc, loader, dest, self.log, it.get("url", ""))
                self.ui(lambda n=len(done): (self.mods_status(f"Готово: {it['title']} (файлов: {n})"),
                                             self.refresh_installed()))
            except ManualDownload as e:
                self.ui(lambda u=e.url: self._manual_download(u))
            except Exception as e:
                self.ui(lambda m=str(e): self.mods_status("Ошибка: " + m))
        threading.Thread(target=work, daemon=True).start()

    def _manual_download(self, url):
        self.mods_status("Автор запретил автоматическую загрузку — открываю страницу CurseForge")
        if url:
            webbrowser.open(url)

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
            tk.Label(body, text="Здесь пока ничего нет.", bg=C["bg1"], fg=C["muted"], font=font(10)).pack(
                anchor="w", pady=10)
            return
        for f in files:
            off = f.name.endswith(".disabled")
            row = tk.Frame(body, bg=C["bg0"], highlightbackground=C["line"], highlightthickness=1)
            row.pack(fill="x", pady=3, padx=(0, 6))
            tk.Label(row, text=("[выкл]  " if off else "") + f.name.replace(".disabled", ""), bg=C["bg0"],
                     fg=C["muted"] if off else C["text"], font=font(10), anchor="w").pack(
                side="left", padx=12, pady=8)
            PixelButton(row, "УДАЛИТЬ", lambda p=f: self.mod_delete(p), width=96, height=30, fs=9,
                        bg=C["bg0"]).pack(side="right", padx=8, pady=4)
            PixelButton(row, "ВКЛЮЧИТЬ" if off else "ВЫКЛЮЧИТЬ", lambda p=f: self.mod_toggle(p),
                        width=116, height=30, fs=9, bg=C["bg0"]).pack(side="right", pady=4)

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
        pad = tk.Frame(page, bg=C["bg1"])
        pad.pack(fill="both", expand=True, padx=26, pady=20)
        pad.columnconfigure(1, weight=1)
        pad.rowconfigure(1, weight=1)
        self._title(pad, "АККАУНТЫ").grid(row=0, column=0, columnspan=2, sticky="w")
        left = tk.Frame(pad, bg=C["bg1"])
        left.grid(row=1, column=0, sticky="ns", padx=(0, 20), pady=(12, 0))
        box = tk.Frame(left, bg=C["line"])
        box.pack(fill="both", expand=True)
        self.acc_list = self._listbox(box, 32, 10)
        self.acc_list.pack(fill="both", expand=True, padx=2, pady=2)
        self.acc_list.bind("<<ListboxSelect>>", lambda e: self.show_account_info())
        self.acc_list.bind("<Double-Button-1>", lambda e: self.use_selected_account())
        right = tk.Frame(pad, bg=C["bg1"])
        right.grid(row=1, column=1, sticky="nsew", pady=(12, 0))
        self.acc_info = tk.Label(right, text="", bg=C["bg1"], fg=C["text"], font=font(10), anchor="nw",
                                 justify="left", wraplength=440)
        self.acc_info.pack(anchor="w", fill="x")
        btns = tk.Frame(right, bg=C["bg1"])
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
        pad = tk.Frame(sf.body, bg=C["bg1"])
        pad.pack(fill="both", expand=True, padx=26, pady=20)
        pad.columnconfigure(0, weight=1, uniform="col")
        pad.columnconfigure(1, weight=1, uniform="col")
        left = tk.Frame(pad, bg=C["bg1"])
        left.grid(row=0, column=0, sticky="new", padx=(0, 14))
        right = tk.Frame(pad, bg=C["bg1"])
        right.grid(row=0, column=1, sticky="new", padx=(14, 0))

        # --- память
        s = self._section(left, "Память")
        s.pack(fill="x", pady=(0, 14))
        row = tk.Frame(s, bg=C["bg1"])
        row.pack(fill="x")
        self._label(row, "Максимум (Xmx)").pack(side="left")
        self.ram_label = tk.Label(row, text="", bg=C["bg1"], fg=C["yellow"], font=font(11, True))
        self.ram_label.pack(side="right")
        PixelSlider(s, self.v_ram, 1024, self.max_ram, 256).pack(fill="x", pady=(4, 6))
        self.v_ram.trace_add("write", lambda *a: self._ram_text())
        self._ram_text()
        row = tk.Frame(s, bg=C["bg1"])
        row.pack(fill="x", pady=(2, 4))
        self._label(row, "Старт (Xms, МБ)").pack(side="left")
        tk.Spinbox(row, from_=256, to=self.max_ram, increment=256, textvariable=self.v_xms, width=7,
                   bg=C["input"], fg=C["text"], insertbackground=C["yellow"],
                   buttonbackground=C["bg2"], relief="flat", highlightthickness=2,
                   highlightbackground=C["line"], highlightcolor=C["yellow"],
                   font=font(11)).pack(side="right")
        row = tk.Frame(s, bg=C["bg1"])
        row.pack(fill="x", pady=(6, 0))
        PixelButton(row, "АВТО", self.auto_ram, width=90, height=30, fs=9).pack(side="left")
        self._label(row, f"  всего ОЗУ: {self.total_ram} МБ", muted=True).pack(side="left")

        # --- флаги
        s = self._section(left, "Флаги оптимизации (JVM)")
        s.pack(fill="x", pady=(0, 14))
        self._label_to_key = {v[0]: k for k, v in FLAG_PRESETS.items()}
        self.preset_combo = ttk.Combobox(s, state="readonly", style="U.TCombobox", font=font(10),
                                         values=[v[0] for v in FLAG_PRESETS.values()])
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
        r1 = tk.Frame(s, bg=C["bg1"])
        r1.pack(anchor="w", pady=(8, 0))
        PixelButton(r1, "ПАПКА MINECRAFT", self.open_minecraft_folder, width=178, height=34, fs=9).pack(side="left")
        PixelButton(r1, "ПАПКА ВЕРСИИ", self.open_version_folder, width=160, height=34, fs=9).pack(
            side="left", padx=6)
        r2 = tk.Frame(s, bg=C["bg1"])
        r2.pack(anchor="w", pady=(6, 0))
        PixelButton(r2, "БЭКАП МИРОВ", self.do_backup, width=140, height=34, fs=9).pack(side="left")
        PixelButton(r2, "ИМПОРТ ИЗ .MINECRAFT", self.do_import, width=210, height=34, fs=9).pack(
            side="left", padx=6)
        r3 = tk.Frame(s, bg=C["bg1"])
        r3.pack(anchor="w", pady=(6, 0))
        PixelButton(r3, "СБРОС НАСТРОЕК", self.reset_settings, width=170, height=34, fs=9).pack(side="left")

        # --- игра
        s = self._section(right, "Игра")
        s.pack(fill="x", pady=(0, 14))
        PixelCheck(s, "Свой размер окна", self.v_custom_res).pack(anchor="w")
        row = tk.Frame(s, bg=C["bg1"])
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
        row = tk.Frame(s, bg=C["bg1"])
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
        self._label(s, "API-ключ CurseForge (необязательно)").pack(anchor="w")
        self._entry(s, self.v_cf_key, 30, show="•").pack(fill="x", pady=(4, 0))
        self._label(s, "Без ключа моды ищутся на Modrinth, там есть почти все популярные. Ключ выдаёт console.curseforge.com.", muted=True,
                    wrap=380).pack(anchor="w", pady=(6, 0))

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
        row = tk.Frame(s, bg=C["bg1"])
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

    def reset_settings(self):
        if not messagebox.askyesno(APP_NAME, "Сбросить все настройки? Аккаунты и сборки останутся."):
            return
        keep = {k: self.cfg[k] for k in ("accounts", "active_account") if k in self.cfg}
        save_cfg(keep)
        self._skip_save = True
        messagebox.showinfo(APP_NAME, "Настройки сброшены. Перезапустите лаунчер.")

    # --- лог
    def _build_log(self, page):
        pad = tk.Frame(page, bg=C["bg1"])
        pad.pack(fill="both", expand=True, padx=22, pady=18)
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
        row = tk.Frame(pad, bg=C["bg1"])
        row.pack(fill="x", pady=(10, 0))
        PixelButton(row, "ОЧИСТИТЬ", self.clear_log, width=130, height=36, fs=10).pack(side="left")
        PixelButton(row, "КОПИРОВАТЬ", self.copy_log, width=140, height=36, fs=10).pack(side="left", padx=10)

    def _build_bottom(self, parent):
        bottom = tk.Frame(parent, bg=C["bg0"])
        bottom.grid(row=1, column=0, columnspan=2, sticky="ew")
        tk.Frame(bottom, bg=C["line"], height=1).pack(fill="x")
        strip = tk.Frame(bottom, bg=C["bg0"])
        strip.pack(fill="x", padx=16, pady=(8, 0))
        self.status = tk.Label(strip, text="Загрузка списка версий...", bg=C["bg0"], fg=C["muted"],
                               font=font(9), anchor="w")
        self.status.pack(fill="x")
        self.pbar = SmoothProgress(strip, bg=C["bg0"])
        self.pbar.pack(fill="x", pady=(3, 0))

        ctrl = tk.Frame(bottom, bg=C["bg0"])
        ctrl.pack(fill="x", padx=16, pady=12)
        ctrl.columnconfigure(0, weight=1, uniform="c")
        ctrl.columnconfigure(1, weight=0)
        ctrl.columnconfigure(2, weight=1, uniform="c")

        left = tk.Frame(ctrl, bg=C["bg0"])
        left.grid(row=0, column=0, sticky="w")
        tk.Label(left, text="ВЕРСИЯ / СБОРКА", bg=C["bg0"], fg=C["muted"], font=font(9, True)).pack(anchor="w")
        self.combo = ttk.Combobox(left, textvariable=self.v_launch, state="readonly", width=26,
                                  style="U.TCombobox", font=font(10))
        self.combo.pack(pady=(3, 0))

        self.play = PixelButton(ctrl, "ИГРАТЬ", self.on_play, width=280, height=58, fs=17, bg=C["bg0"], kind="primary", pulse=True)
        self.play.grid(row=0, column=1)

        right = tk.Frame(ctrl, bg=C["bg0"])
        right.grid(row=0, column=2, sticky="e")
        tk.Label(right, text="АККАУНТ", bg=C["bg0"], fg=C["muted"], font=font(9, True)).pack(anchor="w")
        self.acc_combo = ttk.Combobox(right, textvariable=self.v_account, state="readonly", width=24,
                                      style="U.TCombobox", font=font(10))
        self.acc_combo.pack(pady=(3, 0))
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
    root = tk.Tk()
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()
