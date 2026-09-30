// ИИ-01: голосовой ввод. Запись — в браузере (MediaRecorder), распознавание — локальной
// моделью на сервере ИИ (Р-3). Браузерное распознавание (Web Speech API) не используется:
// оно отправляет звук Google или Apple. Запись здесь же переводится в WAV 16 кГц моно
// (Web Audio) — серверу не нужен ffmpeg, а размер минуты записи — меньше 2 МБ. Звук не
// хранится ни в браузере, ни на сервере; распознанный текст встаёт в поле ввода для правки,
// сам вопрос не отправляется.
import React, { useEffect, useRef, useState } from "react";
import { Loader2, Mic, Square } from "lucide-react";

const TARGET_RATE = 16000;
const MIME_TYPES = ["audio/webm;codecs=opus", "audio/mp4", "audio/webm", "audio/ogg;codecs=opus"];

export function micProblem(error) {
  const name = error?.name || "";
  if (name === "NotAllowedError" || name === "SecurityError") {
    return "Нет доступа к микрофону. Разрешите его для этого сайта: в Safari — «Настройки сайта» → «Микрофон», "
      + "в Chrome — значок замка в адресной строке → «Микрофон».";
  }
  if (name === "NotFoundError" || name === "OverconstrainedError") return "Микрофон не найден — подключите его или выберите в настройках системы.";
  if (name === "NotReadableError") return "Микрофон занят другим приложением — закройте его и попробуйте снова.";
  return "Не удалось включить микрофон — попробуйте ещё раз.";
}

export function voiceSupport() {
  if (typeof window === "undefined") return "Браузер не поддерживает запись звука.";
  if (!window.isSecureContext) return "Микрофон работает только по защищённому адресу (https).";
  if (!navigator.mediaDevices?.getUserMedia || typeof window.MediaRecorder === "undefined") {
    return "Браузер не умеет записывать звук — обновите его или откройте приложение в Safari или Chrome.";
  }
  return "";
}

// Запись любого формата браузера → WAV PCM 16 бит, 16 кГц, моно.
export async function toWav16k(blob) {
  const Context = window.AudioContext || window.webkitAudioContext;
  const context = new Context();
  let decoded;
  try {
    decoded = await context.decodeAudioData(await blob.arrayBuffer());
  } finally {
    context.close?.();
  }
  const frames = Math.max(1, Math.round(decoded.duration * TARGET_RATE));
  const Offline = window.OfflineAudioContext || window.webkitOfflineAudioContext;
  const offline = new Offline(1, frames, TARGET_RATE);
  const source = offline.createBufferSource();
  source.buffer = decoded;
  source.connect(offline.destination);
  source.start(0);
  const rendered = await offline.startRendering();
  const samples = rendered.getChannelData(0);
  const buffer = new ArrayBuffer(44 + samples.length * 2);
  const view = new DataView(buffer);
  const text = (offset, value) => { for (let i = 0; i < value.length; i += 1) view.setUint8(offset + i, value.charCodeAt(i)); };
  text(0, "RIFF"); view.setUint32(4, 36 + samples.length * 2, true); text(8, "WAVE");
  text(12, "fmt "); view.setUint32(16, 16, true); view.setUint16(20, 1, true); view.setUint16(22, 1, true);
  view.setUint32(24, TARGET_RATE, true); view.setUint32(28, TARGET_RATE * 2, true);
  view.setUint16(32, 2, true); view.setUint16(34, 16, true);
  text(36, "data"); view.setUint32(40, samples.length * 2, true);
  for (let i = 0; i < samples.length; i += 1) {
    const value = Math.max(-1, Math.min(1, samples[i]));
    view.setInt16(44 + i * 2, value < 0 ? value * 0x8000 : value * 0x7fff, true);
  }
  return new Blob([buffer], { type: "audio/wav" });
}

// Состояние записи: idle → recording → busy → idle. send(wav) → { text, empty }.
// unavailable — движок распознавания не установлен: кнопка вместо записи показывает, как его поставить.
export function useVoiceInput({ maxSeconds = 60, send, onText, unavailable = "" }) {
  const [state, setState] = useState("idle");
  const [seconds, setSeconds] = useState(0);
  const [message, setMessage] = useState(null);
  const session = useRef(null);
  const handlers = useRef({ send, onText });
  handlers.current = { send, onText };

  function release(current) {
    if (!current) return;
    clearInterval(current.timer);
    current.stream?.getTracks().forEach((track) => track.stop());
  }

  function stop() {
    const current = session.current;
    if (!current) return;
    clearInterval(current.timer);
    if (current.recorder.state !== "inactive") current.recorder.stop();
  }

  function cancel() {
    if (session.current) session.current.cancelled = true;
    stop();
  }

  async function start() {
    if (state !== "idle") return;
    setMessage(null);
    if (unavailable) {
      setMessage({ type: "info", text: unavailable });
      return;
    }
    const problem = voiceSupport();
    if (problem) {
      setMessage({ type: "error", text: problem });
      return;
    }
    let stream;
    try {
      stream = await navigator.mediaDevices.getUserMedia({ audio: { channelCount: 1, echoCancellation: true, noiseSuppression: true } });
    } catch (error) {
      setMessage({ type: "error", text: micProblem(error) });
      return;
    }
    const mimeType = MIME_TYPES.find((type) => window.MediaRecorder.isTypeSupported?.(type));
    let recorder;
    try {
      recorder = new window.MediaRecorder(stream, mimeType ? { mimeType } : undefined);
    } catch {
      stream.getTracks().forEach((track) => track.stop());
      setMessage({ type: "error", text: "Браузер не смог начать запись — попробуйте ещё раз." });
      return;
    }
    const chunks = [];
    const current = { recorder, stream, chunks, cancelled: false, started: Date.now(), timer: null };
    session.current = current;
    recorder.ondataavailable = (event) => { if (event.data?.size) chunks.push(event.data); };
    recorder.onstop = async () => {
      release(current);
      if (session.current === current) session.current = null;
      if (current.cancelled || !chunks.length) {
        setState("idle");
        if (!current.cancelled) setMessage({ type: "error", text: "Запись пустая — скажите вопрос ещё раз." });
        return;
      }
      setState("busy");
      try {
        const wav = await toWav16k(new Blob(chunks, { type: recorder.mimeType || mimeType || "audio/webm" }));
        const result = await handlers.current.send(wav);
        if (result?.text) {
          handlers.current.onText(result.text);
          setMessage(null);
        } else {
          setMessage({ type: "info", text: result?.empty || "Речь не распознана — скажите вопрос ещё раз." });
        }
      } catch (error) {
        setMessage({ type: "error", text: error?.message || "Не удалось распознать речь." });
      } finally {
        setState("idle");
      }
    };
    recorder.start(250);
    current.timer = setInterval(() => {
      const elapsed = (Date.now() - current.started) / 1000;
      setSeconds(elapsed);
      if (elapsed >= maxSeconds) stop();
    }, 200);
    setSeconds(0);
    setState("recording");
  }

  // Esc — отменить запись; уход с экрана — выключить микрофон.
  useEffect(() => {
    if (state !== "recording") return undefined;
    const onKey = (event) => { if (event.key === "Escape") { event.preventDefault(); cancel(); } };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [state]);
  useEffect(() => () => {
    const current = session.current;
    if (current) {
      current.cancelled = true;
      if (current.recorder.state !== "inactive") current.recorder.stop();
      release(current);
    }
  }, []);

  return { state, seconds, message, maxSeconds, start, stop, cancel, unavailable: Boolean(unavailable),
    dismiss: () => setMessage(null) };
}

function clock(value) {
  const whole = Math.floor(value);
  return `${Math.floor(whole / 60)}:${String(whole % 60).padStart(2, "0")}`;
}

export function AiVoiceButton({ voice, disabled = false }) {
  const recording = voice.state === "recording";
  const busy = voice.state === "busy";
  const label = recording ? "Закончить запись" : busy ? "Распознаю речь" : "Сказать вопрос голосом";
  return (
    <button
      type="button"
      className={`ai-voice${recording ? " recording" : ""}${busy ? " busy" : ""}${voice.unavailable ? " off" : ""}`}
      onClick={() => (recording ? voice.stop() : voice.start())}
      disabled={busy || (disabled && !recording)}
      aria-label={label}
      aria-pressed={recording}
      title={voice.unavailable ? "Голосовой ввод ещё не установлен на сервере ИИ — нажмите, чтобы узнать, как включить"
        : recording ? "Закончить запись — текст появится в поле, его можно поправить" : busy ? label
          : `Сказать вопрос голосом (до ${voice.maxSeconds} с). Текст появится в поле — отправите сами`}
    >
      {recording ? <Square size={13} fill="currentColor" aria-hidden="true" />
        : busy ? <Loader2 size={17} className="ai-voice-spin" aria-hidden="true" />
          : <Mic size={17} aria-hidden="true" />}
    </button>
  );
}

// Строка под полем: запись идёт, распознаётся или что пошло не так.
export function AiVoiceStatus({ voice }) {
  if (voice.state === "recording") {
    return (
      <span className="ai-voice-status recording" role="status">
        <i aria-hidden="true" />
        <span>{`Запись ${clock(voice.seconds)} из ${clock(voice.maxSeconds)} — нажмите ■, чтобы закончить`}</span>
        <button type="button" className="ai-voice-cancel" onClick={voice.cancel}>Отменить</button>
      </span>
    );
  }
  if (voice.state === "busy") return <span className="ai-voice-status" role="status">Распознаю речь…</span>;
  if (!voice.message) return null;
  return (
    <span className={`ai-voice-status ${voice.message.type}`} role={voice.message.type === "error" ? "alert" : "status"}>
      <span>{voice.message.text}</span>
      <button type="button" className="ai-voice-cancel" onClick={voice.dismiss} aria-label="Скрыть сообщение">Скрыть</button>
    </span>
  );
}
