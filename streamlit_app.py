"""Transcriptly — YouTube transcript generator (Streamlit edition).

Paste a YouTube link → get the transcript instantly in the video's own language.
Fast & free. Two caption providers: youtube-transcript-api first, yt-dlp fallback.
"""
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.request

import streamlit as st
from youtube_transcript_api import YouTubeTranscriptApi
from youtube_transcript_api._errors import (
    NoTranscriptFound,
    TranscriptsDisabled,
    VideoUnavailable,
)

YTDLP_TIMEOUT = 90

VIDEO_ID_RE = re.compile(
    r"(?:youtube\.com/(?:watch\?.*v=|shorts/|embed/|live/|v/)|youtu\.be/)([A-Za-z0-9_-]{11})"
)
BARE_ID_RE = re.compile(r"^[A-Za-z0-9_-]{11}$")


class BotCheckError(Exception):
    pass


def extract_video_id(url: str) -> str | None:
    url = (url or "").strip()
    m = VIDEO_ID_RE.search(url)
    if m:
        return m.group(1)
    if BARE_ID_RE.match(url):
        return url
    return None


def run_ytdlp(args: list[str]) -> subprocess.CompletedProcess:
    cmd = [sys.executable, "-m", "yt_dlp", "--no-warnings", "--no-playlist"]
    if os.environ.get("TRANSCRIPTLY_INSECURE_SSL") == "1":
        # sandbox-only: MITM proxy breaks cert verification
        cmd.append("--no-check-certificate")
    return subprocess.run(
        cmd + args,
        capture_output=True, text=True, timeout=YTDLP_TIMEOUT, cwd="/tmp",
    )


def ytdlp_info(video_id: str) -> dict:
    proc = run_ytdlp(["--skip-download", "--dump-json",
                      f"https://www.youtube.com/watch?v={video_id}"])
    if proc.returncode != 0:
        err = proc.stderr or proc.stdout or ""
        if "not a bot" in err:
            raise BotCheckError()
        raise RuntimeError(err[-300:])
    return json.loads(proc.stdout)


def ytdlp_languages(info: dict) -> list[dict]:
    langs: list[dict] = []
    for code, tracks in (info.get("subtitles") or {}).items():
        langs.append({"code": code,
                      "name": (tracks[0].get("name") if tracks else None) or code,
                      "auto": False})
    for code, tracks in (info.get("automatic_captions") or {}).items():
        langs.append({"code": code,
                      "name": (tracks[0].get("name") if tracks else None) or code,
                      "auto": True})
    return langs


def pick_detected(langs: list[dict]) -> dict | None:
    manual = [l for l in langs if not l["auto"]]
    if manual:
        return manual[0]
    return langs[0] if langs else None


VTT_TS = re.compile(
    r"(\d{2}):(\d{2}):(\d{2})\.(\d{3})\s*-->\s*(\d{2}):(\d{2}):(\d{2})\.(\d{3})"
)
TAG_RE = re.compile(r"<[^>]+>")


def _ts_to_sec(h, m, s, ms) -> float:
    return int(h) * 3600 + int(m) * 60 + int(s) + int(ms) / 1000


def parse_vtt(path: str) -> list[dict]:
    with open(path, encoding="utf-8", errors="replace") as f:
        content = f.read()
    segments: list[dict] = []
    for block in re.split(r"\n\s*\n", content):
        lines = [ln.strip() for ln in block.strip().splitlines() if ln.strip()]
        if not lines or lines[0].startswith(("WEBVTT", "Kind:", "Language:", "NOTE")):
            continue
        m = VTT_TS.search(lines[0])
        if not m:
            continue
        start = _ts_to_sec(*m.groups()[:4])
        end = _ts_to_sec(*m.groups()[4:])
        text = TAG_RE.sub("", " ".join(lines[1:]))
        text = text.replace("&nbsp;", " ").replace("&amp;", "&")
        text = re.sub(r"\s+", " ", text).strip()
        if not text:
            continue
        if segments and segments[-1]["text"] == text:
            continue
        segments.append({"start": round(start, 2),
                         "duration": round(end - start, 2), "text": text})
    return segments


def fetch_via_ytdlp(video_id: str, lang: str | None, auto: bool = False) -> dict:
    info = ytdlp_info(video_id)
    langs = ytdlp_languages(info)
    if not langs:
        raise NoTranscriptFound(video_id, [], None)
    target = lang or (pick_detected(langs) or {}).get("code")
    if not target:
        raise NoTranscriptFound(video_id, [], None)
    if auto and target not in {l["code"] for l in langs if l["auto"]}:
        raise NoTranscriptFound(video_id, [target], None)

    tmpdir = tempfile.mkdtemp(prefix="tsub_")
    try:
        proc = run_ytdlp([
            "--skip-download", "--write-auto-sub" if auto else "--write-sub",
            "--sub-langs", target, "--sub-format", "vtt",
            "--output", os.path.join(tmpdir, "cap.%(ext)s"),
            f"https://www.youtube.com/watch?v={video_id}",
        ])
        if proc.returncode != 0 and "not a bot" in (proc.stderr or ""):
            raise BotCheckError()
        vtt_files = [f for f in os.listdir(tmpdir) if f.endswith(".vtt")]
        if not vtt_files:
            raise NoTranscriptFound(video_id, [target], None)
        segments = parse_vtt(os.path.join(tmpdir, vtt_files[0]))
        if not segments:
            raise NoTranscriptFound(video_id, [target], None)
        lang_name = next((l["name"] for l in langs if l["code"] == target), target)
        return {"language": lang_name, "language_code": target,
                "segments": segments,
                "text": "\n".join(s["text"] for s in segments)}
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def fetch_via_library(video_id: str, lang: str | None) -> dict:
    ytt = YouTubeTranscriptApi()
    if lang:
        fetched = ytt.fetch(video_id, languages=[lang])
    else:
        tracks = ytt.list(video_id)
        langs = [{"code": t.language_code, "name": t.language,
                  "auto": bool(t.is_generated)} for t in tracks]
        detected = pick_detected(langs)
        if not detected:
            raise NoTranscriptFound(video_id, [], None)
        fetched = ytt.fetch(video_id, languages=[detected["code"]])
    segments = [{"start": round(s.start, 2), "duration": round(s.duration, 2),
                 "text": s.text} for s in fetched.snippets]
    return {"language": fetched.language, "language_code": fetched.language_code,
            "segments": segments,
            "text": "\n".join(s["text"] for s in fetched.snippets)}


def get_title(video_id: str) -> str:
    try:
        req = urllib.request.Request(
            f"https://www.youtube.com/oembed?url="
            f"https://www.youtube.com/watch?v={video_id}&format=json",
            headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=10) as r:
            return json.loads(r.read().decode()).get("title", video_id)
    except Exception:
        return video_id


@st.cache_data(ttl=3600, show_spinner=False)
def cached_languages(url: str) -> tuple[str, list[dict]]:
    video_id = extract_video_id(url)
    try:
        ytt = YouTubeTranscriptApi()
        tracks = ytt.list(video_id)
        langs = [{"code": t.language_code, "name": t.language,
                  "auto": bool(t.is_generated)} for t in tracks]
        if not langs:
            raise NoTranscriptFound(video_id, [], None)
        return get_title(video_id), langs
    except (TranscriptsDisabled, VideoUnavailable, NoTranscriptFound):
        raise
    except Exception:
        info = ytdlp_info(video_id)
        langs = ytdlp_languages(info)
        if not langs:
            raise NoTranscriptFound(video_id, [], None)
        return info.get("title") or video_id, langs


@st.cache_data(ttl=3600, show_spinner=False)
def cached_transcript(url: str, lang: str | None, auto: bool) -> dict:
    video_id = extract_video_id(url)
    try:
        return fetch_via_library(video_id, lang)
    except (TranscriptsDisabled, VideoUnavailable, NoTranscriptFound):
        raise
    except Exception:
        return fetch_via_ytdlp(video_id, lang, auto)


def friendly_error(e: Exception) -> str:
    if isinstance(e, TranscriptsDisabled):
        return "Captions are disabled for this video."
    if isinstance(e, VideoUnavailable):
        return "Video not found or unavailable."
    if isinstance(e, NoTranscriptFound):
        return "No captions found for this video."
    if isinstance(e, BotCheckError):
        return "YouTube blocked this video with a bot-check. Please try a different video."
    return f"Could not fetch captions: {str(e)[-200:]}"


# ------------------------------------------------------------------ UI
st.set_page_config(page_title="Transcriptly", page_icon="📝", layout="centered")

st.markdown("""
<style>
  #MainMenu, footer, header {visibility: hidden;}
  .block-container {max-width: 760px; padding-top: 3rem;}
  h1 {letter-spacing: -1px;}
  .stTextInput input {border-radius: 10px;}
  .stButton>button {border-radius: 10px; font-weight: 600;}
  .badge {display:inline-block; font-size:12px; font-weight:600; color:#4338ca;
          background:#eef2ff; padding:4px 12px; border-radius:20px; margin:6px 0 2px;}
</style>
""", unsafe_allow_html=True)

st.markdown("# Get any YouTube video's transcript <span style='color:#4f46e5'>in seconds</span>",
            unsafe_allow_html=True)
st.write("Paste a video link below. We detect the language spoken in the video "
         "and generate the transcript instantly — free, no sign-up.")

url = st.text_input("YouTube link", placeholder="https://www.youtube.com/watch?v=…",
                    label_visibility="collapsed")

if "meta" not in st.session_state:
    st.session_state.meta = None

if st.button("Generate transcript", type="primary", use_container_width=True):
    if not extract_video_id(url):
        st.error("Please paste a valid YouTube link.")
        st.session_state.meta = None
    else:
        with st.spinner("Reading video info…"):
            try:
                title, langs = cached_languages(url)
            except Exception as e:
                st.error(friendly_error(e))
                st.session_state.meta = None
            else:
                st.session_state.meta = {"url": url, "title": title,
                                         "langs": langs,
                                         "detected": pick_detected(langs)}

meta = st.session_state.meta
if meta:
    st.subheader(meta["title"])
    st.markdown(f"<span class='badge'>Detected: {meta['detected']['name']}</span>",
                unsafe_allow_html=True)

    labels = [f"{l['name']}{' (auto)' if l['auto'] else ''}" for l in meta["langs"]]
    def_idx = next(i for i, l in enumerate(meta["langs"])
                   if l["code"] == meta["detected"]["code"]
                   and l["auto"] == meta["detected"]["auto"])
    sel = st.selectbox("Transcript language", labels, index=def_idx)
    lang = meta["langs"][labels.index(sel)]

    with st.spinner("Generating transcript…"):
        try:
            t = cached_transcript(meta["url"], lang["code"], lang["auto"])
        except Exception as e:
            st.error(friendly_error(e))
        else:
            st.code(t["text"], language=None)
            c1, c2 = st.columns(2)
            with c1:
                st.download_button("⬇ Download .txt", t["text"],
                                   file_name="transcript.txt",
                                   use_container_width=True)
            with c2:
                st.caption("Tip: hover the transcript box and use its copy button")
            st.caption(f"{len(t['segments'])} caption lines · {t['language']}")

st.markdown("---")
c1, c2, c3 = st.columns(3)
c1.markdown("**⚡ Instant**\n\nTranscripts in seconds.")
c2.markdown("**🌍 Auto language**\n\nDetected from the video itself.")
c3.markdown("**📋 One-click copy**\n\nCopy or download as .txt.")
st.caption("Made with ♥ by Transcriptly · Free forever · Works with captioned YouTube videos")
