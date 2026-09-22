import React, { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { createPortal } from "react-dom";
import { createRoot } from "react-dom/client";
import { max } from "d3-array";
import { scaleLinear } from "d3-scale";
import { AnimatePresence, motion, useReducedMotion } from "framer-motion";
// Орб на WebGL: крупные места — пустой экран и блок ожидания. Мелкие места
// остаются на SVG-знаке AiMark, как требует спецификация облика.
import AiOrb, { orbStateFromPipeline } from "./orb/AiOrb.jsx";
import { AiAnalysis, aiAgentStages, aiAnalysisText, AI_DEPTH_FALLBACK } from "./aiAnalysis.jsx";
import {
  AlertTriangle,
  BarChart3,
  Check,
  CheckCircle2,
  ChevronDown,
  ChevronLeft,
  ChevronRight,
  CircleDot,
  Clock3,
  Coffee,
  Copy,
  Download,
  Filter,
  Fuel,
  Gauge,
  Heart,
  Home,
  List,
  LocateFixed,
  Map as MapIcon,
  MessageSquare,
  Monitor,
  MoreHorizontal,
  Navigation,
  PanelLeft,
  Pencil,
  Phone,
  Pin,
  PinOff,
  Plus,
  RefreshCw,
  RotateCcw,
  Search,
  Send,
  Share2,
  ShieldCheck,
  SlidersHorizontal,
  Smartphone,
  Sparkles,
  Star,
  Store,
  Toilet,
  Trash2,
  Users,
  X,
} from "lucide-react";
import "./styles.css";

const statusColors = {
  "Действующая": "#14945f",
  CODO: "#2f9f72",
  Арендованные: "#74ae45",
  Реконструкция: "#8f99a8",
  Консервация: "#c83a45",
  "Временная приостановка работы": "#d9822b",
};

const defaultFilters = {
  npo: "",
  subject: "",
  status: "",
  type: "",
  location: "",
  service: "",
  quality: "",
  fuel: [],
  favorites: false,
};

// Разделы приложения. «ИИ-аналитик» — самостоятельный раздел наравне с остальными,
// а не вкладка внутри аналитики: решение владельца, задача З-9 реестра БТ.
// Пункт показывается, только когда бэкенд отвечает на /api/ai/status.
const viewItems = [
  { id: "list", label: "Реестр", mobileLabel: "Реестр", Icon: List },
  { id: "map", label: "Карта", mobileLabel: "Карта", Icon: MapIcon },
  { id: "home", label: "Главная", mobileLabel: "Главная", Icon: Home },
  { id: "analytics", label: "Аналитика", mobileLabel: "Аналитика", Icon: BarChart3 },
  // accent — значок красится корпоративным красным: на ИИ нужен акцент.
  // Значок и вид — те же, что у кнопки «ИИ-аналитик» на главной (Sparkles,
  // без акцента): решение владельца от 22.09.2026 — кнопка везде одинаковая.
  // Флаги accent и mark у пункта сохранены в коде навигации на будущее.
  { id: "ai", label: "ИИ-аналитик", mobileLabel: "ИИ", Icon: Sparkles, optional: true },
  // mobileHidden — на нижней панели не показываем: обычному пользователю
  // контроль не нужен каждый день, вход остаётся плиткой на главной.
  { id: "quality", label: "Контроль", mobileLabel: "Контроль", Icon: ShieldCheck, mobileHidden: true },
];
const viewIds = new Set(viewItems.map((item) => item.id));

const YANDEX_MAPS_API_KEY = import.meta.env.VITE_YANDEX_MAPS_API_KEY || "";
let yandexMapsPromise;

function useCountUp(target, duration = 680) {
  const reduceMotion = useReducedMotion();
  const fmt = (n) => new Intl.NumberFormat("ru-RU").format(Math.round(n));
  const [display, setDisplay] = useState(() => fmt(reduceMotion ? target : 0));

  useEffect(() => {
    if (reduceMotion || !Number.isFinite(target) || target === 0) {
      setDisplay(fmt(target));
      return;
    }
    const start = performance.now();
    let raf;
    function tick(now) {
      const t = Math.min((now - start) / duration, 1);
      const eased = 1 - Math.pow(1 - t, 3);
      setDisplay(fmt(target * eased));
      if (t < 1) raf = requestAnimationFrame(tick);
    }
    raf = requestAnimationFrame(tick);
    return () => cancelAnimationFrame(raf);
  }, [target, duration, reduceMotion]);

  return display;
}

function uniqueOptions(stations, key) {
  return [...new Set(stations.map((item) => item[key]).filter(Boolean))].sort((a, b) =>
    String(a).localeCompare(String(b), "ru"),
  );
}

function asInt(value) {
  return new Intl.NumberFormat("ru-RU").format(value);
}

function shortStatus(status) {
  if (!status) return "Без статуса";
  if (status.length > 24) return status.slice(0, 22) + "...";
  return status;
}

function statusTone(status) {
  if (status === "Действующая") return "active";
  if (status === "CODO") return "codo";
  if (status === "Арендованные") return "rented";
  if (status === "Реконструкция") return "reconstruction";
  if (status === "Консервация") return "conservation";
  if (status === "Временная приостановка работы") return "paused";
  return "gray";
}

function formatMetaDate(meta) {
  const value = meta?.updatedAt || meta?.generatedAt || meta?.createdAt || meta?.date || meta?.sourceDate;
  if (!value) return "сегодня";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return String(value);
  return date.toLocaleDateString("ru-RU", { day: "2-digit", month: "short", year: "numeric" });
}

function hasValidPoint(station) {
  const lat = Number(station.lat);
  const lon = Number(station.lon);
  return Number.isFinite(lat) && Number.isFinite(lon) && lat !== 0 && lon !== 0;
}

function toLatLng(station) {
  return [Number(station.lat), Number(station.lon)];
}

function toYmapsCoordinates(station) {
  return [Number(station.lon), Number(station.lat)];
}

function mapLocationForYmaps(points, selected, focusSelected = true) {
  if (focusSelected && selected && hasValidPoint(selected)) {
    return { center: toYmapsCoordinates(selected), zoom: 13 };
  }

  if (points.length === 1) {
    return { center: toYmapsCoordinates(points[0]), zoom: 10 };
  }

  if (points.length > 1) {
    const latValues = points.map((station) => Number(station.lat));
    const lonValues = points.map((station) => Number(station.lon));
    return {
      bounds: [
        [Math.min(...lonValues), Math.min(...latValues)],
        [Math.max(...lonValues), Math.max(...latValues)],
      ],
    };
  }

  return { center: [37.6176, 55.7558], zoom: 4 };
}

function mapLocationForYmaps2(points, selected, focusSelected = true) {
  if (focusSelected && selected && hasValidPoint(selected)) {
    return { center: toLatLng(selected), zoom: 13 };
  }

  if (points.length === 1) {
    return { center: toLatLng(points[0]), zoom: 10 };
  }

  if (points.length > 1) {
    const latValues = points.map((station) => Number(station.lat));
    const lonValues = points.map((station) => Number(station.lon));
    return {
      bounds: [
        [Math.min(...latValues), Math.min(...lonValues)],
        [Math.max(...latValues), Math.max(...lonValues)],
      ],
    };
  }

  return { center: [55.7558, 37.6176], zoom: 4 };
}

async function checkYandexApiKey(apiKey) {
  try {
    const res = await fetch(
      `https://api-maps.yandex.ru/v3/?apikey=${encodeURIComponent(apiKey)}&lang=ru_RU`,
      { method: "HEAD", referrerPolicy: "no-referrer" }
    );
    if (res.status === 403) {
      let msg = "YANDEX_MAPS_KEY_FORBIDDEN";
      try {
        const body = await fetch(
          `https://api-maps.yandex.ru/v3/?apikey=${encodeURIComponent(apiKey)}&lang=ru_RU`,
          { referrerPolicy: "no-referrer" }
        ).then((r) => r.json());
        if (body?.message) msg = `YANDEX_MAPS_KEY_FORBIDDEN: ${body.message}`;
      } catch (_) {}
      return msg;
    }
  } catch (_) {}
  return null;
}

function loadYandexMaps(apiKey) {
  if (!apiKey) {
    return Promise.reject(new Error("YANDEX_MAPS_API_KEY_MISSING"));
  }

  if (window.ymaps3) {
    return window.ymaps3.ready.then(() => ({ version: "v3", api: window.ymaps3 }));
  }

  if (window.ymaps) {
    return new Promise((resolve) => window.ymaps.ready(() => resolve({ version: "v2", api: window.ymaps })));
  }

  if (yandexMapsPromise) return yandexMapsPromise;

  function loadScript(version, url, referrerPolicy) {
    return new Promise((resolve, reject) => {
      const timeoutId = window.setTimeout(() => {
        reject(new Error(`YANDEX_MAPS_${version}_TIMEOUT`));
      }, 12000);
      const existingScript = document.querySelector(`script[data-yandex-maps-api="${version}"]`);

      function cleanup() {
        window.clearTimeout(timeoutId);
      }

      function handleReady() {
        cleanup();
        if (version === "v3") {
          if (!window.ymaps3) {
            reject(new Error("YANDEX_MAPS_V3_NOT_AVAILABLE"));
            return;
          }
          window.ymaps3.ready.then(() => resolve({ version: "v3", api: window.ymaps3 })).catch(reject);
          return;
        }

        if (!window.ymaps) {
          reject(new Error("YANDEX_MAPS_V2_NOT_AVAILABLE"));
          return;
        }
        window.ymaps.ready(() => resolve({ version: "v2", api: window.ymaps }));
      }

      if (existingScript) {
        existingScript.addEventListener("load", handleReady, { once: true });
        existingScript.addEventListener("error", () => reject(new Error(`YANDEX_MAPS_${version}_FAILED`)), { once: true });
        return;
      }

      const script = document.createElement("script");
      script.src = url;
      script.async = true;
      script.dataset.yandexMapsApi = version;
      if (referrerPolicy) script.referrerPolicy = referrerPolicy;
      script.addEventListener("load", handleReady, { once: true });
      script.addEventListener(
        "error",
        () => {
          cleanup();
          script.remove();
          reject(new Error(`YANDEX_MAPS_${version}_FAILED`));
        },
        { once: true },
      );
      document.head.appendChild(script);
    });
  }

  yandexMapsPromise = loadScript(
    "v3",
    `https://api-maps.yandex.ru/v3/?apikey=${encodeURIComponent(apiKey)}&lang=ru_RU`,
  ).catch(() =>
    loadScript(
      "v2",
      `https://api-maps.yandex.ru/2.1/?apikey=${encodeURIComponent(apiKey)}&lang=ru_RU`,
      "no-referrer",
    ),
  );

  return yandexMapsPromise;
}

function stationFeature(station, selectedId) {
  return {
    type: "Feature",
    id: station.id,
    geometry: {
      type: "Point",
      coordinates: toLatLng(station),
    },
    properties: {
      hintContent: `${station.name || station.stationNumber} · ${station.subject || ""}`,
      balloonContentHeader: station.name || `АЗС № ${station.stationNumber}`,
      balloonContentBody: station.address || station.subject || "",
    },
    options: {
      preset: selectedId === station.id ? "islands#redCircleDotIcon" : "islands#circleDotIcon",
      iconColor: selectedId === station.id ? "#E31E24" : statusColors[station.status] || "#8f99a8",
    },
  };
}

function createStationMarker(ymaps3, station, selectedId, onSelect) {
  const element = document.createElement("button");
  element.type = "button";
  element.className = `ymap-marker ${selectedId === station.id ? "selected" : ""}`;
  element.style.setProperty("--marker-color", selectedId === station.id ? "#E31E24" : statusColors[station.status] || "#8f99a8");
  element.title = `${station.name || station.stationNumber} · ${station.subject || ""}`;
  element.addEventListener("click", (event) => {
    event.stopPropagation();
    onSelect(station.id);
  });

  return new ymaps3.YMapMarker(
    {
      coordinates: toYmapsCoordinates(station),
      zIndex: selectedId === station.id ? 20 : 10,
    },
    element,
  );
}

function MapLegend() {
  const items = [
    ["Действующая", statusColors["Действующая"]],
    ["CODO", statusColors["CODO"]],
    ["Арендованные", statusColors["Арендованные"]],
    ["Реконструкция", statusColors["Реконструкция"]],
    ["Консервация", statusColors["Консервация"]],
    ["Временная приостановка работы", statusColors["Временная приостановка работы"]],
  ];

  return (
    <div className="map-legend">
      {items.map(([label, color]) => (
        <span key={label}>
          <i style={{ background: color }} />
          {label}
        </span>
      ))}
    </div>
  );
}

function geolocationErrorMessage(error) {
  if (!navigator.geolocation) return "Геолокация недоступна в этом браузере.";
  if (!window.isSecureContext) return "Геолокация работает только через HTTPS или localhost.";
  if (error?.code === 1) return "Доступ к геолокации запрещен.";
  if (error?.code === 2) return "Не удалось определить местоположение.";
  if (error?.code === 3) return "Истекло время ожидания геолокации.";
  return "Не удалось получить геолокацию.";
}

function formatAccuracy(accuracy) {
  if (!accuracy) return "";
  if (accuracy >= 1000) return `точность около ${(accuracy / 1000).toFixed(1)} км`;
  return `точность около ${Math.round(accuracy)} м`;
}

function formatLocationMessage(location) {
  return formatAccuracy(location.accuracy) || (location.source === "yandex" ? "Местоположение определено Яндексом." : "Местоположение найдено.");
}

function getBrowserLocation() {
  return new Promise((resolve, reject) => {
    if (!navigator.geolocation || !window.isSecureContext) {
      reject(new Error("BROWSER_GEOLOCATION_UNAVAILABLE"));
      return;
    }

    navigator.geolocation.getCurrentPosition(
      (position) => {
        resolve({
          lat: position.coords.latitude,
          lon: position.coords.longitude,
          accuracy: position.coords.accuracy,
          source: "browser",
        });
      },
      reject,
      {
        enableHighAccuracy: true,
        timeout: 12000,
        maximumAge: 30000,
      },
    );
  });
}

function getYandexLocation(api, version) {
  if (version === "v3" && api.geolocation?.getPosition) {
    return api.geolocation.getPosition({ enableHighAccuracy: true, timeout: 12000, maximumAge: 30000 }).then((position) => ({
      lat: position.coords[1],
      lon: position.coords[0],
      accuracy: position.accuracy,
      source: "yandex",
    }));
  }

  if (version === "v2" && api.geolocation?.get) {
    return api.geolocation
      .get({
        provider: "yandex",
        mapStateAutoApply: false,
        autoReverseGeocode: false,
        timeout: 12000,
      })
      .then((result) => {
        const geoObject = result.geoObjects?.get(0);
        const coords = geoObject?.geometry?.getCoordinates();
        if (!coords) throw new Error("YANDEX_GEOLOCATION_EMPTY");

        return {
          lat: coords[0],
          lon: coords[1],
          bounds: result.geoObjects?.getBounds?.(),
          source: "yandex",
        };
      });
  }

  return Promise.reject(new Error("YANDEX_GEOLOCATION_UNAVAILABLE"));
}

function isValidPhone(phone) {
  const text = String(phone || "").trim();
  const digits = text.replace(/\D+/g, "");
  return digits.length >= 10 && !/отсутств|нет|nan/i.test(text);
}

function bestPhone(station) {
  return [station.managerPhone, station.seniorOperatorPhone, station.territoryManagerPhone, station.regionalManagerPhone].find(isValidPhone) || "";
}

function pct(value, total) {
  if (!total) return 0;
  return Math.round((value / total) * 100);
}

function currentMonthPeriod() {
  const now = new Date();
  return `${now.getFullYear()}-${String(now.getMonth() + 1).padStart(2, "0")}`;
}

function formatPeriod(period) {
  const [year, month] = period.split("-");
  const date = new Date(Number(year), Number(month) - 1, 1);
  return date.toLocaleDateString("ru-RU", { month: "long", year: "numeric" });
}

function PeriodNavigator({ periods, period, onChange, label = "Период KPI" }) {
  const reduceMotion = useReducedMotion();
  const orderedPeriods = [...periods].sort();
  const currentIndex = orderedPeriods.indexOf(period);
  const previous = currentIndex > 0 ? orderedPeriods[currentIndex - 1] : "";
  const next = currentIndex >= 0 && currentIndex < orderedPeriods.length - 1 ? orderedPeriods[currentIndex + 1] : "";

  if (orderedPeriods.length <= 1) {
    return <span>{formatPeriod(period)}</span>;
  }

  return (
    <div className="period-navigator">
      <motion.button
        className="period-nav-button"
        type="button"
        disabled={!previous}
        onClick={() => previous && onChange(previous)}
        aria-label={previous ? `Предыдущий месяц: ${formatPeriod(previous)}` : "Предыдущего месяца нет"}
        title="Предыдущий месяц"
        whileTap={reduceMotion ? undefined : { scale: 0.94 }}
      >
        <ChevronLeft size={17} />
      </motion.button>
      <select className="period-select" value={period} onChange={(event) => onChange(event.target.value)} aria-label={label}>
        {orderedPeriods.map((item) => (
          <option key={item} value={item}>
            {formatPeriod(item)}
          </option>
        ))}
      </select>
      <motion.button
        className="period-nav-button"
        type="button"
        disabled={!next}
        onClick={() => next && onChange(next)}
        aria-label={next ? `Следующий месяц: ${formatPeriod(next)}` : "Следующего месяца нет"}
        title="Следующий месяц"
        whileTap={reduceMotion ? undefined : { scale: 0.94 }}
      >
        <ChevronRight size={17} />
      </motion.button>
    </div>
  );
}

function formatKpiValue(value, unit) {
  const numeric = Number(value);
  if (!Number.isFinite(numeric)) return "—";

  if (unit === "₽" && Math.abs(numeric) >= 1_000_000) {
    return `${(numeric / 1_000_000).toLocaleString("ru-RU", { maximumFractionDigits: 1 })} млн ₽`;
  }

  if (unit === "л" && Math.abs(numeric) >= 1000) {
    return `${(numeric / 1000).toLocaleString("ru-RU", { maximumFractionDigits: 0 })} тыс. л`;
  }

  return `${numeric.toLocaleString("ru-RU", { maximumFractionDigits: 0 })}${unit ? ` ${unit}` : ""}`;
}

function formatStaffValue(value) {
  const numeric = Number(value);
  if (!Number.isFinite(numeric)) return "—";
  return numeric.toLocaleString("ru-RU", { maximumFractionDigits: Number.isInteger(numeric) ? 0 : 1 });
}

function formatDelta(value) {
  if (value === null || value === undefined || value === "") return "—";
  const numeric = Number(value);
  if (!Number.isFinite(numeric)) return "—";
  return `${numeric > 0 ? "+" : ""}${numeric.toLocaleString("ru-RU", { maximumFractionDigits: 1 })}%`;
}

function deltaTone(value) {
  if (value === null || value === undefined || value === "") return "";
  const numeric = Number(value);
  if (!Number.isFinite(numeric) || numeric === 0) return "";
  return numeric > 0 ? "positive" : "negative";
}

const FUEL_STOCK_REFRESH_MS = 60_000;
const fuelNameCollator = new Intl.Collator("ru-RU", { numeric: true, sensitivity: "base" });

function numericOrNull(value) {
  if (value === null || value === undefined || value === "") return null;
  const numeric = Number(value);
  return Number.isFinite(numeric) ? numeric : null;
}

function sumFuelMetric(a, b) {
  const left = numericOrNull(a);
  const right = numericOrNull(b);
  if (left === null && right === null) return null;
  return (left || 0) + (right || 0);
}

function normalizeFuelToken(value) {
  return String(value || "")
    .toUpperCase()
    .replace(/Ё/g, "Е")
    .replace(/[^0-9A-ZА-Я]+/g, "");
}

function canonicalFuelLabel(item) {
  const value = String(item?.canonicalFuel || item?.fuelCode || item?.fuelName || "")
    .trim()
    .toUpperCase()
    .replace(/Ё/g, "Е")
    .replace(/\s+/g, " ");
  if (!value || value === "UNMAPPED" || value === "UNKNOWN") return "";

  const compact = normalizeFuelToken(value);
  const gasoline = compact.match(/(?:АБ|АИ|AB|AI)(80|91|92|93|95|98|100)/);
  const isEcto = compact.includes("ЭКТО") || compact.includes("ECTO") || compact.includes("EKTO");
  if (gasoline) return `АИ-${gasoline[1]}${isEcto ? " ЭКТО" : ""}`;
  if (compact === "СУГ" || compact.includes("LPG") || compact.includes("СЖИЖЕН")) return "СУГ";
  if (compact === "КПГ" || compact.includes("CNG") || compact.includes("МЕТАН")) return "КПГ";
  if (compact.includes("ДТ") || compact.includes("DT") || compact.includes("DIESEL") || compact.includes("ДИЗЕЛ")) {
    return `ДТ${isEcto ? " ЭКТО" : ""}`;
  }
  return value;
}

function compareFuelGroups(left, right) {
  const gasolinePattern = /^АИ-(\d+)(?: ЭКТО)?$/;
  const leftGasoline = left.label.match(gasolinePattern);
  const rightGasoline = right.label.match(gasolinePattern);
  if (leftGasoline && rightGasoline) {
    const gradeDelta = Number(leftGasoline[1]) - Number(rightGasoline[1]);
    if (gradeDelta) return gradeDelta;
    return Number(left.label.includes("ЭКТО")) - Number(right.label.includes("ЭКТО"));
  }
  if (leftGasoline) return -1;
  if (rightGasoline) return 1;

  const fixedOrder = ["ДТ", "ДТ ЭКТО", "СУГ", "КПГ"];
  const leftIndex = fixedOrder.indexOf(left.label);
  const rightIndex = fixedOrder.indexOf(right.label);
  if (leftIndex !== -1 || rightIndex !== -1) {
    return (leftIndex === -1 ? fixedOrder.length : leftIndex) - (rightIndex === -1 ? fixedOrder.length : rightIndex);
  }
  return fuelNameCollator.compare(left.label, right.label);
}

function mergeFuelStockItem(base, next) {
  return {
    ...base,
    fuelCode: base.fuelCode || next.fuelCode,
    fuelName: base.fuelName || next.fuelName,
    capacityLiters: sumFuelMetric(base.capacityLiters, next.capacityLiters),
    physicalVolumeLiters: sumFuelMetric(base.physicalVolumeLiters, next.physicalVolumeLiters),
    deadRestLiters: sumFuelMetric(base.deadRestLiters, next.deadRestLiters),
    availableVolumeLiters: sumFuelMetric(base.availableVolumeLiters, next.availableVolumeLiters),
    capacityTons: sumFuelMetric(base.capacityTons, next.capacityTons),
    physicalVolumeTons: sumFuelMetric(base.physicalVolumeTons, next.physicalVolumeTons),
    deadRestTons: sumFuelMetric(base.deadRestTons, next.deadRestTons),
    availableVolumeTons: sumFuelMetric(base.availableVolumeTons, next.availableVolumeTons),
    tanksCount: sumFuelMetric(base.tanksCount, next.tanksCount),
    percentage: null,
    rawFillPercent: null,
    capacityExceeded: Boolean(base.capacityExceeded || next.capacityExceeded),
    onDeadStock: Boolean(base.onDeadStock || next.onDeadStock),
    isLow: Boolean(base.isLow || next.isLow),
  };
}

function fuelStockTone(percentage) {
  const numeric = numericOrNull(percentage);
  if (numeric === null) return "empty";
  if (numeric < 30) return "red";
  if (numeric <= 70) return "amber";
  return "green";
}

function fuelStockToneLabel(group) {
  if (group.onDeadStock) return "Отсутствует";
  if (group.capacityExceeded) return "Проверить данные";
  if (group.tone === "red") return "Внимание <30%";
  if (group.tone === "amber") return "Рабочий 30–70%";
  if (group.tone === "green") return "Норма >70%";
  return "Нет данных";
}

function normalizeFuelStockGroups(items = []) {
  const grouped = new Map();

  items.forEach((item) => {
    const label = canonicalFuelLabel(item);
    const key = normalizeFuelToken(label);
    if (!key) return;
    const existing = grouped.get(key);
    grouped.set(key, existing ? mergeFuelStockItem(existing, item) : { ...item, label });
  });

  return Array.from(grouped, ([key, item]) => {
    const capacity = fuelCapacityTons(item);
    const available = numericOrNull(item.availableVolumeLiters);
    const suppliedPercentage = numericOrNull(item.percentage);
    const availableTons = fuelAvailableTons(item);
    const calculatedPercentage = capacity && capacity > 0 && availableTons !== null ? (availableTons / capacity) * 100 : null;
    const rawPercentage = numericOrNull(item.rawFillPercent) ?? suppliedPercentage ?? calculatedPercentage;
    const percentage = rawPercentage === null ? null : Math.max(0, Math.min(100, rawPercentage));
    const capacityExceeded = Boolean(item.capacityExceeded) || (rawPercentage !== null && rawPercentage > 100.1);
    const physicalTons = fuelPhysicalTons(item);
    const deadRestTons = fuelDeadRestTons(item);
    const onDeadStock = Boolean(item.onDeadStock)
      || (availableTons !== null && availableTons <= 0.0001 && physicalTons > 0 && deadRestTons > 0);
    const tone = capacityExceeded ? "red" : fuelStockTone(percentage);
    const flaggedLow = item.isLow === true || item.isLow === 1 || String(item.isLow).toLowerCase() === "true";
    const isLow = onDeadStock || flaggedLow || (numericOrNull(percentage) !== null && percentage < 20);

    return {
      key,
      label: item.label,
      item,
      hasData: available !== null || availableTons !== null,
      percentage,
      rawPercentage,
      capacityExceeded,
      onDeadStock,
      tone,
      isLow,
    };
  }).filter((group) => group.hasData).sort(compareFuelGroups);
}

function fuelCapacityTons(item) {
  const tons = numericOrNull(item?.capacityTons);
  if (tons !== null) return tons;
  const legacyValue = numericOrNull(item?.capacityLiters);
  return legacyValue === null ? null : legacyValue / 1000;
}

function fuelAvailableTons(item) {
  const tons = numericOrNull(item?.availableVolumeTons ?? item?.availableTons);
  if (tons !== null) return tons;
  const legacyValue = numericOrNull(item?.availableVolumeLiters ?? item?.availableLiters);
  return legacyValue === null ? null : legacyValue / 1000;
}

function fuelPhysicalTons(item) {
  const tons = numericOrNull(item?.physicalVolumeTons ?? item?.volumeTons);
  if (tons !== null) return tons;
  const legacyValue = numericOrNull(item?.physicalVolumeLiters ?? item?.volumeLiters);
  return legacyValue === null ? null : legacyValue / 1000;
}

function fuelDeadRestTons(item) {
  const tons = numericOrNull(item?.deadRestTons);
  if (tons !== null) return tons;
  const legacyValue = numericOrNull(item?.deadRestLiters);
  return legacyValue === null ? null : legacyValue / 1000;
}

function formatFuelTons(value) {
  const numeric = numericOrNull(value);
  if (numeric === null) return "—";
  return `${numeric.toLocaleString("ru-RU", { maximumFractionDigits: 2 })} т`;
}

function formatFuelPercent(value) {
  const numeric = numericOrNull(value);
  if (numeric === null) return "—";
  return `${numeric.toLocaleString("ru-RU", { maximumFractionDigits: Number.isInteger(numeric) ? 0 : 1 })}%`;
}

function clampFuelPercent(value) {
  const numeric = numericOrNull(value);
  if (numeric === null) return 0;
  return Math.max(0, Math.min(100, numeric));
}

function formatFuelStockTimestamp(value) {
  if (!value) return "";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return String(value);
  return date.toLocaleString("ru-RU", {
    day: "2-digit",
    month: "short",
    year: "numeric",
    hour: "2-digit",
    minute: "2-digit",
  });
}

function formatOutageDate(value) {
  if (!value) return "Дата не указана";
  const match = String(value).match(/^(\d{4})-(\d{2})-(\d{2})$/);
  const date = match
    ? new Date(Number(match[1]), Number(match[2]) - 1, Number(match[3]))
    : new Date(value);
  if (Number.isNaN(date.getTime())) return String(value);
  return date.toLocaleDateString("ru-RU", { day: "numeric", month: "long", year: "numeric" });
}

function formatOutageDuration(value) {
  const numeric = numericOrNull(value);
  if (numeric === null) return "—";
  return `${numeric.toLocaleString("ru-RU", { maximumFractionDigits: 1 })} ч`;
}

function formatOutageSales(value) {
  const numeric = numericOrNull(value);
  if (numeric === null) return "—";
  return `${numeric.toLocaleString("ru-RU", { maximumFractionDigits: 0 })} л`;
}

function formatOutageCount(value) {
  const count = Math.max(0, Number(value) || 0);
  const mod100 = count % 100;
  const mod10 = count % 10;
  const noun = mod100 >= 11 && mod100 <= 14
    ? "событий"
    : mod10 === 1
      ? "событие"
      : mod10 >= 2 && mod10 <= 4
        ? "события"
        : "событий";
  return `${asInt(count)} ${noun}`;
}

function formatFuelTypeCount(value) {
  const count = Math.max(0, Number(value) || 0);
  const mod100 = count % 100;
  const mod10 = count % 10;
  const noun = mod100 >= 11 && mod100 <= 14
    ? "видов топлива"
    : mod10 === 1
      ? "вид топлива"
      : mod10 >= 2 && mod10 <= 4
        ? "вида топлива"
        : "видов топлива";
  return `${asInt(count)} ${noun}`;
}

function outageTimeRange(item) {
  const start = String(item?.startTime || "").trim();
  const end = String(item?.endTime || "").trim();
  if (start && end) return `${start}–${end}`;
  if (start) return `с ${start}`;
  if (end) return `до ${end}`;
  return "Время не указано";
}

function isOutageDayEnd(item) {
  return String(item?.endTime || "").trim().slice(0, 5) === "23:59";
}

function compareOutageItems(left, right) {
  const leftKey = `${left?.date || ""}T${left?.startTime || "00:00"}`;
  const rightKey = `${right?.date || ""}T${right?.startTime || "00:00"}`;
  return rightKey.localeCompare(leftKey, "ru");
}

function distanceKm(from, station) {
  if (!from || !hasValidPoint(station)) return Number.POSITIVE_INFINITY;
  const toRad = (value) => (value * Math.PI) / 180;
  const earthRadiusKm = 6371;
  const lat1 = toRad(Number(from.lat));
  const lat2 = toRad(Number(station.lat));
  const deltaLat = toRad(Number(station.lat) - Number(from.lat));
  const deltaLon = toRad(Number(station.lon) - Number(from.lon));
  const a = Math.sin(deltaLat / 2) ** 2 + Math.cos(lat1) * Math.cos(lat2) * Math.sin(deltaLon / 2) ** 2;
  return 2 * earthRadiusKm * Math.atan2(Math.sqrt(a), Math.sqrt(1 - a));
}

function nearestStation(location, stations) {
  return stations.reduce(
    (best, station) => {
      const distance = distanceKm(location, station);
      return distance < best.distance ? { station, distance } : best;
    },
    { station: null, distance: Number.POSITIVE_INFINITY },
  );
}

function formatDistance(distance) {
  if (!Number.isFinite(distance)) return "";
  if (distance < 1) return `${Math.max(1, Math.round(distance * 1000))} м`;
  return `${distance.toLocaleString("ru-RU", { maximumFractionDigits: distance < 10 ? 1 : 0 })} км`;
}

function metricById(metrics, id) {
  return metrics?.find((metric) => metric.id === id);
}

function metricDisplay(metrics, id) {
  const metric = metricById(metrics, id);
  return metric ? formatKpiValue(metric.value, metric.unit) : "—";
}

function formatPersonName(value) {
  const parts = String(value || "").trim().split(/\s+/).filter(Boolean);
  if (parts.length < 2) return parts[0] || "—";
  const initials = parts
    .slice(1)
    .flatMap((part) => Array.from(part.matchAll(/\p{L}/gu), (match) => match[0]))
    .slice(0, 2);
  return initials.length
    ? `${parts[0]} ${initials.map((letter) => `${letter.toLocaleUpperCase("ru-RU")}.`).join("")}`
    : parts[0];
}

function analyticsGroupLabel(label, groupBy) {
  return groupBy === "regionalManager" || groupBy === "territoryManager" ? formatPersonName(label) : label;
}

function stableNumber(seed, minimum, maximum) {
  const text = String(seed);
  let hash = 2166136261;
  for (let index = 0; index < text.length; index += 1) {
    hash ^= text.charCodeAt(index);
    hash = Math.imul(hash, 16777619);
  }
  const normalized = Math.abs(hash >>> 0);
  return minimum + (normalized % (maximum - minimum + 1));
}

const noInfoKpiMetrics = [
  ["revenue", "Выручка", "₽"],
  ["fuelVolume", "Объем топлива", "л"],
  ["checks", "Чеки", "шт"],
  ["avgCheck", "Средний чек", "₽"],
].map(([id, label, unit]) => ({
  id,
  label,
  value: undefined,
  unit,
  momPct: undefined,
  yoyPct: undefined,
}));

function monthDates(period) {
  const [year, month] = period.split("-").map(Number);
  const days = new Date(year, month, 0).getDate();
  return Array.from({ length: days }, (_, index) => new Date(year, month - 1, index + 1));
}

function noInfoStaffPayload(ksss, period) {
  const days = monthDates(period).map((date) => {
    const dateIso = date.toISOString().slice(0, 10);
    return {
      date: dateIso,
      label: formatWeekday(dateIso),
      day: undefined,
      night: undefined,
    };
  });
  const todayIso = new Date().toISOString().slice(0, 10);
  const today = days.find((day) => day.date === todayIso) || days[0];
  return {
    ksss,
    period,
    source: "no-info",
    updatedAt: new Date().toISOString(),
    staffTotal: undefined,
    today,
    days,
  };
}

function motionPreset(reduceMotion) {
  return reduceMotion
    ? { initial: false, animate: {}, exit: {}, transition: { duration: 0 } }
    : {
        initial: { opacity: 0, y: 14, scale: 0.99 },
        animate: { opacity: 1, y: 0, scale: 1 },
        exit: { opacity: 0, y: -10, scale: 0.992 },
        transition: { type: "spring", stiffness: 260, damping: 30, mass: 0.85 },
      };
}

function stationOptionText(station) {
  return [station.ksss, station.stationNumber, station.name, station.subject, station.city, station.address]
    .filter(Boolean)
    .join(" ")
    .toLowerCase();
}

function stationTitle(station) {
  return station ? `${station.ksss} · ${station.name || `АЗС № ${station.stationNumber}`}` : "";
}

function formatShortDate(value) {
  const date = new Date(value);
  return date.toLocaleDateString("ru-RU", { day: "numeric", month: "short" });
}

function formatWeekday(value) {
  const date = new Date(value);
  return date.toLocaleDateString("ru-RU", { weekday: "short" }).replace(".", "");
}

function emitAuthRequired() {
  window.dispatchEvent(new CustomEvent("azs:auth-required"));
}

async function purgePrivateCaches() {
  if (!("caches" in window)) return;
  const names = await caches.keys();
  await Promise.all(names.filter((name) => name === "azs-api" || name === "azs-data").map((name) => caches.delete(name)));
}

function fetchJson(url, signal) {
  return fetch(url, { signal, credentials: "include", headers: { Accept: "application/json" } }).then((response) => {
    if (response.status === 401) {
      emitAuthRequired();
      throw new Error("AUTH_REQUIRED");
    }
    if (response.status === 404) return null;
    if (!response.ok) throw new Error(`REQUEST_FAILED_${response.status}`);
    return response.json();
  });
}

// ---------------------------------------------------------------------------
// Usage metrics: visit heartbeat + in-app events (screen views, key actions).
// Data is stored server-side per authenticated user (see /api/metrics/*).
// ---------------------------------------------------------------------------
const METRICS_HEARTBEAT_MS = 60_000;
const metricsState = { queue: [], flushTimer: null, deviceSent: false };

function metricsDeviceContext() {
  let tz = "";
  try {
    tz = Intl.DateTimeFormat().resolvedOptions().timeZone || "";
  } catch {
    tz = "";
  }
  return {
    ua: (navigator.userAgent || "").slice(0, 400),
    platform: (navigator.platform || "").slice(0, 80),
    screen: `${window.screen?.width || 0}x${window.screen?.height || 0}`,
    pwa: isStandaloneApp(),
    lang: (navigator.language || "").slice(0, 20),
    tz: tz.slice(0, 60),
  };
}

function postMetrics(url, payload, { beacon = false } = {}) {
  const body = JSON.stringify(payload);
  if (beacon && navigator.sendBeacon) {
    navigator.sendBeacon(url, new Blob([body], { type: "application/json" }));
    return;
  }
  fetch(url, {
    method: "POST",
    credentials: "include",
    keepalive: beacon,
    headers: { "Content-Type": "application/json" },
    body,
  }).catch(() => {});
}

function flushMetrics({ beacon = false } = {}) {
  if (metricsState.flushTimer) {
    window.clearTimeout(metricsState.flushTimer);
    metricsState.flushTimer = null;
  }
  if (!metricsState.queue.length) return;
  const payload = { events: metricsState.queue.splice(0, 50) };
  if (!metricsState.deviceSent) {
    payload.device = metricsDeviceContext();
    metricsState.deviceSent = true;
  }
  postMetrics("/api/metrics/events", payload, { beacon });
}

function trackEvent(event, screen = "", detail = "") {
  metricsState.queue.push({
    event: String(event).slice(0, 40),
    screen: String(screen || "").slice(0, 80),
    detail: String(detail || "").slice(0, 200),
  });
  if (metricsState.queue.length >= 20) {
    flushMetrics();
    return;
  }
  if (metricsState.flushTimer) return;
  metricsState.flushTimer = window.setTimeout(() => {
    metricsState.flushTimer = null;
    flushMetrics();
  }, 5_000);
}

function sendMetricsBeat(screen = "") {
  const payload = { screen: String(screen || "").slice(0, 80) };
  if (!metricsState.deviceSent) {
    payload.device = metricsDeviceContext();
    metricsState.deviceSent = true;
  }
  postMetrics("/api/metrics/beat", payload, { beacon: false });
}

function useFuelStock(ksss) {
  const [stockState, setStockState] = useState({ status: "idle", data: null, error: "", refreshing: false });
  const controllerRef = useRef(null);
  const requestRef = useRef(0);
  const intervalRef = useRef(null);

  const loadStock = useCallback(
    (background = false) => {
      if (!ksss) {
        setStockState({ status: "no-data", data: null, error: "", refreshing: false });
        return;
      }

      requestRef.current += 1;
      const requestId = requestRef.current;
      controllerRef.current?.abort();
      const controller = new AbortController();
      controllerRef.current = controller;

      setStockState((previous) => {
        const keepCurrent = background && (previous.data || previous.status === "no-data");
        return {
          status: keepCurrent ? previous.status : "loading",
          data: keepCurrent ? previous.data : null,
          error: "",
          refreshing: Boolean(keepCurrent),
        };
      });

      fetchJson(`/api/stations/${encodeURIComponent(ksss)}/fuel-stock`, controller.signal)
        .then((data) => {
          if (requestRef.current !== requestId) return;
          if (!data || !Array.isArray(data.items) || data.items.length === 0) {
            setStockState({ status: "no-data", data: data || null, error: "", refreshing: false });
            return;
          }
          setStockState({ status: "ready", data, error: "", refreshing: false });
        })
        .catch((error) => {
          if (error.name === "AbortError" || error.message === "AUTH_REQUIRED") return;
          if (requestRef.current !== requestId) return;
          setStockState((previous) => {
            if (background && (previous.data || previous.status === "no-data")) {
              return {
                ...previous,
                error: "Не удалось обновить остатки. Показаны последние полученные данные.",
                refreshing: false,
              };
            }
            return { status: "error", data: null, error: error.message, refreshing: false };
          });
        });
    },
    [ksss],
  );

  useEffect(() => {
    if (!ksss) {
      setStockState({ status: "no-data", data: null, error: "", refreshing: false });
      return undefined;
    }

    const refreshVisibleStock = () => {
      if (document.visibilityState === "visible") loadStock(true);
    };

    loadStock(false);
    intervalRef.current = window.setInterval(refreshVisibleStock, FUEL_STOCK_REFRESH_MS);
    document.addEventListener("visibilitychange", refreshVisibleStock);

    return () => {
      window.clearInterval(intervalRef.current);
      document.removeEventListener("visibilitychange", refreshVisibleStock);
      controllerRef.current?.abort();
    };
  }, [ksss, loadStock]);

  return { stockState, refresh: loadStock };
}

function useFuelOutages(ksss) {
  const [outageState, setOutageState] = useState({ status: "idle", data: null, error: "" });
  const controllerRef = useRef(null);
  const requestRef = useRef(0);

  const loadOutages = useCallback(() => {
    if (!ksss) {
      setOutageState({ status: "ready", data: null, error: "" });
      return;
    }

    requestRef.current += 1;
    const requestId = requestRef.current;
    controllerRef.current?.abort();
    const controller = new AbortController();
    controllerRef.current = controller;
    setOutageState({ status: "loading", data: null, error: "" });

    fetchJson(`/api/fuel-outages?ksss=${encodeURIComponent(ksss)}&limit=1000`, controller.signal)
      .then((data) => {
        if (requestRef.current !== requestId) return;
        setOutageState({ status: "ready", data: data || null, error: "" });
      })
      .catch((error) => {
        if (error.name === "AbortError" || error.message === "AUTH_REQUIRED") return;
        if (requestRef.current !== requestId) return;
        setOutageState({ status: "error", data: null, error: error.message });
      });
  }, [ksss]);

  useEffect(() => {
    loadOutages();
    return () => controllerRef.current?.abort();
  }, [loadOutages]);

  return { outageState, refresh: loadOutages };
}

// Registry fuel filter. The server decides which stations qualify: available volume must
// reach FUEL_STOCK_AVAILABLE_MIN_PERCENT of the dispensable capacity (tank capacity minus
// the static dead rest), so a station standing on its dead rest never matches. Several
// fuels intersect — the answer is "where can I refuel all of these", not "any of these".
// Options come from the snapshot, never from a hardcoded list.
const emptyKsssSet = new Set();

// Display form of a canonical fuel code: "АБ95 ЭКТО" -> "АБ-95 ЭКТО". Storage and API
// keep the canonical code, only the label carries the dash.
function fuelDisplayName(canonicalFuel) {
  return String(canonicalFuel || "").replace(/^АБ(?=\d)/, "АБ-");
}

function fuelSelectionKey(fuels) {
  return (Array.isArray(fuels) ? fuels : []).filter(Boolean).slice().sort().join("|");
}

function useFuelAvailability(fuels, enabled) {
  const selection = useMemo(() => (Array.isArray(fuels) ? fuels.filter(Boolean) : []), [fuels]);
  const selectionKey = fuelSelectionKey(selection);
  const [options, setOptions] = useState({ status: "idle", fuels: [], minPercent: null });
  const [matches, setMatches] = useState({ status: "idle", key: "", ksss: emptyKsssSet, meta: null, error: "" });
  const requestRef = useRef(0);

  useEffect(() => {
    if (!enabled) return undefined;
    const controller = new AbortController();
    setOptions((previous) => (previous.status === "ready" ? previous : { ...previous, status: "loading" }));

    fetchJson("/api/fuel-stock/fuels", controller.signal)
      .then((data) => {
        setOptions({
          status: "ready",
          fuels: Array.isArray(data?.fuels) ? data.fuels : [],
          minPercent: numericOrNull(data?.minPercent),
        });
      })
      .catch((error) => {
        if (error.name === "AbortError" || error.message === "AUTH_REQUIRED") return;
        setOptions({ status: "error", fuels: [], minPercent: null });
      });

    return () => controller.abort();
  }, [enabled]);

  useEffect(() => {
    if (!selectionKey || !enabled) {
      requestRef.current += 1;
      setMatches({ status: "idle", key: "", ksss: emptyKsssSet, meta: null, error: "" });
      return undefined;
    }

    requestRef.current += 1;
    const requestId = requestRef.current;
    const controller = new AbortController();
    setMatches({ status: "loading", key: selectionKey, ksss: emptyKsssSet, meta: null, error: "" });

    const query = selectionKey
      .split("|")
      .map((item) => `fuel=${encodeURIComponent(item)}`)
      .join("&");

    fetchJson(`/api/fuel-stock/available?${query}`, controller.signal)
      .then((data) => {
        if (requestRef.current !== requestId) return;
        if (!data || !Array.isArray(data.ksss)) {
          setMatches({ status: "no-data", key: selectionKey, ksss: emptyKsssSet, meta: data || null, error: "" });
          return;
        }
        setMatches({
          status: "ready",
          key: selectionKey,
          ksss: new Set(data.ksss.map((code) => String(code))),
          meta: data,
          error: "",
        });
      })
      .catch((error) => {
        if (error.name === "AbortError" || error.message === "AUTH_REQUIRED") return;
        if (requestRef.current !== requestId) return;
        setMatches({
          status: "error",
          key: selectionKey,
          ksss: emptyKsssSet,
          meta: null,
          error: "Не удалось получить остатки топлива. Фильтр по топливу временно недоступен.",
        });
      });

    return () => controller.abort();
  }, [selectionKey, enabled]);

  // A stale reply must never silently narrow the registry: the filter stays "pending"
  // until the answer for the current selection has arrived.
  const active = Boolean(selectionKey) && enabled;
  return {
    options,
    matches,
    selection,
    ready: active && matches.status === "ready" && matches.key === selectionKey,
    pending: active && (matches.status === "loading" || matches.key !== selectionKey),
    failed: active && matches.status === "error",
  };
}

async function authJson(url, options = {}) {
  const { timeoutMs = 15_000, signal: externalSignal, ...requestOptions } = options;
  const controller = new AbortController();
  let timedOut = false;
  const abortFromExternal = () => controller.abort();
  if (externalSignal?.aborted) controller.abort();
  externalSignal?.addEventListener("abort", abortFromExternal, { once: true });
  const timeoutId = window.setTimeout(() => {
    timedOut = true;
    controller.abort();
  }, timeoutMs);

  try {
    const response = await fetch(url, {
      credentials: "include",
      ...requestOptions,
      signal: controller.signal,
      headers: {
        Accept: "application/json",
        ...(requestOptions.body ? { "Content-Type": "application/json" } : {}),
        ...(requestOptions.headers || {}),
      },
    });
    const data = await response.json().catch(() => ({}));
    if (!response.ok) {
      const error = new Error(data.detail || `REQUEST_FAILED_${response.status}`);
      error.status = response.status;
      error.payload = data;
      throw error;
    }
    return data;
  } catch (error) {
    if (timedOut) {
      const timeoutError = new Error("Сервер не ответил вовремя. Проверьте соединение и повторите.");
      timeoutError.code = "REQUEST_TIMEOUT";
      throw timeoutError;
    }
    throw error;
  } finally {
    window.clearTimeout(timeoutId);
    externalSignal?.removeEventListener("abort", abortFromExternal);
  }
}

function isStandaloneApp() {
  return Boolean(
    window.matchMedia?.("(display-mode: standalone)").matches ||
      window.matchMedia?.("(display-mode: window-controls-overlay)").matches ||
      window.navigator.standalone,
  );
}

function initialViewMode() {
  const view = new URL(window.location.href).searchParams.get("view");
  return viewIds.has(view) ? view : "home";
}

function groupTop(stations, getter, limit = 6) {
  const counts = new Map();
  stations.forEach((station) => {
    const key = getter(station) || "Не заполнено";
    counts.set(key, (counts.get(key) || 0) + 1);
  });
  return [...counts.entries()]
    .map(([name, value]) => ({ name, value }))
    .sort((a, b) => b.value - a.value || a.name.localeCompare(b.name, "ru"))
    .slice(0, limit);
}

function missingResponsible(station) {
  return !station.manager && !station.regionalManager && !station.territoryManager && !station.seniorOperator;
}

function missingContactPhone(station) {
  return !bestPhone(station);
}

function App() {
  const reduceMotion = useReducedMotion();
  const [auth, setAuth] = useState({ status: "checking", user: null, error: "" });
  const [payload, setPayload] = useState({ meta: null, stations: [] });
  const [query, setQuery] = useState("");
  const [filters, setFilters] = useState(defaultFilters);
  const [mode, setMode] = useState(() => initialViewMode());
  const aiStatus = useAiStatus();
  // Необязательные разделы скрыты, пока бэкенд не подтвердил, что они есть.
  const navItems = useMemo(
    () => viewItems.filter((item) => !item.optional || (item.id === "ai" && aiStatus?.enabled)),
    [aiStatus],
  );
  const [selectedId, setSelectedId] = useState("");
  const [selectionMode, setSelectionMode] = useState("auto");
  const [registryCompact, setRegistryCompact] = useState(false);
  const [registryDensity, setRegistryDensity] = useState("comfortable");
  const [favorites, setFavorites] = useState(() => JSON.parse(localStorage.getItem("azs:favorites") || "[]"));
  const [showFilters, setShowFilters] = useState(false);
  const [detailOpen, setDetailOpen] = useState(false);
  const [installPrompt, setInstallPrompt] = useState(null);
  const [standaloneApp, setStandaloneApp] = useState(() => isStandaloneApp());
  const previousModeRef = useRef("list");

  useEffect(() => {
    let alive = true;
    authJson("/api/auth/me", { timeoutMs: 8_000 })
      .then((data) => {
        if (alive) setAuth({ status: "ready", user: data.user, error: "" });
      })
      .catch((authError) => {
        purgePrivateCaches();
        if (!alive) return;
        const connectionError = authError?.status === 401
          ? ""
          : authError?.code === "REQUEST_TIMEOUT"
            ? "Не удалось быстро проверить сессию. Войдите снова."
            : "Не удалось связаться с сервером. Проверьте соединение.";
        setAuth({ status: "guest", user: null, error: connectionError });
      });
    return () => {
      alive = false;
    };
  }, []);

  useEffect(() => {
    function handleAuthRequired() {
      purgePrivateCaches();
      setAuth({ status: "guest", user: null, error: "Сессия истекла. Войдите снова." });
      setPayload({ meta: null, stations: [] });
      setSelectedId("");
      setDetailOpen(false);
    }

    window.addEventListener("azs:auth-required", handleAuthRequired);
    return () => window.removeEventListener("azs:auth-required", handleAuthRequired);
  }, []);

  // Visit tracking: heartbeat while the tab is visible, flush events on hide/close.
  useEffect(() => {
    if (!auth.user) return undefined;
    metricsState.deviceSent = false;
    sendMetricsBeat("session_start");

    const intervalId = window.setInterval(() => {
      if (document.visibilityState === "visible") sendMetricsBeat();
    }, METRICS_HEARTBEAT_MS);

    function handleVisibility() {
      if (document.visibilityState === "hidden") {
        flushMetrics({ beacon: true });
        postMetrics("/api/metrics/beat", { screen: "" }, { beacon: true });
      } else {
        sendMetricsBeat();
      }
    }

    function handlePageHide() {
      flushMetrics({ beacon: true });
    }

    document.addEventListener("visibilitychange", handleVisibility);
    window.addEventListener("pagehide", handlePageHide);

    return () => {
      window.clearInterval(intervalId);
      document.removeEventListener("visibilitychange", handleVisibility);
      window.removeEventListener("pagehide", handlePageHide);
      flushMetrics();
    };
  }, [auth.user?.id]);

  // Screen view tracking for the top-level navigation.
  useEffect(() => {
    if (auth.user) trackEvent("screen_view", mode);
  }, [auth.user?.id, mode]);

  useEffect(() => {
    if (!auth.user) {
      setPayload({ meta: null, stations: [] });
      return undefined;
    }

    const controller = new AbortController();
    fetchJson("/api/stations", controller.signal)
      .then((data) => {
        setPayload(data);
      })
      .catch((error) => {
        if (error.name === "AbortError" || error.message === "AUTH_REQUIRED") return;
        setPayload({ meta: { count: 0, error: error.message }, stations: [] });
      });

    return () => controller.abort();
  }, [auth.user]);

  useEffect(() => {
    localStorage.setItem("azs:favorites", JSON.stringify(favorites));
  }, [favorites]);

  useEffect(() => {
    const standaloneQuery = window.matchMedia?.("(display-mode: standalone)");
    const overlayQuery = window.matchMedia?.("(display-mode: window-controls-overlay)");

    function refreshStandalone() {
      setStandaloneApp(isStandaloneApp());
    }

    function handleBeforeInstallPrompt(event) {
      event.preventDefault();
      setInstallPrompt(event);
    }

    function handleInstalled() {
      setInstallPrompt(null);
      setStandaloneApp(true);
    }

    window.addEventListener("beforeinstallprompt", handleBeforeInstallPrompt);
    window.addEventListener("appinstalled", handleInstalled);
    standaloneQuery?.addEventListener?.("change", refreshStandalone);
    overlayQuery?.addEventListener?.("change", refreshStandalone);
    standaloneQuery?.addListener?.(refreshStandalone);
    overlayQuery?.addListener?.(refreshStandalone);
    refreshStandalone();

    return () => {
      window.removeEventListener("beforeinstallprompt", handleBeforeInstallPrompt);
      window.removeEventListener("appinstalled", handleInstalled);
      standaloneQuery?.removeEventListener?.("change", refreshStandalone);
      overlayQuery?.removeEventListener?.("change", refreshStandalone);
      standaloneQuery?.removeListener?.(refreshStandalone);
      overlayQuery?.removeListener?.(refreshStandalone);
    };
  }, []);

  useEffect(() => {
    setSelectionMode("auto");
    setSelectedId("");
    setRegistryCompact(false);
  }, [query, filters.npo, filters.subject, filters.status, filters.type, filters.location, filters.service, filters.quality, filters.fuel, filters.favorites]);

  useEffect(() => {
    if (mode !== "map") previousModeRef.current = mode;
  }, [mode]);

  // The registry and analytics must reflect the complete operational selection.
  // Coordinates are required only by StationMap, which filters its own points.
  const stations = useMemo(() => payload.stations, [payload.stations]);
  const excludedNoCoords = useMemo(() => stations.filter((station) => !hasValidPoint(station)).length, [stations]);
  const options = useMemo(
    () => ({
      npo: uniqueOptions(stations, "npo"),
      subject: uniqueOptions(stations, "subject"),
      status: uniqueOptions(stations, "status"),
      type: uniqueOptions(stations, "type"),
      location: uniqueOptions(stations, "location"),
    }),
    [stations],
  );

  // Fuel availability is answered by the server, so it applies only once the reply for the
  // selected fuel is in. While it is in flight the registry shows its loading state, and if
  // the request failed the filter is dropped rather than silently emptying the registry.
  const fuelAvailability = useFuelAvailability(filters.fuel, auth.status === "ready");
  const fuelMatches = fuelAvailability.ready ? fuelAvailability.matches.ksss : null;
  const favoriteSet = useMemo(() => new Set(favorites), [favorites]);

  const filtered = useMemo(() => {
    if (fuelAvailability.pending) return [];
    const needle = query.trim().toLowerCase();
    return stations.filter((station) => {
      if (needle && !station.search.includes(needle)) return false;
      if (filters.npo && station.npo !== filters.npo) return false;
      if (filters.subject && station.subject !== filters.subject) return false;
      if (filters.status && station.status !== filters.status) return false;
      if (filters.type && station.type !== filters.type) return false;
      if (filters.location && station.location !== filters.location) return false;
      if (filters.service === "shop" && !station.flags.hasShop) return false;
      if (filters.service === "cafe" && !station.flags.hasCafe) return false;
      if (filters.service === "toilet" && !station.flags.hasToilet) return false;
      if (filters.service === "landmark" && !station.flags.landmark) return false;
      if (filters.quality === "issues" && station.qualityIssues.length === 0) return false;
      if (filters.favorites && !favoriteSet.has(station.id)) return false;
      if (fuelMatches && !fuelMatches.has(String(station.ksss))) return false;
      return true;
    });
  }, [stations, query, filters, favoriteSet, fuelMatches, fuelAvailability.pending]);

  const selected = filtered.find((station) => station.id === selectedId) || null;
  const detailVisible = Boolean(selected && detailOpen);
  const previewVisible = Boolean(selected && !detailOpen && mode === "map");

  const metrics = useMemo(() => {
    const active = stations.filter((station) => station.flags.active).length;
    const cafe = stations.filter((station) => station.flags.hasCafe).length;
    const toilet = stations.filter((station) => station.flags.hasToilet).length;
    return { active, cafe, toilet };
  }, [stations]);
  const issueCount = useMemo(() => filtered.filter((station) => station.qualityIssues.length > 0).length, [filtered]);
  const loading = Boolean(auth.user) && payload.meta === null;
  const viewMotion = motionPreset(reduceMotion);
  const mapSheetMotion = reduceMotion
    ? { initial: false, animate: {}, exit: {}, transition: { duration: 0 } }
    : {
        initial: { y: "100%" },
        animate: { y: 0 },
        exit: { y: "100%" },
        transition: { type: "spring", stiffness: 300, damping: 34, mass: 0.9 },
      };

  function setFilter(key, value) {
    setFilters((current) => ({ ...current, [key]: value }));
    const label = Array.isArray(value) ? value.join("+") : value;
    trackEvent("filter", mode, `${key}:${label || "clear"}`);
  }

  function openFavorites() {
    setFilters((current) => ({ ...current, favorites: true }));
    changeMode("list");
  }

  function toggleFavorite(id) {
    setFavorites((current) => (current.includes(id) ? current.filter((item) => item !== id) : [...current, id]));
  }

  function selectStation(id) {
    setSelectedId(id);
    setSelectionMode("manual");
    setDetailOpen(true);
    trackEvent("station_open", mode, String(id));
  }

  function previewStation(id) {
    setSelectedId(id);
    setSelectionMode("manual");
    setDetailOpen(false);
  }

  function openDetail() {
    if (selectedId) setDetailOpen(true);
  }

  function closeDetailSheet() {
    setDetailOpen(false);
  }

  useEffect(() => {
    if (mode === "ai" && aiStatus && !aiStatus.enabled) setMode("home");
  }, [mode, aiStatus]);

  function changeMode(nextMode, { closeDetail = true } = {}) {
    setMode(nextMode);
    setRegistryCompact(false);
    if (closeDetail && selectedId) setDetailOpen(false);
  }

  function handleRegistryScroll(scrollTop) {
    setRegistryCompact(scrollTop > 28);
  }

  async function installApp() {
    if (!installPrompt) return;
    const prompt = installPrompt;
    setInstallPrompt(null);
    try {
      await prompt.prompt();
      const choice = await prompt.userChoice;
      if (choice?.outcome === "accepted") setStandaloneApp(true);
    } catch (error) {
      setInstallPrompt(prompt);
    }
  }

  function handleAuthenticated(user) {
    setAuth({ status: "ready", user, error: "" });
  }

  async function handleLogout() {
    try {
      await authJson("/api/auth/logout", { method: "POST" });
    } catch (error) {
      // Local logout still clears the UI and private caches if the network request fails.
    }
    await purgePrivateCaches();
    setAuth({ status: "guest", user: null, error: "" });
    setPayload({ meta: null, stations: [] });
    setSelectedId("");
    setDetailOpen(false);
  }

  const canInstallApp = Boolean(installPrompt && !standaloneApp);
  const isAdminMode = mode === "admin";
  const isAiMode = mode === "ai";
  const [aiDrawer, setAiDrawer] = useState(false);
  // Поиск, метрики и фильтры реестра к этим разделам не применяются:
  // ИИ отвечает по витрине и области данных, а не по текущей выборке.
  const isControlMode = mode === "quality" || isAdminMode || isAiMode;

  if (auth.status === "checking") {
    return <AuthLoading />;
  }

  if (!auth.user) {
    return <AuthScreen initialError={auth.error} onAuthenticated={handleAuthenticated} />;
  }

  return (
    <>
      <a href="#main-content" className="skip-link">Перейти к содержимому</a>
      <main id="main-content" className={`app-shell ${mode}-mode ${detailVisible ? "" : "no-detail"} ${mode === "list" && registryCompact ? "registry-compact" : ""}`}>
      <section className="workspace">
        {mode !== "home" && (
          <>
            <header className="topbar">
              <div>
                <h1>{isAdminMode ? "Администрирование" : isAiMode ? "ИИ-аналитик" : isControlMode ? "Контроль АЗС" : "АЗС ЛУКОЙЛ"}</h1>
                {/* В разделе ИИ подписи нет: на телефоне эта строка съедала
                    высоту, а ничего нового не сообщала. */}
                {!isAiMode && (
                <p>
                  {isAdminMode
                    ? "Пользователи и статистика использования"
                    : isControlMode
                    ? `${asInt(stations.length)} объектов в контуре контроля`
                    : payload.meta
                    ? `${asInt(stations.length)} на карте${excludedNoCoords ? ` · скрыто ${asInt(excludedNoCoords)}` : ""}`
                    : "Загрузка данных"}
                </p>
                )}
              </div>
              <div className="topbar-actions">
                {/* История диалогов живёт в шапке: в разделе на телефоне
                    отдельная строка под неё не окупалась. */}
                {isAiMode && (
                  <button
                    className="icon-button ai-drawer-btn"
                    type="button"
                    onClick={() => setAiDrawer(true)}
                    aria-label="История диалогов"
                  >
                    <PanelLeft size={20} />
                  </button>
                )}
                <InstallAppControl canInstall={canInstallApp} standalone={standaloneApp} onInstall={installApp} compact />
                {!isControlMode && (
                  <button className="icon-button" type="button" onClick={() => setShowFilters(true)} aria-label="Фильтры">
                    <SlidersHorizontal size={20} />
                  </button>
                )}
              </div>
            </header>

            {!isControlMode && (
              <button className="landscape-filter-fab" type="button" onClick={() => setShowFilters(true)} aria-label="Фильтры">
                <SlidersHorizontal size={20} />
              </button>
            )}

            {!isControlMode && (
              <>
                <div className="search-row">
                  <Search size={18} />
                  <input
                    value={query}
                    onChange={(event) => setQuery(event.target.value)}
                    placeholder="КССС, номер, адрес, регион"
                    aria-label="Поиск по КССС, номеру, адресу или региону"
                  />
                  {query && (
                    <button className="clear-button" onClick={() => setQuery("")} aria-label="Очистить поиск">
                      <X size={16} />
                    </button>
                  )}
                </div>

                <MetricStrip count={filtered.length} metrics={metrics} />

                <FilterRail
                  filters={filters}
                  options={options}
                  setFilter={setFilter}
                  fuelAvailability={fuelAvailability}
                />
                <FuelFilterNotice availability={fuelAvailability} onClear={() => setFilter("fuel", [])} />
              </>
              )}

            <ModeSwitcher items={navItems} mode={mode} onChange={changeMode} />
          </>
        )}

        <ScopeNotice meta={payload.meta} />

        <AnimatePresence mode="popLayout" initial={false}>
          {mode === "home" ? (
            <motion.div className="view-stage" key="home" {...viewMotion}>
              <HomeDashboard
                count={filtered.length}
                total={stations.length}
                metrics={metrics}
                meta={payload.meta}
                regionCount={options.subject.length}
                excludedNoCoords={excludedNoCoords}
                canInstall={canInstallApp}
                standalone={standaloneApp}
                user={auth.user}
                onInstallApp={installApp}
                onOpenList={() => changeMode("list")}
                onOpenMap={() => changeMode("map")}
                onOpenAnalytics={() => changeMode("analytics")}
                onOpenAi={aiStatus?.enabled ? () => changeMode("ai") : undefined}
                onOpenControl={() => changeMode("quality")}
                onOpenFavorites={openFavorites}
                onOpenAdmin={() => changeMode("admin")}
                onLogout={handleLogout}
              />
            </motion.div>
          ) : mode === "analytics" ? (
            <motion.div className="view-stage" key="analytics" {...viewMotion}>
              <AnalyticsDashboard
                stations={filtered}
                totalStations={stations}
                selected={selected}
                onFilter={setFilter}
                onOpenList={() => changeMode("list")}
                onOpenStation={(id) => {
                  changeMode("list", { closeDetail: false });
                  selectStation(id);
                }}
              />
            </motion.div>
          ) : mode === "ai" && aiStatus?.enabled ? (
            <motion.div className="view-stage" key="ai" {...viewMotion}>
              <section className="analytics-pane">
                <AnalyticsAiConsole status={aiStatus} drawer={aiDrawer} onDrawer={setAiDrawer} />
              </section>
            </motion.div>
          ) : mode === "quality" ? (
            <motion.div className="view-stage" key="quality" {...viewMotion}>
              <ControlDashboard
                stations={stations}
                onOpenStation={(id) => {
                  changeMode("list", { closeDetail: false });
                  selectStation(id);
                }}
              />
            </motion.div>
          ) : mode === "admin" ? (
            <motion.div className="view-stage" key="admin" {...viewMotion}>
              <AdminDashboard onBack={() => changeMode("home")} currentUserId={auth.user?.id} />
            </motion.div>
          ) : (
            <motion.div className="view-stage content-grid" key={mode} {...(mode === "map" ? mapSheetMotion : viewMotion)}>
              <section className={`list-pane ${mode === "map" ? "mobile-hidden" : ""}`}>
                <div className="pane-title">
                  <span>{asInt(filtered.length)} найдено</span>
                  <div className="pane-title-actions">
                    <div className="density-toggle" role="group" aria-label="Плотность реестра">
                      <button
                        className={registryDensity === "comfortable" ? "active" : ""}
                        type="button"
                        onClick={() => setRegistryDensity("comfortable")}
                      >
                        Подробно
                      </button>
                      <button
                        className={registryDensity === "compact" ? "active" : ""}
                        type="button"
                        onClick={() => setRegistryDensity("compact")}
                      >
                        Компактно
                      </button>
                    </div>
                    <button type="button" onClick={() => setFilters(defaultFilters)}>Сбросить</button>
                  </div>
                </div>
                <StationList
                  stations={filtered}
                  selectedId={selected?.id}
                  favorites={favorites}
                  density={registryDensity}
                  onSelect={selectStation}
                  onFavorite={toggleFavorite}
                  onScroll={handleRegistryScroll}
                  loading={loading || fuelAvailability.pending}
                  favoritesOnly={Boolean(filters.favorites)}
                />
              </section>

              <section className={`map-pane ${mode === "list" ? "mobile-hidden" : ""}`}>
                <StationMap
                  stations={filtered}
                  selected={selected}
                  focusSelected={selectionMode === "manual"}
                  onSelect={previewStation}
                  onCloseFullscreen={mode === "map" ? () => changeMode(previousModeRef.current || "list") : undefined}
                />
              </section>
            </motion.div>
          )}
        </AnimatePresence>
      </section>

      <AnimatePresence>
        {previewVisible && (
          <MapStationPreview key={selected.id} station={selected} onOpen={openDetail} onDismiss={() => setSelectedId("")} />
        )}
      </AnimatePresence>

      <AnimatePresence>
        {detailVisible && (
          <StationDetail
            station={selected}
            favorite={favorites.includes(selected.id)}
            onFavorite={() => toggleFavorite(selected.id)}
            onClose={closeDetailSheet}
          />
        )}
      </AnimatePresence>

      {showFilters && (
        <FilterSheet
          filters={filters}
          options={options}
          setFilter={setFilter}
          fuelAvailability={fuelAvailability}
          onClose={() => setShowFilters(false)}
          onReset={() => setFilters(defaultFilters)}
        />
      )}

      <BottomStrip
        items={navItems}
        mode={mode}
        count={filtered.length}
        issueCount={issueCount}
        onChange={changeMode}
      />
    </main>
    </>
  );
}

function ModeSwitcher({ items, mode, onChange }) {
  return (
    <div className="mode-row" role="tablist" aria-label="Разделы классификатора">
      {items.map(({ id, label, Icon, accent, mark }) => (
        <motion.button
          className={mode === id ? "active" : ""}
          key={id}
          type="button"
          role="tab"
          aria-selected={mode === id}
          onClick={() => onChange(id)}
          transition={{ type: "spring", stiffness: 260, damping: 24 }}
          whileTap={{ scale: 0.965 }}
        >
          {mode === id && <motion.span className="mode-active-bg" layoutId="mode-active-bg" />}
          <span className={accent ? "mode-icon accent" : "mode-icon"}>
            {mark ? <AiMark variant="orb" simple decorative /> : <Icon size={16} />}
          </span>
          <span>{label}</span>
        </motion.button>
      ))}
    </div>
  );
}

function BottomStrip({ items, mode, count, issueCount, onChange }) {
  const badges = {
    home: "",
    list: asInt(count),
    map: asInt(count),
    analytics: "KPI",
    quality: issueCount ? asInt(issueCount) : "OK",
  };

  return (
    <nav className="bottom-strip" aria-label="Основная навигация">
      {items.filter((item) => !item.mobileHidden).map(({ id, mobileLabel, Icon, accent, mark }) => {
        const active = mode === id;
        return (
          <motion.button
            className={active ? "active" : ""}
            key={id}
            type="button"
            aria-current={active ? "page" : undefined}
            aria-label={mobileLabel}
            onClick={() => onChange(id)}
            transition={{ type: "spring", stiffness: 260, damping: 24 }}
            whileTap={{ scale: 0.955 }}
          >
            {active && <motion.span className="bottom-nav-active" layoutId="bottom-nav-active" />}
            <span className={accent ? "bottom-nav-icon accent" : "bottom-nav-icon"}>
              {mark ? <AiMark variant="orb" simple decorative /> : <Icon size={19} />}
            </span>
            <span className="bottom-nav-label">{mobileLabel}</span>
            {badges[id] && <span className={`bottom-nav-meta ${id === "quality" && issueCount ? "warning" : ""}`}>{badges[id]}</span>}
          </motion.button>
        );
      })}
    </nav>
  );
}

function InstallAppControl({ canInstall, standalone, onInstall, compact = false }) {
  if (!canInstall && !standalone) return null;

  if (standalone) {
    return (
      <div className={`install-control installed ${compact ? "compact" : ""}`}>
        <CheckCircle2 size={compact ? 16 : 18} />
        {!compact && <span>Открыто как приложение</span>}
      </div>
    );
  }

  return (
    <motion.button
      className={`install-control ${compact ? "compact" : ""}`}
      type="button"
      onClick={onInstall}
      whileTap={{ scale: 0.97 }}
    >
      <Download size={compact ? 16 : 18} />
      <span>{compact ? "Установить" : "Установить приложение"}</span>
    </motion.button>
  );
}

function detectInstallPlatform() {
  if (typeof navigator === "undefined") return "desktop";
  const userAgent = navigator.userAgent || "";
  const isIPad = navigator.platform === "MacIntel" && navigator.maxTouchPoints > 1;
  if (/iPad|iPhone|iPod/i.test(userAgent) || isIPad) return "ios";
  if (/Android/i.test(userAgent)) return "android";
  return "desktop";
}

function PwaInstallGuide({ canInstall, standalone, onInstall }) {
  const reduceMotion = useReducedMotion();
  const [open, setOpen] = useState(false);
  const [platform, setPlatform] = useState(detectInstallPlatform);
  const triggerRef = useRef(null);
  const closeRef = useRef(null);
  const guides = {
    ios: {
      title: "iPhone и iPad",
      intro: "Установка выполняется через Safari и занимает меньше минуты.",
      steps: [
        ["Откройте сайт в Safari", "Перейдите на azs-classifier.ru и войдите в систему."],
        ["Откройте меню «Поделиться»", "Нажмите значок «Поделиться». В новой раскладке Safari сначала может потребоваться кнопка «Еще»."],
        ["Добавьте на экран «Домой»", "Выберите «На экран Домой», включите «Открывать как веб-приложение» и нажмите «Добавить»."],
      ],
    },
    android: {
      title: "Android",
      intro: "В Chrome приложение устанавливается на главный экран устройства.",
      steps: [
        ["Откройте сайт в Chrome", "Перейдите на azs-classifier.ru и войдите в систему."],
        ["Откройте меню браузера", "Нажмите три точки справа от адресной строки."],
        ["Запустите установку", "Выберите «Добавить на главный экран», затем «Установить» и подтвердите действие."],
      ],
    },
    desktop: {
      title: "Компьютер",
      intro: "Сайт можно открыть в отдельном окне и закрепить как обычное приложение.",
      steps: [
        ["Chrome или Edge", "Нажмите значок установки в адресной строке. В Edge также можно открыть меню: Приложения → Установить этот сайт как приложение."],
        ["Подтвердите установку", "После установки закрепите приложение в панели задач, Dock или меню «Пуск»."],
        ["Safari на Mac", "В macOS Sonoma и новее откройте «Поделиться» → «Добавить в Dock», затем нажмите «Добавить»."],
      ],
    },
  };
  const guide = guides[platform];

  useEffect(() => {
    if (!open) return undefined;
    const previousOverflow = document.body.style.overflow;
    const closeOnEscape = (event) => {
      if (event.key === "Escape") setOpen(false);
    };
    document.body.style.overflow = "hidden";
    document.addEventListener("keydown", closeOnEscape);
    window.requestAnimationFrame(() => closeRef.current?.focus());
    return () => {
      document.body.style.overflow = previousOverflow;
      document.removeEventListener("keydown", closeOnEscape);
      triggerRef.current?.focus();
    };
  }, [open]);

  if (standalone) return null;

  const dialog = typeof document !== "undefined" && createPortal(
    <AnimatePresence>
      {open && (
        <motion.div
          className="pwa-guide-backdrop"
          initial={reduceMotion ? false : { opacity: 0 }}
          animate={{ opacity: 1 }}
          exit={{ opacity: 0 }}
          transition={{ duration: reduceMotion ? 0 : 0.18 }}
          onMouseDown={(event) => {
            if (event.target === event.currentTarget) setOpen(false);
          }}
        >
          <motion.section
            className="pwa-guide-dialog"
            role="dialog"
            aria-modal="true"
            aria-labelledby="pwa-guide-title"
            initial={reduceMotion ? false : { opacity: 0, y: 24, scale: 0.98 }}
            animate={{ opacity: 1, y: 0, scale: 1 }}
            exit={reduceMotion ? { opacity: 0 } : { opacity: 0, y: 16, scale: 0.99 }}
            transition={{ duration: reduceMotion ? 0 : 0.22, ease: "easeOut" }}
          >
            <header className="pwa-guide-head">
              <div>
                <span><Smartphone size={16} aria-hidden="true" /> Установка приложения</span>
                <h2 id="pwa-guide-title">Добавить на устройство</h2>
              </div>
              <button ref={closeRef} type="button" onClick={() => setOpen(false)} aria-label="Закрыть инструкцию">
                <X size={20} aria-hidden="true" />
              </button>
            </header>

            <div className="pwa-guide-tabs" role="tablist" aria-label="Выберите устройство">
              {[
                ["ios", "iPhone / iPad", Smartphone],
                ["android", "Android", Smartphone],
                ["desktop", "Компьютер", Monitor],
              ].map(([id, label, Icon]) => (
                <button
                  className={platform === id ? "active" : ""}
                  type="button"
                  role="tab"
                  aria-selected={platform === id}
                  key={id}
                  onClick={() => setPlatform(id)}
                >
                  <Icon size={16} aria-hidden="true" />
                  <span>{label}</span>
                </button>
              ))}
            </div>

            <div className="pwa-guide-content" role="tabpanel" key={platform}>
              <h3>{guide.title}</h3>
              <p>{guide.intro}</p>
              <ol>
                {guide.steps.map(([title, detail], index) => (
                  <li key={title}>
                    <span>{index + 1}</span>
                    <div><strong>{title}</strong><small>{detail}</small></div>
                  </li>
                ))}
              </ol>
            </div>

            <footer className="pwa-guide-actions">
              {canInstall && (
                <motion.button
                  className="primary"
                  type="button"
                  onClick={() => {
                    setOpen(false);
                    onInstall();
                  }}
                  whileTap={reduceMotion ? undefined : { scale: 0.97 }}
                >
                  <Download size={17} aria-hidden="true" /> Установить сейчас
                </motion.button>
              )}
              <button type="button" onClick={() => setOpen(false)}>Готово</button>
            </footer>
          </motion.section>
        </motion.div>
      )}
    </AnimatePresence>,
    document.body,
  );

  return (
    <>
      <section className="pwa-install-banner" aria-labelledby="pwa-install-title">
        <span className="pwa-install-icon" aria-hidden="true"><Smartphone size={20} /></span>
        <div>
          <strong id="pwa-install-title">Добавьте сервис на главный экран</strong>
          <small>Открывается как приложение и всегда остается под рукой.</small>
        </div>
        <div className="pwa-install-actions">
          {canInstall && (
            <motion.button className="primary" type="button" onClick={onInstall} whileTap={reduceMotion ? undefined : { scale: 0.97 }}>
              <Download size={16} aria-hidden="true" /> Установить
            </motion.button>
          )}
          <button ref={triggerRef} type="button" onClick={() => setOpen(true)}>
            <Share2 size={16} aria-hidden="true" /> Как установить
          </button>
        </div>
      </section>
      {dialog}
    </>
  );
}

function AuthLoading() {
  return (
    <main className="auth-shell">
      <section className="auth-panel auth-panel-loading" aria-live="polite">
        <ShieldCheck size={28} />
        <h1>Классификатор АЗС</h1>
        <p>Проверяем сессию...</p>
      </section>
    </main>
  );
}

function AuthScreen({ initialError = "", onAuthenticated }) {
  const [mode, setMode] = useState("login");
  const [form, setForm] = useState({ email: "", password: "", name: "" });
  const [verification, setVerification] = useState({ required: false, email: "", message: "", devCode: "" });
  const [passwordReset, setPasswordReset] = useState({ active: false, email: "", message: "", devCode: "" });
  const [code, setCode] = useState("");
  const [status, setStatus] = useState("idle");
  const [error, setError] = useState(initialError);
  const [policy, setPolicy] = useState({ allowedDomains: [], allowlistEnabled: false, emailVerificationRequired: true });

  useEffect(() => {
    purgePrivateCaches();
    authJson("/api/auth/policy")
      .then((data) => {
        setPolicy({
          allowedDomains: Array.isArray(data.allowedDomains) ? data.allowedDomains : [],
          allowlistEnabled: Boolean(data.allowlistEnabled),
          emailVerificationRequired: Boolean(data.emailVerificationRequired),
        });
      })
      .catch(() => {});
  }, []);

  useEffect(() => {
    setError(initialError);
  }, [initialError]);

  async function submitAuth(event) {
    event.preventDefault();
    setStatus("submitting");
    setError("");

    try {
      const payload = {
        email: form.email.trim(),
        password: form.password,
        name: mode === "register" ? form.name.trim() : "",
      };
      const data = await authJson(`/api/auth/${mode === "register" ? "register" : "login"}`, {
        method: "POST",
        body: JSON.stringify(payload),
      });
      if (data.user) {
        onAuthenticated(data.user);
        return;
      }
      if (data.verificationRequired) {
        setVerification({
          required: true,
          email: data.email || payload.email,
          message: data.message || "Код подтверждения отправлен на корпоративную почту.",
          devCode: data.devCode || "",
        });
        setCode("");
        setStatus("idle");
        return;
      }
      setError("Не удалось завершить вход");
      setStatus("idle");
    } catch (authError) {
      setError(authError.message || "Не удалось выполнить вход");
      setStatus("idle");
    }
  }

  async function submitVerification(event) {
    event.preventDefault();
    setStatus("submitting");
    setError("");

    try {
      const data = await authJson("/api/auth/verify-email", {
        method: "POST",
        body: JSON.stringify({ email: verification.email, code }),
      });
      if (data.user) {
        onAuthenticated(data.user);
        return;
      }
      setError("Не удалось подтвердить email");
      setStatus("idle");
    } catch (authError) {
      setError(authError.message || "Неверный код подтверждения");
      setStatus("idle");
    }
  }

  async function resendVerificationCode() {
    setStatus("submitting");
    setError("");
    try {
      const data = await authJson("/api/auth/resend-code", {
        method: "POST",
        body: JSON.stringify({ email: verification.email }),
      });
      setVerification((current) => ({
        ...current,
        message: data.message || "Новый код отправлен на корпоративную почту.",
        devCode: data.devCode || "",
      }));
      setCode("");
    } catch (authError) {
      setError(authError.message || "Не удалось отправить код повторно");
    } finally {
      setStatus("idle");
    }
  }

  function resetVerification() {
    setVerification({ required: false, email: "", message: "", devCode: "" });
    setCode("");
    setError("");
    setStatus("idle");
  }

  function startPasswordReset() {
    setPasswordReset({ active: true, email: "", message: "", devCode: "" });
    setVerification({ required: false, email: "", message: "", devCode: "" });
    setCode("");
    setForm((current) => ({ ...current, password: "" }));
    setError("");
    setStatus("idle");
  }

  function cancelPasswordReset() {
    setPasswordReset({ active: false, email: "", message: "", devCode: "" });
    setCode("");
    setForm((current) => ({ ...current, password: "" }));
    setError("");
    setStatus("idle");
  }

  async function submitPasswordResetRequest(event) {
    event.preventDefault();
    setStatus("submitting");
    setError("");

    const email = form.email.trim();
    try {
      const data = await authJson("/api/auth/request-password-reset", {
        method: "POST",
        body: JSON.stringify({ email }),
      });
      setPasswordReset({
        active: true,
        email: data.email || email,
        message:
          data.message ||
          "Если адрес зарегистрирован и допущен к системе, мы отправили код восстановления на корпоративную почту.",
        devCode: data.devCode || "",
      });
      setCode("");
      setForm((current) => ({ ...current, password: "" }));
    } catch (authError) {
      setError(authError.message || "Не удалось отправить код восстановления");
    } finally {
      setStatus("idle");
    }
  }

  async function resendPasswordResetCode() {
    setStatus("submitting");
    setError("");
    try {
      const data = await authJson("/api/auth/request-password-reset", {
        method: "POST",
        body: JSON.stringify({ email: passwordReset.email }),
      });
      setPasswordReset((current) => ({
        ...current,
        message: data.message || current.message,
        devCode: data.devCode || "",
      }));
      setCode("");
    } catch (authError) {
      setError(authError.message || "Не удалось отправить код повторно");
    } finally {
      setStatus("idle");
    }
  }

  async function submitPasswordReset(event) {
    event.preventDefault();
    setStatus("submitting");
    setError("");

    try {
      const data = await authJson("/api/auth/reset-password", {
        method: "POST",
        body: JSON.stringify({ email: passwordReset.email, code, password: form.password }),
      });
      if (data.user) {
        onAuthenticated(data.user);
        return;
      }
      setError("Пароль обновлен. Войдите с новым паролем.");
      setMode("login");
      cancelPasswordReset();
    } catch (authError) {
      setError(authError.message || "Не удалось обновить пароль");
      setStatus("idle");
    }
  }

  return (
    <main className="auth-shell">
      <section className="auth-panel" aria-labelledby="auth-title">
        <div className="auth-brand">
          <ShieldCheck size={30} />
          <div>
            <span>Корпоративный доступ</span>
            <h1 id="auth-title">Классификатор АЗС</h1>
          </div>
        </div>

        {passwordReset.active ? (
          <form className="auth-form auth-code-form" onSubmit={passwordReset.email ? submitPasswordReset : submitPasswordResetRequest}>
            <div className="auth-code-head">
              <strong>{passwordReset.email ? "Задайте новый пароль" : "Восстановление пароля"}</strong>
              <span>
                {passwordReset.email
                  ? passwordReset.message
                  : "Укажите корпоративный email. Если адрес есть в системе, на него придет код восстановления."}
              </span>
              {passwordReset.email && <small>{passwordReset.email}</small>}
            </div>

            {!passwordReset.email ? (
              <label htmlFor="reset-email">
                <span>Email</span>
                <input
                  id="reset-email"
                  type="email"
                  autoComplete="email"
                  value={form.email}
                  onChange={(event) => setForm((current) => ({ ...current, email: event.target.value }))}
                  placeholder={policy.allowedDomains[0] ? `name@${policy.allowedDomains[0]}` : "name@company.ru"}
                  required
                />
              </label>
            ) : (
              <>
                <label htmlFor="reset-code">
                  <span>Код из письма</span>
                  <input
                    id="reset-code"
                    autoComplete="one-time-code"
                    inputMode="numeric"
                    value={code}
                    onChange={(event) => setCode(event.target.value.replace(/[^\d]/g, "").slice(0, 12))}
                    placeholder="Например: 493821"
                    required
                  />
                </label>

                <label htmlFor="reset-new-password">
                  <span>Новый пароль</span>
                  <input
                    id="reset-new-password"
                    type="password"
                    autoComplete="new-password"
                    value={form.password}
                    onChange={(event) => setForm((current) => ({ ...current, password: event.target.value }))}
                    placeholder="Минимум 8 символов"
                    minLength={8}
                    required
                  />
                </label>
              </>
            )}

            {passwordReset.devCode && (
              <p className="auth-dev-code">
                Локальный код: <strong>{passwordReset.devCode}</strong>
              </p>
            )}

            {error && (
              <p className="auth-error" role="alert">
                {error}
              </p>
            )}

            <button className="auth-submit" type="submit" disabled={status === "submitting"}>
              {status === "submitting" ? "Проверяем..." : passwordReset.email ? "Сохранить новый пароль" : "Отправить код"}
            </button>
            <div className="auth-inline-actions">
              {passwordReset.email && (
                <button type="button" onClick={resendPasswordResetCode} disabled={status === "submitting"}>
                  Отправить код ещё раз
                </button>
              )}
              {passwordReset.email && (
                <button
                  type="button"
                  onClick={() => {
                    setPasswordReset({ active: true, email: "", message: "", devCode: "" });
                    setCode("");
                    setForm((current) => ({ ...current, password: "" }));
                    setError("");
                  }}
                  disabled={status === "submitting"}
                >
                  Изменить email
                </button>
              )}
              <button type="button" onClick={cancelPasswordReset} disabled={status === "submitting"}>
                Вернуться ко входу
              </button>
            </div>
          </form>
        ) : verification.required ? (
          <form className="auth-form auth-code-form" onSubmit={submitVerification}>
            <div className="auth-code-head">
              <strong>Подтвердите email</strong>
              <span>{verification.message}</span>
              <small>{verification.email}</small>
            </div>

            <label htmlFor="verify-code">
              <span>Код из письма</span>
              <input
                id="verify-code"
                autoComplete="one-time-code"
                inputMode="numeric"
                value={code}
                onChange={(event) => setCode(event.target.value.replace(/[^\d]/g, "").slice(0, 12))}
                placeholder="Например: 493821"
                required
              />
            </label>

            {verification.devCode && (
              <p className="auth-dev-code">
                Локальный код: <strong>{verification.devCode}</strong>
              </p>
            )}

            {error && (
              <p className="auth-error" role="alert">
                {error}
              </p>
            )}

            <button className="auth-submit" type="submit" disabled={status === "submitting"}>
              {status === "submitting" ? "Проверяем..." : "Подтвердить и войти"}
            </button>
            <div className="auth-inline-actions">
              <button type="button" onClick={resendVerificationCode} disabled={status === "submitting"}>
                Отправить код ещё раз
              </button>
              <button type="button" onClick={resetVerification} disabled={status === "submitting"}>
                Изменить email
              </button>
            </div>
          </form>
        ) : (
          <>
            <div className="auth-switch" role="tablist" aria-label="Режим авторизации">
              <button className={mode === "login" ? "active" : ""} type="button" role="tab" aria-selected={mode === "login"} onClick={() => setMode("login")}>
                Вход
              </button>
              <button className={mode === "register" ? "active" : ""} type="button" role="tab" aria-selected={mode === "register"} onClick={() => setMode("register")}>
                Регистрация
              </button>
            </div>

            <form className="auth-form" onSubmit={submitAuth}>
              {mode === "register" && (
                <label htmlFor="auth-name">
                  <span>Имя</span>
                  <input
                    id="auth-name"
                    autoComplete="name"
                    value={form.name}
                    onChange={(event) => setForm((current) => ({ ...current, name: event.target.value }))}
                    placeholder="Например: Иван Петров"
                  />
                </label>
              )}

              <label htmlFor="auth-email">
                <span>Email</span>
                <input
                  id="auth-email"
                  type="email"
                  autoComplete="email"
                  value={form.email}
                  onChange={(event) => setForm((current) => ({ ...current, email: event.target.value }))}
                  placeholder={policy.allowedDomains[0] ? `name@${policy.allowedDomains[0]}` : "name@company.ru"}
                  required
                />
              </label>

              <label htmlFor="auth-password">
                <span>Пароль</span>
                <input
                  id="auth-password"
                  type="password"
                  autoComplete={mode === "register" ? "new-password" : "current-password"}
                  value={form.password}
                  onChange={(event) => setForm((current) => ({ ...current, password: event.target.value }))}
                  placeholder="Минимум 8 символов"
                  minLength={8}
                  required
                />
              </label>

              {mode === "login" && (
                <div className="auth-forgot-row">
                  <button type="button" onClick={startPasswordReset}>
                    Забыли пароль?
                  </button>
                </div>
              )}

              {error && (
                <p className="auth-error" role="alert">
                  {error}
                </p>
              )}

              <button className="auth-submit" type="submit" disabled={status === "submitting"}>
                {status === "submitting" ? "Проверяем..." : mode === "register" ? "Создать доступ" : "Войти"}
              </button>
            </form>
          </>
        )}

        <div className="auth-policy">
          <strong>Только для сотрудников компании</strong>
          <span>
            {policy.allowedDomains.length
              ? `Вход разрешен для доменов: ${policy.allowedDomains.join(", ")}${policy.allowlistEnabled ? " и адресов из allowlist" : ""}.`
              : "Вход разрешен только для адресов из корпоративного allowlist."}
            {policy.emailVerificationRequired ? " При первой авторизации потребуется код из письма." : ""}
          </span>
        </div>

        <p className="auth-note">
          Внешние email не проходят регистрацию и вход. Доступ к реестру, карте, аналитике, контролю и API открывается только после авторизации.
        </p>
      </section>
    </main>
  );
}

const ADMIN_SCREEN_LABELS = {
  home: "Главная",
  list: "Реестр",
  map: "Карта",
  analytics: "Аналитика",
  ai: "ИИ-аналитик",
  quality: "Контроль",
  admin: "Админ",
  session_start: "Открытие приложения",
  "analytics:summary": "Аналитика · свод",
  "analytics:overview": "Аналитика · обзор",
  "analytics:slices": "Аналитика · срезы",
  "analytics:outages": "Аналитика · простои",
  "analytics:similar": "Аналитика · похожие",
  "analytics:compare": "Аналитика · сравнение",
};

const ADMIN_ACTION_LABELS = {
  station_open: "Открытие карточки АЗС",
  filter: "Использование фильтров",
  search: "Поиск",
};

const ADMIN_AUTH_EVENT_LABELS = {
  login_success: "Вход",
  login_failed: "Неверный пароль",
  login_blocked: "Вход заблокирован",
  logout: "Выход",
  register_verification_sent: "Регистрация: код отправлен",
  verify_success: "Почта подтверждена",
  password_reset_code_sent: "Сброс пароля: код отправлен",
  password_reset_success: "Пароль изменен",
  password_reset_failed: "Сброс пароля: ошибка",
  session_blocked: "Сессия заблокирована",
};

function adminScreenLabel(screen) {
  return ADMIN_SCREEN_LABELS[screen] || screen || "—";
}

function formatUnixDateTime(ts) {
  if (!ts) return "—";
  const date = new Date(ts * 1000);
  return date.toLocaleString("ru-RU", { day: "2-digit", month: "2-digit", year: "numeric", hour: "2-digit", minute: "2-digit" });
}

function formatUnixDate(ts) {
  if (!ts) return "—";
  return new Date(ts * 1000).toLocaleDateString("ru-RU", { day: "2-digit", month: "2-digit", year: "numeric" });
}

function formatDurationShort(totalSeconds) {
  const seconds = Math.max(0, Math.round(totalSeconds || 0));
  if (seconds < 60) return `${seconds} с`;
  const minutes = Math.floor(seconds / 60);
  if (minutes < 60) return `${minutes} мин`;
  const hours = Math.floor(minutes / 60);
  const restMinutes = minutes % 60;
  return restMinutes ? `${hours} ч ${restMinutes} мин` : `${hours} ч`;
}

function AdminBarList({ items, valueLabel }) {
  const maxValue = Math.max(1, ...items.map((item) => item.value));
  return (
    <div className="admin-bar-list">
      {items.length === 0 && <p className="admin-empty">Нет данных за выбранный период.</p>}
      {items.map((item) => (
        <div className="admin-bar-row" key={item.key}>
          <span className="admin-bar-label" title={item.label}>{item.label}</span>
          <span className="admin-bar-track">
            <span className="admin-bar-fill" style={{ width: `${Math.max(3, Math.round((item.value / maxValue) * 100))}%` }} />
          </span>
          <span className="admin-bar-value">
            {asInt(item.value)}
            {item.helper ? <em>{item.helper}</em> : null}
          </span>
        </div>
      ))}
      {valueLabel && <p className="admin-bar-caption">{valueLabel}</p>}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Раздел «Качество ответов» (БТ-КК1…КК10).
// Переписки здесь нет: метаданные, оценка, комментарий и текст запроса к
// витрине. Полное содержание чужого диалога раскрывается только по жалобе его
// автора — модель приватности ИБ-4.
// ---------------------------------------------------------------------------
const QUALITY_PERIODS = [
  { days: 7, label: "7 дней" },
  { days: 30, label: "30 дней" },
  { days: 90, label: "90 дней" },
];

const QUALITY_VERDICTS = {
  ok: { label: "Ответ", tone: "ok" },
  rejected: { label: "Отказ", tone: "refused" },
  execution_error: { label: "Ошибка", tone: "failed" },
  model_unavailable: { label: "Ошибка", tone: "failed" },
};

function qualityShare(part, whole) {
  if (!whole) return "—";
  return `${(100 * part / whole).toLocaleString("ru-RU", { maximumFractionDigits: 1 })} %`;
}

function qualitySeconds(ms) {
  if (!ms) return "—";
  return `${(ms / 1000).toLocaleString("ru-RU", { minimumFractionDigits: 1, maximumFractionDigits: 1 })} с`;
}

function QualityTile({ label, value, note, tone }) {
  return (
    <div className={tone ? `quality-tile ${tone}` : "quality-tile"}>
      <div className="quality-tile-value">{value}</div>
      <div className="quality-tile-label">{label}</div>
      {note ? <div className="quality-tile-note">{note}</div> : null}
    </div>
  );
}

function QualitySpread({ spread, rated }) {
  const top = Math.max(1, ...[1, 2, 3, 4, 5].map((n) => spread[String(n)] || 0));
  return (
    <div className="quality-spread">
      {[5, 4, 3, 2, 1].map((star) => {
        const count = spread[String(star)] || 0;
        return (
          <div className="quality-spread-row" key={star}>
            <span className="quality-spread-star">{star}</span>
            <span className="quality-spread-track">
              <span
                className={star <= 3 ? "quality-spread-fill low" : "quality-spread-fill"}
                style={{ width: `${Math.round(100 * count / top)}%` }}
              />
            </span>
            <span className="quality-spread-count">{asInt(count)}</span>
            <span className="quality-spread-share">{qualityShare(count, rated)}</span>
          </div>
        );
      })}
    </div>
  );
}

function QualityEntry({ item, statuses, onReview }) {
  const [open, setOpen] = useState(false);
  const [note, setNote] = useState(item.note || "");
  const [busy, setBusy] = useState(false);
  const verdict = QUALITY_VERDICTS[item.verdict] || { label: item.verdict || "—", tone: "" };

  async function send(patch) {
    setBusy(true);
    try {
      await onReview(item.message_id, patch);
    } finally {
      setBusy(false);
    }
  }

  return (
    <article className={item.overdue ? "quality-entry overdue" : "quality-entry"}>
      <div className="quality-entry-head">
        <AiStars value={item.rating} size={16} />
        <span className={`quality-chip ${verdict.tone}`}>{verdict.label}</span>
        <span className="quality-entry-meta">
          {item.role || "—"}
          {item.scope_label ? ` · ${item.scope_label}` : ""}
        </span>
        <span className="quality-entry-gap" />
        <span className="quality-entry-meta">{formatUnixDateTime(item.created_at)}</span>
      </div>

      <p className="quality-question">{item.question}</p>
      {item.comment ? <p className="quality-comment">{item.comment}</p> : null}

      <div className="quality-entry-facts">
        <span>{item.model || "модель не записана"}</span>
        <span>инструкция {item.prompt_version || "—"}</span>
        <span>{qualitySeconds(item.total_ms)}</span>
        {item.rule ? <span>правило: {item.rule}</span> : null}
        {item.in_golden ? <span className="quality-golden"><Check size={13} /> в эталонном наборе</span> : null}
        {item.overdue ? <span className="quality-overdue">реакция просрочена</span> : null}
      </div>

      <div className="quality-entry-actions">
        <label className="quality-status">
          <span className="visually-hidden">Статус разбора</span>
          <select
            className="ui-select"
            value={item.status}
            disabled={busy}
            onChange={(event) => send({ status: event.target.value })}
          >
            {statuses.map((option) => (
              <option key={option.code} value={option.code}>{option.title}</option>
            ))}
          </select>
        </label>
        {item.owner ? <span className="quality-entry-meta">разбирает: {item.owner}</span> : null}
        {item.first_seen ? (
          <span className="quality-entry-meta">взято в работу {formatUnixDateTime(item.first_seen)}</span>
        ) : null}
        <span className="quality-entry-gap" />
        <button
          type="button"
          className="ai-action"
          disabled={busy || Boolean(item.in_golden)}
          onClick={() => send({ inGolden: true })}
        >
          <Plus size={15} /> {item.in_golden ? "В эталонном наборе" : "В эталонный набор"}
        </button>
        <button type="button" className="ai-action" onClick={() => setOpen((value) => !value)}>
          <Pencil size={15} /> Результат разбора
        </button>
      </div>

      {open && (
        <div className="quality-note">
          <textarea
            className="ui-textarea"
            rows={2}
            value={note}
            placeholder="Что выяснили и что сделали"
            onChange={(event) => setNote(event.target.value)}
          />
          <div className="quality-note-actions">
            <button
              type="button"
              className="ui-button"
              disabled={busy || note === (item.note || "")}
              onClick={() => send({ note }).then(() => setOpen(false))}
            >
              Сохранить
            </button>
            <button type="button" className="ui-button ghost" onClick={() => { setNote(item.note || ""); setOpen(false); }}>
              Отмена
            </button>
          </div>
        </div>
      )}
      {!open && item.note ? <p className="quality-note-text">{item.note}</p> : null}
    </article>
  );
}

function AiQualityPanel() {
  const [days, setDays] = useState(30);
  const [filters, setFilters] = useState({ rating: "", verdict: "", status: "", role: "", promptVersion: "" });
  const [state, setState] = useState({ status: "loading", data: null, error: "" });

  const query = useMemo(() => {
    const params = new URLSearchParams({ days: String(days), limit: "200" });
    Object.entries(filters).forEach(([key, value]) => {
      if (value) params.set(key, value);
    });
    return params.toString();
  }, [days, filters]);

  const load = useCallback(() => {
    let alive = true;
    setState((previous) => ({ ...previous, status: previous.data ? "refreshing" : "loading", error: "" }));
    fetchJson(`/api/ai/quality?${query}`)
      .then((data) => { if (alive) setState({ status: "ready", data, error: "" }); })
      .catch((error) => {
        if (!alive || error.message === "AUTH_REQUIRED") return;
        setState({
          status: "error",
          data: null,
          error: error.message === "REQUEST_FAILED_403"
            ? "Недостаточно прав для просмотра качества ответов."
            : "Не удалось загрузить качество ответов.",
        });
      });
    return () => { alive = false; };
  }, [query]);

  useEffect(() => load(), [load]);

  async function review(messageId, patch) {
    try {
      await aiSend(`/api/ai/quality/${messageId}/review`, {
        method: "POST",
        body: JSON.stringify(patch),
      });
      load();
    } catch (error) {
      setState((previous) => ({ ...previous, error: error.message || "Не удалось сохранить разбор" }));
    }
  }

  const data = state.data;
  const summary = data?.summary;
  const roles = useMemo(() => {
    const seen = new Set((data?.entries || []).map((item) => item.role).filter(Boolean));
    return [...seen].sort();
  }, [data]);

  if (state.status === "loading") {
    return <div className="admin-card"><h3>Качество ответов ИИ</h3><p className="admin-bar-caption">Загружаю…</p></div>;
  }
  if (state.status === "error") {
    return <div className="admin-card"><h3>Качество ответов ИИ</h3><p className="admin-error">{state.error}</p></div>;
  }

  return (
    <div className="admin-card quality-card">
      <div className="quality-head">
        <h3>Качество ответов ИИ</h3>
        <span className="quality-head-gap" />
        <label className="quality-period">
          <span className="visually-hidden">Период</span>
          <select className="ui-select" value={days} onChange={(event) => setDays(Number(event.target.value))}>
            {QUALITY_PERIODS.map((item) => (
              <option key={item.days} value={item.days}>{item.label}</option>
            ))}
          </select>
        </label>
        <a className="ai-action" href={`/api/ai/quality/export?days=${days}`} download>
          <Download size={15} /> В Excel
        </a>
      </div>

      <div className="quality-tiles">
        <QualityTile label="Задано вопросов" value={asInt(summary.asked)} />
        <QualityTile label="Отвечено" value={asInt(summary.answered)} note={qualityShare(summary.answered, summary.asked)} />
        <QualityTile label="Отказов" value={asInt(summary.refused)} note={`${qualityShare(summary.refused, summary.asked)} · граница области данных`} />
        <QualityTile label="Технических ошибок" value={asInt(summary.failed)} note={qualityShare(summary.failed, summary.asked)} tone={summary.failed ? "warn" : ""} />
        <QualityTile label="Оценено" value={asInt(summary.rated)} note={`${qualityShare(summary.rated, summary.answered)} от ответов`} />
        <QualityTile label="Средняя оценка" value={summary.average === null ? "—" : summary.average.toLocaleString("ru-RU", { minimumFractionDigits: 2 })} />
        <QualityTile label="Время ответа" value={qualitySeconds(summary.avgMs)} note={`наибольшее ${qualitySeconds(summary.maxMs)}`} />
        <QualityTile
          label="В разборе"
          value={asInt(summary.openReview)}
          note={summary.overdue ? `просрочено ${asInt(summary.overdue)}` : `срок реакции ${summary.firstResponseDays} дня`}
          tone={summary.overdue ? "bad" : ""}
        />
      </div>

      {summary.rated > 0 && <QualitySpread spread={summary.spread} rated={summary.rated} />}

      <div className="quality-filters">
        <label className="ui-field">
          <span>Оценка</span>
          <select className="ui-select" value={filters.rating}
                  onChange={(event) => setFilters((f) => ({ ...f, rating: event.target.value }))}>
            <option value="">любая</option>
            {[1, 2, 3, 4, 5].map((n) => <option key={n} value={n}>{n}</option>)}
          </select>
        </label>
        <label className="ui-field">
          <span>Исход</span>
          <select className="ui-select" value={filters.verdict}
                  onChange={(event) => setFilters((f) => ({ ...f, verdict: event.target.value }))}>
            <option value="">любой</option>
            <option value="ok">ответ</option>
            <option value="refused">отказ</option>
            <option value="failed">техническая ошибка</option>
          </select>
        </label>
        <label className="ui-field">
          <span>Разбор</span>
          <select className="ui-select" value={filters.status}
                  onChange={(event) => setFilters((f) => ({ ...f, status: event.target.value }))}>
            <option value="">любой</option>
            {(data.statuses || []).map((option) => (
              <option key={option.code} value={option.code}>{option.title}</option>
            ))}
          </select>
        </label>
        <label className="ui-field">
          <span>Роль</span>
          <select className="ui-select" value={filters.role}
                  onChange={(event) => setFilters((f) => ({ ...f, role: event.target.value }))}>
            <option value="">любая</option>
            {roles.map((role) => <option key={role} value={role}>{role}</option>)}
          </select>
        </label>
        <label className="ui-field">
          <span>Версия инструкции</span>
          <select className="ui-select" value={filters.promptVersion}
                  onChange={(event) => setFilters((f) => ({ ...f, promptVersion: event.target.value }))}>
            <option value="">любая</option>
            {(data.versions || []).map((item) => (
              <option key={`${item.version}-${item.model}`} value={item.version}>
                {item.version}{item.version === data.promptVersion ? " (текущая)" : ""}
              </option>
            ))}
          </select>
        </label>
      </div>

      {state.error && <p className="admin-error">{state.error}</p>}

      <div className="quality-entries">
        {(data.entries || []).length === 0 ? (
          <p className="admin-bar-caption">За этот период оценок нет.</p>
        ) : (
          data.entries.map((item) => (
            <QualityEntry key={item.message_id} item={item} statuses={data.statuses || []} onReview={review} />
          ))
        )}
      </div>

      <h4 className="quality-subhead">Срез по версиям</h4>
      <p className="admin-bar-caption">
        Средняя оценка сама по себе ничего не говорит, пока не с чем сравнить: этот срез показывает,
        улучшила ли новая версия инструкции или модели качество.
      </p>
      <div className="admin-table-wrap">
        <table className="admin-table">
          <thead>
            <tr>
              <th>Версия</th>
              <th>Модель</th>
              <th>Задано</th>
              <th>Отвечено</th>
              <th>Оценено</th>
              <th>Средняя</th>
              <th>Низких</th>
              <th>Последний вопрос</th>
            </tr>
          </thead>
          <tbody>
            {(data.versions || []).map((item) => (
              <tr key={`${item.version}-${item.model}`}>
                <td>
                  {item.version}
                  {item.version === data.promptVersion ? <small>текущая</small> : null}
                </td>
                <td>{item.model || "—"}</td>
                <td>{asInt(item.asked)}</td>
                <td>{asInt(item.answered)}</td>
                <td>{asInt(item.rated)}</td>
                <td>{item.average === null ? "—" : item.average.toLocaleString("ru-RU", { minimumFractionDigits: 2 })}</td>
                <td>{asInt(item.low)}</td>
                <td>{formatUnixDateTime(item.untilAt)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      <p className="admin-bar-caption">
        Здесь видны метаданные, оценка, комментарий и текст запроса к витрине. Переписки нет:
        полное содержание чужого диалога раскрывается только по жалобе его автора.
        Оценки — обратная связь по продукту: они не используются для оценки работы сотрудника
        и не влияют на премирование (БТ-КК10).
      </p>
    </div>
  );
}

// Администрирование разложено на три вкладки: раньше это была одна длинная
// страница, где качество ответов ИИ пряталось между таблицей пользователей
// и журналом входов.
const ADMIN_TABS = [
  { code: "usage", tab: "Аналитика", title: "Пользовательская аналитика",
    note: "Регистрации, входы, длительность визитов и востребованность разделов." },
  { code: "users", tab: "Пользователи", title: "Перечень пользователей",
    note: "Учётные записи, роли и область данных. Здесь же удаление аккаунта." },
  { code: "quality", tab: "Качество ИИ", title: "Качество ответов ИИ",
    note: "Оценки, разбор низких оценок и срез по версиям инструкции." },
];

function AdminDashboard({ onBack, currentUserId }) {
  const aiStatus = useAiStatus();
  const [tab, setTab] = useState("usage");
  const [days, setDays] = useState(30);
  const [usersState, setUsersState] = useState({ status: "loading", data: null, error: "" });
  const [activityState, setActivityState] = useState({ status: "loading", data: null, error: "" });
  const [deleteState, setDeleteState] = useState({ id: null, busy: false, error: "", info: "" });
  const [rolesCatalog, setRolesCatalog] = useState(null);

  const load = useCallback(() => {
    const controller = new AbortController();
    setDeleteState((previous) => ({ ...previous, id: null, busy: false }));
    setUsersState((previous) => ({ ...previous, status: previous.data ? "refreshing" : "loading", error: "" }));
    setActivityState((previous) => ({ ...previous, status: previous.data ? "refreshing" : "loading", error: "" }));

    fetchJson("/api/roles/catalog", controller.signal)
      .then((data) => data && setRolesCatalog(data))
      .catch(() => {});

    fetchJson("/api/admin/users", controller.signal)
      .then((data) => setUsersState({ status: "ready", data, error: "" }))
      .catch((error) => {
        if (error.name === "AbortError" || error.message === "AUTH_REQUIRED") return;
        setUsersState({
          status: "error",
          data: null,
          error: error.message === "REQUEST_FAILED_403" ? "Недостаточно прав для просмотра статистики." : "Не удалось загрузить список пользователей.",
        });
      });

    fetchJson(`/api/admin/activity?days=${days}`, controller.signal)
      .then((data) => setActivityState({ status: "ready", data, error: "" }))
      .catch((error) => {
        if (error.name === "AbortError" || error.message === "AUTH_REQUIRED") return;
        setActivityState({
          status: "error",
          data: null,
          error: error.message === "REQUEST_FAILED_403" ? "Недостаточно прав для просмотра статистики." : "Не удалось загрузить статистику активности.",
        });
      });

    return () => controller.abort();
  }, [days]);

  useEffect(() => load(), [load]);

  useEffect(() => {
    if (tab === "quality" && aiStatus && !aiStatus.enabled) setTab("usage");
  }, [tab, aiStatus]);

  async function handleDeleteUser(item) {
    // Первый клик — переводим кнопку в режим подтверждения, второй — удаляем.
    if (deleteState.id !== item.id) {
      setDeleteState({ id: item.id, busy: false, error: "", info: "" });
      return;
    }
    if (deleteState.busy) return;
    setDeleteState({ id: item.id, busy: true, error: "", info: "" });
    try {
      const result = await authJson(`/api/admin/users/${item.id}`, { method: "DELETE" });
      trackEvent("admin_user_deleted", "admin", String(result?.deletedEmail || item.email));
      setDeleteState({ id: null, busy: false, error: "", info: `Пользователь ${result?.deletedEmail || item.email} удален из базы.` });
      load();
    } catch (error) {
      setDeleteState({
        id: null,
        busy: false,
        error: error.message === "AUTH_REQUIRED" ? "Сессия истекла. Войдите снова." : error.message || "Не удалось удалить пользователя.",
        info: "",
      });
    }
  }

  // Вкладка качества появляется только при включённом контуре ИИ.
  const tabs = ADMIN_TABS.filter((item) => item.code !== "quality" || aiStatus?.enabled);
  const current = tabs.find((item) => item.code === tab) || tabs[0];

  const users = usersState.data?.users || [];
  const activity = activityState.data || null;
  const visitsInPeriod = activity?.totalVisits || 0;
  const totalSecondsInPeriod = (activity?.daily || []).reduce((sum, day) => sum + (day.totalSeconds || 0), 0);
  const avgVisitSeconds = visitsInPeriod ? Math.round(totalSecondsInPeriod / visitsInPeriod) : 0;
  const pwaShare = visitsInPeriod ? Math.round(((activity?.pwaVisits || 0) / visitsInPeriod) * 100) : 0;

  const summaryCards = [
    { label: "Пользователей", value: asInt(usersState.data?.totalUsers || 0), helper: "всего в системе" },
    { label: "Активны за 7 дней", value: asInt(usersState.data?.activeLast7d || 0), helper: "заходили в приложение" },
    { label: `Визитов за ${days} дн.`, value: asInt(visitsInPeriod), helper: `суммарно ${formatDurationShort(totalSecondsInPeriod)}` },
    { label: "Средний визит", value: formatDurationShort(avgVisitSeconds), helper: `PWA: ${pwaShare}% визитов` },
  ];

  const dailyItems = (activity?.daily || []).map((day) => ({
    key: day.date,
    label: new Date(`${day.date}T00:00:00`).toLocaleDateString("ru-RU", { day: "2-digit", month: "2-digit" }),
    value: day.visits,
    helper: `${asInt(day.users)} чел · ${formatDurationShort(day.totalSeconds)}`,
  }));

  const screenItems = (activity?.screens || []).map((item) => ({
    key: item.screen,
    label: adminScreenLabel(item.screen),
    value: item.views,
    helper: `${asInt(item.users)} чел`,
  }));

  const actionItems = (activity?.actions || []).map((item) => ({
    key: item.event,
    label: ADMIN_ACTION_LABELS[item.event] || item.event,
    value: item.count,
  }));

  const deviceItems = (activity?.deviceTypes || []).map((item) => ({
    key: item.label,
    label: item.label === "mobile" ? "Смартфон" : item.label === "tablet" ? "Планшет" : item.label === "desktop" ? "Компьютер" : item.label,
    value: item.count,
  }));

  const browserItems = (activity?.browsers || []).map((item) => ({ key: item.label, label: item.label, value: item.count }));

  const loading = usersState.status === "loading" || activityState.status === "loading";
  const errorText = usersState.error || activityState.error;

  return (
    <section className="admin-pane" aria-labelledby="admin-title">
      <div className="admin-head">
        <div>
          <h2 id="admin-title">{current.title}</h2>
          <p>{current.note}</p>
        </div>
        <div className="admin-head-actions">
          {tab === "usage" && (
            <div className="admin-days-toggle" role="group" aria-label="Период статистики">
              {[7, 30, 90].map((value) => (
                <button key={value} type="button" className={days === value ? "active" : ""} onClick={() => setDays(value)}>
                  {value} дн.
                </button>
              ))}
            </div>
          )}
          {tab !== "quality" && (
            <button className="admin-refresh" type="button" onClick={load} aria-label="Обновить статистику">
              <RefreshCw size={15} />
              <span>Обновить</span>
            </button>
          )}
          {onBack && (
            <button className="admin-back" type="button" onClick={onBack}>
              На главную
            </button>
          )}
        </div>
      </div>

      <div className="admin-tabs" role="tablist" aria-label="Разделы администрирования">
        {tabs.map((item) => (
          <button
            key={item.code}
            type="button"
            role="tab"
            aria-selected={tab === item.code}
            className={tab === item.code ? "active" : ""}
            onClick={() => setTab(item.code)}
          >
            {item.tab}
          </button>
        ))}
      </div>

      {tab === "quality" && <AiQualityPanel />}

      {tab !== "quality" && errorText && <p className="admin-error" role="alert">{errorText}</p>}
      {tab !== "quality" && loading && !errorText && <p className="admin-empty">Загружаем статистику…</p>}

      {tab !== "quality" && !loading && !errorText && (
        <>
          {tab === "usage" && (
          <>
          <div className="admin-summary">
            {summaryCards.map((card) => (
              <div className="admin-summary-card" key={card.label}>
                <small>{card.label}</small>
                <strong>{card.value}</strong>
                <em>{card.helper}</em>
              </div>
            ))}
          </div>

          <div className="admin-grid">
            <div className="admin-card">
              <h3>Визиты по дням</h3>
              <AdminBarList items={dailyItems} valueLabel="Число визитов · уникальные пользователи · суммарное время" />
            </div>
            <div className="admin-card">
              <h3>Популярность разделов</h3>
              <AdminBarList items={screenItems} valueLabel="Просмотры экранов за период" />
            </div>
            <div className="admin-card">
              <h3>Действия</h3>
              <AdminBarList items={actionItems} valueLabel="Ключевые действия за период" />
            </div>
            <div className="admin-card">
              <h3>Устройства</h3>
              <AdminBarList items={deviceItems} />
              <h3 className="admin-subhead">Браузеры</h3>
              <AdminBarList items={browserItems} />
            </div>
          </div>

          </>
          )}

          {tab === "users" && (
          <div className="admin-card admin-users-card">
            {deleteState.error && <p className="admin-error" role="alert">{deleteState.error}</p>}
            {deleteState.info && <p className="admin-info" role="status">{deleteState.info}</p>}
            <div className="admin-table-wrap">
              <table className="admin-table">
                <thead>
                  <tr>
                    <th>Пользователь</th>
                    <th>Регистрация</th>
                    <th>Последний вход</th>
                    <th>Был в сети</th>
                    <th>Визитов</th>
                    <th>Всего времени</th>
                    <th>Средний визит</th>
                    <th>Действий</th>
                    <th>Роль и область данных</th>
                    <th aria-label="Управление" />
                  </tr>
                </thead>
                <tbody>
                  {users.map((item) => (
                    <tr key={item.id}>
                      <td>
                        <strong>{item.name || "Без ФИО"}</strong>
                        <small>{item.email}</small>
                      </td>
                      <td>{formatUnixDate(item.createdAt)}</td>
                      <td>{formatUnixDateTime(item.lastLoginAt)}</td>
                      <td>{formatUnixDateTime(item.lastSeenAt)}</td>
                      <td className="admin-num">{asInt(item.visitCount)}</td>
                      <td className="admin-num">{formatDurationShort(item.totalSeconds)}</td>
                      <td className="admin-num">{formatDurationShort(item.avgSeconds)}</td>
                      <td className="admin-num">{asInt(item.eventCount)}</td>
                      <td>
                        <RoleCell item={item} catalog={rolesCatalog} onSaved={load} />
                      </td>
                      <td className="admin-actions-cell">
                        {item.id === currentUserId ? (
                          <span className="admin-self-tag">это вы</span>
                        ) : item.isAdmin ? (
                          <span className="admin-self-tag" title="Уберите адрес из ADMIN_EMAILS, чтобы удалить">админ</span>
                        ) : (
                          <button
                            type="button"
                            className={`admin-delete-btn ${deleteState.id === item.id ? "confirm" : ""}`}
                            disabled={deleteState.busy && deleteState.id === item.id}
                            onClick={() => handleDeleteUser(item)}
                            onBlur={() => {
                              if (deleteState.id === item.id && !deleteState.busy) {
                                setDeleteState((previous) => (previous.id === item.id ? { ...previous, id: null } : previous));
                              }
                            }}
                            title="Удаляет пользователя и все его данные: сессии, визиты, события, коды"
                          >
                            {deleteState.id === item.id
                              ? deleteState.busy
                                ? "Удаляем…"
                                : "Точно удалить?"
                              : "Удалить"}
                          </button>
                        )}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
            <p className="admin-bar-caption">
              Удаление необратимо: стираются аккаунт, сессии, визиты, события и коды. Повторный клик по кнопке подтверждает удаление.
            </p>
          </div>

          )}

          {tab === "usage" && (
          <div className="admin-card">
            <h3>Последние события входа</h3>
            <div className="admin-table-wrap">
              <table className="admin-table">
                <thead>
                  <tr>
                    <th>Когда</th>
                    <th>Email</th>
                    <th>Событие</th>
                    <th>IP</th>
                  </tr>
                </thead>
                <tbody>
                  {(activity?.recentAuthEvents || []).slice(0, 25).map((item, index) => (
                    <tr key={`${item.createdAt}-${index}`}>
                      <td>{formatUnixDateTime(item.createdAt)}</td>
                      <td>{item.email}</td>
                      <td>
                        {ADMIN_AUTH_EVENT_LABELS[item.event] || item.event}
                        {item.reason ? <small>{item.reason}</small> : null}
                      </td>
                      <td>{item.ip || "—"}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </div>
          )}
        </>
      )}
    </section>
  );
}

function HomeDashboard({
  count,
  total,
  metrics,
  meta,
  regionCount,
  excludedNoCoords,
  canInstall,
  standalone,
  user,
  onInstallApp,
  onOpenList,
  onOpenMap,
  onOpenAnalytics,
  onOpenAi,
  onOpenControl,
  onOpenFavorites,
  onOpenAdmin,
  onLogout,
}) {
  const activeShare = total ? Math.round((metrics.active / total) * 100) : 0;
  const homeLinks = [
    { label: "Реестр", Icon: List, onClick: onOpenList },
    { label: "Карта", Icon: MapIcon, onClick: onOpenMap },
    { label: "Аналитика", Icon: BarChart3, onClick: onOpenAnalytics },
    ...(onOpenAi ? [{ label: "ИИ-аналитик", Icon: Sparkles, onClick: onOpenAi }] : []),
    { label: "Контроль", Icon: ShieldCheck, onClick: onOpenControl },
    { label: "Избранное", Icon: Heart, onClick: onOpenFavorites },
  ];
  if (user?.isAdmin && onOpenAdmin) {
    homeLinks.push({ label: "Админ", Icon: Users, onClick: onOpenAdmin });
  }
  const passportItems = [
    { label: "Объектов", value: asInt(total), helper: `в текущем срезе ${asInt(count)}` },
    { label: "Активная сеть", value: `${activeShare}%`, helper: `${asInt(metrics.active)} действующих` },
    { label: "Регионов", value: asInt(regionCount), helper: "география присутствия" },
    { label: "Источник", value: formatMetaDate(meta), helper: excludedNoCoords ? `без координат ${asInt(excludedNoCoords)}` : "координаты готовы" },
  ];

  return (
    <section className="home-pane dala-home" aria-labelledby="home-title">
      <div className="home-hero">
        <div className="hero-orb hero-orb-1" aria-hidden="true" />
        <div className="hero-orb hero-orb-2" aria-hidden="true" />
        <div className="hero-orb hero-orb-3" aria-hidden="true" />
        <div className="home-copy">
          <span>Классификатор АЗС</span>
          <h2 id="home-title">Операционный контур АЗС</h2>
          <p>
            Инструмент собирает реестр АЗС, координаты, сервисы, классификацию, контакты, показатели месяца и рекомендации по персоналу в одном рабочем контуре.
          </p>
          <div className="home-passport" aria-label="Паспорт данных">
            {passportItems.map((item, index) => (
              <motion.div
                className="home-passport-item"
                key={item.label}
                initial={{ opacity: 0, y: 10 }}
                animate={{ opacity: 1, y: 0 }}
                transition={{ duration: 0.24, delay: 0.04 + index * 0.035 }}
              >
                <small>{item.label}</small>
                <strong>{item.value}</strong>
                <em>{item.helper}</em>
              </motion.div>
            ))}
          </div>
          <nav className="home-section-nav" aria-label="Переходы с главной">
            {homeLinks.map(({ label, Icon, onClick }, index) => (
              <motion.button
                type="button"
                key={label}
                onClick={onClick}
                initial={{ opacity: 0, y: 8 }}
                animate={{ opacity: 1, y: 0 }}
                transition={{ duration: 0.22, delay: 0.08 + index * 0.04 }}
                whileTap={{ scale: 0.97 }}
              >
                <Icon size={16} />
                <span>{label}</span>
              </motion.button>
            ))}
          </nav>
        </div>
      </div>

      <PwaInstallGuide canInstall={canInstall} standalone={standalone} onInstall={onInstallApp} />

      <div className="home-grid">
        <motion.button className="home-card home-feedback-card" type="button" aria-labelledby="home-card-feedback" onClick={onOpenControl} whileHover={{ y: -3, boxShadow: "0 18px 40px rgba(21,27,36,0.11)" }} whileTap={{ scale: 0.99 }} transition={{ duration: 0.18 }}>
          <MessageSquare size={18} aria-hidden="true" />
          <h3 id="home-card-feedback">Обратная связь</h3>
          <p>Сообщите о неточности в карточке АЗС или оставьте уточнение по данным.</p>
          <span className="home-feedback-action">Перейти в контроль <ChevronRight size={16} aria-hidden="true" /></span>
        </motion.button>
      </div>

      <div className="home-user-panel" aria-label="Текущий пользователь">
        {user && (
          <div>
            <ShieldCheck size={15} />
            <span>
              <strong>Пользователь: {user.name || "без ФИО"}</strong>
              <small>{user.email}</small>
            </span>
          </div>
        )}
        <button className="home-logout-button" type="button" onClick={onLogout}>
          Выйти
        </button>
      </div>
    </section>
  );
}

function MetricStrip({ count, metrics }) {
  return (
    <div className="metrics">
      <Metric label="Найдено" value={count} />
      <Metric label="Действующих" value={metrics.active} tone="green" />
      <Metric label="Кафе" value={metrics.cafe} tone="red" />
      <Metric label="С санузлом" value={metrics.toilet} tone="amber" />
    </div>
  );
}

function AnalyticsDashboard({ stations, totalStations, selected, onFilter, onOpenList, onOpenStation }) {
  const [period, setPeriod] = useState(() => currentMonthPeriod());
  const [periods, setPeriods] = useState([]);
  const [view, setView] = useState("overview");
  const [groupBy, setGroupBy] = useState("territoryManager");
  const [outageGroupBy, setOutageGroupBy] = useState("region");
  const [overviewState, setOverviewState] = useState({ status: "idle", data: null, error: "" });
  const [outageAnalyticsState, setOutageAnalyticsState] = useState({ status: "idle", data: null, error: "" });
  const [similarState, setSimilarState] = useState({ status: "idle", data: null, error: "" });
  const [compareState, setCompareState] = useState({ status: "idle", data: null, error: "" });
  const [similarBaseId, setSimilarBaseId] = useState("");
  const [similarGeo, setSimilarGeo] = useState({ status: "idle", message: "" });
  const [similarAutoTried, setSimilarAutoTried] = useState(false);
  const [compareIds, setCompareIds] = useState(() => (selected?.ksss ? [selected.ksss] : []));
  const [compareNotice, setCompareNotice] = useState("");
  const similarBase = stations.find((station) => station.ksss === similarBaseId);

  useEffect(() => {
    trackEvent("screen_view", `analytics:${view}`);
  }, [view]);

  useEffect(() => {
    const controller = new AbortController();
    fetchJson("/api/kpis/periods", controller.signal)
      .then((data) => {
        const available = Array.isArray(data?.periods) ? data.periods : [];
        setPeriods(available);
        if (available.length && !available.includes(period)) {
          setPeriod(available[available.length - 1]);
        }
      })
      .catch(() => {});

    return () => controller.abort();
  }, []);

  useEffect(() => {
    if (!selected?.ksss || compareIds.length) return;
    setCompareIds([selected.ksss]);
  }, [selected?.ksss, compareIds.length]);

  useEffect(() => {
    if (!similarBaseId) return;
    if (!stations.some((station) => station.ksss === similarBaseId)) {
      setSimilarBaseId("");
      setSimilarState({ status: "no-data", data: null, error: "" });
    }
  }, [stations, similarBaseId]);

  useEffect(() => {
    if (view !== "similar" || similarBaseId || similarAutoTried) return undefined;
    let cancelled = false;
    setSimilarAutoTried(true);
    setSimilarGeo({ status: "locating", message: "Определяем ближайшую АЗС..." });

    async function detectNearest() {
      try {
        let location;
        try {
          if (!YANDEX_MAPS_API_KEY) throw new Error("YANDEX_MAPS_API_KEY_MISSING");
          const { version, api } = await loadYandexMaps(YANDEX_MAPS_API_KEY);
          location = await getYandexLocation(api, version);
        } catch (yandexError) {
          location = await getBrowserLocation();
        }

        if (cancelled) return;
        const nearest = nearestStation(location, stations);
        if (nearest.station) {
          setSimilarBaseId(nearest.station.ksss);
          setSimilarGeo({
            status: "found",
            message: `Ближайшая АЗС: ${nearest.station.name || nearest.station.stationNumber} · ${formatDistance(nearest.distance)}`,
          });
        } else {
          setSimilarGeo({ status: "empty", message: "Не нашли АЗС с координатами в текущей выборке." });
        }
      } catch (error) {
        if (cancelled) return;
        setSimilarGeo({ status: "manual", message: "Геолокация недоступна. Начните вводить КССС, номер или адрес." });
      }
    }

    detectNearest();
    return () => {
      cancelled = true;
    };
  }, [view, similarBaseId, similarAutoTried, stations]);

  useEffect(() => {
    if (view !== "slices") return undefined;
    const controller = new AbortController();
    setOverviewState({ status: "loading", data: null, error: "" });

    fetchJson(`/api/analytics/overview?period=${period}&groupBy=${groupBy}`, controller.signal)
      .then((data) => {
        if (!data || !Array.isArray(data.rows) || !data.rows.length) {
          setOverviewState({ status: "no-data", data: null, error: "" });
          return;
        }
        setOverviewState({ status: "ready", data, error: "" });
      })
      .catch((error) => {
        if (error.name === "AbortError") return;
        setOverviewState({ status: "error", data: null, error: error.message });
      });

    return () => controller.abort();
  }, [view, groupBy, period]);

  useEffect(() => {
    if (view !== "outages") return undefined;
    const controller = new AbortController();
    setOutageAnalyticsState((current) => ({ status: "loading", data: current.data, error: "" }));

    fetchJson(`/api/analytics/fuel-outages?groupBy=${outageGroupBy}`, controller.signal)
      .then((data) => {
        if (!data || !Array.isArray(data.rows)) {
          setOutageAnalyticsState({ status: "no-data", data: null, error: "" });
          return;
        }
        setOutageAnalyticsState({ status: data.rows.length ? "ready" : "no-data", data, error: "" });
      })
      .catch((error) => {
        if (error.name === "AbortError") return;
        setOutageAnalyticsState({ status: "error", data: null, error: error.message });
      });

    return () => controller.abort();
  }, [view, outageGroupBy]);

  useEffect(() => {
    if (view !== "similar") return undefined;
    if (!similarBaseId) {
      setSimilarState({ status: "no-data", data: null, error: "" });
      return undefined;
    }

    const controller = new AbortController();
    setSimilarState({ status: "loading", data: null, error: "" });

    fetchJson(`/api/stations/${encodeURIComponent(similarBaseId)}/similar?period=${period}&limit=10`, controller.signal)
      .then((data) => {
        if (!data || !Array.isArray(data.items) || !data.items.length) {
          setSimilarState({ status: "no-data", data: null, error: "" });
          return;
        }
        setSimilarState({ status: "ready", data, error: "" });
      })
      .catch((error) => {
        if (error.name === "AbortError") return;
        setSimilarState({ status: "error", data: null, error: error.message });
      });

    return () => controller.abort();
  }, [view, similarBaseId, period]);

  useEffect(() => {
    if (view !== "compare") return undefined;
    if (!compareIds.length) {
      setCompareState({ status: "no-data", data: null, error: "" });
      return undefined;
    }

    const controller = new AbortController();
    const params = compareIds.map((id) => `ksss=${encodeURIComponent(id)}`).join("&");
    setCompareState({ status: "loading", data: null, error: "" });

    fetchJson(`/api/analytics/compare?period=${period}&${params}`, controller.signal)
      .then((data) => {
        if (!data || !Array.isArray(data.items) || !data.items.length) {
          setCompareState({ status: "no-data", data: null, error: "" });
          return;
        }
        setCompareState({ status: "ready", data, error: "" });
      })
      .catch((error) => {
        if (error.name === "AbortError") return;
        setCompareState({ status: "error", data: null, error: error.message });
      });

    return () => controller.abort();
  }, [view, compareIds, period]);

  function addCompare(ksss) {
    if (!ksss || compareIds.includes(ksss)) return;
    if (compareIds.length >= 5) {
      setCompareNotice("Можно сравнить до 5 АЗС одновременно.");
      return;
    }
    setCompareNotice("");
    setCompareIds((current) => [...current, ksss]);
  }

  function addCompareMany(ksssValues) {
    setCompareIds((current) => {
      const next = [...current];
      for (const ksss of ksssValues) {
        if (!ksss || next.includes(ksss)) continue;
        if (next.length >= 5) {
          setCompareNotice("Можно сравнить до 5 АЗС одновременно.");
          return next;
        }
        next.push(ksss);
      }
      setCompareNotice("");
      return next;
    });
  }

  function removeCompare(ksss) {
    setCompareNotice("");
    setCompareIds((current) => current.filter((item) => item !== ksss));
  }

  const summaryCatalog = useSummaryCatalog();
  const tabs = [
    ...(summaryCatalog ? [["summary", "Свод"]] : []),
    ["overview", "Обзор"],
    ["slices", "Разрезы"],
    ["outages", "Простои"],
    ["similar", "Аналоги"],
    ["compare", "Сравнение"],
  ];

  return (
    <section className="analytics-pane">
      <div className="analytics-head">
        <div>
          <h2>Аналитика сети</h2>
          <p>
            {view === "summary"
              ? "Свод по витрине: состав плиток настраивается, сравнение идёт с тем же отрезком прошлого года."
              : view === "overview"
              ? `Показатели пересчитываются по текущей выборке: ${asInt(stations.length)} из ${asInt(totalStations.length)} объектов.`
              : view === "outages"
                ? outageAnalyticsState.data?.reportDate
                  ? `Последний отчет: ${formatOutageDate(outageAnalyticsState.data.reportDate)} · источник API /api`
                  : "Сводка по последнему загруженному отчету о простоях топлива."
              : `Период: ${formatPeriod(period)} · источник API /api`}
          </p>
        </div>
        {view !== "outages" && view !== "summary" && periods.length > 1 && <PeriodNavigator periods={periods} period={period} onChange={setPeriod} />}
      </div>

      <div className="analytics-tabs" role="tablist" aria-label="Режим аналитики">
        {tabs.map(([id, label]) => (
          <button className={view === id ? "active" : ""} type="button" key={id} role="tab" aria-selected={view === id} onClick={() => setView(id)}>
            {label}
          </button>
        ))}
      </div>

      {view === "summary" && summaryCatalog && <AnalyticsSummary catalog={summaryCatalog} />}
      {view === "overview" && (
        <AnalyticsLocalOverview
          stations={stations}
          totalStations={totalStations}
          onFilter={onFilter}
          onOpenList={onOpenList}
        />
      )}
      {view === "slices" && (
        <AnalyticsSlices
          state={overviewState}
          groupBy={groupBy}
          setGroupBy={setGroupBy}
        />
      )}
      {view === "outages" && (
        <AnalyticsFuelOutages
          state={outageAnalyticsState}
          groupBy={outageGroupBy}
          setGroupBy={setOutageGroupBy}
        />
      )}
      {view === "similar" && (
        <AnalyticsSimilar
          stations={stations}
          selected={similarBase}
          geo={similarGeo}
          state={similarState}
          onSelectBase={(station) => {
            setSimilarBaseId(station.ksss);
            setSimilarGeo({ status: "manual", message: "АЗС выбрана вручную." });
          }}
          onClearBase={() => {
            setSimilarBaseId("");
            setSimilarState({ status: "no-data", data: null, error: "" });
            setSimilarGeo({ status: "manual", message: "Начните вводить КССС, номер или адрес." });
          }}
          onOpenStation={onOpenStation}
          onCompare={(ksss) => {
            addCompareMany([similarBaseId, ksss]);
            setView("compare");
          }}
        />
      )}
      {view === "compare" && (
        <AnalyticsCompare
          stations={stations}
          state={compareState}
          compareIds={compareIds}
          notice={compareNotice}
          onAdd={addCompare}
          onRemove={removeCompare}
        />
      )}
    </section>
  );
}

// ---------------------------------------------------------------------------
// Демонстрационный контур ИИ: вопрос на русском -> SQL -> ответ из витрины.
// Раздел появляется, только если бэкенд отвечает на /api/ai/status.
// Переключатель роли здесь демонстрирует область данных, а не ролевую модель
// продукта: она описана в бизнес-требованиях отдельно.
// ---------------------------------------------------------------------------
// Примеры привязаны к периодам, которые в витрине действительно есть.
// Пилотная заливка — сентябрь 2026 и сентябрь 2025; при расширении обновить.
// Подсказка и вопрос — разные вещи. На кнопке нужен короткий ярлык, который
// читается с одного взгляда; модели уходит полная формулировка с периодом,
// иначе она начнёт угадывать год.
const AI_EXAMPLES = [
  { hint: "Выручка НТУ за сентябрь", ask: "Выручка НТУ по моим АЗС за сентябрь 2026" },
  { hint: "Выполнение плана НТУ", ask: "Выполнение плана НТУ в текущем месяце" },
  { hint: "Топливо в прошлом году", ask: "Сравни объём топлива за сентябрь 2026 с сентябрём 2025" },
  { hint: "Топ-5 по конверсии", ask: "Топ-5 АЗС по конверсии за сентябрь 2026" },
  { hint: "Средний чек по ОНПО", ask: "Средний чек НТУ по ОНПО за сентябрь 2026" },
  { hint: "АЗС с кафе на трассе", ask: "Сколько действующих АЗС с кафе на трассе" },
];

function RoleCell({ item, catalog, onSaved }) {
  const [editing, setEditing] = useState(false);
  const [role, setRole] = useState(item.role || "");
  const [binding, setBinding] = useState(item.roleBinding || "");
  const [state, setState] = useState({ busy: false, error: "" });

  const spec = (catalog?.roles || []).find((r) => r.code === role);
  const options = catalog?.options?.[role] || [];
  const needsBinding = Boolean(spec && spec.bindingKind !== "none");
  const freeform = spec?.bindingKind === "list";

  async function save() {
    setState({ busy: true, error: "" });
    try {
      const response = await fetch(`/api/admin/users/${item.id}/role`, {
        method: "POST",
        credentials: "include",
        headers: { "Content-Type": "application/json", Accept: "application/json" },
        body: JSON.stringify({ role, binding: needsBinding ? binding : "" }),
      });
      const data = await response.json();
      if (!response.ok) throw new Error(data.detail || `Ошибка ${response.status}`);
      setEditing(false);
      setState({ busy: false, error: "" });
      onSaved?.();
    } catch (error) {
      setState({ busy: false, error: error.message || "Не удалось сохранить" });
    }
  }

  if (!editing) {
    return (
      <div className="admin-role-cell">
        <strong>{item.roleTitle}</strong>
        {item.roleBinding && <small>{item.roleBinding}</small>}
        <small className={item.role && item.scopeStations === 0 ? "admin-role-warn" : ""}>
          {item.scopeStations < 0 ? "вся сеть" : `${asInt(item.scopeStations)} объектов`}
        </small>
        {!item.isAdmin && (
          <button type="button" className="admin-role-edit" onClick={() => setEditing(true)}>
            изменить
          </button>
        )}
      </div>
    );
  }

  return (
    <div className="admin-role-cell editing">
      <select className="ui-select" value={role} onChange={(event) => { setRole(event.target.value); setBinding(""); }}>
        <option value="">Роль не назначена</option>
        {(catalog?.roles || []).map((r) => (
          <option key={r.code} value={r.code}>{r.title}</option>
        ))}
      </select>

      {needsBinding && (freeform || options.length === 0 ? (
        <input
          className="ui-input"
          type="text"
          value={binding}
          placeholder={spec?.bindingLabel || "Привязка"}
          onChange={(event) => setBinding(event.target.value)}
        />
      ) : (
        <select className="ui-select" value={binding} onChange={(event) => setBinding(event.target.value)}>
          <option value="">{spec?.bindingLabel || "Выберите"}</option>
          {options.map((option) => (
            <option key={option.value} value={option.value}>
              {option.value}{option.stations ? ` (${option.stations})` : ""}
            </option>
          ))}
        </select>
      ))}

      {state.error && <small className="admin-role-warn">{state.error}</small>}
      <div className="admin-role-buttons">
        <button type="button" className="ui-button" onClick={save} disabled={state.busy}>
          {state.busy ? "Сохраняю…" : "Сохранить"}
        </button>
        <button type="button" className="ui-button ghost" onClick={() => { setEditing(false); setState({ busy: false, error: "" }); }}>
          Отмена
        </button>
      </div>
    </div>
  );
}

function ScopeNotice({ meta }) {
  const scope = meta?.scope;
  if (!scope || scope.unrestricted) return null;

  const blocked = scope.problems?.length > 0 || scope.stations === 0;
  return (
    <div className={blocked ? "scope-notice blocked" : "scope-notice"}>
      {blocked ? (
        <>
          <strong>Объекты не показаны.</strong>{" "}
          {scope.problems?.length
            ? scope.problems.join(". ")
            : "В вашей области данных нет действующих объектов."}{" "}
          Обратитесь к администратору — он назначает роль и привязку.
        </>
      ) : (
        <>
          Область данных: <strong>{scope.label}</strong>. Показаны только ваши объекты —
          реестр, карта, аналитика и сравнение считаются по ним.
        </>
      )}
    </div>
  );
}

function useAiStatus() {
  const [status, setStatus] = useState(undefined);
  useEffect(() => {
    let alive = true;
    fetchJson("/api/ai/status")
      .then((data) => {
        if (alive) setStatus(data || null);
      })
      .catch(() => {
        if (alive) setStatus(null);
      });
    return () => {
      alive = false;
    };
  }, []);
  return status;
}

function aiFormatCell(value, decimals) {
  if (value === null || value === undefined) return "—";
  if (typeof value === "number") {
    const places = Number.isInteger(decimals)
      ? decimals
      : (Math.abs(value % 1) > 0 ? 2 : 0);
    return value.toLocaleString("ru-RU", {
      minimumFractionDigits: places,
      maximumFractionDigits: places,
    });
  }
  return String(value);
}

// Разрядность берётся по колонке, а не по ячейке: иначе в столбце процентов
// рядом стоят «43,1» и «33», и колонка перестаёт читаться как один ряд чисел.
function aiColumnDecimals(rows, index) {
  let places = 0;
  for (const row of rows) {
    const value = row[index];
    if (typeof value !== "number" || Number.isInteger(value)) continue;
    const text = String(value);
    const dot = text.indexOf(".");
    if (dot >= 0) places = Math.max(places, Math.min(2, text.length - dot - 1));
  }
  return places;
}

function aiIdentityKey(identity) {
  return `${identity.role}::${identity.binding || ""}`;
}

// ---------------------------------------------------------------------------
// Раздел «ИИ-аналитик»: история диалогов, лента ответов, оценка точности.
// Разметка следует прототипу, согласованному с владельцем 21.09.2026.
// Знак ИИ собран по спецификации облика: круг, красный контур, три столбца.
// Шейдерная версия знака придёт отдельно (З-16) и заменит этот SVG.
// ---------------------------------------------------------------------------
// Размер задаётся либо числом отсюда, либо стилями — тогда size не передаётся
// и знак подстраивается под высоту окна. Инлайновый стиль перебивает правило,
// поэтому ставить его «на всякий случай» нельзя.
// Орб ИИ-аналитика. Облик согласован владельцем — «Облик_ИИ_аналитика_
// заполнено.xlsx» (З-16): круг с красным контуром и тремя белыми столбцами,
// восемь состояний, корпоративная гамма.
//
// Здесь собран обязательный вариант на SVG и CSS. По спецификации он нужен
// всегда: крупный орб на мощной машине рисует шейдер, но при отключённой
// анимации, на слабом устройстве и в статике показывается именно этот знак.
// Поэтому он не «заглушка до шейдера», а самостоятельный уровень.
//
// В SVG нет ни <defs>, ни градиентов, ни фильтров: им обязательны id, а знак
// рисуется на странице до пяти раз — одинаковые id сделали бы разметку
// невалидной. Поэтому корпус и внутреннее свечение нарисованы фоном самой
// обёртки: у CSS радиальные градиенты есть, и красный получается настоящим
// светом внутри, а не плоской заливкой поверх графита.
const AI_MARK_STATES = new Set([
  "idle",     // покой: вопроса нет
  "parse",    // разбор вопроса: модель читает формулировку
  "draft",    // составление запроса к витрине
  "check",    // проверка допустимости
  "read",     // чтение витрины — основная стадия
  "done",     // ответ готов
  "refused",  // вне области данных: контролируемый исход, не авария
  "error",    // модель недоступна или витрина не ответила
]);

// Старые названия состояний, чтобы прежние места вызова не сломались.
const AI_MARK_ALIASES = {
  working: "read",
  ready: "done",
  clarify: "refused",   // просьба уточнить — такой же спокойный исход, как отказ
  write: "read",        // спецификация не описывает отдельный вид для «формулирую ответ»
};

// Подпись для тех, кто пользуется экранным диктором: состояние орба иначе
// не читается вовсе.
const AI_MARK_TITLES = {
  idle: "ИИ-аналитик",
  parse: "Разбираю вопрос",
  draft: "Составляю запрос к витрине",
  check: "Проверяю допустимость запроса",
  read: "Читаю витрину",
  done: "Ответ готов",
  refused: "Запрос вне области данных",
  error: "Ответ не получен",
};

function AiMark({ size, state = "idle", title, simple = false, decorative = false, variant = "plain" }) {
  const resolved = AI_MARK_ALIASES[state] || state;
  const mode = AI_MARK_STATES.has(resolved) ? resolved : "idle";
  // decorative — рядом уже есть видимая подпись («ИИ», «ИИ-аналитик»),
  // и диктор не должен читать её дважды.
  const label = decorative ? "" : (title || AI_MARK_TITLES[mode]);

  // Вариант plain — прежний знак в блоках ленты и на панели: графитовый
  // круг с тремя белыми столбцами, без красного контура. Владелец вернул
  // его 22.09.2026: орб — для крупных мест, мелкий значок должен быть тихим.
  // Состояние здесь читается только по столбцам: работа — пульс,
  // отказ и ошибка — приглушённый цвет.
  if (variant === "plain") {
    const busy = ["parse", "draft", "check", "read"].includes(mode);
    const bad = mode === "refused" || mode === "error";
    return (
      <span
        className={`ai-mark ai-mark-plain${busy ? " is-busy" : ""}${bad ? " is-bad" : ""}`}
        style={size ? { "--ai-mark-size": `${size}px` } : undefined}
      >
        <svg viewBox="0 0 100 100" role={label ? "img" : "presentation"} aria-label={label || undefined}>
          <circle cx="50" cy="50" r="48" className="ai-plain-body" />
          <circle cx="50" cy="36" r="40" className="ai-plain-sheen" />
          <rect x="38" y="52" width="6" height="18" rx="2" className="ai-plain-bar b1" />
          <rect x="47" y="44" width="6" height="26" rx="2" className="ai-plain-bar b2" />
          <rect x="56" y="36" width="6" height="34" rx="2" className="ai-plain-bar b3" />
        </svg>
      </span>
    );
  }

  // Вариант orb — знак с красным контуром и внутренним светом, по
  // спецификации облика. Остался только в навигации.
  // simple — облегчённая сборка: два-три слоя вместо девяти.
  return (
    <span
      className={`ai-mark ai-mark-${mode}${simple ? " ai-mark-simple" : ""}`}
      style={size ? { "--ai-mark-size": `${size}px` } : undefined}
    >
      <svg
        viewBox="0 0 100 100"
        role={label ? "img" : "presentation"}
        aria-label={label || undefined}
      >
        {!simple && (
          <g className="orb-layers" aria-hidden="true">
            {/* вложенные слои: намёк на recursive erosion без шейдера */}
            <circle cx="50" cy="50" r="38" className="orb-layer l1" />
            <circle cx="50" cy="50" r="30" className="orb-layer l2" />
            <circle cx="50" cy="50" r="22" className="orb-layer l3" />
          </g>
        )}
        {!simple && (
          <g className="orb-flow" aria-hidden="true">
            {/* световые потоки: встречные, видны на составлении и чтении */}
            <circle cx="50" cy="50" r="41" className="orb-stream s1" />
            <circle cx="50" cy="50" r="34" className="orb-stream s2" />
            <circle cx="50" cy="50" r="27" className="orb-stream s3" />
          </g>
        )}
        {/* красный контур — постоянная часть знака */}
        <circle cx="50" cy="50" r="49" className="orb-ring" />
        {/* контрольный импульс: проверка и финальный pulse */}
        <circle cx="50" cy="50" r="49" className="orb-pulse" aria-hidden="true" />
        {/* блик стекла */}
        <circle cx="50" cy="32" r="33" className="orb-sheen" aria-hidden="true" />
        {/* три белых столбца — ядро знака, оно же результат */}
        <g className="orb-bars">
          <rect x="38" y="52" width="6" height="18" rx="2" className="orb-bar b1" />
          <rect x="47" y="44" width="6" height="26" rx="2" className="orb-bar b2" />
          <rect x="56" y="36" width="6" height="34" rx="2" className="orb-bar b3" />
        </g>
      </svg>
    </span>
  );
}

// Этап конвейера → состояние орба. Конвейер знает четыре ключа
// (draft, check, read, write), спецификация — восемь состояний; «разбор
// вопроса» наступает до первого события, пока модель читает формулировку.
function aiMarkState({ pending, stages = [], outcome }) {
  if (!pending) {
    if (outcome === "ready") return "done";
    if (outcome === "error") return "error";
    if (outcome === "refused" || outcome === "clarify") return "refused";
    return "idle";
  }
  const active = [...stages].reverse().find((stage) => stage.state === "active");
  if (!active) return "parse";
  return AI_MARK_ALIASES[active.key] || (AI_MARK_STATES.has(active.key) ? active.key : "read");
}

function aiMs(value) {
  if (!Number.isFinite(value) || value <= 0) return "";
  // Разделитель — запятая: «2,1 с», а не «2.1 с».
  return value >= 1000
    ? `${(value / 1000).toLocaleString("ru-RU", { minimumFractionDigits: 1, maximumFractionDigits: 1 })} с`
    : `${Math.round(value)} мс`;
}

function aiTotalMs(answer) {
  return (answer?.modelMs || 0) + (answer?.narrateMs || 0) + (answer?.sqlMs || 0);
}

// Три исхода вместо двух: ошибка системы, граница области данных и просьба
// уточнить. Они выглядят по-разному, потому что человеку нужно разное:
// подождать, попросить права или переспросить точнее.
// Движение в ленте — три вещи и не больше: вход сообщения, раскрытие блока,
// проявление этапов. Всё уважает prefers-reduced-motion: при выключенной
// анимации элементы просто появляются на своих местах.
const AI_EASE = [0.22, 1, 0.36, 1];
const AI_FOLD_SPRING = { type: "spring", stiffness: 340, damping: 34, mass: 0.7 };

function AiReveal({ open, children, className }) {
  const reduced = useReducedMotion();
  return (
    <AnimatePresence initial={false}>
      {open && (
        <motion.div
          className={className}
          style={{ overflow: "hidden" }}
          initial={reduced ? false : { height: 0, opacity: 0 }}
          animate={{ height: "auto", opacity: 1 }}
          exit={reduced ? { opacity: 0 } : { height: 0, opacity: 0 }}
          transition={reduced
            ? { duration: 0.12 }
            : { ...AI_FOLD_SPRING, opacity: { duration: 0.16, ease: AI_EASE } }}
        >
          {children}
        </motion.div>
      )}
    </AnimatePresence>
  );
}

const AI_CLARIFY_RULES = new Set(["parse", "multi", "empty"]);
const AI_FAILURE_RULES = new Set(["execution", "model_unavailable"]);

function aiOutcome(answer) {
  if (!answer) return "idle";
  if (answer.ok) return "ready";
  if (AI_FAILURE_RULES.has(answer.rule)) return "error";
  if (AI_CLARIFY_RULES.has(answer.rule)) return "clarify";
  return "refused";
}

const AI_REFUSAL_TITLES = {
  model_unavailable: "Модель недоступна",
  execution: "Витрина не ответила на запрос",
  unknown_table: "Это вне вашей области данных",
  qualified_table: "Это вне вашей области данных",
  unknown_column: "Такого показателя в витрине нет",
  forbidden_function: "Запрос не разрешён",
  not_select: "Запрос не разрешён",
  parse: "Уточните, о чём речь",
  multi: "Уточните, о чём речь",
  empty: "Уточните, о чём речь",
};

const AI_REFUSAL_HINTS = {
  model_unavailable: "Это сбой контура, а не отказ: повторите вопрос через минуту.",
  execution: "Запрос был допустим, но витрина его не выполнила. Повторите вопрос — если повторится, сообщите администратору.",
  unknown_table: "Область данных назначает администратор: расширение прав идёт через него.",
  qualified_table: "Область данных назначает администратор: расширение прав идёт через него.",
  unknown_column: "Проверьте название показателя — справочник сокращений есть в базе знаний.",
  forbidden_function: "Разрешено только чтение витрины: изменять и удалять данные ИИ не может.",
  not_select: "Разрешено только чтение витрины: изменять и удалять данные ИИ не может.",
  parse: "Назовите показатель, период и объекты — тогда не придётся угадывать.",
  multi: "Задайте один вопрос за раз — так понятнее, что именно считать.",
  empty: "Назовите показатель, период и объекты — тогда не придётся угадывать.",
};

// Этапы строятся только из измеренных величин. Промежутка, который никто
// не засекал, здесь быть не должно: иначе интерфейс рассказывает о работе
// системы то, чего не знает.
function aiStages(answer) {
  if (!answer) return [];
  // Ответ агента несёт свои измеренные шаги: разбор задачи, запросы,
  // вычисления, графики, формулировка. Показываем их, а не этапы FAST.
  if (answer.analysis && (answer.steps || []).length) return aiAgentStages(answer);
  const stages = [
    { key: "draft", label: "Составил запрос к витрине", ms: answer.modelMs, done: true },
  ];
  if (answer.ok) {
    stages.push({
      key: "check",
      label: answer.attempts > 1
        ? `Проверил допустимость — принято с ${answer.attempts}-й попытки`
        : "Проверил допустимость — запрос разрешён, область данных подставлена",
      done: true,
    });
    stages.push({
      key: "read",
      label: `Прочитал витрину — ${asInt(answer.rowCount ?? (answer.rows || []).length)} строк`,
      ms: answer.sqlMs,
      done: true,
    });
    if (answer.narrateMs) {
      stages.push({ key: "write", label: "Сформулировал ответ", ms: answer.narrateMs, done: true });
    }
  } else {
    stages.push({
      key: "check",
      label: AI_REFUSAL_TITLES[answer.rule] || "Проверка не пропустила запрос",
      done: false,
      ms: answer.sqlMs,
    });
  }
  return stages;
}

// Строка рассуждения: одна строка со знаком и временем, разворачивается
// в этапы. Так же, как в знакомых людям ассистентах, — чтобы не объяснять
// отдельно, что за блок висит над ответом.
function aiStageClass(state) {
  if (state === "active") return "ai-stage current";
  if (state === "failed") return "ai-stage failed";
  if (state === "retry") return "ai-stage retry";
  return "ai-stage";
}

function AiThinking({ answer, pending, stages: live_stages = [] }) {
  const [open, setOpen] = useState(false);
  const reduced = useReducedMotion();
  if (pending) {
    // Этапы приходят потоком; пока не пришёл ни один — показываем первый,
    // чтобы строка не висела пустой.
    const live = live_stages.length
      ? live_stages
      : [{ key: "draft", state: "active", label: "Составляю запрос к витрине" }];
    const current = [...live].reverse().find((stage) => stage.state === "active") || live[live.length - 1];
    return (
      <div className="ai-think pending">
        {/* Орб живёт этапами конвейера: составление и проверка — «ищет»,
            чтение витрины — «анализирует», формулирование — «формирует».
            Это настоящие этапы, а не анимация ради анимации: по ним видно,
            что модель не ходит в данные сама. Момент «готово» показывает
            уже AiSettleOrb в пришедшем ответе — он встаёт ровно на это
            место, потому что вопрос и ответ свёрстаны одинаково. */}
        <div className="ai-think-orb">
          <AiOrb
            size="var(--ai-orb-pending, 120px)"
            state={orbStateFromPipeline({ pending: true, stages: live })}
            interactive={false}
            fallback={<AiMark state={aiMarkState({ pending: true, stages: live })} />}
          />
        </div>
        <span className="ai-think-line">
          <strong>{current?.label || "Работаю"}</strong>
          <span className="ai-think-dots" aria-hidden="true"><i /><i /><i /></span>
        </span>
        <div className="ai-think-body">
          <AnimatePresence initial={false}>
            {live.map((stage) => (
              <motion.div
                key={stage.key}
                className={aiStageClass(stage.state)}
                initial={reduced ? false : { opacity: 0, y: 4 }}
                animate={{ opacity: 1, y: 0 }}
                transition={{ duration: 0.18, ease: AI_EASE }}
              >
                <span className="ai-stage-mark" aria-hidden="true">
                  {stage.state === "active"
                    ? <span className="ai-stage-spin" />
                    : stage.state === "failed"
                      ? <X size={13} />
                      : stage.state === "retry"
                        ? <RefreshCw size={13} />
                        : <Check size={13} />}
                </span>
                <span>{stage.label}</span>
                {stage.ms ? <span className="ai-stage-ms">{aiMs(stage.ms)}</span> : null}
              </motion.div>
            ))}
          </AnimatePresence>
        </div>
        <div className="ai-skeleton" aria-hidden="true"><i style={{ width: "82%" }} /><i style={{ width: "94%" }} /><i style={{ width: "61%" }} /></div>
      </div>
    );
  }
  if (!answer) return null;
  const stages = aiStages(answer);
  const total = aiTotalMs(answer);
  const outcome = aiOutcome(answer);
  return (
    <div className="ai-think">
      <button type="button" className="ai-think-line" onClick={() => setOpen((value) => !value)} aria-expanded={open}>
        {/* Тот же графитовый знак, что на боковой панели (решение владельца от 22.09.2026). */}
        <AiMark state={aiMarkState({ outcome })} />
        <strong>{answer.ok ? "Рассуждал" : "Разбирал вопрос"}</strong>
        <span className="ai-think-ms">{aiMs(total) || "меньше секунды"}</span>
        <ChevronDown size={15} className={open ? "ai-caret open" : "ai-caret"} />
      </button>
      <AiReveal open={open}>
        <div className="ai-think-body">
          {stages.map((stage, index) => (
            <motion.div
              key={stage.key}
              className={stage.done ? "ai-stage" : "ai-stage failed"}
              initial={reduced ? false : { opacity: 0, y: 4 }}
              animate={{ opacity: 1, y: 0 }}
              transition={{ duration: 0.18, delay: reduced ? 0 : index * 0.04, ease: AI_EASE }}
            >
              <span className="ai-stage-mark" aria-hidden="true">{stage.done ? <Check size={13} /> : <X size={13} />}</span>
              <span>{stage.label}</span>
              {stage.ms ? <span className="ai-stage-ms">{aiMs(stage.ms)}</span> : null}
            </motion.div>
          ))}
          {answer.model && (
            <motion.p
              className="ai-think-note"
              initial={reduced ? false : { opacity: 0 }}
              animate={{ opacity: 1 }}
              transition={{ duration: 0.2, delay: reduced ? 0 : stages.length * 0.04, ease: AI_EASE }}
            >
              <span>Ход рассуждения — текст модели, не факт</span>
              Запрос составляла модель {answer.model}. Решение о допустимости принимала проверка, данные читал
              исполнитель: сама модель к витрине не обращается.
            </motion.p>
          )}
        </div>
      </AiReveal>
    </div>
  );
}

function AiStars({ value, onPick, size = 38 }) {
  return (
    <span className="ai-stars">
      {[1, 2, 3, 4, 5].map((n) => (
        <button
          key={n}
          type="button"
          className={n <= value ? "on" : ""}
          onClick={onPick ? () => onPick(n) : undefined}
          aria-label={`Оценка ${n} из 5`}
          disabled={!onPick}
        >
          <svg viewBox="0 0 24 24" width={size} height={size} aria-hidden="true">
            <path d="M12 3.2l2.6 5.5 5.9.8-4.3 4.2 1.1 6-5.3-2.9-5.3 2.9 1.1-6L3.5 9.5l5.9-.8z" strokeWidth="1.6" strokeLinejoin="round" />
          </svg>
        </button>
      ))}
    </span>
  );
}

const AI_RATING_HINTS = {
  0: "Выберите оценку",
  1: "Ответ неверный",
  2: "Ответ в основном неверный",
  3: "Ответ частично верный",
  4: "Ответ верный, есть замечания",
  5: "Ответ верный",
};

// Комментарий обязателен при низкой оценке: оценка без причины ничего не даёт
// разбору, а разбирать придётся именно такие ответы.
function AiRatingDialog({ item, requiredUpTo, onClose, onSave }) {
  const [rating, setRating] = useState(item.rating || 0);
  const [comment, setComment] = useState(item.comment || "");
  const [state, setState] = useState({ busy: false, error: "" });

  const mustComment = rating > 0 && rating <= requiredUpTo;
  const blocked = rating === 0 || (mustComment && !comment.trim());

  async function submit() {
    if (blocked) return;
    setState({ busy: true, error: "" });
    try {
      await onSave(rating, comment.trim());
      onClose();
    } catch (error) {
      setState({ busy: false, error: error.message || "Не удалось сохранить оценку" });
    }
  }

  return createPortal(
    <div className="ai-modal-scrim" role="presentation" onClick={onClose}>
      <div
        className="ai-modal"
        role="dialog"
        aria-modal="true"
        aria-label="Оценка точности ответа"
        onClick={(event) => event.stopPropagation()}
      >
        <div className="ai-modal-head">
          <div>
            <p className="ai-modal-title">Насколько точен ответ?</p>
            <p className="ai-modal-sub">Оценка попадает в разбор качества ответов и в эталонный набор вопросов.</p>
          </div>
          <button type="button" className="icon-button" onClick={onClose} aria-label="Закрыть">
            <X size={18} />
          </button>
        </div>

        <p className="ai-modal-question">{item.question}</p>

        <div className="ai-rating-pick">
          <AiStars value={rating} onPick={setRating} />
          <span className="ai-rating-hint">{AI_RATING_HINTS[rating]}</span>
        </div>

        <label className="ui-field">
          <span className={mustComment && !comment.trim() ? "ai-required" : ""}>
            {mustComment ? "Что именно не так — обязательно" : "Комментарий, по желанию"}
          </span>
          <textarea
            className={`ui-textarea${mustComment && !comment.trim() ? " invalid" : ""}`}
            rows={3}
            value={comment}
            onChange={(event) => setComment(event.target.value)}
            placeholder="Что именно не так: цифра, период, область данных, формулировка"
          />
        </label>

        {state.error && <p className="ai-modal-error">{state.error}</p>}

        <div className="ai-modal-actions">
          <button type="button" className="ui-button" disabled={blocked || state.busy} onClick={submit}>
            {state.busy ? "Сохраняю…" : "Отправить"}
          </button>
          <button type="button" className="ui-button ghost" onClick={onClose}>Отмена</button>
          <span className={blocked && mustComment ? "ai-modal-note warn" : "ai-modal-note"}>
            {blocked && mustComment
              ? "При оценке до трёх звёзд нужна причина"
              : "Оценка сохраняется вместе с вопросом и ответом"}
          </span>
        </div>
      </div>
    </div>,
    document.body,
  );
}

// --- история диалогов ------------------------------------------------------

const AI_DAY = 86400;

function aiDialogGroup(updatedAt) {
  const now = new Date();
  const midnight = Math.floor(new Date(now.getFullYear(), now.getMonth(), now.getDate()).getTime() / 1000);
  if (updatedAt >= midnight) return "Сегодня";
  if (updatedAt >= midnight - AI_DAY) return "Вчера";
  if (updatedAt >= midnight - 7 * AI_DAY) return "На этой неделе";
  if (updatedAt >= midnight - 30 * AI_DAY) return "В этом месяце";
  return "Ранее";
}

const AI_GROUP_ORDER = ["Закреплённые", "Сегодня", "Вчера", "На этой неделе", "В этом месяце", "Ранее"];

function aiGroupDialogs(dialogs) {
  const buckets = new Map();
  dialogs.forEach((dialog) => {
    const name = dialog.pinned ? "Закреплённые" : aiDialogGroup(dialog.updated_at || 0);
    if (!buckets.has(name)) buckets.set(name, []);
    buckets.get(name).push(dialog);
  });
  return AI_GROUP_ORDER.filter((name) => buckets.has(name)).map((name) => ({ name, items: buckets.get(name) }));
}

function AiDialogRow({ dialog, active, onOpen, onRename, onPin, onDelete }) {
  const [menu, setMenu] = useState(false);
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState(dialog.title);

  useEffect(() => { setDraft(dialog.title); }, [dialog.title]);

  if (editing) {
    return (
      <div className="ai-dialog editing">
        <input
          className="ui-input"
          value={draft}
          autoFocus
          onChange={(event) => setDraft(event.target.value)}
          onKeyDown={(event) => {
            if (event.key === "Enter") { onRename(draft); setEditing(false); }
            if (event.key === "Escape") { setDraft(dialog.title); setEditing(false); }
          }}
          onBlur={() => { onRename(draft); setEditing(false); }}
          aria-label="Название диалога"
        />
      </div>
    );
  }

  return (
    <div className={active ? "ai-dialog active" : "ai-dialog"}>
      <button type="button" className="ai-dialog-open" onClick={onOpen}>
        <span className="ai-dialog-title">
          {dialog.pinned ? <Pin size={12} aria-label="Закреплён" /> : null}
          <span className="ai-dialog-name">{dialog.title}</span>
        </span>
        {dialog.last_question && <span className="ai-dialog-last">{dialog.last_question}</span>}
      </button>
      <button
        type="button"
        className="ai-dialog-more"
        aria-label="Действия с диалогом"
        aria-expanded={menu}
        onClick={() => setMenu((value) => !value)}
      >
        <MoreHorizontal size={15} />
      </button>
      {menu && (
        <>
          <button type="button" className="ai-menu-scrim" aria-label="Закрыть меню" onClick={() => setMenu(false)} />
          <div className="ai-dialog-menu" role="menu">
            <button type="button" role="menuitem" onClick={() => { setMenu(false); setEditing(true); }}>
              <Pencil size={14} /> Переименовать
            </button>
            <button type="button" role="menuitem" onClick={() => { setMenu(false); onPin(!dialog.pinned); }}>
              {dialog.pinned ? <PinOff size={14} /> : <Pin size={14} />}
              {dialog.pinned ? "Открепить" : "Закрепить"}
            </button>
            <button type="button" role="menuitem" className="danger" onClick={() => { setMenu(false); onDelete(); }}>
              <Trash2 size={14} /> Удалить
            </button>
          </div>
        </>
      )}
    </div>
  );
}

function AiSidebar({ dialogs, activeId, loading, onNew, onOpen, onRename, onPin, onDelete, whoLabel, whoName, onClose }) {
  const [query, setQuery] = useState("");
  const needle = query.trim().toLowerCase();
  const visible = needle
    ? dialogs.filter((dialog) =>
        `${dialog.title} ${dialog.last_question || ""}`.toLowerCase().includes(needle))
    : dialogs;
  const groups = aiGroupDialogs(visible);

  return (
    <aside className="ai-sidebar">
      <div className="ai-sidebar-head">
        <div className="ai-sidebar-brand">
          <AiMark />
          <span>ИИ-аналитик</span>
          {onClose && (
            <button type="button" className="ai-sidebar-close" onClick={onClose} aria-label="Закрыть историю">
              <X size={18} />
            </button>
          )}
        </div>
        <button type="button" className="ui-button ai-new" onClick={onNew}>
          <Plus size={16} /> Новый диалог
        </button>
        <label className="ai-search">
          <Search size={15} />
          <input
            type="search"
            value={query}
            onChange={(event) => setQuery(event.target.value)}
            placeholder="Поиск по диалогам"
            aria-label="Поиск по диалогам"
          />
        </label>
      </div>

      <div className="ai-sidebar-list">
        {loading ? (
          <p className="ai-sidebar-empty">Загружаю историю…</p>
        ) : groups.length === 0 ? (
          <p className="ai-sidebar-empty">
            {needle ? "По этому запросу диалогов нет." : "Диалогов пока нет. Они появятся здесь и будут храниться за вами."}
          </p>
        ) : (
          groups.map((group) => (
            <div className="ai-dialog-group" key={group.name}>
              <p className="ai-group-name">{group.name}</p>
              {group.items.map((dialog) => (
                <AiDialogRow
                  key={dialog.id}
                  dialog={dialog}
                  active={dialog.id === activeId}
                  onOpen={() => onOpen(dialog.id)}
                  onRename={(title) => onRename(dialog.id, title)}
                  onPin={(pinned) => onPin(dialog.id, pinned)}
                  onDelete={() => onDelete(dialog.id)}
                />
              ))}
            </div>
          ))
        )}
      </div>

      <div className="ai-sidebar-foot">
        <span className="ai-avatar" aria-hidden="true">{aiInitials(whoName)}</span>
        <span className="ai-who">
          <strong>{whoName || "Пользователь"}</strong>
          <small>{whoLabel}</small>
        </span>
      </div>
    </aside>
  );
}

function aiInitials(name) {
  const parts = String(name || "").trim().split(/\s+/).filter(Boolean);
  if (!parts.length) return "—";
  if (parts.length === 1) return parts[0].slice(0, 2).toUpperCase();
  return (parts[0][0] + parts[1][0]).toUpperCase();
}

// --- один ответ ------------------------------------------------------------

function AiFold({ title, meta, children, tone }) {
  const [open, setOpen] = useState(false);
  return (
    <div className={tone ? `ai-fold ${tone}` : "ai-fold"}>
      <button type="button" className="ai-fold-head" onClick={() => setOpen((value) => !value)} aria-expanded={open}>
        <ChevronRight size={14} className={open ? "ai-caret open" : "ai-caret"} />
        <strong>{title}</strong>
        {meta ? <span className="ai-fold-meta">{meta}</span> : null}
      </button>
      <AiReveal open={open}>
        <div className="ai-fold-body">{children}</div>
      </AiReveal>
    </div>
  );
}

function AiAnswerBody({ answer, maySeeSql }) {
  const outcome = aiOutcome(answer);

  if (outcome !== "ready") {
    return (
      <div className={`ai-verdict ${outcome}`}>
        {/* Без значка орба: в отказе, уточнении и ошибке достаточно обозначения
            (решение владельца от 22.09.2026). */}
        <div className="ai-verdict-head">
          <p>{AI_REFUSAL_TITLES[answer.rule] || "Запрос отклонён"}</p>
        </div>
        <p className="ai-verdict-text">{answer.error || "Запрос не выполнен."}</p>
        <p className="ai-verdict-hint">{AI_REFUSAL_HINTS[answer.rule] || "Переформулируйте вопрос или обратитесь к администратору."}</p>
        {maySeeSql && answer.sqlRaw ? (
          <AiFold title="Что составила модель" meta={answer.rule ? `правило: ${answer.rule}` : ""} tone="bad">
            <pre className="ai-sql bad">{answer.sqlRaw}</pre>
          </AiFold>
        ) : null}
      </div>
    );
  }

  if (answer.analysis) {
    return (
      <AiAnalysis
        answer={answer}
        maySeeSql={maySeeSql}
        Fold={AiFold}
        fmt={{ cell: aiFormatCell, decimals: aiColumnDecimals, int: asInt }}
      />
    );
  }

  const rows = answer.rows || [];
  const columns = answer.columns || [];
  const single = rows.length === 1 && columns.length === 1;
  const facts = rows.length === 1 && columns.length > 1 && columns.length <= 4;
  const decimals = columns.map((_, index) => aiColumnDecimals(rows, index));

  return (
    <>
      {single && (
        <div className="ai-hero">
          <strong className="ai-hero-value">{aiFormatCell(rows[0][0], decimals[0])}</strong>
          <span className="ai-hero-label">{columns[0]}</span>
          <span className="ai-hero-scope">{answer.scopeLabel}</span>
        </div>
      )}

      {facts && (
        <div className="ai-facts">
          {columns.map((column, index) => (
            <div className="ai-fact" key={column}>
              <div className="ai-fact-value">{aiFormatCell(rows[0][index], decimals[index])}</div>
              <div className="ai-fact-label">{column}</div>
            </div>
          ))}
        </div>
      )}

      {answer.summary && <p className="ai-summary">{answer.summary}</p>}

      {rows.length === 0 && <div className="ai-empty">Запрос выполнен, данных по условию нет.</div>}

      {rows.length > 0 && !single && !facts && (
        <div className="ai-table-card">
          <div className="ai-table-head">
            <span className="ai-table-title">Разбивка по строкам</span>
            <span className="ai-table-count">
              {answer.truncated ? `первые ${asInt(rows.length)}` : `${asInt(rows.length)} строк`}
            </span>
          </div>
          <div className="ai-table-wrap">
            <table className="ai-table">
              <thead>
                <tr>{columns.map((column) => <th key={column}>{column}</th>)}</tr>
              </thead>
              <tbody>
                {rows.map((row, index) => (
                  <tr key={index}>
                    {row.map((cell, cellIndex) => (
                      <td key={cellIndex} className={typeof cell === "number" ? "num" : ""}>
                        {aiFormatCell(cell, decimals[cellIndex])}
                      </td>
                    ))}
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
      )}

      {(answer.notes || []).length > 0 && (
        <ul className="ai-notes">
          {answer.notes.map((note) => <li key={note}>{note}</li>)}
        </ul>
      )}

      <div className="ai-folds">
        <AiFold
          title="Откуда число"
          meta={`DWH ЛИКАРД · ${answer.scopeLabel}`}
        >
          <p className="ai-source-text">
            Источник: DWH ЛИКАРД. Область данных: {answer.scopeLabel} — подставлена системой, а не выбрана моделью.
            Прочитано {asInt(answer.rowCount ?? rows.length)} строк{answer.truncated ? `, показано ${asInt(rows.length)}` : ""}.
            {answer.attempts > 1 ? ` Запрос принят с ${answer.attempts}-й попытки.` : ""}
          </p>
        </AiFold>

        {maySeeSql && answer.sql ? (
          <AiFold title="SQL-запрос" meta="администратор и субадминистратор">
            <pre className="ai-sql">{answer.sql}</pre>
          </AiFold>
        ) : null}
      </div>
    </>
  );
}

// Передача из блока ожидания в ответ. Блок ожидания уходит из дерева в тот
// же кадр, в который приходит ответ, — поэтому «готово» показывает не он,
// а этот орб: он монтируется на том же месте (вопрос и ответ свёрстаны
// одинаково), стартует с параметров «формирую» и переходит в исход —
// импульс готовности, отказ или ошибку. Через 1,4 с сворачивается, и
// остаётся обычная строка разбора с мелким SVG-знаком. Ответ при этом
// не задерживается ни на кадр.
function AiSettleOrb({ outcome, onSettled }) {
  const [shown, setShown] = useState(true);
  const reduced = useReducedMotion();
  // Колбэк — через ref: родитель перерисовывается при каждом нажатии
  // клавиши в поле, и таймер иначе сбрасывался бы, не дойдя до конца.
  const settled = useRef(onSettled);
  settled.current = onSettled;
  useEffect(() => {
    const timer = setTimeout(() => { setShown(false); settled.current?.(); }, reduced ? 900 : 1400);
    return () => clearTimeout(timer);
  }, [reduced]);
  return (
    <AnimatePresence initial={false}>
      {shown && (
        <motion.div
          className="ai-think-orb settle"
          style={{ overflow: "hidden" }}
          exit={reduced ? { opacity: 0 } : { height: 0, opacity: 0, marginBottom: 0 }}
          transition={reduced ? { duration: 0.15 } : { duration: 0.45, ease: AI_EASE }}
        >
          <AiOrb
            size="var(--ai-orb-pending, 120px)"
            initialState="forming"
            appear={false}
            state={orbStateFromPipeline({ outcome })}
            interactive={false}
            fallback={null}
          />
        </motion.div>
      )}
    </AnimatePresence>
  );
}

function AiMessage({ item, maySeeSql, copied, fresh, onRate, onRepeat, onCopy, onSettled }) {
  const answer = item.answer || {};
  const reduced = useReducedMotion();
  return (
    <motion.article
      className="ai-turn"
      // Анимируется только то, что появилось при этом заходе: иначе при
      // открытии старого диалога вся переписка въезжала бы заново.
      initial={fresh && !reduced ? { opacity: 0, y: 8 } : false}
      animate={{ opacity: 1, y: 0 }}
      transition={{ duration: 0.22, ease: AI_EASE }}
    >
      <div className="ai-ask-row">
        <p className="ai-question">{item.question}</p>
      </div>
      <div className="ai-reply">
        {fresh && <AiSettleOrb outcome={aiOutcome(answer)} onSettled={onSettled} />}
        <AiThinking answer={answer} />
        <AiAnswerBody answer={answer} maySeeSql={maySeeSql} />
        <div className="ai-actions">
          <button type="button" className="ai-action rate" onClick={() => onRate(item)}>
            <Star size={15} /> {item.rating ? "Изменить оценку" : "Оценить ответ"}
          </button>
          <span className="ai-rated">
            {item.rating ? `Ваша оценка: ${item.rating} из 5` : "Ответ ещё не оценён"}
          </span>
          <span className="ai-actions-gap" />
          <button type="button" className="ai-action" onClick={() => onCopy(item)}>
            {copied ? <Check size={15} /> : <Copy size={15} />} {copied ? "Скопировано" : "Копировать"}
          </button>
          <button type="button" className="ai-action" onClick={() => onRepeat(item.question)}>
            <RotateCcw size={15} /> Повторить
          </button>
        </div>
      </div>
    </motion.article>
  );
}

// --- рабочая область -------------------------------------------------------

function aiAnswerText(item) {
  const answer = item.answer || {};
  const parts = [item.question];
  if (answer.analysis) parts.push(aiAnalysisText(answer));
  else if (answer.summary) parts.push(answer.summary);
  if ((answer.rows || []).length) {
    parts.push([(answer.columns || []).join("\t"), ...answer.rows.map((row) => row.map(aiFormatCell).join("\t"))].join("\n"));
  }
  if (!answer.ok && answer.error) parts.push(answer.error);
  parts.push(`Источник: DWH ЛИКАРД · ${answer.scopeLabel || ""}`.trim());
  return parts.filter(Boolean).join("\n\n");
}

// Поток приходит кадрами «event: …\ndata: …\n\n». Читаем как текст и режем
// по пустой строке: EventSource здесь не годится, он умеет только GET, а
// вопрос в адресной строке — это вопрос в логах прокси.
async function aiStream(body, { onStage, signal }) {
  const response = await fetch("/api/ai/ask/stream", {
    method: "POST",
    credentials: "include",
    signal,
    headers: { "Content-Type": "application/json", Accept: "text/event-stream" },
    body: JSON.stringify(body),
  });
  if (response.status === 401) {
    emitAuthRequired();
    throw new Error("Сессия истекла — войдите заново");
  }
  if (!response.ok) {
    const detail = await response.json().catch(() => ({}));
    const error = new Error(detail.detail || `Ошибка ${response.status}`);
    error.status = response.status;
    throw error;
  }
  if (!response.body?.getReader) {
    const error = new Error("Поток не поддерживается");
    error.noStream = true;
    throw error;
  }

  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  let answer = null;
  let failure = null;

  for (;;) {
    const { value, done } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    let cut = buffer.indexOf("\n\n");
    while (cut >= 0) {
      const frame = buffer.slice(0, cut);
      buffer = buffer.slice(cut + 2);
      cut = buffer.indexOf("\n\n");
      let name = "message";
      const chunks = [];
      for (const line of frame.split("\n")) {
        if (line.startsWith("event:")) name = line.slice(6).trim();
        else if (line.startsWith("data:")) chunks.push(line.slice(5).trim());
      }
      if (!chunks.length) continue;
      let data;
      try {
        data = JSON.parse(chunks.join("\n"));
      } catch {
        continue;
      }
      if (name === "stage") onStage?.(data);
      else if (name === "answer") answer = data;
      else if (name === "failed") failure = data;
    }
  }

  if (failure) throw new Error(failure.detail || "Не удалось получить ответ");
  if (!answer) {
    const error = new Error("Поток оборвался до ответа");
    error.noStream = true;
    throw error;
  }
  return answer;
}

// Насколько человек растянул раздел вниз. Хранится у него в браузере: это
// его привычка, а не общая настройка продукта, и на сервере ей делать нечего.
const AI_HEIGHT_KEY = "ai.shellExtra";
const AI_EXTRA_MAX = 520;

function aiReadExtra() {
  try {
    const raw = Number(window.localStorage.getItem(AI_HEIGHT_KEY));
    return Number.isFinite(raw) ? Math.min(Math.max(raw, 0), AI_EXTRA_MAX) : 0;
  } catch {
    return 0;
  }
}

function aiWriteExtra(value) {
  try {
    window.localStorage.setItem(AI_HEIGHT_KEY, String(Math.round(value)));
  } catch {
    // приватный режим или запрет на хранение — молча живём без запоминания
  }
}

// Нижняя кромка раздела. Тянется мышью и стрелками: клавиатура здесь не
// вежливость, а единственный способ для тех, кто не пользуется мышью.
function AiResizeHandle({ extra, onChange }) {
  const [dragging, setDragging] = useState(false);
  const from = useRef({ y: 0, extra: 0 });

  function start(event) {
    event.preventDefault();
    from.current = { y: event.clientY, extra };
    setDragging(true);
    event.currentTarget.setPointerCapture?.(event.pointerId);
  }

  function move(event) {
    if (!dragging) return;
    const next = from.current.extra + (event.clientY - from.current.y);
    onChange(Math.min(Math.max(next, 0), AI_EXTRA_MAX));
  }

  function stop(event) {
    if (!dragging) return;
    setDragging(false);
    event.currentTarget.releasePointerCapture?.(event.pointerId);
  }

  return (
    <div
      className={dragging ? "ai-resize dragging" : "ai-resize"}
      role="separator"
      aria-orientation="horizontal"
      aria-label="Высота раздела"
      aria-valuenow={Math.round(extra)}
      aria-valuemin={0}
      aria-valuemax={AI_EXTRA_MAX}
      tabIndex={0}
      onPointerDown={start}
      onPointerMove={move}
      onPointerUp={stop}
      onPointerCancel={stop}
      onDoubleClick={() => onChange(0)}
      onKeyDown={(event) => {
        const step = event.shiftKey ? 80 : 24;
        if (event.key === "ArrowDown") { event.preventDefault(); onChange(Math.min(extra + step, AI_EXTRA_MAX)); }
        if (event.key === "ArrowUp") { event.preventDefault(); onChange(Math.max(extra - step, 0)); }
        if (event.key === "Home") { event.preventDefault(); onChange(0); }
      }}
      title="Потяните, чтобы сделать раздел выше. Двойной щелчок — вернуть как было"
    />
  );
}

async function aiSend(path, options = {}) {
  const response = await fetch(path, {
    credentials: "include",
    headers: { "Content-Type": "application/json", Accept: "application/json" },
    ...options,
  });
  if (response.status === 401) {
    emitAuthRequired();
    throw new Error("Сессия истекла — войдите заново");
  }
  if (!response.ok) {
    const detail = await response.json().catch(() => ({}));
    throw new Error(detail.detail || `Ошибка ${response.status}`);
  }
  if (response.status === 204) return null;
  return response.json();
}

function AnalyticsAiConsole({ status, drawer = false, onDrawer = () => {} }) {
  const identities = status?.identities || [];
  const [identityKey, setIdentityKey] = useState("");
  const [model, setModel] = useState(status?.model || "");
  // Глубина анализа: auto — выбирает разбор задачи; остальное — принудительно.
  const [depth, setDepth] = useState("auto");
  const depthOptions = (status?.depths || []).length ? status.depths : AI_DEPTH_FALLBACK;
  const [showSettings, setShowSettings] = useState(false);

  const [dialogs, setDialogs] = useState([]);
  const [dialogsLoading, setDialogsLoading] = useState(true);
  const [activeId, setActiveId] = useState(null);
  const [items, setItems] = useState([]);
  const [question, setQuestion] = useState("");
  const [pending, setPending] = useState("");
  const [liveStages, setLiveStages] = useState([]);
  // Орб пустого экрана слушает, пока курсор в поле или набран текст.
  const [composing, setComposing] = useState(false);
  const [shellExtra, setShellExtra] = useState(aiReadExtra);
  const [error, setError] = useState("");
  const [ratingFor, setRatingFor] = useState(null);
  const [copied, setCopied] = useState(0);

  const reducedMotion = useReducedMotion();
  const feedEnd = useRef(null);
  const composer = useRef(null);
  // Идентификаторы ответов, пришедших в этом сеансе: только они въезжают
  // при появлении, загруженная история показывается сразу на своих местах.
  // Сбрасывать при смене диалога не нужно — номера сообщений сквозные.
  const freshIds = useRef(new Set());
  const maySeeSql = Boolean(status?.maySeeSql);
  const requiredUpTo = status?.commentRequiredUpTo ?? 3;
  const identity = identities.find((item) => aiIdentityKey(item) === identityKey) || null;
  const activeDialog = dialogs.find((dialog) => dialog.id === activeId) || null;

  useEffect(() => {
    if (model) return;
    const installed = status?.installedModels || [];
    if (!installed.length) return;
    setModel(installed.includes(status.model) ? status.model : installed[0]);
  }, [status, model]);

  const loadDialogs = useCallback(async () => {
    try {
      const data = await aiSend("/api/ai/dialogs");
      setDialogs(data?.dialogs || []);
    } catch (err) {
      setError(err.message || "Не удалось загрузить историю");
    } finally {
      setDialogsLoading(false);
    }
  }, []);

  useEffect(() => { loadDialogs(); }, [loadDialogs]);

  useEffect(() => {
    if (!activeId) { setItems([]); return; }
    let alive = true;
    aiSend(`/api/ai/dialogs/${activeId}/messages`)
      .then((data) => { if (alive) setItems(data?.messages || []); })
      .catch((err) => { if (alive) setError(err.message || "Диалог не открылся"); });
    return () => { alive = false; };
  }, [activeId]);

  useEffect(() => {
    if (!items.length && !pending) return;
    feedEnd.current?.scrollIntoView({
      block: "end",
      behavior: reducedMotion ? "auto" : "smooth",
    });
  }, [items.length, pending, reducedMotion]);

  // Этап приходит дважды: «начал» и «закончил». Второй заменяет первый,
  // иначе список рос бы вдвое и показывал одно и то же по два раза.
  function pushStage(event) {
    setLiveStages((list) => {
      const index = list.findIndex((stage) => stage.key === event.key);
      if (index < 0) return [...list, event];
      const next = [...list];
      next[index] = { ...next[index], ...event };
      return next;
    });
  }

  async function ask(text) {
    const value = (text ?? question).trim();
    if (!value || pending) return;
    setPending(value);
    setLiveStages([]);
    setQuestion("");
    setError("");
    const body = {
      question: value,
      role: identity?.role || undefined,
      binding: identity?.binding || undefined,
      model: model || undefined,
      depth,
      dialogId: activeId ?? undefined,
    };
    try {
      let data;
      try {
        data = await aiStream(body, { onStage: pushStage });
      } catch (streamError) {
        // Поток мог не подняться: старый браузер, прокси без потоковой
        // передачи, обрыв до ответа. Обычный запрос всё ещё работает —
        // человек просто не увидит этапы по ходу дела.
        if (!streamError.noStream) throw streamError;
        setLiveStages([]);
        data = await aiSend("/api/ai/ask", { method: "POST", body: JSON.stringify(body) });
      }
      if (data.messageId) freshIds.current.add(data.messageId);
      setItems((list) => [...list, {
        id: data.messageId,
        createdAt: Math.floor(Date.now() / 1000),
        question: data.question || value,
        answer: data,
        rating: null,
        comment: "",
      }]);
      if (data.dialogId && data.dialogId !== activeId) setActiveId(data.dialogId);
      loadDialogs();
    } catch (err) {
      setError(err.message || "Не удалось получить ответ");
      setQuestion(value);
    } finally {
      setPending("");
      setLiveStages([]);
    }
  }

  async function newDialog() {
    setActiveId(null);
    setItems([]);
    setError("");
    onDrawer(false);
  }

  async function renameDialog(id, title) {
    const clean = (title || "").trim();
    if (!clean || clean === dialogs.find((d) => d.id === id)?.title) return;
    try {
      await aiSend(`/api/ai/dialogs/${id}`, { method: "PATCH", body: JSON.stringify({ title: clean }) });
      setDialogs((list) => list.map((d) => (d.id === id ? { ...d, title: clean } : d)));
    } catch (err) {
      setError(err.message || "Не удалось переименовать диалог");
    }
  }

  async function pinDialog(id, pinned) {
    try {
      await aiSend(`/api/ai/dialogs/${id}/pin`, { method: "POST", body: JSON.stringify({ pinned }) });
      setDialogs((list) => list.map((d) => (d.id === id ? { ...d, pinned: pinned ? 1 : 0 } : d)));
    } catch (err) {
      setError(err.message || "Не удалось закрепить диалог");
    }
  }

  async function removeDialog(id) {
    try {
      await aiSend(`/api/ai/dialogs/${id}`, { method: "DELETE" });
      setDialogs((list) => list.filter((d) => d.id !== id));
      if (id === activeId) { setActiveId(null); setItems([]); }
    } catch (err) {
      setError(err.message || "Не удалось удалить диалог");
    }
  }

  async function saveRating(item, rating, comment) {
    await aiSend("/api/ai/feedback", {
      method: "POST",
      body: JSON.stringify({ messageId: item.id, rating, comment }),
    });
    setItems((list) => list.map((row) => (row.id === item.id ? { ...row, rating, comment } : row)));
  }

  function copyAnswer(item) {
    const text = aiAnswerText(item);
    if (navigator.clipboard?.writeText) {
      navigator.clipboard.writeText(text).then(() => setCopied(item.id)).catch(() => setCopied(0));
      window.setTimeout(() => setCopied(0), 2000);
    }
  }

  const whoLabel = status?.ownScopeLabel
    ? `${status.ownRoleTitle || ""} · ${status.ownScopeLabel}`.replace(/^ · /, "")
    : status?.ownRoleTitle || "";

  const sidebar = (
    <AiSidebar
      dialogs={dialogs}
      activeId={activeId}
      loading={dialogsLoading}
      onNew={newDialog}
      onOpen={(id) => { setActiveId(id); onDrawer(false); }}
      onRename={renameDialog}
      onPin={pinDialog}
      onDelete={removeDialog}
      whoLabel={whoLabel}
      whoName={status?.ownName || status?.ownEmail || ""}
      onClose={drawer ? () => onDrawer(false) : undefined}
    />
  );

  // Подсказка не отправляет вопрос, а кладёт его в поле: почти всегда его
  // хочется поправить — уточнить период или объекты.
  function suggest(text) {
    setQuestion(text);
    const field = composer.current;
    if (!field) return;
    field.focus();
    field.setSelectionRange(text.length, text.length);
  }

  function changeShellExtra(value) {
    setShellExtra(value);
    aiWriteExtra(value);
  }

  return (
    <div className="ai-shell" style={{ "--ai-extra": `${Math.round(shellExtra)}px` }}>
      <div className="ai-shell-side">{sidebar}</div>
      {drawer && (
        <div className="ai-drawer" role="presentation" onClick={() => onDrawer(false)}>
          <div className="ai-drawer-panel" onClick={(event) => event.stopPropagation()}>{sidebar}</div>
        </div>
      )}

      <div className="ai-main">
        <header className="ai-main-head">
          <h2>{activeDialog ? activeDialog.title : "Новый диалог"}</h2>
          {status?.backend && (
            <span className="ai-chip">
              {status.backend === "postgres" ? "Витрина ОХД" : "Демонстрационный стенд"}
            </span>
          )}
          <span className="ai-head-gap" />
          <button
            type="button"
            className={showSettings ? "ai-head-button on" : "ai-head-button"}
            onClick={() => setShowSettings((value) => !value)}
            aria-expanded={showSettings}
          >
            <SlidersHorizontal size={15} /> Настройки
          </button>
        </header>

        {showSettings && (
          <div className="ai-settings">
            {status?.mayImpersonate ? (
              <label className="ui-field">
                <span>Спросить от имени</span>
                <select className="ui-select" value={identityKey} onChange={(event) => setIdentityKey(event.target.value)}>
                  <option value="">{`Себя · ${status.ownRoleTitle || "администратор"}`}</option>
                  {identities.map((item) => (
                    <option key={aiIdentityKey(item)} value={aiIdentityKey(item)}>
                      {item.binding ? `${item.roleTitle} — ${item.binding} (${item.stations})` : item.roleTitle}
                    </option>
                  ))}
                </select>
              </label>
            ) : (
              <div className="ui-field">
                <span>Вы спрашиваете как</span>
                <div className="ai-own-role">
                  <strong>{status?.ownRoleTitle}</strong>
                  {status?.ownScopeLabel ? ` · ${status.ownScopeLabel}` : ""}
                </div>
              </div>
            )}
            <label className="ui-field ai-field-depth">
              <span>Глубина анализа</span>
              <select className="ui-select" value={depth} onChange={(event) => setDepth(event.target.value)}>
                {depthOptions.map((item) => <option key={item.code} value={item.code}>{item.title}</option>)}
              </select>
              <span className="ai-depth-hint">{(depthOptions.find((item) => item.code === depth) || {}).hint || ""}</span>
            </label>
            <label className="ui-field ai-field-model">
              <span>Модель</span>
              {(status?.installedModels || []).length > 0 ? (
                <select className="ui-select" value={model} onChange={(event) => setModel(event.target.value)}>
                  {status.installedModels.map((name) => <option key={name} value={name}>{name}</option>)}
                </select>
              ) : (
                <div className="ai-model">
                  <span className="ai-dot off" />
                  Ollama не отвечает — запустите `ollama serve`
                </div>
              )}
            </label>
            <p className="ai-settings-note">
              Вопрос переводит в SQL модель, запрос проверяет детерминированный валидатор, читает витрину исполнитель
              только на чтение. Модель — недоверенный генератор: решение о допустимости принимает проверка.
            </p>
          </div>
        )}

        <div className="ai-feed">
          {items.length === 0 && !pending ? (
            <div className="ai-welcome">
              <AiOrb
                className="ai-welcome-orb"
                size="var(--ai-orb-size, 240px)"
                state={composing || question.trim() ? "listening" : "idle"}
                onTap={() => composer.current?.focus()}
                fallback={<AiMark state="idle" />}
              />
              <div className="ai-welcome-text">
                <p className="ai-welcome-title">Спросите о ваших объектах</p>
                <p className="ai-welcome-sub">
                  Вопрос на русском языке превращается в запрос к витрине. Запрос проверяется перед выполнением,
                  и ответ считается только по вашей области данных.
                </p>
              </div>
              <div className="ai-welcome-examples">
                {/* Три подсказки: четвёртая не помещалась в отведённую высоту
                    и вылезала за нижний контур. */}
                {AI_EXAMPLES.slice(0, 3).map((example) => (
                  <button
                    type="button"
                    key={example.ask}
                    title={example.ask}
                    onClick={() => suggest(example.ask)}
                  >
                    {example.hint}
                  </button>
                ))}
              </div>
            </div>
          ) : (
            <div className="ai-turns">
              {items.map((item) => (
                <AiMessage
                  key={item.id}
                  item={item}
                  maySeeSql={maySeeSql}
                  copied={copied === item.id}
                  fresh={freshIds.current.has(item.id)}
                  onSettled={() => freshIds.current.delete(item.id)}
                  onRate={setRatingFor}
                  onRepeat={(text) => ask(text)}
                  onCopy={copyAnswer}
                />
              ))}
              {pending && (
                <motion.article
                  className="ai-turn"
                  initial={reducedMotion ? false : { opacity: 0, y: 8 }}
                  animate={{ opacity: 1, y: 0 }}
                  transition={{ duration: 0.22, ease: AI_EASE }}
                >
                  <div className="ai-ask-row"><p className="ai-question">{pending}</p></div>
                  <div className="ai-reply"><AiThinking pending stages={liveStages} /></div>
                </motion.article>
              )}
              <div ref={feedEnd} />
            </div>
          )}
        </div>

        {error && (
          <div className="ai-error" role="alert">
            <AlertTriangle size={15} /> {error}
          </div>
        )}

        <div className="ai-composer-wrap">
          {items.length > 0 && !pending && (
            <div className="ai-chips">
              {AI_EXAMPLES.slice(0, 3).map((example) => (
                <button
                  type="button"
                  key={example.ask}
                  title={example.ask}
                  onClick={() => suggest(example.ask)}
                >
                  {example.hint}
                </button>
              ))}
            </div>
          )}
          <div className="ai-composer">
            <label>
              <span className="visually-hidden">Вопрос к витрине данных</span>
              <textarea
                ref={composer}
                rows={2}
                value={question}
                placeholder="Спросите о показателях ваших объектов"
                onChange={(event) => setQuestion(event.target.value)}
                onFocus={() => setComposing(true)}
                onBlur={() => setComposing(false)}
                onKeyDown={(event) => {
                  if (event.key === "Enter" && !event.shiftKey) {
                    event.preventDefault();
                    ask();
                  }
                }}
              />
            </label>
            <span className="ai-composer-hint">Enter — отправить, Shift+Enter — перенос строки</span>
            <button
              type="button"
              className="ai-send"
              onClick={() => ask()}
              disabled={Boolean(pending) || !question.trim()}
              aria-label="Отправить вопрос"
            >
              <Send size={17} />
            </button>
          </div>
        </div>
      </div>

      <AiResizeHandle extra={shellExtra} onChange={changeShellExtra} />

      {ratingFor && (
        <AiRatingDialog
          item={ratingFor}
          requiredUpTo={requiredUpTo}
          onClose={() => setRatingFor(null)}
          onSave={(rating, comment) => saveRating(ratingFor, rating, comment)}
        />
      )}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Свод аналитики: настраиваемая панель показателей витрины.
// Состав плиток хранится в профиле, поэтому у каждой роли он свой, а период
// считается как в справках: неполный месяц сравнивается с тем же отрезком
// прошлого года, а не с полным месяцем — иначе падение выходит мнимым.
// ---------------------------------------------------------------------------
const SUMMARY_MONTHS = ["Январь", "Февраль", "Март", "Апрель", "Май", "Июнь",
  "Июль", "Август", "Сентябрь", "Октябрь", "Ноябрь", "Декабрь"];

function useSummaryCatalog() {
  const [catalog, setCatalog] = useState(undefined);
  useEffect(() => {
    let alive = true;
    fetchJson("/api/summary/catalog")
      .then((data) => {
        if (alive) setCatalog(data && Array.isArray(data.available) ? data : null);
      })
      .catch(() => {
        if (alive) setCatalog(null);
      });
    return () => {
      alive = false;
    };
  }, []);
  return catalog;
}

function summaryPeriodLabel(value) {
  const month = Number(value.slice(5, 7));
  return `${SUMMARY_MONTHS[month - 1] || value} ${value.slice(0, 4)}`;
}

// Витрина залита не сплошным периодом, поэтому список месяцев берём из неё.
// Пока он не пришёл — показываем календарный год назад от последней даты.
function summaryPeriodOptions(latestDate, known) {
  if (known?.length) return known.map((value) => [value, summaryPeriodLabel(value)]);
  const anchor = latestDate ? new Date(`${latestDate}T00:00:00`) : new Date();
  const base = Number.isNaN(anchor.getTime()) ? new Date() : anchor;
  const options = [];
  for (let back = 0; back < 13; back += 1) {
    const month = new Date(base.getFullYear(), base.getMonth() - back, 1);
    const value = `${month.getFullYear()}-${String(month.getMonth() + 1).padStart(2, "0")}`;
    options.push([value, summaryPeriodLabel(value)]);
  }
  return options;
}

// Крупные суммы в плитке режутся до «5,29 млрд»: точное значение остаётся
// в подсказке, а плитка читается с одного взгляда и на телефоне.
function summaryFormatValue(value, decimals) {
  if (value === null || value === undefined) return { text: "—", exact: "" };
  const exact = value.toLocaleString("ru-RU", {
    minimumFractionDigits: decimals,
    maximumFractionDigits: decimals,
  });
  const magnitude = Math.abs(value);
  if (decimals <= 1 && magnitude >= 1_000_000) {
    const [divider, suffix] = magnitude >= 1_000_000_000 ? [1_000_000_000, " млрд"] : [1_000_000, " млн"];
    const short = (value / divider).toLocaleString("ru-RU", {
      minimumFractionDigits: 2,
      maximumFractionDigits: 2,
    });
    return { text: short + suffix, exact };
  }
  return { text: exact, exact: "" };
}

function summaryDelta(tile) {
  if (tile.delta === null || tile.delta === undefined) return null;
  const sign = tile.delta > 0 ? "+" : tile.delta < 0 ? "−" : "";
  const digits = tile.deltaDecimals ?? 1;
  const size = Math.abs(tile.delta).toLocaleString("ru-RU", {
    minimumFractionDigits: digits,
    maximumFractionDigits: digits,
  });
  const text = `${sign}${size} ${tile.deltaUnit || "%"}`;
  if (tile.delta === 0) return { text, tone: "flat" };
  const better = tile.lowerIsBetter ? tile.delta < 0 : tile.delta > 0;
  return { text, tone: better ? "up" : "down" };
}

function SummaryTile({ tile }) {
  const value = summaryFormatValue(tile.value, tile.decimals);
  const previous = summaryFormatValue(tile.previous, tile.decimals);
  const delta = summaryDelta(tile);
  return (
    <article className="summary-tile">
      <p className="summary-tile-title" title={tile.hint || tile.title}>
        {tile.title}
        {tile.hint ? <span className="summary-tile-mark" aria-hidden="true">?</span> : null}
      </p>
      <p className="summary-tile-value" title={value.exact ? `${value.exact} ${tile.unit}` : ""}>
        <strong>{value.text}</strong>
        <span className="summary-tile-unit">{tile.unit}</span>
      </p>
      <p className="summary-tile-foot">
        {delta ? <span className={`summary-delta ${delta.tone}`}>{delta.text}</span> : <span className="summary-delta flat">нет сравнения</span>}
        <span className="summary-tile-prev">год назад {previous.text || "—"}</span>
      </p>
    </article>
  );
}

function SummaryEditor({ catalog, selected, busy, error, onSave, onReset, onCancel }) {
  const [draft, setDraft] = useState(selected);
  const maxTiles = catalog.maxTiles || 8;
  const minTiles = catalog.minTiles || 4;

  const groups = [];
  for (const tile of catalog.available) {
    const found = groups.find((item) => item[0] === tile.group);
    if (found) found[1].push(tile);
    else groups.push([tile.group, [tile]]);
  }

  function toggle(code) {
    setDraft((current) => {
      if (current.includes(code)) return current.filter((item) => item !== code);
      if (current.length >= maxTiles) return current;
      return [...current, code];
    });
  }

  const tooFew = draft.length < minTiles;
  return (
    <div className="summary-editor">
      <div className="summary-editor-head">
        <strong>Состав свода</strong>
        <span className={tooFew ? "summary-editor-count warn" : "summary-editor-count"}>
          Выбрано {draft.length} из {maxTiles} · минимум {minTiles}
        </span>
      </div>
      <p className="summary-editor-note">
        В списке только те показатели, которые витрина умеет посчитать. Остальные появятся,
        когда в витрине будут нужные столбцы.
      </p>
      {groups.map(([group, tiles]) => (
        <fieldset className="summary-group" key={group}>
          <legend>{group}</legend>
          <div className="summary-group-items">
            {tiles.map((tile) => {
              const checked = draft.includes(tile.code);
              const locked = !checked && draft.length >= maxTiles;
              return (
                <label
                  key={tile.code}
                  className={`summary-option${checked ? " checked" : ""}${locked ? " locked" : ""}`}
                  title={tile.hint || ""}
                >
                  <input
                    type="checkbox"
                    checked={checked}
                    disabled={locked || busy}
                    onChange={() => toggle(tile.code)}
                  />
                  <span className="summary-option-title">{tile.title}</span>
                  <span className="summary-option-unit">{tile.unit}</span>
                </label>
              );
            })}
          </div>
        </fieldset>
      ))}
      {error ? <p className="summary-editor-error">{error}</p> : null}
      <div className="summary-editor-actions">
        <button type="button" className="ui-button" disabled={tooFew || busy} onClick={() => onSave(draft)}>
          {busy ? "Сохраняю…" : "Сохранить"}
        </button>
        <button type="button" className="ui-button ghost" disabled={busy} onClick={onCancel}>
          Отмена
        </button>
        <button type="button" className="ui-link-button" disabled={busy} onClick={onReset}>
          Вернуть состав по умолчанию
        </button>
      </div>
    </div>
  );
}

function AnalyticsSummary({ catalog }) {
  const [period, setPeriod] = useState(() => currentMonthPeriod());
  const [state, setState] = useState({ status: "idle", data: null, error: "" });
  const [editing, setEditing] = useState(false);
  const [saving, setSaving] = useState(false);
  const [saveError, setSaveError] = useState("");
  const [revision, setRevision] = useState(0);
  const [knownPeriods, setKnownPeriods] = useState([]);
  // Витрина обычно отстаёт от календаря. Один раз подводим период к
  // последнему месяцу, который в ней есть, иначе свод открывается пустым.
  const periodAligned = useRef(false);

  useEffect(() => {
    const controller = new AbortController();
    setState((current) => ({ status: "loading", data: current.data, error: "" }));
    fetchJson(`/api/summary?period=${period}`, controller.signal)
      .then((data) => {
        if (!data) throw new Error("Свод недоступен");
        const latestPeriod = (data.latestDate || "").slice(0, 7);
        if (!periodAligned.current && latestPeriod && latestPeriod < period) {
          periodAligned.current = true;
          setPeriod(latestPeriod);
          return;
        }
        periodAligned.current = true;
        setState({ status: "ready", data, error: "" });
      })
      .catch((error) => {
        if (error.name === "AbortError") return;
        setState({ status: "error", data: null, error: error.message || "Не удалось получить свод" });
      });
    return () => controller.abort();
  }, [period, revision]);

  useEffect(() => {
    const controller = new AbortController();
    fetchJson("/api/summary/periods", controller.signal)
      .then((data) => {
        const list = Array.isArray(data?.periods) ? data.periods : [];
        setKnownPeriods(list);
        if (list.length && !list.includes(period)) setPeriod(list[0]);
      })
      .catch(() => {});
    return () => controller.abort();
    // Список месяцев витрины запрашивается один раз за открытие раздела.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  async function persist(url, body) {
    setSaving(true);
    setSaveError("");
    try {
      const response = await fetch(url, {
        method: "POST",
        credentials: "include",
        headers: { "Content-Type": "application/json", Accept: "application/json" },
        body: body ? JSON.stringify(body) : "{}",
      });
      if (!response.ok) {
        const detail = await response.json().catch(() => ({}));
        throw new Error(detail.detail || `Ошибка ${response.status}`);
      }
      setEditing(false);
      setRevision((value) => value + 1);
    } catch (error) {
      setSaveError(error.message || "Не удалось сохранить состав");
    } finally {
      setSaving(false);
    }
  }

  const data = state.data;
  const options = summaryPeriodOptions(data?.latestDate, knownPeriods);
  const emptyPeriod = Boolean(data?.tiles?.length) && data.tiles.every((tile) => tile.value === null || tile.value === undefined);
  const selected = data?.selected || catalog.selected || [];

  return (
    <div className="summary-pane">
      <div className="summary-head">
        <div className="summary-head-text">
          <p className="summary-period">{data?.periodLabel || "Период загружается…"}</p>
          <p className="summary-note">
            {data ? `Сравнение с ${data.comparedTo}` : "Сравнение год к году по тому же отрезку дней"}
            {data?.scopeLabel ? ` · ${data.scopeLabel}` : ""}
          </p>
        </div>
        <div className="summary-controls">
          <label className="ui-field summary-period-field">
            <span>Период</span>
            <select className="ui-select" value={period} onChange={(event) => setPeriod(event.target.value)}>
              {options.map(([value, label]) => (
                <option key={value} value={value}>{label}</option>
              ))}
              {options.some(([value]) => value === period) ? null : <option value={period}>{period}</option>}
            </select>
          </label>
          <button type="button" className="ui-button ghost summary-settings" onClick={() => setEditing((value) => !value)}>
            {editing ? "Свернуть настройку" : "Настроить состав"}
          </button>
        </div>
      </div>

      {editing ? (
        <SummaryEditor
          key={selected.join(",")}
          catalog={catalog}
          selected={selected}
          busy={saving}
          error={saveError}
          onSave={(tiles) => persist("/api/summary/tiles", { tiles })}
          onReset={() => persist("/api/summary/reset", null)}
          onCancel={() => { setSaveError(""); setEditing(false); }}
        />
      ) : null}

      {state.status === "error" ? (
        <p className="summary-message error">{state.error}</p>
      ) : data?.error ? (
        <p className="summary-message error">{data.error}</p>
      ) : null}

      {emptyPeriod ? (
        <p className="summary-message">
          За этот месяц в витрине нет данных. Выберите другой период — в списке те месяцы,
          которые витрина уже содержит.
        </p>
      ) : null}

      {data?.tiles?.length ? (
        <div className={state.status === "loading" ? "summary-grid loading" : "summary-grid"}>
          {data.tiles.map((tile) => <SummaryTile key={tile.code} tile={tile} />)}
        </div>
      ) : state.status === "loading" ? (
        <p className="summary-message">Считаю показатели по витрине…</p>
      ) : null}

      {data?.source ? (
        <p className="summary-source">
          Источник: витрина {data.source === "postgres" ? "ОХД" : "демонстрационного стенда"} ·
          значения считаются одним запросом с той же проверкой области данных, что и у ИИ-аналитика.
        </p>
      ) : null}
    </div>
  );
}

function AnalyticsLocalOverview({ stations, totalStations, onFilter, onOpenList }) {
  const total = stations.length;
  const active = stations.filter((station) => station.status === "Действующая").length;
  const quality = stations.filter((station) => station.qualityIssues.length > 0).length;
  const shop = stations.filter((station) => station.flags.hasShop).length;
  const cafe = stations.filter((station) => station.flags.hasCafe).length;
  const toilet = stations.filter((station) => station.flags.hasToilet).length;
  const landmark = stations.filter((station) => station.flags.landmark).length;
  const agency = stations.filter((station) => station.flags.agency).length;

  const statusTop = groupTop(stations, (station) => station.status, 7);
  const npoTop = groupTop(stations, (station) => station.npo, 5);
  const formatTop = groupTop(stations, (station) => station.formatLevel2 || station.format, 7);
  const regionTop = groupTop(stations, (station) => station.subject, 8);
  const locationTop = groupTop(stations, (station) => station.location, 4);

  return (
    <>
      <div className="analytics-kpis">
        <Kpi title="Действующие" value={active} share={pct(active, total)} tone="green" />
        <Kpi title="С магазином" value={shop} share={pct(shop, total)} />
        <Kpi title="С кафе" value={cafe} share={pct(cafe, total)} tone="red" />
        <Kpi title="С санузлом" value={toilet} share={pct(toilet, total)} />
        <Kpi title="Замечания" value={quality} share={pct(quality, total)} tone="amber" />
        <Kpi title="Знаковые" value={landmark} share={pct(landmark, total)} />
        <Kpi title="Агентская схема" value={agency} share={pct(agency, total)} />
      </div>

      <div className="analytics-grid">
        <ChartCard title="Статусы" items={statusTop} total={total} />
        <ChartCard title="НПО" items={npoTop} total={total} />
        <ChartCard title="Форматы" items={formatTop} total={total} />
        <ChartCard title="Регионы" items={regionTop} total={total} />
        <ChartCard title="Локация" items={locationTop} total={total} compact />
        <div className="analytics-card quality-list-card">
          <h3>Качество данных</h3>
          <button
            type="button"
            onClick={() => {
              onFilter("quality", "issues");
              onOpenList();
            }}
          >
            <AlertTriangle size={16} />
            <span>Есть замечания</span>
            <strong>{asInt(quality)}</strong>
          </button>
          <p>Объекты без координат учитываются в аналитике и реестре. На карте отображаются только АЗС с указанными координатами.</p>
        </div>
      </div>
    </>
  );
}

const groupByOptions = [
  ["territoryManager", "ТМ"],
  ["regionalManager", "РУ"],
  ["station", "АЗС"],
];

function AnalyticsStateMessage({ state, emptyText, errorText }) {
  if (state.status === "loading") {
    return (
      <div className="analytics-card analytics-message">
        <CircleDot size={18} />
        <span>Загружаем данные...</span>
      </div>
    );
  }

  if (state.status === "error") {
    return (
      <div className="analytics-card analytics-message warning">
        <AlertTriangle size={18} />
        <span>{errorText}</span>
      </div>
    );
  }

  if (state.status === "no-data") {
    return (
      <div className="analytics-card analytics-message">
        <CircleDot size={18} />
        <span>{emptyText}</span>
      </div>
    );
  }

  return null;
}

function AnalyticsSlices({ state, groupBy, setGroupBy }) {
  const rows = state.data?.rows || [];
  const maxRevenue = Math.max(...rows.map((row) => metricById(row.metrics, "revenue")?.value || 0), 1);

  return (
    <div className="analytics-api-block">
      <div className="analytics-toolbar">
        <div className="segmented">
          {groupByOptions.map(([id, label]) => (
            <button className={groupBy === id ? "active" : ""} type="button" key={id} onClick={() => setGroupBy(id)}>
              {label}
            </button>
          ))}
        </div>
      </div>

      <AnalyticsStateMessage
        state={state}
        emptyText="По выбранному разрезу пока нет данных."
        errorText="Аналитика временно недоступна"
      />

      {state.status === "ready" && (
        <div className="analytics-card analytics-table-card">
          <div className="analytics-table">
            <div className="analytics-table-head">
              <span>{groupByOptions.find(([id]) => id === groupBy)?.[1]}</span>
              <span>Выручка</span>
              <span>Топливо</span>
              <span>Чеки</span>
              <span>MoM / YoY</span>
            </div>
            {rows.slice(0, 18).map((row) => {
              const revenue = metricById(row.metrics, "revenue");
              return (
                <article className="analytics-table-row" key={row.id}>
                  <div>
                    <strong>{analyticsGroupLabel(row.label, groupBy)}</strong>
                    <small>{asInt(row.count)} объект.</small>
                    <i style={{ width: `${Math.max(5, ((revenue?.value || 0) / maxRevenue) * 100)}%` }} />
                  </div>
                  <span>{metricDisplay(row.metrics, "revenue")}</span>
                  <span>{metricDisplay(row.metrics, "fuelVolume")}</span>
                  <span>{metricDisplay(row.metrics, "checks")}</span>
                  <span className="delta-pair">
                    <em className={deltaTone(revenue?.momPct)}>MoM {formatDelta(revenue?.momPct)}</em>
                    <em className={deltaTone(revenue?.yoyPct)}>YoY {formatDelta(revenue?.yoyPct)}</em>
                  </span>
                </article>
              );
            })}
          </div>
        </div>
      )}
    </div>
  );
}

const outageGroupByOptions = [
  ["region", "Регион"],
  ["regionalManager", "РУ"],
  ["territoryManager", "Территория"],
];

function AnalyticsFuelOutages({ state, groupBy, setGroupBy }) {
  const reduceMotion = useReducedMotion();
  const [expandedRows, setExpandedRows] = useState(false);
  const rows = state.data?.rows || [];
  const visibleRows = expandedRows ? rows : rows.slice(0, 30);
  const totals = state.data?.totals || {};
  const maxHours = Math.max(...rows.map((row) => Number(row.totalHours) || 0), 1);
  const reportTimestamp = formatFuelStockTimestamp(state.data?.sourceReceivedAt || state.data?.importedAt);
  const summary = [
    { label: "АЗС с простоями", value: asInt(totals.stationCount || 0), helper: formatFuelTypeCount(totals.productCount || 0) },
    { label: "События", value: asInt(totals.eventCount || 0), helper: "в последнем отчете" },
    { label: "Не завершены", value: asInt(totals.ongoingCount || 0), helper: "включая 23:59", tone: "amber" },
    { label: "Суммарный простой", value: formatOutageDuration(totals.totalHours || 0), helper: "по всем объектам" },
    { label: "Ожид. реализация", value: formatOutageSales(totals.expectedSalesLiters || 0), helper: "за время простоя", tone: "red" },
  ];

  useEffect(() => {
    setExpandedRows(false);
  }, [groupBy]);

  return (
    <div className="analytics-api-block outage-analytics">
      <div className="analytics-toolbar outage-analytics-toolbar">
        <div className="segmented" role="group" aria-label="Группировка простоев">
          {outageGroupByOptions.map(([id, label]) => (
            <button className={groupBy === id ? "active" : ""} type="button" key={id} onClick={() => setGroupBy(id)}>
              {label}
            </button>
          ))}
        </div>
        {reportTimestamp && <span className="source-pill">Отчет {reportTimestamp}</span>}
      </div>

      <AnalyticsStateMessage
        state={state}
        emptyText={state.data?.importedAt ? "В последнем отчете простои не зафиксированы." : "Отчет о простоях еще не загружен."}
        errorText="Аналитика простоев временно недоступна"
      />

      {state.status === "ready" && (
        <>
          <div className="outage-analytics-kpis" aria-label="Итоги по простоям">
            {summary.map((item) => (
              <div className={`outage-analytics-kpi ${item.tone || ""}`} key={item.label}>
                <span>{item.label}</span>
                <strong>{item.value}</strong>
                <small>{item.helper}</small>
              </div>
            ))}
          </div>

          <div className="analytics-card outage-ranking-card">
            <div className="outage-ranking-head">
              <div>
                <h3>Сводка по группам</h3>
                <p>Ранжирование по суммарной длительности простоев</p>
              </div>
              <span>{asInt(rows.length)} групп</span>
            </div>
            <ol className="outage-ranking-list">
              {visibleRows.map((row, index) => (
                <motion.li
                  key={row.id}
                  initial={reduceMotion ? false : { opacity: 0, y: 7 }}
                  animate={{ opacity: 1, y: 0 }}
                  transition={{ duration: reduceMotion ? 0 : 0.2, delay: reduceMotion ? 0 : Math.min(index * 0.025, 0.15) }}
                >
                  <div className="outage-ranking-label">
                    <span aria-hidden="true">{index + 1}</span>
                    <div>
                      <strong>{analyticsGroupLabel(row.label, groupBy)}</strong>
                      <small>{asInt(row.stationCount)} АЗС · {formatFuelTypeCount(row.productCount)}</small>
                    </div>
                  </div>
                  <div className="outage-ranking-bar" aria-hidden="true">
                    <motion.i
                      initial={reduceMotion ? false : { scaleX: 0 }}
                      animate={{ scaleX: Math.max(0.03, (Number(row.totalHours) || 0) / maxHours) }}
                      transition={{ duration: reduceMotion ? 0 : 0.34, delay: reduceMotion ? 0 : Math.min(index * 0.025, 0.15), ease: "easeOut" }}
                    />
                  </div>
                  <dl className="outage-ranking-metrics">
                    <div><dt>События</dt><dd>{asInt(row.eventCount)}</dd></div>
                    <div><dt>Не завершены</dt><dd className={row.ongoingCount ? "attention" : ""}>{asInt(row.ongoingCount)}</dd></div>
                    <div><dt>Простой</dt><dd>{formatOutageDuration(row.totalHours)}</dd></div>
                    <div><dt>Ожид. реализация</dt><dd>{formatOutageSales(row.expectedSalesLiters)}</dd></div>
                  </dl>
                </motion.li>
              ))}
            </ol>
            {rows.length > 30 && (
              <button className="outage-ranking-more" type="button" onClick={() => setExpandedRows((current) => !current)}>
                {expandedRows ? "Свернуть список" : `Показать еще ${rows.length - 30}`}
                <ChevronDown size={17} aria-hidden="true" />
              </button>
            )}
          </div>
        </>
      )}
    </div>
  );
}

function StationAutocomplete({ stations, excludedIds = [], selectedStation, placeholder, emptyHint, disabled = false, onPick }) {
  const [query, setQuery] = useState("");
  const [open, setOpen] = useState(false);
  const excluded = useMemo(() => new Set(excludedIds), [excludedIds]);
  const needle = query.trim().toLowerCase();
  const suggestions = useMemo(() => {
    if (!needle) return [];
    return stations
      .filter((station) => station.ksss && !excluded.has(station.ksss))
      .filter((station) => stationOptionText(station).includes(needle))
      .slice(0, 8);
  }, [stations, excluded, needle]);

  const showPanel = open && !disabled;

  function choose(station) {
    onPick(station);
    setQuery("");
    setOpen(false);
  }

  return (
    <div className={`station-autocomplete ${disabled ? "disabled" : ""}`}>
      <div className="station-autocomplete-field">
        <Search size={16} />
        <input
          value={open ? query : query || stationTitle(selectedStation)}
          onChange={(event) => {
            setQuery(event.target.value);
            setOpen(true);
          }}
          onFocus={() => {
            setQuery("");
            setOpen(true);
          }}
          onBlur={() => window.setTimeout(() => setOpen(false), 120)}
          placeholder={placeholder}
          disabled={disabled}
        />
      </div>
      {showPanel && (
        <div className="station-suggest-panel">
          {!needle && <div className="station-suggest-empty">{emptyHint || "Начните вводить КССС, номер или адрес"}</div>}
          {needle && suggestions.length === 0 && <div className="station-suggest-empty">Ничего не найдено</div>}
          {suggestions.map((station) => (
            <button type="button" key={station.ksss} onMouseDown={(event) => event.preventDefault()} onClick={() => choose(station)}>
              <strong>{station.ksss} · {station.name}</strong>
              <span>{station.subject || station.address || "Регион не заполнен"}</span>
            </button>
          ))}
        </div>
      )}
    </div>
  );
}

function AnalyticsSimilar({ stations, selected, geo, state, onSelectBase, onClearBase, onOpenStation, onCompare }) {
  return (
    <div className="analytics-api-block">
      <div className="analytics-context">
        <span>База подбора</span>
        <strong>{selected ? `${selected.name} · ${selected.ksss}` : "АЗС не выбрана"}</strong>
        {geo.message && <small className={`geo-inline ${geo.status}`}>{geo.message}</small>}
        <div className="similar-base-tools">
          <StationAutocomplete
            stations={stations}
            selectedStation={selected}
            placeholder="КССС, номер, адрес"
            emptyHint="Начните вводить АЗС для подбора аналогов"
            onPick={onSelectBase}
          />
          {selected && (
            <button className="clear-base-button" type="button" onClick={onClearBase}>
              Очистить
            </button>
          )}
        </div>
      </div>

      <AnalyticsStateMessage
        state={state}
        emptyText="Выберите АЗС, чтобы подобрать аналоги."
        errorText="Подбор аналогов временно недоступен"
      />

      {state.status === "ready" && (
        <div className="similar-list">
          {state.data.items.map((item) => (
            <article className="analytics-card similar-card" key={item.ksss} aria-labelledby={`similar-${item.ksss}`}>
              <div className="similar-score">
                <strong>{item.score}</strong>
                <span>score</span>
              </div>
              <div className="similar-main">
                <h3 id={`similar-${item.ksss}`}>{item.name}</h3>
                <p>{item.ksss} · {item.subject || "Регион не заполнен"}</p>
                <div className="similar-reasons">
                  {item.reasons.map((reason) => (
                    <span key={reason}>{reason}</span>
                  ))}
                </div>
                <div className="similar-metrics">
                  <span>{metricDisplay(item.metrics, "revenue")}</span>
                  <span>{metricDisplay(item.metrics, "fuelVolume")}</span>
                  <span>{metricDisplay(item.metrics, "checks")}</span>
                </div>
              </div>
              <div className="similar-actions">
                <button type="button" onClick={() => onOpenStation(item.ksss)}>Открыть</button>
                <button type="button" onClick={() => onCompare(item.ksss)}>Сравнить</button>
              </div>
            </article>
          ))}
        </div>
      )}
    </div>
  );
}

function AnalyticsCompare({ stations, state, compareIds, notice, onAdd, onRemove }) {
  const availableStations = stations.filter((station) => station.ksss && !compareIds.includes(station.ksss));
  const rows = [
    ["Регион", (item) => item.subject || "—"],
    ["РУ", (item) => formatPersonName(item.regionalManager)],
    ["ТМ", (item) => formatPersonName(item.territoryManager)],
    ["Формат", (item) => item.format || "—"],
    ["Локация", (item) => item.location || "—"],
    ["ТРК", (item) => (item.trkCount ? asInt(item.trkCount) : "—")],
    ["Посты", (item) => (item.postsCount ? asInt(item.postsCount) : "—")],
    ["Штат", (item) => `${asInt(item.staffTotal)} чел.`],
    ["Выручка", (item) => metricDisplay(item.metrics, "revenue")],
    ["Объем топлива", (item) => metricDisplay(item.metrics, "fuelVolume")],
    ["Чеки", (item) => metricDisplay(item.metrics, "checks")],
    ["Средний чек", (item) => metricDisplay(item.metrics, "avgCheck")],
  ];

  return (
    <div className="analytics-api-block">
      <div className="compare-picker">
        <StationAutocomplete
          stations={availableStations}
          excludedIds={compareIds}
          placeholder={compareIds.length >= 5 ? "Лимит 5 АЗС" : "Введите КССС или адрес"}
          emptyHint="Начните вводить, появятся подсказки АЗС"
          disabled={compareIds.length >= 5}
          onPick={(station) => onAdd(station.ksss)}
        />
        <span>{compareIds.length} / 5</span>
      </div>

      <div className="compare-chips">
        {compareIds.map((ksss) => (
          <button type="button" key={ksss} onClick={() => onRemove(ksss)}>
            {ksss}
            <X size={13} />
          </button>
        ))}
      </div>
      {notice && <p className="compare-notice">{notice}</p>}

      <AnalyticsStateMessage
        state={state}
        emptyText="Добавьте АЗС для сравнения."
        errorText="Сравнение временно недоступно"
      />

      {state.status === "ready" && (
        <div className="compare-scroll" aria-label="Сравнение АЗС">
          <table className="compare-table">
            <thead>
              <tr>
                <th scope="col">Показатель</th>
                {state.data.items.map((item) => (
                  <th key={item.ksss} scope="col">
                    <strong>{item.name}</strong>
                    <span>{item.ksss}</span>
                  </th>
                ))}
              </tr>
            </thead>
            <tbody>
              {rows.map(([label, getter]) => (
                <tr key={label}>
                  <th scope="row">{label}</th>
                  {state.data.items.map((item) => (
                    <td key={`${item.ksss}-${label}`}>{getter(item)}</td>
                  ))}
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}

function ControlDashboard({ stations, onOpenStation }) {
  const [feedback, setFeedback] = useState({ station: "", field: "", message: "" });
  const [feedbackSent, setFeedbackSent] = useState(false);
  const issueStations = stations.filter((station) => station.qualityIssues.length > 0);
  const issueCounts = groupTop(
    stations.flatMap((station) => station.qualityIssues).map((issue) => ({ issue })),
    (item) => item.issue,
    8,
  );

  function submitFeedback(event) {
    event.preventDefault();
    const text = feedback.message.trim();
    if (!text) return;
    const entry = {
      ...feedback,
      message: text,
      createdAt: new Date().toISOString(),
    };
    const current = JSON.parse(localStorage.getItem("azs:feedback") || "[]");
    localStorage.setItem("azs:feedback", JSON.stringify([entry, ...current].slice(0, 100)));
    setFeedback({ station: "", field: "", message: "" });
    setFeedbackSent(true);
    window.setTimeout(() => setFeedbackSent(false), 2600);
  }

  return (
    <section className="analytics-pane control-pane">
      <div className="analytics-head">
        <div>
          <h2>Контроль АЗС</h2>
          <p>
            Рабочий срез для выезда: карточка, контакты, координаты, сервисы и готовность данных по текущей выборке.
          </p>
        </div>
      </div>

      <div className="control-grid">
        <div className="analytics-card issue-card">
          <h3>Типы замечаний</h3>
          <div className="issue-stack">
            {issueCounts.length ? (
              issueCounts.map((item) => (
                <div className="issue-row" key={item.name}>
                  <span>{item.name}</span>
                  <strong>{asInt(item.value)}</strong>
                </div>
              ))
            ) : (
              <p>В текущей выборке замечаний нет.</p>
            )}
          </div>
        </div>

        <FeedbackCard
          feedback={feedback}
          sent={feedbackSent}
          onChange={setFeedback}
          onSubmit={submitFeedback}
        />
      </div>

      <div className="analytics-card issue-table-card">
        <h3>Объекты для проверки</h3>
        <div className="issue-table">
          {issueStations.slice(0, 28).map((station) => (
            <button type="button" key={station.id} onClick={() => onOpenStation(station.id)}>
              <span className="status-dot" style={{ background: statusColors[station.status] || "#8f99a8" }} aria-hidden="true" />
              <span>
                <strong>{station.name}</strong>
                <small>{station.ksss} · {station.subject || station.address || "Регион не заполнен"}</small>
              </span>
              <em>{station.qualityIssues[0]}</em>
            </button>
          ))}
          {!issueStations.length && <p>В текущей выборке нет объектов с замечаниями.</p>}
        </div>
      </div>
    </section>
  );
}

function FeedbackCard({ feedback, sent, onChange, onSubmit }) {
  const disabled = !feedback.message.trim();
  return (
    <form className="analytics-card feedback-card" onSubmit={onSubmit}>
      <div className="feedback-head">
        <MessageSquare size={18} />
        <div>
          <h3>Обратная связь по данным</h3>
          <p>Сообщите, если в карточке АЗС нашли неверную информацию.</p>
        </div>
      </div>
      <label htmlFor="feedback-station">
        <span>АЗС или КССС</span>
        <input
          id="feedback-station"
          value={feedback.station}
          onChange={(event) => onChange((current) => ({ ...current, station: event.target.value }))}
          placeholder="Например: 2707 или АЗС №02003"
        />
      </label>
      <label htmlFor="feedback-field">
        <span>Что исправить</span>
        <input
          id="feedback-field"
          value={feedback.field}
          onChange={(event) => onChange((current) => ({ ...current, field: event.target.value }))}
          placeholder="Телефон, адрес, персонал, сервисы..."
        />
      </label>
      <label htmlFor="feedback-message">
        <span>Комментарий</span>
        <textarea
          id="feedback-message"
          value={feedback.message}
          onChange={(event) => onChange((current) => ({ ...current, message: event.target.value }))}
          placeholder="Опишите, какая информация неправильная и что должно быть указано."
          rows={4}
          required
        />
      </label>
      <button type="submit" disabled={disabled}>
        {sent ? <CheckCircle2 size={16} /> : <Send size={16} />}
        {sent ? "Сохранено локально" : "Отправить замечание"}
      </button>
    </form>
  );
}

const Kpi = React.memo(function Kpi({ title, value, share, tone = "" }) {
  const displayValue = useCountUp(value);
  return (
    <div className={`analytics-kpi ${tone}`}>
      <span>{title}</span>
      <strong>{displayValue}</strong>
      <small>{share}% выборки</small>
    </div>
  );
});

const ChartCard = React.memo(function ChartCard({ title, items, total, compact = false }) {
  const reduceMotion = useReducedMotion();
  const maxValue = max(items, (item) => item.value) || 1;
  const widthScale = scaleLinear().domain([0, maxValue]).range([4, 100]).clamp(true);
  return (
    <div className={`analytics-card ${compact ? "compact" : ""}`}>
      <h3>{title}</h3>
      <div className="bar-list">
        {items.map((item, index) => (
          <div className="bar-row" key={item.name}>
            <div className="bar-label">
              <span>{item.name}</span>
              <strong>{asInt(item.value)}</strong>
            </div>
            <div className="bar-track">
              <motion.i
                initial={reduceMotion ? false : { width: "4%" }}
                animate={{ width: `${widthScale(item.value)}%` }}
                transition={{ duration: reduceMotion ? 0 : 0.42, delay: reduceMotion ? 0 : index * 0.035, ease: "easeOut" }}
              />
            </div>
            <small>{pct(item.value, total)}%</small>
          </div>
        ))}
      </div>
    </div>
  );
});

const Metric = React.memo(function Metric({ label, value, tone = "" }) {
  const displayValue = useCountUp(value);
  return (
    <div className={`metric ${tone}`}>
      <span>{label}</span>
      <strong>{displayValue}</strong>
    </div>
  );
});

function FilterRail({ filters, options, setFilter, fuelAvailability }) {
  const fuelOptions = fuelAvailability?.options?.fuels || [];
  return (
    <div className="filter-rail">
      <ToggleChip
        label="Избранное"
        Icon={Heart}
        active={Boolean(filters.favorites)}
        onChange={(value) => setFilter("favorites", value)}
      />
      <SelectChip label="НПО" value={filters.npo} options={options.npo} onChange={(value) => setFilter("npo", value)} />
      <SelectChip label="Регион" value={filters.subject} options={options.subject} onChange={(value) => setFilter("subject", value)} />
      <SelectChip label="Статус" value={filters.status} options={options.status} onChange={(value) => setFilter("status", value)} />
      <SelectChip label="Тип" value={filters.type} options={options.type} onChange={(value) => setFilter("type", value)} />
      <SelectChip label="Сервис" value={filters.service} options={[
        ["shop", "Магазин"],
        ["cafe", "Кафе"],
        ["toilet", "Санузел"],
        ["landmark", "Знаковый"],
      ]} onChange={(value) => setFilter("service", value)} />
      {fuelOptions.length > 0 && (
        <MultiSelectChip
          label="Топливо"
          values={filters.fuel}
          options={fuelOptions.map((option) => [option.canonicalFuel, fuelDisplayName(option.canonicalFuel)])}
          onChange={(values) => setFilter("fuel", values)}
        />
      )}
    </div>
  );
}

function FuelFilterNotice({ availability, onClear }) {
  if (!availability) return null;
  const { matches, options, failed, pending, ready } = availability;
  if (!availability.selection.length) return null;

  if (failed) {
    return (
      <div className="fuel-filter-notice warning" role="alert">
        <span>{matches.error}</span>
        <button type="button" onClick={onClear}>Снять фильтр</button>
      </div>
    );
  }
  if (pending) {
    return (
      <div className="fuel-filter-notice" role="status">
        <span>Подбираем АЗС…</span>
      </div>
    );
  }
  if (!ready) return null;

  const minPercent = numericOrNull(matches.meta?.minPercent) ?? numericOrNull(options.minPercent) ?? 5;
  return (
    <div className={`fuel-filter-notice ${matches.meta?.stale ? "warning" : ""}`} role="status">
      <span>
        {`Показаны АЗС, где запас выбранного топлива больше ${formatFuelPercent(minPercent)} от доступного к отпуску.`}
        {matches.meta?.stale ? " Снимок остатков давно не обновлялся." : ""}
      </span>
    </div>
  );
}

// Multi-value sibling of SelectChip: the registry fuel filter needs several fuels at once,
// which a native <select> cannot do well on mobile. The menu is rendered through a portal
// because .filter-rail scrolls horizontally and would clip an absolutely positioned popup.
const MULTI_CHIP_MENU_WIDTH = 200;

function MultiSelectChip({ label, values, options, onChange }) {
  const [open, setOpen] = useState(false);
  const [position, setPosition] = useState(null);
  const buttonRef = useRef(null);
  const menuRef = useRef(null);
  const selected = Array.isArray(values) ? values : [];
  const normalized = options.map((option) => (Array.isArray(option) ? option : [option, option]));

  useEffect(() => {
    if (!open) return undefined;

    function place() {
      const rect = buttonRef.current?.getBoundingClientRect();
      if (!rect) return;
      const maxLeft = Math.max(8, window.innerWidth - MULTI_CHIP_MENU_WIDTH - 8);
      setPosition({ top: rect.bottom + 6, left: Math.min(Math.max(8, rect.left), maxLeft) });
    }
    function handlePointer(event) {
      if (buttonRef.current?.contains(event.target)) return;
      if (menuRef.current?.contains(event.target)) return;
      setOpen(false);
    }
    function handleKey(event) {
      if (event.key === "Escape") setOpen(false);
    }

    place();
    window.addEventListener("scroll", place, true);
    window.addEventListener("resize", place);
    document.addEventListener("pointerdown", handlePointer);
    document.addEventListener("keydown", handleKey);
    return () => {
      window.removeEventListener("scroll", place, true);
      window.removeEventListener("resize", place);
      document.removeEventListener("pointerdown", handlePointer);
      document.removeEventListener("keydown", handleKey);
    };
  }, [open]);

  function toggle(id) {
    onChange(selected.includes(id) ? selected.filter((item) => item !== id) : [...selected, id]);
  }

  const caption = selected.length
    ? selected.map((id) => normalized.find(([optionId]) => optionId === id)?.[1] || id).join(", ")
    : label;

  return (
    <div className={`multi-chip ${selected.length ? "filled" : ""}`}>
      <button
        ref={buttonRef}
        type="button"
        className="multi-chip-button"
        aria-expanded={open}
        aria-label={selected.length ? `${label}: ${caption}` : label}
        onClick={() => setOpen((current) => !current)}
      >
        <span>{caption}</span>
        <ChevronDown size={14} />
      </button>
      {open && position && createPortal(
        <div
          ref={menuRef}
          className="multi-chip-menu"
          role="group"
          aria-label={label}
          style={{ top: position.top, left: position.left, width: MULTI_CHIP_MENU_WIDTH }}
        >
          {normalized.map(([id, title]) => (
            <label className="multi-chip-option" key={id}>
              <input type="checkbox" checked={selected.includes(id)} onChange={() => toggle(id)} />
              <span>{title}</span>
            </label>
          ))}
          {selected.length > 0 && (
            <button type="button" className="multi-chip-clear" onClick={() => onChange([])}>
              Сбросить
            </button>
          )}
        </div>,
        document.body,
      )}
    </div>
  );
}

function ToggleChip({ label, Icon, active, onChange }) {
  return (
    <button
      type="button"
      className={`toggle-chip ${active ? "on" : ""}`}
      aria-pressed={active}
      onClick={() => onChange(!active)}
    >
      {Icon ? <Icon size={14} fill={active ? "currentColor" : "none"} /> : null}
      <span>{label}</span>
    </button>
  );
}

function SelectChip({ label, value, options, onChange }) {
  const normalized = options.map((option) => (Array.isArray(option) ? option : [option, option]));
  return (
    <label className={`select-chip ${value ? "filled" : ""}`}>
      <span>{value ? normalized.find(([id]) => id === value)?.[1] || value : label}</span>
      <ChevronDown size={14} />
      <select value={value} onChange={(event) => onChange(event.target.value)} aria-label={label}>
        <option value="">{label}</option>
        {normalized.map(([id, title]) => (
          <option value={id} key={id}>
            {title}
          </option>
        ))}
      </select>
    </label>
  );
}

function StationList({ stations, selectedId, favorites, density = "comfortable", onSelect, onFavorite, onScroll, loading = false, favoritesOnly = false }) {
  const reduceMotion = useReducedMotion();

  if (!stations.length && loading) {
    const skeletonCount = density === "compact" ? 8 : 6;
    return (
      <div className={`station-list ${density === "compact" ? "compact" : ""}`} aria-busy="true" aria-label="Загружаем список АЗС">
        {Array.from({ length: skeletonCount }, (_, i) => (
          <div key={i} className="station-row skeleton" aria-hidden="true" />
        ))}
      </div>
    );
  }

  if (!stations.length) {
    if (favoritesOnly && !favorites.length) {
      return (
        <div className="empty">
          <Heart size={24} />
          <strong>В избранном пусто</strong>
          <span>Откройте АЗС и нажмите на сердечко — она появится в этом перечне.</span>
        </div>
      );
    }
    return (
      <div className="empty">
        <Filter size={24} />
        <strong>Нет объектов</strong>
        <span>
          {favoritesOnly
            ? "Среди избранных АЗС нет подходящих под остальные фильтры."
            : "Попробуйте изменить поиск или фильтры."}
        </span>
      </div>
    );
  }

  return (
    <div className={`station-list ${density === "compact" ? "compact" : ""}`} onScroll={(event) => onScroll?.(event.currentTarget.scrollTop)}>
      {stations.slice(0, 350).map((station, index) => (
        <motion.button
          className={`station-row ${selectedId === station.id ? "selected" : ""}`}
          key={station.id}
          type="button"
          onClick={() => onSelect(station.id)}
          initial={reduceMotion ? false : { opacity: 0, y: 8 }}
          animate={{ opacity: 1, y: 0 }}
          transition={{ duration: reduceMotion ? 0 : undefined, type: "spring", stiffness: 230, damping: 30, delay: reduceMotion ? 0 : Math.min(index, 12) * 0.014 }}
          whileTap={reduceMotion ? undefined : { scale: 0.99 }}
        >
          <span
            className={`status-dot tone-${statusTone(station.status)}`}
            style={{ "--status-color": statusColors[station.status] || "#8f99a8" }}
            aria-hidden="true"
          />
          <span className="row-main">
            <span className="row-title">
              {station.name || `АЗС № ${station.stationNumber}`}
              <small>{station.ksss}</small>
            </span>
            <span className="row-address">{station.address || station.subject}</span>
            <span className="badges">
              <Badge tone={`status ${statusTone(station.status)}`}>{shortStatus(station.status)}</Badge>
              {station.flags.hasShop && <Badge icon={<Store size={12} />}>Магазин</Badge>}
              {station.flags.hasCafe && <Badge icon={<Coffee size={12} />}>Кафе</Badge>}
              {station.flags.hasToilet && <Badge icon={<Toilet size={12} />}>Санузел</Badge>}
              {station.qualityIssues.length > 0 && <Badge tone="warn" icon={<AlertTriangle size={12} />}>{station.qualityIssues.length}</Badge>}
            </span>
          </span>
          <span
            className={`favorite-dot ${favorites.includes(station.id) ? "on" : ""}`}
            role="button"
            tabIndex={0}
            aria-label={favorites.includes(station.id) ? "Убрать из избранного" : "Добавить в избранное"}
            aria-pressed={favorites.includes(station.id)}
            onClick={(event) => {
              event.stopPropagation();
              onFavorite(station.id);
            }}
            onKeyDown={(event) => {
              if (event.key === "Enter" || event.key === " ") {
                event.stopPropagation();
                event.preventDefault();
                onFavorite(station.id);
              }
            }}
          >
            <Heart size={15} fill="currentColor" />
          </span>
        </motion.button>
      ))}
    </div>
  );
}

function Badge({ children, icon, tone = "" }) {
  return (
    <span className={`badge ${tone}`}>
      {icon}
      {children}
    </span>
  );
}

function StationMap({ stations, selected, focusSelected, onSelect, onCloseFullscreen }) {
  const reduceMotion = useReducedMotion();
  const mapNodeRef = useRef(null);
  const mapRef = useRef(null);
  const ymapsRef = useRef(null);
  const apiVersionRef = useRef(null);
  const objectManagerRef = useRef(null);
  const markerRefs = useRef([]);
  const userMarkerRef = useRef(null);
  const onSelectRef = useRef(onSelect);
  const headerDragStartRef = useRef(null);
  const headerDragTimeRef = useRef(0);
  const [mapStatus, setMapStatus] = useState(YANDEX_MAPS_API_KEY ? "idle" : "missing-key");
  const [mapError, setMapError] = useState("");
  const [geoStatus, setGeoStatus] = useState("idle");
  const [geoMessage, setGeoMessage] = useState("");
  const [userLocation, setUserLocation] = useState(null);

  const points = useMemo(() => stations.filter(hasValidPoint), [stations]);

  useEffect(() => {
    onSelectRef.current = onSelect;
  }, [onSelect]);

  useEffect(() => {
    let cancelled = false;

    if (!YANDEX_MAPS_API_KEY) {
      setMapStatus("missing-key");
      return undefined;
    }

    setMapStatus("loading");

    let loadPromise = loadYandexMaps(YANDEX_MAPS_API_KEY);

    // Быстрая диагностика ключа параллельно с загрузкой скрипта
    checkYandexApiKey(YANDEX_MAPS_API_KEY).then((keyError) => {
      if (keyError && !cancelled) {
        setMapError(keyError);
        setMapStatus("error");
        yandexMapsPromise = null; // сбрасываем кэш промиса чтобы не блокировать повтор
      }
    });

    loadPromise
      .then(({ version, api }) => {
        if (cancelled || !mapNodeRef.current) return;

        if (version === "v3") {
          const location = mapLocationForYmaps(points, selected, focusSelected);
          const map = new api.YMap(mapNodeRef.current, { location });

          map.addChild(new api.YMapDefaultSchemeLayer({}));
          map.addChild(new api.YMapDefaultFeaturesLayer({}));

          apiVersionRef.current = "v3";
          ymapsRef.current = api;
          mapRef.current = map;
          setMapStatus("ready");
          return;
        }

        const location = mapLocationForYmaps2(points, selected, focusSelected);
        const map = new api.Map(mapNodeRef.current, {
          center: location.center || [55.7558, 37.6176],
          zoom: location.zoom || 4,
          controls: ["zoomControl", "fullscreenControl"],
        });

        if (location.bounds) {
          map.setBounds(location.bounds, { checkZoomRange: true, zoomMargin: 32 });
        }

        const objectManager = new api.ObjectManager({
          clusterize: true,
          gridSize: 48,
          clusterDisableClickZoom: false,
        });

        objectManager.objects.events.add("click", (event) => {
          onSelectRef.current(event.get("objectId"));
        });

        map.geoObjects.add(objectManager);
        apiVersionRef.current = "v2";
        ymapsRef.current = api;
        mapRef.current = map;
        objectManagerRef.current = objectManager;
        setMapStatus("ready");
      })
      .catch((error) => {
        if (cancelled) return;
        setMapError(error.message);
        setMapStatus("error");
      });

    return () => {
      cancelled = true;
      markerRefs.current = [];
      userMarkerRef.current = null;
      objectManagerRef.current = null;
      apiVersionRef.current = null;
      if (mapRef.current) {
        mapRef.current.destroy();
        mapRef.current = null;
      }
    };
  }, []);

  useEffect(() => {
    if (!mapRef.current) return;
    if (apiVersionRef.current === "v3") {
      const location = mapLocationForYmaps(points, selected, focusSelected);
      mapRef.current.setLocation({ ...location, duration: 450 });
      return;
    }

    const location = mapLocationForYmaps2(points, selected, focusSelected);
    if (location.bounds && !focusSelected) {
      mapRef.current.setBounds(location.bounds, { checkZoomRange: true, duration: 450, zoomMargin: 32 });
    } else if (location.center) {
      mapRef.current.setCenter(location.center, location.zoom, { duration: 450 });
    }
  }, [points, selected, focusSelected, mapStatus]);

  useEffect(() => {
    if (!mapRef.current || !ymapsRef.current) return;

    if (apiVersionRef.current === "v3") {
      markerRefs.current.forEach((marker) => mapRef.current.removeChild(marker));
      markerRefs.current = points.map((station) => createStationMarker(ymapsRef.current, station, selected?.id, onSelectRef.current));
      markerRefs.current.forEach((marker) => mapRef.current.addChild(marker));
      return;
    }

    if (!objectManagerRef.current) return;
    objectManagerRef.current.removeAll();
    objectManagerRef.current.add({
      type: "FeatureCollection",
      features: points.map((station) => stationFeature(station, selected?.id)),
    });
  }, [points, selected?.id, mapStatus]);

  useEffect(() => {
    if (!mapRef.current || !ymapsRef.current || !userLocation) return;

    if (apiVersionRef.current === "v3") {
      const coords = [userLocation.lon, userLocation.lat];

      if (userMarkerRef.current) {
        mapRef.current.removeChild(userMarkerRef.current);
      }

      const userElement = document.createElement("div");
      userElement.className = "ymap-user-marker";
      userElement.title = `Вы здесь · ${formatLocationMessage(userLocation)}`;

      userMarkerRef.current = new ymapsRef.current.YMapMarker({ coordinates: coords, zIndex: 30 }, userElement);
      mapRef.current.addChild(userMarkerRef.current);
      mapRef.current.setLocation({ center: coords, zoom: 15, duration: 450 });
      return;
    }

    const coords = [userLocation.lat, userLocation.lon];
    const caption = formatLocationMessage(userLocation);

    if (!userMarkerRef.current) {
      userMarkerRef.current = new ymapsRef.current.Placemark(
        coords,
        {
          iconCaption: "Вы здесь",
          balloonContentHeader: "Ваше местоположение",
          balloonContentBody: caption,
        },
        {
          preset: "islands#blueCircleDotIcon",
          iconColor: "#3077d8",
        },
      );
      mapRef.current.geoObjects.add(userMarkerRef.current);
    } else {
      userMarkerRef.current.geometry.setCoordinates(coords);
      userMarkerRef.current.properties.set({
        iconCaption: "Вы здесь",
        balloonContentHeader: "Ваше местоположение",
        balloonContentBody: caption,
      });
    }

    if (userLocation.bounds) {
      mapRef.current.setBounds(userLocation.bounds, { checkZoomRange: true, duration: 450, zoomMargin: 48 });
    } else {
      mapRef.current.setCenter(coords, Math.max(mapRef.current.getZoom(), 15), { duration: 450 });
    }
  }, [userLocation, mapStatus]);

  async function locateUser() {
    if (!points.length) {
      setGeoStatus("error");
      setGeoMessage("В текущей выборке нет АЗС с координатами.");
      return;
    }

    function applyNearest(nextLocation) {
      const nearest = nearestStation(nextLocation, points);
      setUserLocation(nextLocation);
      if (nearest.station) {
        onSelectRef.current(nearest.station.id);
        setGeoStatus("found");
        setGeoMessage(`Ближайшая АЗС: ${nearest.station.name || nearest.station.stationNumber} · ${formatDistance(nearest.distance)}`);
      } else {
        setGeoStatus("error");
        setGeoMessage("Не удалось найти ближайшую АЗС в текущей выборке.");
      }
    }

    setGeoStatus("locating");
    setGeoMessage("Определяем местоположение через Яндекс...");

    try {
      const nextLocation = await getYandexLocation(ymapsRef.current, apiVersionRef.current);
      applyNearest(nextLocation);
      return;
    } catch (yandexError) {
      try {
        const nextLocation = await getBrowserLocation();
        applyNearest(nextLocation);
      } catch (browserError) {
        setGeoStatus("error");
        setGeoMessage(
          browserError.message === "BROWSER_GEOLOCATION_UNAVAILABLE"
            ? "Яндекс не смог определить местоположение, а браузерная геолокация работает только через HTTPS или localhost."
            : geolocationErrorMessage(browserError),
        );
      }
    }
  }

  function handleHeaderTouchStart(event) {
    if (event.target.closest("button")) return;
    headerDragStartRef.current = event.touches[0]?.clientY ?? 0;
    headerDragTimeRef.current = Date.now();
  }

  function handleHeaderTouchEnd(event) {
    if (headerDragStartRef.current == null || !onCloseFullscreen) return;
    const endY = event.changedTouches[0]?.clientY ?? headerDragStartRef.current;
    const delta = endY - headerDragStartRef.current;
    const elapsedMs = Math.max(Date.now() - headerDragTimeRef.current, 1);
    const velocity = delta / elapsedMs;
    headerDragStartRef.current = null;
    if (delta > 20 && (velocity > 0.6 || delta > 90)) {
      onCloseFullscreen();
    }
  }

  return (
    <motion.div
      className="map-surface"
      initial={reduceMotion ? false : { opacity: 0, y: 12, scale: 0.992 }}
      animate={{ opacity: 1, y: 0, scale: 1 }}
      transition={{ type: "spring", stiffness: 220, damping: 28, mass: 0.9 }}
    >
      <div
        className="map-header"
        onTouchStart={handleHeaderTouchStart}
        onTouchEnd={handleHeaderTouchEnd}
        onTouchCancel={handleHeaderTouchEnd}
      >
        <div>
          <strong>{asInt(points.length)} точек на карте</strong>
          <span>Все объекты с координатами</span>
        </div>
        <div className="map-header-actions">
          <button
            className={`map-locate-button ${geoStatus === "found" ? "active" : ""}`}
            type="button"
            onClick={locateUser}
            disabled={mapStatus !== "ready" || geoStatus === "locating"}
            aria-label="Показать мою геолокацию"
            title="Показать мою геолокацию"
          >
            <LocateFixed size={18} />
          </button>
          {onCloseFullscreen && (
            <button className="map-close-button" type="button" onClick={onCloseFullscreen} aria-label="Закрыть карту">
              <X size={18} />
            </button>
          )}
        </div>
      </div>
      <div className="map-canvas">
        <div className="yandex-map" ref={mapNodeRef} />
        {mapStatus === "ready" && <MapLegend />}
        {mapStatus === "ready" && geoMessage && (
          <div className={`geo-toast ${geoStatus}`}>
            <LocateFixed size={14} />
            <span>{geoMessage}</span>
          </div>
        )}
        {mapStatus !== "ready" && (
          <div className="map-state">
            {mapStatus === "missing-key" ? (
              <>
                <strong>Нужен ключ Yandex Maps API</strong>
                <span>Создай `azs-mobile/.env.local` и добавь `VITE_YANDEX_MAPS_API_KEY=...`.</span>
              </>
            ) : mapStatus === "error" ? (
              <>
                <strong>Карта не загрузилась</strong>
                <span>
                  {mapError.includes("FORBIDDEN") || mapError.includes("Invalid api key")
                    ? "Ключ Yandex Maps API не разрешает этот адрес. Добавь localhost:5174 в HTTP Referer в кабинете developer.tech.yandex.ru → Ключ → Ограничения."
                    : mapError.includes("limited")
                    ? "Ключ Yandex Maps требует HTTP Referer. Добавь localhost:5174 в разрешённые домены ключа."
                    : mapError.includes("YANDEX_MAPS")
                    ? "Яндекс не отдал JS API. Проверь, что ключ активен и домен приложения добавлен в разрешения."
                    : mapError || "Проверь API-ключ и ограничения HTTP Referer."}
                </span>
                <a
                  href="https://developer.tech.yandex.ru/"
                  target="_blank"
                  rel="noopener noreferrer"
                  className="action-button primary"
                  style={{ marginTop: 12, display: "inline-flex", fontSize: 13 }}
                >
                  Открыть кабинет Яндекса
                </a>
              </>
            ) : (
              <>
                <strong>Загрузка Яндекс Карты</strong>
                <span>Подключаем слой карты и точки АЗС.</span>
              </>
            )}
          </div>
        )}
      </div>
    </motion.div>
  );
}

function MapStationPreview({ station, onOpen, onDismiss }) {
  return (
    <motion.div
      className="map-preview"
      role="dialog"
      aria-label={`Превью АЗС № ${station.stationNumber || station.ksss}`}
      initial={{ opacity: 0, y: 26, scale: 0.96 }}
      animate={{ opacity: 1, y: 0, scale: 1 }}
      exit={{ opacity: 0, y: 18, scale: 0.97 }}
      transition={{ type: "spring", stiffness: 380, damping: 32, mass: 0.8 }}
    >
      <button className="map-preview-dismiss" type="button" onClick={onDismiss} aria-label="Скрыть превью">
        <X size={15} />
      </button>
      <div className="map-preview-body">
        <div className="map-preview-number" aria-hidden="true">
          {station.stationNumber || station.ksss || "—"}
        </div>
        <div className="map-preview-info">
          <strong>{station.name || `АЗС № ${station.stationNumber}`}</strong>
          <span>{station.address || station.subject || "Адрес не указан"}</span>
          <span className={`status-chip tone-${statusTone(station.status)}`}>
            <i aria-hidden="true" />
            {shortStatus(station.status)}
          </span>
        </div>
      </div>
      <button className="map-preview-open action-button primary" type="button" onClick={onOpen}>
        Подробнее <ChevronRight size={16} />
      </button>
    </motion.div>
  );
}

function StationDetail({ station, favorite, onFavorite, onClose }) {
  const routeUrl = hasValidPoint(station)
    ? `https://yandex.ru/maps/?rtext=~${station.lat},${station.lon}&rtt=auto`
    : "";
  const phone = bestPhone(station);
  const [dragOffset, setDragOffset] = useState(0);
  const [dragging, setDragging] = useState(false);
  const dragStartRef = useRef(null);
  const dragStartTimeRef = useRef(0);
  const panelRef = useRef(null);

  useEffect(() => {
    const timer = setTimeout(() => {
      panelRef.current?.querySelector('button, [href], input')?.focus();
    }, 100);
    return () => clearTimeout(timer);
  }, [station.id]);

  // Свайп-закрытие разрешён только когда контент проскроллен до самого верха —
  // иначе обычный скролл анкеты будет случайно закрывать её.
  function beginSheetDrag(clientY, scrollTop) {
    if (scrollTop > 0) return;
    dragStartRef.current = clientY;
    dragStartTimeRef.current = Date.now();
    setDragging(true);
  }

  function updateSheetDrag(clientY) {
    if (dragStartRef.current == null) return;
    setDragOffset(Math.max(clientY - dragStartRef.current, 0));
  }

  function finishSheetDrag(clientY) {
    if (dragStartRef.current == null) return;
    const delta = clientY - dragStartRef.current;
    const elapsedMs = Math.max(Date.now() - dragStartTimeRef.current, 1);
    const velocity = delta / elapsedMs;
    dragStartRef.current = null;
    setDragging(false);
    setDragOffset(0);
    // Закрываем только по резкому (быстрому) или явно завершённому свайпу вниз.
    if (delta > 20 && (velocity > 0.6 || delta > 140)) {
      onClose();
    }
  }

  function handleTouchStart(event) {
    if (event.target.closest("a, button, summary, select, input")) return;
    beginSheetDrag(event.touches[0]?.clientY ?? 0, event.currentTarget.scrollTop);
  }

  function handleTouchMove(event) {
    updateSheetDrag(event.touches[0]?.clientY ?? dragStartRef.current ?? 0);
  }

  function handleTouchEnd(event) {
    finishSheetDrag(event.changedTouches[0]?.clientY ?? dragStartRef.current ?? 0);
  }

  return (
    <motion.aside
      ref={panelRef}
      role="dialog"
      aria-modal="true"
      aria-labelledby="detail-station-title"
      className={`detail ${dragging ? "dragging" : ""}`}
      initial={{ y: "100%" }}
      animate={{ y: dragging ? dragOffset : 0 }}
      exit={{ y: "100%" }}
      transition={dragging ? { duration: 0 } : { type: "spring", stiffness: 340, damping: 36, mass: 0.9 }}
      onTouchStart={handleTouchStart}
      onTouchMove={handleTouchMove}
      onTouchEnd={handleTouchEnd}
      onTouchCancel={handleTouchEnd}
    >
      <div className="detail-grabber" aria-hidden="true" />
      <div className="detail-head">
        <div className="detail-title-block">
          <div className="detail-meta-line">
            <span className="eyeless">{station.ksss}</span>
            <span className={`status-chip tone-${statusTone(station.status)}`}>
              <i aria-hidden="true" />
              {shortStatus(station.status)}
            </span>
          </div>
          <h2 id="detail-station-title">{station.name}</h2>
          <p>{station.address || station.subject}</p>
          <div className="detail-quick-facts" aria-label="Краткая информация">
            <span>{station.subject || "Регион не указан"}</span>
            <span>{station.npo || "НПО не указан"}</span>
            <span className={station.qualityIssues.length ? "warn" : "ok"}>
              {station.qualityIssues.length ? `${station.qualityIssues.length} замеч.` : "Данные без критичных замечаний"}
            </span>
          </div>
        </div>
        <div className="detail-head-actions">
          <button className={`icon-button favorite ${favorite ? "on" : ""}`} type="button" onClick={onFavorite} aria-label={favorite ? "Убрать из избранного" : "Добавить в избранное"} aria-pressed={favorite}>
            <Heart size={19} fill="currentColor" />
          </button>
          <button className="icon-button detail-close" type="button" onClick={onClose} aria-label="Закрыть анкету">
            <X size={20} />
          </button>
        </div>
      </div>

      <div className="action-row">
        <a className={`action-button ${phone ? "" : "disabled"}`} href={phone ? `tel:${phone}` : undefined}>
          <Phone size={17} /> Позвонить
        </a>
        <a className={`action-button primary ${routeUrl ? "" : "disabled"}`} href={routeUrl || undefined} target="_blank" rel="noreferrer">
          <Navigation size={17} /> Яндекс маршрут
        </a>
      </div>

      <StationFuelStock ksss={station.ksss} />
      <StationFuelOutages ksss={station.ksss} />
      <StationKpis ksss={station.ksss} />
      <StationStaff ksss={station.ksss} />

      <DetailGroup title="Основное" defaultOpen>
        <div className="fact-grid">
          <Fact label="КССС" value={station.ksss} />
          <Fact label="Номер" value={station.stationNumber} />
          <Fact label="Статус" value={station.status} />
          <Fact label="Тип" value={station.type} />
          <Fact label="НПО" value={station.npo} />
          <Fact label="Регион" value={station.subject} />
          <Fact label="Город" value={station.city} />
          <Fact label="Координаты" value={hasValidPoint(station) ? `${station.lat}, ${station.lon}` : ""} />
        </div>
      </DetailGroup>

      <DetailGroup title="Классификация">
        <div className="fact-grid">
          <Fact label="Формат" value={station.format} />
          <Fact label="Формат L2" value={station.formatLevel2} />
          <Fact label="Minale" value={station.formatMinale} />
          <Fact label="Локация" value={station.location} />
          <Fact label="Окружение" value={station.environment} wide />
        </div>
        <div className="flag-line">
          {station.flags.active && <Badge>Действующая</Badge>}
          {station.flags.agency && <Badge>Агентская схема</Badge>}
          {station.flags.likard && <Badge>Ликард</Badge>}
          {station.flags.teboil && <Badge>Тебойл</Badge>}
          {station.flags.md && <Badge>MD</Badge>}
        </div>
      </DetailGroup>

      <DetailGroup title="Сервисы" defaultOpen>
        <div className="service-icons">
          <Service icon={<Store size={17} />} title="Магазин" active={station.flags.hasShop} note={station.shop} />
          <Service icon={<Coffee size={17} />} title="Кафе" active={station.flags.hasCafe} note={station.lukCafeL2} />
          <Service icon={<Toilet size={17} />} title="Санузел" active={station.flags.hasToilet} note={station.toilet} />
          <Service icon={<ShieldCheck size={17} />} title="Знаковый" active={station.flags.landmark} note={station.flags.m11 ? "М-11" : station.flags.m12 ? "М-12" : ""} />
        </div>
        <div className="fact-grid section-gap">
          <Fact label="Кластер" value={station.serviceCluster} wide />
          <Fact label="LukCafe L1" value={station.lukCafeL1} />
          <Fact label="LukCafe L2" value={station.lukCafeL2} />
        </div>
      </DetailGroup>

      <DetailGroup title="Ответственные" defaultOpen>
        <Contact title="РУ" name={station.regionalManager} phone={station.regionalManagerPhone} />
        <Contact title="ТМ" name={station.territoryManager} phone={station.territoryManagerPhone} />
        <Contact title="Менеджер" name={station.manager} phone={station.managerPhone} />
        <Contact title="Старший оператор" name={station.seniorOperator} phone={station.seniorOperatorPhone} />
      </DetailGroup>

      <DetailGroup title="Инфраструктура">
        <div className="fact-grid">
          <Fact label="ТРК" value={station.trkCount} />
          <Fact label="Посты" value={station.postsCount} />
          <Fact label="Торгзал, м2" value={station.shopArea} />
          <Fact label="Операторная, м2" value={station.operatorArea} />
          <Fact label="Оплата" value={station.paymentType} wide />
        </div>
      </DetailGroup>

      <DetailGroup title="Трасса и история">
        <div className="fact-grid">
          <Fact label="Фед. трасса" value={station.roadFederal} />
          <Fact label="Номер трассы" value={station.roadNumber} />
          <Fact label="Наименование" value={station.roadName} wide />
          <Fact label="Дата изменения" value={station.dateChanged} wide />
          <Fact label="Комментарий" value={station.comments} wide />
        </div>
      </DetailGroup>

      {station.qualityIssues.length > 0 && (
        <DetailGroup title="Качество данных" warning defaultOpen>
          {station.qualityIssues.map((issue) => (
            <span key={issue}>{issue}</span>
          ))}
        </DetailGroup>
      )}
    </motion.aside>
  );
}

function StationFuelStock({ ksss }) {
  const { stockState, refresh } = useFuelStock(ksss);
  const groups = useMemo(() => normalizeFuelStockGroups(stockState.data?.items || []), [stockState.data]);
  const snapshotAt = formatFuelStockTimestamp(stockState.data?.snapshotAt);
  const importedAt = formatFuelStockTimestamp(stockState.data?.importedAt);
  const isLoadingInitial = stockState.status === "loading" && !stockState.data;
  const isUnavailable = stockState.status === "error" && !stockState.data;
  const hasAnyGroupData = groups.some((group) => group.hasData);
  const isNoData = stockState.status === "no-data" || (stockState.status === "ready" && !hasAnyGroupData);

  return (
    <section
      className="detail-section fuel-stock-section"
      aria-labelledby="fuel-stock-title"
      aria-busy={isLoadingInitial || stockState.refreshing}
    >
      <div className="fuel-stock-head">
        <div>
          <h3 id="fuel-stock-title">
            <Fuel size={16} /> Остатки топлива
          </h3>
          <div className="fuel-stock-meta">
            <span>
              <Clock3 size={13} aria-hidden="true" />
              {snapshotAt ? `Снимок ${snapshotAt}` : "Снимок не получен"}
            </span>
            {importedAt && <span>Импорт {importedAt}</span>}
          </div>
        </div>
        {stockState.refreshing && (
          <span className="fuel-stock-refreshing" role="status">
            <RefreshCw size={13} aria-hidden="true" /> Обновление
          </span>
        )}
      </div>

      {isLoadingInitial && <FuelStockSkeleton />}

      {isUnavailable && (
        <div className="kpi-message warning fuel-stock-message" role="alert">
          <AlertTriangle size={16} aria-hidden="true" />
          <span>Остатки временно недоступны</span>
          <button className="fuel-stock-retry" type="button" onClick={() => refresh(false)}>
            <RefreshCw size={14} aria-hidden="true" /> Повторить
          </button>
        </div>
      )}

      {isNoData && (
        <div className="kpi-message fuel-stock-message" role="status">
          <Gauge size={16} aria-hidden="true" />
          <span>Нет данных об остатках для этой АЗС</span>
        </div>
      )}

      {stockState.data?.stale && (
        <div className="fuel-stock-inline-warning" role="alert">
          <AlertTriangle size={15} aria-hidden="true" />
          <span>Данные устарели: проверьте актуальность перед операционным решением.</span>
        </div>
      )}

      {stockState.error && !isUnavailable && (
        <div className="fuel-stock-inline-warning" role="status">
          <AlertTriangle size={15} aria-hidden="true" />
          <span>{stockState.error}</span>
        </div>
      )}

      {!isLoadingInitial && groups.length > 0 && <FuelStockCarousel groups={groups} />}
    </section>
  );
}

function StationFuelOutages({ ksss }) {
  const reduceMotion = useReducedMotion();
  const { outageState, refresh } = useFuelOutages(ksss);
  const [expanded, setExpanded] = useState(false);
  const items = useMemo(
    () => [...(Array.isArray(outageState.data?.items) ? outageState.data.items : [])].sort(compareOutageItems),
    [outageState.data],
  );
  const reportAvailable = Boolean(outageState.data?.importedAt) || Number(outageState.data?.rowCount) > 0;
  const liveActiveCount = items.filter((item) => !String(item?.endTime || "").trim()).length;
  const dayEndCount = items.filter(isOutageDayEnd).length;
  const activeCount = liveActiveCount + dayEndCount;
  const totalHours = items.reduce((total, item) => total + (numericOrNull(item?.hours) || 0), 0);
  const expectedSales = items.reduce((total, item) => total + (numericOrNull(item?.expectedSalesLiters) || 0), 0);
  const visibleItems = expanded ? items : items.slice(0, 3);
  const reportTimestamp = formatFuelStockTimestamp(
    outageState.data?.sourceReceivedAt || outageState.data?.importedAt,
  );

  useEffect(() => {
    setExpanded(false);
  }, [ksss]);

  const stateMotion = reduceMotion
    ? { initial: false, animate: {}, exit: {}, transition: { duration: 0 } }
    : {
        initial: { opacity: 0, y: 8 },
        animate: { opacity: 1, y: 0 },
        exit: { opacity: 0, y: -5 },
        transition: { duration: 0.2, ease: "easeOut" },
      };

  return (
    <section
      className="detail-section fuel-outage-section"
      aria-labelledby="fuel-outage-title"
      aria-busy={outageState.status === "loading"}
    >
      <div className="fuel-outage-head">
        <div>
          <h3 id="fuel-outage-title">
            <Clock3 size={16} aria-hidden="true" /> Простои топлива
          </h3>
          {reportAvailable && reportTimestamp && (
            <span className="fuel-outage-report-meta">Отчет получен {reportTimestamp}</span>
          )}
        </div>
        {outageState.status === "ready" && reportAvailable && (
          <span className={`fuel-outage-state ${liveActiveCount ? "active" : dayEndCount ? "recorded" : items.length ? "recorded" : "clear"}`}>
            {liveActiveCount
              ? `Не завершены ${activeCount}`
              : dayEndCount
                ? `На конец суток ${dayEndCount}`
                : items.length
                  ? formatOutageCount(items.length)
                  : "Без простоев"}
          </span>
        )}
      </div>

      <AnimatePresence mode="wait" initial={false}>
        {outageState.status === "loading" && (
          <motion.div className="fuel-outage-loading" key="loading" role="status" {...stateMotion}>
            <i />
            <span><b /><small /></span>
          </motion.div>
        )}

        {outageState.status === "error" && (
          <motion.div className="fuel-outage-message error" key="error" role="alert" {...stateMotion}>
            <AlertTriangle size={18} aria-hidden="true" />
            <span>
              <strong>Простои временно недоступны</strong>
              <small>Не удалось получить актуальный отчет.</small>
            </span>
            <motion.button
              type="button"
              onClick={refresh}
              whileTap={reduceMotion ? undefined : { scale: 0.96 }}
            >
              <RefreshCw size={14} aria-hidden="true" /> Повторить
            </motion.button>
          </motion.div>
        )}

        {outageState.status === "ready" && !reportAvailable && (
          <motion.div className="fuel-outage-message neutral" key="no-report" role="status" {...stateMotion}>
            <Clock3 size={18} aria-hidden="true" />
            <span>
              <strong>Отчет о простоях еще не загружен</strong>
              <small>Блок заполнится после первого импорта из почты.</small>
            </span>
          </motion.div>
        )}

        {outageState.status === "ready" && reportAvailable && items.length === 0 && (
          <motion.div className="fuel-outage-message clear" key="clear" role="status" {...stateMotion}>
            <CheckCircle2 size={19} aria-hidden="true" />
            <span>
              <strong>Простоев топлива на АЗС нет</strong>
              <small>В актуальном отчете события по этому объекту не зафиксированы.</small>
            </span>
          </motion.div>
        )}

        {outageState.status === "ready" && reportAvailable && items.length > 0 && (
          <motion.div className="fuel-outage-content" key="events" {...stateMotion}>
            <dl className="fuel-outage-summary" aria-label="Сводка по простоям">
              <div>
                <dt>События</dt>
                <dd>{asInt(items.length)}</dd>
              </div>
              <div>
                <dt>Суммарный простой</dt>
                <dd>{formatOutageDuration(totalHours)}</dd>
              </div>
              <div>
                <dt>Ожид. реализация</dt>
                <dd>{formatOutageSales(expectedSales)}</dd>
              </div>
            </dl>

            <ul className="fuel-outage-list" aria-label="События простоев">
              <AnimatePresence initial={false}>
                {visibleItems.map((item, index) => {
                  const dayEnd = isOutageDayEnd(item);
                  const active = !String(item?.endTime || "").trim();
                  const statusClass = active ? "active" : dayEnd ? "day-end" : "completed";
                  const statusLabel = active ? "Идет сейчас" : dayEnd ? "На конец суток" : "Завершен";
                  return (
                    <motion.li
                      className={`fuel-outage-event ${statusClass}`}
                      key={`${item.date || "date"}-${item.startTime || "time"}-${item.product || "fuel"}-${index}`}
                      initial={reduceMotion ? false : { opacity: 0, y: 8 }}
                      animate={{ opacity: 1, y: 0 }}
                      exit={reduceMotion ? { opacity: 1 } : { opacity: 0, y: -5 }}
                      transition={reduceMotion ? { duration: 0 } : { duration: 0.2, delay: Math.min(index * 0.035, 0.14) }}
                    >
                      <span className="fuel-outage-marker" aria-hidden="true">
                        {active ? <AlertTriangle size={14} /> : dayEnd ? <Clock3 size={14} /> : <CheckCircle2 size={14} />}
                      </span>
                      <div className="fuel-outage-event-body">
                        <div className="fuel-outage-event-head">
                          <strong>{item.product || "Топливо не указано"}</strong>
                          <span>{statusLabel}</span>
                        </div>
                        <div className="fuel-outage-event-time">
                          <span>{formatOutageDate(item.date)}</span>
                          <i aria-hidden="true" />
                          <span>{outageTimeRange(item)}</span>
                        </div>
                        <div className="fuel-outage-event-facts">
                          <span>
                            <small>Длительность</small>
                            <b>{formatOutageDuration(item.hours)}</b>
                          </span>
                          <span>
                            <small>Ожид. реализация</small>
                            <b>{formatOutageSales(item.expectedSalesLiters)}</b>
                          </span>
                        </div>
                      </div>
                    </motion.li>
                  );
                })}
              </AnimatePresence>
            </ul>

            {items.length > 3 && (
              <motion.button
                className="fuel-outage-more"
                type="button"
                onClick={() => setExpanded((current) => !current)}
                whileTap={reduceMotion ? undefined : { scale: 0.98 }}
                aria-expanded={expanded}
              >
                {expanded ? "Свернуть" : `Показать еще ${items.length - 3}`}
                <motion.span animate={{ rotate: expanded ? 180 : 0 }} transition={{ duration: reduceMotion ? 0 : 0.18 }}>
                  <ChevronDown size={17} aria-hidden="true" />
                </motion.span>
              </motion.button>
            )}
          </motion.div>
        )}
      </AnimatePresence>
    </section>
  );
}

function FuelStockCarousel({ groups }) {
  const reduceMotion = useReducedMotion();
  const trackRef = useRef(null);
  const frameRef = useRef(0);
  const [navigation, setNavigation] = useState({ activeIndex: 0, canBack: false, canForward: groups.length > 1 });

  const updateNavigation = useCallback(() => {
    const track = trackRef.current;
    if (!track) return;
    const cards = Array.from(track.querySelectorAll("[data-fuel-stock-card]"));
    const trackLeft = track.getBoundingClientRect().left;
    let activeIndex = 0;
    let closestDistance = Number.POSITIVE_INFINITY;
    cards.forEach((card, index) => {
      const distance = Math.abs(card.getBoundingClientRect().left - trackLeft);
      if (distance < closestDistance) {
        closestDistance = distance;
        activeIndex = index;
      }
    });
    const maxScroll = Math.max(0, track.scrollWidth - track.clientWidth);
    setNavigation({
      activeIndex,
      canBack: track.scrollLeft > 4,
      canForward: track.scrollLeft < maxScroll - 4,
    });
  }, []);

  const scheduleNavigationUpdate = useCallback(() => {
    cancelAnimationFrame(frameRef.current);
    frameRef.current = requestAnimationFrame(updateNavigation);
  }, [updateNavigation]);

  useEffect(() => {
    scheduleNavigationUpdate();
    const track = trackRef.current;
    const observer = typeof ResizeObserver === "undefined" ? null : new ResizeObserver(scheduleNavigationUpdate);
    if (track) observer?.observe(track);
    window.addEventListener("resize", scheduleNavigationUpdate);
    return () => {
      cancelAnimationFrame(frameRef.current);
      observer?.disconnect();
      window.removeEventListener("resize", scheduleNavigationUpdate);
    };
  }, [groups.length, scheduleNavigationUpdate]);

  const move = useCallback((direction) => {
    const track = trackRef.current;
    if (!track) return;
    const cards = Array.from(track.querySelectorAll("[data-fuel-stock-card]"));
    const targetIndex = Math.max(0, Math.min(cards.length - 1, navigation.activeIndex + direction));
    const firstOffset = cards[0]?.offsetLeft || 0;
    const target = cards[targetIndex];
    if (!target) return;
    track.scrollTo({
      left: Math.max(0, target.offsetLeft - firstOffset),
      behavior: reduceMotion ? "auto" : "smooth",
    });
  }, [navigation.activeIndex, reduceMotion]);

  const handleKeyDown = (event) => {
    if (event.key !== "ArrowLeft" && event.key !== "ArrowRight") return;
    event.preventDefault();
    move(event.key === "ArrowLeft" ? -1 : 1);
  };

  return (
    <div className="fuel-stock-carousel">
      <div className="fuel-stock-carousel-nav">
        <span>{navigation.activeIndex + 1} из {groups.length}</span>
        <div>
          <motion.button
            type="button"
            className="fuel-stock-nav-button"
            disabled={!navigation.canBack}
            onClick={() => move(-1)}
            whileTap={reduceMotion ? undefined : { scale: 0.94 }}
            aria-label="Предыдущий вид топлива"
            title="Назад"
          >
            <ChevronLeft size={18} aria-hidden="true" />
          </motion.button>
          <motion.button
            type="button"
            className="fuel-stock-nav-button"
            disabled={!navigation.canForward}
            onClick={() => move(1)}
            whileTap={reduceMotion ? undefined : { scale: 0.94 }}
            aria-label="Следующий вид топлива"
            title="Вперед"
          >
            <ChevronRight size={18} aria-hidden="true" />
          </motion.button>
        </div>
      </div>
      <div
        ref={trackRef}
        className="fuel-stock-track-list"
        role="region"
        aria-label="Остатки по видам топлива. Используйте стрелки для прокрутки."
        tabIndex={groups.length > 1 ? 0 : -1}
        onScroll={scheduleNavigationUpdate}
        onKeyDown={handleKeyDown}
      >
        {groups.map((group, index) => (
          <FuelStockCard group={group} index={index} reduceMotion={reduceMotion} key={group.key} />
        ))}
      </div>
    </div>
  );
}

function FuelStockSkeleton() {
  return (
    <div className="fuel-stock-track-list loading" aria-label="Загрузка остатков топлива">
      {[0, 1, 2].map((item) => (
        <article className="fuel-stock-card loading" key={item}>
          <i />
          <b />
          <small />
        </article>
      ))}
    </div>
  );
}

function FuelStockCard({ group, index, reduceMotion }) {
  const item = group.item || {};
  const available = fuelAvailableTons(item);
  const fill = clampFuelPercent(group.percentage);
  const StatusIcon = group.tone === "green" ? CheckCircle2 : group.tone === "empty" ? Gauge : AlertTriangle;
  const stateLabel = group.onDeadStock
    ? ", отсутствует, доступен только технологический остаток"
    : group.capacityExceeded
      ? ", исходный объем DWH выше емкости"
      : group.isLow
        ? ", низкий остаток"
        : "";
  const ariaLabel = `${group.label}: доступно ${formatFuelTons(available)}, заполненность ${formatFuelPercent(group.percentage)}${stateLabel}`;

  return (
    <motion.article
      data-fuel-stock-card
      className={`fuel-stock-card tone-${group.tone} ${group.hasData ? "" : "no-data"} ${group.isLow ? "low" : ""} ${group.capacityExceeded ? "capacity-exceeded" : ""} ${group.onDeadStock ? "on-dead-stock" : ""}`}
      style={{ "--fuel-stock-fill": fill / 100 }}
      aria-label={ariaLabel}
      initial={reduceMotion ? false : { opacity: 0, y: 8 }}
      animate={{ opacity: 1, y: 0 }}
      transition={reduceMotion ? { duration: 0 } : { type: "spring", stiffness: 320, damping: 28, delay: Math.min(index * 0.04, 0.2) }}
    >
      <div className="fuel-stock-card-head">
        <strong>{group.label}</strong>
        <span className={`fuel-stock-band tone-${group.tone}`}>
          <StatusIcon size={13} aria-hidden="true" />
          {fuelStockToneLabel(group)}
        </span>
      </div>

      <div className="fuel-stock-value">
        <span>Доступно</span>
        <strong>
          {formatFuelTons(available)}
          <small>{formatFuelPercent(group.percentage)}</small>
        </strong>
      </div>

      <div className="fuel-stock-track" aria-hidden="true">
        <i />
      </div>

      {group.capacityExceeded && (
        <div className="fuel-stock-low capacity-warning">
          <AlertTriangle size={13} aria-hidden="true" />
          <span>DWH: объем выше емкости</span>
        </div>
      )}

      {group.onDeadStock && (
        <div className="fuel-stock-low dead-stock">
          <AlertTriangle size={13} aria-hidden="true" />
          <span>Только технологический остаток</span>
        </div>
      )}

    </motion.article>
  );
}

function StationKpis({ ksss }) {
  const reduceMotion = useReducedMotion();
  const [period, setPeriod] = useState(() => currentMonthPeriod());
  const [periods, setPeriods] = useState([]);
  const [kpiState, setKpiState] = useState({ status: "idle", data: null, error: "" });

  useEffect(() => {
    const controller = new AbortController();
    fetchJson("/api/kpis/periods", controller.signal)
      .then((data) => {
        const available = Array.isArray(data?.periods) ? data.periods : [];
        setPeriods(available);
        if (available.length && !available.includes(period)) {
          setPeriod(available[available.length - 1]);
        }
      })
      .catch(() => {});

    return () => controller.abort();
  }, []);

  useEffect(() => {
    if (!ksss) {
      setKpiState({ status: "no-data", data: null, error: "" });
      return undefined;
    }

    const controller = new AbortController();
    setKpiState({ status: "loading", data: null, error: "" });

    fetch(`/api/stations/${encodeURIComponent(ksss)}/kpis?period=${period}`, {
      signal: controller.signal,
      credentials: "include",
      headers: { Accept: "application/json" },
    })
      .then((response) => {
        if (response.status === 401) {
          emitAuthRequired();
          throw new Error("AUTH_REQUIRED");
        }
        if (response.status === 404) return null;
        if (!response.ok) throw new Error(`KPI_REQUEST_FAILED_${response.status}`);
        return response.json();
      })
      .then((data) => {
        if (!data || !Array.isArray(data.metrics) || data.metrics.length === 0) {
          setKpiState({ status: "no-data", data: null, error: "" });
          return;
        }
        setKpiState({ status: "ready", data, error: "" });
      })
      .catch((error) => {
        if (error.name === "AbortError") return;
        if (error.message === "AUTH_REQUIRED") return;
        setKpiState({ status: "error", data: null, error: error.message });
      });

    return () => controller.abort();
  }, [ksss, period]);

  return (
    <section className="detail-section kpi-section">
      <div className="kpi-head">
        <h3>
          <BarChart3 size={16} /> Показатели месяца
        </h3>
        <PeriodNavigator periods={periods} period={period} onChange={setPeriod} />
      </div>

      {kpiState.status === "loading" && (
        <div className="kpi-grid" aria-label="Загрузка показателей">
          {["revenue", "fuelVolume", "checks", "avgCheck"].map((id) => (
            <div className="kpi-card loading" key={id}>
              <i />
              <b />
              <small />
            </div>
          ))}
        </div>
      )}

      {kpiState.status === "error" && (
        <div className="kpi-message warning">
          <AlertTriangle size={16} />
          <span>Показатели временно недоступны</span>
        </div>
      )}

      {kpiState.status === "no-data" && (
        <>
          <div className="kpi-source">Нет информации по этой АЗС за выбранный месяц</div>
          <div className="kpi-grid">
            {noInfoKpiMetrics.map((metric) => (
              <article className="kpi-card no-info" key={metric.id}>
                <span>{metric.label}</span>
                <strong>{formatKpiValue(metric.value, metric.unit)}</strong>
                <small>нет инфы</small>
              </article>
            ))}
          </div>
        </>
      )}

      {kpiState.status === "ready" && (
        <>
          <div className="kpi-source">
            {kpiState.data.source === "mock"
              ? "Демо-данные API до подключения SQL"
              : kpiState.data.source === "local"
                ? `Агрегаты DWH · обновлено ${formatMetaDate(kpiState.data)}`
                : "Данные из БД"}
          </div>
          <div className="kpi-grid">
            {kpiState.data.metrics.map((metric, index) => (
              <motion.article
                className="kpi-card"
                key={metric.id}
                initial={reduceMotion ? false : { opacity: 0, y: 10 }}
                animate={{ opacity: 1, y: 0 }}
                transition={{ type: "spring", stiffness: 240, damping: 28, delay: reduceMotion ? 0 : index * 0.065 }}
                whileHover={reduceMotion ? undefined : { y: -2, transition: { duration: 0.15 } }}
              >
                <span>{metric.label}</span>
                <strong>{formatKpiValue(metric.value, metric.unit)}</strong>
                <div className="kpi-deltas">
                  <small className={deltaTone(metric.momPct)}>MoM {formatDelta(metric.momPct)}</small>
                  <small className={deltaTone(metric.yoyPct)}>YoY {formatDelta(metric.yoyPct)}</small>
                </div>
              </motion.article>
            ))}
          </div>
        </>
      )}
    </section>
  );
}

function StationStaff({ ksss }) {
  const [period, setPeriod] = useState(() => currentMonthPeriod());
  const [periods, setPeriods] = useState([]);
  const [periodsStatus, setPeriodsStatus] = useState("loading");
  const [staffState, setStaffState] = useState({ status: "idle", data: null, error: "" });
  const calendarRef = useRef(null);
  const activeDayRef = useRef(null);

  useEffect(() => {
    const controller = new AbortController();
    fetchJson("/api/staff/periods", controller.signal)
      .then((data) => {
        const available = Array.isArray(data?.periods) ? data.periods : [];
        setPeriods(available);
        if (available.length && !available.includes(period)) {
          setPeriod(available[available.length - 1]);
        }
        setPeriodsStatus("ready");
      })
      .catch((error) => {
        if (error.name === "AbortError") return;
        setPeriodsStatus("error");
      });

    return () => controller.abort();
  }, []);

  useEffect(() => {
    if (periodsStatus !== "ready" || periods.length === 0) {
      return undefined;
    }

    if (!ksss) {
      setStaffState({ status: "no-data", data: null, error: "" });
      return undefined;
    }

    const controller = new AbortController();
    setStaffState({ status: "loading", data: null, error: "" });

    fetchJson(`/api/stations/${encodeURIComponent(ksss)}/staff?period=${period}`, controller.signal)
      .then((data) => {
        if (!data || !Array.isArray(data.days) || !data.days.length) {
          setStaffState({ status: "no-data", data: noInfoStaffPayload(ksss, period), error: "" });
          return;
        }
        setStaffState({ status: "ready", data, error: "" });
      })
      .catch((error) => {
        if (error.name === "AbortError") return;
        setStaffState({ status: "error", data: null, error: error.message });
      });

    return () => controller.abort();
  }, [ksss, period, periodsStatus, periods.length]);

  useEffect(() => {
    if (!["ready", "no-data"].includes(staffState.status)) return undefined;

    const frame = window.requestAnimationFrame(() => {
      const calendar = calendarRef.current;
      const activeDay = activeDayRef.current;
      if (!calendar || !activeDay) return;

      calendar.scrollTo({
        left: activeDay.offsetLeft - calendar.offsetLeft,
        behavior: "auto",
      });
    });

    return () => window.cancelAnimationFrame(frame);
  }, [staffState.status, staffState.data?.ksss, staffState.data?.period, staffState.data?.today?.date]);

  if (periodsStatus === "ready" && periods.length === 0) {
    return null;
  }

  return (
    <section className="detail-section staff-section">
      <div className="kpi-head">
        <h3>
          <Users size={16} /> Рекомендации по персоналу
        </h3>
        <PeriodNavigator periods={periods} period={period} onChange={setPeriod} label="Период рекомендаций" />
      </div>

      {(periodsStatus === "loading" || staffState.status === "loading") && (
        <div className="staff-loading">
          <div className="kpi-card loading">
            <i />
            <b />
            <small />
          </div>
          <div className="staff-calendar">
            {[1, 2, 3, 4].map((item) => (
              <span className="staff-day loading" key={item} />
            ))}
          </div>
        </div>
      )}

      {(periodsStatus === "error" || staffState.status === "error") && (
        <div className="kpi-message warning">
          <AlertTriangle size={16} />
          <span>Данные по персоналу временно недоступны</span>
        </div>
      )}

      {staffState.status === "no-data" && (
        <>
          <div className="kpi-source">Нет рекомендаций по этой АЗС за выбранный месяц</div>
          <div className="staff-summary no-info">
            <article>
              <span>Максимум за сутки</span>
              <strong>{formatStaffValue(staffState.data?.staffTotal)} чел.</strong>
            </article>
            <article>
              <span>Выбранный день</span>
              <strong>{formatStaffValue(staffState.data?.today?.day)} днем · {formatStaffValue(staffState.data?.today?.night)} ночью</strong>
            </article>
          </div>
          <div className="staff-calendar" ref={calendarRef} aria-label="Рекомендации по сменам на месяц">
            {(staffState.data?.days || []).map((day) => {
              const active = day.date === staffState.data?.today?.date;
              return (
                <article className={`staff-day no-info ${active ? "active" : ""}`} ref={active ? activeDayRef : null} key={day.date}>
                  <span>{formatShortDate(day.date)}</span>
                  <small>{formatWeekday(day.date)}</small>
                  <b>{formatStaffValue(day.day)}</b>
                  <em>{formatStaffValue(day.night)}</em>
                </article>
              );
            })}
          </div>
        </>
      )}

      {staffState.status === "ready" && (
        <>
          <div className="kpi-source">
            {staffState.data.source === "mock"
              ? "Демо-рекомендации до подключения SQL"
              : staffState.data.source === "file"
                ? "Рекомендации из Excel"
                : "Данные из БД"}
          </div>
          <div className="staff-summary">
            <article>
              <span>Максимум за сутки</span>
              <strong>{formatStaffValue(staffState.data.staffTotal)} чел.</strong>
            </article>
            <article>
              <span>Выбранный день</span>
              <strong>{formatStaffValue(staffState.data.today.day)} днем · {formatStaffValue(staffState.data.today.night)} ночью</strong>
            </article>
          </div>
          <div className="staff-calendar" ref={calendarRef} aria-label="Рекомендации по сменам на месяц">
            {staffState.data.days.map((day) => {
              const active = day.date === staffState.data.today.date;
              return (
                <article className={`staff-day ${active ? "active" : ""}`} ref={active ? activeDayRef : null} key={day.date}>
                  <span>{formatShortDate(day.date)}</span>
                  <small>{formatWeekday(day.date)}</small>
                  <b>{formatStaffValue(day.day)}</b>
                  <em>{formatStaffValue(day.night)}</em>
                </article>
              );
            })}
          </div>
        </>
      )}
    </section>
  );
}

function DetailGroup({ title, children, defaultOpen = false, warning = false }) {
  return (
    <details className={`detail-section detail-group ${warning ? "warning" : ""}`} open={defaultOpen}>
      <summary>
        <h3>{warning && <AlertTriangle size={16} />} {title}</h3>
        <ChevronDown size={16} />
      </summary>
      {children}
    </details>
  );
}

function Fact({ label, value, wide = false }) {
  return (
    <div className={`fact ${wide ? "wide" : ""}`}>
      <span>{label}</span>
      <strong>{value || "—"}</strong>
    </div>
  );
}

function Service({ icon, title, active, note }) {
  return (
    <div className={`service ${active ? "active" : ""}`}>
      {icon}
      <strong>{title}</strong>
      <span>{note || (active ? "Есть" : "Нет")}</span>
    </div>
  );
}

function Contact({ title, name, phone }) {
  return (
    <div className="contact">
      <CircleDot size={15} />
      <div>
        <span>{title}</span>
        <strong>{formatPersonName(name)}</strong>
      </div>
      {phone && <a href={`tel:${phone}`}>{phone}</a>}
    </div>
  );
}

function FilterSheet({ filters, options, setFilter, onClose, onReset, fuelAvailability }) {
  const [touchStart, setTouchStart] = useState(null);

  function handleTouchEnd(event) {
    if (touchStart == null) return;
    const endY = event.changedTouches[0]?.clientY ?? touchStart;
    setTouchStart(null);
    if (endY - touchStart > 58) onClose();
  }

  return (
    <div className="sheet-backdrop" onClick={onClose}>
      <div
        className="filter-sheet"
        role="dialog"
        aria-modal="true"
        aria-label="Фильтры"
        onClick={(event) => event.stopPropagation()}
        onTouchStart={(event) => setTouchStart(event.touches[0]?.clientY ?? null)}
        onTouchEnd={handleTouchEnd}
      >
        <button className="sheet-grabber" type="button" onClick={onClose} aria-label="Закрыть фильтры" />
        <div className="sheet-title">
          <strong>Фильтры</strong>
          <button className="icon-button" onClick={onClose} type="button" aria-label="Закрыть">
            <X size={19} />
          </button>
        </div>
        <FilterRail filters={filters} options={options} setFilter={setFilter} fuelAvailability={fuelAvailability} />
        <SelectChip label="Качество" value={filters.quality} options={[
          ["issues", "Есть замечания"],
        ]} onChange={(value) => setFilter("quality", value)} />
        <FuelFilterNotice availability={fuelAvailability} onClear={() => setFilter("fuel", [])} />
        <button className="reset-button" onClick={onReset} type="button">Сбросить фильтры</button>
      </div>
    </div>
  );
}

createRoot(document.getElementById("root")).render(<App />);
