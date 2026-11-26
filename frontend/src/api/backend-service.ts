import { request } from './client'
import { moduleMeta } from '@/data/modules'
import type { ActionResult, EntryRow, OverviewResult, PageResult } from '@/data/types'

export { moduleMeta }

export async function listEntries(
  key: string,
  filters: Record<string, string> = {},
): Promise<PageResult> {
  const query = new URLSearchParams(filters).toString()
  const response = await request(`/api/${key}?${query}`)
  if (!response.ok) throw new Error('业务数据读取失败')
  return await response.json()
}

export async function createEntry(
  key: string,
  values: Record<string, string>,
): Promise<ActionResult> {
  const response = await request(`/api/${key}`, {
    method: 'POST',
    body: JSON.stringify({ values }),
  })
  return await response.json()
}

export async function runAction(
  key: string,
  id: number,
  action: string,
): Promise<ActionResult> {
  const response = await request(`/api/${key}/${id}/actions`, {
    method: 'POST',
    body: JSON.stringify({ action }),
  })
  return await response.json()
}

export async function downloadEntries(
  key: string,
  filters: Record<string, string> = {},
): Promise<void> {
  const query = new URLSearchParams(filters).toString()
  const response = await request(`/api/${key}/export?${query}`)
  if (!response.ok) throw new Error('业务数据导出失败')
  const url = URL.createObjectURL(await response.blob())
  const anchor = document.createElement('a')
  anchor.href = url
  anchor.download = `${key}.csv`
  document.body.appendChild(anchor)
  anchor.click()
  document.body.removeChild(anchor)
  URL.revokeObjectURL(url)
}

export async function loadOverview(): Promise<OverviewResult> {
  const response = await request('/api/overview')
  if (!response.ok) throw new Error('运营概览读取失败')
  return await response.json()
}

export type HttpEnvelope<T = any> = {
  httpStatus: number
  ok: boolean
  body: T
}

async function readEnvelope<T = any>(response: Response): Promise<HttpEnvelope<T>> {
  let body: any = null
  try {
    body = await response.json()
  } catch {
    body = null
  }
  return { httpStatus: response.status, ok: response.ok, body: body as T }
}

export async function fetchContinuation(
  entry: 'legacy' | 'new',
  batch: string,
): Promise<HttpEnvelope> {
  const response = await request(`/api/${entry}/continuation/${encodeURIComponent(batch)}`)
  return readEnvelope(response)
}

export async function postContinuation(
  entry: 'legacy' | 'new',
  batch: string,
  action: 'claim' | 'continue',
  payload: Record<string, unknown> = {},
): Promise<HttpEnvelope> {
  const response = await request(
    `/api/${entry}/continuation/${encodeURIComponent(batch)}/${action}`,
    { method: 'POST', body: JSON.stringify(payload) },
  )
  return readEnvelope(response)
}

export async function migrationApi(
  action: 'start' | 'prepare' | 'switch' | 'rollback' | 'simulate-failure' | 'scenarios/k-l8',
  payload: Record<string, unknown> = {},
): Promise<HttpEnvelope> {
  const response = await request(`/api/migration/${action}`, {
    method: 'POST',
    body: JSON.stringify(payload),
  })
  return readEnvelope(response)
}

export async function migrationStatus(): Promise<any> {
  const response = await request('/api/migration/status')
  if (!response.ok) throw new Error('迁移状态读取失败')
  return response.json()
}

export async function recomputeCompareBaseline(id: number | string): Promise<any> {
  const response = await request(`/api/price_compare/${id}/recompute-baseline`, {
    method: 'POST',
    body: JSON.stringify({}),
  })
  return response.json()
}

export async function registerCompareBasis(
  id: number | string,
  payload: Record<string, unknown>,
): Promise<any> {
  const response = await request(`/api/price_compare/${id}/basis`, {
    method: 'POST',
    body: JSON.stringify(payload),
  })
  return response.json()
}

export async function correctBaseline(
  id: number | string,
  payload: Record<string, unknown>,
): Promise<any> {
  const response = await request(`/api/price_baseline/${id}/corrections`, {
    method: 'POST',
    body: JSON.stringify(payload),
  })
  return response.json()
}
