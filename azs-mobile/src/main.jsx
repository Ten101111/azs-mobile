import React, { useEffect, useMemo, useRef, useState } from "react";
import { createRoot } from "react-dom/client";
import {
  AlertTriangle,
  BarChart3,
  ChevronDown,
  CircleDot,
  Coffee,
  Filter,
  Heart,
  List,
  LocateFixed,
  Map as MapIcon,
  Navigation,
  Phone,
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

const YANDEX_MAPS_API_KEY = import.meta.env.VITE_YANDEX_MAPS_API_KEY || "";
const YANDEX_MAPS_MARKER_LIMIT = 900;
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
  const [payload, setPayload] = useState({ meta: null, stations: [] });
  const [query, setQuery] = useState("");
  const [filters, setFilters] = useState(defaultFilters);
  const [mode, setMode] = useState("list");
  const [selectedId, setSelectedId] = useState("");
  const [selectionMode, setSelectionMode] = useState("auto");
  const [favorites, setFavorites] = useState(() => JSON.parse(localStorage.getItem("azs:favorites") || "[]"));
  const [showFilters, setShowFilters] = useState(false);
  const [detailSheet, setDetailSheet] = useState("half");

  useEffect(() => {
    fetch("/stations.json")
      .then((response) => (response.ok ? response : fetch("/stations.sample.json")))
      .then((response) => response.json())
      .then((data) => {
        setPayload(data);
        setSelectedId(data.stations.find(hasValidPoint)?.id || "");
      });
  }, []);

  useEffect(() => {
    localStorage.setItem("azs:favorites", JSON.stringify(favorites));
  }, [favorites]);

  useEffect(() => {
    setSelectionMode("auto");
    setSelectedId("");
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

  const selected = filtered.find((station) => station.id === selectedId) || filtered[0] || stations[0];

  const metrics = useMemo(() => {
    const active = stations.filter((station) => station.flags.active).length;
    const cafe = stations.filter((station) => station.flags.hasCafe).length;
    const toilet = stations.filter((station) => station.flags.hasToilet).length;
    return { active, cafe, toilet };
  }, [stations]);

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

  function changeMode(nextMode) {
    setMode(nextMode);
    if (nextMode === "map") setDetailSheet("peek");
    if (nextMode === "list") setDetailSheet("half");
  }

  return (
    <main className={`app-shell ${mode}-mode`}>
      <section className="workspace">
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

        <div className="mode-row">
          <button className={mode === "list" ? "active" : ""} onClick={() => changeMode("list")} type="button">
            <List size={16} /> Список
          </button>
          <button className={mode === "map" ? "active" : ""} onClick={() => changeMode("map")} type="button">
            <MapIcon size={16} /> Карта
          </button>
          <button className={mode === "analytics" ? "active" : ""} onClick={() => changeMode("analytics")} type="button">
            <BarChart3 size={16} /> Аналитика
          </button>
          <button className={mode === "quality" ? "active" : ""} onClick={() => changeMode("quality")} type="button">
            <ShieldCheck size={16} /> Контроль
          </button>
        </div>

        <MetricStrip count={filtered.length} metrics={metrics} />

        <FilterRail filters={filters} options={options} setFilter={setFilter} />

        {mode === "analytics" ? (
          <AnalyticsDashboard
            stations={filtered}
            totalStations={stations}
            onFilter={setFilter}
            onOpenList={() => changeMode("list")}
          />
        ) : mode === "quality" ? (
          <ControlDashboard
            stations={filtered}
            totalStations={stations}
            onFilter={setFilter}
            onOpenList={() => changeMode("list")}
            onOpenStation={(id) => {
              selectStation(id);
              changeMode("list");
            }}
          />
        ) : (
          <div className="content-grid">
            <section className={`list-pane ${mode === "map" ? "mobile-hidden" : ""}`}>
              <div className="pane-title">
                <span>{asInt(filtered.length)} найдено</span>
                <button type="button" onClick={() => setFilters(defaultFilters)}>Сбросить</button>
              </div>
              <StationList
                stations={filtered}
                selectedId={selected?.id}
                favorites={favorites}
                onSelect={selectStation}
                onFavorite={toggleFavorite}
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
          </div>
        )}
      </section>

      {selected && (
        <StationDetail
          station={selected}
          favorite={favorites.includes(selected.id)}
          onFavorite={() => toggleFavorite(selected.id)}
          sheetState={detailSheet}
          onSheetState={setDetailSheet}
        />
      )}

      {showFilters && (
        <FilterSheet
          filters={filters}
          options={options}
          setFilter={setFilter}
          onClose={() => setShowFilters(false)}
          onReset={() => setFilters(defaultFilters)}
        />
      )}
    </main>
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

function AnalyticsDashboard({ stations, totalStations, onFilter, onOpenList }) {
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
    <section className="analytics-pane">
      <div className="analytics-head">
        <div>
          <h2>Аналитика сети</h2>
          <p>
            Показатели пересчитываются по текущей выборке: {asInt(total)} из {asInt(totalStations.length)} объектов.
          </p>
        </div>
        <button
          className="quality-button"
          type="button"
          onClick={() => {
            onFilter("quality", "issues");
            onOpenList();
          }}
        >
          <AlertTriangle size={16} /> Объекты с замечаниями
        </button>
      </div>

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
    </section>
  );
}

function ControlDashboard({ stations, totalStations, onFilter, onOpenList, onOpenStation }) {
  const [copied, setCopied] = useState(false);
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

  return (
    <section className="analytics-pane control-pane">
      <div className="analytics-head">
        <div>
          <h2>Контроль объектов</h2>
          <p>
            Рабочий срез для выезда: карточка, контакты, координаты, сервисы и готовность данных по текущей выборке.
          </p>
        </div>
        <button
          className="quality-button"
          type="button"
          onClick={() => {
            onFilter("quality", "issues");
            onOpenList();
          }}
        >
          <AlertTriangle size={16} /> Открыть проблемные
        </button>
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
  const max = Math.max(...items.map((item) => item.value), 1);
  return (
    <div className={`analytics-card ${compact ? "compact" : ""}`}>
      <h3>{title}</h3>
      <div className="bar-list">
        {items.map((item) => (
          <div className="bar-row" key={item.name}>
            <div className="bar-label">
              <span>{item.name}</span>
              <strong>{asInt(item.value)}</strong>
            </div>
            <div className="bar-track">
              <i style={{ width: `${Math.max(4, (item.value / max) * 100)}%` }} />
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

function StationList({ stations, selectedId, favorites, onSelect, onFavorite }) {
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
    <div className="station-list">
      {stations.slice(0, 350).map((station) => (
        <button
          className={`station-row ${selectedId === station.id ? "selected" : ""}`}
          key={station.id}
          type="button"
          onClick={() => onSelect(station.id)}
        >
          <span className="status-dot" style={{ background: statusColors[station.status] || "#8f99a8" }} />
          <span className="row-main">
            <span className="row-title">
              {station.name || `АЗС № ${station.stationNumber}`}
              <small>{station.ksss}</small>
            </span>
            <span className="row-address">{station.address || station.subject}</span>
            <span className="badges">
              <Badge>{shortStatus(station.status)}</Badge>
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
        </button>
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
  const visiblePoints = useMemo(() => points.slice(0, YANDEX_MAPS_MARKER_LIMIT), [points]);
  const hiddenPointCount = Math.max(points.length - visiblePoints.length, 0);

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
      markerRefs.current = visiblePoints.map((station) => createStationMarker(ymapsRef.current, station, selected?.id, onSelectRef.current));
      markerRefs.current.forEach((marker) => mapRef.current.addChild(marker));
      return;
    }

    if (!objectManagerRef.current) return;
    objectManagerRef.current.removeAll();
    objectManagerRef.current.add({
      type: "FeatureCollection",
      features: visiblePoints.map((station) => stationFeature(station, selected?.id)),
    });
  }, [visiblePoints, selected?.id, mapStatus]);

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
    setGeoStatus("locating");
    setGeoMessage("Определяем местоположение через Яндекс...");

    try {
      const nextLocation = await getYandexLocation(ymapsRef.current, apiVersionRef.current);
      setUserLocation(nextLocation);
      setGeoStatus("found");
      setGeoMessage(formatLocationMessage(nextLocation));
      return;
    } catch (yandexError) {
      try {
        const nextLocation = await getBrowserLocation();
        setUserLocation(nextLocation);
        setGeoStatus("found");
        setGeoMessage(formatLocationMessage(nextLocation));
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
    <div className="map-surface">
      <div className="map-header">
        <div>
          <strong>{asInt(points.length)} точек на карте</strong>
          <span>
            {hiddenPointCount > 0
              ? `Показано ${asInt(visiblePoints.length)} из ${asInt(points.length)}`
              : "Все объекты с координатами"}
          </span>
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
    </div>
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
    <aside
      className={`detail sheet-${sheetState} ${dragOffset ? "dragging" : ""}`}
      style={{ "--sheet-drag": `${dragOffset}px` }}
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
        <div>
          <span className="eyeless">{station.ksss}</span>
          <h2>{station.name}</h2>
          <p>{station.address || station.subject}</p>
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
    </aside>
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
          setKpiState({ status: "no-data", data: null, error: "" });
          return;
        }
        setKpiState({ status: "ready", data, error: "" });
      })
      .catch((error) => {
        if (error.name === "AbortError") return;
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
            {kpiState.data.source === "mock" ? "Демо-данные до подключения SQL" : "Данные из БД"}
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
