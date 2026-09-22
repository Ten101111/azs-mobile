// Орб «Мобильного аналитика» — React-обёртка над движком.
//
//   <AiOrb state="analyzing" size={240} label />
//
// Движок (orbEngine.js) живёт вне React и не пересоздаётся на каждый рендер:
// смена пропса state — это orb.setState(), пружины делают остальное.
// Framer Motion отвечает за то, что вокруг орба: подпись состояния и
// появление самого холста. Внутренняя жизнь объекта — целиком в шейдере.
//
// Состояния: idle | listening | searching | analyzing | forming | ready
// плюс два исхода приложения: refused | error.
import React, { useEffect, useRef, useState } from "react";
import { AnimatePresence, motion, useReducedMotion } from "framer-motion";
import { createOrb, isOrbSupported, ORB_LABELS } from "./orbEngine.js";

export { ORB_LABELS };

export default function AiOrb({
  state = "idle",
  size = 240,
  label = false,
  interactive = true,
  tapToListen = false,
  quality = "auto",
  mode = "auto",            // dark | light | auto — палитра под фон страницы
  onStateChange,
  fallback = null,          // что показать без WebGL2 (например, <AiMark />)
  engineRef = null,         // ref: получит экземпляр движка — для setState снаружи React
  onTap = null,             // клик по орбу (например, поставить курсор в поле)
  initialState = "idle",    // откуда стартуют пружины: «forming» даёт бесшовную передачу из блока ожидания
  appear = true,            // появление холста через Framer Motion; false — сразу, без перехода
  className = "",
  style,
}) {
  const canvasRef = useRef(null);
  const orbRef = useRef(null);
  const [supported] = useState(() => (typeof window !== "undefined" ? isOrbSupported() : true));
  const [shown, setShown] = useState(state);
  const reduced = useReducedMotion();

  useEffect(() => {
    if (!supported || !canvasRef.current) return undefined;
    const orb = createOrb(canvasRef.current, {
      quality,
      mode,
      interactive,
      tapToListen,
      reducedMotion: "auto",
      initialState,
      onState: (next, prev) => { setShown(next); onStateChange?.(next, prev); },
    });
    orbRef.current = orb;
    if (engineRef) engineRef.current = orb;
    orb.setState(state);
    return () => { orb.destroy(); orbRef.current = null; if (engineRef) engineRef.current = null; };
    // движок создаётся один раз; параметры ниже меняются через его методы
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [supported, quality, mode, interactive, tapToListen]);

  useEffect(() => { orbRef.current?.setState(state); }, [state]);

  const px = typeof size === "number" ? `${size}px` : size;

  if (!supported) {
    return (
      <div className={`ai-orb ai-orb-fallback ${className}`} style={{ width: px, height: px, ...style }} onClick={onTap || undefined}>
        {fallback}
      </div>
    );
  }

  return (
    <div
      className={`ai-orb ${className}`}
      style={{ display: "inline-flex", flexDirection: "column", alignItems: "center", gap: 16, cursor: onTap ? "pointer" : undefined, ...style }}
      onClick={onTap || undefined}
    >
      <motion.canvas
        ref={canvasRef}
        role="img"
        aria-label={ORB_LABELS[shown] || "ИИ-аналитик"}
        style={{ width: px, height: px, display: "block", touchAction: "none" }}
        initial={reduced || !appear ? false : { opacity: 0, scale: 0.96 }}
        animate={{ opacity: 1, scale: 1 }}
        transition={{ type: "spring", stiffness: 180, damping: 26, mass: 0.9 }}
      />
      {label && (
        <div style={{ position: "relative", height: "1.4em", minWidth: "14ch", textAlign: "center" }} aria-live="polite">
          <AnimatePresence initial={false}>
            <motion.span
              key={shown}
              style={{ position: "absolute", inset: 0, color: shown === "ready" ? "#F5F5F3" : "#8A8D92", fontSize: 15 }}
              initial={reduced ? { opacity: 0 } : { opacity: 0, y: 6 }}
              animate={{ opacity: 1, y: 0 }}
              exit={reduced ? { opacity: 0 } : { opacity: 0, y: -6 }}
              transition={{ duration: 0.45, ease: [0.22, 1, 0.36, 1] }}
            >
              {ORB_LABELS[shown]}
            </motion.span>
          </AnimatePresence>
        </div>
      )}
    </div>
  );
}

// Этап конвейера «Мобильного аналитика» → состояние орба.
// Конвейер знает четыре этапа (draft, check, read, write) и три исхода;
// «слушаю» — это набор текста в поле, «готово» — ответ пришёл.
export function orbStateFromPipeline({ typing = false, pending = false, stages = [], outcome = null } = {}) {
  if (pending) {
    const active = [...stages].reverse().find((s) => s.state === "active");
    if (!active) return "searching";
    // Шаги агента приходят с полем kind: разбор и схема — «ищет», запросы и
    // вычисления — «анализирует», график и формулировка — «формирует».
    if (active.kind === "plan" || active.kind === "schema") return "searching";
    if (active.kind === "sql" || active.kind === "python") return "analyzing";
    if (active.kind === "chart" || active.kind === "write") return "forming";
    if (active.key === "draft" || active.key === "check" || active.key === "plan") return "searching";
    if (active.key === "read") return "analyzing";
    if (active.key === "write") return "forming";
    return "analyzing";
  }
  if (outcome === "ready") return "ready";
  if (outcome === "error") return "error";
  if (outcome === "refused" || outcome === "clarify") return "refused";
  return typing ? "listening" : "idle";
}
