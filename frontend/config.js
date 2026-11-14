// The static site defaults to the backend-free evaluator demo.
window.AGNIWATCH_FRONTEND_MODE = window.AGNIWATCH_FRONTEND_MODE || "PUBLIC_DEMO";
window.AGNIWATCH_PUBLIC_DEMO = window.AGNIWATCH_FRONTEND_MODE === "PUBLIC_DEMO";

// For API mode, set this to the deployed FastAPI URL, including /api.
window.AGNIWATCH_API_BASE = window.AGNIWATCH_API_BASE || "";

// Override only when the deployed backend needs different endpoint budgets.
window.AGNIWATCH_API_TIMEOUTS = window.AGNIWATCH_API_TIMEOUTS || {};
