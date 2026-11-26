/**
 * Programmable fetch mock for component and API-client tests.
 *
 * Routes match on method plus a `:param`-segment path pattern and receive the
 * parsed JSON body, so tests can assert exactly what the UI sent (idempotency
 * headers, draft revisions, command payloads) and script the response.
 */

import { vi } from 'vitest'

export interface MockContext {
  url: URL
  init: RequestInit
  body: unknown
}

type Handler = (context: MockContext) => Response | Promise<Response>

export interface MockRoute {
  method: 'GET' | 'POST' | 'PUT' | 'DELETE'
  path: string
  handler: Handler
}

export function jsonResponse(body: unknown, status = 200): Response {
  return {
    ok: status >= 200 && status < 300,
    status,
    json: () => Promise.resolve(body),
  } as Response
}

export function errorResponse(
  status: number,
  code: string,
  message: string,
  details: Record<string, unknown> = {},
): Response {
  return jsonResponse({ error: { code, message, details } }, status)
}

export function headerValue(init: RequestInit, name: string): string | undefined {
  const headers = init.headers as Record<string, string> | undefined
  return headers?.[name]
}

function matchPath(pattern: string, pathname: string): boolean {
  const patternParts = pattern.split('/').filter(Boolean)
  const pathParts = pathname.split('/').filter(Boolean)
  if (patternParts.length !== pathParts.length) {
    return false
  }
  return patternParts.every((part, index) => part.startsWith(':') || part === pathParts[index])
}

export function mockApi(routes: MockRoute[]) {
  const mock = vi.fn(async (input: RequestInfo | URL, init: RequestInit = {}) => {
    const url = new URL(String(input), 'http://localhost')
    const method = init.method ?? 'GET'
    const body = typeof init.body === 'string' ? (JSON.parse(init.body) as unknown) : undefined
    for (const route of routes) {
      if (route.method === method && matchPath(route.path, url.pathname)) {
        return route.handler({ url, init, body })
      }
    }
    throw new Error(`Unexpected request: ${method} ${url.pathname}${url.search}`)
  })
  vi.stubGlobal('fetch', mock)
  return mock
}

export interface Deferred<T> {
  promise: Promise<T>
  resolve: (value: T) => void
  reject: (reason: unknown) => void
}

export function deferred<T>(): Deferred<T> {
  let resolve!: (value: T) => void
  let reject!: (reason: unknown) => void
  const promise = new Promise<T>((res, rej) => {
    resolve = res
    reject = rej
  })
  return { promise, resolve, reject }
}
