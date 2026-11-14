(function () {
  const dataUrl = "public_demo_data.json";
  let dataPromise;

  function loadData() {
    if (!dataPromise) {
      dataPromise = fetch(dataUrl).then((response) => {
        if (!response.ok) throw new Error("Unable to load the public NASA sample");
        return response.json();
      });
    }
    return dataPromise;
  }

  function query(path) {
    return new URLSearchParams(path.split("?")[1] || "");
  }

  function filteredRecords(data, params) {
    const sensors = params.getAll("sensors").map((sensor) => sensor.toUpperCase());
    const start = params.get("start");
    const end = params.get("end");
    return data.observations.filter((record) => {
      const day = record.acq_datetime_utc.slice(0, 10);
      return (!sensors.length || sensors.includes(record.source_sensor)) &&
        sensors[0] !== "__NONE__" &&
        (!start || day >= start) &&
        (!end || day <= end);
    });
  }

  function grouped(records) {
    return records.reduce((result, record) => {
      const day = record.acq_datetime_utc.slice(0, 10);
      if (!result[day]) result[day] = { MODIS: 0, VIIRS: 0 };
      result[day][record.source_sensor] += 1;
      return result;
    }, {});
  }

  function csv(records) {
    const fields = ["observation_id", "record_id", "source_sensor", "source_platform", "product", "acq_datetime_utc", "acquisition_datetime_utc", "latitude", "longitude", "confidence", "brightness", "frp", "area_of_interest", "quality_flag", "source", "is_demo"];
    const quote = (value) => `"${String(value ?? "").replaceAll('"', '""')}"`;
    return [fields.join(","), ...records.map((record) => fields.map((field) => quote(record[field])).join(","))].join("\r\n");
  }

  async function request(path) {
    const data = await loadData();
    const params = query(path);
    const records = filteredRecords(data, params);
    const key = path.split("?")[0];
    if (key === "/health") return { status: "ok", data_status: "PUBLIC_DEMO", message: "PUBLIC DEMO — CURATED SAMPLE OF NASA DATA" };
    if (key === "/metadata") {
      return {
        status: "PUBLIC_DEMO", source: data.provenance.source, products: ["MODIS", "VIIRS"],
        freshness: "curated", is_demo: false, datasets: data.provenance.committed_index_counts,
        records_loaded: data.summary.total_records, date_range: data.summary.coverage,
        record_cap: data.summary.total_records, index_ready: false, index_state: "public_sample",
        note: "PUBLIC DEMO — CURATED SAMPLE OF NASA DATA. This is not the complete indexed database.",
        provenance: data.provenance, summary: data.summary,
      };
    }
    if (key === "/methodology") return {
      standardization: "NASA source records retain sensor identity and UTC timestamps.",
      comparison: "MODIS and VIIRS are shown separately and are not equivalent intensity measures.",
      aggregation: "Charts and calendar values are calculated from the curated sample.",
      limitations: "This public demo contains a small genuine NASA sample, not the complete 205-million-record index.",
    };
    if (key === "/observations") {
      const offset = Math.max(0, Number(params.get("offset") || 0));
      const limit = Math.min(10000, Math.max(1, Number(params.get("limit") || 5000)));
      return { status: "PUBLIC_DEMO", is_demo: false, total_records: records.length, offset, limit, records: records.slice(offset, offset + limit) };
    }
    if (key === "/summary") {
      const summary = Object.assign({}, data.summary, { total_records: records.length, by_sensor: { MODIS: records.filter((r) => r.source_sensor === "MODIS").length, VIIRS: records.filter((r) => r.source_sensor === "VIIRS").length } });
      return { status: "PUBLIC_DEMO", ...summary, confidence: { available: records.some((r) => r.confidence != null) }, frp: summary.frp };
    }
    if (key === "/activity-calendar") return { status: "PUBLIC_DEMO", metric: "Sample observation records per UTC day", days: grouped(records) };
    if (key === "/trends") return { status: "PUBLIC_DEMO", metric: "Daily sample observation count", points: Object.entries(grouped(records)).sort().map(([date, counts]) => ({ date, ...counts })) };
    if (key === "/export") return csv(records);
    if (key === "/auth/login") return { access_token: "public-demo-session", token_type: "Bearer", user: { username: "public-demo", role: "viewer", name: "NASA Public Demo", auth_mode: "static-demo" } };
    if (key === "/auth/me") return { authenticated: true, user: { username: "public-demo", role: "viewer", name: "NASA Public Demo", auth_mode: "static-demo" }, auth_mode: "static-demo" };
    if (key === "/auth/logout") return { ok: true };
    throw new Error("Unsupported public demo endpoint: " + key);
  }

  window.AgniWatchPublicDemo = { request };
})();
