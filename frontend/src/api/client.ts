/**
 * Thin fetch wrapper for the application API.
 *
 * Every error leaves the backend as `{error: {code, message, details}}`;
 * this module parses that envelope into a typed `ApiError` so components can
 * branch on stable codes (`stale_revision`, `remote_conflict`, ...) instead
 * of raw status text. Upstream (Keitaro/Five) bodies never reach the browser.
 */

const API_BASE = '/api/v1'

export class ApiError extends Error {
  readonly status: number
  readonly code: string
  readonly details: Record<string, unknown>

  constructor(
    status: number,
    code: string,
    message: string,
    details: Record<string, unknown> = {},
  ) {
    super(message)
    this.name = 'ApiError'
    this.status = status
    this.code = code
    this.details = details
  }
}

interface RequestOptions {
  method?: 'GET' | 'POST' | 'PUT' | 'DELETE'
  body?: unknown
  headers?: Record<string, string>
  signal?: AbortSignal
}

interface ErrorEnvelope {
  error?: {
    code?: string
    message?: string
    details?: Record<string, unknown>
  }
}

export async function apiRequest<T>(path: string, options: RequestOptions = {}): Promise<T> {
  const { method = 'GET', body, headers = {}, signal } = options
  let response: Response
  try {
    response = await fetch(`${API_BASE}${path}`, {
      method,
      headers: body !== undefined ? { 'Content-Type': 'application/json', ...headers } : headers,
      body: body !== undefined ? JSON.stringify(body) : undefined,
      signal,
    })
  } catch (cause) {
    if (cause instanceof DOMException && cause.name === 'AbortError') {
      throw cause
    }
    throw new ApiError(
      0,
      'network_error',
      'The server could not be reached. Check your connection and try again.',
    )
  }

  let payload: unknown = null
  try {
    payload = await response.json()
  } catch {
    payload = null
  }

  if (!response.ok) {
    const envelope = (payload as ErrorEnvelope | null)?.error
    throw new ApiError(
      response.status,
      envelope?.code ?? 'http_error',
      envelope?.message ?? `The request failed with status ${response.status}.`,
      envelope?.details ?? {},
    )
  }
  return payload as T
}
