# UI Improvements

## 1. Dark mode (`prefers-color-scheme: dark`)
Full CSS variable override block added at the end of `styles.css`. New token set:
`--bg: #0f1117`, `--surface: #1a1d26`, `--surface-2: #22263a`, `--line: #2d3347`,
`--text: #e8ecf2`, `--muted: #7a8599`, `--red: #e8253e`, `--green: #1ab86f`,
`--amber: #e09c1a`, `--blue: #4b8fee`.
All hardcoded light backgrounds (`#fff1f3`, `#eef5ff`, `#eef1f5`, `#eaf8f1`, `#fff5df`,
map overlays, bottom nav, home hero, passport items, badges, status chips, staff days,
loading shimmer gradients) replaced with dark equivalents via the media query.
Combined `@media (prefers-color-scheme: dark) and (hover: hover)` block handles
hover-state overrides that require both conditions.

## 2. Skeleton loading for StationList
`StationList` now accepts a `loading` boolean prop. When `stations.length === 0 && loading`
the component renders 6 (or 8 in compact mode) `<div className="station-row skeleton">`
placeholder rows with `aria-busy="true"` on the container.
CSS: `.station-row.skeleton` uses the shimmer gradient, suppresses pointer-events, and
sets `min-height: 110px`.
`loading` is computed in `App` as `Boolean(auth.user) && payload.meta === null` and
passed down to `StationList`.

## 3. Shimmer utility class
`.skeleton` utility class added with `@keyframes skeleton-shimmer`:
`background: linear-gradient(90deg, var(--surface-2) 25%, var(--line) 37%, var(--surface-2) 63%)`;
`background-size: 400% 100%; animation: skeleton-shimmer 1.4s ease infinite`.
Works correctly in both light and dark modes via CSS variables.

## 4. Station-row selected-state bar
`.station-row` gained `position: relative` so the pseudo-element can position absolutely.
`.station-row.selected::before` renders a 3 px animated left-edge bar in `var(--red)`.
`@keyframes selected-bar` scales the bar from `scaleY(0)` to `scaleY(1)` in 220 ms with
a spring-like `cubic-bezier(0.22, 1, 0.36, 1)` easing.

## 5. Select chip filled animation
`.select-chip.filled` now has explicit `transition: background 200ms ease, color 180ms ease, border-color 180ms ease`
and runs `@keyframes chip-pop` (scale 0.92 → 1, opacity 0.72 → 1) on class application,
giving tactile feedback when a filter is selected.

## 6. Mobile hero improvements (`max-width: 768px`)
New `@media (max-width: 768px)` block:
- `h2` font-size set to 28 px with `line-height: 1.1`
- `h2::after` pseudo-element: 48 × 3 px red accent line, `margin-top: 8px`, `border-radius: 2px`
- Passport badge items: reduced `min-height` to 76 px, tighter padding, smaller `strong` (18 px)

## Build
`npm run build` passes with 0 errors. CSS bundle: 56.67 kB (11.04 kB gzip).
