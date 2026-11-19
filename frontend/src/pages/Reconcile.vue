<template>
  <div>
    <div class="hero">
      <div>
        <h1>三联对账</h1>
        <p>归并键恒为「供应商 + 货号」，严禁跨供应商数量加总。
          别名归并下映射失败进 alias_miss 桶；未决差额与未映射清零后方可结案。</p>
      </div>
    </div>
    <div class="panel">
      <div class="row">
        <input v-model="day" placeholder="到货日" />
        <button @click="run">跑三联对账</button>
        <button class="warn" @click="seal">结案封印</button>
        <button class="secondary" @click="load">刷新</button>
        <span class="tag seal">当前口径：{{ modeText(currentMode) }}</span>
      </div>
      <p class="msg">{{ err }}</p>
      <p class="okmsg">{{ ok }}</p>
    </div>

    <div class="panel">
      <h2>当日批次（切换架构后的新旧批次留存对照）</h2>
      <table>
        <thead>
          <tr>
            <th>批次号</th><th>口径</th><th>alias 版本</th><th>对齐</th><th>归并键</th>
            <th>别名命中</th><th>缺映射</th><th>差额</th><th>状态</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="b in batches" :key="b.id" :class="{ picked: batch && b.id === batch.id }"
              style="cursor: pointer" @click="pick(b)">
            <td>{{ b.batch_no }}</td>
            <td><span class="tag" :class="b.mode === 'alias_merge' ? 'ok' : 'seal'">{{ modeText(b.mode) }}</span></td>
            <td>{{ b.alias_version ?? '—' }}</td>
            <td>{{ b.matched_count }}</td>
            <td>{{ b.mapped_count }}</td>
            <td>{{ b.alias_hit_count }}</td>
            <td><span :class="b.alias_miss_count ? 'tag bad' : ''">{{ b.alias_miss_count }}</span></td>
            <td>{{ b.variance_count }}</td>
            <td>
              <span class="tag" :class="statusClass(b.status)">{{ statusText(b.status) }}</span>
            </td>
          </tr>
        </tbody>
      </table>
    </div>

    <div class="panel" v-if="batch">
      <h2>
        对齐行 · {{ batch.batch_no }}
        <span class="tag" :class="batch.mode === 'alias_merge' ? 'ok' : 'seal'">{{ modeText(batch.mode) }}</span>
        <span class="tag seal" v-if="batch.alias_version">钉版 v{{ batch.alias_version }}</span>
      </h2>
      <table>
        <thead>
          <tr><th>供应商</th><th>归并货号</th><th>原始码留痕</th><th>预报</th><th>实收</th><th>回执</th><th>状态</th></tr>
        </thead>
        <tbody>
          <tr v-for="r in matches" :key="r.id">
            <td>{{ r.supplier_code }}</td>
            <td>{{ r.sku }}</td>
            <td class="raw">{{ detailText(r.raw_detail) }}</td>
            <td>{{ r.expect_qty }}</td><td>{{ r.receive_qty }}</td><td>{{ r.confirm_qty }}</td>
            <td><span class="tag" :class="r.aligned ? 'ok' : 'bad'">{{ r.aligned ? '对齐' : '差额' }}</span></td>
          </tr>
        </tbody>
      </table>
    </div>

    <div class="panel" v-if="batch && batch.mode === 'alias_merge'">
      <h2>alias_miss 缺映射桶（绝不进 matched）</h2>
      <p v-if="!misses.length" class="okmsg">无缺映射。</p>
      <table v-else>
        <thead><tr><th>供应商</th><th>腿</th><th>外部码</th><th>数量</th><th>原因</th></tr></thead>
        <tbody>
          <tr v-for="r in misses" :key="r.id">
            <td>{{ r.supplier_code }}</td><td>{{ legText(r.leg) }}</td><td>{{ r.raw_sku }}</td>
            <td>{{ r.qty }}</td><td>{{ r.reason }}</td>
          </tr>
        </tbody>
      </table>
      <p v-if="misses.length" class="msg">
        处置方式：到「别名维护」补映射并重新跑对账；存在缺映射时结案写闸会拒绝封印。
      </p>
    </div>

    <div class="panel" v-if="batch">
      <h2>差额处置</h2>
      <p v-if="batch.status !== 'open'" class="msg">
        该批次状态为「{{ statusText(batch.status) }}」，物化结果只读，差额命运不可再改。
      </p>
      <table>
        <thead><tr><th>供应商</th><th>货号</th><th>原始码</th><th>三数量</th><th>命运</th><th>操作</th></tr></thead>
        <tbody>
          <tr v-for="r in variances" :key="r.id">
            <td>{{ r.supplier_code }}</td><td>{{ r.sku }}</td>
            <td class="raw">{{ detailText(r.raw_detail) }}</td>
            <td>{{ r.expect_qty }} / {{ r.receive_qty }} / {{ r.confirm_qty }}</td>
            <td>{{ r.fate }}</td>
            <td>
              <button class="secondary" :disabled="batch.status !== 'open'"
                      @click="fate(r.id, 'accept_short')">认短</button>
              <button class="secondary" :disabled="batch.status !== 'open'"
                      @click="fate(r.id, 'accept_over')">认溢</button>
              <button class="secondary" :disabled="batch.status !== 'open'"
                      @click="fate(r.id, 'quarantine')">隔离</button>
            </td>
          </tr>
        </tbody>
      </table>
    </div>
  </div>
</template>
<script setup>
import { ref, onMounted } from 'vue'
import { api } from '../api'
const day = ref('2026-11-18')
const currentMode = ref('alias_merge')
const batches = ref([])
const batch = ref(null)
const matches = ref([])
const variances = ref([])
const misses = ref([])
const err = ref('')
const ok = ref('')

function modeText(m) { return m === 'alias_merge' ? '别名归并' : (m === 'strict' ? '严格码' : m) }
function legText(l) { return { expect: '预报', receive: '实收', confirm: '回执' }[l] || l }
function statusText(s) {
  return { open: '进行中', sealed: '已封批', superseded: '已被新批取代' }[s] || s
}
function statusClass(s) { return s === 'sealed' ? 'seal' : (s === 'open' ? 'ok' : 'bad') }
function detailText(d) {
  if (!d) return ''
  try {
    const obj = JSON.parse(d)
    return ['expect', 'receive', 'confirm']
      .map((leg) => `${legText(leg)}: ${(obj[leg] || []).join(', ') || '—'}`)
      .join(' ｜ ')
  } catch { return d }
}

async function load() {
  err.value = ''
  try {
    const settings = await api('/settings')
    currentMode.value = settings.reconcile_mode
    batches.value = await api(`/reconcile/batches?dock_day=${encodeURIComponent(day.value)}`)
    const open = batches.value.find((b) => b.status === 'open') || batches.value[0]
    if (open) await pick(open)
    else { batch.value = null; matches.value = []; variances.value = []; misses.value = [] }
  } catch (e) { err.value = e.message }
}
async function pick(b) {
  batch.value = b
  matches.value = await api(`/reconcile/matches?batch_id=${b.id}`)
  variances.value = await api(`/reconcile/variances?batch_id=${b.id}`)
  misses.value = b.mode === 'alias_merge'
    ? await api(`/reconcile/alias-miss?batch_id=${b.id}`) : []
}
async function run() {
  err.value = ''; ok.value = ''
  try {
    batch.value = await api('/reconcile/run', { method: 'POST', body: JSON.stringify({ dock_day: day.value }) })
    ok.value = '对账完成（旧 open 批次已标记 superseded 留存）'
    await load()
  } catch (e) { err.value = e.message }
}
async function seal() {
  err.value = ''; ok.value = ''
  try {
    batch.value = await api('/reconcile/seal', { method: 'POST', body: JSON.stringify({ dock_day: day.value }) })
    ok.value = '已结案封印'
    await load()
  } catch (e) { err.value = e.message }
}
async function fate(id, f) {
  err.value = ''; ok.value = ''
  try {
    await api(`/reconcile/variances/${id}/fate`, { method: 'POST', body: JSON.stringify({ fate: f }) })
    ok.value = '已更新命运'
    await load()
  } catch (e) { err.value = e.message }
}
onMounted(load)
</script>
<style scoped>
tr.picked { outline: 2px solid var(--accent); outline-offset: -2px; }
td.raw { font-size: 0.8rem; color: var(--muted); max-width: 320px; }
</style>
