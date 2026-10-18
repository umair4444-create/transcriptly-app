"""Transcriptly — YouTube transcript generator (Streamlit edition).

Paste a YouTube link → get the transcript instantly in the video's own language.
Fast & free. Three caption providers, tried in order:
  1. youtube-transcript-api (fast path)
  2. yt-dlp with the Android player client (resists YouTube datacenter blocks)
  3. Piped API (subtitles proxied through Piped's own servers)
"""
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request
import xml.etree.ElementTree as ET
from html import escape as html_escape

import streamlit as st
import streamlit.components.v1 as components
from youtube_transcript_api import YouTubeTranscriptApi
from youtube_transcript_api._errors import (
    NoTranscriptFound,
    TranscriptsDisabled,
    VideoUnavailable,
)

YTDLP_TIMEOUT = 60
HTTP_TIMEOUT = 15

# Bump this whenever the fetch/detect logic changes. Streamlit's
# st.cache_data lives in server memory and can survive a code redeploy,
# so without a version in the key users would keep getting stale results.
CACHE_VERSION = "v5"


def _retry(fn, tries=3, delay=2.0, skip=()):
    """Retry a fetch on transient failures (YouTube's blocking is often
    per-request flaky). Exceptions in `skip` (definitive answers like
    'video has no captions') are never retried."""
    last_exc = None
    for attempt in range(tries):
        try:
            return fn()
        except skip:
            raise
        except Exception as e:
            last_exc = e
            if attempt < tries - 1:
                time.sleep(delay)
    raise last_exc

VIDEO_ID_RE = re.compile(
    r"(?:youtube\.com/(?:watch\?.*v=|shorts/|embed/|live/|v/)|youtu\.be/)([A-Za-z0-9_-]{11})"
)
BARE_ID_RE = re.compile(r"^[A-Za-z0-9_-]{11}$")

PIPED_INSTANCES = [
    "https://pipedapi.kavin.rocks",
    "https://pipedapi.adminforge.de",
    "https://pipedapi.reallyaweso.me",
    "https://api.piped.private.coffee",
    "https://pipedapi.leptons.xyz",
    "https://pipedapi-libre.kavin.rocks",
]


class NoCaptions(Exception):
    """The video genuinely has no captions/subtitles."""


class VideoGone(Exception):
    """The video is unavailable, private or deleted."""


class Blocked(Exception):
    """YouTube (or every fallback) is blocking our server right now."""


def extract_video_id(url: str) -> str | None:
    url = (url or "").strip()
    m = VIDEO_ID_RE.search(url)
    if m:
        return m.group(1)
    if BARE_ID_RE.match(url):
        return url
    return None


def http_get(url: str, timeout: int = HTTP_TIMEOUT) -> bytes:
    req = urllib.request.Request(
        url,
        headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
                 "Accept": "*/*"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def get_title(video_id: str) -> str:
    try:
        data = http_get(
            f"https://www.youtube.com/oembed?url="
            f"https://www.youtube.com/watch?v={video_id}&format=json",
            timeout=10,
        )
        return json.loads(data.decode()).get("title", video_id)
    except Exception:
        return video_id


# ---------------------------------------------------------------- yt-dlp
def run_ytdlp(args: list[str]) -> subprocess.CompletedProcess:
    cmd = [sys.executable, "-m", "yt_dlp", "--no-warnings", "--no-playlist",
           "--socket-timeout", "20",
           "--extractor-args", "youtube:player_client=android,web"]
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
        if "not a bot" in err or "Sign in to confirm" in err:
            raise Blocked("yt-dlp hit a YouTube bot-check")
        raise Blocked(err[-200:] or "yt-dlp failed")
    try:
        return json.loads(proc.stdout)
    except Exception:
        raise Blocked("yt-dlp returned invalid data")


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


# ---------------------------------------------------------------- parsing
VTT_TS = re.compile(
    r"(\d{2}):(\d{2}):(\d{2})\.(\d{3})\s*-->\s*(\d{2}):(\d{2}):(\d{2})\.(\d{3})"
)
TAG_RE = re.compile(r"<[^>]+>")


def _ts_to_sec(h, m, s, ms) -> float:
    return int(h) * 3600 + int(m) * 60 + int(s) + int(ms) / 1000


def parse_vtt_text(content: str) -> list[dict]:
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


def _parse_clock(t: str) -> float | None:
    """Parse '1.23' seconds or 'HH:MM:SS.mmm' into seconds."""
    t = (t or "").strip().replace(",", ".")
    if not t:
        return None
    if ":" in t:
        parts = t.split(":")
        try:
            secs = float(parts[-1])
            mins = int(parts[-2]) if len(parts) > 1 else 0
            hrs = int(parts[-3]) if len(parts) > 2 else 0
            return hrs * 3600 + mins * 60 + secs
        except ValueError:
            return None
    try:
        return float(t)
    except ValueError:
        return None


def parse_xml_captions(content: str) -> list[dict]:
    root = ET.fromstring(content)
    segments: list[dict] = []
    for el in root.iter():
        tag = el.tag.split("}")[-1]
        if tag not in ("text", "p"):
            continue
        text = re.sub(r"\s+", " ", "".join(el.itertext())).strip()
        if not text:
            continue
        start = _parse_clock(el.get("start") or el.get("begin") or "")
        dur = _parse_clock(el.get("dur") or "")
        end = _parse_clock(el.get("end") or "")
        if start is None:
            continue
        if dur is None and end is not None:
            dur = max(0.0, end - start)
        segments.append({"start": round(start, 2),
                         "duration": round(dur if dur else 2.0, 2),
                         "text": text})
    segments.sort(key=lambda s: s["start"])
    deduped = [s for i, s in enumerate(segments)
               if i == 0 or s["text"] != segments[i - 1]["text"]]
    return deduped


def parse_caption_file(content: bytes) -> list[dict]:
    text = content.decode("utf-8", errors="replace").lstrip("\ufeff").strip()
    if text.startswith("<"):
        try:
            return parse_xml_captions(text)
        except ET.ParseError:
            pass
    return parse_vtt_text(text)


# ---------------------------------------------------------------- providers
def pick_detected(langs: list[dict]) -> dict | None:
    manual = [l for l in langs if not l["auto"]]
    if manual:
        return manual[0]
    auto = [l for l in langs if l["auto"]]
    # Prefer English auto-captions when available (most reliable track;
    # YouTube often mislabels the ASR language on Shorts)
    for l in auto:
        if l["code"].lower().startswith("en"):
            return l
    return auto[0] if auto else None


def english_auto_fallback(langs: list[dict], exclude_code: str) -> dict | None:
    """Return an English auto-caption track different from exclude_code, if any."""
    return next(
        (l for l in langs
         if l["auto"] and l["code"].lower().startswith("en")
         and l["code"] != exclude_code),
        None,
    )


def match_lang(langs: list[dict], code: str | None) -> dict | None:
    if not code:
        return pick_detected(langs)
    for l in langs:  # exact match first
        if l["code"].lower() == code.lower():
            return l
    for l in langs:  # prefix match (en <-> en-US)
        a, b = l["code"].lower(), code.lower()
        if a.startswith(b) or b.startswith(a):
            return l
    return None


# ---- Tier 1: youtube-transcript-api
def tier1_languages(video_id: str) -> tuple[str, list[dict]]:
    try:
        ytt = YouTubeTranscriptApi()
        tracks = ytt.list(video_id)
    except (TranscriptsDisabled, NoTranscriptFound):
        raise NoCaptions()
    except VideoUnavailable:
        raise VideoGone()
    langs = [{"code": t.language_code, "name": t.language,
              "auto": bool(t.is_generated)} for t in tracks]
    if not langs:
        raise NoCaptions()
    return get_title(video_id), langs


def tier1_transcript(video_id: str, lang: str | None) -> dict:
    try:
        ytt = YouTubeTranscriptApi()
        fetched = ytt.fetch(video_id) if not lang else ytt.fetch(
            video_id, languages=[lang])
    except (TranscriptsDisabled, NoTranscriptFound):
        raise NoCaptions()
    except VideoUnavailable:
        raise VideoGone()
    segments = [{"start": round(s.start, 2), "duration": round(s.duration, 2),
                 "text": s.text} for s in fetched.snippets]
    if not segments:
        raise NoCaptions()
    return {"language": fetched.language, "language_code": fetched.language_code,
            "segments": segments,
            "text": "\n".join(s["text"] for s in fetched.snippets)}


# ---- Tier 2: yt-dlp (Android player client)
def tier2_languages(video_id: str) -> tuple[str, list[dict]]:
    info = ytdlp_info(video_id)  # raises Blocked on failure
    langs = ytdlp_languages(info)
    if not langs:
        raise NoCaptions()
    return info.get("title") or video_id, langs


def tier2_transcript(video_id: str, lang: str | None, auto: bool) -> dict:
    info = ytdlp_info(video_id)
    langs = ytdlp_languages(info)
    target = match_lang(langs, lang)
    if not target:
        raise NoCaptions()
    if auto and target["auto"] is False and any(l["auto"] for l in langs):
        # requested auto track; keep the matched manual one only if no auto exists
        pass
    tmpdir = tempfile.mkdtemp(prefix="tsub_")
    try:
        proc = run_ytdlp([
            "--skip-download",
            "--write-auto-sub" if target["auto"] else "--write-sub",
            "--sub-langs", target["code"], "--sub-format", "vtt",
            "--output", os.path.join(tmpdir, "cap.%(ext)s"),
            f"https://www.youtube.com/watch?v={video_id}",
        ])
        if proc.returncode != 0 and ("not a bot" in (proc.stderr or "")
                                    or "Sign in to confirm" in (proc.stderr or "")):
            raise Blocked("yt-dlp hit a YouTube bot-check")
        vtt_files = [f for f in os.listdir(tmpdir) if f.endswith(".vtt")]
        if not vtt_files:
            raise NoCaptions()
        with open(os.path.join(tmpdir, vtt_files[0]), "rb") as f:
            segments = parse_caption_file(f.read())
        if not segments:
            raise NoCaptions()
        return {"language": target["name"], "language_code": target["code"],
                "segments": segments,
                "text": "\n".join(s["text"] for s in segments)}
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


# ---- Tier 3: Piped API (proxied subtitles, different network path)
def piped_streams(video_id: str) -> dict:
    errors = []
    for base in PIPED_INSTANCES:
        try:
            data = json.loads(http_get(f"{base}/streams/{video_id}"))
            if isinstance(data, dict) and "title" in data:
                return data
        except Exception as e:
            errors.append(f"{base}: {e}")
    raise Blocked("Piped backends unreachable: " + "; ".join(errors[-2:]))


def piped_languages(data: dict) -> list[dict]:
    subs = data.get("subtitles") or []
    return [{"code": s.get("code") or "und",
             "name": s.get("name") or s.get("code") or "und",
             "auto": bool(s.get("autoGenerated")),
             "url": s.get("url")} for s in subs if s.get("url")]


def tier3_languages(video_id: str) -> tuple[str, list[dict]]:
    data = piped_streams(video_id)
    langs = piped_languages(data)
    if not langs:
        raise NoCaptions()
    return data.get("title") or video_id, langs


def tier3_transcript(video_id: str, lang: str | None) -> dict:
    # Try every Piped instance: each serves subtitle files through its own
    # proxy host, so a dead proxy on one instance doesn't kill the tier.
    saw_valid_response = False
    for base in PIPED_INSTANCES:
        try:
            data = json.loads(http_get(f"{base}/streams/{video_id}"))
        except Exception:
            continue
        if not isinstance(data, dict) or "title" not in data:
            continue
        saw_valid_response = True
        target = match_lang(piped_languages(data), lang)
        if not target:
            continue
        try:
            segments = parse_caption_file(http_get(target["url"], timeout=20))
        except Exception:
            continue  # this instance's subtitle proxy is down; try next
        if not segments:
            continue
        return {"language": target["name"], "language_code": target["code"],
                "segments": segments,
                "text": "\n".join(s["text"] for s in segments)}
    if saw_valid_response:
        raise NoCaptions()
    raise Blocked("all Piped backends unreachable")


# ---------------------------------------------------------------- orchestration
@st.cache_data(ttl=3600, show_spinner=False)
def cached_languages(version: str, video_id: str) -> tuple[str, list[dict]]:
    # Tier 1 (fast library; YouTube's blocking is per-request flaky, retried)
    try:
        return _retry(lambda: tier1_languages(video_id), tries=2,
                      skip=(NoCaptions, VideoGone))
    except (NoCaptions, VideoGone):
        raise
    except Exception:
        pass
    # Tier 2 (yt-dlp workhorse; most likely to succeed, gets the most chances)
    try:
        return _retry(lambda: tier2_languages(video_id), tries=3, delay=3.0,
                      skip=(NoCaptions, VideoGone))
    except (NoCaptions, VideoGone):
        raise
    except Exception:
        pass
    # Tier 3 (Piped proxies; their own instance loop is the retry)
    try:
        return tier3_languages(video_id)
    except (NoCaptions, VideoGone):
        raise
    except Exception as e:
        raise Blocked(f"all providers failed ({e})")


@st.cache_data(ttl=3600, show_spinner=False)
def cached_transcript(version: str, video_id: str, lang: str | None,
                      auto: bool) -> dict:
    # Tier 1
    try:
        return _retry(lambda: tier1_transcript(video_id, lang), tries=2,
                      skip=(NoCaptions, VideoGone))
    except (NoCaptions, VideoGone):
        raise
    except Exception:
        pass
    # Tier 2
    try:
        return _retry(lambda: tier2_transcript(video_id, lang, auto), tries=3,
                      delay=3.0, skip=(NoCaptions, VideoGone))
    except (NoCaptions, VideoGone):
        raise
    except Exception:
        pass
    # Tier 3
    try:
        return tier3_transcript(video_id, lang)
    except (NoCaptions, VideoGone):
        raise
    except Exception as e:
        raise Blocked(f"all providers failed ({e})")


def friendly_error(e: Exception) -> str:
    if isinstance(e, NoCaptions):
        return ("No captions found for this video. "
                "Try another video that has subtitles / closed captions.")
    if isinstance(e, VideoGone):
        return "Video not found — it may be private or deleted."
    if isinstance(e, Blocked):
        return ("YouTube is blocking requests from our server right now. "
                "Please wait a couple of minutes and try again.")
    return f"Could not fetch captions: {str(e)[-200:]}"


# ------------------------------------------------------------------ UI
def transcript_div_html(text: str) -> str:
    """Transcript in a self-styled box: guaranteed word-wrap (portrait
    reading, no sideways scrolling) via inline styles."""
    escaped = html_escape(text)
    return (
        '<div id="transcript-box" style="'
        "background: rgba(127,127,127,.10);"
        "border: 1px solid rgba(127,127,127,.30);"
        "border-radius: 10px;"
        "padding: 14px 16px;"
        "font-family: ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;"
        "font-size: 14px;"
        "line-height: 1.75;"
        "white-space: pre-wrap;"
        "overflow-wrap: break-word;"
        "word-break: break-word;"
        "max-height: 55vh;"
        'overflow-y: auto;">'
        f"{escaped}</div>"
    )


def copy_button_html(text: str) -> str:
    """Big one-tap copy button. Rendered via components.html so the
    JavaScript actually runs (st.markdown strips event handlers)."""
    payload = json.dumps(text)
    return (
        '<button id="cpbtn" style="display:block;width:100%;padding:13px;'
        "border:none;border-radius:10px;background:#ff2b2b;color:#fff;"
        'font-size:16px;font-weight:700;cursor:pointer;">'
        "\U0001F4CB Copy Transcript</button>"
        "<script>"
        f"var TEXT = {payload};"
        "document.getElementById('cpbtn').addEventListener('click', function() {"
        "  var b = this;"
        "  function ok() { b.textContent = '\u2713 Copied!';"
        "    setTimeout(function(){ b.textContent = '\U0001F4CB Copy Transcript'; }, 2000); }"
        "  function legacy() {"
        "    var ta = document.createElement('textarea');"
        "    ta.value = TEXT; ta.setAttribute('readonly','');"
        "    ta.style.position='fixed'; ta.style.top='0'; ta.style.opacity='0';"
        "    document.body.appendChild(ta); ta.select();"
        "    try { document.execCommand('copy'); ok(); }"
        "    catch(e) { b.textContent = '\u26A0 Long-press to copy'; }"
        "    document.body.removeChild(ta);"
        "  }"
        "  if (navigator.clipboard && navigator.clipboard.writeText) {"
        "    navigator.clipboard.writeText(TEXT).then(ok, function(){ legacy(); });"
        "  } else { legacy(); }"
        "});"
        "</script>"
    )


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
  /* transcript wraps instead of scrolling sideways (portrait reading) */
  pre {white-space: pre-wrap !important; overflow-wrap: break-word !important;}
</style>
""", unsafe_allow_html=True)

st.markdown("# Get any YouTube video's transcript <span style='color:#4f46e5'>in seconds</span>",
            unsafe_allow_html=True)
st.markdown("<p style='color:#9ca3af; font-size:14px; margin-top:-12px;'>by <b>M Umair</b></p>",
            unsafe_allow_html=True)
st.write("Paste a video link below. We detect the language spoken in the video "
         "and generate the transcript instantly — free, no sign-up.")

url = st.text_input("YouTube link", placeholder="https://www.youtube.com/watch?v=…",
                    label_visibility="collapsed")

if "meta" not in st.session_state:
    st.session_state.meta = None
if "retry_pending" not in st.session_state:
    st.session_state.retry_pending = None


def do_generate(target_url):
    """Run the caption-listing fetch. On a Blocked error the URL is stashed
    in retry_pending so the UI can offer a one-tap 'Try again'."""
    vid = extract_video_id(target_url)
    if not vid:
        st.error("Please paste a valid YouTube link.")
        st.session_state.meta = None
        return
    with st.spinner("Reading video info…"):
        try:
            title, langs = cached_languages(CACHE_VERSION, vid)
        except Blocked:
            st.session_state.meta = None
            st.session_state.retry_pending = target_url
            return
        except Exception as e:
            st.error(friendly_error(e))
            st.session_state.meta = None
            return
        detected = pick_detected(langs)
        if not detected:
            st.error(friendly_error(NoCaptions()))
            st.session_state.meta = None
            return
        st.session_state.meta = {"url": target_url, "title": title,
                                 "langs": langs, "detected": detected}
        st.session_state.retry_pending = None


if st.button("Generate transcript", type="primary", use_container_width=True):
    do_generate(url)

if st.session_state.retry_pending:
    st.error("⏳ YouTube is temporarily blocking our server. "
             "This usually clears in a minute or two.")
    if st.button("🔄 Try again"):
        retry_url = st.session_state.retry_pending
        st.session_state.retry_pending = None
        do_generate(retry_url)

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
        t = None
        try:
            t = cached_transcript(CACHE_VERSION, extract_video_id(meta["url"]),
                                lang["code"], lang["auto"])
        except NoCaptions:
            # The chosen language had no downloadable captions — fall back
            # to English auto-captions when available.
            fb = english_auto_fallback(meta["langs"], lang["code"])
            if fb is not None:
                try:
                    t = cached_transcript(CACHE_VERSION,
                                        extract_video_id(meta["url"]),
                                        fb["code"], True)
                except Exception:
                    t = None
                if t is not None:
                    st.info("The selected language had no downloadable captions — "
                            "showing English (auto-generated) instead.")
            if t is None:
                st.error(friendly_error(NoCaptions()))
        except Blocked:
            st.error("⏳ YouTube is temporarily blocking our server. "
                     "This usually clears in a minute or two.")
            if st.button("🔄 Try again", key="retry_transcript"):
                st.rerun()
        except Exception as e:
            st.error(friendly_error(e))
        if t is not None:
            st.markdown(transcript_div_html(t["text"]), unsafe_allow_html=True)
            components.html(copy_button_html(t["text"]), height=70)
            st.download_button("⬇ Download .txt", t["text"],
                               file_name="transcript.txt",
                               use_container_width=True)
            st.caption(f"{len(t['segments'])} caption lines · {t['language']}")

st.markdown("---")
c1, c2, c3 = st.columns(3)
c1.markdown("**⚡ Instant**\n\nTranscripts in seconds.")
c2.markdown("**🌍 Auto language**\n\nDetected from the video itself.")
c3.markdown("**📋 One-click copy**\n\nCopy or download as .txt.")
st.caption("Made with ♥ by M Umair · Free forever · Works with captioned YouTube videos")
