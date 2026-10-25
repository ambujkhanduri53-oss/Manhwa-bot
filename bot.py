"""Runs once per GitHub Actions run. Reads new Telegram messages,
turns each album of manhwa images into a narrated video, sends it back."""
import os
import time
import shutil
import asyncio
import subprocess
import tempfile

import requests
from PIL import Image
import edge_tts
from google import genai
from google.genai import types

TOKEN = os.environ["TELEGRAM_TOKEN"]
GEMINI_KEY = os.environ["GEMINI_API_KEY"]
MODEL = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash")
ALLOWED = os.environ.get("ALLOWED_USER_ID", "").strip()

API = f"https://api.telegram.org/bot{TOKEN}"
FILE_API = f"https://api.telegram.org/file/bot{TOKEN}"
VOICES = {"Hindi": "hi-IN-SwaraNeural", "English": "en-US-AriaNeural"}
HELP = (
    "Images ka album bhejo (ek saath sab images select karke).\n"
    "Pehli image ki caption me likho:\n"
    "Manhwa ka naam, chapter number\n\n"
    "Jaise: Solo Leveling Chapter 12\n"
    "English narration ke liye: Solo Leveling Chapter 12 | english\n\n"
    "Jawab 5 se 30 minute me aata hai."
)

client = genai.Client(api_key=GEMINI_KEY)


def tg(method, **params):
    r = requests.post(f"{API}/{method}", json=params, timeout=60)
    return r.json()


def say(chat_id, text):
    for i in range(0, len(text), 4000):
        tg("sendMessage", chat_id=chat_id, text=text[i:i + 4000])


def is_image(m):
    if "photo" in m:
        return True
    return m.get("document", {}).get("mime_type", "").startswith("image/")


def gemini(prompt, search=False):
    cfg = None
    if search:
        cfg = types.GenerateContentConfig(
            tools=[types.Tool(google_search=types.GoogleSearch())]
        )
    r = client.models.generate_content(model=MODEL, contents=prompt, config=cfg)
    return (r.text or "").strip()


def write_script(title, lang):
    return gemini(
        f"Search the web for the story / plot of: {title}. "
        f"Then write a narration script for a YouTube manhwa recap video in {lang}. "
        "Use your own words, do not copy sentences from any website. "
        "About 300 to 400 words, engaging storytelling tone, plain text only, "
        "no headings, no emojis, no stage directions. "
        "If you cannot find reliable information, reply with exactly: NOT_FOUND",
        search=True,
    )


def download(m, path):
    file_id = m["photo"][-1]["file_id"] if "photo" in m else m["document"]["file_id"]
    info = tg("getFile", file_id=file_id)
    fp = info["result"]["file_path"]
    data = requests.get(f"{FILE_API}/{fp}", timeout=120).content
    with open(path, "wb") as f:
        f.write(data)


def build_video(workdir, images, script, lang):
    voice_path = os.path.join(workdir, "voice.mp3")
    asyncio.run(edge_tts.Communicate(script, VOICES[lang]).save(voice_path))

    out = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "default=nw=1:nk=1", voice_path],
        capture_output=True, text=True, check=True,
    )
    per = max(float(out.stdout.strip()) / len(images), 1.0)

    fitted = []
    for i, p in enumerate(images):
        img = Image.open(p).convert("RGB")
        img.thumbnail((1280, 720))
        canvas = Image.new("RGB", (1280, 720), (0, 0, 0))
        canvas.paste(img, ((1280 - img.width) // 2, (720 - img.height) // 2))
        fp = os.path.join(workdir, f"fit_{i:03d}.jpg")
        canvas.save(fp, quality=90)
        fitted.append(fp)

    lst = os.path.join(workdir, "list.txt")
    with open(lst, "w") as f:
        for fp in fitted:
            f.write(f"file '{fp}'\nduration {per:.3f}\n")
        f.write(f"file '{fitted[-1]}'\n")

    video = os.path.join(workdir, "final.mp4")
    subprocess.run(
        ["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", lst,
         "-i", voice_path, "-vf", "fps=24,format=yuv420p",
         "-c:v", "libx264", "-preset", "veryfast", "-c:a", "aac",
         "-shortest", video],
        check=True, capture_output=True,
    )
    return video


def process_group(chat_id, msgs, title, lang):
    say(chat_id, f"{len(msgs)} images mili. '{title}' ({lang}) ka video ban raha hai...")
    workdir = tempfile.mkdtemp()
    try:
        images = []
        for i, m in enumerate(msgs):
            path = os.path.join(workdir, f"img_{i:03d}.jpg")
            download(m, path)
            images.append(path)

        script = write_script(title, lang)
        if not script or "NOT_FOUND" in script:
            say(chat_id, "Is manhwa ki reliable story nahi mili. Naam/chapter "
                         "check karke dobara bhejo.")
            return

        video = build_video(workdir, images, script, lang)
        size_mb = os.path.getsize(video) / 1e6
        if size_mb > 49:
            say(chat_id, f"Video {size_mb:.0f} MB ka hai, Telegram limit 50 MB hai. "
                         "Kam images ke saath try karo.")
        else:
            with open(video, "rb") as f:
                requests.post(
                    f"{API}/sendVideo",
                    data={"chat_id": chat_id, "supports_streaming": "true"},
                    files={"video": ("video.mp4", f)},
                    timeout=600,
                )
        say(chat_id, "Script (description ke liye use kar sakte ho):\n\n" + script)
    except subprocess.CalledProcessError as e:
        tail = (e.stderr or b"")[-400:]
        say(chat_id, f"Video banane me error: {tail!r}")
    except Exception as e:  # noqa
        say(chat_id, f"Error aaya: {type(e).__name__}: {str(e)[:300]}")
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


def main():
    res = tg("getUpdates", timeout=0, allowed_updates=["message"])
    updates = res.get("result", [])
    if not updates:
        print("No new messages.")
        return
    last_id = updates[-1]["update_id"]
    msgs = [u["message"] for u in updates if "message" in u]

    # First-time setup: tell the sender their ID, process nothing.
    if not ALLOWED:
        tg("getUpdates", offset=last_id + 1, timeout=0)
        for chat_id in {m["chat"]["id"] for m in msgs}:
            say(chat_id, "Aapki Telegram ID hai: "
                         f"{msgs[0]['from']['id']}\n"
                         "Ise GitHub secret ALLOWED_USER_ID me daalo.")
        return

    msgs = [m for m in msgs if str(m.get("from", {}).get("id")) == ALLOWED]
    if not msgs:
        tg("getUpdates", offset=last_id + 1, timeout=0)
        return

    # Wait until a whole album has arrived.
    if any(is_image(m) for m in msgs) and time.time() - max(m["date"] for m in msgs) < 90:
        print("Images abhi aa rahi hain, agli run me process hoga.")
        return

    tg("getUpdates", offset=last_id + 1, timeout=0)  # confirm these updates

    chat_id = msgs[0]["chat"]["id"]
    if any(not is_image(m) for m in msgs):
        say(chat_id, HELP)

    groups = {}
    for m in msgs:
        if is_image(m):
            key = m.get("media_group_id") or f"single_{m['message_id']}"
            groups.setdefault(key, []).append(m)

    for group in groups.values():
        group.sort(key=lambda m: m["message_id"])
        caption = next((m["caption"] for m in group if m.get("caption")), "").strip()
        if not caption:
            say(chat_id, "Caption nahi mili. " + HELP)
            continue
        lang = "Hindi"
        title = caption
        if "|" in caption:
            title, opt = [s.strip() for s in caption.split("|", 1)]
            if "english" in opt.lower():
                lang = "English"
        process_group(chat_id, group, title, lang)


if __name__ == "__main__":
    main()
