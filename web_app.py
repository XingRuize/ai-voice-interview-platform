import csv
import json
import mimetypes
import os
import re
import shutil
import subprocess
import threading
import uuid
import wave
import webbrowser
from datetime import datetime
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

import requests


BASE_DIR = Path(__file__).resolve().parent
PUBLIC_DIR = BASE_DIR / "public"
DATA_DIR = BASE_DIR / "data"
SETTINGS = json.loads((BASE_DIR / "settings.json").read_text(encoding="utf-8"))
QUESTIONS = json.loads((BASE_DIR / "questions.json").read_text(encoding="utf-8"))["questions"]
HOST = SETTINGS.get("web_host", "127.0.0.1")
PORT = int(SETTINGS.get("web_port", 8765))
ADMIN_PIN = str(SETTINGS.get("admin_pin", "2468"))
MAX_BODY = 80 * 1024 * 1024

RISK = re.compile(r"自杀|不想活|结束生命|伤害自己|活不下去|伤害他人")
UNCLEAR = re.compile(r"不知道|说不清|忘了|没什么|还好|不方便说")
AFFECT = re.compile(r"紧张|焦虑|害怕|尴尬|难受|压力|生气|不舒服")
COMMUNICATION = re.compile(r"说不出|表达|解释|没听懂|不理解|打断|称呼|误解")


def now_iso():
    return datetime.now().astimezone().isoformat(timespec="seconds")


def safe_part(value):
    cleaned = re.sub(r"[^0-9A-Za-z_-]+", "_", str(value).strip())
    return cleaned[:60] or "anonymous"


def session_path(session_id):
    return DATA_DIR / safe_part(session_id)


def load_session(session_id):
    path = session_path(session_id) / "interview.json"
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def save_session(session):
    folder = session_path(session["session_id"])
    folder.mkdir(parents=True, exist_ok=True)
    session["updated_at"] = now_iso()
    (folder / "interview.json").write_text(
        json.dumps(session, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    records = session.get("responses", [])
    if records:
        fields = [
            "participant_id", "session_id", "turn_id", "question_id", "section",
            "is_followup", "question_text", "decision_reason", "audio_file",
            "audio_duration_seconds", "asr_text", "corrected_text", "created_at",
        ]
        with (folder / "interview.csv").open("w", encoding="utf-8-sig", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
            writer.writeheader()
            writer.writerows(records)


def current_question(session):
    pending = session.get("pending", [])
    if pending:
        q = dict(pending[0])
        q["is_followup"] = True
        return q
    index = int(session.get("core_index", 0))
    if index >= len(QUESTIONS):
        return None
    q = dict(QUESTIONS[index])
    q["is_followup"] = False
    return q


def public_session(session):
    question = current_question(session)
    return {
        "session_id": session["session_id"],
        "participant_id": session["participant_id"],
        "created_at": session["created_at"],
        "completed": session.get("completed", False),
        "safety_paused": session.get("safety_paused", False),
        "core_index": session.get("core_index", 0),
        "total_core_questions": len(QUESTIONS),
        "response_count": len(session.get("responses", [])),
        "question": question,
        "responses": session.get("responses", []),
    }


def choose_followup(session, answer, answered_question):
    if not answered_question.get("allow_followup", True):
        return None
    if session.get("followups_for_core", 0) >= int(
        SETTINGS.get("max_followups_per_core_question", 2)
    ):
        return None
    compact = re.sub(r"\s+", "", answer)
    text = reason = kind = None
    if len(compact) < 10 or UNCLEAR.search(answer):
        text = "如果你愿意，可以选一个具体时刻，讲讲当时发生了什么。"
        reason = "回答较短或不明确，需要非诱导性澄清"
        kind = "CLARIFY"
    elif COMMUNICATION.search(answer):
        text = "这件事具体怎样影响了你继续向医务人员说明自己的情况？"
        reason = "回答提到沟通困难，追问表达影响"
        kind = "COMMUNICATION"
    elif AFFECT.search(answer):
        text = "你注意到这种感受对你的说话方式产生了什么变化吗？"
        reason = "回答提到压力感受，追问表达变化"
        kind = "EMOTION"
    if not text:
        return None
    session["followups_for_core"] = session.get("followups_for_core", 0) + 1
    parent = answered_question["id"].split("_followup")[0]
    return {
        "id": f"{parent}_followup{session['followups_for_core']}",
        "section": answered_question["section"],
        "text": text,
        "allow_followup": True,
        "decision_reason": reason,
        "followup_type": kind,
    }


def advance_session(session, corrected_text):
    question = current_question(session)
    if not question:
        session["completed"] = True
        return
    if question.get("is_followup"):
        session["pending"].pop(0)
    else:
        session["core_index"] = int(session.get("core_index", 0)) + 1

    pending_audio = session.pop("pending_audio", {})
    record = {
        "participant_id": session["participant_id"],
        "session_id": session["session_id"],
        "turn_id": len(session.get("responses", [])) + 1,
        "question_id": question["id"],
        "section": question["section"],
        "is_followup": bool(question.get("is_followup")),
        "question_text": question["text"],
        "decision_reason": question.get("decision_reason", "核心题库"),
        "audio_file": pending_audio.get("audio_file", ""),
        "audio_duration_seconds": pending_audio.get("audio_duration_seconds", ""),
        "asr_text": pending_audio.get("asr_text", ""),
        "corrected_text": corrected_text.strip(),
        "created_at": now_iso(),
    }
    session.setdefault("responses", []).append(record)

    if RISK.search(corrected_text):
        session["safety_paused"] = True
        session["completed"] = True
        session["pending"] = []
        return

    followup = choose_followup(session, corrected_text, question)
    if followup:
        session.setdefault("pending", []).append(followup)
    elif not session.get("pending"):
        session["followups_for_core"] = 0
    if current_question(session) is None:
        session["completed"] = True


class SpeechServices:
    def __init__(self):
        self.asr_url = SETTINGS["asr_url"].rstrip("/")
        self.tts_url = SETTINGS["tts_url"].rstrip("/")
        self.timeout = int(SETTINGS.get("service_timeout_seconds", 180))

    def health(self):
        result = {}
        for name, url in (("asr", self.asr_url), ("tts", self.tts_url)):
            try:
                result[name] = requests.get(url + "/docs", timeout=3).ok
            except requests.RequestException:
                result[name] = False
        return result

    def transcribe(self, wav_path):
        with wav_path.open("rb") as f:
            response = requests.post(
                self.asr_url + "/v1/audio/transcriptions",
                files={"file": (wav_path.name, f, "audio/wav")},
                data={"model": SETTINGS.get("asr_model", "sensevoice")},
                timeout=self.timeout,
            )
        response.raise_for_status()
        payload = response.json()
        value = payload.get("text") or payload.get("transcript") or ""
        if isinstance(value, list):
            value = "".join(str(item) for item in value)
        return str(value).strip()

    def synthesize(self, text, output_path):
        response = requests.post(
            self.tts_url + "/inference_sft",
            data={"tts_text": text, "spk_id": SETTINGS.get("tts_speaker", "中文女")},
            timeout=self.timeout,
        )
        response.raise_for_status()
        with wave.open(str(output_path), "wb") as wav_file:
            wav_file.setnchannels(1)
            wav_file.setsampwidth(2)
            wav_file.setframerate(int(SETTINGS.get("tts_sample_rate", 22050)))
            wav_file.writeframes(response.content)


SPEECH = SpeechServices()
LOCK = threading.RLock()


class AppHandler(BaseHTTPRequestHandler):
    server_version = "ClinicalInterview/1.0"

    def log_message(self, fmt, *args):
        print(f"[{self.log_date_time_string()}] {fmt % args}")

    def send_json(self, payload, status=200):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def read_body(self):
        size = int(self.headers.get("Content-Length", "0"))
        if size > MAX_BODY:
            raise ValueError("上传文件超过80MB限制")
        return self.rfile.read(size)

    def read_json(self):
        raw = self.read_body()
        return json.loads(raw.decode("utf-8")) if raw else {}

    def serve_file(self, path, download_name=None):
        if not path.exists() or not path.is_file():
            return self.send_error(404)
        mime = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        self.send_response(200)
        self.send_header("Content-Type", mime)
        self.send_header("Content-Length", str(path.stat().st_size))
        if download_name:
            self.send_header(
                "Content-Disposition", f'attachment; filename="{download_name}"'
            )
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        with path.open("rb") as f:
            shutil.copyfileobj(f, self.wfile)

    def require_pin(self, query):
        return parse_qs(query).get("pin", [""])[0] == ADMIN_PIN

    def do_GET(self):
        parsed = urlparse(self.path)
        path = unquote(parsed.path)
        try:
            if path == "/api/health":
                return self.send_json({"ok": True, "services": SPEECH.health()})
            if path.startswith("/api/sessions/") and path.endswith("/question/audio"):
                session_id = path.split("/")[3]
                return self.get_question_audio(session_id)
            if path.startswith("/api/sessions/"):
                session_id = path.split("/")[3]
                session = load_session(session_id)
                if not session:
                    return self.send_json({"error": "访谈不存在"}, 404)
                return self.send_json(public_session(session))
            if path == "/api/admin/sessions":
                if not self.require_pin(parsed.query):
                    return self.send_json({"error": "管理员PIN错误"}, 403)
                return self.send_json({"sessions": self.list_sessions()})
            if path.startswith("/api/admin/session/"):
                if not self.require_pin(parsed.query):
                    return self.send_json({"error": "管理员PIN错误"}, 403)
                session_id = path.split("/")[4]
                session = load_session(session_id)
                return self.send_json(session or {"error": "访谈不存在"}, 200 if session else 404)
            if path.startswith("/api/admin/export/"):
                if not self.require_pin(parsed.query):
                    return self.send_json({"error": "管理员PIN错误"}, 403)
                session_id = Path(path).stem
                export = session_path(session_id) / "interview.csv"
                return self.serve_file(export, f"{session_id}.csv")
            if path.startswith("/api/admin/audio/"):
                if not self.require_pin(parsed.query):
                    return self.send_json({"error": "管理员PIN错误"}, 403)
                parts = path.split("/")
                if len(parts) < 6:
                    return self.send_error(404)
                session_id = parts[4]
                filename = Path(parts[5]).name
                target = (session_path(session_id) / filename).resolve()
                if session_path(session_id).resolve() not in target.parents:
                    return self.send_error(403)
                return self.serve_file(target)
            return self.serve_static(path)
        except Exception as exc:
            self.send_json({"error": str(exc)}, 500)

    def do_POST(self):
        parsed = urlparse(self.path)
        path = unquote(parsed.path)
        try:
            if path == "/api/sessions":
                return self.create_session(self.read_json())
            if path.startswith("/api/sessions/") and path.endswith("/audio"):
                session_id = path.split("/")[3]
                return self.upload_audio(session_id, self.read_body(), self.headers.get("Content-Type", ""))
            if path.startswith("/api/sessions/") and path.endswith("/answer"):
                session_id = path.split("/")[3]
                return self.submit_answer(session_id, self.read_json())
            return self.send_error(404)
        except ValueError as exc:
            self.send_json({"error": str(exc)}, 400)
        except requests.RequestException as exc:
            self.send_json({"error": f"本地语音模型调用失败：{exc}"}, 503)
        except Exception as exc:
            self.send_json({"error": str(exc)}, 500)

    def create_session(self, payload):
        participant_id = safe_part(payload.get("participant_id", ""))
        if not payload.get("consent"):
            raise ValueError("请先确认知情说明")
        session_id = datetime.now().strftime("%Y%m%d_%H%M%S_") + uuid.uuid4().hex[:8]
        session = {
            "schema_version": "web-1.0",
            "session_id": session_id,
            "participant_id": participant_id,
            "created_at": now_iso(),
            "updated_at": now_iso(),
            "consent_confirmed": True,
            "core_index": 0,
            "pending": [],
            "followups_for_core": 0,
            "responses": [],
            "completed": False,
            "safety_paused": False,
            "question_bank_version": "prototype-0.1",
        }
        with LOCK:
            save_session(session)
        self.send_json(public_session(session), 201)

    def get_question_audio(self, session_id):
        session = load_session(session_id)
        if not session:
            return self.send_error(404)
        question = current_question(session)
        if not question:
            return self.send_error(404)
        filename = f"question_{safe_part(question['id'])}.wav"
        path = session_path(session_id) / filename
        if not path.exists():
            SPEECH.synthesize(question["text"], path)
        self.serve_file(path)

    def upload_audio(self, session_id, body, content_type):
        session = load_session(session_id)
        if not session or session.get("completed"):
            raise ValueError("访谈不存在或已经结束")
        if not body:
            raise ValueError("没有收到录音")
        turn = len(session.get("responses", [])) + 1
        ext = ".webm" if "webm" in content_type else ".ogg" if "ogg" in content_type else ".bin"
        folder = session_path(session_id)
        source = folder / f"answer_{turn:03d}{ext}"
        wav_path = folder / f"answer_{turn:03d}.wav"
        source.write_bytes(body)
        command = [
            "ffmpeg", "-y", "-i", str(source), "-ac", "1", "-ar",
            str(SETTINGS.get("sample_rate", 16000)), "-c:a", "pcm_s16le", str(wav_path),
        ]
        completed = subprocess.run(command, capture_output=True, text=True, timeout=120)
        if completed.returncode != 0:
            raise RuntimeError("录音格式转换失败，请确认已安装FFmpeg")
        asr_text = SPEECH.transcribe(wav_path)
        with wave.open(str(wav_path), "rb") as wav_file:
            duration = wav_file.getnframes() / float(wav_file.getframerate())
        session["pending_audio"] = {
            "audio_file": wav_path.name,
            "source_audio_file": source.name,
            "audio_duration_seconds": round(duration, 3),
            "asr_text": asr_text,
        }
        with LOCK:
            save_session(session)
        self.send_json({"asr_text": asr_text, "duration_seconds": round(duration, 3)})

    def submit_answer(self, session_id, payload):
        corrected = str(payload.get("corrected_text", "")).strip()
        if not corrected:
            raise ValueError("转写内容不能为空")
        session = load_session(session_id)
        if not session or session.get("completed"):
            raise ValueError("访谈不存在或已经结束")
        if not session.get("pending_audio"):
            raise ValueError("请先完成录音和自动转写")
        with LOCK:
            advance_session(session, corrected)
            save_session(session)
        self.send_json(public_session(session))

    def list_sessions(self):
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        rows = []
        for item in DATA_DIR.iterdir():
            if not item.is_dir() or not (item / "interview.json").exists():
                continue
            try:
                session = json.loads((item / "interview.json").read_text(encoding="utf-8"))
                if str(session.get("participant_id", "")).startswith("TEST_"):
                    continue
                rows.append({
                    "session_id": session["session_id"],
                    "participant_id": session.get("participant_id", ""),
                    "created_at": session.get("created_at", ""),
                    "updated_at": session.get("updated_at", ""),
                    "completed": session.get("completed", False),
                    "safety_paused": session.get("safety_paused", False),
                    "response_count": len(session.get("responses", [])),
                })
            except Exception:
                continue
        return sorted(rows, key=lambda row: row["created_at"], reverse=True)

    def serve_static(self, path):
        relative = "index.html" if path in ("/", "/admin") else path.lstrip("/")
        target = (PUBLIC_DIR / relative).resolve()
        if PUBLIC_DIR.resolve() not in target.parents and target != PUBLIC_DIR.resolve():
            return self.send_error(403)
        if not target.exists():
            target = PUBLIC_DIR / "index.html"
        self.serve_file(target)


def run():
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    address = f"http://{HOST}:{PORT}"
    server = ThreadingHTTPServer((HOST, PORT), AppHandler)
    print(f"AI语音研究访谈平台已启动：{address}")
    print("关闭此窗口可停止网页服务。")
    threading.Timer(1.0, lambda: webbrowser.open(address)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    run()
