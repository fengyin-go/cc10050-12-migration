<template>
  <section class="page" :data-module="meta.key">
    <header class="page-head">
      <div>
        <h2>{{ meta.name }}</h2>
        <p class="page-desc">{{ meta.desc }}</p>
      </div>
      <div class="page-actions">
        <button class="btn primary" data-testid="open-create" type="button" @click="openCreate">
          登记{{ meta.entity }}
        </button>
        <button class="btn" data-testid="export" type="button" @click="exportRows">
          导出{{ meta.name }}清单
        </button>
      </div>
    </header>

    <MigrationHandoffPanel
      v-if="isHandoffModule"
      ref="handoffPanelRef"
      :module-key="meta.key"
      @changed="reload"
    />

    <div v-if="recomputeFeedback" class="recompute-feedback" data-testid="recompute-feedback">
      <div class="recompute-head">
        <strong>{{ recomputeFeedback.ok ? '复算结果' : '复算前缺项提示' }}</strong>
        <button class="link" type="button" data-testid="close-recompute-feedback" @click="recomputeFeedback = null">关闭</button>
      </div>
      <p :class="{ 'error-text': !recomputeFeedback.ok }">{{ recomputeFeedback.message }}</p>
      <div v-if="recomputeFeedback.missing?.length" class="missing-line" data-testid="recompute-missing">
        缺项：{{ recomputeFeedback.missing.join('、') }}
      </div>
      <details v-if="recomputeFeedback.original_record">
        <summary>查看原记录</summary>
        <pre>{{ JSON.stringify(recomputeFeedback.original_record, null, 2) }}</pre>
      </details>
    </div>

    <div class="stat-row">
      <article v-for="item in stats" :key="item.label" class="stat-card">
        <span class="stat-label">{{ item.label }}</span>
        <strong class="stat-value">{{ item.value }}</strong>
      </article>
    </div>

    <p class="status-legend">
      <span v-for="item in statusSummary" :key="item.status" class="legend-item">
        {{ item.status }}：{{ item.count }}
      </span>
    </p>

    <form class="filter-bar" @submit.prevent="reload">
      <label v-for="field in filterFields" :key="field" class="filter-item">
        <span>{{ field }}</span>
        <input v-model="filters[field]" :data-testid="`filter-${field}`" :placeholder="`按${field}检索`" />
      </label>
      <button class="btn" data-testid="query" type="submit">查询</button>
      <button class="btn ghost" data-testid="reset" type="button" @click="resetFilters">重置条件</button>
    </form>

    <table class="data-table">
      <thead>
        <tr>
          <th v-for="column in meta.fields" :key="column">{{ column }}</th>
          <th>当前状态</th>
          <th>可执行动作</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="row in rows" :key="String(row.id)" :data-testid="`row-${row.id}`" :data-status="String(row.status)">
          <td v-for="column in meta.fields" :key="column">{{ row[column] ?? '—' }}</td>
          <td>
            <span>{{ row.status }}</span>
            <span
              v-if="row.currency_status === 'missing_pending_check'"
              class="inline-warning"
              data-testid="currency-pending"
            >币种待核对</span>
            <span
              v-if="meta.key === 'price_compare' && row.basis_complete === false && row.status !== '已计算'"
              class="inline-warning"
              data-testid="basis-pending"
            >计价依据待登记</span>
          </td>
          <td class="row-actions">
            <button
              v-for="action in meta.actions"
              :key="action"
              class="link"
              type="button"
              :data-testid="`action-${row.id}-${action}`"
              @click="runAction(action, row)"
            >
              {{ action }}
            </button>
            <button
              v-if="meta.key === 'price_compare'"
              class="link"
              type="button"
              :data-testid="`register-basis-${row.id}`"
              @click="openBasisModal(row)"
            >
              登记计价依据
            </button>
            <button
              v-if="meta.key === 'price_compare'"
              class="link"
              type="button"
              :data-testid="`recompute-${row.id}`"
              @click="recomputeBaseline(row)"
            >
              复算基线
            </button>
            <button
              v-if="meta.key === 'price_baseline'"
              class="link"
              type="button"
              :data-testid="`correct-price-${row.id}`"
              @click="openCorrectionModal(row)"
            >
              更正价格
            </button>
            <button
              v-if="meta.key === 'price_compare'"
              class="link"
              type="button"
              :data-testid="`open-continuation-${row.id}`"
              @click="openInPanel(row)"
            >
              续办回执
            </button>
          </td>
        </tr>
        <tr v-if="!rows.length">
          <td :colspan="meta.fields.length + 2" class="empty-state">
            暂无{{ meta.name }}数据，可先登记{{ meta.entity }}
          </td>
        </tr>
      </tbody>
    </table>

    <div v-if="showCreate" class="modal-backdrop" data-testid="create-modal">
      <form class="modal" @submit.prevent="submitCreate">
        <h3>登记{{ meta.entity }}</h3>
        <label v-for="field in meta.fields" :key="field" class="create-field">
          <span>{{ field }}</span>
          <input v-model="createValues[field]" :data-testid="`create-${field}`" required />
        </label>
        <div class="page-actions">
          <button class="btn" type="button" @click="showCreate = false">取消</button>
          <button class="btn primary" data-testid="submit-create" type="submit" :disabled="creating">
            {{ creating ? '提交中…' : '确认登记' }}
          </button>
        </div>
      </form>
    </div>

    <div v-if="showBasisModal" class="modal-backdrop" data-testid="basis-modal">
      <form class="modal wide-modal" @submit.prevent="submitBasis">
        <h3>为 {{ basisTarget?.['比价批次'] || basisTarget?.id }} 登记计价依据</h3>
        <p class="modal-tip">
          可填写绑定的 <code>baseline_version_key</code>、<code>site_prices</code>、来源、样本与公式；缺项会在复算前明确提示。
        </p>
        <label class="create-field">
          <span>计价依据 JSON</span>
          <textarea
            v-model="basisText"
            data-testid="basis-payload-input"
            class="json-input"
            rows="10"
            spellcheck="false"
          />
        </label>
        <p v-if="modalError" class="error-text" data-testid="basis-modal-error">{{ modalError }}</p>
        <div class="page-actions">
          <button class="btn" type="button" @click="showBasisModal = false">取消</button>
          <button class="btn primary" data-testid="submit-basis" type="submit" :disabled="submittingModal">
            {{ submittingModal ? '提交中…' : '确认登记依据' }}
          </button>
        </div>
      </form>
    </div>

    <div v-if="showCorrectionModal" class="modal-backdrop" data-testid="correction-modal">
      <form class="modal" @submit.prevent="submitCorrection">
        <h3>更正基线价格 #{{ correctionTarget?.id }}</h3>
        <label class="create-field">
          <span>新价格</span>
          <input v-model="correctionPrice" data-testid="correction-price-input" inputmode="decimal" required />
        </label>
        <label class="create-field">
          <span>更正原因/核价单</span>
          <input v-model="correctionReason" data-testid="correction-reason-input" placeholder="例如：核价单 L8-41" />
        </label>
        <p v-if="modalError" class="error-text" data-testid="correction-modal-error">{{ modalError }}</p>
        <div class="page-actions">
          <button class="btn" type="button" @click="showCorrectionModal = false">取消</button>
          <button class="btn primary" data-testid="submit-correction" type="submit" :disabled="submittingModal">
            {{ submittingModal ? '提交中…' : '确认更正并生成新版本' }}
          </button>
        </div>
      </form>
    </div>

    <footer class="page-foot">
      <span>共 {{ total }} 条{{ meta.name }}记录</span>
      <span v-if="errorMessage" class="error-text">{{ errorMessage }}</span>
    </footer>
  </section>
</template>

<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'

import {
  correctBaseline,
  createEntry,
  downloadEntries,
  listEntries,
  moduleMeta,
  recomputeCompareBaseline,
  registerCompareBasis,
  runAction as applyAction,
} from '@/api/backend-service'
import MigrationHandoffPanel from '@/components/MigrationHandoffPanel.vue'
import type { EntryRow } from '@/data/types'

const props = defineProps<{ moduleKey: string }>()
const meta = moduleMeta(props.moduleKey)
const isHandoffModule = computed(() => ['price_baseline', 'price_compare'].includes(meta.key))
const handoffPanelRef = ref<InstanceType<typeof MigrationHandoffPanel> | null>(null)
const recomputeFeedback = ref<any>(null)
const showBasisModal = ref(false)
const basisTarget = ref<EntryRow | null>(null)
const basisText = ref('{}')
const showCorrectionModal = ref(false)
const correctionTarget = ref<EntryRow | null>(null)
const correctionPrice = ref('')
const correctionReason = ref('当次基线更正')
const submittingModal = ref(false)
const modalError = ref('')
const rows = ref<EntryRow[]>([])
const total = ref(0)
const errorMessage = ref('')
const filters = ref<Record<string, string>>({})
const filterFields = meta.fields.slice(0, 3)
const showCreate = ref(false)
const creating = ref(false)
const createValues = ref<Record<string, string>>({})
const stats = computed(() => [
  { label: meta.metrics[0], value: total.value },
  { label: meta.metrics[1], value: rows.value.filter((row) => row.status === meta.statuses[1]).length },
  {
    label: meta.metrics[2],
    value: rows.value.filter((row) => row.status === meta.statuses[meta.statuses.length - 1]).length,
  },
])
const statusSummary = computed(() =>
  meta.statuses.map((status) => ({
    status,
    count: rows.value.filter((row) => String(row.status) === status).length,
  })),
)

function resetFilters() {
  filters.value = {}
  void reload()
}

async function exportRows() {
  errorMessage.value = ''
  try {
    await downloadEntries(meta.key, filters.value)
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '导出失败'
  }
}

function openCreate() {
  createValues.value = Object.fromEntries(meta.fields.map((field) => [field, '']))
  errorMessage.value = ''
  showCreate.value = true
}

async function submitCreate() {
  creating.value = true
  errorMessage.value = ''
  try {
    const result = await createEntry(meta.key, createValues.value)
    if (!result.ok) throw new Error(result.message)
    showCreate.value = false
    await reload()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '登记失败'
  } finally {
    creating.value = false
  }
}

async function runAction(action: string, row: EntryRow) {
  errorMessage.value = ''
  recomputeFeedback.value = null
  try {
    const result = await applyAction(meta.key, Number(row.id), action)
    if (!result.ok) {
      const missing = Array.isArray((result as any).missing) ? (result as any).missing : []
      throw new Error(missing.length ? `${result.message}；缺项：${missing.join('、')}` : result.message)
    }
    await reload()
    await handoffPanelRef.value?.refreshReceipts()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '操作失败'
  }
}

function openBasisModal(row: EntryRow) {
  basisTarget.value = row
  modalError.value = ''
  const version = row.baseline_version_key ? String(row.baseline_version_key) : ''
  basisText.value = JSON.stringify({
    ...(version ? { baseline_version_key: version } : {}),
    source: row['来源'] ? String(row['来源']) : '',
    site_prices: [{ site: 'A站', price: '' }],
  }, null, 2)
  showBasisModal.value = true
}

async function submitBasis() {
  if (!basisTarget.value) return
  modalError.value = ''
  let payload: Record<string, unknown>
  try {
    const parsed = JSON.parse(basisText.value) as unknown
    if (!parsed || typeof parsed !== 'object') throw new Error('根节点必须是 JSON 对象')
    payload = parsed as Record<string, unknown>
  } catch (error) {
    modalError.value = error instanceof Error ? `JSON 不合法：${error.message}` : 'JSON 不合法'
    return
  }
  submittingModal.value = true
  try {
    const result = await registerCompareBasis(basisTarget.value.id, payload)
    if (!result.ok) {
      modalError.value = result.message || '计价依据登记失败'
      return
    }
    showBasisModal.value = false
    recomputeFeedback.value = result
    await reload()
    await handoffPanelRef.value?.refreshReceipts()
  } finally {
    submittingModal.value = false
  }
}

function openCorrectionModal(row: EntryRow) {
  correctionTarget.value = row
  correctionPrice.value = String(row['基线价格'] ?? '')
  correctionReason.value = '当次基线更正'
  modalError.value = ''
  showCorrectionModal.value = true
}

async function submitCorrection() {
  if (!correctionTarget.value) return
  if (!correctionPrice.value.trim() || Number.isNaN(Number(correctionPrice.value))) {
    modalError.value = '新价格必须是数值'
    return
  }
  submittingModal.value = true
  modalError.value = ''
  try {
    const result = await correctBaseline(correctionTarget.value.id, {
      new_price: Number(correctionPrice.value),
      reason: correctionReason.value,
    })
    if (!result.ok) {
      modalError.value = result.message || '更正失败'
      return
    }
    showCorrectionModal.value = false
    recomputeFeedback.value = result
    await reload()
    await handoffPanelRef.value?.refreshStatus()
  } finally {
    submittingModal.value = false
  }
}

async function recomputeBaseline(row: EntryRow) {
  errorMessage.value = ''
  try {
    const result = await recomputeCompareBaseline(row.id)
    recomputeFeedback.value = result
    if (!result.ok) {
      errorMessage.value = result.message || '缺少实际计算依据，不能复算'
    }
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '复算接口请求失败'
  }
}

function openInPanel(row: EntryRow) {
  const batch = String(row['比价批次'] || '')
  if (batch) handoffPanelRef.value?.setBatch(batch)
  void handoffPanelRef.value?.refreshReceipts()
  window.scrollTo({ top: 0, behavior: 'smooth' })
}

async function reload() {
  errorMessage.value = ''
  try {
    const payload = await listEntries(meta.key, filters.value)
    rows.value = payload.items
    total.value = payload.total
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '业务列表读取失败'
  }
}

onMounted(reload)
</script>

<style scoped>
.inline-warning {
  display: inline-block;
  margin-left: 6px;
  padding: 2px 6px;
  border-radius: 6px;
  background: #ffedd5;
  color: #9a3412;
  font-size: 12px;
  font-weight: 700;
  white-space: nowrap;
}
.recompute-feedback {
  margin: 12px 0;
  padding: 12px;
  border: 1px solid #fdba74;
  border-radius: 10px;
  background: #fff7ed;
}
.recompute-head {
  display: flex;
  justify-content: space-between;
  align-items: center;
}
.missing-line {
  margin: 8px 0;
  color: #9a3412;
  font-weight: 700;
}
.recompute-feedback pre {
  max-height: 240px;
  overflow: auto;
  padding: 10px;
  border-radius: 8px;
  background: #0f172a;
  color: #e2e8f0;
  font-size: 12px;
}
.wide-modal {
  width: min(760px, calc(100vw - 32px));
}
.json-input {
  width: 100%;
  border: 1px solid #cbd5e1;
  border-radius: 8px;
  padding: 10px;
  font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
  font-size: 12px;
}
.modal-tip {
  color: #475569;
  line-height: 1.6;
}
.modal-tip code {
  background: #f1f5f9;
  padding: 1px 4px;
  border-radius: 4px;
}
</style>
