import hashlib
import ipaddress
import json
import re
import shutil
import subprocess
import time
from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path
from urllib.parse import urlsplit
from uuid import uuid4

import psutil
import requests

VIDEO_EXTENSIONS = {".mp4", ".mov", ".mkv", ".webm", ".m4v", ".avi", ".mpeg", ".mpg", ".ts"}
FFMPEG_BIN = Path(__file__).resolve().parent.parent / ".tools" / "ffmpeg" / "bin"


def program(setting, name):
    if setting:
        path = Path(setting) / name
    elif name in {"ffmpeg.exe", "ffprobe.exe"} and all(
            (FFMPEG_BIN / tool).is_file() for tool in ("ffmpeg.exe", "ffprobe.exe")):
        path = FFMPEG_BIN / name
    else:
        path = shutil.which(name)
    if not path or not Path(path).is_file():
        raise ValueError(f"Укажите папку с {name} в настройках.")
    return str(Path(path).resolve())


def run(executable, args, timeout=25, binary=False):
    try:
        result = subprocess.run(
            [executable, *map(str, args)], cwd=Path(executable).parent,
            capture_output=True, timeout=timeout, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except subprocess.TimeoutExpired:
        raise ValueError(f"{Path(executable).name}: превышено время ожидания ({timeout} с).") from None
    if result.returncode:
        detail = decode(result.stderr or result.stdout).strip()[-600:]
        raise ValueError(f"{Path(executable).name}: код {result.returncode}. {detail}")
    return result.stdout if binary else decode(result.stdout).strip()


def decode(value):
    try:
        return value.decode("utf-8-sig")
    except UnicodeDecodeError:
        return value.decode("mbcs", errors="replace")


@dataclass(frozen=True)
class Instance:
    index: int
    name: str
    window: int
    android: bool
    player_pid: int
    vm_pid: int

    @property
    def status(self):
        if self.android:
            return "Android готов"
        return "Запускается" if self.player_pid > 0 or self.window else "Остановлен"


def parse_instances(output):
    instances = []
    for line in output.splitlines():
        try:
            index, rest = line.split(",", 1)
            name, window, _, started, pid, vm = rest.rsplit(",", 5)
            if int(index) < 0 or started not in ("0", "1"):
                raise ValueError
            instances.append(Instance(int(index), name, int(window), started == "1", int(pid), int(vm)))
        except ValueError:
            raise ValueError("LDPlayer вернул неизвестный формат списка экземпляров.") from None
    return instances


class LDPlayer:
    def __init__(self, directory):
        self.directory = directory
        if not directory:
            raise ValueError("Выберите папку установленного LDPlayer в настройках.")
        names = ("ldconsole.exe", "dnconsole.exe")
        self.console = next((str((Path(directory) / name).resolve()) for name in names
                             if (Path(directory) / name).is_file()), None)
        if not self.console:
            raise ValueError("В выбранной папке отсутствуют ldconsole.exe и dnconsole.exe.")

    def instances(self):
        return parse_instances(run(self.console, ["list2"]))

    def action(self, index, action):
        arguments = {
            "launch": ["launch"],
            "quit": ["quit"],
            "instagram": ["runapp", "--packagename", "com.instagram.android"],
        }
        instance = next((row for row in self.instances() if row.index == index), None)
        if not instance:
            raise ValueError("Экземпляр LDPlayer с этим индексом не найден.")
        if action == "instagram":
            diagnostic = self.diagnose(instance)
            if not diagnostic["instagram"]:
                raise ValueError("Instagram недоступен. Проверьте ADB и установку приложения.")
        output = run(self.console, [*arguments[action], "--index", index])
        if re.search(r"(?i)\b(error|failed|not found|invalid)\b", output):
            raise ValueError(f"LDPlayer отклонил команду: {output[:400]}")
        return output

    def adb(self, instance):
        if not instance.android:
            raise ValueError("Дождитесь загрузки Android.")
        adb = program(self.directory, "adb.exe")
        output = run(adb, ["devices"])
        candidates = (f"emulator-{5554 + instance.index * 2}", f"127.0.0.1:{5555 + instance.index * 2}")
        devices = {parts[0]: parts[1] for line in output.splitlines() if len(parts := line.split()) == 2}
        serial = next((serial for serial in candidates if devices.get(serial) == "device"), None)
        if not serial:
            status = ", ".join(f"{serial}: {devices[serial]}" for serial in candidates if serial in devices)
            raise ValueError("Локальное ADB-подключение экземпляра недоступно. " + status)
        return adb, serial

    def diagnose(self, instance):
        result = {"status": instance.status, "adb": "Недоступно", "instagram": False,
                  "cpu": None, "ram": None, "boot": None}
        processes = []
        for pid in {instance.player_pid, instance.vm_pid} - {-1, 0}:
            try:
                process = psutil.Process(pid)
                if re.match(r"(?i)^(dnplayer|ldplayer|ldvbox|vboxheadless)", process.name()):
                    process.cpu_percent()
                    processes.append(process)
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue
        try:
            adb, serial = self.adb(instance)
            result["adb"] = serial
            result["boot"] = run(adb, ["-s", serial, "shell", "getprop", "sys.boot_completed"], timeout=8)
            packages = run(adb, ["-s", serial, "shell", "pm", "path", "com.instagram.android"], timeout=8)
            result["instagram"] = any(line.startswith("package:") for line in packages.splitlines())
        except ValueError as exc:
            result["note"] = str(exc)
        if processes:
            time.sleep(0.25)
            try:
                result["cpu"] = round(sum(p.cpu_percent() for p in processes) / (psutil.cpu_count() or 1), 1)
                result["ram"] = round(sum(p.memory_info().rss for p in processes) / 1024 ** 2)
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                pass
        return result

    def screenshot(self, instance, destination):
        adb, serial = self.adb(instance)
        content = run(adb, ["-s", serial, "exec-out", "screencap", "-p"], binary=True)
        if not content.startswith(b"\x89PNG\r\n\x1a\n"):
            raise ValueError("ADB не вернул PNG-снимок.")
        with Path(destination).open("xb") as target:
            target.write(content)


def sha256(path):
    with Path(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def probe(path, directory):
    output = run(program(directory, "ffprobe.exe"), [
        "-v", "error", "-protocol_whitelist", "file",
        "-show_format", "-show_streams", "-of", "json", str(Path(path).resolve()),
    ])
    info = json.loads(output)
    streams = info.get("streams", [])
    video = next((s for s in streams if s["codec_type"] == "video" and
                  not s.get("disposition", {}).get("attached_pic")), None)
    if not video:
        raise ValueError("В файле нет видеодорожки.")
    audio = next((s for s in streams if s["codec_type"] == "audio"), {})
    width, height = video.get("width"), video.get("height")
    rotation = next((d["rotation"] for d in video.get("side_data_list", []) if "rotation" in d), 0)
    if rotation % 180 and width and height:
        width, height = height, width
    duration = video.get("duration") or info.get("format", {}).get("duration")
    frame_rate = video.get("avg_frame_rate")
    fps = float(Fraction(frame_rate)) if frame_rate and frame_rate != "0/0" else None
    return {"width": width, "height": height, "video": video.get("codec_name"),
            "audio": audio.get("codec_name"), "seconds": float(duration) if duration not in (None, "N/A") else None,
            "fps": fps, "audio_rate": int(audio["sample_rate"]) if audio.get("sample_rate") else None,
            "video_bitrate": int(video["bit_rate"]) if video.get("bit_rate") else None,
            "container": info.get("format", {}).get("format_name", "")}


def import_video(db, path, directory):
    if Path(path).suffix.lower() not in VIDEO_EXTENSIONS:
        raise ValueError("Выберите видео с поддерживаемым расширением.")
    digest = sha256(path)
    metadata = probe(path, directory)
    return db.add_video(path, digest, metadata)


def vertical(source, target, directory):
    target = Path(target).resolve()
    if target.suffix.lower() != ".mp4" or target.exists():
        raise ValueError("Выберите новый файл MP4.")
    temporary = target.with_name(f".{uuid4().hex}.mp4")
    try:
        run(program(directory, "ffmpeg.exe"), [
            "-nostdin", "-v", "error", "-n", "-protocol_whitelist", "file",
            "-i", str(Path(source).resolve()),
            "-map", "0:V:0", "-map", "0:a:0?",
            "-vf", "scale=1080:1920:force_original_aspect_ratio=decrease:force_divisible_by=2,"
            "pad=1080:1920:(ow-iw)/2:(oh-ih)/2,setsar=1,fps=30",
            "-c:v", "libx264", "-pix_fmt", "yuv420p", "-maxrate", "20M", "-bufsize", "40M",
            "-c:a", "aac", "-ar", "48000", "-ac", "2", "-b:a", "128k", "-movflags", "+faststart", str(temporary),
        ], timeout=1200)
        metadata = probe(temporary, directory)
        temporary.rename(target)
        return metadata
    finally:
        temporary.unlink(missing_ok=True)


def network_check(platform, proxy=None, external_ip=False):
    hosts = {"YouTube": "https://www.googleapis.com", "Instagram": "https://graph.instagram.com"}
    if proxy:
        parsed = urlsplit(proxy)
        if parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.path not in ("", "/"):
            raise ValueError("Укажите HTTP(S)-прокси в формате URL.")
    latencies = []
    ip = None
    with requests.Session() as session:
        session.trust_env = False
        if proxy:
            session.proxies.update({"https": proxy})
        for _ in range(3):
            start = time.monotonic()
            try:
                response = session.head(hosts[platform], timeout=(4, 5), allow_redirects=False)
                latencies.append(round((time.monotonic() - start) * 1000)
                                 if response.status_code < 500 and response.status_code != 407 else None)
            except requests.RequestException:
                latencies.append(None)
        if external_ip:
            try:
                response = session.get("https://api.ipify.org", timeout=(4, 5), allow_redirects=False)
                if response.status_code == 200:
                    ip = str(ipaddress.ip_address(response.text.strip()))
            except (ValueError, requests.RequestException):
                pass
    return latencies, ip
