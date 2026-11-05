"""Build original local demo assets. No account access or publication."""

import argparse
from array import array
import html
import json
import math
from pathlib import Path
import sys
import wave

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from PySide6.QtCore import QByteArray
from PySide6.QtGui import QFont, QFontDatabase, QFontMetrics, QGuiApplication, QImage, QPainter
from PySide6.QtSvg import QSvgRenderer

from creator_control.tools import probe, program, run, sha256


def svg_image(body, width, height):
    return f'''<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}">
    <defs><linearGradient id="bg" x2="1" y2="1"><stop stop-color="#0D1628"/><stop offset="1" stop-color="#20304B"/></linearGradient>
    <linearGradient id="ink" x2="1" y2="1"><stop stop-color="#95E5F0"/><stop offset="1" stop-color="#B5A0FA"/></linearGradient></defs>
    <rect width="{width}" height="{height}" fill="url(#bg)"/>{body}</svg>'''


def word(text, x, y, size=64, fill="#F1F6FF", weight=600):
    return (f'<text x="{x}" y="{y}" fill="{fill}" font-family="Segoe UI" '
            f'font-size="{size}" font-weight="{weight}">{html.escape(text)}</text>')


def mark(cx, cy, size=1):
    return f'''<g transform="translate({cx} {cy}) scale({size})">
    <circle r="225" fill="#172740" stroke="#3A5570" stroke-width="2"/>
    <ellipse rx="300" ry="112" transform="rotate(-30)" fill="none" stroke="#81DAE9" stroke-width="4"/>
    <path d="M-104 115 V-115 H-54 L104 115 V-115" fill="none" stroke="url(#ink)" stroke-width="46" stroke-linejoin="round"/>
    <circle cx="258" cy="-138" r="17" fill="#FBAA91"/>
    </g>'''


def render(svg, destination, width, height):
    renderer = QSvgRenderer(QByteArray(svg.encode("utf-8")))
    if not renderer.isValid():
        raise ValueError("Invalid generated SVG")
    image = QImage(width, height, QImage.Format.Format_ARGB32)
    image.fill(0)
    painter = QPainter(image)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    renderer.render(painter)
    painter.end()
    if not image.save(str(destination)):
        raise ValueError("Image could not be saved")


def timecode(seconds):
    value = round(seconds * 1000)
    return f"{value // 3600000:02}:{value // 60000 % 60:02}:{value // 1000 % 60:02},{value % 1000:03}"


def sound_bed(path, duration):
    rate = 48000
    samples = array("h")
    for index in range(round(rate * duration)):
        time = index / rate
        envelope = min(1, time / .8, (duration - time) / .8)
        value = int(32767 * .028 * envelope * sum(
            math.sin(2 * math.pi * frequency * time) for frequency in (220, 277.1826, 329.6276)))
        samples.extend((value, value))
    if sys.byteorder != "little":
        samples.byteswap()
    with wave.open(str(path), "wb") as stream:
        stream.setnchannels(2)
        stream.setsampwidth(2)
        stream.setframerate(rate)
        stream.writeframes(samples.tobytes())


def build_video(output, name, scenes, duration):
    frames = output / name
    frames.mkdir()
    captions = []
    entries = []
    interval = duration / len(scenes)
    for index, (lines, caption, colors) in enumerate(scenes):
        art = "".join(f'<circle cx="{260 + i * 280}" cy="690" r="116" fill="{color}"/>'
                      for i, color in enumerate(colors)) if colors else mark(540, 680, 1.13)
        body = word("NOVA / VIRTUAL STUDIO", 90, 150, 34, "#95E5F0")
        body += word(f"{index + 1:02}", 918, 150, 34, "#A1B3CD") + art
        for line_index, line in enumerate(lines):
            body += word(line, 90, 1160 + line_index * 100, 68)
        body += '<rect x="90" y="1540" width="900" height="2" fill="#45617E"/>'
        body += word("VIRTUAL CHARACTER CONCEPT", 90, 1615, 28, "#95E5F0")
        body += word("Original graphics + synthesized sound", 90, 1670, 28, "#B3C1D5", 400)
        svg = svg_image(body, 1080, 1920)
        frame = frames / f"frame-{index + 1}.png"
        (frames / f"frame-{index + 1}.svg").write_text(svg, encoding="utf-8")
        render(svg, frame, 1080, 1920)
        entries.extend([f"file '{name}/frame-{index + 1}.png'", f"duration {interval}"])
        captions.append(f"{index + 1}\n{timecode(index * interval)} --> {timecode((index + 1) * interval)}\n{caption}\n")
    entries.append(f"file '{name}/frame-{len(scenes)}.png'")
    timeline = output / f"{name}.ffconcat"
    timeline.write_text("\n".join(entries) + "\n", encoding="utf-8")
    (output / f"{name}.srt").write_text("\n".join(captions), encoding="utf-8")
    audio = output / f"{name}.wav"
    sound_bed(audio, duration)
    target = output / f"{name}.mp4"
    run(program("", "ffmpeg.exe"), [
        "-nostdin", "-v", "error", "-n", "-f", "concat", "-safe", "1", "-i", str(timeline),
        "-i", str(audio), "-t", duration, "-r", "30", "-c:v", "libx264", "-preset", "veryfast",
        "-crf", "21", "-pix_fmt", "yuv420p", "-vf", "setsar=1", "-c:a", "aac", "-ar", "48000",
        "-ac", "2", "-b:a", "128k", "-movflags", "+faststart", str(target),
    ], timeout=300)
    metadata = probe(target, "")
    assert (metadata["width"], metadata["height"], metadata["video"], metadata["audio"]) == (1080, 1920, "h264", "aac")
    assert abs(metadata["seconds"] - duration) < .2 and metadata["audio_rate"] == 48000
    # Decode the entire output, including sound, to catch broken streams.
    run(program("", "ffmpeg.exe"), ["-nostdin", "-v", "error", "-i", str(target), "-f", "null", "-"], timeout=120)
    return {"file": target.name, "sha256": sha256(target), **metadata}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    options = parser.parse_args()
    output = options.output.resolve()
    if output.exists() and any(output.iterdir()):
        raise SystemExit("Choose a new empty output directory; existing assets are preserved.")
    output.mkdir(parents=True, exist_ok=True)
    app = QGuiApplication.instance() or QGuiApplication([])
    font = Path("C:/Windows/Fonts/segoeui.ttf")
    if font.is_file():
        QFontDatabase.addApplicationFont(str(font))
    app.setFont(QFont("Segoe UI"))
    if not QFontMetrics(app.font()).inFont("N"):
        raise SystemExit("A font with Latin glyphs is required to render the demo.")
    avatar = svg_image(mark(512, 512, 1.35), 1024, 1024)
    banner = svg_image(mark(880, 720, .65) + word("NOVA", 1170, 713, 105) +
                       word("VIRTUAL STYLE STUDIO", 1178, 780, 30, "#95E5F0"), 2560, 1440)
    for name, svg, width, height in (("avatar", avatar, 1024, 1024), ("banner", banner, 2560, 1440)):
        (output / f"{name}.svg").write_text(svg, encoding="utf-8")
        render(svg, output / f"{name}.png", width, height)
    intro = [
        (["Meet NOVA.", "A virtual character."], "Meet NOVA.\nA virtual character.", None),
        (["Original style.", "Color experiments."], "Original style.\nColor experiments.", ["#95E5F0", "#FBAA91", "#B5A0FA"]),
        (["Every look starts", "with a choice."], "Every look starts with a choice.", None),
        (["Follow the", "creative process."], "Follow the creative process.\nDigitally created.", None),
    ]
    palettes = [
        (["One character.", "Three color worlds."], "One character.\nThree color worlds.", ["#95E5F0", "#FBAA91", "#B5A0FA"]),
        (["Glacier:", "cool and quiet."], "Glacier: cool and quiet.", ["#D9F8FA", "#95E5F0", "#387391"]),
        (["Ember:", "warm and bold."], "Ember: warm and bold.", ["#FAD5BB", "#FBAA91", "#AA4C60"]),
        (["Orbit:", "soft and curious.", "Which would you choose?"], "Orbit: soft and curious.\nWhich would you choose?", ["#E6DDFB", "#B5A0FA", "#645485"]),
    ]
    report = {"created_for": "Local test cards; owner review before publication", "voice": "none",
              "sound": "Original synthesized tones; no samples", "videos": [
                  build_video(output, "nova-intro", intro, 18),
                  build_video(output, "nova-palettes", palettes, 15),
              ]}
    (output / "verification.json").write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    app.quit()


if __name__ == "__main__":
    main()
