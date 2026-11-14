(function () {
  const localHost = ["localhost", "127.0.0.1", "::1"].includes(window.location.hostname);
  const defaultBaseUrl = localHost
    ? "http://127.0.0.1:8000/api"
    : window.location.protocol === "http:" || window.location.protocol === "https:"
      ? window.location.origin + "/api"
      : "http://127.0.0.1:8000/api";
  const baseUrl = (window.AGNIWATCH_API_BASE || defaultBaseUrl).replace(/\/$/, "");
  const defaultTimeouts = {
    "/health": 5000,
    "/metadata": 15000,
    "/methodology": 10000,
    "/observations": 30000,
    "/summary": 60000,
    "/activity-calendar": 60000,
    "/trends": 60000,
    "/export": 120000,
    "/auth/login": 10000,
    "/auth/logout": 10000,
    "/auth/me": 10000,
  };
  const configuredTimeouts = window.AGNIWATCH_API_TIMEOUTS || {};

  function timeoutFor(path) {
    const key = path.split("?")[0];
    const configured = Number(configuredTimeouts[key]);
    return Number.isFinite(configured) && configured > 0 ? configured : defaultTimeouts[key] || 10000;
  }

  class ApiError extends Error {
    constructor(message, status, payload) {
      super(message);
      this.name = "ApiError";
      this.status = status;
      this.payload = payload;
    }
  }

  async function request(path, options = {}) {
    if (window.AGNIWATCH_PUBLIC_DEMO && window.AgniWatchPublicDemo) {
      return window.AgniWatchPublicDemo.request(path, options);
    }
    const { timeoutMs: requestedTimeout, ...fetchOptions } = options;
    const timeoutMs = requestedTimeout || timeoutFor(path);
    const controller = new AbortController();
    const timeout = window.setTimeout(() => controller.abort(), timeoutMs);
    const headers = new Headers(fetchOptions.headers || {});
    const token = window.localStorage.getItem("agniwatch_token");
    if (token) headers.set("Authorization", "Bearer " + token);
    if (fetchOptions.body && !headers.has("Content-Type")) headers.set("Content-Type", "application/json");
    try {
      const response = await fetch(baseUrl + path, { ...fetchOptions, headers, signal: controller.signal });
      const contentType = response.headers.get("content-type") || "";
      const payload = contentType.includes("json") ? await response.json() : await response.text();
      if (!response.ok) {
        const detail = payload && typeof payload === "object" ? payload.detail : "";
        throw new ApiError(detail || "Request failed (" + response.status + ")", response.status, payload);
      }
      return payload;
    } catch (error) {
      if (error.name === "AbortError") throw new ApiError("Request timed out after " + timeoutMs / 1000 + " seconds", 408);
      if (error instanceof ApiError) throw error;
      throw new ApiError("Unable to reach the AgniWatch API", 0, error);
    } finally {
      window.clearTimeout(timeout);
    }
  }

  window.AgniWatchApi = {
    baseUrl,
    get: (path) => request(path),
    post: (path, body) => request(path, { method: "POST", body: JSON.stringify(body) }),
    health: () => request("/health"),
    metadata: () => request("/metadata"),
    login: (credentials) => request("/auth/login", { method: "POST", body: JSON.stringify(credentials) }),
    logout: (token) => request("/auth/logout?session_token=" + encodeURIComponent(token), { method: "POST" }),
    me: (token) => request("/auth/me?session_token=" + encodeURIComponent(token)),
    request,
  };
})();
