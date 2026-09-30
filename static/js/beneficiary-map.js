(() => {
  const source = document.getElementById("beneficiary-map-data");
  if (!source) return;
  const features = JSON.parse(source.textContent).features;
  const search = document.querySelector("[data-map-search]");
  const status = document.querySelector("[data-map-status]");
  const requestedId = new URLSearchParams(window.location.search).get("record_id");
  const requestedFeature = features.find((feature) => String(feature.properties.record_id) === requestedId);
  if (requestedFeature) search.value = requestedFeature.properties.uid || requestedFeature.properties.name;
  const list = document.querySelector("[data-map-list]");
  const cbfList = document.querySelector("[data-map-cbf-list]");
  const cbfSection = document.querySelector("[data-map-cbf-section]");
  const cbfKey = document.querySelector("[data-map-cbf-key]");
  const cbfToggle = document.querySelector("[data-map-cbf-toggle]");
  const empty = document.querySelector("[data-map-empty]");
  const counts = { validated: 0, failed: 0, unassessed: 0 };
  const trainings = { validated: 0, failed: 0 };
  features.forEach(({ properties: item }) => {
    counts[item.status] += 1;
    if (item.status in trainings) trainings[item.status] += item.training_count;
  });
  document.querySelector("[data-map-total]").textContent = features.length;
  Object.keys(counts).forEach((key) => {
    document.querySelector(`[data-map-${key}]`).textContent = counts[key];
  });
  Object.keys(trainings).forEach((key) => {
    document.querySelector(`[data-map-${key}-trainings]`).textContent = `${trainings[key]} training types received`;
  });

  const colors = { validated: "#23825b", failed: "#c34838", unassessed: "#a07822" };
  let map = null;
  let layer = null;
  let cbfOverlay = null;
  let showCbfs = true;
  const canvas = document.getElementById("beneficiary-map");
  if (window.L) {
    map = L.map(canvas, { scrollWheelZoom: false }).setView([0, 20], 3);
    L.tileLayer("https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png", {
      attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors',
      maxZoom: 19,
    }).addTo(map);
  } else {
    canvas.classList.add("map-unavailable");
    canvas.textContent = "The interactive map could not load. The beneficiary list remains available below.";
  }

  function label(value) {
    return value === null || value === undefined ? "Not assessed" : value ? "Passed" : "Not met";
  }

  function details(item) {
    const wrap = document.createElement("div");
    wrap.className = "map-feature-details";
    const title = document.createElement("strong");
    title.textContent = item.name;
    const info = document.createElement("p");
    info.textContent = `${item.uid || "No ID"} · ${item.village || "Village unknown"} · ${item.date}`;
    const result = document.createElement("p");
    result.textContent = `RVO: ${label(item.rvo_passed)} · Project: ${label(item.project_passed)}${item.outcome ? ` · Outcome ${item.outcome}` : ""}`;
    const training = document.createElement("p");
    training.textContent = `${item.training_count} training types received`;
    const link = document.createElement("a");
    link.href = `/records/${encodeURIComponent(item.record_id)}`;
    link.textContent = "Open beneficiary record";
    wrap.append(title, info, result, training, link);
    return wrap;
  }

  function renderCbfConnections() {
    cbfList.replaceChildren();
    if (!showCbfs) return;
    const groups = new Map();
    if (map && layer) {
      layer.eachLayer((shape) => {
        const name = String(shape.feature.properties.cbf || "").trim();
        if (!name) return;
        const center = shape.getLatLng ? shape.getLatLng() : shape.getBounds().getCenter();
        if (!groups.has(name)) groups.set(name, []);
        groups.get(name).push(center);
      });
    }
    document.querySelector("[data-map-cbf-visible]").textContent = `${groups.size} shown`;
    if (!map || !groups.size) return;
    cbfOverlay = L.layerGroup().addTo(map);
    [...groups].sort(([a], [b]) => a.localeCompare(b)).forEach(([name, centers]) => {
      const average = L.latLng(
        centers.reduce((sum, point) => sum + point.lat, 0) / centers.length,
        centers.reduce((sum, point) => sum + point.lng, 0) / centers.length,
      );
      // Offset the derived group center on screen so a single field still has a visible link.
      const hub = map.unproject(map.project(average).add(L.point(-26, -26)));
      centers.forEach((center) => L.polyline([hub, center], {
        color: "#667d91", weight: 1, opacity: .35, dashArray: "3 5", interactive: false,
      }).addTo(cbfOverlay));
      const marker = L.circleMarker(hub, {
        radius: 8, color: "#fff", weight: 1, fillColor: "#667d91", fillOpacity: .85,
      }).addTo(cbfOverlay);
      const tooltip = document.createElement("span");
      tooltip.textContent = name;
      marker.bindTooltip(tooltip, { direction: "top" });
      const popup = document.createElement("div");
      const title = document.createElement("strong");
      title.textContent = name;
      const description = document.createElement("p");
      description.textContent = `${centers.length} mapped ${centers.length === 1 ? "beneficiary" : "beneficiaries"} · marker shows their group center, not the CBF location`;
      popup.append(title, description);
      marker.bindPopup(popup);

      const button = document.createElement("button");
      button.type = "button";
      button.className = "map-cbf-item";
      const dot = document.createElement("span");
      dot.className = "map-cbf-symbol";
      dot.textContent = "C";
      const text = document.createElement("span");
      text.textContent = `${name} · ${centers.length} ${centers.length === 1 ? "field" : "fields"}`;
      button.append(dot, text);
      button.addEventListener("click", () => {
        map.fitBounds(L.latLngBounds([...centers, hub]).pad(.35), { maxZoom: 16 });
        marker.openPopup();
      });
      cbfList.append(button);
    });
  }

  function render() {
    const query = search.value.trim().toLocaleLowerCase();
    const selected = features.filter(({ properties: item }) =>
      (status.value === "all" || item.status === status.value)
      && [item.name, item.uid, item.village, item.cbf].some((part) =>
        String(part || "").toLocaleLowerCase().includes(query)));
    list.replaceChildren();
    document.querySelector("[data-map-visible]").textContent = `${selected.length} shown`;
    empty.hidden = selected.length > 0;
    if (map) {
      if (cbfOverlay) map.removeLayer(cbfOverlay);
      cbfOverlay = null;
      if (layer) map.removeLayer(layer);
      layer = L.geoJSON({ type: "FeatureCollection", features: selected }, {
        style: (feature) => ({ color: colors[feature.properties.status], weight: 3, fillOpacity: .3 }),
        pointToLayer: (feature, latlng) => L.circleMarker(latlng, {
          radius: 8, color: "#fff", weight: 2, fillColor: colors[feature.properties.status], fillOpacity: 1,
        }),
        onEachFeature: (feature, shape) => shape.bindPopup(details(feature.properties)),
      }).addTo(map);
      if (selected.length) map.fitBounds(layer.getBounds().pad(.15), { maxZoom: 16 });
    }
    renderCbfConnections();
    selected.forEach(({ properties: item }) => {
      const button = document.createElement("button");
      button.type = "button";
      button.className = "map-result";
      const heading = document.createElement("strong");
      heading.textContent = item.name;
      const pill = document.createElement("span");
      pill.className = `map-status is-${item.status}`;
      pill.textContent = item.status === "unassessed" ? "Not assessed" : item.status === "validated" ? "Validated" : "Failed";
      const meta = document.createElement("small");
      meta.textContent = `${item.uid || "No ID"} · ${item.training_count} trainings · ${item.date}`;
      button.append(heading, pill, meta);
      button.addEventListener("click", () => {
        if (!map || !layer) {
          window.location.assign(`/records/${encodeURIComponent(item.record_id)}`);
          return;
        }
        layer.eachLayer((shape) => {
          if (shape.feature.properties.record_id !== item.record_id) return;
          map.fitBounds(shape.getBounds ? shape.getBounds().pad(.4) : L.latLngBounds([shape.getLatLng()]).pad(1), { maxZoom: 17 });
          shape.openPopup();
        });
      });
      list.append(button);
    });
  }
  search.addEventListener("input", render);
  status.addEventListener("change", render);
  cbfToggle.addEventListener("click", () => {
    showCbfs = !showCbfs;
    cbfToggle.textContent = showCbfs ? "Hide CBFs" : "Show CBFs";
    cbfToggle.setAttribute("aria-pressed", String(showCbfs));
    cbfSection.hidden = !showCbfs;
    cbfKey.hidden = !showCbfs;
    if (map && cbfOverlay) map.removeLayer(cbfOverlay);
    cbfOverlay = null;
    if (showCbfs) renderCbfConnections();
  });
  render();
})();
