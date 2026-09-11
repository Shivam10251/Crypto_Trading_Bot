// Single place where the frontend talks to the backend.
// Components never call fetch directly, so error handling and the base URL
// stay in one place.

import type { SystemStatus } from "./types";

const API_BASE = "/api/v1";

export class ApiError extends Error {
  constructor(
    message: string,
    readonly status?: number,
    options?: { cause?: unknown },
  ) {
    super(message, options);
    this.name = "ApiError";
  }
}

async function getJson<T>(path: string): Promise<T> {
  let response: Response;
  try {
    response = await fetch(`${API_BASE}${path}`);
  } catch (cause) {
    // Backend not running, or the network is down.
    throw new ApiError(`cannot reach backend at ${API_BASE}${path}`, undefined, { cause });
  }
  if (!response.ok) {
    throw new ApiError(`${path} returned HTTP ${response.status}`, response.status);
  }
  return (await response.json()) as T;
}

export function fetchSystemStatus(): Promise<SystemStatus> {
  return getJson<SystemStatus>("/system-status");
}
