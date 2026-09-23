// React-обёртка малого орба (miniOrb.js).
//
//   <AiMiniOrb size={20} state="compute" beat={stepsDone} />
//
// size — 20 (строка текста) или 64 (вместо большого орба без WebGL);
// другие размеры берут ближайший пресет. beat — любое число: когда оно
// меняется, орб даёт короткий импульс (закончился внутренний шаг).
// label — подпись для экранного диктора; без неё орб декоративный.
import React, { useEffect, useRef } from "react";
import { createMiniOrb, MINI_LABELS } from "./miniOrb.js";

export { MINI_LABELS };

export default function AiMiniOrb({ size = 20, state = "idle", beat = 0, label = false, className = "", style }) {
  const canvasRef = useRef(null);
  const orbRef = useRef(null);
  const lastBeat = useRef(beat);

  useEffect(() => {
    if (!canvasRef.current) return undefined;
    let orb = null;
    try {
      orb = createMiniOrb(canvasRef.current, { size, state });
    } catch {
      return undefined;      // без холста остаётся пустое место нужного размера
    }
    orbRef.current = orb;
    return () => { orb.destroy(); orbRef.current = null; };
    // движок создаётся заново только при смене размера
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [size]);

  useEffect(() => { orbRef.current?.setState(state); }, [state]);

  useEffect(() => {
    if (beat !== lastBeat.current) {
      lastBeat.current = beat;
      orbRef.current?.beat();
    }
  }, [beat]);

  const text = label ? (typeof label === "string" ? label : MINI_LABELS[state]) : "";
  return (
    <canvas
      ref={canvasRef}
      className={`ai-mini-orb ${className}`}
      style={{ width: size, height: size, ...style }}
      role={text ? "img" : undefined}
      aria-label={text || undefined}
      aria-hidden={text ? undefined : "true"}
    />
  );
}
