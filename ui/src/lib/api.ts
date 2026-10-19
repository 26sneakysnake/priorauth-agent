// Typed client for the priorauth API. Types are generated from docs/openapi.json (npm run gen:api); the browser
// calls /api/*, which src/app/api/[...path]/route.ts forwards to the FastAPI server.
import createClient, { type Middleware } from "openapi-fetch";

import { clearToken, getToken } from "./auth";
import type { components, paths } from "./schema";

type Schemas = components["schemas"];
export type CaseReview = Schemas["CaseReview"];
export type CriterionView = Schemas["CriterionView"];
export type EvidenceView = Schemas["EvidenceView"];
export type PageView = Schemas["PageView"];
export type QueueItem = Schemas["QueueItem"];
export type ReasonView = Schemas["ReasonView"];
export type ReviewIn = Schemas["ReviewIn"];
export type ReviewView = Schemas["ReviewView"];
export type Status = Schemas["Status"];

export const STATUSES: readonly Status[] = ["MET", "NOT_MET", "INSUFFICIENT"];

export class Unauthorized extends Error {}

const auth: Middleware = {
  onRequest({ request }) {
    const token = getToken();
    if (token) request.headers.set("Authorization", `Bearer ${token}`);
    return request;
  },
  onResponse({ response }) {
    if (response.status === 401) clearToken();
    return response;
  },
};

export const api = createClient<paths>({ baseUrl: "/api" });
api.use(auth);

/** The detail of a FastAPI error body, as one line. */
export function errorText(error: unknown): string {
  const detail = (error as { detail?: unknown } | undefined)?.detail;
  if (typeof detail === "string") return detail;
  if (Array.isArray(detail)) {
    return detail.map((d) => (d as { msg?: string }).msg ?? JSON.stringify(d)).join("; ");
  }
  return "request failed";
}
