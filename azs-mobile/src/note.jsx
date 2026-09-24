// Плашка-заметка (Note) — единый вид коротких сообщений: лимиты, остановка
// запроса, очередь, предупреждения. Визуал взят с образца владельца
// (24.09.2026): рамка, значок или подпись «Лимит:», текст и действие справа.
// Цвета — токены приложения (styles.css, раздел «Note»), тёмная тема из тех же токенов.
//
//   <Note type="warning" fill label="Лимит" action={<NoteAction …/>}>Текст</Note>
//
// label: true — значок типа; строка — подпись «Лимит:» вместо значка; false — ни того ни другого.
// as: "span" — для вставки внутрь кнопки (в <button> нельзя класть <div>).
import React from "react";

const PATHS = {
  info: "M8 14.5C11.5899 14.5 14.5 11.5899 14.5 8C14.5 4.41015 11.5899 1.5 8 1.5C4.41015 1.5 1.5 4.41015 1.5 8C1.5 11.5899 4.41015 14.5 8 14.5ZM8 16C12.4183 16 16 12.4183 16 8C16 3.58172 12.4183 0 8 0C3.58172 0 0 3.58172 0 8C0 12.4183 3.58172 16 8 16ZM6.25 7H7H7.74999C8.30227 7 8.74999 7.44772 8.74999 8V11.5V12.25H7.24999V11.5V8.5H7H6.25V7ZM8 6C8.55229 6 9 5.55228 9 5C9 4.44772 8.55229 4 8 4C7.44772 4 7 4.44772 7 5C7 5.55228 7.44772 6 8 6Z",
  success: "M14.5 8C14.5 11.5899 11.5899 14.5 8 14.5C4.41015 14.5 1.5 11.5899 1.5 8C1.5 4.41015 4.41015 1.5 8 1.5C11.5899 1.5 14.5 4.41015 14.5 8ZM16 8C16 12.4183 12.4183 16 8 16C3.58172 16 0 12.4183 0 8C0 3.58172 3.58172 0 8 0C12.4183 0 16 3.58172 16 8ZM11.5303 6.53033L12.0607 6L11 4.93934L10.4697 5.46967L6.5 9.43934L5.53033 8.46967L5 7.93934L3.93934 9L4.46967 9.53033L5.96967 11.0303C6.26256 11.3232 6.73744 11.3232 7.03033 11.0303L11.5303 6.53033Z",
  warning: "M8.55846 2H7.44148L1.88975 13.5H14.1102L8.55846 2ZM9.90929 1.34788C9.65902 0.829456 9.13413 0.5 8.55846 0.5H7.44148C6.86581 0.5 6.34092 0.829454 6.09065 1.34787L0.192608 13.5653C-0.127943 14.2293 0.355835 15 1.09316 15H14.9068C15.6441 15 16.1279 14.2293 15.8073 13.5653L9.90929 1.34788ZM8.74997 4.75V5.5V8V8.75H7.24997V8V5.5V4.75H8.74997ZM7.99997 12C8.55226 12 8.99997 11.5523 8.99997 11C8.99997 10.4477 8.55226 10 7.99997 10C7.44769 10 6.99997 10.4477 6.99997 11C6.99997 11.5523 7.44769 12 7.99997 12Z",
  error: "M5.30761 1.5L1.5 5.30761L1.5 10.6924L5.30761 14.5H10.6924L14.5 10.6924V5.30761L10.6924 1.5H5.30761ZM5.10051 0C4.83529 0 4.58094 0.105357 4.3934 0.292893L0.292893 4.3934C0.105357 4.58094 0 4.83529 0 5.10051V10.8995C0 11.1647 0.105357 11.4191 0.292894 11.6066L4.3934 15.7071C4.58094 15.8946 4.83529 16 5.10051 16H10.8995C11.1647 16 11.4191 15.8946 11.6066 15.7071L15.7071 11.6066C15.8946 11.4191 16 11.1647 16 10.8995V5.10051C16 4.83529 15.8946 4.58093 15.7071 4.3934L11.6066 0.292893C11.4191 0.105357 11.1647 0 10.8995 0H5.10051ZM8.75 3.75V4.5V8L8.75 8.75H7.25V8V4.5V3.75H8.75ZM8 12C8.55229 12 9 11.5523 9 11C9 10.4477 8.55229 10 8 10C7.44772 10 7 10.4477 7 11C7 11.5523 7.44772 12 8 12Z",
};

const ICON_OF = { success: "success", warning: "warning", error: "error" };

export const NOTE_TYPES = ["default", "secondary", "success", "warning", "error", "alert"];

function NoteIcon({ type }) {
  return (
    <svg className="note-icon" width="16" height="16" viewBox="0 0 16 16" strokeLinejoin="round" aria-hidden="true">
      <path fillRule="evenodd" clipRule="evenodd" d={PATHS[ICON_OF[type] || "info"]} />
    </svg>
  );
}

export function Note({
  size = "medium",
  action = null,
  type = "default",
  fill = false,
  disabled = false,
  label = true,
  as: Tag = "div",
  role,
  className = "",
  children,
}) {
  const kind = NOTE_TYPES.includes(type) ? type : "default";
  const Inner = Tag === "span" ? "span" : "div";
  const classes = [
    "note",
    `note-${size}`,
    `note-${kind === "alert" ? "error" : kind}`,
    fill ? "note-fill" : "",
    disabled ? "note-disabled" : "",
    typeof label === "string" ? "note-labelled" : "",
    className,
  ].filter(Boolean).join(" ");
  return (
    <Tag className={classes} role={role}>
      <Inner className="note-body">
        {label === true && <NoteIcon type={kind} />}
        {typeof label === "string" && <span className="note-label">{label}:</span>}
        <span className="note-text">{children}</span>
      </Inner>
      {action}
    </Tag>
  );
}

// Действие справа в плашке: компактная кнопка в тон приложения.
export function NoteAction({ onClick, children, title }) {
  return (
    <button type="button" className="note-action" onClick={onClick} title={title}>
      {children}
    </button>
  );
}
