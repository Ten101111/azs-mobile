// Значок файла — по образцу владельца (25.09.2026): лист с условной разметкой,
// своей для каждого формата, и цветная плашка с расширением в углу.
// Образец был на Tailwind; здесь — классы .file-card* в styles.css и токены
// приложения, поэтому тёмная тема работает без отдельной вёрстки.
// Форматы ИИ-07: PDF, DOCX, XLSX, CSV, TXT, PPTX; остальные рисуются как текст.
import React from "react";

const KIND_OF = {
  pdf: "pdf", doc: "docx", docx: "docx", docm: "docx", xls: "xlsx", xlsx: "xlsx", xlsm: "xlsx", xlsb: "xlsx",
  csv: "csv", txt: "txt", md: "txt", ppt: "pptx", pptx: "pptx", pptm: "pptx",
};
const TAG = { pdf: "pdf", docx: "docx", xlsx: "xlsx", csv: "csv", txt: "txt", pptx: "pptx" };

function extensionOf(nameOrKind) {
  const text = String(nameOrKind || "").toLowerCase();
  return text.includes(".") ? text.split(".").pop() : text;
}

export function fileKindOf(nameOrKind) {
  return KIND_OF[extensionOf(nameOrKind)] || "txt";
}

function Bars({ rows }) {
  return (
    <span className="file-card-lines">
      {rows.map((row, index) => (
        <span className="file-card-row" key={index}>
          {row.map((width, cell) => (
            <span key={cell} className={index === 0 ? "file-card-bar strong" : "file-card-bar"} style={{ width: `${width}%` }} />
          ))}
        </span>
      ))}
    </span>
  );
}

function Grid({ pills = false }) {
  // Таблица: шапка плотнее, дальше — строки с разным числом заполненных ячеек.
  const rows = [3, 3, 2, 3, 1];
  return (
    <span className={pills ? "file-card-grid pills" : "file-card-grid"}>
      {rows.map((cells, index) => (
        <span className="file-card-grid-row" key={index}>
          {[0, 1, 2].map((cell) => (
            <span key={cell} className={`file-card-cell${index === 0 ? " head" : ""}${cell >= cells ? " empty" : ""}`} />
          ))}
        </span>
      ))}
    </span>
  );
}

function Slide() {
  return (
    <span className="file-card-slide-wrap">
      <span className="file-card-slide">
        <span className="file-card-slide-mark" />
        <span className="file-card-bar strong" style={{ width: "60%" }} />
      </span>
      <Bars rows={[[55, 30], [30], [40]]} />
    </span>
  );
}

const PLACEHOLDER = {
  xlsx: () => <Grid />,
  csv: () => <Grid pills />,
  pptx: () => <Slide />,
  pdf: () => <Bars rows={[[50], [33, 33], [50, 33], [33, 33], [33, 50], [33]]} />,
  docx: () => <Bars rows={[[60], [85], [70], [85], [40]]} />,
  txt: () => <Bars rows={[[40], [85], [60], [75], [30]]} />,
};

export function FileCard({ kind, size = "md", className = "" }) {
  const format = fileKindOf(kind);
  // На плашке — настоящее расширение: «xlsm» не должен выглядеть как принятый «xlsx».
  const ext = extensionOf(kind);
  const tag = /^[a-z0-9]{1,5}$/.test(ext) ? ext : TAG[format];
  const Placeholder = PLACEHOLDER[format] || PLACEHOLDER.txt;
  return (
    <span aria-hidden="true" className={`file-card file-card-${size} is-${format}${className ? ` ${className}` : ""}`}>
      <span className="file-card-sheet"><Placeholder /></span>
      <span className="file-card-tag">{tag}</span>
    </span>
  );
}

export default FileCard;
