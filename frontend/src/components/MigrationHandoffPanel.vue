<template>
  <section class="handoff-panel" :data-module="moduleKey" data-testid="migration-handoff-panel">
    <header class="handoff-head">
      <div>
        <h3>SQLite 迁移与新旧入口续办清册</h3>
        <p>
          原子迁移基线与未办比价；同一批次只有一份受理回执。价格版本、规格、币种待核对项和来源会绑定到对应任务。
        </p>
      </div>
      <span class="entry-badge" data-testid="active-entry">当前入口：{{ activeEntryText }}</span>
    </header>

    <div class="handoff-controls">
      <label class="batch-input">
        <span>比价批次/轮次</span>
        <input v-model="batch" data-testid="continuation-batch" placeholder="例如 K" @keyup.enter="refreshReceipts" />
      </label>
      <button class="btn" type="button" data-testid="refresh-receipts" @click="refreshReceipts">
        取两个入口续办回执
      </button>
      <button class="btn primary" type="button" data-testid="start-migration" :disabled="busy" @click="startMigration">
        启动迁移并切换新库
      </button>
      <button class="btn" type="button" data-testid="prepare-migration" :disabled="busy" @click="prepareMigration">
        仅原子迁移核对
      </button>
      <button class="btn" type="button" data-testid="switch-new" :disabled="busy" @click="switchNew">
        切换到新入口
      </button>
      <button class="btn ghost" type="button" data-testid="simulate-failback" :disabled="busy" @click="simulateFailback">
        模拟切换故障并回滚旧入口
      </button>
      <button class="btn ghost" type="button" data-testid="load-k-l8-drill" :disabled="busy" @click="loadKl8Drill">
        构造 K/L8 演练（两规格+一未办）
      </button>
    </div>

    <div v-if="status" class="migration-summary" data-testid="migration-summary">
      <span>阶段：<strong>{{ status.state?.phase }}</strong></span>
      <span>旧基线/新库：{{ status.counts?.source_price_baseline }} / {{ status.counts?.target_baseline }}</span>
      <span>未办来源/任务表：{{ status.counts?.source_unfinished_compare }} / {{ status.counts?.target_compare_task }}</span>
      <span>受理：{{ status.counts?.claims }}</span>
      <span>有效结果：{{ status.counts?.results }}</span>
      <span class="warning">缺币种待核对：{{ status.missing_currency_count ?? currencyPending }}</span>
    </div>

    <div class="basis-editor">
      <label for="basis-payload">继续时登记的计价依据（JSON，可留空）</label>
      <textarea
        id="basis-payload"
        v-model="basisText"
        data-testid="basis-payload"
        rows="3"
        spellcheck="false"
      />
      <div class="handoff-controls compact">
        <button class="btn primary" type="button" data-testid="legacy-continue" @click="continueBatch('legacy')">
          从旧入口继续同一批次
        </button>
        <button class="btn primary" type="button" data-testid="new-continue" @click="continueBatch('new')">
          从新入口领取/继续
        </button>
        <button class="btn" type="button" data-testid="legacy-claim" @click="claimBatch('legacy')">
          旧入口领取
        </button>
        <button class="btn" type="button" data-testid="new-claim" @click="claimBatch('new')">
          新入口领取
        </button>
      </div>
    </div>

    <p v-if="notice" class="handoff-notice" data-testid="handoff-notice">{{ notice }}</p>
    <p v-if="errorMessage" class="error-text" data-testid="handoff-error">{{ errorMessage }}</p>

    <div class="receipt-grid">
      <article
        v-for="envelope in envelopes"
        :key="envelope.side"
        class="receipt-card"
        :data-testid="`${envelope.side}-receipt-card`"
        :data-kind="envelope.body?.receipt?.kind || 'error'"
      >
        <header>
          <h4>{{ envelope.side === 'legacy' ? '旧入口回执' : '新入口回执' }}</h4>
          <span :data-testid="`${envelope.side}-http-status`">HTTP {{ envelope.httpStatus ?? '未请求' }}</span>
        </header>
        <template v-if="envelope.body?.receipt">
          <div class="receipt-kind" :data-testid="`${envelope.side}-receipt-kind`">
            {{ envelope.body.receipt.kind === 'confirmed_result' ? '已确认结果（不可重复领取）' : '可操作待办' }}
          </div>
          <p class="explanation" :data-testid="`${envelope.side}-explanation`">
            {{ envelope.body.receipt.trace?.explanation }}
          </p>
          <dl class="receipt-meta">
            <div><dt>受理回执</dt><dd>{{ envelope.body.receipt.claim?.receipt_no || '无' }}</dd></div>
            <div><dt>原始受理侧</dt><dd>{{ sideText(envelope.body.receipt.trace?.original_owner_side) }}</dd></div>
            <div><dt>当前执行侧</dt><dd>{{ sideText(envelope.body.receipt.trace?.execution_side) }}</dd></div>
            <div><dt>绑定规格</dt><dd>{{ envelope.body.receipt.result?.spec || envelope.body.receipt.bound_version?.spec || '待登记' }}</dd></div>
            <div><dt>绑定版本/价格</dt><dd>{{ priceText(envelope.body.receipt) }}</dd></div>
            <div><dt>来源</dt><dd>{{ envelope.body.receipt.trace?.source_ref || envelope.body.receipt.bound_version?.source_ref || '原内存记录' }}</dd></div>
          </dl>
          <div v-if="envelope.body.receipt.correction" class="correction" data-testid="price-correction-warning">
            L8/基线更正已交接为 {{ envelope.body.receipt.correction.current_version_key }}
            （当前 {{ envelope.body.receipt.correction.current_price }}）；旧结果仍按绑定版本
            {{ envelope.body.receipt.correction.bound_version_key }} 的
            {{ envelope.body.receipt.correction.bound_price }} 计价，不会悄悄换价。
          </div>
          <div v-if="envelope.body.receipt.migration_comparison" class="comparison-box" data-testid="migration-comparison">
            回滚后比对：旧任务 {{ envelope.body.receipt.migration_comparison.old_task_source_key }}
            绑定 {{ envelope.body.receipt.migration_comparison.bound_version_key }} /
            {{ envelope.body.receipt.migration_comparison.bound_price }}；
            迁移基线当前为 {{ envelope.body.receipt.migration_comparison.migrated_current_version }} /
            {{ envelope.body.receipt.migration_comparison.migrated_current_price }}
            <span v-if="envelope.body.receipt.migration_comparison.price_changed">
              （差额 {{ envelope.body.receipt.migration_comparison.price_delta }}，旧任务不换价）
            </span>
          </div>
          <div v-if="(envelope.body.receipt.task?.blocking_missing || []).length" class="missing-box" data-testid="missing-basis">
            <strong>复算/继续前缺项：</strong>
            <span v-for="item in envelope.body.receipt.task.blocking_missing" :key="item" class="missing-tag">{{ item }}</span>
            <p>可查看原记录；补齐实际计价依据后才能复算。</p>
          </div>
          <details>
            <summary>查看完整实际回执 JSON</summary>
            <pre :data-testid="`${envelope.side}-receipt-json`">{{ JSON.stringify(envelope.body, null, 2) }}</pre>
          </details>
        </template>
        <pre v-else class="raw-json">{{ JSON.stringify(envelope.body, null, 2) }}</pre>
      </article>
    </div>
  </section>
</template>

<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'

import {
  fetchContinuation,
  migrationApi,
  migrationStatus,
  postContinuation,
  type HttpEnvelope,
} from '@/api/backend-service'

const props = defineProps<{ moduleKey: string }>()
const emit = defineEmits<{ changed: [] }>()

type Side = 'legacy' | 'new'
type EnvelopeState = { side: Side } & Partial<HttpEnvelope>

const batch = ref(new URLSearchParams(window.location.search).get('batch') || 'K')
const busy = ref(false)
const notice = ref('')
const errorMessage = ref('')
const status = ref<any>(null)
const basisText = ref('{\n  "baseline_version_key": "L8@v1",\n  "site_prices": [{ "site": "A站", "price": 35 }]\n}')
const envelopes = ref<EnvelopeState[]>([
  { side: 'legacy' },
  { side: 'new' },
])

const activeEntry = computed(() => status.value?.state?.active_entry || 'legacy')
const activeEntryText = computed(() => (activeEntry.value === 'new' ? '新入口 / SQLite' : '旧入口 / 内存兼容层'))
const currencyPending = computed(() => status.value?.baseline_items?.filter((x: any) => x.currency_status === 'missing_pending_check').length ?? 0)

function sideText(side?: string | null): string {
  if (side === 'legacy') return '旧入口'
  if (side === 'new') return '新入口'
  return '尚未受理'
}

function priceText(receipt: any): string {
  const result = receipt.result
  if (result?.baseline_version_key) {
    return `${result.baseline_version_key} / ${result.price} ${result.currency === '待核对' ? '（币种待核对）' : result.currency || ''}`
  }
  if (receipt.bound_version) {
    return `${receipt.bound_version.version_key} / ${receipt.bound_version.amount_decimal} ${receipt.bound_version.currency === '待核对' ? '（币种待核对）' : receipt.bound_version.currency || ''}`
  }
  return '未绑定，需登记计价依据'
}

async function refreshStatus() {
  status.value = await migrationStatus()
}

async function refreshReceipts() {
  errorMessage.value = ''
  notice.value = ''
  const currentBatch = batch.value.trim()
  if (!currentBatch) {
    errorMessage.value = '请输入比价批次，例如 K'
    return
  }
  const [legacy, next] = await Promise.all([
    fetchContinuation('legacy', currentBatch).catch((error): HttpEnvelope => ({
      httpStatus: 0,
      ok: false,
      body: { message: error instanceof Error ? error.message : '旧入口请求失败' },
    })),
    fetchContinuation('new', currentBatch).catch((error): HttpEnvelope => ({
      httpStatus: 0,
      ok: false,
      body: { message: error instanceof Error ? error.message : '新入口请求失败' },
    })),
  ])
  envelopes.value = [
    { side: 'legacy', ...legacy },
    { side: 'new', ...next },
  ]
}

async function runMigrationAction(
  action: 'start' | 'prepare' | 'switch' | 'rollback' | 'simulate-failure' | 'scenarios/k-l8',
  payload: Record<string, unknown> = {},
  success?: string,
) {
  busy.value = true
  errorMessage.value = ''
  notice.value = ''
  try {
    const envelope = await migrationApi(action, payload)
    const body = envelope.body || {}
    if (body.ok === false) {
      errorMessage.value = body.message || '迁移操作未成功'
    } else {
      notice.value = success || body.message || '操作完成'
    }
    await refreshStatus()
    await refreshReceipts()
    emit('changed')
    return envelope
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '迁移接口请求失败'
    return null
  } finally {
    busy.value = false
  }
}

function startMigration() {
  return runMigrationAction('start', { activate: true })
}

function prepareMigration() {
  return runMigrationAction('prepare', { activate: false })
}

function switchNew() {
  return runMigrationAction('switch', {})
}

function simulateFailback() {
  return runMigrationAction('simulate-failure', {}, '已模拟切换故障，两个续办回执均指向旧入口可继续的同一申请')
}

function loadKl8Drill() {
  batch.value = 'K'
  return runMigrationAction(
    'scenarios/k-l8',
    {
      reset: true,
      activate: true,
      continue_during_handoff: true,
      site_prices: [
        { site: 'A站', price: 35 },
        { site: 'B站', price: 39 },
      ],
    },
    'K/L8 演练已就绪：两个规格、一份未完成任务；L8 为 37→41，K 保留 37 绑定版本。',
  )
}

function parseBasis(): Record<string, unknown> {
  if (!basisText.value.trim()) return {}
  try {
    const parsed = JSON.parse(basisText.value) as unknown
    return parsed && typeof parsed === 'object' ? (parsed as Record<string, unknown>) : {}
  } catch {
    errorMessage.value = '计价依据不是合法 JSON，请检查后再提交'
    return {}
  }
}

async function claimBatch(side: Side) {
  busy.value = true
  errorMessage.value = ''
  try {
    const envelope = await postContinuation(side, batch.value.trim(), 'claim', {})
    if (envelope.body?.ok === false) {
      errorMessage.value = envelope.body.message || '领取冲突'
    } else {
      notice.value = envelope.body?.message || '已返回唯一受理回执'
    }
    await refreshReceipts()
    emit('changed')
  } finally {
    busy.value = false
  }
}

async function continueBatch(side: Side) {
  const payload = parseBasis()
  if (basisText.value.trim() && Object.keys(payload).length === 0 && errorMessage.value) return
  busy.value = true
  errorMessage.value = ''
  try {
    const envelope = await postContinuation(side, batch.value.trim(), 'continue', payload)
    if (envelope.body?.ok === false) {
      errorMessage.value = envelope.body.message || '仍有缺项，不能复算/交付'
    } else {
      notice.value = envelope.body?.message || '续办完成'
    }
    await refreshReceipts()
    emit('changed')
  } finally {
    busy.value = false
  }
}

onMounted(async () => {
  try {
    await refreshStatus()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '迁移状态读取失败'
  }
  await refreshReceipts()
})

function setBatch(value: string) {
  batch.value = value
}

defineExpose({ refreshReceipts, refreshStatus, setBatch })
</script>

<style scoped>
.handoff-panel {
  margin: 16px 0;
  padding: 16px;
  border: 1px solid #d8dee9;
  border-radius: 12px;
  background: #f8fafc;
}
.handoff-head {
  display: flex;
  justify-content: space-between;
  gap: 16px;
  align-items: flex-start;
}
.handoff-head h3 {
  margin: 0 0 6px;
  font-size: 18px;
}
.handoff-head p {
  margin: 0;
  color: #475569;
  line-height: 1.6;
}
.entry-badge {
  flex: none;
  padding: 6px 10px;
  border-radius: 999px;
  background: #e0f2fe;
  color: #075985;
  font-size: 13px;
  font-weight: 700;
}
.handoff-controls {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
  align-items: end;
  margin: 14px 0;
}
.handoff-controls.compact {
  margin: 8px 0 0;
}
.batch-input,
.basis-editor {
  display: flex;
  flex-direction: column;
  gap: 6px;
  font-size: 13px;
  font-weight: 700;
}
.batch-input input {
  width: 180px;
}
.batch-input input,
.basis-editor textarea {
  border: 1px solid #cbd5e1;
  border-radius: 8px;
  padding: 8px 10px;
  font: inherit;
  font-weight: 400;
}
.basis-editor {
  margin-top: 10px;
}
.basis-editor textarea {
  width: min(760px, 100%);
  font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
  font-size: 12px;
}
.migration-summary {
  display: flex;
  flex-wrap: wrap;
  gap: 12px;
  padding: 10px 12px;
  border-radius: 8px;
  background: #eef2ff;
  font-size: 13px;
}
.warning,
.correction,
.missing-tag {
  color: #9a3412;
}
.warning {
  font-weight: 700;
}
.handoff-notice {
  padding: 10px 12px;
  border-radius: 8px;
  background: #dcfce7;
  color: #166534;
}
.error-text {
  color: #b91c1c;
  font-weight: 700;
  white-space: pre-wrap;
}
.receipt-grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(320px, 1fr));
  gap: 12px;
  margin-top: 12px;
}
.receipt-card {
  background: white;
  border: 1px solid #cbd5e1;
  border-radius: 10px;
  padding: 12px;
  min-width: 0;
}
.receipt-card[data-kind='confirmed_result'] {
  border-color: #16a34a;
}
.receipt-card[data-kind='actionable_todo'] {
  border-color: #d97706;
}
.receipt-card header {
  display: flex;
  justify-content: space-between;
  align-items: center;
  gap: 8px;
}
.receipt-card h4 {
  margin: 0;
}
.receipt-kind {
  display: inline-block;
  margin: 8px 0;
  padding: 4px 8px;
  border-radius: 999px;
  font-size: 12px;
  font-weight: 800;
  background: #fef3c7;
  color: #92400e;
}
.receipt-card[data-kind='confirmed_result'] .receipt-kind {
  background: #dcfce7;
  color: #166534;
}
.explanation {
  line-height: 1.6;
  color: #334155;
}
.receipt-meta {
  display: grid;
  gap: 6px;
  margin: 10px 0;
}
.receipt-meta div {
  display: grid;
  grid-template-columns: 96px 1fr;
  gap: 8px;
  font-size: 13px;
}
.receipt-meta dt {
  color: #64748b;
}
.receipt-meta dd {
  margin: 0;
  overflow-wrap: anywhere;
}
.correction,
.missing-box,
.comparison-box {
  padding: 8px;
  border-radius: 8px;
  background: #fff7ed;
  font-size: 13px;
  line-height: 1.6;
  margin-top: 6px;
}
.comparison-box {
  background: #eff6ff;
  color: #1e3a8a;
}
.missing-tag {
  display: inline-block;
  margin: 4px 4px 0 0;
  padding: 2px 6px;
  border-radius: 6px;
  background: #ffedd5;
}
pre {
  overflow: auto;
  max-height: 280px;
  background: #0f172a;
  color: #e2e8f0;
  padding: 10px;
  border-radius: 8px;
  font-size: 12px;
}
.raw-json {
  color: #e2e8f0;
}
</style>
