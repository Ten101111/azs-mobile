import React, { useEffect, useMemo, useRef, useState } from "react";
import { createRoot } from "react-dom/client";
import L from "leaflet";
import "leaflet/dist/leaflet.css";
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
  MapPin,
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
  return station.lat && station.lon && station.lat !== 0 && station.lon !== 0;
}

function toLatLng(station) {
  return [Number(station.lat), Number(station.lon)];
}

function leafletBounds(points) {
  if (!points.length) return null;
  return L.latLngBounds(points.map(toLatLng));
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

function loadYandexMaps(apiKey) {
  if (!apiKey) {
    return Promise.reject(new Error("YANDEX_MAPS_API_KEY_MISSING"));
  }

  if (window.ymaps3) {
    return window.ymaps3.ready.then(() => window.ymaps3);
  }

  if (yandexMapsPromise) return yandexMapsPromise;

  yandexMapsPromise = new Promise((resolve, reject) => {
    const existingScript = document.querySelector("script[data-yandex-maps-api]");
    const timeoutId = window.setTimeout(() => reject(new Error("YANDEX_MAPS_SCRIPT_TIMEOUT")), 12000);

    function handleReady() {
      window.clearTimeout(timeoutId);
      if (!window.ymaps3) {
        reject(new Error("YANDEX_MAPS_API_NOT_AVAILABLE"));
        return;
      }

      window.ymaps3.ready.then(() => resolve(window.ymaps3)).catch(reject);
    }

    if (existingScript) {
      existingScript.addEventListener("load", handleReady, { once: true });
      existingScript.addEventListener(
        "error",
        () => {
          window.clearTimeout(timeoutId);
          reject(new Error("YANDEX_MAPS_SCRIPT_FAILED"));
        },
        { once: true },
      );
      return;
    }

    const script = document.createElement("script");
    script.src = `https://api-maps.yandex.ru/v3/?apikey=${encodeURIComponent(apiKey)}&lang=ru_RU`;
    script.async = true;
    script.dataset.yandexMapsApi = "true";
    script.addEventListener("load", handleReady, { once: true });
    script.addEventListener(
      "error",
      () => {
        window.clearTimeout(timeoutId);
        reject(new Error("YANDEX_MAPS_SCRIPT_FAILED"));
      },
      { once: true },
    );
    document.head.appendChild(script);
  });

  return yandexMapsPromise;
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

function leafletStationIcon(station, selectedId) {
  const color = selectedId === station.id ? "#c91d32" : statusColors[station.status] || "#8f99a8";
  return L.divIcon({
    className: "",
    html: `<span class="ymap-marker ${selectedId === station.id ? "selected" : ""}" style="--marker-color:${color}"></span>`,
    iconSize: selectedId === station.id ? [22, 22] : [14, 14],
    iconAnchor: selectedId === station.id ? [11, 11] : [7, 7],
  });
}

function leafletUserIcon() {
  return L.divIcon({
    className: "",
    html: '<span class="ymap-user-marker"></span>',
    iconSize: [20, 20],
    iconAnchor: [10, 10],
  });
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
        setSelectedId(data.stations[0]?.id || "");
      });
  }, []);

  useEffect(() => {
    localStorage.setItem("azs:favorites", JSON.stringify(favorites));
  }, [favorites]);

  useEffect(() => {
    setSelectionMode("auto");
    setSelectedId("");
  }, [query, filters.npo, filters.subject, filters.status, filters.type, filters.location, filters.service, filters.quality]);

  const stations = payload.stations;
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
      if (filters.quality === "noCoords" && hasValidPoint(station)) return false;
      return true;
    });
  }, [stations, query, filters]);

  const selected = filtered.find((station) => station.id === selectedId) || filtered[0] || stations[0];

  const metrics = useMemo(() => {
    const active = stations.filter((station) => station.flags.active).length;
    const noCoords = stations.filter((station) => !hasValidPoint(station)).length;
    const cafe = stations.filter((station) => station.flags.hasCafe).length;
    return { active, noCoords, cafe };
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
            <p>{payload.meta ? `${payload.meta.source} · ${asInt(payload.meta.count)} объектов` : "Загрузка данных"}</p>
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
                isVisible={mode === "map"}
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
      <Metric label="Без координат" value={metrics.noCoords} tone="amber" />
    </div>
  );
}

function AnalyticsDashboard({ stations, totalStations, onFilter, onOpenList }) {
  const total = stations.length;
  const active = stations.filter((station) => station.flags.active).length;
  const invalidCoords = stations.filter((station) => !hasValidPoint(station)).length;
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
        <Kpi title="Без координат" value={invalidCoords} share={pct(invalidCoords, total)} tone="amber" />
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
              onFilter("quality", "noCoords");
              onOpenList();
            }}
          >
            <MapPin size={16} />
            <span>Без координат</span>
            <strong>{asInt(invalidCoords)}</strong>
          </button>
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
          <p>Эти фильтры можно использовать как рабочий список для чистки данных перед подключением полноценной карты.</p>
        </div>
      </div>
    </section>
  );
}

function ControlDashboard({ stations, totalStations, onFilter, onOpenList, onOpenStation }) {
  const [copied, setCopied] = useState(false);
  const total = stations.length;
  const issueStations = stations.filter((station) => station.qualityIssues.length > 0);
  const noCoords = stations.filter((station) => !hasValidPoint(station));
  const noResponsible = stations.filter(missingResponsible);
  const noPhone = stations.filter(missingContactPhone);
  const issueCounts = groupTop(
    stations.flatMap((station) => station.qualityIssues).map((issue) => ({ issue })),
    (item) => item.issue,
    8,
  );
  const contactReady = stations.filter((station) => !missingResponsible(station) && !missingContactPhone(station)).length;
  const serviceReady = stations.filter((station) => station.flags.hasShop || station.flags.hasCafe || station.flags.hasToilet).length;
  const mapReady = total - noCoords.length;

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
        <Kpi title="Без координат" value={noCoords.length} share={pct(noCoords.length, total)} tone="amber" />
        <Kpi title="Без телефона" value={noPhone.length} share={pct(noPhone.length, total)} tone="red" />
      </div>

      <div className="control-grid">
        <div className="analytics-card readiness-card">
          <h3>Операционная готовность</h3>
          <ReadinessRow title="Карта" value={mapReady} total={total} />
          <ReadinessRow title="Контакты" value={Math.max(contactReady, 0)} total={total} />
          <ReadinessRow title="Сервисный профиль" value={serviceReady} total={total} />
        </div>

        <div className="analytics-card quality-list-card">
          <h3>Основные разрывы</h3>
          <button type="button" onClick={() => onFilter("quality", "noCoords")}>
            <MapPin size={16} />
            <span>Без координат</span>
            <strong>{asInt(noCoords.length)}</strong>
          </button>
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

function StationMap({ stations, selected, isVisible, focusSelected, onSelect }) {
  const mapNodeRef = useRef(null);
  const mapRef = useRef(null);
  const markerLayerRef = useRef(null);
  const markerRefs = useRef([]);
  const userMarkerRef = useRef(null);
  const onSelectRef = useRef(onSelect);
  const [mapStatus, setMapStatus] = useState("loading");
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

    setMapStatus("loading");
    try {
      if (!mapNodeRef.current) return undefined;

      const map = L.map(mapNodeRef.current, {
        zoomControl: true,
        attributionControl: true,
      }).setView([55.7558, 37.6176], 4);

      L.tileLayer("https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png", {
        maxZoom: 19,
        attribution: "&copy; OpenStreetMap",
      }).addTo(map);

      const markerLayer = L.layerGroup().addTo(map);
      const bounds = leafletBounds(points);
      if (bounds?.isValid()) {
        map.fitBounds(bounds, { padding: [28, 28], maxZoom: 12 });
      }

      if (cancelled) {
        map.remove();
        return undefined;
      }

      mapRef.current = map;
      markerLayerRef.current = markerLayer;
      setMapStatus("ready");
      window.requestAnimationFrame(() => {
        map.invalidateSize();
        window.setTimeout(() => map.invalidateSize(), 250);
      });
    } catch (error) {
      if (cancelled) return undefined;
      setMapError(error.message);
      setMapStatus("error");
    }

    return () => {
      cancelled = true;
      markerRefs.current = [];
      userMarkerRef.current = null;
      if (mapRef.current) {
        mapRef.current.remove();
        mapRef.current = null;
      }
      markerLayerRef.current = null;
    };
  }, []);

  useEffect(() => {
    if (!mapRef.current || !isVisible) return;
    window.requestAnimationFrame(() => {
      mapRef.current?.invalidateSize();
      window.setTimeout(() => mapRef.current?.invalidateSize(), 250);
    });
  }, [isVisible]);

  useEffect(() => {
    if (!mapRef.current) return;
    if (focusSelected && selected && hasValidPoint(selected)) {
      mapRef.current.setView(toLatLng(selected), 13, { animate: true });
      return;
    }

    const bounds = leafletBounds(points);
    if (bounds?.isValid()) {
      mapRef.current.fitBounds(bounds, { padding: [28, 28], maxZoom: 12, animate: true });
    }
  }, [points, selected, focusSelected]);

  useEffect(() => {
    if (!mapRef.current || !markerLayerRef.current) return;

    markerLayerRef.current.clearLayers();
    markerRefs.current = visiblePoints.map((station) => {
      const marker = L.marker(toLatLng(station), {
        icon: leafletStationIcon(station, selected?.id),
        title: `${station.name || station.stationNumber} · ${station.subject || ""}`,
      });
      marker.on("click", () => onSelectRef.current(station.id));
      marker.addTo(markerLayerRef.current);
      return marker;
    });
  }, [visiblePoints, selected?.id, mapStatus]);

  useEffect(() => {
    if (!mapRef.current || !userLocation) return;

    const coords = [userLocation.lat, userLocation.lon];

    if (userMarkerRef.current) {
      mapRef.current.removeLayer(userMarkerRef.current);
    }

    userMarkerRef.current = L.marker(coords, {
      icon: leafletUserIcon(),
      title: `Вы здесь · ${formatAccuracy(userLocation.accuracy)}`,
    }).addTo(mapRef.current);
    mapRef.current.setView(coords, 15, { animate: true });
  }, [userLocation, mapStatus]);

  function locateUser() {
    if (!navigator.geolocation || !window.isSecureContext) {
      setGeoStatus("error");
      setGeoMessage(geolocationErrorMessage());
      return;
    }

    setGeoStatus("locating");
    setGeoMessage("Определяем местоположение...");

    navigator.geolocation.getCurrentPosition(
      (position) => {
        const nextLocation = {
          lat: position.coords.latitude,
          lon: position.coords.longitude,
          accuracy: position.coords.accuracy,
        };
        setUserLocation(nextLocation);
        setGeoStatus("found");
        setGeoMessage(formatAccuracy(nextLocation.accuracy) || "Местоположение найдено.");
      },
      (error) => {
        setGeoStatus("error");
        setGeoMessage(geolocationErrorMessage(error));
      },
      {
        enableHighAccuracy: true,
        timeout: 12000,
        maximumAge: 30000,
      },
    );
  }

  return (
    <div className="map-surface">
      <div className="map-header">
        <div>
          <strong>{asInt(points.length)} точек на карте</strong>
          <span>
            {asInt(stations.length - points.length)} без координат
            {hiddenPointCount > 0 ? ` · показано ${asInt(visiblePoints.length)}` : ""}
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
            {mapStatus === "error" ? (
              <>
                <strong>Карта не загрузилась</strong>
                <span>{mapError || "Не удалось инициализировать OpenStreetMap/Leaflet."}</span>
              </>
            ) : (
              <>
                <strong>Загрузка карты</strong>
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
  const [touchStart, setTouchStart] = useState(null);

  function cycleSheet() {
    onSheetState(sheetState === "full" ? "half" : "full");
  }

  function handleTouchEnd(event) {
    if (touchStart == null) return;
    const endY = event.changedTouches[0]?.clientY ?? touchStart;
    const delta = endY - touchStart;
    setTouchStart(null);
    if (Math.abs(delta) < 42) return;
    if (delta > 0 && event.currentTarget.scrollTop > 4) return;
    if (delta < 0) {
      onSheetState(sheetState === "closed" || sheetState === "peek" ? "half" : "full");
    } else {
      onSheetState("closed");
    }
  }

  return (
    <aside
      className={`detail sheet-${sheetState}`}
      onTouchStart={(event) => setTouchStart(event.touches[0]?.clientY ?? null)}
      onTouchEnd={handleTouchEnd}
    >
      <button className="detail-grabber" type="button" onClick={cycleSheet} aria-label="Развернуть карточку" />
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
          <Navigation size={17} /> Маршрут
        </a>
      </div>

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
          ["noCoords", "Без координат"],
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
