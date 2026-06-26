import React, { useEffect, useMemo, useRef, useState } from "react";
import { createRoot } from "react-dom/client";
import { max } from "d3-array";
import { scaleLinear } from "d3-scale";
import { AnimatePresence, motion, useReducedMotion } from "framer-motion";
import {
  AlertTriangle,
  BarChart3,
  CheckCircle2,
  ChevronDown,
  CircleDot,
  Coffee,
  Filter,
  Heart,
  Home,
  List,
  LocateFixed,
  Map as MapIcon,
  MessageSquare,
  Navigation,
  Phone,
  Send,
  Users,
  Search,
  ShieldCheck,
  SlidersHorizontal,
  Store,
  Toilet,
  X,
} from "lucide-react";
import "./styles.css";

const statusColors = {
  "Действующая": "#14945f",
  CODO: "#14945f",
  Консервация: "#d09416",
  Реконструкция: "#7b61ff",
  Строительство: "#3077d8",
  Оптимизация: "#d65f32",
  Продана: "#8f99a8",
};

const defaultFilters = {
  npo: "",
  subject: "",
  status: "",
  type: "",
  location: "",
  service: "",
  quality: "",
};

const viewItems = [
  { id: "list", label: "Реестр", mobileLabel: "Реестр", Icon: List },
  { id: "map", label: "Карта", mobileLabel: "Карта", Icon: MapIcon },
  { id: "home", label: "Главная", mobileLabel: "Главная", Icon: Home },
  { id: "analytics", label: "Аналитика", mobileLabel: "Аналитика", Icon: BarChart3 },
  { id: "quality", label: "Контроль", mobileLabel: "Контроль", Icon: ShieldCheck },
];

const YANDEX_MAPS_API_KEY = import.meta.env.VITE_YANDEX_MAPS_API_KEY || "";
const UPDATE_DATA_COMMAND = "cd /Users/artmanoking/Downloads/Projects/azs-mobile/azs-mobile && npm run prepare-data";
let yandexMapsPromise;

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
  if (status === "Действующая" || status === "CODO") return "green";
  if (status === "Реконструкция" || status === "Строительство") return "blue";
  if (status === "Консервация") return "amber";
  if (status === "Оптимизация" || status === "Продана") return "red";
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
      iconColor: selectedId === station.id ? "#c91d32" : statusColors[station.status] || "#8f99a8",
    },
  };
}

function createStationMarker(ymaps3, station, selectedId, onSelect) {
  const element = document.createElement("button");
  element.type = "button";
  element.className = `ymap-marker ${selectedId === station.id ? "selected" : ""}`;
  element.style.setProperty("--marker-color", selectedId === station.id ? "#c91d32" : statusColors[station.status] || "#8f99a8");
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
    ["Консервация", statusColors["Консервация"]],
    ["Реконструкция", statusColors["Реконструкция"]],
    ["Оптимизация", statusColors["Оптимизация"]],
    ["Другой статус", "#8f99a8"],
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
  const numeric = Number(value);
  if (!Number.isFinite(numeric)) return "—";
  return `${numeric > 0 ? "+" : ""}${numeric.toLocaleString("ru-RU", { maximumFractionDigits: 1 })}%`;
}

function deltaTone(value) {
  const numeric = Number(value);
  if (!Number.isFinite(numeric) || numeric === 0) return "";
  return numeric > 0 ? "positive" : "negative";
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

function demoPct(ksss, period, metricId, salt) {
  return stableNumber(`${ksss}:${period}:${metricId}:${salt}`, -120, 180) / 10;
}

function demoKpiMetrics(ksss, period) {
  const revenue = stableNumber(`${ksss}:${period}:revenue`, 4_000_000, 28_000_000);
  const fuelVolume = stableNumber(`${ksss}:${period}:fuelVolume`, 120_000, 850_000);
  const checks = stableNumber(`${ksss}:${period}:checks`, 8_000, 62_000);
  const avgCheck = Math.round(revenue / Math.max(checks, 1));
  return [
    ["revenue", "Выручка", revenue, "₽"],
    ["fuelVolume", "Объем топлива", fuelVolume, "л"],
    ["checks", "Чеки", checks, "шт"],
    ["avgCheck", "Средний чек", avgCheck, "₽"],
  ].map(([id, label, value, unit]) => ({
    id,
    label,
    value,
    unit,
    momPct: demoPct(ksss, period, id, "mom"),
    yoyPct: demoPct(ksss, period, id, "yoy"),
  }));
}

function demoKpiPayload(ksss, period, reason = "fallback") {
  return {
    ksss,
    period,
    source: "placeholder",
    fallbackReason: reason,
    updatedAt: new Date().toISOString(),
    metrics: demoKpiMetrics(ksss, period),
  };
}

function monthDates(period) {
  const [year, month] = period.split("-").map(Number);
  const days = new Date(year, month, 0).getDate();
  return Array.from({ length: days }, (_, index) => new Date(year, month - 1, index + 1));
}

function demoStaffPayload(ksss, period, reason = "fallback") {
  const staffTotal = stableNumber(`${ksss}:${period}:staffTotal`, 9, 24);
  const days = monthDates(period).map((date) => {
    const weekday = date.getDay();
    const dayMin = Math.max(2, Math.round(staffTotal * 0.34));
    const dayMax = Math.max(dayMin, Math.round(staffTotal * 0.58));
    const nightMin = Math.max(1, Math.round(staffTotal * 0.18));
    const nightMax = Math.max(nightMin, Math.round(staffTotal * 0.34));
    const dateIso = date.toISOString().slice(0, 10);
    const weekendOffset = weekday === 0 || weekday === 6 ? 1 : 0;
    return {
      date: dateIso,
      label: formatWeekday(dateIso),
      day: Math.max(dayMin, stableNumber(`${ksss}:${dateIso}:day`, dayMin, dayMax) - weekendOffset),
      night: stableNumber(`${ksss}:${dateIso}:night`, nightMin, nightMax),
    };
  });
  const todayIso = new Date().toISOString().slice(0, 10);
  const today = days.find((day) => day.date === todayIso) || days[0];
  return {
    ksss,
    period,
    source: "placeholder",
    fallbackReason: reason,
    updatedAt: new Date().toISOString(),
    staffTotal,
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

function fetchJson(url, signal) {
  return fetch(url, { signal, headers: { Accept: "application/json" } }).then((response) => {
    if (response.status === 404) return null;
    if (!response.ok) throw new Error(`REQUEST_FAILED_${response.status}`);
    return response.json();
  });
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
  const [payload, setPayload] = useState({ meta: null, stations: [] });
  const [query, setQuery] = useState("");
  const [filters, setFilters] = useState(defaultFilters);
  const [mode, setMode] = useState("home");
  const [selectedId, setSelectedId] = useState("");
  const [selectionMode, setSelectionMode] = useState("auto");
  const [registryCompact, setRegistryCompact] = useState(false);
  const [registryDensity, setRegistryDensity] = useState("comfortable");
  const [favorites, setFavorites] = useState(() => JSON.parse(localStorage.getItem("azs:favorites") || "[]"));
  const [showFilters, setShowFilters] = useState(false);
  const [detailSheet, setDetailSheet] = useState("half");

  useEffect(() => {
    fetch("/stations.json")
      .then((response) => (response.ok ? response : fetch("/stations.sample.json")))
      .then((response) => response.json())
      .then((data) => {
        setPayload(data);
      });
  }, []);

  useEffect(() => {
    localStorage.setItem("azs:favorites", JSON.stringify(favorites));
  }, [favorites]);

  useEffect(() => {
    setSelectionMode("auto");
    setSelectedId("");
    setRegistryCompact(false);
  }, [query, filters.npo, filters.subject, filters.status, filters.type, filters.location, filters.service, filters.quality]);

  const stations = useMemo(() => payload.stations.filter(hasValidPoint), [payload.stations]);
  const excludedNoCoords = Math.max((payload.meta?.count || payload.stations.length) - stations.length, 0);
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

  const filtered = useMemo(() => {
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
      return true;
    });
  }, [stations, query, filters]);

  const selected = filtered.find((station) => station.id === selectedId) || null;
  const detailVisible = Boolean(selected && detailSheet !== "closed");

  const metrics = useMemo(() => {
    const active = stations.filter((station) => station.flags.active).length;
    const cafe = stations.filter((station) => station.flags.hasCafe).length;
    const toilet = stations.filter((station) => station.flags.hasToilet).length;
    return { active, cafe, toilet };
  }, [stations]);
  const issueCount = useMemo(() => filtered.filter((station) => station.qualityIssues.length > 0).length, [filtered]);
  const viewMotion = motionPreset(reduceMotion);

  function setFilter(key, value) {
    setFilters((current) => ({ ...current, [key]: value }));
  }

  function toggleFavorite(id) {
    setFavorites((current) => (current.includes(id) ? current.filter((item) => item !== id) : [...current, id]));
  }

  function selectStation(id) {
    setSelectedId(id);
    setSelectionMode("manual");
    setDetailSheet("half");
  }

  function changeMode(nextMode, { closeDetail = true } = {}) {
    setMode(nextMode);
    setRegistryCompact(false);
    if (closeDetail && selectedId) setDetailSheet("closed");
  }

  function handleRegistryScroll(scrollTop) {
    setRegistryCompact(scrollTop > 28);
  }

  return (
    <main className={`app-shell ${mode}-mode ${detailVisible ? "" : "no-detail"} ${mode === "list" && registryCompact ? "registry-compact" : ""}`}>
      <section className="workspace">
        {mode !== "home" && (
          <>
            <header className="topbar">
              <div>
                <h1>АЗС</h1>
                <p>
                  {payload.meta
                    ? `${asInt(stations.length)} на карте${excludedNoCoords ? ` · скрыто ${asInt(excludedNoCoords)}` : ""}`
                    : "Загрузка данных"}
                </p>
              </div>
              <button className="icon-button" type="button" onClick={() => setShowFilters(true)} aria-label="Фильтры">
                <SlidersHorizontal size={20} />
              </button>
            </header>

            <div className="search-row">
              <Search size={18} />
              <input
                value={query}
                onChange={(event) => setQuery(event.target.value)}
                placeholder="КССС, номер, адрес, регион"
              />
              {query && (
                <button className="clear-button" onClick={() => setQuery("")} aria-label="Очистить поиск">
                  <X size={16} />
                </button>
              )}
            </div>

            <ModeSwitcher items={viewItems} mode={mode} onChange={changeMode} />

            <MetricStrip count={filtered.length} metrics={metrics} />

            <FilterRail filters={filters} options={options} setFilter={setFilter} />
          </>
        )}

        <AnimatePresence mode="popLayout" initial={false}>
          {mode === "home" ? (
            <motion.div className="view-stage" key="home" {...viewMotion}>
              <HomeDashboard
                count={filtered.length}
                total={stations.length}
                metrics={metrics}
                issueCount={issueCount}
                meta={payload.meta}
                regionCount={options.subject.length}
                excludedNoCoords={excludedNoCoords}
                onOpenList={() => changeMode("list")}
                onOpenMap={() => changeMode("map")}
                onOpenAnalytics={() => changeMode("analytics")}
                onOpenControl={() => changeMode("quality")}
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
          ) : mode === "quality" ? (
            <motion.div className="view-stage" key="quality" {...viewMotion}>
              <ControlDashboard
                stations={filtered}
                totalStations={stations}
                onFilter={setFilter}
                onOpenList={() => changeMode("list")}
                onOpenStation={(id) => {
                  changeMode("list", { closeDetail: false });
                  selectStation(id);
                }}
              />
            </motion.div>
          ) : (
            <motion.div className="view-stage content-grid" key={mode} {...viewMotion}>
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
                />
              </section>

              <section className={`map-pane ${mode === "list" ? "mobile-hidden" : ""}`}>
                <StationMap
                  stations={filtered}
                  selected={selected}
                  focusSelected={selectionMode === "manual"}
                  onSelect={selectStation}
                />
              </section>
            </motion.div>
          )}
        </AnimatePresence>
      </section>

      <AnimatePresence>
        {detailVisible && (
          <StationDetail
            station={selected}
            favorite={favorites.includes(selected.id)}
            onFavorite={() => toggleFavorite(selected.id)}
            sheetState={detailSheet}
            onSheetState={setDetailSheet}
          />
        )}
      </AnimatePresence>

      {showFilters && (
        <FilterSheet
          filters={filters}
          options={options}
          setFilter={setFilter}
          onClose={() => setShowFilters(false)}
          onReset={() => setFilters(defaultFilters)}
        />
      )}

      <BottomStrip
        items={viewItems}
        mode={mode}
        count={filtered.length}
        issueCount={issueCount}
        onChange={changeMode}
      />
    </main>
  );
}

function ModeSwitcher({ items, mode, onChange }) {
  return (
    <div className="mode-row" role="tablist" aria-label="Разделы классификатора">
      {items.map(({ id, label, Icon }) => (
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
          <Icon size={16} />
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
      {items.map(({ id, mobileLabel, Icon }) => {
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
            <span className="bottom-nav-icon">
              <Icon size={19} />
            </span>
            <span className="bottom-nav-label">{mobileLabel}</span>
            {badges[id] && <span className={`bottom-nav-meta ${id === "quality" && issueCount ? "warning" : ""}`}>{badges[id]}</span>}
          </motion.button>
        );
      })}
    </nav>
  );
}

function HomeDashboard({
  count,
  total,
  metrics,
  issueCount,
  meta,
  regionCount,
  excludedNoCoords,
  onOpenList,
  onOpenMap,
  onOpenAnalytics,
  onOpenControl,
}) {
  const activeShare = total ? Math.round((metrics.active / total) * 100) : 0;
  const homeLinks = [
    { label: "Реестр", Icon: List, onClick: onOpenList },
    { label: "Карта", Icon: MapIcon, onClick: onOpenMap },
    { label: "Аналитика", Icon: BarChart3, onClick: onOpenAnalytics },
    { label: "Контроль", Icon: ShieldCheck, onClick: onOpenControl },
  ];
  const passportItems = [
    { label: "Объектов", value: asInt(total), helper: `в текущем срезе ${asInt(count)}` },
    { label: "Активная сеть", value: `${activeShare}%`, helper: `${asInt(metrics.active)} действующих` },
    { label: "Регионов", value: asInt(regionCount), helper: "география присутствия" },
    { label: "Источник", value: formatMetaDate(meta), helper: excludedNoCoords ? `без координат ${asInt(excludedNoCoords)}` : "координаты готовы" },
  ];

  return (
    <section className="home-pane dala-home" aria-labelledby="home-title">
      <div className="home-hero">
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

      <div className="home-grid">
        <motion.article className="home-card" whileHover={{ y: -3 }} transition={{ duration: 0.18 }}>
          <BarChart3 size={18} />
          <h3>Аналитика и сравнение</h3>
          <p>Показывает распределения по статусам, НПО, регионам, форматам, а также подбор похожих АЗС и сравнение показателей.</p>
        </motion.article>
        <motion.article className="home-card" whileHover={{ y: -3 }} transition={{ duration: 0.18 }}>
          <ShieldCheck size={18} />
          <h3>Контроль качества</h3>
          <p>Помогает найти карточки с замечаниями: отсутствующие контакты, ответственные, координаты или неполные сервисные признаки.</p>
        </motion.article>
        <motion.article className="home-card" whileHover={{ y: -3 }} transition={{ duration: 0.18 }}>
          <Users size={18} />
          <h3>Персонал и KPI</h3>
          <p>Показатели месяца и персонал находятся внутри карточки конкретной АЗС. Откройте объект из реестра или карты, чтобы увидеть эти блоки.</p>
        </motion.article>
        <motion.article className="home-card" whileHover={{ y: -3 }} transition={{ duration: 0.18 }}>
          <MessageSquare size={18} />
          <h3>Обратная связь</h3>
          <p>Если в карточке обнаружена неправильная информация, перейдите в “Контроль” и оставьте уточнение в форме обратной связи.</p>
        </motion.article>
      </div>

      <div className="home-summary">
        <span><CircleDot size={15} /><strong>{asInt(count)}</strong> в текущем срезе</span>
        <span><CheckCircle2 size={15} /><strong>{asInt(metrics.active)}</strong> действующих</span>
        <span><Coffee size={15} /><strong>{asInt(metrics.cafe)}</strong> с кафе</span>
        <button type="button" onClick={onOpenControl}>
          {issueCount ? `${asInt(issueCount)} замечаний` : "Замечаний нет"}
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
  const period = useMemo(() => currentMonthPeriod(), []);
  const [view, setView] = useState("overview");
  const [groupBy, setGroupBy] = useState("territoryManager");
  const [overviewState, setOverviewState] = useState({ status: "idle", data: null, error: "" });
  const [similarState, setSimilarState] = useState({ status: "idle", data: null, error: "" });
  const [compareState, setCompareState] = useState({ status: "idle", data: null, error: "" });
  const [similarBaseId, setSimilarBaseId] = useState("");
  const [similarGeo, setSimilarGeo] = useState({ status: "idle", message: "" });
  const [similarAutoTried, setSimilarAutoTried] = useState(false);
  const [compareIds, setCompareIds] = useState(() => (selected?.ksss ? [selected.ksss] : []));
  const [compareNotice, setCompareNotice] = useState("");
  const similarBase = stations.find((station) => station.ksss === similarBaseId);

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

  const tabs = [
    ["overview", "Обзор"],
    ["slices", "Разрезы"],
    ["similar", "Похожие"],
    ["compare", "Сравнение"],
  ];

  return (
    <section className="analytics-pane">
      <div className="analytics-head">
        <div>
          <h2>Аналитика сети</h2>
          <p>
            {view === "overview"
              ? `Показатели пересчитываются по текущей выборке: ${asInt(stations.length)} из ${asInt(totalStations.length)} объектов.`
              : `Период: ${formatPeriod(period)} · источник API /api`}
          </p>
        </div>
      </div>

      <div className="analytics-tabs" role="tablist" aria-label="Режим аналитики">
        {tabs.map(([id, label]) => (
          <button className={view === id ? "active" : ""} type="button" key={id} onClick={() => setView(id)}>
            {label}
          </button>
        ))}
      </div>

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

function AnalyticsLocalOverview({ stations, totalStations, onFilter, onOpenList }) {
  const total = stations.length;
  const active = stations.filter((station) => station.flags.active).length;
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
          <p>Объекты без координат скрыты из рабочего среза, чтобы карта, маршруты и выездной сценарий оставались чистыми.</p>
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
        {state.data?.source && <span className="source-pill">{state.data.source === "mock" ? "mock" : "db"}</span>}
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
                    <strong>{row.label}</strong>
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
            emptyHint="Начните вводить АЗС для подбора похожих"
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
        emptyText="Выберите АЗС, чтобы подобрать похожие объекты."
        errorText="Подбор похожих АЗС временно недоступен"
      />

      {state.status === "ready" && (
        <div className="similar-list">
          {state.data.items.map((item) => (
            <article className="analytics-card similar-card" key={item.ksss}>
              <div className="similar-score">
                <strong>{item.score}</strong>
                <span>score</span>
              </div>
              <div className="similar-main">
                <h3>{item.name}</h3>
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
    ["РУ", (item) => item.regionalManager || "—"],
    ["ТМ", (item) => item.territoryManager || "—"],
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
                <th>Показатель</th>
                {state.data.items.map((item) => (
                  <th key={item.ksss}>
                    <strong>{item.name}</strong>
                    <span>{item.ksss}</span>
                  </th>
                ))}
              </tr>
            </thead>
            <tbody>
              {rows.map(([label, getter]) => (
                <tr key={label}>
                  <th>{label}</th>
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

function ControlDashboard({ stations, totalStations, onFilter, onOpenList, onOpenStation }) {
  const [copied, setCopied] = useState(false);
  const [feedback, setFeedback] = useState({ station: "", field: "", message: "" });
  const [feedbackSent, setFeedbackSent] = useState(false);
  const total = stations.length;
  const issueStations = stations.filter((station) => station.qualityIssues.length > 0);
  const noResponsible = stations.filter(missingResponsible);
  const noPhone = stations.filter(missingContactPhone);
  const issueCounts = groupTop(
    stations.flatMap((station) => station.qualityIssues).map((issue) => ({ issue })),
    (item) => item.issue,
    8,
  );
  const contactReady = stations.filter((station) => !missingResponsible(station) && !missingContactPhone(station)).length;
  const serviceReady = stations.filter((station) => station.flags.hasShop || station.flags.hasCafe || station.flags.hasToilet).length;

  function copyUpdateCommand() {
    navigator.clipboard?.writeText(UPDATE_DATA_COMMAND).then(() => {
      setCopied(true);
      window.setTimeout(() => setCopied(false), 1800);
    });
  }

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
          <h2>Контроль объектов</h2>
          <p>
            Рабочий срез для выезда: карточка, контакты, координаты, сервисы и готовность данных по текущей выборке.
          </p>
        </div>
      </div>

      <div className="analytics-kpis">
        <Kpi title="Карточек в срезе" value={total} share={pct(total, totalStations.length)} />
        <Kpi title="С замечаниями" value={issueStations.length} share={pct(issueStations.length, total)} tone="amber" />
        <Kpi title="Без телефона" value={noPhone.length} share={pct(noPhone.length, total)} tone="red" />
        <Kpi title="Без ответственного" value={noResponsible.length} share={pct(noResponsible.length, total)} tone="amber" />
      </div>

      <div className="control-grid">
        <div className="analytics-card readiness-card">
          <h3>Операционная готовность</h3>
          <ReadinessRow title="Карта и маршрут" value={total} total={total} />
          <ReadinessRow title="Контакты" value={Math.max(contactReady, 0)} total={total} />
          <ReadinessRow title="Сервисный профиль" value={serviceReady} total={total} />
        </div>

        <div className="analytics-card quality-list-card">
          <h3>Основные разрывы</h3>
          <button type="button" onClick={() => onFilter("quality", "issues")}>
            <AlertTriangle size={16} />
            <span>Любые замечания</span>
            <strong>{asInt(issueStations.length)}</strong>
          </button>
          <button type="button" onClick={onOpenList}>
            <Phone size={16} />
            <span>Проверить контакты</span>
            <strong>{asInt(noPhone.length)}</strong>
          </button>
        </div>

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

        <div className="analytics-card update-card">
          <h3>Обновление классификатора</h3>
          <p>Источник: `cls_2026_05_AZS.xlsx`. Локальная подготовка пересобирает `public/stations.json` для приложения.</p>
          <code>{UPDATE_DATA_COMMAND}</code>
          <button type="button" onClick={copyUpdateCommand}>{copied ? "Скопировано" : "Скопировать команду"}</button>
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
              <span className="status-dot" style={{ background: statusColors[station.status] || "#8f99a8" }} />
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
      <label>
        <span>АЗС или КССС</span>
        <input
          value={feedback.station}
          onChange={(event) => onChange((current) => ({ ...current, station: event.target.value }))}
          placeholder="Например: 2707 или АЗС №02003"
        />
      </label>
      <label>
        <span>Что исправить</span>
        <input
          value={feedback.field}
          onChange={(event) => onChange((current) => ({ ...current, field: event.target.value }))}
          placeholder="Телефон, адрес, персонал, сервисы..."
        />
      </label>
      <label>
        <span>Комментарий</span>
        <textarea
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

function ReadinessRow({ title, value, total }) {
  const share = pct(value, total);
  return (
    <div className="readiness-row">
      <div>
        <span>{title}</span>
        <strong>{asInt(value)} / {asInt(total)}</strong>
      </div>
      <div className="bar-track">
        <i style={{ width: `${Math.max(4, share)}%` }} />
      </div>
      <small>{share}%</small>
    </div>
  );
}

function Kpi({ title, value, share, tone = "" }) {
  return (
    <div className={`analytics-kpi ${tone}`}>
      <span>{title}</span>
      <strong>{asInt(value)}</strong>
      <small>{share}% выборки</small>
    </div>
  );
}

function ChartCard({ title, items, total, compact = false }) {
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
}

function Metric({ label, value, tone = "" }) {
  return (
    <div className={`metric ${tone}`}>
      <span>{label}</span>
      <strong>{asInt(value)}</strong>
    </div>
  );
}

function FilterRail({ filters, options, setFilter }) {
  return (
    <div className="filter-rail">
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
    </div>
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

function StationList({ stations, selectedId, favorites, density = "comfortable", onSelect, onFavorite, onScroll }) {
  const reduceMotion = useReducedMotion();
  if (!stations.length) {
    return (
      <div className="empty">
        <Filter size={24} />
        <strong>Нет объектов</strong>
        <span>Попробуйте изменить поиск или фильтры.</span>
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
            onClick={(event) => {
              event.stopPropagation();
              onFavorite(station.id);
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

function StationMap({ stations, selected, focusSelected, onSelect }) {
  const reduceMotion = useReducedMotion();
  const mapNodeRef = useRef(null);
  const mapRef = useRef(null);
  const ymapsRef = useRef(null);
  const apiVersionRef = useRef(null);
  const objectManagerRef = useRef(null);
  const markerRefs = useRef([]);
  const userMarkerRef = useRef(null);
  const onSelectRef = useRef(onSelect);
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
    loadYandexMaps(YANDEX_MAPS_API_KEY)
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

  return (
    <motion.div
      className="map-surface"
      initial={reduceMotion ? false : { opacity: 0, y: 12, scale: 0.992 }}
      animate={{ opacity: 1, y: 0, scale: 1 }}
      transition={{ type: "spring", stiffness: 220, damping: 28, mass: 0.9 }}
    >
      <div className="map-header">
        <div>
          <strong>{asInt(points.length)} точек на карте</strong>
          <span>Все объекты с координатами</span>
        </div>
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
                  {mapError.includes("YANDEX_MAPS")
                    ? "Яндекс не отдал JS API. Проверь, что ключ активен для JavaScript API и разрешает текущий адрес приложения."
                    : mapError || "Проверь API-ключ и ограничения HTTP Referer."}
                </span>
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

function StationDetail({ station, favorite, onFavorite, sheetState, onSheetState }) {
  const routeUrl = hasValidPoint(station)
    ? `https://yandex.ru/maps/?rtext=~${station.lat},${station.lon}&rtt=auto`
    : "";
  const phone = bestPhone(station);
  const [dragOffset, setDragOffset] = useState(0);
  const dragStartRef = useRef(null);
  const suppressGrabberClickRef = useRef(false);

  function cycleSheet() {
    if (suppressGrabberClickRef.current) {
      suppressGrabberClickRef.current = false;
      return;
    }

    if (sheetState === "closed" || sheetState === "peek") {
      onSheetState("half");
      return;
    }

    onSheetState(sheetState === "full" ? "half" : "full");
  }

  function beginSheetDrag(clientY) {
    dragStartRef.current = clientY;
    setDragOffset(0);
  }

  function updateSheetDrag(clientY) {
    if (dragStartRef.current == null) return;
    setDragOffset(Math.max(clientY - dragStartRef.current, -80));
  }

  function finishSheetDrag(clientY, scrollTop = 0) {
    if (dragStartRef.current == null) return;
    const delta = clientY - dragStartRef.current;
    dragStartRef.current = null;
    setDragOffset(0);
    if (Math.abs(delta) < 36) return;
    suppressGrabberClickRef.current = true;
    window.setTimeout(() => {
      suppressGrabberClickRef.current = false;
    }, 0);
    if (delta > 0 && scrollTop > 4) return;
    if (delta < 0) {
      onSheetState(sheetState === "closed" || sheetState === "peek" ? "half" : "full");
    } else {
      onSheetState("closed");
    }
  }

  function beginMouseSheetDrag(clientY) {
    beginSheetDrag(clientY);

    function handleMouseMove(event) {
      updateSheetDrag(event.clientY);
    }

    function handleMouseUp(event) {
      window.removeEventListener("mousemove", handleMouseMove);
      finishSheetDrag(event.clientY);
    }

    window.addEventListener("mousemove", handleMouseMove);
    window.addEventListener("mouseup", handleMouseUp, { once: true });
  }

  function handleTouchMove(event) {
    updateSheetDrag(event.touches[0]?.clientY ?? dragStartRef.current ?? 0);
  }

  function handleTouchEnd(event) {
    finishSheetDrag(event.changedTouches[0]?.clientY ?? dragStartRef.current ?? 0, event.currentTarget.scrollTop);
  }

  return (
    <motion.aside
      className={`detail sheet-${sheetState} ${dragOffset ? "dragging" : ""}`}
      style={{ "--sheet-drag": `${dragOffset}px` }}
      initial={{ opacity: sheetState === "closed" ? 0 : 1 }}
      animate={{ opacity: sheetState === "closed" ? 0 : 1 }}
      exit={{ opacity: 0, y: 80 }}
      transition={{ opacity: { duration: 0.18 }, y: { duration: 0.2, ease: "easeOut" } }}
      onTouchStart={(event) => {
        if (event.target.closest("a, button, summary, select, input")) return;
        beginSheetDrag(event.touches[0]?.clientY ?? 0);
      }}
      onTouchMove={handleTouchMove}
      onTouchEnd={handleTouchEnd}
    >
      <button
        className="detail-grabber"
        type="button"
        onClick={cycleSheet}
        onTouchStart={(event) => {
          event.stopPropagation();
          beginSheetDrag(event.touches[0]?.clientY ?? 0);
        }}
        onTouchMove={(event) => {
          event.stopPropagation();
          updateSheetDrag(event.touches[0]?.clientY ?? dragStartRef.current ?? 0);
        }}
        onTouchEnd={(event) => {
          event.stopPropagation();
          finishSheetDrag(event.changedTouches[0]?.clientY ?? dragStartRef.current ?? 0);
        }}
        onPointerDown={(event) => {
          if (event.pointerType === "touch") return;
          event.currentTarget.setPointerCapture?.(event.pointerId);
          beginSheetDrag(event.clientY);
        }}
        onPointerMove={(event) => {
          if (event.pointerType === "touch") return;
          updateSheetDrag(event.clientY);
        }}
        onPointerUp={(event) => {
          if (event.pointerType === "touch") return;
          finishSheetDrag(event.clientY);
        }}
        onMouseDown={(event) => {
          event.preventDefault();
          beginMouseSheetDrag(event.clientY);
        }}
        aria-label="Развернуть карточку"
      />
      <div className="detail-head">
        <div className="detail-title-block">
          <div className="detail-meta-line">
            <span className="eyeless">{station.ksss}</span>
            <span className={`status-chip tone-${statusTone(station.status)}`}>
              <i />
              {shortStatus(station.status)}
            </span>
          </div>
          <h2>{station.name}</h2>
          <p>{station.address || station.subject}</p>
          <div className="detail-quick-facts" aria-label="Краткая информация">
            <span>{station.subject || "Регион не указан"}</span>
            <span>{station.npo || "НПО не указан"}</span>
            <span className={station.qualityIssues.length ? "warn" : "ok"}>
              {station.qualityIssues.length ? `${station.qualityIssues.length} замеч.` : "Данные без критичных замечаний"}
            </span>
          </div>
        </div>
        <button className={`icon-button favorite ${favorite ? "on" : ""}`} type="button" onClick={onFavorite} aria-label="Избранное">
          <Heart size={19} fill="currentColor" />
        </button>
      </div>

      <div className="action-row">
        <a className={`action-button ${phone ? "" : "disabled"}`} href={phone ? `tel:${phone}` : undefined}>
          <Phone size={17} /> Позвонить
        </a>
        <a className={`action-button primary ${routeUrl ? "" : "disabled"}`} href={routeUrl || undefined} target="_blank" rel="noreferrer">
          <Navigation size={17} /> Яндекс маршрут
        </a>
      </div>

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

function StationKpis({ ksss }) {
  const period = useMemo(() => currentMonthPeriod(), []);
  const [kpiState, setKpiState] = useState({ status: "idle", data: null, error: "" });

  useEffect(() => {
    if (!ksss) {
      setKpiState({ status: "no-data", data: null, error: "" });
      return undefined;
    }

    const controller = new AbortController();
    setKpiState({ status: "loading", data: null, error: "" });

    fetch(`/api/stations/${encodeURIComponent(ksss)}/kpis?period=${period}`, {
      signal: controller.signal,
      headers: { Accept: "application/json" },
    })
      .then((response) => {
        if (response.status === 404) return null;
        if (!response.ok) throw new Error(`KPI_REQUEST_FAILED_${response.status}`);
        return response.json();
      })
      .then((data) => {
        if (!data || !Array.isArray(data.metrics) || data.metrics.length === 0) {
          setKpiState({ status: "ready", data: demoKpiPayload(ksss, period, "no-data"), error: "" });
          return;
        }
        setKpiState({ status: "ready", data, error: "" });
      })
      .catch((error) => {
        if (error.name === "AbortError") return;
        setKpiState({ status: "ready", data: demoKpiPayload(ksss, period, error.message), error: "" });
      });

    return () => controller.abort();
  }, [ksss, period]);

  return (
    <section className="detail-section kpi-section">
      <div className="kpi-head">
        <h3>
          <BarChart3 size={16} /> Показатели месяца
        </h3>
        <span>{formatPeriod(period)}</span>
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
        <div className="kpi-message">
          <CircleDot size={16} />
          <span>По этой АЗС пока нет данных за месяц</span>
        </div>
      )}

      {kpiState.status === "ready" && (
        <>
          <div className="kpi-source">
            {kpiState.data.source === "placeholder"
              ? "Заглушка показателей до подключения API"
              : kpiState.data.source === "mock"
                ? "Демо-данные API до подключения SQL"
                : "Данные из БД"}
          </div>
          <div className="kpi-grid">
            {kpiState.data.metrics.map((metric) => (
              <article className="kpi-card" key={metric.id}>
                <span>{metric.label}</span>
                <strong>{formatKpiValue(metric.value, metric.unit)}</strong>
                <div className="kpi-deltas">
                  <small className={deltaTone(metric.momPct)}>MoM {formatDelta(metric.momPct)}</small>
                  <small className={deltaTone(metric.yoyPct)}>YoY {formatDelta(metric.yoyPct)}</small>
                </div>
              </article>
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
      })
      .catch(() => {});

    return () => controller.abort();
  }, []);

  useEffect(() => {
    if (!ksss) {
      setStaffState({ status: "no-data", data: null, error: "" });
      return undefined;
    }

    const controller = new AbortController();
    setStaffState({ status: "loading", data: null, error: "" });

    fetchJson(`/api/stations/${encodeURIComponent(ksss)}/staff?period=${period}`, controller.signal)
      .then((data) => {
        if (!data || !Array.isArray(data.days) || !data.days.length) {
          setStaffState({ status: "ready", data: demoStaffPayload(ksss, period, "no-data"), error: "" });
          return;
        }
        setStaffState({ status: "ready", data, error: "" });
      })
      .catch((error) => {
        if (error.name === "AbortError") return;
        setStaffState({ status: "ready", data: demoStaffPayload(ksss, period, error.message), error: "" });
      });

    return () => controller.abort();
  }, [ksss, period]);

  useEffect(() => {
    if (staffState.status !== "ready") return undefined;

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

  return (
    <section className="detail-section staff-section">
      <div className="kpi-head">
        <h3>
          <Users size={16} /> Персонал
        </h3>
        {periods.length > 1 ? (
          <select className="period-select" value={period} onChange={(event) => setPeriod(event.target.value)} aria-label="Период рекомендаций">
            {periods.map((item) => (
              <option key={item} value={item}>
                {formatPeriod(item)}
              </option>
            ))}
          </select>
        ) : (
          <span>{formatPeriod(period)}</span>
        )}
      </div>

      {staffState.status === "loading" && (
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

      {staffState.status === "error" && (
        <div className="kpi-message warning">
          <AlertTriangle size={16} />
          <span>Данные по персоналу временно недоступны</span>
        </div>
      )}

      {staffState.status === "no-data" && (
        <div className="kpi-message">
          <CircleDot size={16} />
          <span>По этой АЗС пока нет данных по персоналу</span>
        </div>
      )}

      {staffState.status === "ready" && (
        <>
          <div className="kpi-source">
            {staffState.data.source === "placeholder"
              ? "Заглушка персонала до подключения API"
              : staffState.data.source === "mock"
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
        <strong>{name || "—"}</strong>
      </div>
      {phone && <a href={`tel:${phone}`}>{phone}</a>}
    </div>
  );
}

function FilterSheet({ filters, options, setFilter, onClose, onReset }) {
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
        <FilterRail filters={filters} options={options} setFilter={setFilter} />
        <SelectChip label="Качество" value={filters.quality} options={[
          ["issues", "Есть замечания"],
        ]} onChange={(value) => setFilter("quality", value)} />
        <button className="reset-button" onClick={onReset} type="button">Сбросить фильтры</button>
      </div>
    </div>
  );
}

createRoot(document.getElementById("root")).render(<App />);

if ("serviceWorker" in navigator) {
  window.addEventListener("load", () => {
    navigator.serviceWorker.register("/sw.js").catch(() => {});
  });
}
